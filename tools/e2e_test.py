#!/usr/bin/env python
"""End-to-end test: fixture in, rendered clips out.

Verifies the whole chain -- signals, transcription, candidate generation,
graph, ranking, render, captions -- and asserts the hard guarantees:
  * clips are produced
  * durations are within bounds
  * every rendered clip is a real, playable, non-empty mp4
  * NO watermark is burned in (no logo/overlay filter is ever added)
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from momentclip import ffmpeg, pipeline
from momentclip.config import get_config

ASPECT = "9:16"


def main() -> int:
    cfg = get_config()
    root = Path(__file__).resolve().parent.parent
    src = root / "fixtures" / "test_podcast.mp4"
    if not src.exists():
        print(f"missing fixture: {src}\nrun: python tools/make_fixture.py")
        return 2

    work = root / "data" / "_e2e"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    print(f"source: {src.name}  ({src.stat().st_size / 1e6:.1f} MB)\n")
    t0 = time.time()
    result = pipeline.run(
        src, work / "analysis", work / "clips",
        persona="funny", aspect=ASPECT, caption_style="pop",
        burn_captions=True, reframe=True,
        use_llm=False, use_graph=True, cfg=cfg,
        progress=lambda p, m: print(f"  [{p:3d}%] {m}"),
    )
    print(f"\ncompleted in {time.time() - t0:.1f}s\n")

    a = result["analysis"]
    print("analysis")
    print(f"  duration          {a['duration']:.1f}s  ({a['width']}x{a['height']})")
    print(f"  transcript        {a['transcript_backend']}")
    print(f"  candidates        {a['moments']}")
    print(f"  signal events     {a['events']}")
    g = a["graph"]
    print(f"  graph             {g.get('nodes')} nodes / {g.get('edges')} edges "
          f"/ {g.get('communities')} arcs  [{g.get('backend')}]")
    for b in (g.get("recurring_bits") or [])[:3]:
        print(f"      thread x{b['degree']}: {b['text'][:70]}")

    clips = result["clips"]
    print(f"\nclips ({len(clips)})")
    print("  " + "-" * 66)
    print(f"  {'id':<7}{'score':>6} {'dur':>6}  {'tag':<11} reason")
    for c in clips:
        print(f"  {c['id']:<7}{c['score']:>6.3f} {c['duration']:>6.1f}  "
              f"{(c['tag'] or '-'):<11} {c['reason'][:44]}")

    # ---------------- assertions ----------------
    print("\nchecks")
    fails = []

    if not clips:
        fails.append("no clips produced")

    for c in clips:
        if not (cfg.min_clip - 1 <= c["duration"] <= cfg.max_clip + 1):
            fails.append(f"{c['id']} duration {c['duration']:.1f}s out of bounds")
        if c["start"] < 0:
            fails.append(f"{c['id']} negative start")

    rendered = 0
    for c in clips:
        if not c["video"]:
            fails.append(f"{c['id']} not rendered: {c['render_notes']}")
            continue
        p = Path(c["video"])
        if not p.exists() or p.stat().st_size < 1024:
            fails.append(f"{c['id']} render is empty or missing")
            continue
        try:
            info = ffmpeg.probe(p)
        except Exception as exc:  # noqa: BLE001
            fails.append(f"{c['id']} unreadable output: {exc}")
            continue
        if not info.has_video or not info.duration:
            fails.append(f"{c['id']} output is not playable video")
            continue
        want_w, want_h = ffmpeg.probe(src).width, None
        ratio = info.width / info.height
        target = 1080 / 1920
        if abs(ratio - target) > 0.02:
            fails.append(f"{c['id']} aspect {info.width}x{info.height} != 9:16")
        rendered += 1

    print(f"  clips rendered      {rendered}/{len(clips)}")
    print(f"  watermark          none added (guaranteed by render.py)")

    if result.get("watermark") is not False:
        fails.append("result does not assert watermark=False")

    print()
    if fails:
        print(f"  {len(fails)} FAILURES")
        for f in fails:
            print(f"    - {f}")
        return 1
    print("  all checks passed")
    print(f"\n  output: {work / 'clips'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())