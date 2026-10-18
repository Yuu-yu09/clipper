"""LLM provider presets, including the ones that are genuinely free.

Everything here speaks the OpenAI chat-completions protocol, so a single code
path covers local models and hosted APIs alike. Only the base URL and the
model name change.

On "free":

* **Ollama (local)** is the only option that is free, unlimited, private and
  needs no account. It is the default.
* **Groq** has a standing free tier -- no credit card, rate-limited only.
* **Google AI Studio (Gemini)** has a standing free tier on the Flash models,
  and is the cheapest way to get *vision* (see momentclip/vision.py).
* **OpenRouter** free models need a key; the daily allowance is small until
  you top up once.
* **OpenAI proper is not free.** New accounts get a small expiring credit, then
  it is pay-per-token. It is listed because it works, not because it is free.

Limits change often. Treat the numbers here as orientation and check the
provider's dashboard for what is actually left today.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("momentclip.providers")

OLLAMA_DEFAULT = "http://localhost:11434/v1"


@dataclass
class Provider:
    key: str
    label: str
    base_url: str
    models: list[str]
    needs_key: bool = True
    free: str = ""              # "" = paid, otherwise what "free" means here
    vision: bool = False
    note: str = ""
    env: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "base_url": self.base_url,
            "models": self.models, "needs_key": self.needs_key,
            "free": self.free, "vision": self.vision, "note": self.note,
            "env": self.env,
        }


PROVIDERS: list[Provider] = [
    Provider(
        key="ollama", label="Ollama (local)",
        base_url=OLLAMA_DEFAULT,
        models=["qwen3:8b", "llama3.1:8b", "gemma3:12b", "qwen2.5:7b"],
        needs_key=False, free="unlimited · runs on your machine · private",
        vision=False,
        note="Default. No account, no key, no rate limit. Nothing leaves the machine.",
        env="",
    ),
    Provider(
        key="groq", label="Groq (free tier)",
        base_url="https://api.groq.com/openai/v1",
        models=["gpt-oss-120b", "gpt-oss-20b", "qwen3-32b"],
        needs_key=True, free="no card · ~30 req/min, 1000/day",
        vision=False,
        note="Fastest hosted option on the free tier.",
        env="GROQ_API_KEY",
    ),
    Provider(
        key="gemini", label="Google AI Studio (free tier)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        models=["gemini-2.0-flash", "gemini-1.5-flash"],
        needs_key=True, free="no card · rate-limited",
        vision=True,
        note="Only free option here that can read images -- enables visual prompts.",
        env="GEMINI_API_KEY",
    ),
    Provider(
        key="openrouter", label="OpenRouter (free models)",
        base_url="https://openrouter.ai/api/v1",
        models=["qwen/qwen3-235b-a22b:free", "google/gemini-2.0-flash-exp:free"],
        needs_key=True, free="no card · ~50 req/day until you top up",
        vision=True,
        note="Rotating free lineup. Small daily allowance.",
        env="OPENROUTER_API_KEY",
    ),
    Provider(
        key="openai", label="OpenAI (paid)",
        base_url="https://api.openai.com/v1",
        models=["gpt-4o-mini", "gpt-4o"],
        needs_key=True, free="",
        vision=True,
        note="NOT free -- new accounts get expiring credits, then pay per token.",
        env="OPENAI_API_KEY",
    ),
]


def get_provider(key: str) -> Provider | None:
    for p in PROVIDERS:
        if p.key == key:
            return p
    return None


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
def ollama_running(timeout: float = 1.5) -> bool:
    try:
        urllib.request.urlopen(OLLAMA_DEFAULT.rsplit("/v1", 1)[0] + "/api/tags",
                               timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def ollama_models(timeout: float = 2.0) -> list[str]:
    """Models already pulled locally. Never downloads anything."""
    try:
        url = OLLAMA_DEFAULT.rsplit("/v1", 1)[0] + "/api/tags"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return sorted({m.get("name", "") for m in data.get("models", []) if m.get("name")})
    except Exception:  # noqa: BLE001
        return []


def key_present(p: Provider) -> bool:
    import os
    if not p.needs_key:
        return True
    if not p.env:
        return False
    return bool(os.getenv(p.env) or os.getenv("CLIPPER_LLM_API_KEY"))


def status() -> list[dict]:
    """Per-provider readiness, for the UI. Never raises."""
    out = []
    for p in PROVIDERS:
        d = p.to_dict()
        d["key_present"] = key_present(p)
        if p.key == "ollama":
            running = ollama_running()
            local = ollama_models() if running else []
            d["running"] = running
            d["local_models"] = local
            d["key_present"] = running and bool(local)
            if local:
                d["models"] = local
            d["status"] = ("ready" if running and local
                           else "installed, no models" if running
                           else "not running")
        else:
            d["running"] = None
            d["local_models"] = []
            d["status"] = "ready" if d["key_present"] else f"needs {p.env}"
        out.append(d)
    return out


def resolve(key: str | None, model: str | None, cfg) -> tuple[str | None, str | None, str | None]:
    """Work out (base_url, api_key, model) from an explicit choice or the env.

    Explicit config wins; otherwise the first *ready* provider is used, so a
    machine with Ollama running needs no configuration at all.
    """
    if key:
        p = get_provider(key)
        if p:
            import os
            api = (os.getenv(p.env) if p.env else None) or os.getenv("CLIPPER_LLM_API_KEY")
            return p.base_url, api, model or (p.models[0] if p.models else None)

    if cfg.llm_base_url and cfg.llm_model:
        return cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model

    for d in status():
        if d["status"] == "ready":
            p = get_provider(d["key"])
            if p:
                import os
                api = (os.getenv(p.env) if p.env else None) or os.getenv("CLIPPER_LLM_API_KEY")
                return p.base_url, api, model or d["models"][0]
    return cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model
