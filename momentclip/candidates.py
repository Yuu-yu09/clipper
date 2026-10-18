"""Candidate moment generation.

We over-generate here on purpose, then rank hard downstream. Cheap deterministic
passes beat asking a language model to scan a two-hour transcript for a good cut
point -- it costs nothing, it is reproducible, and it never misses a moment
because the model got bored.

Three generators, merged:
  1. sentence  -- groups of utterances that form a complete thought
  2. event     -- windows anchored on a detected signal event (a laugh, a shock)
  3. exhaustive-- fixed sliding window, the safety net so we never return nothing
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import asr
from .config import Config, get_config
from .signals import Event, window_features

log = logging.getLogger("momentclip.candidates")

# Words that mean the thought is not finished -- never cut here.
CONTINUATIONS = {
    "and", "but", "so", "because", "which", "that", "then", "also", "or", "if",
    "when", "while", "although", "though", "since", "unless", "until", "with",
    "like", "anyway", "basically", "actually", "literally", "honestly",
}

# Openers/closers that make a clip feel self-contained.
HOOK_PATTERNS = re.compile(
    r"\b(you (?:know|gonna|gotta)|here'?s (?:the|what|how)|wait|honestly|"
    r"the (?:craziest|wildest|most|best|worst)|nobody (?:believes|expected)|"
    r"i (?:can'?t|never|swear|hate|love)|stop|okay so|listen|what if|"
    r"it'?s (?:crazy|insane|actually)|turns out|plot twist|funny thing|"
    r"true story|didn'?t (?:you|we)|guess what)\b",
    re.I,
)
SURPRISE_WORDS = re.compile(
    r"\b(wow|whoa|oh my god|oh my gosh|what|holy|no way|actually|seriously|"
    r"unbelievable|insane|crazy|ridiculous|amazing|scared|shocked)\b", re.I
)
LAUGH_WORDS = re.compile(
    r"\b(lol|lmao|rofl|hahaha|hehe|funny|hilarious|killing me|"
    r"that'?s (?:so )?funny|dying)\b", re.I,
)

_WORD_RE = re.compile(r"[A-Za-z0-9']+")


@dataclass
class Moment:
    id: str
    start: float
    end: float
    text: str = ""
    words: list[asr.Word] = field(default_factory=list)
    features: dict[str, float] = field(default_factory=dict)
    source: str = "sentence"
    tag: str = ""

    # populated by the graph layer
    node: dict = field(default_factory=dict)
    graph_score: float = 0.0
    graph_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.end - self.start, 3),
            "text": self.text,
            "tag": self.tag,
            "source": self.source,
            "features": {k: round(v, 4) for k, v in self.features.items()},
            "graph_score": round(self.graph_score, 4),
            "graph_reasons": self.graph_reasons,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _keywords(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text) if len(w) > 3}


def _snap_to_words(words: list[asr.Word], start: float, end: float,
                   cfg: Config) -> tuple[float, float, list[asr.Word]]:
    """Snap a window outward to the nearest word edges, so we never cut mid-word."""
    if not words:
        return start, end, []
    inside = [w for w in words if w.start < end and w.end > start]
    if inside:
        return inside[0].start, inside[-1].end, inside

    before = [w for w in words if w.end <= start]
    after = [w for w in words if w.start >= end]
    if before and after:
        s = before[-1].start if (start - before[-1].end) < (after[0].start - end) else start
        e = after[0].end if (start - before[-1].end) < (after[0].start - end) else end
        return s, e, []
    return start, end, []


def _complete_thought(words: list[asr.Word]) -> tuple[bool, str]:
    """A clip should start and finish on a thought boundary."""
    if not words:
        return False, "no words"
    first = words[0].text.strip().lower().strip(",.;:!?")
    last = words[-1].text.strip().lower().strip(",.;:!?")
    problems = []
    if first in CONTINUATIONS:
        problems.append(f"opens on connective {first!r}")
    if last in CONTINUATIONS:
        problems.append(f"ends on connective {last!r}")
    return (not problems, "; ".join(problems))


def _guess_tag(text: str) -> str:
    if LAUGH_WORDS.search(text):
        return "funny"
    if SURPRISE_WORDS.search(text):
        return "surprising"
    if HOOK_PATTERNS.search(text):
        return "candid"
    return ""


# --------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------
def _from_sentences(transcript: asr.Transcript, cfg: Config) -> list[tuple[float, float, str]]:
    """Group utterances until a sentence boundary closes a thought."""
    out: list[tuple[float, float, str]] = []
    utts = transcript.utterances
    i = 0
    while i < len(utts):
        j = i
        start = utts[i].start
        while j < len(utts):
            end = utts[j].end
            too_long = (end - start) > cfg.max_clip * 1.6
            if asr.ends_sentence(utts[j].text) and (end - start) >= cfg.min_clip * 0.6:
                break
            if too_long:
                break
            j += 1
        # The inner loop can exit with j == len(utts) when nothing closed the
        # thought (a trailing run of fragments, or speech with no punctuation).
        # Clamp rather than index past the end.
        j = min(j, len(utts) - 1)
        end = utts[j].end
        if cfg.min_clip <= (end - start) <= cfg.max_clip:
            text = " ".join(u.text for u in utts[i : j + 1]).strip()
            out.append((start, end, text))
        i = j + 1
    return out


def _from_events(events: list[Event], track: dict, duration: float,
                 cfg: Config) -> list[tuple[float, float, str]]:
    """Anchor windows on the strongest detected spectacle events."""
    order = ["laughter", "energy_shock", "pitch_surge", "overlap", "silence_beat"]
    by_kind: dict[str, list[Event]] = {k: [] for k in order}
    for e in events:
        if e.kind in by_kind:
            by_kind[e.kind].append(e)

    out: list[tuple[float, float, str]] = []
    for kind in order:
        # best few per kind, strongest first
        for ev in sorted(by_kind[kind], key=lambda e: -e.strength)[:12]:
            for target in (35.0, 55.0, 80.0):
                target = min(target, duration)
                half = target / 2.0
                # Centre the window on the event. Anchoring at the start makes
                # every long window on a short source collapse to the same
                # end-of-file range, which then dedupes to nothing useful.
                start = max(0.0, min(duration - target, ev.start - half))
                end = min(duration, start + target)
                if (end - start) < cfg.min_clip:
                    continue
                out.append((start, end, ""))
    return out


def _exhaustive(duration: float, cfg: Config) -> list[tuple[float, float, str]]:
    """Safety net so a short, silent or untalkative input still yields something.

    The window shrinks on short sources: with a fixed 45 s window a 90-second
    clip can hold barely two candidates, which starves the ranker even though
    the source has plenty of usable footage.
    """
    target_windows = max(2, min(12, int(duration // max(30.0, cfg.max_clip * 0.8)) + 2))
    window = min(cfg.max_clip, max(cfg.min_clip * 2.0, duration / target_windows))
    out = []
    step = window * 0.5
    t = 0.0
    while t < duration - cfg.min_clip:
        end = min(duration, t + window)
        if (end - t) >= cfg.min_clip:
            out.append((t, end, ""))
        t += step
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def generate(transcript: asr.Transcript, events: list[Event],
             track: dict, duration: float,
             cfg: Config | None = None) -> list[Moment]:
    cfg = cfg or get_config()
    if duration <= 0:
        return []

    raw: list[tuple[float, float, str, str]] = []
    if transcript.available:
        raw += [(s, e, t, "sentence") for s, e, t in _from_sentences(transcript, cfg)]
    # Event-anchored windows are the highest-value candidates and must NOT be
    # gated on transcription: a signal spike is a moment whether or not we
    # managed to get words for it.
    raw += [(s, e, "", "event") for s, e, _ in _from_events(events, track, duration, cfg)]
    raw += [(s, e, "", "exhaustive") for s, e, _ in _exhaustive(duration, cfg)]

    # normalise length + snap to words
    words = transcript.words()
    prepared: list[Moment] = []
    for idx, (start, end, text, source) in enumerate(raw):
        end = min(float(duration), max(start + cfg.min_clip, end))
        start = max(0.0, start - cfg.pad_before)
        end = min(float(duration), end + cfg.pad_after)

        if (end - start) > cfg.max_clip:
            end = start + cfg.max_clip
        if (end - start) < cfg.min_clip:
            continue

        start, end, w = _snap_to_words(words, start, end, cfg)
        if w:
            text = text or " ".join(x.text for x in w).strip()
            start, end = w[0].start, w[-1].end
            if (end - start) < cfg.min_clip or (end - start) > cfg.max_clip:
                continue
        else:
            start = max(0.0, start)
            end = min(float(duration), end)
            if (end - start) < cfg.min_clip:
                continue

        prepared.append(Moment(id=f"m{idx:04d}", start=start, end=end,
                               text=text, words=w, source=source))

    prepared = _dedupe_overlaps(prepared)

    # attach features + text-derived tags
    for m in prepared:
        m.features = window_features(track, events, m.start, m.end)
        if not m.text and transcript.available:
            m.text = transcript.slice_text(m.start, m.end)
        m.tag = _guess_tag(m.text)
        ok, why = _complete_thought(m.words)
        m.features["complete_thought"] = 1.0 if ok else 0.0
        if not ok:
            m.features["boundary_penalty"] = 0.6
        if HOOK_PATTERNS.search(m.text or ""):
            m.features["hook"] = 1.0
        if SURPRISE_WORDS.search(m.text or ""):
            m.features["surprise_words"] = 1.0
        if LAUGH_WORDS.search(m.text or ""):
            m.features["laugh_words"] = 1.0
        kws = _keywords(m.text or "")
        m.features["word_count"] = float(len(kws))

    log.info("generated %d candidate moments from %d raw windows",
             len(prepared), len(raw))
    return prepared


def _dedupe_overlaps(moments: list[Moment], iou_max: float = 0.80) -> list[Moment]:
    """Collapse near-identical windows, preferring the earlier (better context) one."""
    ordered = sorted(moments, key=lambda m: (m.start, -(m.end - m.start)))
    kept: list[Moment] = []
    for m in ordered:
        clash = False
        for k in kept:
            inter = max(0.0, min(m.end, k.end) - max(m.start, k.start))
            union = max(m.end, k.end) - min(m.start, k.start)
            if union <= 0:
                continue
            if (inter / union) > iou_max:
                clash = True
                break
        if not clash:
            kept.append(m)
    kept.sort(key=lambda m: m.start)
    for i, m in enumerate(kept):
        m.id = f"m{i:04d}"
    return kept