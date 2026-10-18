"""Ranking.

Two stages, and the order matters:

  1. `heuristic_rank`  -- deterministic. Signal + text + graph. Free, instant,
     reproducible, and it never misses a moment because a model skipped it.
  2. `llm_judge`      -- OPTIONAL, and only over a shortlist of ~24 candidates.
     A language model ranks the text of clips we already found. It never picks
     timestamps, so it cannot hallucinate a moment that isn't in the video.

The result: an LLM improves the ordering but can never invent or miss a clip.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace

from .candidates import Moment
from .config import Config, get_config

log = logging.getLogger("momentclip.rank")

PERSONAS = ("funny", "candid", "surprising", "general")


@dataclass
class Ranked:
    moment: Moment
    base_score: float
    final_score: float
    reason: str = ""
    subscores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = self.moment.to_dict()
        d.update({
            "score": round(self.final_score, 4),
            "base_score": round(self.base_score, 4),
            "reason": self.reason,
            "subscores": {k: round(v, 4) for k, v in self.subscores.items()},
        })
        return d


# --------------------------------------------------------------------------
# stage 1 -- deterministic
# --------------------------------------------------------------------------
def _length_score(duration: float) -> float:
    """Sweet spot around 45 s. Penalise both stub and sprawl."""
    if duration < 10:
        return 0.2
    if duration <= 25:
        return 0.6 + 0.4 * (duration - 10) / 15.0
    if duration <= 55:
        return 1.0
    if duration <= 90:
        return 1.0 - 0.5 * (duration - 55) / 35.0
    return 0.5


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return float(max(lo, min(hi, x)))


def heuristic_rank(moments: list[Moment], cfg: Config | None = None) -> list[Ranked]:
    cfg = cfg or get_config()
    ranked: list[Ranked] = []

    for m in moments:
        f = m.features
        dur = m.end - m.start

        spectacle = _clamp(f.get("spectacle", 0.0))
        hook = f.get("hook", 0.0)
        complete = f.get("complete_thought", 0.0)
        surprise = f.get("graph_surprise", 0.0)
        centrality = f.get("graph_centrality", 0.0)
        novelty = f.get("lexical_novelty", 0.5)
        density = _clamp(
            (f.get("laughter_density", 0.0)
             + f.get("energy_shock_density", 0.0)
             + f.get("pitch_surge_density", 0.0)) / 3.0
        )
        len_score = _length_score(dur)
        boundary_penalty = f.get("boundary_penalty", 0.0)

        subscores = {
            "spectacle": spectacle,
            "signal_density": density,
            "hook": hook,
            "complete_thought": complete,
            "surprise": surprise,
            "centrality": centrality,
            "lexical_novelty": novelty,
            "length": len_score,
        }

        score = (
            0.24 * spectacle
            + 0.10 * density
            + 0.12 * hook
            + 0.14 * complete
            + 0.14 * m.graph_score
            + 0.10 * novelty
            + 0.10 * len_score
            + 0.06 * surprise
        ) - 0.25 * boundary_penalty

        if not m.text:
            score -= 0.10  # no transcript support

        ranked.append(Ranked(
            moment=m, base_score=_clamp(score), final_score=_clamp(score),
            reason=_explain(subscores), subscores=subscores,
        ))

    ranked.sort(key=lambda r: -r.final_score)
    return ranked


def _explain(s: dict[str, float]) -> str:
    bits = []
    if s["spectacle"] > 0.45:
        bits.append("strong audio reaction")
    if s["surprise"] > 0.3:
        bits.append("connects unrelated threads")
    if s["centrality"] > 0.6:
        bits.append("recurring bit")
    if s["hook"] > 0.3:
        bits.append("strong hook")
    if s["complete_thought"] > 0.5 and s["hook"] == 0:
        bits.append("complete thought")
    if s["lexical_novelty"] > 0.75:
        bits.append("unusual topic")
    if s["length"] > 0.85:
        bits.append("good length")
    if not bits:
        bits.append("solid general-purpose clip")
    return ", ".join(bits)


# --------------------------------------------------------------------------
# diversity -- stop ten variations of the same joke
# --------------------------------------------------------------------------
def _select_diverse(ranked: list[Ranked], k: int) -> list[Ranked]:
    from .momentgraph import _content_terms

    chosen: list[Ranked] = []
    seen: list[set[str]] = []
    for r in ranked:
        if len(chosen) >= k:
            break
        terms = set(_content_terms(r.moment.text))
        if terms:
            redundant = False
            for prev in seen:
                union = terms | prev
                if union and len(terms & prev) / len(union) > 0.45:
                    redundant = True
                    break
            if redundant:
                continue
            seen.append(terms)
        chosen.append(r)
    # top up if diversity was too aggressive
    if len(chosen) < k:
        have = {id(r) for r in chosen}
        for r in ranked:
            if len(chosen) >= k:
                break
            if id(r) not in have:
                chosen.append(r)
    chosen.sort(key=lambda r: r.moment.start)
    return chosen


# --------------------------------------------------------------------------
# stage 2 -- optional LLM judge
# --------------------------------------------------------------------------
JUDGE_PROMPT = """You are a short-form video editor. You are given numbered \
candidate clips extracted from a longer video. Score how likely each is to be a \
genuinely {persona} moment that a viewer would replay and share.

Score 0-10. Judge the TRANSCRIPT TEXT and the reported signal evidence only. \
Do not invent new clips. Do not change timings.

Signal evidence per clip:
{evidence}

Clips:
{clips}

Return ONLY a JSON object of this exact shape, with one entry per clip:
{{"clips": [{{"id": "<the clip id>", "score": <0-10>, "reason": "<= 15 words>"}}]}}

Use the `id` given in each clip block. Do not invent ids. Do not add commentary.
"""


def _endpoint(cfg: Config) -> tuple[str | None, str | None, str | None]:
    """Resolve (base_url, api_key, model) across providers and raw config."""
    try:
        from . import providers
        return providers.resolve(cfg.llm_provider, cfg.llm_model, cfg)
    except Exception:  # noqa: BLE001
        return cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model


def _clip_block(m: Moment) -> str:
    f = m.features
    ev = []
    if f.get("laughter_max", 0) > 0.3:
        ev.append(f"laughter strength {f['laughter_max']:.2f}")
    if f.get("energy_shock_max", 0) > 0.3:
        ev.append(f"energy spike {f['energy_shock_max']:.2f}")
    if f.get("pitch_surge_max", 0) > 0.3:
        ev.append(f"pitch surge {f['pitch_surge_max']:.2f}")
    if f.get("overlap_max", 0) > 0.3:
        ev.append(f"voice overlap {f['overlap_max']:.2f}")
    if m.graph_reasons:
        ev.append("; ".join(m.graph_reasons))
    text = (m.text or "")[:600].replace("\n", " ")
    return (f"- id={m.id} [{m.start:.1f}-{m.end:.1f}s] "
            f"evidence: {', '.join(ev) if ev else 'none'}\n  text: {text}")


def llm_judge(ranked: list[Ranked], persona: str = "general",
              cfg: Config | None = None) -> list[Ranked]:
    """Re-rank a shortlist with an OpenAI-compatible chat endpoint.

    Best-effort: any failure returns the input untouched. With no provider
    configured this is a no-op, which is why the deterministic path is the
    default rather than a fallback.
    """
    cfg = cfg or get_config()
    base, api_key, model = _endpoint(cfg)
    if not base or not model:
        log.info("no LLM reachable, keeping heuristic order")
        return ranked
    cfg = replace(cfg, llm_base_url=base, llm_api_key=api_key, llm_model=model)

    shortlist = ranked[: cfg.llm_top_k]
    prompt = JUDGE_PROMPT.format(
        persona=persona,
        evidence="(per-clip evidence is listed inline below)",
        clips="\n".join(_clip_block(r.moment) for r in shortlist),
    )

    payload = {
        "model": cfg.llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    try:
        raw = _chat(cfg, payload)
        parsed = _parse_scores(raw)
        if not parsed:
            # Some local servers advertise json_object but ignore it, or reject
            # it outright. Retry once without the hint rather than giving up.
            log.info("no scores parsed with response_format; retrying without it")
            raw = _chat(cfg, {k: v for k, v in payload.items()
                              if k != "response_format"})
            parsed = _parse_scores(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM judge failed (%s), keeping heuristic order", exc)
        return ranked

    if not parsed:
        # Never fail silently again: an empty parse used to look identical to
        # "no model configured".
        log.warning("LLM judge (%s) returned no usable scores; sample: %r",
                    cfg.llm_model, (raw or "")[:200])
        return ranked

    weights: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for item in parsed:
        mid = str(item.get("id", ""))
        try:
            weights[mid] = _clamp(float(item.get("score", 0)) / 10.0)
        except (TypeError, ValueError):
            continue
        if item.get("reason"):
            reasons[mid] = str(item["reason"])[:120]

    if not weights:
        return ranked

    out: list[Ranked] = []
    for r in ranked:
        if r.moment.id in weights:
            llm = weights[r.moment.id]
            # 55% heuristic, 45% LLM -- the LLM refines, it does not overrule.
            r.final_score = _clamp(0.55 * r.base_score + 0.45 * llm)
            r.reason = reasons.get(r.moment.id) or r.reason
            r.subscores["llm"] = llm
        out.append(r)

    out.sort(key=lambda r: -r.final_score)
    return out


def _chat(cfg: Config, payload: dict) -> str:
    url = cfg.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.llm_api_key:
        headers["Authorization"] = f"Bearer {cfg.llm_api_key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _parse_scores(raw: str) -> list[dict]:
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", raw, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        for key in ("clips", "scores", "results", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def rank(moments: list[Moment], persona: str = "general",
         cfg: Config | None = None, use_llm: bool = True,
         prompt_plan=None) -> list[Ranked]:
    cfg = cfg or get_config()
    if persona not in PERSONAS:
        persona = "general"
    ranked = heuristic_rank(moments, cfg)
    if not moments:
        return []
    if use_llm:
        # A free-text prompt is more specific than any fixed persona, so the
        # judge gets the user's actual words rather than a rubric label.
        target = getattr(prompt_plan, "raw", "") or persona
        ranked = llm_judge(ranked, target, cfg)
    else:
        for r in ranked:
            r.reason = _explain(r.subscores)
    return ranked


def select(ranked: list[Ranked], k: int | None = None,
           cfg: Config | None = None) -> list[Ranked]:
    cfg = cfg or get_config()
    return _select_diverse(ranked, k or cfg.max_clips)