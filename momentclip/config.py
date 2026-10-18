"""Runtime configuration. Everything is overridable via env vars."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Config:
    root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("CLIPPER_DATA", Path(__file__).resolve().parent.parent / "data"))
    )

    # --- binaries -------------------------------------------------------
    ffmpeg: str = field(default_factory=lambda: os.getenv("FFMPEG_BIN", "ffmpeg"))
    ffprobe: str = field(default_factory=lambda: os.getenv("FFPROBE_BIN", "ffprobe"))

    # --- analysis -------------------------------------------------------
    frame_hz: float = field(default_factory=lambda: _env_float("CLIPPER_FRAME_HZ", 4.0))
    analysis_sr: int = field(default_factory=lambda: _env_int("CLIPPER_ANALYSIS_SR", 16000))
    whisper_model: str = field(default_factory=lambda: os.getenv("CLIPPER_WHISPER_MODEL", "small"))
    whisper_device: str = field(default_factory=lambda: os.getenv("CLIPPER_WHISPER_DEVICE", "auto"))
    whisper_compute: str = field(
        default_factory=lambda: os.getenv("CLIPPER_WHISPER_COMPUTE", "int8")
    )

    # --- clip shape -----------------------------------------------------
    min_clip: float = field(default_factory=lambda: _env_float("CLIPPER_MIN_CLIP", 15.0))
    max_clip: float = field(default_factory=lambda: _env_float("CLIPPER_MAX_CLIP", 90.0))
    max_clips: int = field(default_factory=lambda: _env_int("CLIPPER_MAX_CLIPS", 10))
    pad_before: float = field(default_factory=lambda: _env_float("CLIPPER_PAD_BEFORE", 1.2))
    pad_after: float = field(default_factory=lambda: _env_float("CLIPPER_PAD_AFTER", 1.8))

    # --- optional LLM judge (OpenAI-compatible) -------------------------
    llm_base_url: str | None = field(default_factory=lambda: os.getenv("CLIPPER_LLM_BASE_URL"))
    llm_api_key: str | None = field(default_factory=lambda: os.getenv("CLIPPER_LLM_API_KEY"))
    llm_model: str | None = field(default_factory=lambda: os.getenv("CLIPPER_LLM_MODEL"))
    llm_top_k: int = field(default_factory=lambda: _env_int("CLIPPER_LLM_TOP_K", 24))

    # --- behaviour ------------------------------------------------------
    use_graphify: bool = field(default_factory=lambda: _env_bool("CLIPPER_USE_GRAPHIFY", True))
    reframe: bool = field(default_factory=lambda: _env_bool("CLIPPER_REFRAME", True))
    burn_captions: bool = field(default_factory=lambda: _env_bool("CLIPPER_CAPTIONS", True))
    keep_source: bool = field(default_factory=lambda: _env_bool("CLIPPER_KEEP_SOURCE", True))
    max_workers: int = field(default_factory=lambda: _env_int("CLIPPER_MAX_WORKERS", 2))

    host: str = field(default_factory=lambda: os.getenv("CLIPPER_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("CLIPPER_PORT", 8000))

    # --- derived --------------------------------------------------------
    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    @property
    def has_ffmpeg(self) -> bool:
        return shutil.which(self.ffmpeg) is not None and shutil.which(self.ffprobe) is not None

    @property
    def frame_dt(self) -> float:
        return 1.0 / self.frame_hz

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.jobs_dir, self.sources_dir):
            d.mkdir(parents=True, exist_ok=True)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
        _config.ensure_dirs()
    return _config