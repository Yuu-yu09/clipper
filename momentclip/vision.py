"""Optional vision pass -- lets a prompt match things that are only SEEN.

The prompt compiler in momentclip/prompt.py is honest that it cannot do visual
matching: "find the woman in the yellow dress" needs a model that looks at
frames, not a word list. This module adds that, on a free tier.

How it works:

  1. Sample frames across the source at a fixed rate, plus dense sampling
     around every detected signal event -- reactions are where the visually
     interesting frame usually is.
  2. Ask a vision model, once per batch, what is happening and what stands out.
  3. Store the answers as `visual` nodes on the moment graph, and match
     prompt keywords against them.

Cost is kept down by batching frames into a single request and by only running
when a provider that supports images is actually configured.
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg, providers
from .config import Config, get_config

log = logging.getLogger("momentclip.vision")

_WORD = __import__("re").compile(r"[A-Za-z']+")

SYSTEM = """You describe frames from one video so an editor can find moments later.

For each numbered frame, reply on one line as:
N | visible: <subjects, clothing, colours, setting, objects> | action: <what is happening> | notable: <anything unusual, funny, emotional, or a reaction>

Be concrete and literal. Mention colours and clothing. If a frame shows nothing
notable, write "notable: -". Do not invent anything you cannot see."""


@dataclass
class VisualNote:
    t: float
    description: str = ""
    frame: Path | None = None
    deleted: bool = False


@dataclass
class VisionResult:
    notes: list[VisualNote] = field(default_factory=list)
    provider: str = ""
    ok: bool = False
    note: str = ""

    def text_windows(self) -> list[tuple[float, float, str]]:
        """(start, end, text) for matching a prompt against what was seen."""
        out = []
        for n in self.notes:
            if not n.description:
                continue
            out.append((max(0.0, n.t - 4.0), n.t + 4.0, n.description))
        return out


def vision_available(cfg: Config | None = None) -> bool:
    cfg = cfg or get_config()
    base, key, model = providers.resolve(cfg.llm_provider, cfg.llm_model, cfg)
    if not base or not model:
        return False
    for p in providers.PROVIDERS:
        if base.startswith(p.base_url.split("/v1")[0][:40]) and not p.vision:
            return False
    return True


# --------------------------------------------------------------------------
# frame sampling
# --------------------------------------------------------------------------
def sample_frames(
    src: str | Path,
    start: float,
    end: float,
    times: list[float],
    outdir: Path,
    width: int = 512,
) -> list[VisualNote]:
    """Extract single frames at the requested timestamps."""
    notes: list[VisualNote] = []
    outdir.mkdir(parents=True, exist_ok=True)
    for t in times:
        if t < start or t > end:
            continue
        dest = outdir / f"f_{int(t * 10):07d}.jpg"
        if not dest.exists():
            try:
                ffmpeg.run([
                    get_config().ffmpeg, "-y", "-ss", f"{t:.3f}", "-i", str(src),
                    "-frames:v", "1", "-vf", f"scale={width}:-2",
                    "-q:v", "4", str(dest),
                ], timeout=120)
            except Exception as exc:  # noqa: BLE001
                log.debug("frame grab failed at %.2fs: %s", t, exc)
                continue
        if dest.exists():
            notes.append(VisualNote(t=t, frame=dest))
    return notes


def sampling_times(duration: float, events: list | None = None,
                   base: int = 8, around_events: int = 2) -> list[float]:
    """Even coverage plus denser sampling where the audio says something moved."""
    times: list[float] = []
    if duration <= 0:
        return times
    for i in range(base):
        times.append((i + 0.5) * duration / base)
    for e in (events or []):
        start = getattr(e, "start", None)
        if start is None:
            continue
        for k in range(around_events):
            times.append(start + (k + 0.5) * 0.4)
    # dedupe, keep in range, cap the total so a long video stays affordable
    seen = sorted({round(min(max(0.0, t), duration), 2) for t in times if 0 <= t <= duration})
    return seen[:48]


# --------------------------------------------------------------------------
# model call
# --------------------------------------------------------------------------
def _data_url(p: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()


def _chat_with_images(base_url: str, api_key: str | None, model: str,
                      notes: list[VisualNote]) -> str:
    content: list[dict] = [{"type": "text", "text": SYSTEM}]
    for i, n in enumerate(notes, 1):
        if not n.frame or not n.frame.exists():
            continue
        content.append({"type": "text", "text": f"Frame {i} @ {n.t:.1f}s:"})
        content.append({"type": "image_url", "image_url": {"url": _data_url(n.frame)}})

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": content},
        ],
        "temperature": 0.1,
        "max_tokens": 1400,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"] or ""


_LINE = __import__("re").compile(r"^\s*(\d+)\s*\|(.*)$", __import__("re").S)


def _parse(text: str, notes: list[VisualNote]) -> None:
    """Pull each frame's description back onto its timestamp."""
    for raw in text.splitlines():
        m = _LINE.match(raw.strip())
        if not m:
            continue
        idx = int(m.group(1))
        body = " ".join(m.group(2).split())
        # normalise the key: value pairs into something searchable
        bits = []
        for part in body.split("|"):
            if ":" in part:
                k, _, v = part.partition(":")
                bits.append(f"{k.strip()}: {v.strip()}")
            else:
                bits.append(part)
        desc = " | ".join(b for b in bits if b)
        if 1 <= idx <= len(notes):
            notes[idx - 1].description = desc


# --------------------------------------------------------------------------
def describe(
    src: str | Path,
    start: float,
    end: float,
    events: list | None = None,
    cfg: Config | None = None,
    workdir: Path | None = None,
) -> VisionResult:
    """Sample frames and ask a vision model what is in them.

    Never raises: any failure returns a VisionResult with ok=False and a reason,
    so a missing or rate-limited model degrades to the existing pipeline.
    """
    cfg = cfg or get_config()
    base, key, model = providers.resolve(cfg.llm_provider, cfg.llm_model, cfg)
    if not base or not model:
        return VisionResult(ok=False, note="no model configured")

    times = sampling_times(end - start, events)
    tmpdir = Path(workdir or tempfile.mkdtemp(prefix="clipper-vision-"))
    notes = sample_frames(src, start, end, times, tmpdir)
    if not notes:
        return VisionResult(ok=False, note="no frames could be sampled")

    try:
        raw = _chat_with_images(base, key, model, notes)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return VisionResult(ok=False, notes=notes, provider=model,
                            note=f"vision API HTTP {exc.code}: {detail}")
    except Exception as exc:  # noqa: BLE001
        return VisionResult(ok=False, notes=notes, provider=model,
                            note=f"vision API failed: {type(exc).__name__}: {exc}")

    _parse(raw, notes)
    described = [n for n in notes if n.description]
    log.info("vision: %d/%d frames described by %s", len(described), len(notes), model)
    shutil.rmtree(tmpdir, ignore_errors=True)
    return VisionResult(notes=notes, provider=model, ok=bool(described),
                        note="" if described else "model returned no descriptions")


def keywords_match(plan, vision: VisionResult) -> dict[str, float]:
    """Score how well each visual note matches the prompt's topic keywords."""
    from .prompt import _STOPWORDS
    if not plan.keywords or not vision or not vision.ok:
        return {}
    out: dict[str, float] = {}
    for n in vision.notes:
        if not n.description:
            continue
        toks = {w.lower() for w in _WORD.findall(n.description)}
        toks -= _STOPWORDS
        hits = sum(1 for k in plan.keywords if k in toks)
        if hits:
            out[f"{n.t:.2f}"] = hits / len(plan.keywords)
    return out