"""Modern short-form caption generation.

OpusClip-style captions are not subtitles -- they are word-by-word animations.
Each word gets its own timing, a colour change while it is being spoken, and a
scale "pop" as it lands. That is what \\\\k (karaoke timing) and \\\\t (transform)
override blocks do in ASS, and libass renders both.

Presets are modelled on the look people actually ship: bold yellow Hormozi,
clean white, boxed podcast, and a karaoke gradient.

A sidecar .srt is always written too -- some editors want plain subtitles.

This module never draws a watermark or logo. Captions are the speaker's own
words and nothing else is overlaid on the frame.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .asr import Word

_SRT_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")


def _srt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms, s = 0, s + 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs == 100:
        cs, s = 0, s + 1
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    """ASS needs literal braces and backslashes escaped inside override text."""
    return text.replace("\\", "\\").replace("{", "(").replace("}", ")").replace("\n", " ")


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------
@dataclass
class Preset:
    name: str
    font: str
    size: int                    # at 1920px tall; scaled for the real output
    primary: str                 # resting colour (ASS is &HAABBGGRR)
    active: str                  # colour of the word currently being spoken
    outline: str
    back: str
    outline_w: float = 3.0
    shadow: float = 0.0
    bold: int = 1
    italic: int = 0
    align: int = 2                # 2 = bottom centre
    margin_v: int = 260
    border_style: int = 1
    max_words: int = 4
    max_chars: int = 22
    # pop: scale up briefly as the word lands, then settle
    pop_scale: int = 118
    pop_ms: int = 110
    uppercase: bool = False
    # per-word gradient sweep instead of a flat highlight
    gradient: bool = False
    gap_ms: int = 60              # pause that ends a caption line


PRESETS: dict[str, Preset] = {
    "pop": Preset(
        name="pop", font="Impact", size=78,
        primary="&H00FFFFFF", active="&H0033E5FF",
        outline="&H00000000", back="&H90000000",
        outline_w=4.5, shadow=0.0, margin_v=280,
        pop_scale=122, pop_ms=110,
    ),
    "hormozi": Preset(
        # bold yellow on black, all caps -- the most-copied short-form look
        name="hormozi", font="Arial Black", size=84,
        primary="&H0000D7FF", active="&H0000FFFF",
        outline="&H00000000", back="&H80000000",
        outline_w=6.0, shadow=0.0, margin_v=300,
        pop_scale=126, pop_ms=120, uppercase=True,
    ),
    "clean": Preset(
        name="clean", font="Segoe UI", size=58,
        primary="&H00FFFFFF", active="&H00A8FF6E",
        outline="&H00101010", back="&H78000000",
        outline_w=2.6, shadow=0.4, bold=0, margin_v=200,
        pop_scale=110, pop_ms=90, max_words=5, max_chars=28,
    ),
    "podcast": Preset(
        name="podcast", font="Arial", size=52,
        primary="&H00FFFFFF", active="&H0000E5FF",
        outline="&H00101010", back="&H60000000",
        outline_w=2.2, shadow=0.3, bold=0, margin_v=130,
        pop_scale=106, pop_ms=80, max_words=6, max_chars=32,
    ),
    "boxed": Preset(
        name="boxed", font="Arial Black", size=70,
        primary="&H00FFFFFF", active="&H0046F0FF",
        outline="&H00000000", back="&HC8000000",
        outline_w=0.0, shadow=0.0, border_style=3, margin_v=250,
        pop_scale=115, pop_ms=100, uppercase=True,
    ),
    "gradient": Preset(
        name="gradient", font="Montserrat", size=72,
        primary="&H00FFFFFF", active="&H00E5FF4F",
        outline="&H00000000", back="&H80000000",
        outline_w=3.6, margin_v=270, gradient=True,
        pop_scale=124, pop_ms=130,
    ),
}


def available_presets() -> list[str]:
    return list(PRESETS)


def get_preset(name: str) -> Preset:
    return PRESETS.get(name, PRESETS["pop"])


# --------------------------------------------------------------------------
# grouping
# --------------------------------------------------------------------------
def group_words(words: list[Word], max_words: int = 4, max_chars: int = 22,
                gap_ms: int = 60) -> list[list[Word]]:
    """Chunk words into caption lines.

    Short-form captions show 3-4 short words at a time, not a whole sentence.
    Break on length, word count, sentence punctuation, or a real pause.
    """
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            prev = cur[-1]
            pause = (w.start - prev.end) * 1000.0
            if pause > gap_ms:
                groups.append(cur)
                cur = []
        cur.append(w)
        text = " ".join(x.text for x in cur).strip()
        ends = w.text.strip().endswith((".", "!", "?", "…"))
        if (len(text) > max_chars or len(cur) > max_words or ends) and cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return [g for g in groups if g]


def write_srt(words: list[Word], path: str | Path, max_words: int = 4,
              max_chars: int = 22) -> Path | None:
    path = Path(path)
    groups = group_words(words, max_words, max_chars)
    if not groups:
        return None
    blocks = []
    for i, g in enumerate(groups, 1):
        text = " ".join(w.text for w in g).strip()
        blocks.append(f"{i}\n{_srt_ts(g[0].start)} --> {_srt_ts(g[-1].end)}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# ASS
# --------------------------------------------------------------------------
def _word_override(p: Preset, ms: int, is_last: bool) -> str:
    """Build one word's override block: karaoke timing + colour + scale pop."""
    bits = [f"\\k{max(1, ms)}"]
    bits.append(f"\\c{p.active}")
    if p.pop_scale and p.pop_scale != 100:
        # overshoot on entry, settle back -- the "pop" everyone recognises
        bits.append(
            f"\\t(0,{max(20, p.pop_ms // 2)},\\fscx{p.pop_scale}\\fscy{p.pop_scale})"
            f"\\t({max(20, p.pop_ms // 2)},{p.pop_ms},\\fscx100\\fscy100)"
        )
    # return to the resting colour once the word is done
    return "{" + "".join(bits) + "}" if not is_last else \
        "{" + "".join(bits) + f"\\c{p.primary}" + "}"


def write_ass(words: list[Word], path: str | Path, preset: str = "pop",
              offset: float = 0.0, width: int = 1080, height: int = 1920,
              all_caps: bool | None = None) -> Path | None:
    """Styled ASS captions, relative to the clip start.

    `offset` is the clip's start time in the source, so timings are rebased to
    zero for the rendered segment.
    """
    p = get_preset(preset)
    caps = p.uppercase if all_caps is None else all_caps
    path = Path(path)

    groups = group_words(words, p.max_words, p.max_chars, p.gap_ms)
    if not groups:
        return None

    scale = max(0.5, height / 1920.0)
    size = max(12, int(p.size * scale))
    margin_v = int(p.margin_v * scale)

    header = f"""[Script Info]
; Generated by Clipper
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{p.font},{size},{p.primary},{p.active},{p.outline},{p.back},{p.bold},{p.italic},0,0,100,100,0.6,0,{p.border_style},{p.outline_w},{p.shadow},{p.align},80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines: list[str] = []
    for g in groups:
        line_start = g[0].start - offset
        line_end = g[-1].end - offset
        if line_end <= 0 or line_start < 0 and line_end <= 0:
            continue
        line_start = max(0.0, line_start)

        parts: list[str] = []
        for i, w in enumerate(g):
            is_last = (i == len(g) - 1)
            # karaoke duration is measured from this word's own start to the
            # end of the line, which is how libass advances \k
            dur_ms = int(round(((w.end if not is_last else w.end) - w.start) * 1000))
            dur_ms = max(1, dur_ms)
            text = _escape(w.text.strip())
            if caps:
                text = text.upper()
            parts.append(_word_override(p, dur_ms, is_last) + text)

        if not parts:
            continue
        body = (" " if not p.gradient else " ").join(parts)
        lines.append(
            f"Dialogue: 0,{_ass_ts(line_start)},{_ass_ts(line_end)},Cap,,0,0,0,,{body}"
        )

    if not lines:
        return None
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_headline(text: str, path: str | Path, offset: float = 0.0,
                   width: int = 1080, height: int = 1920,
                   hold: float = 3.5) -> Path | None:
    """A short hook line over the first few seconds of a clip.

    This is OpusClip's "auto headline": a hook pulled from the clip's own words.
    It is the speaker's text, not a watermark -- and it is off by default
    (CLIPPER_AUTO_HOOK), because OpusClip also disables it by default on API
    projects.
    """
    text = " ".join((text or "").split())[:120].strip()
    if not text:
        return None

    p = get_preset("pop")
    path = Path(path)
    scale = max(0.5, height / 1920.0)
    size = max(12, int(64 * scale))
    margin_v = int((height - 640) * scale)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Head,{p.font},{size},{p.active},{p.active},{p.outline},&H80000000,1,0,0,0,100,100,0.6,0,1,4.0,0.5,8,90,90,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    # fade in fast, hold, fade out
    body = _escape(text)
    anim = "{\\fad(200,250)}"
    line = (f"Dialogue: 0,{_ass_ts(0.0)},{_ass_ts(hold)},Head,,0,0,0,,"
            f"{anim}{body}")
    path.write_text(header + line + "\n", encoding="utf-8")
    return path