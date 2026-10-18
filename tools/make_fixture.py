#!/usr/bin/env python
"""Generate a synthetic test video with KNOWN planted moments.

Plants, by design, at known timestamps:
  ~8s   loud broadband burst   -> should trigger `laughter`
  ~22s  sudden energy rise     -> should trigger `energy_shock`
  ~36s  pitch sweep upward     -> should trigger `pitch_surge`
  ~50s  quiet beat between speech -> should trigger `silence_beat`
  ~64s  two voices overlapping -> should trigger `overlap`

Run:  python tools/make_fixture.py
Then: python -c "from momentclip import signals; ..." or just use the web UI.
"""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import wave
from pathlib import Path

SR = 16000
DURATION = 80.0
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "fixtures"
VIDEO = OUT / "test_podcast.mp4"


def speechlike(t: float, seed: int = 0) -> float:
    """Roughly voice-like: harmonic stack + formant-ish shaping + jitter."""
    base = 120 + 18 * math.sin(t * 0.7 + seed) + 10 * math.sin(t * 2.3 + seed)
    v = 0.0
    for k, amp in ((1, 1.0), (2, 0.55), (3, 0.32), (4, 0.18), (6, 0.09)):
        v += amp * math.sin(2 * math.pi * base * k * t)
    # syllable envelope ~4 Hz
    env = 0.55 + 0.45 * abs(math.sin(2 * math.pi * 3.8 * t + seed))
    return 0.28 * v * env


def broadband(t: float, dur: float, t0: float) -> float:
    """Noise burst -- stands in for laughter/applause."""
    if not (t0 <= t < t0 + dur):
        return 0.0
    env = math.sin(math.pi * (t - t0) / dur) ** 0.6
    return 0.85 * env


def sweep(t: float, t0: float, dur: float, f_lo: float, f_hi: float) -> float:
    """Rising pitch glide."""
    if not (t0 <= t < t0 + dur):
        return 0.0
    p = (t - t0) / dur
    f = f_lo + (f_hi - f_lo) * p
    return 0.4 * math.sin(2 * math.pi * f * t) * math.sin(math.pi * p)


def build_audio(path: Path) -> None:
    n = int(SR * DURATION)
    frames = bytearray()
    for i in range(n):
        t = i / SR

        # base "speech" everywhere except the planted quiet beat
        quiet = 50.0 <= t < 51.4
        v = 0.0 if quiet else speechlike(t)

        # planted 1: laughter burst at 8s
        v += broadband(t, 1.8, 8.0)
        # planted 2: energy shock at 22s (sudden + louder)
        if 22.0 <= t < 24.5:
            v += 0.75 * speechlike(t, seed=2.0)
        # planted 3: pitch sweep at 36s
        v += sweep(t, 36.0, 2.6, 160.0, 330.0)
        # planted 4: the quiet beat at 50-51.4s is already applied above
        # planted 5: overlapping voices at 64s (two detuned speech sources)
        if 64.0 <= t < 66.5:
            v += 0.55 * speechlike(t, seed=0.0) + 0.55 * speechlike(t, seed=1.7)
            v += 0.35 * broadband(t, 2.5, 64.0)

        v = max(-1.0, min(1.0, v))
        frames += struct.pack("<h", int(v * 32767))

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(frames))


def mux(wav_path: Path, out: Path) -> bool:
    """Add a visible test pattern and mux. Uses the installed ffmpeg."""
    exe = subprocess.run(["ffmpeg", "-version"], capture_output=True)
    if exe.returncode != 0:
        print("  ffmpeg not on PATH; wrote audio only", file=sys.stderr)
        return False
    proc = subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc2=size=1280x720:rate=25:duration={DURATION}",
        "-i", str(wav_path),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", str(out),
    ], capture_output=True, text=True, timeout=1200)
    if proc.returncode != 0:
        print(proc.stderr[-1500:], file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    wav = OUT / "test_podcast.wav"
    print("building synthetic audio…")
    build_audio(wav)
    print(f"  {wav}")
    print("muxing to video…")
    if mux(wav, VIDEO):
        print(f"  {VIDEO}")
        print("\nplanted moments: laughter@8s  energy_shock@22s  pitch_surge@36s  "
              "silence_beat@50s  overlap@64s")
    else:
        print("  (video mux failed — run with ffmpeg on PATH)")