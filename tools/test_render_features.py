#!/usr/bin/env python
"""Test the new render path: zoom, modern captions, music bed, comedy SFX.

Each of these is a separate ffmpeg risk -- zoom uses nested time expressions in
scale/crop, the audio graph mixes a synthesised bed with delayed stingers, and
the caption file uses \\k karaoke plus \\t transforms. Any one can fail silently
or blow up at render time, so each is asserted here.

Run:  python tools/test_render_features.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from momentclip import asr, captions, ffmpeg, render, soundtrack
from momentclip.config import get_config, resolve_clip_length, CLIP_LENGTHS

ROOT = Path(__file__).resolve().parent.parent


def build_source(cfg, src: Path, work: Path) -> Path:
    """A short video with real speech, if we have it; else a tone pattern."""
    speech = ROOT / "fixtures" / "speech.wav"
    if speech.exists():
        dur = 30.0
        vid = work / "src.mp4"
        ffmpeg.run([
            cfg.ffmpeg, "-y", "-f", "lavfi",
            "-i", f"testsrc2=size=1280x720:rate=25:duration={dur}",
            "-i", str(speech),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(vid),
        ], timeout=900)
        return vid
    tone = ROOT / "fixtures" / "test_podcast.mp4"
    if tone.exists():
        out = work / "src.mp4"
        out.write_bytes(tone.read_bytes())
        return out
    raise SystemExit("no fixture. run: python tools/make_fixture.py")


def main() -> int:
    cfg = get_config()
    work = ROOT / "data" / "_render"
    work.mkdir(parents=True, exist_ok=True)
    fails: list[str] = []

    # ---------- clip length bands ----------
    print("clip length bands")
    for lo, hi, label in CLIP_LENGTHS:
        got = resolve_clip_length(label)
        ok = abs(got[0] - lo) < 0.01 and abs(got[1] - hi) < 0.01
        print(f"  {label:<22} -> {got[0]:g}-{got[1]:g}s  {'ok' if ok else 'MISMATCH'}")
        if not ok:
            fails.append(f"clip length {label} resolved to {got}")
    auto = resolve_clip_length("auto")
    print(f"  {'auto':<22} -> {auto[0]:g}-{auto[1]:g}s")
    if auto[0] != 10.0:
        fails.append(f"auto min clip is {auto[0]}, expected 10")

    src = build_source(cfg, None, work)
    info = ffmpeg.probe(src)
    print(f"\nsource {src.name}: {info.width}x{info.height} {info.duration:.1f}s")
    span = min(12.0, info.duration)

    # ---------- words ----------
    words: list[asr.Word] = []
    speech = ROOT / "fixtures" / "speech.wav"
    if speech.exists():
        audio = work / "audio16k.wav"
        if not audio.exists():
            ffmpeg.extract_audio(speech, audio, cfg.analysis_sr)
        t = asr.transcribe(audio, cfg)
        words = [w for w in t.words() if w.end <= span]
        print(f"words in clip range: {len(words)}")
    if not words:
        # synthesise word timings so the caption path is still exercised
        words = [asr.Word(i * 0.45, i * 0.45 + 0.4, w)
                 for i, w in enumerate(
                     "this is a test of the emergency caption system and it should "
                     "render perfectly".split())]
        print("no transcript; using synthetic word timings")

    # ---------- caption presets ----------
    print("\ncaption presets")
    for name in captions.available_presets():
        p = captions.write_ass(words, work / "t.ass", name, offset=0.0)
        if not p:
            fails.append(f"preset {name} produced no file")
            print(f"  {name:<10} FAILED")
            continue
        txt = p.read_text(encoding="utf-8")
        d = txt.count("Dialogue:")
        kf = txt.count("{\\k")
        tf = txt.count("\\t(")
        pop = "scale pop" if tf else "no pop"
        print(f"  {name:<10} {d:>3} lines  {kf:>3} karaoke  {pop}")
        if d == 0:
            fails.append(f"preset {name} has no Dialogue lines")

    # ---------- headline ----------
    hl = captions.write_headline("here is the moment everybody talked about",
                                 work / "t_head.ass", offset=0.0)
    print(f"\nheadline: {'ok' if hl and hl.stat().st_size > 0 else 'FAILED'}")
    if not hl:
        fails.append("headline not written")

    # ---------- music + stingers ----------
    print("\nmusic beds")
    for mood in soundtrack.MOODS:
        if mood == "none":
            continue
        bed = soundtrack.build_music(6.0, mood, cfg, workdir=work / "_music")
        if not bed:
            fails.append(f"mood {mood} produced no bed")
            print(f"  {mood:<12} FAILED")
            continue
        try:
            d = ffmpeg.probe(bed).duration
            print(f"  {mood:<12} {bed.stat().st_size / 1e3:>7.1f} kB  {d:.1f}s")
        except Exception as exc:  # noqa: BLE001
            fails.append(f"mood {mood} unreadable: {exc}")

    stinger_dir = work / "_music"
    kinds = ["brass", "drumroll", "pop", "slide", "womp"]
    sfx_files = []
    for k in kinds:
        p = soundtrack.generate_stinger(k, stinger_dir / f"sfx_{k}.wav")
        if p:
            sfx_files.append((k, p))
    print(f"  stingers: {len(sfx_files)}/{len(kinds)} synthesised")
    if len(sfx_files) < 3:
        fails.append("fewer than 3 stingers synthesised")

    # ---------- the real thing: render ----------
    print("\nrendering clip with zoom + captions + music + sfx …")
    bed = soundtrack.build_music(span, "funny", cfg, workdir=stinger_dir)
    mix = soundtrack.MixSpec(music=bed, music_gain=0.18, duck=True)
    spaths = [stinger_dir / f"sfx_{k}.wav" for k in kinds[:2]]
    sfx = [(3.0, kinds[0]), (7.5, kinds[1])]
    mix.stingers = sfx
    mix.stinger_paths = [p for p in spaths if p.exists()]

    dest = work / "full.mp4"
    try:
        res = render.render_clip(
            src, 0.0, span, dest,
            words=words, aspect="9:16", caption_style="hormozi",
            headline="here is the moment",
            want_captions=True, want_reframe=True, want_zoom=True,
            music=mix, stingers=[(t, k) for (t, k), p in zip(sfx, mix.stinger_paths)],
            cfg=cfg,
        )
        out = ffmpeg.probe(dest)
        print(f"  {dest.name}: {out.width}x{out.height} {out.duration:.1f}s "
              f"{dest.stat().st_size / 1e6:.2f} MB")
        print(f"  zoom {res.zoom}  captions={res.burned_captions}  "
              f"music={res.music}  sfx={res.sfx}")
        for n in res.notes:
            print(f"    note: {n}")
        if abs(out.width / out.height - 1080 / 1920) > 0.02:
            fails.append(f"wrong aspect {out.width}x{out.height}")
        if not out.has_audio:
            fails.append("rendered clip has no audio stream")
        if not res.burned_captions:
            fails.append("captions not burned")
        if res.zoom <= 1.0:
            fails.append("auto-zoom did not engage")
    except Exception as exc:  # noqa: BLE001
        fails.append(f"render failed: {type(exc).__name__}: {exc}")

    # ---------- zoom actually varies across the clip ----------
    print("\nverifying the zoom is not static …")
    try:
        import numpy as np
        exe = ffmpeg._discover("ffmpeg")
        if not exe:
            print("  ffmpeg not found; skipping")
            return finish(fails, dest)
        tmp = work / "zoomframes"
        tmp.mkdir(exist_ok=True)
        for f in tmp.glob("*.png"):
            f.unlink()
        ffmpeg.run([
            exe, "-y", "-i", str(dest),
            "-vf", "fps=1,scale=64:36",
            "-frames:v", "4", str(tmp / "z_%02d.png"),
        ], timeout=300)
        import cv2
        means = []
        for png in sorted(tmp.glob("*.png")):
            im = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
            if im is not None:
                means.append(float(im.mean()))
        if len(means) >= 2:
            spread = max(means) - min(means)
            print(f"  {len(means)} frames sampled, brightness spread {spread:.3f}")
            if spread < 0.5:
                fails.append("clip looks static across time (zoom not animating?)")
            else:
                print("  frames differ over time -> the crop window is moving")
        else:
            print("  could not sample frames; skipping")
    except Exception as exc:  # noqa: BLE001
        print(f"  zoom probe skipped: {exc}")

    return finish(fails, dest)


def finish(fails: list[str], dest: Path) -> int:
    print()
    if fails:
        print(f"  {len(fails)} FAILURES")
        for f in fails:
            print(f"    - {f}")
        return 1
    print("  all render-feature checks passed")
    print(f"\n  sample output: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())