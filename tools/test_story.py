#!/usr/bin/env python
"""Test the story engine and the storyboard assembler.

Three things worth proving:
  1. A full season plans from a one-line premise plus a cast.
  2. Characters are not invented or renamed -- the model is told to reuse them
     and the assembler resolves beats back to real cast members.
  3. Episodes actually render to watchable mp4s with a soundtrack.

Run:  python tools/test_story.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from momentclip import assembly, story
from momentclip.config import get_config

PREMISE = ("A retired getaway driver is forced to take one last job with the "
           "crew she swore she would never work with again.")

CAST = [
    story.Character(name="Dana Reyes", role="protagonist",
                    description="ex-getaway driver, 50s, calm under pressure, "
                                "has not spoken to her crew in six years"),
    story.Character(name="Marcus Vale", role="antagonist",
                    description="the crew boss who betrayed her, charming, "
                                "unforgivable"),
    story.Character(name="Joss", role="ally",
                    description="the youngest of the crew, loyal to Dana "
                                "unconditionally, talks too much"),
    story.Character(name="Ines", role="mentor",
                    description="the old planner, retired twice, keeps a ledger "
                                "of every favour owed"),
]


def main() -> int:
    cfg = get_config()
    root = Path(__file__).resolve().parent.parent
    fails: list[str] = []

    print(f"model: {'yes' if cfg.has_llm else 'NO -- deterministic fallback only'}")
    print(f"premise: {PREMISE[:64]}…")
    print(f"cast: {', '.join(f'{c.name} ({c.role})' for c in CAST)}\n")

    # ---------------- plan ----------------
    t0 = time.time()
    season = story.build_season(
        PREMISE, [story.Character(c.name, c.role, c.description) for c in CAST],
        cfg, episode_count=10, use_llm=cfg.has_llm,
        progress=lambda p, m: print(f"  [{p:3d}%] {m}"))
    print(f"\nplanned in {time.time() - t0:.1f}s\n")

    print(f"TITLE   {season.title}")
    print(f"LOGLINE {season.logline[:90]}")
    print(f"THEME   {season.theme} — {season.theme_statement[:70]}")
    print(f"TONE    {season.tone}   SETTING {season.setting}")

    print(f"\n{len(season.episodes)} episodes:")
    for e in season.episodes:
        print(f"  {e.number:>2}. {e.title:<34} {len(e.beats)} beats "
              f"{e.runtime:>5.1f}s   {e.function[:34]}")

    if len(season.episodes) != 10:
        fails.append(f"expected 10 episodes, got {len(season.episodes)}")

    titles = [e.title.lower() for e in season.episodes]
    if len(set(titles)) != len(titles):
        fails.append("duplicate episode titles")
    else:
        print("\n  all episode titles unique")

    names = {c.name for c in CAST}
    beats_total = sum(len(e.beats) for e in season.episodes)
    attributed = sum(1 for e in season.episodes for b in e.beats if b.character)
    print(f"  {attributed}/{beats_total} beats attributed to a character")
    invented = {b.character for e in season.episodes for b in e.beats
                if b.character and b.character not in names}
    if invented:
        fails.append(f"model invented characters: {sorted(invented)}")
    else:
        print("  no invented characters — every attribution is a real cast member")

    md = season.to_markdown()
    (root / "data" / "_story").mkdir(parents=True, exist_ok=True)
    (root / "data" / "_story" / "season.md").write_text(md, encoding="utf-8")
    print(f"  markdown export: {len(md)} chars")

    # ---------------- render ----------------
    print("\nrendering episodes 1 and 2 …")
    outdir = root / "data" / "_story" / "episodes"
    if outdir.exists():
        shutil.rmtree(outdir)
    t0 = time.time()
    results, _ = assembly.assemble_season(
        season, outdir, cfg, aspect="9:16", caption_style="pop",
        music_mood="auto", auto_sfx=True, max_episodes=2,
        progress=lambda p, m: print(f"  [{p:3d}%] {m}"))
    print(f"  rendered in {time.time() - t0:.1f}s\n")

    for r in results:
        ok = r.path is not None and r.path.exists()
        size = r.path.stat().st_size / 1e6 if ok else 0
        print(f"  ep{r.number} {r.title[:26]:<28} "
              f"{'OK' if ok else 'FAIL'}  {r.duration:>5.1f}s  {size:>5.2f} MB  "
              f"music={r.music} sfx={'+'.join(r.stingers) or '-'}")
        for n in r.notes[:3]:
            print(f"      note: {n}")
        if r.error:
            print(f"      ERROR: {r.error}")
        if not ok:
            fails.append(f"episode {r.number} did not render: {r.error}")
        elif size < 0.05:
            fails.append(f"episode {r.number} suspiciously small ({size:.3f} MB)")

    # ---------------- filtergraph guard ----------------
    # This caught a real bug: "[bed]amix" (a label stripped of its brackets
    # and concatenated) fails in ffmpeg as "No such filter: 'bedamix'".
    print("\nfiltergraph guard")
    from momentclip import soundtrack as _st
    guard_cases = [
        ("stripped label (the bug)",
         "[1:a]aformat=channel_layouts=stereo[bed];bedamix=inputs=2[x];[0:a][x]amix=inputs=2[aout]",
         True),
        ("correct bracketed label",
         "[1:a]aformat=channel_layouts=stereo[bed];[0:a][bed]amix=inputs=2[aout]",
         False),
        ("unescaped comma in min()",
         "crop=w='1080/(1.02+0.05*min(1,n/240))':h=1920", True),
        ("escaped comma in min()",
         "crop=w='1080/(1.02+0.05*min(1\\,n/240))':h=1920", False),
    ]
    for label, graph, want_flag in guard_cases:
        got = _st.check_filtergraph(graph)
        ok = bool(got) == want_flag
        print(f"  {label:<26} {'ok' if ok else 'WRONG'}  "
              f"{'flagged' if got else 'clean'}")
        if not ok:
            fails.append(f"filtergraph guard wrong for {label!r}")

    print()
    if fails:
        print(f"  {len(fails)} FAILURES")
        for f in fails:
            print(f"    - {f}")
        return 1
    print("  all story checks passed")
    print(f"\n  season doc : {root / 'data' / '_story' / 'season.md'}")
    print(f"  episodes   : {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())