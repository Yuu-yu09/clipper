#!/usr/bin/env python
"""Validate signal detection against the planted moments in the fixture.

Pass criteria: each planted event is detected with at least one event of the
expected kind whose window overlaps the planted timestamp.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from momentclip import ffmpeg, signals
from momentclip.config import get_config

PLANTED = [
    (8.0, "laughter", 3.0),
    (22.0, "energy_shock", 3.0),
    (36.0, "pitch_surge", 3.5),
    (50.0, "silence_beat", 2.5),
    (64.0, "overlap", 3.5),
]

TOLERANCE = 4.0  # seconds


def main() -> int:
    cfg = get_config()
    root = Path(__file__).resolve().parent.parent
    src = root / "fixtures" / "test_podcast.mp4"
    if not src.exists():
        print(f"missing fixture: {src}\nrun: python tools/make_fixture.py")
        return 2

    work = root / "data" / "_selftest"
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "audio16k.wav"
    print(f"decoding {src.name} …")
    ffmpeg.extract_audio(src, wav, cfg.analysis_sr)

    print(f"analysing at {cfg.frame_hz} Hz …")
    track, events, dur = signals.analyse_audio(wav, cfg.frame_hz)
    print(f"  {dur:.1f}s, {len(track['t'])} frames, {len(events)} events\n")

    kinds = {}
    for e in events:
        kinds.setdefault(e.kind, []).append(e)
    for k in sorted(kinds):
        print(f"  {k:<16} {len(kinds[k]):>3} detected")

    print("\n  planted -> detected")
    print("  " + "-" * 52)
    failures = []
    for t0, kind, tol in PLANTED:
        hits = [e for e in kinds.get(kind, [])
                if e.start - tol <= t0 <= e.end + tol]
        if hits:
            best = max(hits, key=lambda e: e.strength)
            print(f"  {t0:>5.1f}s {kind:<14} FOUND   "
                  f"at {best.start:.1f}-{best.end:.1f}s  strength {best.strength:.2f}")
        else:
            near = [e for e in events if e.start - tol <= t0 <= e.end + tol]
            alt = ", ".join(sorted({e.kind for e in near})) or "nothing"
            print(f"  {t0:>5.1f}s {kind:<14} MISS    near: {alt}")
            failures.append((t0, kind, alt))

    print()
    if failures:
        print(f"  {len(failures)}/{len(PLANTED)} planted events not detected as expected")
        return 1
    print(f"  all {len(PLANTED)} planted events detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())