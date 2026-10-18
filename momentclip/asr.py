"""Transcription with word-level timestamps.

Primary path: faster-whisper (local, free, no key).
Fallback:    any OpenAI-compatible speech endpoint if one is configured.

If neither is available the pipeline still runs -- it degrades to a
signal-only analysis (no transcript) rather than failing the whole job. That is
deliberate: a clip that has no captions still beats an error page.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .config import Config, get_config

log = logging.getLogger("momentclip.asr")


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0


@dataclass
class Utterance:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "words": [asdict(w) for w in self.words],
        }


@dataclass
class Transcript:
    utterances: list[Utterance] = field(default_factory=list)
    language: str = ""
    backend: str = "none"
    duration: float = 0.0

    @property
    def available(self) -> bool:
        return bool(self.utterances)

    @property
    def text(self) -> str:
        return " ".join(u.text for u in self.utterances)

    def words(self) -> list[Word]:
        return [w for u in self.utterances for w in u.words]

    def in_range(self, start: float, end: float) -> list[Utterance]:
        return [u for u in self.utterances if u.start < end and u.end > start]

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "backend": self.backend,
            "duration": round(self.duration, 3),
            "utterances": [u.to_dict() for u in self.utterances],
        }

    def slice_text(self, start: float, end: float) -> str:
        """Words-only text inside a range. More precise than utterance slicing."""
        out = [w.text for w in self.words() if w.start < end and w.end > start]
        return " ".join(out).strip()

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Transcript":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        utts = []
        for u in data.get("utterances", []):
            words = [Word(**w) for w in u.get("words", [])]
            utts.append(Utterance(u["start"], u["end"], u.get("text", ""), words))
        return cls(utts, data.get("language", ""), data.get("backend", "none"),
                   float(data.get("duration", 0.0)))


# --------------------------------------------------------------------------
# sentence splitting on punctuation -- used for cut boundaries
# --------------------------------------------------------------------------
_SENT_END = re.compile(r"[.!?…]+[\"')\]]*\s*$")


def ends_sentence(text: str) -> bool:
    return bool(_SENT_END.search(text.strip()))


def starts_sentence(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    return t[0].isupper() or t[0].isdigit()


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------
def _device(cfg: Config) -> str:
    if cfg.whisper_device != "auto":
        return cfg.whisper_device
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _transcribe_whisper(audio: Path, cfg: Config) -> Transcript:
    from faster_whisper import WhisperModel  # type: ignore

    log.info("loading faster-whisper %s on %s", cfg.whisper_model, _device(cfg))
    model = WhisperModel(
        cfg.whisper_model,
        device=_device(cfg),
        compute_type=cfg.whisper_compute,
    )
    segments, info = model.transcribe(
        str(audio),
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
        beam_size=5,
    )

    utts: list[Utterance] = []
    for seg in segments:
        words = [
            Word(float(w.start), float(w.end), (w.word or "").strip(),
                 float(getattr(w, "probability", 1.0) or 1.0))
            for w in (seg.words or [])
            if w.start is not None and w.end is not None and (w.word or "").strip()
        ]
        utts.append(Utterance(float(seg.start), float(seg.end),
                              (seg.text or "").strip(), words))

    return Transcript(utts, getattr(info, "language", "") or "", "faster-whisper",
                      float(getattr(info, "duration", 0.0) or 0.0))


def _transcribe_http(audio: Path, cfg: Config) -> Transcript:
    """OpenAI-compatible /audio/transcriptions endpoint (Whisper API, Groq, etc.).

    Note: most such endpoints do NOT return word-level timestamps. We fall back to
    proportional timing across the utterance, which is good enough for cuts but
    not for karaoke captions.
    """
    if not cfg.llm_base_url:
        raise RuntimeError("no ASR endpoint configured")

    boundary = "----clipperboundary"
    body = bytearray()
    audio_bytes = audio.read_bytes()
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
    body += b"Content-Type: audio/wav\r\n\r\n"
    body += audio_bytes + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="model"\r\n\r\n'
    body += (cfg.whisper_model + "\r\n").encode()
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="response_format"\r\n\r\njson\r\n'
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        cfg.llm_base_url.rstrip("/") + "/audio/transcriptions",
        data=bytes(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            **({"Authorization": f"Bearer {cfg.llm_api_key}"} if cfg.llm_api_key else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=1800) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    dur = float(data.get("duration") or 0.0)
    utts: list[Utterance] = []
    for seg in data.get("segments", []) or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        utts.append(Utterance(float(seg["start"]), float(seg["end"]), text, []))
    if not utts and data.get("text"):
        utts.append(Utterance(0.0, dur, data["text"].strip(), []))

    return Transcript(utts, data.get("language", ""), "http-whisper", dur)


def transcribe(audio: str | Path, cfg: Config | None = None) -> Transcript:
    """Transcribe, degrading gracefully. Never raises."""
    cfg = cfg or get_config()
    audio = Path(audio)
    if not audio.exists():
        log.warning("audio missing: %s", audio)
        return Transcript()

    errors = []
    try:
        t = _transcribe_whisper(audio, cfg)
        if t.available:
            return t
        errors.append("faster-whisper produced no speech")
    except ImportError:
        errors.append("faster-whisper not installed")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"faster-whisper: {exc}")
        log.warning("faster-whisper failed: %s", exc)

    try:
        t = _transcribe_http(audio, cfg)
        if t.available:
            return t
        errors.append("http ASR produced no speech")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"http ASR: {exc}")

    log.warning("no transcript available (%s); continuing signal-only", "; ".join(errors))
    return Transcript()


def capability() -> dict:
    """What transcription backends this install can actually use."""
    cfg = get_config()
    caps = {"faster_whisper": False, "http": False, "llm": cfg.has_llm}
    try:
        import faster_whisper  # noqa: F401
        caps["faster_whisper"] = True
    except ImportError:
        pass
    caps["http"] = bool(cfg.llm_base_url)
    return caps