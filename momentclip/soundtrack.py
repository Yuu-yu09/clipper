"""Automatic background soundtrack and comedy stingers.

Two sources, both licence-clean by construction:

  1. Procedurally synthesised beds. Generated from oscillators with numpy, so
     there is no audio asset in this repo and nothing to license. Tunable by
     "mood" rather than by song.
  2. Your own files in `assets/music/`. Any audio format ffmpeg reads.

Plus comedy stingers -- the short brass/percussion hits dropped at the peak of a
moment, which is the single most recognisable piece of the auto-editing look.

Ducking is automatic: the music bed is sidechain-compressed against the speech,
so it sits under the voice and swells in the gaps. No manual mixing.
"""

from __future__ import annotations

import logging
import math
import re
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import Config, get_config

log = logging.getLogger("momentclip.soundtrack")

SR = 44100

# Semitone offsets from the root, for chords and bass.
_MAJOR = [0, 4, 7, 12, 16, 19, 24]
_MINOR = [0, 3, 7, 10, 14, 15, 19]


@dataclass
class Mood:
    name: str
    bpm: float
    root: int                 # midi-ish root frequency Hz base
    scale: list               # semitone offsets
    bass_pattern: str         # "four" | "walk" | "sustained"
    drums: bool
    brightness: float         # 0 dark .. 1 bright
    swing: float = 0.0


MOODS: dict[str, Mood] = {
    "upbeat":      Mood("upbeat", 118, 261.63, _MAJOR, "four", True, 0.80),
    "funny":       Mood("funny", 132, 293.66, _MAJOR, "walk", True, 0.90, 0.12),
    "chill":       Mood("chill", 84, 220.00, _MINOR, "sustained", True, 0.45),
    "documentary": Mood("documentary", 96, 196.00, _MINOR, "walk", True, 0.55),
    "tense":       Mood("tense", 104, 174.61, _MINOR, "sustained", True, 0.30),
    "none":        Mood("none", 0, 0.0, [], "sustained", False, 0.0),
}


def _adsr(n: int, a: float, d: float, s: float, r: float) -> np.ndarray:
    env = np.zeros(n)
    ai, di = int(a * n), int(d * n)
    ri = int(r * n)
    si = max(0, n - ai - di - ri)
    idx = 0
    if ai:
        env[idx:idx + ai] = np.linspace(0, 1, ai)
        idx += ai
    if di:
        env[idx:idx + di] = np.linspace(1, s, di)
        idx += di
    if si:
        env[idx:idx + si] = s
        idx += si
    if ri and idx < n:
        env[idx:n] = np.linspace(s, 0, n - idx)
    return env


def _osc(freq: float, n: int, kind: str = "saw", brightness: float = 0.6) -> np.ndarray:
    t = np.arange(n) / SR
    phase = 2 * np.pi * freq * t
    if kind == "sine":
        return np.sin(phase)
    if kind == "square":
        return np.sign(np.sin(phase)) * 0.6
    if kind == "tri":
        return 2 / np.pi * np.arcsin(np.sin(phase))
    # saw: additive, band-limited by truncating the harmonic count
    out = np.zeros(n)
    for h in range(1, 13):
        amp = (1.0 / h) * (brightness ** (h - 1) * 0.35 + 0.15)
        if amp < 0.005:
            break
        out += amp * np.sin(phase * h)
    return out / 2.5


def _kick(n: int) -> np.ndarray:
    t = np.arange(n) / SR
    f = 110 * np.exp(-t * 28) + 45
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * _adsr(n, 0.002, 0.25, 0.0, 0.2)


def _snare(n: int) -> np.ndarray:
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n)
    t = np.arange(n) / SR
    body = np.sin(2 * np.pi * 190 * t) * 0.35
    return (noise * 0.8 + body) * _adsr(n, 0.001, 0.12, 0.0, 0.1)


def _hat(n: int, open_: bool = False) -> np.ndarray:
    rng = np.random.default_rng(11)
    noise = rng.standard_normal(n)
    dur = 0.18 if open_ else 0.05
    return noise * _adsr(n, 0.001, dur * 0.3, 0.0, dur)


def _chord(freqs: list[float], n: int, brightness: float) -> np.ndarray:
    out = np.zeros(n)
    for f in freqs:
        out += _osc(f, n, "saw", brightness)
    return out / max(1, len(freqs))


def _stinger(kind: str, n: int) -> np.ndarray:
    """A short comic hit."""
    t = np.arange(n) / SR
    if kind == "brass":
        # rising fifth stab
        f = 220 * np.exp(t * 2.2)
        sig = np.zeros(n)
        for k, amp in ((1, 1.0), (1.5, 0.7), (2, 0.5), (3, 0.3)):
            sig += amp * np.sin(2 * np.pi * f * k * t)
        return sig / 3 * _adsr(n, 0.01, 0.25, 0.2, 0.5)
    if kind == "slide":
        f = 600 * np.exp(-t * 3.5) + 180
        return np.sign(np.sin(2 * np.pi * np.cumsum(f) / SR)) * 0.5 * _adsr(n, 0.005, 0.2, 0.1, 0.3)
    if kind == "drumroll":
        out = np.zeros(n)
        step = int(SR / 26)
        for i in range(0, n, step):
            k = min(step, n - i)
            out[i:i + k] += _snare(k) * (0.25 + 0.75 * (i / n))
        return out * 0.6
    if kind == "pop":
        return np.sin(2 * np.pi * 880 * np.exp(-t * 6) * t) * _adsr(n, 0.002, 0.1, 0.0, 0.15)
    # "womp" -- the classic bass drop
    f = 90 * np.exp(-t * 1.5) + 38
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * _adsr(n, 0.005, 0.4, 0.1, 0.4)


def generate_bed(mood_name: str, seconds: float, out: Path, sr: int = SR) -> Path | None:
    """Synthesise a seamless loop of `seconds` for the given mood."""
    mood = MOODS.get(mood_name, MOODS["upbeat"])
    if mood.name == "none" or seconds <= 0.2:
        return None

    beat = 60.0 / mood.bpm
    bar = beat * 4
    n = int(seconds * sr)
    track = np.zeros(n + int(bar * sr) * 2, dtype=np.float64)  # tail for looping

    # I - V - vi - IV, the universal short-form progression
    progression = [[0, 4, 7], [7, 11, 14], [9, 12, 16], [5, 9, 12]]
    progression = [[s - 12 if s > 12 else s for s in ch] for ch in progression]

    bars = int(np.ceil(seconds / bar)) + 1
    for b in range(bars):
        # bar is a float tempo, so this must be cast before it becomes a slice
        t0 = int(b * bar * sr)
        if t0 >= track.size:
            break
        chord = progression[b % 4]
        chord_len = int(bar * sr)

        # pad
        freqs = [mood.root * (2 ** (s / 12)) for s in chord]
        pad = _chord(freqs, chord_len, mood.brightness) * _adsr(
            chord_len, 0.08, 0.25, 0.55, 0.35) * 0.32
        end = min(track.size, t0 + chord_len)
        track[t0:end] += pad[: end - t0]

        # bass
        if mood.bass_pattern == "four":
            for k in range(4):
                s0 = t0 + int(k * beat * sr)
                if s0 >= track.size:
                    break
                bl = int(beat * 0.9 * sr)
                e = min(track.size, s0 + bl)
                bf = mood.root * (2 ** (chord[0] / 12)) * 0.5
                bass = _osc(bf, e - s0, "sine") * _adsr(e - s0, 0.01, 0.3, 0.3, 0.3) * 0.45
                track[s0:e] += bass
        elif mood.bass_pattern == "walk":
            for k in range(4):
                s0 = t0 + int(k * beat * sr)
                if s0 >= track.size:
                    break
                bl = int(beat * 0.8 * sr)
                e = min(track.size, s0 + bl)
                bf = mood.root * (2 ** (chord[k % len(chord)] / 12)) * 0.5
                bass = _osc(bf, e - s0, "tri") * _adsr(e - s0, 0.01, 0.35, 0.25, 0.3) * 0.40
                track[s0:e] += bass
        else:
            bf = mood.root * (2 ** (chord[0] / 12)) * 0.5
            e = min(track.size, t0 + chord_len)
            track[t0:e] += _osc(bf, e - t0, "sine") * \
                _adsr(e - t0, 0.2, 0.3, 0.6, 0.4) * 0.30

        # drums
        if mood.drums:
            for k in range(4):
                pos = t0 + int(k * beat * sr)
                if mood.swing and k % 2 == 1:
                    pos += int(mood.swing * beat * sr * 0.15)
                if pos >= track.size:
                    continue
                if k in (0, 2):
                    ln = int(0.22 * sr)
                    e = min(track.size, pos + ln)
                    track[pos:e] += _kick(e - pos) * 0.55
                if k in (1, 3):
                    ln = int(0.18 * sr)
                    e = min(track.size, pos + ln)
                    track[pos:e] += _snare(e - pos) * 0.30
                hn = int(0.06 * sr)
                e = min(track.size, pos + hn)
                track[pos:e] += _hat(e - pos, open_=k == 3) * 0.10

    peak = float(np.max(np.abs(track))) or 1.0
    track = track / peak * 0.85
    track = track[:n]

    # light stereo widening
    delay = int(0.012 * sr)
    left = track
    right = np.concatenate([np.zeros(delay), track[:-delay]]) * 0.85
    stereo = np.stack([left, right], axis=1).reshape(-1)

    write_wav(stereo.astype(np.float32), out, sr)
    log.info("synthesised %s bed (%.1fs, %d bpm)", mood.name, seconds, mood.bpm)
    return out


def generate_stinger(kind: str, out: Path, seconds: float = 1.2, sr: int = SR) -> Path | None:
    n = int(seconds * sr)
    sig = _stinger(kind, n)
    peak = float(np.max(np.abs(sig))) or 1.0
    write_wav((sig / peak * 0.9).astype(np.float32), out, sr)
    return out


def write_wav(samples: np.ndarray, path: Path, sr: int = SR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1 if pcm.ndim == 1 else 2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


# --------------------------------------------------------------------------
# user-supplied music
# --------------------------------------------------------------------------
def music_dir(cfg: Config | None = None) -> Path:
    cfg = cfg or get_config()
    return cfg.root / "assets" / "music"


def user_tracks(cfg: Config | None = None) -> list[Path]:
    d = music_dir(cfg)
    if not d.exists():
        return []
    exts = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts)


# --------------------------------------------------------------------------
# mixing
# --------------------------------------------------------------------------
@dataclass
class MixSpec:
    """What the renderer should mix under the speech."""
    music: Path | None = None
    music_gain: float = 0.16          # 1.0 == as loud as the source
    duck: bool = True
    # stingers are (offset_seconds, kind); stinger_paths holds the rendered
    # wav for each, in the same order
    stingers: list[tuple[float, str]] = field(default_factory=list)
    stinger_paths: list[Path] = field(default_factory=list)
    stinger_gain: float = 0.55

    def __post_init__(self) -> None:
        # keep paths and descriptions aligned so render never zips past the end
        if self.stinger_paths and len(self.stinger_paths) != len(self.stingers):
            n = min(len(self.stinger_paths), len(self.stingers))
            self.stinger_paths = self.stinger_paths[:n]
            self.stingers = self.stingers[:n]


def build_music(
    duration: float,
    mood: str = "upbeat",
    cfg: Config | None = None,
    *,
    prefer_user: bool = False,
    workdir: Path | None = None,
) -> Path | None:
    """Resolve the music bed for a clip of `duration` seconds."""
    cfg = cfg or get_config()
    if mood == "none" or duration <= 0.5:
        return None

    if prefer_user:
        tracks = user_tracks(cfg)
        if tracks:
            # deterministic pick so the same clip always gets the same bed
            pick = tracks[int(hashlib8(str(duration))) % len(tracks)]
            log.info("using user music: %s", pick.name)
            return pick

    workdir = workdir or (cfg.jobs_dir / "_music")
    dest = workdir / f"bed_{mood}_{int(duration)}s.wav"
    if dest.exists() and dest.stat().st_size > 1024:
        return dest
    try:
        return generate_bed(mood, duration, dest)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not synthesise music (%s); continuing without it", exc)
        return None


def hashlib8(s: str) -> int:
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


# --------------------------------------------------------------------------
# per-clip adaptation
# --------------------------------------------------------------------------
# Equal temperament, A4 = 440. Used to put the music in the same key as the
# speaker's own pitch, which is the difference between a bed that sits under
# a voice and one that fights it.
_A4 = 440.0
_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def root_from_pitch(f0_hz: float | None) -> float:
    """Snap a detected fundamental to the nearest equal-tempered root."""
    if not f0_hz or f0_hz <= 40 or f0_hz > 500:
        return 261.63                      # C4, the default
    semis = 12 * math.log2(f0_hz / _A4)
    midi = int(round(69 + semis))
    midi = max(36, min(84, midi))
    return _A4 * (2 ** ((midi - 69) / 12.0))


def mood_from_clip(features: dict, events: list | None = None,
                   persona: str = "general") -> tuple[str, str]:
    """Pick a music mood from the clip's own measured character.

    Without this every clip gets the same bed, which is the "music that
    doesn't match" problem: a calm explanation and a heated argument end up
    scored identically.

    Returns (mood, reason).
    """
    f = features or {}
    ev = events or []
    kinds = [getattr(e, "kind", "") for e in ev]

    n_laugh = kinds.count("laughter")
    n_shock = kinds.count("energy_shock")
    n_surge = kinds.count("pitch_surge")
    n_overlap = kinds.count("overlap")
    n_silence = kinds.count("silence_beat")

    spectacle = float(f.get("spectacle", 0.0) or 0.0)
    swing = float(f.get("loudness_swing", 0.0) or 0.0)
    surprise = float(f.get("graph_surprise", 0.0) or 0.0)
    centrality = float(f.get("graph_centrality", 0.0) or 0.0)

    if n_laugh >= 2 or (n_laugh >= 1 and spectacle > 0.4):
        return "funny", f"{n_laugh} laughter burst(s) — bouncy bed"
    if n_overlap >= 2:
        return "tense", f"{n_overlap} overlapping-voice moments — driving bed"
    if n_shock >= 3 or (n_shock >= 1 and spectacle > 0.6):
        return "upbeat", "high energy and sudden reactions"
    if n_surge >= 2:
        return "upbeat", "repeated pitch surges"
    if surprise > 0.5:
        return "tense", "surprising / bridging moments"
    if swing < 6.0 and n_silence >= 1 and centrality < 0.5:
        return "chill", "flat delivery with pauses — calm bed"
    if persona == "candid":
        return "chill", "understated clips suit a soft bed"
    if n_shock or spectacle > 0.3:
        return "upbeat", "some energy in this clip"
    return "chill", "even delivery, nothing spiking — calm bed"


def build_music_adaptive(
    duration: float,
    features: dict | None = None,
    events: list | None = None,
    f0_hz: float | None = None,
    persona: str = "general",
    cfg: Config | None = None,
    workdir: Path | None = None,
) -> tuple[Path | None, str, str, str]:
    """Build a bed matched to this specific clip.

    Returns (path, mood, root_name, reason).
    """
    cfg = cfg or get_config()
    mood, reason = mood_from_clip(features or {}, events or [], persona)
    root = root_from_pitch(f0_hz)
    root_name = _NOTE_NAMES[int(round(12 * math.log2(root / _A4))) % 12]
    # note names like "C#" are fine in a filename, but guard the separator
    root_name = re.sub(r"[^A-Za-z0-9]", "", root_name) or "C"

    workdir = workdir or (cfg.jobs_dir / "_music")
    dest = workdir / f"bed_{mood}_{root_name}_{int(round(root))}_{int(duration)}s.wav"
    if dest.exists() and dest.stat().st_size > 1024:
        return dest, mood, root_name, reason

    mood_spec = MOODS.get(mood, MOODS["upbeat"])
    tuned = Mood(mood_spec.name, mood_spec.bpm, root, mood_spec.scale,
                 mood_spec.bass_pattern, mood_spec.drums,
                 mood_spec.brightness, mood_spec.swing)
    saved = MOODS.get(mood)
    MOODS[mood] = tuned
    try:
        path = generate_bed(mood, duration, dest)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not synthesise music (%s); continuing without it", exc)
        path = None
    finally:
        if saved is not None:
            MOODS[mood] = saved
    return path, mood, root_name, reason


def pick_stingers(events, duration: float, limit: int = 2) -> list[tuple[float, str]]:
    """Place at most `limit` stingers at the strongest moments in a clip."""
    if not events:
        return []
    kinds = ["brass", "drumroll", "pop", "slide", "womp"]
    strong = sorted(events, key=lambda e: -e.strength)
    placed: list[tuple[float, str]] = []
    for ev in strong:
        if len(placed) >= limit:
            break
        if ev.kind not in {"laughter", "energy_shock", "pitch_surge"}:
            continue
        # land slightly after the hit, and never in the first 2 s
        t = max(2.0, ev.start + 0.15)
        if t >= duration - 1.5:
            continue
        if any(abs(t - p) < 3.0 for p, _ in placed):
            continue
        placed.append((round(t, 2), kinds[len(placed) % len(kinds)]))
    return sorted(placed)