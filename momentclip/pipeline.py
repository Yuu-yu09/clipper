"""End-to-end pipeline: source video in, ranked moments + rendered clips out."""

from __future__ import annotations

import json
import logging
import hashlib
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from . import asr, captions, candidates, ffmpeg, momentgraph, rank, render, signals, soundtrack
from .config import Config, get_config, resolve_clip_length
from .soundtrack import MixSpec

log = logging.getLogger("momentclip.pipeline")


class PipelineError(RuntimeError):
    pass


@dataclass
class ClipResult:
    id: str
    start: float
    end: float
    duration: float
    score: float
    reason: str
    tag: str
    text: str
    video: str | None = None
    captions: str | None = None
    render_notes: list[str] = field(default_factory=list)
    features: dict = field(default_factory=dict)
    graph_reasons: list[str] = field(default_factory=list)
    music: str | None = None
    sfx: list[str] = field(default_factory=list)
    zoom: float = 1.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "score": round(self.score, 4),
            "reason": self.reason,
            "tag": self.tag,
            "text": self.text,
            "video": self.video,
            "captions": self.captions,
            "render_notes": self.render_notes,
            "features": {k: round(v, 4) for k, v in self.features.items()},
            "graph_reasons": self.graph_reasons,
            "music": self.music,
            "sfx": self.sfx,
            "zoom": self.zoom,
        }


@dataclass
class AnalysisResult:
    source: str
    duration: float
    width: int
    height: int
    transcript_backend: str
    moments: int
    events: dict
    graph: dict
    signals: dict
    has_llm: bool
    # kept for the renderer so SFX can be placed on the same peaks that
    # scored the clip in the first place
    event_list: list = field(default_factory=list)
    track: dict = field(default_factory=dict)


def download(url: str, dest_dir: Path, cfg: Config) -> Path:
    """Fetch a remote video with yt-dlp if present."""
    exe = shutil.which("yt-dlp")
    if not exe:
        raise PipelineError(
            "yt-dlp is not installed.\n"
            "Install it with:  pip install yt-dlp\n"
            "or upload a file directly instead of pasting a URL."
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = hashlib_name(url)
    out_tmpl = str(dest_dir / f"{stem}.%(ext)s")
    proc = subprocess.run(
        [exe, "-f", "bv*+ba/b", "--merge-output-format", "mp4",
         "--no-playlist", "-o", out_tmpl, url],
        capture_output=True, text=True, timeout=7200,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise PipelineError("yt-dlp failed:\n" + "\n".join(tail))
    for p in sorted(dest_dir.glob(f"{stem}.*")):
        if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}:
            return p
    raise PipelineError("yt-dlp produced no video file")


def hashlib_name(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
def analyse(
    source: str | Path,
    workdir: Path,
    *,
    persona: str = "general",
    use_llm: bool = True,
    use_graph: bool = True,
    min_clip: float | None = None,
    max_clip: float | None = None,
    cfg: Config | None = None,
    progress=None,
) -> tuple[AnalysisResult, list[rank.Ranked]]:
    cfg = cfg or get_config()
    source = Path(source)
    workdir.mkdir(parents=True, exist_ok=True)

    if min_clip is not None or max_clip is not None:
        cfg = replace(cfg, min_clip=min_clip if min_clip is not None else cfg.min_clip,
                      max_clip=max_clip if max_clip is not None else cfg.max_clip)

    def step(pct: int, msg: str) -> None:
        log.info("[%3d%%] %s", pct, msg)
        if progress:
            progress(pct, msg)

    step(3, "probing source")
    info = ffmpeg.probe(source)
    if not info.duration or info.duration <= 0:
        raise PipelineError("could not determine video duration")
    if not info.has_audio and not info.has_video:
        raise PipelineError("source has neither video nor audio")

    step(8, "extracting audio for signal analysis")
    audio = workdir / "audio16k.wav"
    transcript = asr.Transcript()
    if info.has_audio:
        ffmpeg.extract_audio(source, audio, cfg.analysis_sr)
        step(18, "analysing audio signals")
        track, events, dur = signals.analyse_audio(audio, cfg.frame_hz)
        step(30, "transcribing")
        transcript = asr.transcribe(audio, cfg)
    else:
        step(30, "no audio stream, skipping transcription")
        track = {k: np.zeros(0) for k in
                 ("t", "rms_db", "zcr", "centroid", "flatness", "f0", "peak")}
        events, dur = [], 0.0

    duration = info.duration

    step(40, "generating candidate moments")
    moments = candidates.generate(transcript, events, track, duration, cfg)
    if not moments:
        raise PipelineError("no candidate moments could be generated")

    graph_summary: dict = {"backend": "disabled"}
    if use_graph:
        step(52, "building moment graph")
        try:
            G = momentgraph.build(moments)
            momentgraph.annotate(moments, G)
            graph_summary = momentgraph.summarise(moments, G)
        except Exception as exc:  # noqa: BLE001
            log.warning("graph stage failed: %s", exc)
            graph_summary = {"backend": "error", "error": str(exc)}
            for m in moments:
                m.features.setdefault("graph_centrality", 0.0)
                m.features.setdefault("graph_surprise", 0.0)
                m.features.setdefault("lexical_novelty", 0.5)

    step(66, "ranking moments")
    ranked = rank.rank(moments, persona=persona, cfg=cfg, use_llm=use_llm)
    ranked = rank.select(ranked, cfg.max_clips, cfg)
    ranked.sort(key=lambda r: -r.final_score)

    step(78, "writing analysis artefacts")
    transcript.save(workdir / "transcript.json")
    _write_signal_report(workdir / "signals.json", track, events, duration)
    (workdir / "graph.json").write_text(json.dumps({
        "moments": [m.to_dict() for m in moments],
        "summary": graph_summary,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    event_counts: dict[str, int] = {}
    for e in events:
        event_counts[e.kind] = event_counts.get(e.kind, 0) + 1

    result = AnalysisResult(
        source=str(source),
        duration=duration,
        width=info.width,
        height=info.height,
        transcript_backend=transcript.backend,
        moments=len(moments),
        events=event_counts,
        graph=graph_summary,
        signals={
            "frame_hz": cfg.frame_hz,
            "analysis_seconds": round(dur, 2),
            "peak_loudness_db": round(float(track["rms_db"].max()), 2)
            if len(track.get("rms_db", [])) else None,
        },
        has_llm=cfg.has_llm,
        event_list=events,
        track=track,
    )
    return result, ranked


def _write_signal_report(path: Path, track: dict, events: list, duration: float) -> None:
    step = max(1, int(track.get("t", [0]).shape[0] / 400)) if len(track.get("t", [])) else 1
    series = []
    t = track.get("t", [])
    for i in range(0, len(t), step):
        series.append({
            "t": round(float(t[i]), 2),
            "rms_db": round(float(track["rms_db"][i]), 2),
            "f0": round(float(track["f0"][i]), 1),
            "flatness": round(float(track["flatness"][i]), 4),
        })
    path.write_text(json.dumps({
        "duration": round(duration, 2),
        "series": series,
        "events": [
            {"start": round(e.start, 2), "end": round(e.end, 2),
             "kind": e.kind, "strength": round(e.strength, 3)}
            for e in events
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def export(
    ranked: list[rank.Ranked],
    source: str | Path,
    outdir: Path,
    *,
    aspect: str = "9:16",
    caption_style: str = "pop",
    burn_captions: bool = True,
    reframe: bool = True,
    auto_zoom: bool = True,
    music_mood: str = "upbeat",
    music_gain: float = 0.16,
    auto_sfx: bool = True,
    auto_hook: bool = False,
    track: dict | None = None,
    events: list | None = None,
    cfg: Config | None = None,
    progress=None,
) -> list[ClipResult]:
    cfg = cfg or get_config()
    outdir.mkdir(parents=True, exist_ok=True)
    results: list[ClipResult] = []
    total = len(ranked)
    events = events or []

    for i, r in enumerate(ranked, 1):
        m = r.moment
        pct = 80 + int(20 * (i - 1) / max(1, total))
        if progress:
            progress(pct, f"rendering clip {i}/{total}")

        dest = outdir / f"clip_{m.id}.mp4"

        # --- audio: music bed + comedy SFX placed on this clip's peaks ---
        mix = soundtrack.MixSpec(music_gain=music_gain, duck=True)
        sfx: list[tuple[float, str]] = []
        if music_mood and music_mood != "none":
            bed = soundtrack.build_music(m.end - m.start, music_mood, cfg,
                                         workdir=outdir / "_music")
            mix.music = bed
        if auto_sfx and events:
            inside = [e for e in events if e.start < m.end and e.end > m.start]
            local = [type("E", (), {"start": e.start - m.start, "end": e.end - m.start,
                                    "kind": e.kind, "strength": e.strength})()
                     for e in inside]
            sfx = soundtrack.pick_stingers(local, m.end - m.start, limit=2)
            paths = []
            for _off, kind in sfx:
                sp = outdir / "_music" / f"sfx_{kind}_{m.id}.wav"
                soundtrack.generate_stinger(kind, sp)
                paths.append(sp)
            mix.stinger_paths = paths

        headline = ""
        if auto_hook and m.text:
            headline = _hook_line(m.text)

        try:
            res = render.render_clip(
                source, m.start, m.end, dest,
                words=m.words, aspect=aspect, caption_style=caption_style,
                headline=headline,
                want_captions=burn_captions, want_reframe=reframe,
                want_zoom=auto_zoom,
                music=mix if mix.music else None, stingers=sfx if sfx else None,
                cfg=cfg,
            )
            srt = dest.with_suffix(".srt")
            results.append(ClipResult(
                id=m.id, start=m.start, end=m.end, duration=res.duration,
                score=r.final_score, reason=r.reason, tag=m.tag, text=m.text,
                video=str(dest),
                captions=str(srt) if srt.exists() else None,
                render_notes=res.notes,
                features={**r.subscores, **{k: v for k, v in m.features.items()
                                           if k.startswith(("graph_", "lexical_"))}},
                graph_reasons=m.graph_reasons,
                music=res.music, sfx=res.sfx, zoom=res.zoom,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning("clip %s render failed: %s", m.id, exc)
            results.append(ClipResult(
                id=m.id, start=m.start, end=m.end, duration=m.end - m.start,
                score=r.final_score, reason=r.reason, tag=m.tag, text=m.text,
                video=None, render_notes=[f"render failed: {exc}"],
                features=r.subscores, graph_reasons=m.graph_reasons,
            ))

    if progress:
        progress(100, "done")
    return results


def _hook_line(text: str, max_words: int = 7) -> str:
    """Pull a short hook from the clip's own opening words."""
    words = text.split()
    for n in (max_words, 6, 5, 4, 3):
        if len(words) >= n:
            return " ".join(words[:n])
    return " ".join(words)


def run(
    source: str | Path,
    workdir: Path,
    outdir: Path,
    *,
    persona: str = "general",
    aspect: str = "9:16",
    caption_style: str = "pop",
    clip_length: str = "auto",
    burn_captions: bool = True,
    reframe: bool = True,
    auto_zoom: bool = True,
    music: str = "upbeat",
    music_gain: float = 0.16,
    auto_sfx: bool = True,
    auto_hook: bool = False,
    use_llm: bool = True,
    use_graph: bool = True,
    cfg: Config | None = None,
    progress=None,
) -> dict:
    t0 = time.time()
    cfg = cfg or get_config()

    lo, hi = resolve_clip_length(clip_length)
    lo = max(cfg.min_clip, lo)
    hi = max(lo, min(cfg.max_clip if clip_length == "auto" else hi, cfg.max_clip))

    analysis, ranked = analyse(
        source, workdir, persona=persona, use_llm=use_llm,
        use_graph=use_graph, cfg=cfg, progress=progress,
        min_clip=lo, max_clip=hi,
    )
    clips = export(
        ranked, source, outdir, aspect=aspect, caption_style=caption_style,
        burn_captions=burn_captions, reframe=reframe, auto_zoom=auto_zoom,
        music_mood=music, music_gain=music_gain, auto_sfx=auto_sfx,
        auto_hook=auto_hook,
        track=getattr(analysis, "track", None),
        events=getattr(analysis, "event_list", None),
        cfg=cfg, progress=progress,
    )
    return {
        "analysis": {
            "source": analysis.source,
            "duration": analysis.duration,
            "width": analysis.width,
            "height": analysis.height,
            "transcript_backend": analysis.transcript_backend,
            "moments": analysis.moments,
            "events": analysis.events,
            "graph": analysis.graph,
            "signals": analysis.signals,
            "has_llm": analysis.has_llm,
            "clip_length": [lo, hi],
        },
        "clips": [c.to_dict() for c in clips],
        "elapsed_seconds": round(time.time() - t0, 2),
        "watermark": False,
    }