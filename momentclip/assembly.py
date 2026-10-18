"""Storyboard assembly -- turn a written season into watchable episodes.

Each episode is a sequence of shots, one per beat. A shot is a still (the
character's image, or a generated plate if none) given Ken Burns motion, an
animated caption of the beat text, and a hard cut or dissolve between shots.
Music runs the whole episode, ducked under nothing and cut to stingers at the
tense beats.

This is a MOTION STORYBOARD, not generative animation. The characters do not
move; the frame moves around them. That is the honest limit of doing this
locally for free, and it is why the visual provider is an interface rather
than something baked in -- swap in generated clips and nothing else changes.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import captions, ffmpeg, soundtrack
from .asr import Word
from .config import Config
from .story import Beat, Episode, Season

log = logging.getLogger("momentclip.assembly")

FPS = 30

# How many shots to render at once. Each shot is its own ffmpeg process, so
# this is pure core utilisation. Two cores are left free on purpose: ffmpeg is
# not the only thing on the machine, and saturating every core makes the whole
# box feel hung while a season renders.
DEFAULT_WORKERS = max(1, min(6, (os.cpu_count() or 4) - 2))


@dataclass
class ShotSpec:
    """One beat rendered to one clip."""
    beat: Beat
    image: Path | None
    seconds: float
    motion: str
    index: int
    tension: float = 0.0


@dataclass
class EpisodeResult:
    number: int
    title: str
    path: Path | None
    duration: float
    shots: int
    music: str | None = None
    stingers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "number": self.number, "title": self.title,
            "path": str(self.path) if self.path else None,
            "duration": round(self.duration, 1), "shots": self.shots,
            "music": self.music, "stingers": self.stingers,
            "notes": self.notes, "error": self.error,
        }


# --------------------------------------------------------------------------
# plates
# --------------------------------------------------------------------------
_PALETTE = [
    ((18, 22, 34), (38, 30, 62)),
    ((30, 18, 26), (62, 26, 44)),
    ((14, 28, 30), (22, 54, 58)),
    ((28, 26, 16), (58, 48, 24)),
    ((22, 18, 32), (44, 32, 66)),
]


def make_plate(dest: Path, w: int, h: int, seed: int = 0) -> Path:
    """A gradient plate for beats with no character image.

    Deliberately plain. It must read as 'no image available', not as a
    generated scene we are pretending is art.
    """
    a, b = _PALETTE[seed % len(_PALETTE)]
    y = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    x = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    t = (0.65 * y + 0.35 * x)
    img = (np.array(a, np.float32)[None, None, :] * (1 - t[..., None])
           + np.array(b, np.float32)[None, None, :] * t[..., None])
    # faint vignette so it does not look like a rendering bug
    vig = 1.0 - 0.35 * (((x - 0.5) ** 2 + (y - 0.5) ** 2) * 2.0)
    img *= vig[..., None]
    rgb = np.clip(img, 0, 255).astype(np.uint8)

    dest.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run([
        get_cfg_ffmpeg(), "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
        "-i", "pipe:0", "-frames:v", "1", str(dest),
    ], timeout=120, check=False, input_bytes=rgb.tobytes()) if False else None

    # write via ffmpeg from a temp raw file -- avoids shell pipe entirely
    raw = dest.with_suffix(".raw")
    raw.write_bytes(rgb.tobytes())
    try:
        ffmpeg.run([
            get_cfg_ffmpeg(), "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
            "-i", str(raw), "-frames:v", "1", "-q:v", "2", str(dest),
        ], timeout=120)
    finally:
        raw.unlink(missing_ok=True)
    return dest


_CFG_FF = None


def get_cfg_ffmpeg() -> str:
    global _CFG_FF
    if _CFG_FF is None:
        from .config import get_config
        _CFG_FF = get_config().ffmpeg
    return _CFG_FF


# --------------------------------------------------------------------------
# Ken Burns
# --------------------------------------------------------------------------
def _motion_expr(motion: str, frames: int) -> tuple[str, str, str]:
    """zoompan z/x/y expressions for a named move.

    Standard practice: upscale first, then zoompan, because zoompan quantises
    its crop to integer pixels on the input and a 1080p input gives visibly
    stepping pans.
    """
    n = max(1, frames)
    # z ramps across the shot
    if motion == "in":
        z = f"1.0+0.14*on/{n}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif motion == "out":
        z = f"1.15-0.14*on/{n}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif motion == "left":
        z = "1.14"
        x = f"(iw-iw/zoom)*(1-on/{n})"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "right":
        z = "1.14"
        x = f"(iw-iw/zoom)*(on/{n})"
        y = "ih/2-(ih/zoom/2)"
    else:
        z = f"1.0+0.10*on/{n}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    return z, x, y


def _shot_words(text: str, duration: float) -> list[Word]:
    """Synthetic word timings so the caption engine can animate the beat."""
    toks = str(text or "").split()
    if not toks:
        return []
    n = len(toks)
    per = max(0.18, duration / n)
    out = []
    t = 0.05
    for w in toks:
        out.append(Word(t, min(duration - 0.02, t + per * 0.9), w))
        t += per
    return out


def render_shot(spec: ShotSpec, dest: Path, width: int, height: int,
                caption_style: str, cfg: Config) -> bool:
    """Render one beat to a silent clip."""
    frames = max(2, int(round(spec.seconds * FPS)))
    src = spec.image if (spec.image and Path(spec.image).exists()) else None
    if src is None:
        src = make_plate(dest.parent / f"plate_{spec.index:03d}.png",
                         width, height, seed=spec.index)

    # upscale before zoompan so the pan does not step
    up_w, up_h = width * 2, height * 2
    z, x, y = _motion_expr(spec.motion, frames)

    # punch-in on a tense beat: an extra zoom step at the shot's midpoint
    mid = frames // 2
    pw = max(4, int(frames * 0.22))
    if spec.tension > 0.35:
        z = f"({z})+{0.06 * min(1.0, spec.tension):.3f}*max(0\\,1-abs(on-{mid})/{pw})"

    vf = (f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
          f"crop={up_w}:{up_h},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={FPS},"
          f"format=yuv420p")

    # caption overlay
    words = _shot_words(spec.beat.text, spec.seconds)
    ass = None
    if words and cfg.burn_captions and ffmpeg.has_libass(cfg):
        ass = dest.with_suffix(".ass")
        if captions.write_ass(words, ass, caption_style, offset=0.0,
                              width=width, height=height) is None:
            ass = None
    if ass is not None:
        esc = str(ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        vf += f",subtitles='{esc}'"

    cmd = [
        cfg.ffmpeg, "-y",
        "-loop", "1", "-t", f"{spec.seconds:.3f}", "-i", str(src),
        "-vf", vf,
        "-frames:v", str(frames),
        "-c:v", ffmpeg.pick_h264_encoder(), "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-f", "mp4", str(dest),
    ]
    try:
        ffmpeg.run(cmd, timeout=600)
        return dest.exists() and dest.stat().st_size > 512
    except Exception as exc:  # noqa: BLE001
        log.warning("shot %d failed: %s", spec.index, exc)
        return False


# --------------------------------------------------------------------------
# episode
# --------------------------------------------------------------------------
_SILENT_SRC: dict[str, str] = {}


def _silent_source(cfg: Config) -> str:
    """A silent stereo audio source, spelled for whatever ffmpeg this is.

    The trap here is that `anullsrc` has had three spellings for the channel
    layout and ffmpeg 9.0.2 accepts *none* of the two you would guess:

        channel_layouts=stereo   the old (pre-6) name -- REMOVED, "Option not found"
        ch_layout=stereo         you would assume this replaced it -- ALSO removed
        cl=stereo                the actual name, and the one to use

    So probe instead of guessing from a version string. Bare `anullsrc` is the
    last candidate: stereo at 44100 is its default, so it cannot fail.
    """
    key = cfg.ffmpeg
    if key in _SILENT_SRC:
        return _SILENT_SRC[key]
    candidates = [
        "anullsrc=r=44100:cl=stereo",              # canonical, current ffmpeg
        "anullsrc=channel_layout=stereo:sample_rate=44100",  # very old builds
        "anullsrc",                                # defaults: stereo, 44100
    ]
    chosen = candidates[-1]
    for src in candidates:
        try:
            # check=True so an unsupported option raises rather than passing
            ffmpeg.run([cfg.ffmpeg, "-y", "-loglevel", "error",
                        "-f", "lavfi", "-t", "0.1", "-i", src,
                        "-f", "null", "-"], timeout=60, check=True)
            chosen = src
            break
        except Exception as exc:  # noqa: BLE001
            log.debug("silent source %r rejected: %s", src, str(exc)[:120])
            continue
    _SILENT_SRC[key] = chosen
    log.info("silent audio source for this ffmpeg: %s", chosen)
    return chosen


def _tension(beat: Beat, ep: Episode) -> float:
    """Cheap dramatic weighting so stingers land somewhere sensible."""
    text = (beat.text or "").lower()
    hot = ("dies", "death", "kill", "betray", "reveal", "truth", "confront",
           "fight", "scream", "final", "lost", "break", "ends", "last")
    score = 0.0
    score += 0.45 if any(h in text for h in hot) else 0.0
    score += 0.25 if any(w in text for w in ("but", "until", "suddenly")) else 0.0
    # later episodes run hotter
    score += 0.3 * (ep.number / 10.0)
    return min(1.0, score)


def assemble_episode(
    ep: Episode,
    outdir: Path,
    cfg: Config,
    *,
    width: int = 1080,
    height: int = 1920,
    aspect: str = "9:16",
    caption_style: str = "pop",
    music_mood: str = "auto",
    auto_sfx: bool = True,
    max_workers: int | None = None,
    progress=None,
) -> EpisodeResult:
    """Render one episode end to end."""
    outdir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"clipper-ep{ep.number:02d}-"))
    notes: list[str] = []
    try:
        specs = [
            ShotSpec(b, Path(b.image) if b.image else None, b.seconds, b.motion,
                     i, _tension(b, ep))
            for i, b in enumerate(ep.beats)
        ]
        if not specs:
            return EpisodeResult(ep.number, ep.title, None, 0.0, 0,
                                 notes=["episode had no beats"], error="no beats")

        if progress:
            progress(5, f"episode {ep.number}: {len(specs)} shots")

        workers = max_workers if max_workers else DEFAULT_WORKERS

        # ---- 1. each shot, in parallel ----
        # Shots share nothing: each is an independent ffmpeg process over its
        # own image. Running them one at a time left every core but one idle.
        # Measured on this machine: 3.1s per shot sequential at 1080x1920, so
        # a 5-shot episode spent 15s doing work that takes ~4s across 4
        # workers. ffmpeg is a subprocess, so this scales properly rather than
        # queueing behind the GIL.
        parts: list[Path] = []

        def one(idx_spec):
            i, spec = idx_spec
            dest = work / f"shot_{i:03d}.mp4"
            try:
                return i, dest, render_shot(spec, dest, width, height,
                                            caption_style, cfg)
            except Exception as exc:  # noqa: BLE001
                log.warning("shot %d raised: %s", i, exc)
                return i, dest, False

        if workers > 1 and len(specs) > 1:
            with ThreadPoolExecutor(max_workers=min(workers, len(specs))) as pool:
                for i, dest, ok in pool.map(one, enumerate(specs)):
                    if ok:
                        parts.append(dest)
                    else:
                        notes.append(f"shot {i} failed to render")
        else:
            for i, s in enumerate(specs):
                dest = work / f"shot_{i:03d}.mp4"
                if render_shot(s, dest, width, height, caption_style, cfg):
                    parts.append(dest)
                else:
                    notes.append(f"shot {i} failed to render")

        # pool.map preserves input order, but be explicit: the concat order is
        # the beat order, and a shot landing out of sequence would silently
        # scramble the episode.
        parts.sort(key=lambda p: p.name)
        if not parts:
            return EpisodeResult(ep.number, ep.title, None, 0.0, 0,
                                 notes=notes, error="every shot failed")

        # ---- 2. cut together ----
        if progress:
            progress(45, f"episode {ep.number}: cutting {len(parts)} shots")
        silent = work / "silent.mp4"
        lst = work / "concat.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts),
                       encoding="utf-8")
        duration = sum(s.seconds for s in specs)
        # Cut the shots together by remuxing, not re-encoding. Every shot is
        # already H.264 at identical settings, so re-encoding the whole episode
        # here was paying for a second full generation pass -- slow, and it
        # threw away quality that had just been computed. `-c copy` is close to
        # instant; the re-encode stays as a fallback for the case where copy
        # cannot handle the stream (mismatched parameters, odd timestamps).
        def _concat(copy_streams: bool) -> None:
            cmd = [cfg.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(lst)]
            if copy_streams:
                cmd += ["-c", "copy", "-an"]
            else:
                cmd += ["-c:v", ffmpeg.pick_h264_encoder(), "-preset", "veryfast",
                        "-crf", "20", "-pix_fmt", "yuv420p", "-an"]
            cmd += [str(silent)]
            ffmpeg.run(cmd, timeout=1800)

        try:
            _concat(copy_streams=True)
        except Exception as exc:  # noqa: BLE001
            log.info("concat by copy failed (%s), re-encoding instead", exc)
            try:
                _concat(copy_streams=False)
            except Exception as exc2:  # noqa: BLE001
                return EpisodeResult(ep.number, ep.title, None, 0.0, len(parts),
                                     notes=notes, error=f"concat failed: {exc2}")

        # ---- 3. score it ----
        if progress:
            progress(70, f"episode {ep.number}: scoring")
        final = outdir / f"episode_{ep.number:02d}.mp4"

        # input 0 = silent video, input 1 = silent audio base, 2.. = music/sfx
        # NB: ffmpeg 9 removed `channel_layouts` from anullsrc in favour of
        # `ch_layout`, so probe which spelling this binary accepts rather than
        # guessing from a version string.
        cmd = [cfg.ffmpeg, "-y", "-i", str(silent),
               "-f", "lavfi", "-i", _silent_source(cfg)]
        music_name = None
        stingers: list[str] = []
        complex_parts: list[str] = []

        bed = None
        if music_mood != "none":
            feats = {"spectacle": 0.2 + 0.05 * ep.number,
                     "loudness_swing": 8.0 + ep.number}
            bed, mood, _root, _why = soundtrack.build_music_adaptive(
                duration, feats, [], None, "general", cfg, workdir=outdir / "_music")
            music_name = mood

        sfx_files: list[Path] = []
        offsets: list[float] = []
        if auto_sfx:
            # land a stinger on the tense beats, spaced apart
            picks: list[tuple[float, float]] = []
            cursor = 1.0
            for s in specs:
                if s.tension > 0.55 and cursor < duration - 1.5:
                    picks.append((round(cursor, 2), s.tension))
                    cursor += max(4.0, s.seconds)
            picks = picks[:2]
            kinds = ["brass", "drumroll", "pop", "slide", "womp"]
            for i, (t, _s) in enumerate(picks):
                kind = kinds[i % len(kinds)]
                p = outdir / "_music" / f"ep{ep.number:02d}_sfx_{kind}.wav"
                if soundtrack.generate_stinger(kind, p, seconds=1.4):
                    sfx_files.append(p)
                    offsets.append(t)
                    stingers.append(kind)

        inputs: list[str] = []
        # NOTE: filter labels carry their own brackets. Stripping them and
        # concatenating produced "[bed]amix", which ffmpeg reads as a filter
        # named "bedamix" and fails with "No such filter". Always build the
        # full bracketed label; never interpolate the bare name.
        bg = None
        if bed is not None:
            inputs += ["-i", str(bed)]
            g = cfg.music_gain
            # input index is 2: 0 = video, 1 = anullsrc audio base
            complex_parts.append(
                f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                f"volume={g:.3f}[bed]")
            bg = "[bed]"

        # stingers follow the music: inputs 3..N
        for i, p in enumerate(sfx_files):
            inputs += ["-i", str(p)]
            ms = int(offsets[i] * 1000)
            complex_parts.append(
                f"[{3 + i}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                f"volume={cfg.music_gain * 2.6:.3f},adelay={ms}|{ms}[s{i}]")

        if sfx_files:
            merged = "".join(f"[s{i}]" for i in range(len(sfx_files)))
            complex_parts.append(
                f"{merged}amix=inputs={len(sfx_files)}:duration=first:normalize=0[sfx]")
            if bg:
                complex_parts.append(f"{bg}[sfx]amix=inputs=2:duration=first:"
                                     f"normalize=0[bg2]")
            else:
                complex_parts.append("[sfx]anull[bg2]")
            bg = "[bg2]"

        if bg:
            # [1:a] is the anullsrc base, so the score is trimmed to the video
            # by duration=first rather than by -shortest.
            complex_parts.append(
                f"[1:a]{bg}amix=inputs=2:duration=first:"
                f"dropout_transition=0,alimiter=limit=0.97[aout]")

        problems = soundtrack.check_filtergraph(";".join(complex_parts))
        if problems:
            for p in problems:
                log.error("filtergraph: %s", p)
            return EpisodeResult(ep.number, ep.title, None, duration, len(parts),
                                 music_name, stingers, notes,
                                 error="bad audio graph: " + "; ".join(problems[:2]))

        cmd += inputs
        if bg:
            cmd += ["-filter_complex", ";".join(complex_parts)]
            cmd += ["-map", "0:v:0", "-map", "[aout]",
                    "-c:a", "aac", "-b:a", "160k", "-shortest"]
        else:
            # no score at all -- a silent episode is still watchable
            cmd += ["-map", "0:v:0", "-map", "1:a:0",
                    "-c:a", "aac", "-b:a", "128k", "-shortest"]
        cmd += ["-c:v", "copy", "-movflags", "+faststart", str(final)]

        try:
            ffmpeg.run(cmd, timeout=1800)
        except Exception as exc:  # noqa: BLE001
            return EpisodeResult(ep.number, ep.title, None, duration, len(parts),
                                 music_name, stingers, notes,
                                 error=f"mux failed: {exc}")

        if not final.exists() or final.stat().st_size < 1024:
            return EpisodeResult(ep.number, ep.title, None, duration, len(parts),
                                 music_name, stingers, notes, error="empty output")

        if progress:
            progress(100, f"episode {ep.number} done")
        return EpisodeResult(ep.number, ep.title, final, duration, len(parts),
                             music_name, stingers, notes)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
def assemble_season(
    season: Season,
    outdir: Path,
    cfg: Config,
    *,
    aspect: str = "9:16",
    caption_style: str = "pop",
    music_mood: str = "auto",
    auto_sfx: bool = True,
    max_episodes: int | None = None,
    max_workers: int | None = None,
    progress=None,
) -> tuple[list[EpisodeResult], dict]:
    """Assemble every episode. Returns (results, season dict)."""
    dims = {"9:16": (1080, 1920), "1:1": (1080, 1080),
            "4:5": (1080, 1350), "16:9": (1920, 1080)}.get(aspect, (1080, 1920))
    w, h = dims
    eps = season.episodes[:max_episodes] if max_episodes else season.episodes

    results: list[EpisodeResult] = []
    n = max(1, len(eps))

    # Episodes are independent of each other, exactly as shots are within one.
    # Rendering them one after another is why a ten-episode season felt like a
    # chore: one ffmpeg at a time on a twelve-core machine.
    #
    # But do not also give each episode the full shot budget on top of this, or
    # ten episodes would each spawn six ffmpegs and thrash. Split the cores:
    # a few episodes in flight, each using the rest for its own shots.
    if max_workers is not None:
        ep_workers, shot_workers = max_workers, max_workers
    else:
        ep_workers = max(1, min(3, DEFAULT_WORKERS // 2))
        shot_workers = max(1, DEFAULT_WORKERS // ep_workers)

    def make(i: int, ep: Episode) -> EpisodeResult:
        def prog(p: int, msg: str, _i: int = i) -> None:
            if progress:
                # each episode owns a slice of the bar
                progress(int((_i * 100) / n) + int(p / max(1, n)), msg)

        return assemble_episode(
            ep, outdir, cfg, width=w, height=h, aspect=aspect,
            caption_style=caption_style, music_mood=music_mood,
            auto_sfx=auto_sfx, max_workers=shot_workers, progress=prog)

    if ep_workers > 1 and len(eps) > 1:
        # collect by index so episode order never depends on which finished
        # first
        ordered: list[EpisodeResult | None] = [None] * len(eps)
        with ThreadPoolExecutor(max_workers=min(ep_workers, len(eps))) as pool:
            futures = {pool.submit(make, i, ep): i for i, ep in enumerate(eps)}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    ordered[i] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    ordered[i] = EpisodeResult(
                        eps[i].number, eps[i].title, None, 0.0, 0,
                        error=f"assembly failed: {exc}")
        results = [r for r in ordered if r is not None]
    else:
        for i, ep in enumerate(eps):
            results.append(make(i, ep))
    return results, season.to_dict()