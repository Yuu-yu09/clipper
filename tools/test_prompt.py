#!/usr/bin/env python
"""Test the free-text prompt compiler and the caption preview endpoint.

The prompt compiler is the ClipAnything equivalent. Two properties matter:

  1. It must extract the five documented axes (subject / action / sentiment /
     sound) without a network call, so it works with zero API keys.
  2. It must NOT invent moments. It re-ranks moments that already exist, so
     every timestamp it produces has to be one the pipeline really found.

Run:  python tools/test_prompt.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from momentclip import asr, candidates, prompt, rank, signals
from momentclip.config import get_config

CASES = [
    # (prompt, keywords that must be found, sentiment OR keyword, sound cue)
    ("find the funniest moments",              [],            "funny",  None),
    ("where they argue",                       [],            "argue",  "argue"),
    ("show me all the laughing scenes",        [],            "funny",  "laugh"),
    ("audience cheering and applause",         [],            None,    "cheer"),
    ("most likely to go viral",                [],            "viral",  None),
    ("the most surprising revelations",        [],            "surprising", None),
    ("compile all the touchdowns",             ["touchdowns"], None,   None),
    ("find the moment where the jaguar kills the snake",
     ["jaguar", "snake"], None, None),
    ("do it",                                  [],            None,    None),
]

# Grammar and filler words that must never become searchable topics.
LEAKY = ["they", "them", "their", "there", "each", "seconds", "long",
         "where", "find", "show", "video", "moment"]

VISUAL = "show me the woman in the yellow dress"
LENGTH = "make each clip 30 seconds long"


def main() -> int:
    fails: list[str] = []
    cfg = get_config()

    print("prompt compilation (no network, no API key)")
    print("  " + "-" * 72)
    for text, want_kw, want_sent, want_sound in CASES:
        p = prompt.compile_prompt(text)
        kw_ok = all(k in p.keywords for k in want_kw)
        # a cue may land on either axis -- "argue" is both a sound and a word
        s_ok = (want_sent is None) or (
            want_sent in p.sentiments or want_sent in p.sound_cues
            or any(want_sent in k for k in p.keywords))
        so_ok = (want_sound is None) or any(
            want_sound.startswith(c) or c.startswith(want_sound)
            for c in p.sound_cues)
        leaky = [w for w in LEAKY if w in p.keywords]
        status = "ok" if (kw_ok and s_ok and so_ok and not leaky) else "MISMATCH"
        print(f"  {text[:44]:<46} {status}")
        print(f"      keywords={p.keywords[:5]} mood={p.sentiments[:3]} "
              f"sound={p.sound_cues[:3]} intent={p.intent}")
        if not kw_ok:
            fails.append(f"missing keywords {want_kw} in {p.keywords} for {text!r}")
        if not s_ok:
            fails.append(f"missing sentiment {want_sent} for {text!r}")
        if not so_ok:
            fails.append(f"missing sound {want_sound} for {text!r}")
        if leaky:
            fails.append(f"stopwords leaked into keywords {leaky} for {text!r}")

    # ---- honesty checks -------------------------------------------------
    print("\nhonesty")
    v = prompt.compile_prompt(VISUAL)
    print(f"  visual prompt -> unsupported: {v.unsupported}")
    if not v.unsupported:
        fails.append("visual prompt did not report the vision gap")

    l = prompt.compile_prompt(LENGTH)
    print(f"  length prompt -> intent={l.intent} keywords={l.keywords[:4]}")
    print(f"  length prompts are not honoured: {prompt.DISCLAIMERS['length']}")

    e = prompt.compile_prompt("")
    print(f"  empty prompt -> active={e.active}")
    if e.active:
        fails.append("empty prompt compiled as active")

    # ---- it must not invent moments -------------------------------------
    print("\nno-transcript honesty")
    p = prompt.compile_prompt("find the touchdown", transcript=asr.Transcript())
    print(f"  notes: {p.notes}")
    if not p.notes:
        fails.append("no note about keyword matching without a transcript")

    # ---- end to end: prompt re-ranks real moments -----------------------
    print("\napplying a prompt to real candidates")
    root = Path(__file__).resolve().parent.parent
    wav = root / "data" / "_speech" / "audio16k.wav"
    tj = root / "data" / "_speech" / "transcript.json"
    if not wav.exists() or not tj.exists():
        print("  (speech fixture missing — run tools/test_transcript.py first)")
        print("\n" + ("FAILURES" if fails else "all prompt checks passed"))
        for f in fails:
            print(f"    - {f}")
        return 1 if fails else 0

    t = asr.Transcript.load(tj)
    track, events, dur = signals.analyse_audio(wav, cfg.frame_hz)
    moments = candidates.generate(t, events, track, t.duration or 40.0, cfg)
    print(f"  {len(moments)} candidate moments from the fixture")

    base = rank.rank(moments, "general", cfg, use_llm=False)
    before = {r.moment.id: r.final_score for r in base}

    pl = prompt.compile_prompt("the funniest moments about luck", t)
    after_list = prompt.apply_plan(pl, rank.rank(moments, "general", cfg,
                                                 use_llm=False))
    changed = sum(1 for r in after_list
                  if abs(r.final_score - before.get(r.moment.id, 0)) > 1e-6)
    print(f"  prompt '{pl.raw}' re-ranked {changed}/{len(after_list)} moments")

    # every returned moment must be one we actually found
    real_ids = {m.id for m in moments}
    invented = [r.moment.id for r in after_list if r.moment.id not in real_ids]
    print(f"  invented moments: {len(invented)}")
    if invented:
        fails.append(f"prompt invented moments: {invented}")
    if changed == 0:
        fails.append("prompt had no effect on the ranking")

    # timestamps must be untouched -- only scores may move
    orig = {m.id: (m.start, m.end) for m in moments}
    moved = [r.moment.id for r in after_list
             if (r.moment.start, r.moment.end) != orig.get(r.moment.id)]
    print(f"  timestamps changed: {len(moved)}")
    if moved:
        fails.append(f"prompt altered timestamps for {moved}")

    print("\n  top 3 after the prompt:")
    for r in after_list[:3]:
        kw = r.subscores.get("prompt_keywords", 0.0)
        print(f"    {r.moment.id} score={r.final_score:.3f} kw={kw:.2f} "
              f"{(r.moment.text or '')[:52]}")

    print()
    if fails:
        print(f"  {len(fails)} FAILURES")
        for f in fails:
            print(f"    - {f}")
        return 1
    print("  all prompt checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())