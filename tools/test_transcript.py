#!/usr/bin/env python
"""Exercise the TRANSCRIPT path with real spoken audio.

make_fixture.py builds tones, which whisper correctly reports as "no speech" --
good for signal testing, useless for testing ASR. This test uses Windows TTS
output (real words) and asserts the whole text path works:

  faster-whisper runs -> word-level timestamps exist -> utterances exist
  -> candidates snap to word boundaries -> text is attached -> captions render

Run:
    powershell -File tools\\make_speech_fixture.ps1
    python tools\\test_transcript.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from momentclip import asr, candidates, captions, ffmpeg, render, signals
from momentclip.config import get_config

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    cfg = get_config()
    src = ROOT / "fixtures" / "speech.wav"
    if not src.exists():
        print(f"missing {src}\nrun: powershell -File tools/make_speech_fixture.ps1")
        return 2

    work = ROOT / "data" / "_speech"
    work.mkdir(parents=True, exist_ok=True)
    audio = work / "audio16k.wav"
    print("decoding speech to 16 kHz mono …")
    ffmpeg.extract_audio(src, audio, cfg.analysis_sr)

    caps = asr.capability()
    print(f"backends: {caps}")
    if not caps["faster_whisper"]:
        print("\n  faster-whisper not installed -- cannot test the transcript path")
        print("  install: pip install \"av<19\" faster-whisper")
        return 2

    print("transcribing (first run downloads the model) …")
    t = asr.transcribe(audio, cfg)

    fails = []
    print(f"\nbackend    {t.backend}")
    print(f"language   {t.language or '?'}")
    print(f"duration   {t.duration:.1f}s")
    print(f"utterances {len(t.utterances)}")
    print(f"words      {len(t.words())}")

    if not t.available:
        print("\n  FAILED: transcription produced nothing")
        return 1

    words = t.words()
    if not words:
        fails.append("no word-level timestamps (captions need them)")
    else:
        bad = [w for w in words if w.end < w.start]
        if bad:
            fails.append(f"{len(bad)} words with end < start")
        span = words[-1].end - words[0].start
        print(f"word span  {words[0].start:.1f}s -> {words[-1].end:.1f}s "
              f"({span:.1f}s of {t.duration:.1f}s)")

    # sanity: the transcript should look like the script we spoke
    print(f"\ntranscript sample:\n  \"{t.text[:300]}\"")

    # ---- candidates must snap to real word boundaries ----
    track, events, _ = signals.analyse_audio(audio, cfg.frame_hz)
    duration = t.duration or 40.0
    moments = candidates.generate(t, events, track, duration, cfg)
    print(f"\ncandidates {len(moments)} from {len(events)} signal events")

    if not moments:
        fails.append("no candidates generated from a transcript")
    for m in moments[:5]:
        print(f"  {m.id} {m.start:6.2f}-{m.end:6.2f}s  {m.source:<11} "
              f"{(m.text[:60] or '(no text)')}")

    with_text = [m for m in moments if m.text]
    if moments and not with_text:
        fails.append("candidates produced no text")
    pct = 100 * len(with_text) / max(1, len(moments))
    print(f"candidates with text: {len(with_text)}/{len(moments)} ({pct:.0f}%)")

    # word-aligned: a clip should not start in the middle of a word
    if words:
        boundaries = {round(w.start, 2) for w in words} | {round(w.end, 2) for w in words}
        for m in moments[:8]:
            if m.words and m.words[0].start != m.start:
                fails.append(f"{m.id} start {m.start} is not a word boundary")
                break

    # ---- captions must render and burn ----
    if words:
        srt = captions.write_srt(words, work / "test.srt")
        ass = captions.write_ass(words, work / "test.ass", "pop", offset=0.0)
        print(f"\nsrt        {srt.stat().st_size if srt else 0} bytes")
        print(f"ass        {ass.stat().st_size if ass else 0} bytes")
        if not srt or srt.stat().st_size == 0:
            fails.append("no .srt written")
        if not ass or ass.stat().st_size == 0:
            fails.append("no .ass written")
        else:
            head = ass.read_text(encoding="utf-8")
            if "Dialogue:" not in head:
                fails.append("ass has no Dialogue events")
            if "{kf" not in head:
                print("  note: no karaoke tags found (single-word groups?)")

    # ---- full render with burned captions ----
    print("\nrendering a clip with burned captions …")
    dest = work / "clip.mp4"
    try:
        res = render.render_clip(audio, 0.0, min(duration, 20.0), dest,
                                 words=words, aspect="9:16",
                                 caption_style="pop", want_captions=True,
                                 want_reframe=False, cfg=cfg)
        info = ffmpeg.probe(dest)
        print(f"  {dest.name}  {info.width}x{info.height}  "
              f"{info.duration:.1f}s  {dest.stat().st_size / 1e6:.2f} MB")
        print(f"  burned captions: {res.burned_captions}")
        if not res.burned_captions:
            fails.append(f"captions were not burned: {res.notes}")
        if abs(info.width / info.height - 1080 / 1920) > 0.02:
            fails.append(f"wrong aspect {info.width}x{info.height}")
    except Exception as exc:  # noqa: BLE001
        fails.append(f"render failed: {type(exc).__name__}: {exc}")

    print()
    if fails:
        print(f"  {len(fails)} FAILURES")
        for f in fails:
            print(f"    - {f}")
        return 1
    print("  all transcript-path checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())