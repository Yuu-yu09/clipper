"""Caption generation.

Produces a sidecar .srt always, and a styled .ass when we have word-level
timestamps. No watermark is ever drawn onto a frame -- the caption text is the
only overlay this project puts on a clip.
"""

from __future__ import annotations

import re
from pathlib import Path

from .asr import Word

_SRT_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")


def _srt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:  # rounding overflow
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs == 100:
        cs = 0
        s += 1
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def group_words(words: list[Word], max_chars: int = 32, max_words: int = 5) -> list[list[Word]]:
    """Greedy caption grouping -- breaks on length, word count, or punctuation."""
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        trial = cur + [w]
        text = " ".join(x.text for x in trial).strip()
        ends_sentence = w.text.strip().endswith((".", "!", "?", "…"))
        if cur and (len(text) > max_chars or len(trial) > max_words or ends_sentence):
            groups.append(cur)
            cur = [w]
        else:
            cur = trial
    if cur:
        groups.append(cur)
    return groups


def write_srt(words: list[Word], path: str | Path) -> Path | None:
    path = Path(path)
    groups = group_words(words)
    if not groups:
        return None
    blocks = []
    for i, g in enumerate(groups, 1):
        text = " ".join(w.text for w in g).strip()
        blocks.append(f"{i}\n{_srt_ts(g[0].start)} --> {_srt_ts(g[-1].end)}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


PRESETS = {
    "clean": {
        "font": "Segoe UI",
        "size": 64,
        "primary": "&H00FFFFFF",
        "outline": "&H00000000",
        "outline_w": 3.0,
        "shadow": 0.0,
        "bold": 1,
        "align": 2,
        "margin_v": 220,
    },
    "pop": {
        "font": "Impact",
        "size": 82,
        "primary": "&H00FFFFFF",
        "highlight": "&H0046F0FF",
        "outline": "&H00000000",
        "outline_w": 5.0,
        "shadow": 1.5,
        "bold": 1,
        "align": 2,
        "margin_v": 260,
    },
    "podcast": {
        "font": "Arial",
        "size": 54,
        "primary": "&H00FFFFFF",
        "outline": "&H00101010",
        "outline_w": 2.5,
        "shadow": 0.5,
        "bold": 0,
        "align": 2,
        "margin_v": 140,
    },
}


def write_ass(words: list[Word], path: str | Path, preset: str = "pop",
              offset: float = 0.0, width: int = 1080, height: int = 1920) -> Path | None:
    """Styled ASS, relative to the clip start.

    Karaoke is approximated by colouring the active word via a per-word event,
    which reads far better than one colour for the whole line.
    """
    p = dict(PRESETS.get(preset, PRESETS["pop"]))
    p.setdefault("highlight", p.get("primary", "&H00FFFFFF"))
    p.setdefault("secondary", p.get("highlight"))
    path = Path(path)

    groups = group_words(words)
    if not groups:
        return None

    scale = height / 1920.0
    size = int(p["size"] * scale) or 48
    margin_v = int(p["margin_v"] * scale)
    play_resx, play_resy = 1080, 1920

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_resx}
PlayResY: {play_resy}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{p['font']},{size},{p['primary']},{p.get('secondary', p['primary'])},{p['outline']},&H80000000,{p['bold']},0,0,0,100,100,0,0,1,{p['outline_w']},{p['shadow']},{p['align']},60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = []
    for g in groups:
        line_start = g[0].start - offset
        line_end = g[-1].end - offset
        if line_end <= 0 or line_start >= (g[-1].end - g[0].start):
            continue
        line_start = max(0.0, line_start)

        # per-word karaoke colouring, clamped inside the line
        kf = ""
        for w in g:
            ws = max(line_start, min(line_end, w.start - offset))
            we = max(line_start, min(line_end, w.end - offset))
            if we <= ws:
                continue
            kf += f"{{\\kf1:{(ws - line_start) * 100:.0f},{max(1, (we - ws) * 100 - 1):.0f}}}"
        text = kf + " ".join(w.text for w in g).replace("\n", " ")
        lines.append(
            f"Dialogue: 0,{_ass_ts(line_start)},{_ass_ts(line_end)},Main,,0,0,0,,{text}"
        )

    if not lines:
        return None
    path.write_text(header + "\n".join(lines), encoding="utf-8")
    return path


def available_presets() -> list[str]:
    return list(PRESETS)