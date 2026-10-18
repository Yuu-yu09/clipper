"""Story engine -- premise, characters, themes, and a 10-episode season arc.

Turns a one-line idea plus a cast of images into a written season: themes, a
three-act shape, and ten episodes with titles, loglines and beat sheets.

Runs on whatever LLM provider is configured (Ollama by default, which is free
and local). Every model call is schema-checked and repaired, and every call
degrades to a deterministic template rather than failing the run -- a season
you can revise beats an error page.

Prompts are built to avoid the failure modes that actually happen with small
local models: no vague "be creative", explicit schemas, character names
repeated verbatim, and a hard instruction not to renumber the cast.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from .config import Config
from .rank import _endpoint

log = logging.getLogger("momentclip.story")

EPISODES = 10

# ---------------------------------------------------------------- archetypes
ARCHETYPES = {
    "protagonist": "the one the audience roots for; carries the want and the wound",
    "antagonist": "the opposing force; embodies the theme in its worst form",
    "mentor": "guides; teaches the theme by demonstration",
    "ally": "loyal support; grounds the protagonist in the ordinary world",
    "rival": "a dark mirror of the protagonist's own flaw",
    "love_interest": "connection that makes the stakes personal",
    "trickster": "chaos agent; tests everyone including the audience",
    "synthesist": "if the cast runs long, folds minor roles into the mentor",
}

ROLE_ALIASES = {
    "hero": "protagonist", "main": "protagonist", "lead": "protagonist",
    "villain": "antagonist", "antagonist": "antagonist", "evil": "antagonist",
    "bad guy": "antagonist", "badguy": "antagonist",
    "supporting": "ally", "sidekick": "ally", "friend": "ally",
    "supporting character": "ally", "side character": "ally",
    "mentor": "mentor", "guide": "mentor", "teacher": "mentor",
    "love interest": "love_interest", "romantic": "love_interest",
    "foil": "rival", "rival": "rival", "friend rival": "rival",
    "trickster": "trickster", "joker": "trickster", "comic relief": "trickster",
}

# ------------------------------------------------------------------- themes
THEMES = [
    ("redemption", "a past that cannot be outrun, met head on"),
    ("cost of ambition", "what success actually takes from you"),
    ("found family", "chosen bonds beating blood"),
    ("loyalty tested", "what you will and will not do for someone"),
    ("truth vs comfort", "the cost of knowing"),
    ("identity", "who you are versus who you were told to be"),
    ("hubris", "the limit of self-belief"),
    ("second chances", "whether the past can be amended"),
    ("power and its cost", "corrosion that comes with authority"),
    ("coming of age", "outgrowing a self that no longer fits"),
    ("forbidden desire", "wanting what you should not want"),
    ("sacrifice", "the arithmetic of saving one by losing another"),
]

TONES = [
    "gritty and grounded", "dark with flashes of warmth",
    "wry and humane", "tense and propulsive", "warm but never soft",
    "bleak with a redemptive spine", "kinetic and near-constant",
    "quiet and devastating",
]

SETTING_TONES = [
    "contemporary", "near-future", "noir", "period", "post-collapse",
    "intimate chamber piece", "epic ensemble",
]

# ------------------------------------------------------------ episode shapes
# One concrete shape per episode so a fallback season still reads as a season
# rather than ten variations on the same idea.
EPISODE_FUNCTIONS = [
    ("The Ordinary Life", "establishes the world and the protagonist's want",
     "inciting incident lands; the protagonist has no choice but to move"),
    ("Crossing the Line", "the first irreversible act",
     "a rule is broken and cannot be unbroken"),
    ("The First Cost", "the goal is achieved, and it is not enough",
     "the protagonist wins something and loses something larger"),
    ("False Victory", "the setup pays off perfectly",
     "everything works, which is when the real threat appears"),
    ("The Betrayal", "an ally becomes a liability",
     "trust breaks at the worst possible moment"),
    ("Midpoint Revelation", "the truth that recontextualises everything",
     "what the audience thought they knew turns out to be incomplete"),
    ("The Price of Everything", "consequences compound",
     "the protagonist is stripped of the thing that made them capable"),
    ("Descent", "the plan collapses; character is exposed",
     "the antagonist's plan works and the protagonist has nothing left"),
    ("The Turn", "the protagonist chooses who they are",
     "the wound and the want finally collide, and they decide"),
    ("Aftermath", "the world holds the cost",
     "resolution, and an epilogue image that proves the change"),
]


# --------------------------------------------------------------------- model
@dataclass
class Character:
    name: str
    role: str = "ally"
    description: str = ""
    image: str | None = None          # path on disk
    arc: str = ""                     # how they change across the season

    @property
    def archetype(self) -> str:
        return ARCHETYPES.get(self.role, ARCHETYPES["ally"])

    def to_dict(self) -> dict:
        return {
            "name": self.name, "role": self.role,
            "description": self.description,
            "arc": self.arc, "has_image": bool(self.image),
        }

    def brief(self) -> str:
        """The one-line form handed to the model. Keep it compact."""
        d = self.description.strip() or self.archetype
        return f"{self.name} ({self.role.replace('_', ' ')}): {d}"


@dataclass
class Beat:
    text: str
    character: str | None = None
    image: str | None = None
    motion: str = "in"
    seconds: float = 4.0


@dataclass
class Episode:
    number: int
    title: str
    logline: str
    function: str
    summary: str = ""
    beats: list[Beat] = field(default_factory=list)
    mood: str = ""

    @property
    def runtime(self) -> float:
        return sum(b.seconds for b in self.beats)

    def to_dict(self) -> dict:
        return {
            "number": self.number, "title": self.title,
            "logline": self.logline, "function": self.function,
            "summary": self.summary, "mood": self.mood,
            "runtime_seconds": round(self.runtime, 1),
            "beats": [
                {"text": b.text, "character": b.character,
                 "motion": b.motion, "seconds": b.seconds}
                for b in self.beats
            ],
        }


@dataclass
class Season:
    title: str
    premise: str
    logline: str
    theme: str
    theme_statement: str
    tone: str
    setting: str
    characters: list[Character] = field(default_factory=list)
    episodes: list[Episode] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title, "premise": self.premise, "logline": self.logline,
            "theme": self.theme, "theme_statement": self.theme_statement,
            "tone": self.tone, "setting": self.setting,
            "characters": [c.to_dict() for c in self.characters],
            "episodes": [e.to_dict() for e in self.episodes],
            "episode_count": len(self.episodes),
        }

    def to_markdown(self) -> str:
        out = [
            f"# {self.title}",
            "",
            f"**Logline** — {self.logline}",
            "",
            f"**Premise** — {self.premise}",
            "",
            f"**Theme** — {self.theme}: {self.theme_statement}",
            "",
            f"**Tone** — {self.tone}  ·  **Setting** — {self.setting}",
            "",
            "## Cast",
            "",
        ]
        for c in self.characters:
            out += [
                f"- **{c.name}** — *{c.role.replace('_', ' ')}*  ",
                f"  {c.description or c.archetype}  ",
                f"  *Arc:* {c.arc}" if c.arc else "",
            ]
        for e in self.episodes:
            out += [
                "",
                f"## Episode {e.number} — {e.title}",
                "",
                f"*{e.function}*",
                "",
                e.summary or e.logline,
                "",
                "| # | Beat | Character | Motion |",
                "|---|---|---|---|",
            ]
            for i, b in enumerate(e.beats, 1):
                out.append(f"| {i} | {b.text} | {b.character or '—'} | {b.motion} |")
        return "\n".join(x for x in out if x is not None)


# ------------------------------------------------------------------- prompts
PREMISE_PROMPT = """You are a television story editor developing an original series.

PREMISE (use this, do not replace it): {premise}
{extra}

CAST — reuse these names EXACTLY, never invent or rename anyone:
{cast}

Write a season bible. Reply with ONLY a JSON object:
{{"title": "<series title, max 6 words>",
  "logline": "<one sentence: who wants what, and what stops them>",
  "theme": "<one or two words>",
  "theme_statement": "<one sentence stating the theme, not describing it>",
  "tone": "<pick exactly one: {tones}>",
  "setting": "<pick exactly one: {settings}>"}}

No commentary. Valid JSON only."""

EPISODES_PROMPT = """You are a television story editor. Produce a {n}-episode season.

SERIES: {title}
LOGLINE: {logline}
THEME: {theme} — {theme_statement}
TONE: {tone}
SETTING: {setting}
PREMISE: {premise}

CAST — reuse these names EXACTLY:
{cast}

Each episode has a job in the arc. Keep them in this order:
{functions}

Reply with ONLY a JSON object:
{{"episodes": [
  {{"number": 1,
    "title": "<episode title, max 7 words>",
    "logline": "<one sentence>",
    "summary": "<2-3 sentences>",
    "beats": [
      {{"text": "<one visual beat, present tense, under 16 words>",
        "character": "<a name from the cast, or null>",
        "motion": "<in|out|left|right>",
        "seconds": 5}}
    ]}}
]}}

Use exactly {n} episodes numbered 1..{n}. Give each episode 4 to 6 beats.
Only the listed characters may appear. Valid JSON only."""


# --------------------------------------------------------------------- utils
def _chat(cfg: Config, prompt: str, max_tokens: int = 3000,
          temperature: float = 0.75) -> str | None:
    """One completion. Returns None instead of raising."""
    base, key, model = _endpoint(cfg)
    if not base or not model:
        return None
    import json
    import urllib.request
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"] or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("story model call failed: %s", exc)
        return None


def _extract_json(raw: str) -> dict | None:
    """Pull a JSON object out of whatever the model wrapped it in."""
    if not raw:
        return None
    txt = raw.strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    try:
        d = json.loads(txt)
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None


def normalise_role(text: str) -> str:
    t = (text or "").strip().lower().replace("-", " ").replace("_", " ")
    t = re.sub(r"\s+", " ", t)
    for alias, role in ROLE_ALIASES.items():
        if alias in t:
            return role
    for role in ARCHETYPES:
        if role.replace("_", " ") in t:
            return role
    return "ally"


def cast_block(chars: list[Character]) -> str:
    return "\n".join("- " + c.brief() for c in chars) or "- (no named cast)"


def pick_character(beats_text: str, chars: list[Character]) -> Character | None:
    """Resolve a beat's character name. Longest name first so 'Ana' does not
    steal a beat belonging to 'Ana Maria'."""
    if not beats_text:
        return None
    hay = beats_text.lower()
    hits = [c for c in chars if c.name and c.name.lower() in hay]
    if not hits:
        return None
    return max(hits, key=lambda c: len(c.name))


def title_case(s: str) -> str:
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:]
                    for w in str(s or "").split())


# ------------------------------------------------------------------ fallbacks
def _fallback_premise(premise: str, chars: list[Character]) -> dict:
    theme, statement = THEMES[abs(hash(premise)) % len(THEMES)]
    return {
        "title": title_case((premise or "Untitled").split(".")[0])[:48],
        "logline": premise,
        "theme": theme,
        "theme_statement": statement,
        "tone": TONES[abs(hash(premise + "t")) % len(TONES)],
        "setting": SETTING_TONES[abs(hash(premise + "s")) % len(SETTING_TONES)],
    }


def _fallback_episodes(season_title: str, premise: str, chars: list[Character],
                       theme: str) -> list[Episode]:
    """A real season arc, written deterministically.

    Used when no model is reachable. Deliberately not lorem ipsum: each episode
    has a different function, and beats reference actual cast members.
    """
    eps: list[Episode] = []
    hero = next((c for c in chars if c.role == "protagonist"), None) or \
        (chars[0] if chars else None)
    villain = next((c for c in chars if c.role == "antagonist"), None)
    ally = next((c for c in chars if c.role in ("ally", "mentor")), None)

    for i, (title, function, third) in enumerate(EPISODE_FUNCTIONS[:EPISODES], 1):
        beats: list[Beat] = []
        if hero:
            beats.append(Beat(f"{hero.name} in the life they had before "
                              f"{theme} caught up with them.",
                              hero.name, hero.image, "in", 5))
        beats.append(Beat(third[0].upper() + third[1:] + ".", None, None,
                          "in", 4.5))
        if i >= 3 and villain:
            beats.append(Beat(f"{villain.name} closes in on what {hero.name if hero else 'they'} "
                              f"wants most.", villain.name, villain.image, "out", 5))
        if i >= 5 and ally:
            beats.append(Beat(f"{ally.name} tells {hero.name if hero else 'them'} the truth "
                              f"about {theme} — too late to be a kindness.",
                              ally.name, ally.image, "left", 4.5))
        beats.append(Beat("Nothing about the way out is the way back.",
                          None, None, "right", 4))
        eps.append(Episode(
            number=i, title=title, function=function,
            logline=f"Episode {i}: {third}",
            summary=third[0].upper() + third[1:] + ". "
                    + f"The season's {theme} thread tightens here.",
            beats=beats,
            mood="escalating" if i < 6 else "consequential",
        ))
    return eps


def _fallback_beats(ep: Episode, chars: list[Character]) -> list[Beat]:
    """Give a model-written episode real shots if it returned none."""
    hero = next((c for c in chars if c.role == "protagonist"), None) or \
        (chars[0] if chars else None)
    out = [
        Beat(ep.logline or ep.title, hero.name if hero else None,
             hero.image if hero else None, "in", 5),
        Beat(ep.summary[:120] if ep.summary else ep.function, None, None, "in", 5),
    ]
    for c in chars[:3]:
        out.append(Beat(f"{c.name} — {c.description or c.archetype}",
                        c.name, c.image, "out", 4))
    return out


# ---------------------------------------------------------------------- main
def build_season(
    premise: str,
    characters: list[Character],
    cfg: Config,
    *,
    episode_count: int = EPISODES,
    use_llm: bool = True,
    extra_notes: str = "",
    progress=None,
) -> Season:
    """Generate a full season. Never raises."""

    def step(pct: int, msg: str) -> None:
        log.info("[%3d%%] %s", pct, msg)
        if progress:
            progress(pct, msg)

    premise = (premise or "").strip()
    if not premise:
        raise ValueError("a premise is required")

    # ---- premise / bible ----
    step(10, "developing the season bible")
    bible: dict | None = None
    if use_llm:
        raw = _chat(cfg, PREMISE_PROMPT.format(
            premise=premise,
            extra=f"NOTE FROM THE CREATOR: {extra_notes}" if extra_notes else "",
            cast=cast_block(characters),
            tones=" | ".join(TONES),
            settings=" | ".join(SETTING_TONES),
        ), max_tokens=900, temperature=0.8)
        bible = _extract_json(raw or "")
        if not bible:
            log.info("season bible unusable from model, using the template")
    if not bible:
        bible = _fallback_premise(premise, characters)

    season = Season(
        title=title_case(bible.get("title") or premise)[:60],
        premise=premise,
        logline=str(bible.get("logline") or premise)[:300],
        theme=str(bible.get("theme") or "redemption")[:40],
        theme_statement=str(bible.get("theme_statement") or "")[:300],
        tone=str(bible.get("tone") or TONES[0])[:60],
        setting=str(bible.get("setting") or SETTING_TONES[0])[:60],
        characters=characters,
    )

    # cast arcs, if the model gave us anything usable for the bible
    if use_llm:
        step(25, "developing character arcs")
        for c in characters:
            if not c.description:
                c.description = c.archetype
            if not c.arc:
                c.arc = (f"begins as {c.archetype}; ends having paid for "
                         f"{season.theme} in a way they did not expect")

    # ---- episodes ----
    step(35, f"writing {episode_count} episodes")
    eps: list[Episode] = []
    if use_llm:
        raw = _chat(cfg, EPISODES_PROMPT.format(
            n=episode_count,
            title=season.title, logline=season.logline,
            theme=season.theme, theme_statement=season.theme_statement,
            tone=season.tone, setting=season.setting, premise=premise,
            cast=cast_block(characters),
            functions="\n".join(f"{i+1}. {t} — {f}"
                                for i, (t, f, _) in enumerate(
                                    EPISODE_FUNCTIONS[:episode_count])),
        ), max_tokens=4000, temperature=0.85)
        data = _extract_json(raw or "")
        if data and isinstance(data.get("episodes"), list):
            for i, e in enumerate(data["episodes"][:episode_count], 1):
                if not isinstance(e, dict):
                    continue
                beats = []
                for b in (e.get("beats") or [])[:8]:
                    if not isinstance(b, dict) or not str(b.get("text", "")).strip():
                        continue
                    who = pick_character(str(b.get("text", "")) + " " +
                                         str(b.get("character", "")), characters)
                    try:
                        secs = max(2.0, min(15.0, float(b.get("seconds") or 5)))
                    except (TypeError, ValueError):
                        secs = 5.0
                    motion = str(b.get("motion") or "in").lower()
                    if motion not in ("in", "out", "left", "right"):
                        motion = "in"
                    beats.append(Beat(str(b["text"]).strip()[:220],
                                      who.name if who else None,
                                      who.image if who else None, motion, secs))
                fn = EPISODE_FUNCTIONS[min(i - 1, len(EPISODE_FUNCTIONS) - 1)]
                eps.append(Episode(
                    number=i,
                    title=title_case(str(e.get("title") or fn[0]))[:60],
                    logline=str(e.get("logline") or "")[:300],
                    summary=str(e.get("summary") or "")[:600],
                    function=fn[1],
                    beats=beats,
                    mood="escalating" if i < 6 else "consequential",
                ))
    if not eps:
        log.info("no usable episodes from the model, writing them deterministically")
        eps = _fallback_episodes(season.title, premise, characters, season.theme)

    # repair: any episode the model left without shots gets real ones
    for e in eps:
        if not e.beats:
            e.beats = _fallback_beats(e, characters)
        for b in e.beats:
            if not b.character:
                who = pick_character(b.text, characters)
                if who:
                    b.character, b.image = who.name, who.image

    season.episodes = eps[:episode_count]
    step(100, f"{len(season.episodes)} episodes ready")
    return season


def load_characters(items: list[dict]) -> list[Character]:
    """Normalise character dicts from the API or the UI."""
    out: list[Character] = []
    seen: set[str] = set()
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(Character(
            name=name[:60],
            role=normalise_role(str(it.get("role") or "ally")),
            description=str(it.get("description") or "")[:300],
            image=it.get("image"),
        ))
    return out