"""Free-text prompt compilation -- the ClipAnything equivalent.

OpusClip's ClipAnything takes a natural-language prompt and runs it through a
cloud multimodal model. This module gets as far as it can *without* a network
call, and hands the rest to the optional LLM judge.

The split matters, and it keeps this project's core rule intact:

    the prompt reweights and re-ranks candidates that signal processing and
    the moment graph already found. It never invents a timestamp.

So a prompt can change WHICH of the real moments win. It cannot make up a
moment that is not in your video.

A prompt is parsed along the same five axes OpusClip documents:

    Subject  -- who / what      -> transcript keywords, graph centrality
    Visual   -- what it looks like -> NOT IMPLEMENTED (needs a vision model)
    Action   -- what happens        -> transcript verbs, signal events
    Sentiment-- how it feels        -> signal features + emotion words
    Sound    -- what you hear       -> detected audio events

Everything except Visual is handled here. Visual is a real gap and is called
out in the UI rather than silently ignored.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import asr
from .candidates import Moment

log = logging.getLogger("momentclip.prompt")

_WORD = re.compile(r"[A-Za-z']+")


# --------------------------------------------------------------------------
# lexicons
# --------------------------------------------------------------------------
# Sentiment words -> the signal features that most plausibly carry them.
# Values are weights, not probabilities; the ranker combines them.
SENTIMENT: dict[str, dict[str, float]] = {
    "funny": {
        "laughter": 1.0, "energy_shock": 0.45, "hook": 0.30,
        "lexical_novelty": 0.20, "centrality": 0.20,
    },
    "hilarious": {
        "laughter": 1.0, "energy_shock": 0.45, "hook": 0.30,
        "lexical_novelty": 0.20, "centrality": 0.20,
    },
    "exciting": {
        "energy_shock": 0.85, "pitch_surge": 0.70, "signal_density": 0.55,
        "hook": 0.30,
    },
    "surprising": {
        "graph_surprise": 1.0, "pitch_surge": 0.45, "lexical_novelty": 0.65,
        "energy_shock": 0.30,
    },
    "shocking": {
        "graph_surprise": 1.0, "energy_shock": 0.55, "pitch_surge": 0.40,
        "lexical_novelty": 0.55,
    },
    "heartwarming": {"hook": 0.55, "complete_thought": 0.60, "length": 0.45},
    "inspirational": {"hook": 0.60, "complete_thought": 0.60, "length": 0.45},
    "emotional": {"pitch_surge": 0.60, "spectacle": 0.70, "hook": 0.35},
    "awkward": {"hook": 0.70, "complete_thought": 0.40, "lexical_novelty": 0.45},
    "embarrassing": {"hook": 0.70, "energy_shock": 0.45, "lexical_novelty": 0.50},
    "tense": {"silence_beat": 0.60, "energy_shock": 0.55, "graph_surprise": 0.40},
    "dramatic": {"energy_shock": 0.75, "silence_beat": 0.55, "spectacle": 0.60},
    "controversial": {"hook": 0.85, "overlap": 0.60, "spectacle": 0.55},
    "sad": {"pitch_surge": 0.25, "silence_beat": 0.70, "complete_thought": 0.55},
    "candid": {"complete_thought": 0.70, "hook": 0.55, "lexical_novelty": 0.40},
    "viral": {"hook": 0.85, "spectacle": 0.75, "length": 0.55, "centrality": 0.35},
    "memorable": {"centrality": 0.75, "hook": 0.50, "graph_surprise": 0.45},
    "insightful": {"complete_thought": 0.80, "length": 0.70, "hook": 0.40},
    "action": {"energy_shock": 0.90, "signal_density": 0.70, "spectacle": 0.65},
}

# Sound cues -> the audio event classes that most plausibly carry them.
SOUND_CUES: dict[str, dict[str, float]] = {
    "laugh": {"laughter": 1.0},
    "laughter": {"laughter": 1.0},
    "laughing": {"laughter": 1.0},
    "funny": {"laughter": 1.0},
    "cheer": {"laughter": 0.8, "energy_shock": 0.5},
    "cheering": {"laughter": 0.8, "energy_shock": 0.5},
    "applause": {"laughter": 0.9, "energy_shock": 0.4},
    "clap": {"laughter": 0.7},
    "clapping": {"laughter": 0.7},
    "shout": {"energy_shock": 0.85, "pitch_surge": 0.45},
    "yell": {"energy_shock": 0.85, "pitch_surge": 0.45},
    "scream": {"pitch_surge": 0.85, "energy_shock": 0.7},
    "bark": {"energy_shock": 0.45},
    "horn": {"energy_shock": 0.55},
    "crash": {"energy_shock": 0.9},
    "bang": {"energy_shock": 0.85},
    "gunshot": {"energy_shock": 0.9},
    "explosion": {"energy_shock": 1.0},
    "music": {},
    "silence": {"silence_beat": 0.9},
    "pause": {"silence_beat": 0.8},
    "argument": {"overlap": 0.9, "energy_shock": 0.5},
    "argue": {"overlap": 0.9, "energy_shock": 0.5},
    "interrupt": {"overlap": 0.8},
    "crowd": {"laughter": 0.5, "energy_shock": 0.4},
}

# Verbs and phrases that indicate an action rather than a topic.
_ACTION_HINTS = {
    "throw", "throws", "threw", "hit", "hits", "kicks", "kick", "scoring",
    "score", "scores", "goal", "smack", "slap", "punch", "jumps", "falls",
    "drops", "throws", "catches", "runs", "dancing", "sings", "explains",
    "unboxing", "shows", "demonstrates", "reveals", "kills", "attacks",
    "crying", "laughs", "yells", "argues", "attacks", "attacks",
}

# Words that are instructions to Clipper rather than content to search for.
_STOPWORDS = {
    "find", "show", "give", "me", "the", "all", "a", "an", "of", "moments",
    "moment", "clips", "clip", "video", "videos", "scenes", "scene", "parts",
    "part", "where", "when", "that", "this", "with", "and", "or", "to", "for",
    "in", "on", "at", "is", "are", "was", "were", "be", "been", "most",
    "likely", "go", "going", "viral", "social", "media", "please", "can",
    "you", "i", "want", "need", "compile", "create", "make", "get",
    "anything", "something", "from", "my", "our", "best", "good",
    "great", "interesting", "cool", "nice", "stuff", "things", "thing",
    # grammar and filler that would otherwise be mistaken for topics
    "they", "them", "their", "there", "then", "these", "those", "which",
    "who", "whom", "whose", "have", "has", "had", "does", "did", "doing",
    "each", "every", "other", "others", "same", "such", "into", "over",
    "under", "about", "after", "before", "during", "while", "because",
    # units and descriptors: these belong to controls, not to content
    "seconds", "second", "secs", "sec", "minutes", "minute", "mins", "min",
    "long", "short", "longer", "shorter", "big", "small", "biggest",
    "smallest", "much", "many", "few", "less", "little", "large", "tiny",
    "new", "old", "real", "actual", "first", "last", "next", "one", "two",
}


# Some sound cues imply a mood as well. "laughing scenes" is a request for
# the funny moments, not merely for a noise detection -- treat it as both.
_SOUND_TO_SENTIMENT = {
    "laugh": "funny", "laughter": "funny", "laughing": "funny", "funny": "funny",
    "cheer": "exciting", "cheering": "exciting", "applause": "exciting",
    "clap": "exciting", "clapping": "exciting",
    "shout": "exciting", "yell": "exciting", "scream": "exciting",
    "argument": "controversial", "argue": "controversial",
    "silence": "tense", "pause": "tense",
    "crash": "action", "bang": "action", "explosion": "action",
}


def _stem(word: str) -> str:
    """Prefix for loose matching, so 'funny' also matches 'funniest'.

    Dropping a trailing 'y' handles the funniest/funnier/funnily family,
    which a plain stemmer would not get right either.
    """
    return word[:-1] if len(word) > 4 and word.endswith("y") else word


def _has(text: str, word: str) -> bool:
    """Word-boundary match restricted to real inflections.

    A naive prefix match is a trap here: "yell" would then match "yellow" in
    "the woman in the yellow dress", and the prompt would claim the clip has
    shouting in it. Only actual English suffixes are accepted.
    """
    if word.endswith("y") and len(word) > 4:
        # funn + y / iest / ier / ily  ->  funny, funniest, funnier, funnily
        core = word[:-1]
        pat = rf"\b{re.escape(core)}(?:y|iest|ier|ily|ie)?\b"
    else:
        pat = rf"\b{re.escape(word)}(?:s|es|ed|d|ing|er|ers|est|ly)?\b"
    return re.search(pat, text) is not None


# --------------------------------------------------------------------------
@dataclass
class PromptPlan:
    raw: str
    keywords: list[str] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    sentiments: list[str] = field(default_factory=list)
    sound_cues: list[str] = field(default_factory=list)
    intent: str = "moment"          # moment | compile | generic
    count: int = 0                  # for "compile all X" style prompts
    has_transcript: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.raw.strip()) and bool(self.keywords or self.weights)

    @property
    def unsupported(self) -> list[str]:
        """Prompt axes we cannot honour without a vision model."""
        return list(self._unsupported)

    _unsupported: list[str] = field(default_factory=list)

    def describe(self) -> str:
        bits = []
        if self.keywords:
            bits.append(f"topics: {', '.join(self.keywords[:6])}")
        if self.sentiments:
            bits.append(f"mood: {', '.join(self.sentiments[:4])}")
        if self.sound_cues:
            bits.append(f"sound: {', '.join(self.sound_cues[:4])}")
        bits.append(f"intent: {self.intent}")
        return " · ".join(bits)


# --------------------------------------------------------------------------
def compile_prompt(text: str, transcript: asr.Transcript | None = None) -> PromptPlan:
    """Turn free text into a weighting plan. Never raises, never needs a network."""
    raw = (text or "").strip()
    plan = PromptPlan(raw=raw)
    if not raw:
        return plan

    low = raw.lower()
    tokens = [w for w in _WORD.findall(low)]
    plan.has_transcript = bool(transcript and transcript.available)

    # --- intent: compile-many vs find-one ------------------------------
    if re.search(r"\b(all|every|each|compile|collection|compilation|every time)\b", low):
        plan.intent = "compile"
    elif re.search(r"\b(the moment|that moment|one moment|single|highlight)\b", low):
        plan.intent = "moment"
    else:
        plan.intent = "generic"

    m = re.search(r"\b(\d+)\s*(?:clips?|moments?|videos?)\b", low)
    if m:
        plan.count = max(1, min(30, int(m.group(1))))

    # --- sentiment axis ------------------------------------------------
    for name, weights in SENTIMENT.items():
        if _has(low, name):
            plan.sentiments.append(name)
            for k, v in weights.items():
                plan.weights[k] = max(plan.weights.get(k, 0.0), v)

    # --- sound axis ----------------------------------------------------
    for cue, weights in SOUND_CUES.items():
        if _has(low, cue):
            plan.sound_cues.append(cue)
            for k, v in weights.items():
                plan.weights[k] = max(plan.weights.get(k, 0.0), v)

    # a sound cue can imply a mood as well
    for cue in plan.sound_cues:
        mood = _SOUND_TO_SENTIMENT.get(cue)
        if mood and mood not in plan.sentiments:
            plan.sentiments.append(mood)
            for k, v in SENTIMENT.get(mood, {}).items():
                plan.weights[k] = max(plan.weights.get(k, 0.0), v)

    # --- subject / topic keywords --------------------------------------
    vocab: set[str] = set()
    if plan.has_transcript and transcript is not None:
        from .momentgraph import STOPWORDS
        vocab = STOPWORDS
    matched_terms = set(plan.sentiments) | set(plan.sound_cues)
    for t in tokens:
        if len(t) < 3 or t in _STOPWORDS:
            continue
        if t in vocab:                 # a known filler word, not a topic
            continue
        # anything already claimed by the mood or sound axis is not also a topic
        if any(_has(t, m) or _has(m, t) for m in matched_terms):
            continue
        plan.keywords.append(t)

    # de-duplicate, preserve order, cap
    seen: set[str] = set()
    uniq = [k for k in plan.keywords if not (k in seen or seen.add(k))]
    plan.keywords = uniq[:8]

    # --- honesty: say what we cannot do --------------------------------
    visual_markers = re.search(
        r"\b(wearing|dressed|colou?r|colour|shirt|jacket|dress|camera angle|"
        r"close-?up|wide shot|background|scene with|looks like|appears|"
        r"wearing|holding|shows the)\b", low)
    if visual_markers:
        plan._unsupported.append(
            "visual appearance cues (no vision model -- "
            "matching on audio, transcript and graph structure only)")
    if not plan.has_transcript:
        plan.notes.append(
            "no transcript for this source: matching on audio signals and "
            "timing only, so spoken topics cannot be found")
    if plan.sound_cues:
        unknown = [c for c in plan.sound_cues
                   if not any(c.startswith(k) for k in SOUND_CUES)]
        if unknown:
            plan.notes.append(
                "no detector for sound: " + ", ".join(sorted(unknown)[:4]))
    if plan.keywords and not plan.has_transcript:
        plan.notes.append(
            f"keywords {', '.join(plan.keywords[:4])} cannot be matched "
            "without a transcript")

    log.info("prompt plan: %s", plan.describe())
    return plan


# --------------------------------------------------------------------------
def keyword_score(plan: PromptPlan, m: Moment) -> float:
    """Fraction of the prompt's topics that appear in this moment's words."""
    if not plan.keywords:
        return 0.0
    text = (m.text or "").lower()
    if not text:
        return 0.0
    toks = set(_WORD.findall(text))
    hits = sum(1 for k in plan.keywords if k in toks)
    return hits / len(plan.keywords)


def apply_plan(plan: PromptPlan, ranked, *, boost: float = 0.55):
    """Re-rank `ranked` under the plan.

    Keyword agreement is the dominant signal when the prompt names topics,
    because that is the most literal thing a prompt can ask for. Sentiment and
    sound weights then re-balance the sub-scores that matched.

    Moments that match nothing at all are kept but pushed down -- we never
    silently drop a real moment, because the prompt might have been phrased
    differently than we understood.
    """
    if not plan.active:
        return ranked

    for r in ranked:
        kw = keyword_score(plan, r.moment)
        r.subscores["prompt_keywords"] = kw

        # re-balance the signal sub-scores the prompt actually named
        if plan.weights:
            matched = 0.0
            total = 0.0
            for key, w in plan.weights.items():
                total += w
                matched += w * float(r.subscores.get(key, r.moment.features.get(key, 0.0)))
            r.subscores["prompt_affinity"] = matched / total if total else 0.0

            # apply the re-balance as a nudge to the base score
            r.final_score = _clamp(
                r.final_score * (0.65 + 0.70 * r.subscores["prompt_affinity"])
            )

        if plan.keywords:
            r.final_score = _clamp(r.final_score + boost * kw)
            if kw > 0:
                r.reason = (f"matches “{' '.join(plan.keywords[:3])}”"
                            + (f"; {r.reason}" if r.reason else ""))
        if kw == 0 and (plan.sentiments or plan.sound_cues):
            r.final_score = _clamp(r.final_score * 0.92)

        # "compile all X" wants breadth across the topic, so spread picks out
        # rather than letting one cluster dominate
        if plan.intent == "compile" and plan.keywords:
            r.subscores["prompt_spread"] = 1.0 - float(
                r.moment.features.get("graph_centrality", 0.0))
            r.final_score = _clamp(
                r.final_score * (0.88 + 0.24 * r.subscores["prompt_spread"]))

    ranked.sort(key=lambda r: -r.final_score)
    return ranked


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return float(max(lo, min(hi, x)))


# --------------------------------------------------------------------------
# suggestions shown in the UI, drawn from what we can actually honour
# --------------------------------------------------------------------------
EXAMPLES = [
    "the funniest moments",
    "most likely to go viral",
    "where they argue",
    "surprising revelations",
    "the strongest opinions",
    "audience cheering and applause",
    "awkward or embarrassing",
    "the most quotable lines",
]

# Prompts we deliberately do not pretend to support.
DISCLAIMERS = {
    "visual": "Visual cues (clothing, colours, objects, camera angles) need a "
              "vision model and are not supported.",
    "audio_edit": "Prompts cannot edit audio or video. Captions, music, zoom "
                  "and aspect ratio are set with their own controls.",
    "length": "Prompts cannot set clip length. Use the Clip length control.",
}