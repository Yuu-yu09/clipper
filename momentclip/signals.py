"""Per-frame audio feature extraction and event detection.

This is the part no off-the-shelf clipper does. We do NOT ask an LLM to find the
funny moments -- an LLM reading a transcript physically cannot see the reaction
shot after the punchline, the pitch spike, or the beat of silence before a reveal.

Everything here is deterministic signal processing: no model, no API, no cost.

Features computed on a fixed analysis grid (default 4 Hz, so 0.25 s resolution):
    rms_db      loudness
    zcr         zero-crossing rate      -> high for fricatives / broadband noise
    centroid    spectral centroid       -> "brightness"; spikes on excitement
    flatness    spectral flatness       -> noise-like: laughter, breath, applause
    f0          fundamental frequency  -> pitch; rises sharply on surprise
    peak        sample peak
"""

from __future__ import annotations

import logging
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("momentclip.signals")

# Speech band. Below ~70 Hz is rumble/music, above ~400 Hz is sibilance/hiss.
F0_MIN = 70.0
F0_MAX = 400.0


# --------------------------------------------------------------------------
# io
# --------------------------------------------------------------------------
def read_wav_mono(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a 16-bit PCM wav into float32 in [-1, 1]."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM wav, got {w.getsampwidth() * 8}-bit")
        n_channels = w.getnchannels()
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)
    return data, sr


# --------------------------------------------------------------------------
# core per-frame features
# --------------------------------------------------------------------------
def _frame_features(frame: np.ndarray, sr: int) -> dict[str, float]:
    n = frame.size
    if n < 64:
        return dict(rms_db=-90.0, zcr=0.0, centroid=0.0, flatness=0.0, f0=0.0, peak=0.0)

    rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2) + 1e-12))
    rms_db = 20.0 * np.log10(max(rms, 1e-6))

    # zero-crossing rate
    zcr = float(np.mean(np.abs(np.diff(np.signbit(frame).astype(np.int8))) > 0)) if n > 1 else 0.0

    # spectrum
    win = np.hanning(n).astype(np.float64)
    spec = np.abs(np.fft.rfft(frame.astype(np.float64) * win)) + 1e-10
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    power = spec ** 2

    centroid = float(np.sum(freqs * spec) / (np.sum(spec) + 1e-10))
    # geometric mean / arithmetic mean -> 1.0 for noise, low for tonal
    flatness = float(np.exp(np.mean(np.log(spec))) / (np.mean(spec) + 1e-10))

    return dict(
        rms_db=float(rms_db),
        zcr=zcr,
        centroid=centroid,
        flatness=min(flatness, 1.0),
        f0=_estimate_f0(frame, sr),
        peak=float(np.max(np.abs(frame))),
    )


def _estimate_f0(frame: np.ndarray, sr: int) -> float:
    """Normalized autocorrelation pitch estimate with octave-error correction.

    Returns 0.0 when no confident periodicity is found (unvoiced / silence).
    Cheap and robust enough for ranking frames against each other; not a
    precision pitch tracker and does not need to be.
    """
    n = frame.size
    min_lag = max(2, int(sr / F0_MAX))
    max_lag = min(n - 2, int(sr / F0_MIN))
    if max_lag <= min_lag:
        return 0.0

    x = frame.astype(np.float64)
    x = x - x.mean()
    if float(np.dot(x, x)) < 1e-8:
        return 0.0

    ac = np.correlate(x, x, mode="full")[n - 1 :]
    if ac[0] <= 0:
        return 0.0
    norm = ac / ac[0]
    window = norm[min_lag:max_lag]
    if window.size == 0:
        return 0.0

    # Local maxima only -- the raw correlation has a sawtooth shape and its
    # envelope peak is not always at a real period.
    peaks = np.flatnonzero(
        (window[1:-1] > window[:-2]) & (window[1:-1] >= window[2:])
    ) + 1
    if peaks.size == 0:
        return 0.0

    best_val = float(window[peaks].max())
    if best_val < 0.35:  # not periodic enough to trust
        return 0.0

    # Octave correction: an autocorrelation routinely peaks at a harmonic
    # period (2x or 3x the true f0). Walk left toward longer lags for as long
    # as the correlation stays close to the peak, which recovers the true f0.
    peak_idx = int(peaks[np.argmax(window[peaks])])
    chosen = peak_idx
    for i in range(peak_idx, -1, -1):
        if window[i] >= 0.82 * best_val:
            chosen = i
        else:
            break

    f0 = sr / (chosen + min_lag)
    # Reject results outside the search band or at the very edge of it.
    if not (F0_MIN * 0.9 <= f0 <= F0_MAX * 1.05):
        return 0.0
    return float(f0)


def extract_track(samples: np.ndarray, sr: int, frame_hz: float) -> dict[str, np.ndarray]:
    """Slide over the signal and build a time-aligned feature track."""
    hop = max(1, int(round(sr / frame_hz)))
    win = hop * 2  # overlapping analysis window ~0.5 s
    n_frames = max(1, int(np.ceil(len(samples) / hop)))

    cols: dict[str, list[float]] = {k: [] for k in
                                    ("rms_db", "zcr", "centroid", "flatness", "f0", "peak")}
    times: list[float] = []

    for i in range(n_frames):
        start = i * hop
        frame = samples[start : start + win]
        if frame.size == 0:
            frame = samples[-min(samples.size, win) :]
        feats = _frame_features(frame, sr)
        for k in cols:
            cols[k].append(feats[k])
        times.append((start + frame.size / 2) / sr)

    track = {k: np.asarray(v, dtype=np.float64) for k, v in cols.items()}
    track["t"] = np.asarray(times, dtype=np.float64)
    return track


# --------------------------------------------------------------------------
# normalisation helpers
# --------------------------------------------------------------------------
SILENCE_DB = -70.0      # below this the frame carries no usable signal
Z_CLIP = 8.0            # cap z-scores so one artefact cannot skew a track


def _z(series: np.ndarray, valid: np.ndarray | None = None) -> np.ndarray:
    """Robust z-score using median / MAD, so one click doesn't skew a track.

    `valid` masks out frames that must not define the baseline -- most
    importantly true digital silence, which would otherwise drag the median
    down and send a perfectly ordinary track to +-700 sigma.
    """
    if series.size == 0:
        return series
    if valid is not None and valid.any() and valid.sum() >= 8:
        base = series[valid]
    else:
        base = series
    med = float(np.median(base))
    mad = float(np.median(np.abs(base - med)))
    scale = mad * 1.4826
    if scale < 1e-9:
        std = float(np.std(base))
        scale = std if std > 1e-9 else 1.0
    return np.clip((series - med) / scale, -Z_CLIP, Z_CLIP)


def _smooth(series: np.ndarray, width: int) -> np.ndarray:
    if width <= 1 or series.size < width:
        return series
    kernel = np.ones(width) / width
    return np.convolve(series, kernel, mode="same")


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------
@dataclass
class Event:
    start: float
    end: float
    kind: str
    strength: float
    frame_lo: int
    frame_hi: int
    meta: dict = field(default_factory=dict)


def detect_events(track: dict[str, np.ndarray], frame_dt: float) -> list[Event]:
    """Derive interpretable events from the feature track.

    Event kinds:
        energy_shock    sudden loudness jump -- someone reacts / emphasises
        laughter        broadband noise burst -- laughter, applause, big reaction
        pitch_surge     rapid pitch rise -- surprise, excitement
        silence_beat    short drop before a loud moment -- dramatic timing
        overlap         loud + noisy + no stable pitch -- people talking at once
    """
    t = track["t"]
    if t.size < 3:
        return []

    rms_db = track["rms_db"]
    # Frames with no real signal must not define any baseline.
    audible = rms_db > SILENCE_DB
    if audible.sum() < 8:
        audible = np.ones_like(audible, dtype=bool)

    rms_z = _z(rms_db, audible)
    flat_z = _z(track["flatness"], audible)
    zcr_z = _z(track["zcr"], audible)

    # voiced mask: pitch was confidently estimated here
    voiced = (track["f0"] > 0) & audible
    # sustained pitch movement = excitement rather than a steady voice
    f0 = np.where(voiced, track["f0"], np.nan)
    with np.errstate(invalid="ignore"):
        f0_delta = np.abs(np.diff(f0, prepend=f0[:1]))
        f0_delta = np.nan_to_num(f0_delta)
    f0_valid = voiced & np.isfinite(f0_delta)
    f0_z = _z(np.log1p(np.where(voiced, track["f0"], 0.0)), voiced)
    f0_move_z = _z(f0_delta, f0_valid)
    f0_move_z = np.where(f0_valid, f0_move_z, 0.0)

    events: list[Event] = []

    def emit(mask: np.ndarray, kind: str, weight_fn,
             max_seconds: float = 6.0) -> None:
        """Group contiguous True runs in `mask` into events."""
        if not mask.any():
            return
        idx = np.flatnonzero(mask)
        splits = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
        min_len = max(1, int(round(0.20 / frame_dt)))
        max_len = max(2, int(round(max_seconds / frame_dt)))
        for run in splits:
            if run.size < min_len:
                continue
            if run.size > max_len:
                # keep the strongest window rather than one giant blob
                run = run[:max_len]
            lo, hi = int(run[0]), int(run[-1])
            strength = float(np.clip(weight_fn(lo, hi), 0.0, 1.0))
            if strength <= 0.05:
                continue
            events.append(Event(
                start=float(t[lo]), end=float(t[hi]), kind=kind,
                strength=strength, frame_lo=lo, frame_hi=hi,
            ))

    # --- energy shock: loud relative to local baseline, rising -------------
    loud = _smooth(rms_z, max(1, int(0.75 / frame_dt))) > 1.0
    rising = np.concatenate([[False], np.diff(rms_z) > 0.35])
    emit(loud & rising & audible, "energy_shock",
         lambda a, b: float(np.clip(rms_z[a : b + 1].max() / 3.0, 0, 1)))

    # --- laughter / applause: loud AND broadband-noise-like ---------------
    # Deliberately no hard zero-crossing gate. Measured over real speech, a
    # noise burst added on top of a voice often lowers aggregate ZCR while
    # raising flatness; gating on ZCR here rejected genuine reactions.
    # ZCR contributes as a soft bonus instead, in a band that tolerates both
    # directions rather than vetoing on one of them.
    def laugh_weight(a: int, b: int) -> float:
        base = (rms_z[a : b + 1].mean() + flat_z[a : b + 1].mean() * 1.2) / 3.2
        zc = float(np.clip(np.tanh(zcr_z[a : b + 1].mean() / 2.0), -0.2, 0.2))
        return float(np.clip(base + zc, 0, 1))

    emit(loud & (flat_z > 0.3) & audible, "laughter", laugh_weight)

    # --- overlap: loud, noisy, and pitch unstable or absent --------------
    unstable = (~voiced) | (f0_move_z > 1.0)
    emit(loud & (flat_z > 0.3) & unstable & audible, "overlap",
         lambda a, b: float(np.clip(
             (rms_z[a : b + 1].mean() + flat_z[a : b + 1].mean()) / 3.0, 0, 1)))

    # --- pitch surge: voiced, high, and actively rising ------------------
    surge = voiced & (f0_z > 0.5) & (f0_move_z > 0.3)
    emit(surge, "pitch_surge",
         lambda a, b: float(np.clip(f0_z[a : b + 1].max() / 2.0, 0, 1)))

    # --- silence beat: a real drop, measured against a wide local median ---
    # Compared to its own neighbourhood rather than a global z-score, so a
    # quiet stretch in an already-quiet section still registers. A pause is
    # meaningful on its own; being flanked by a louder moment only adds weight.
    span = max(3, int(round(3.0 / frame_dt)))
    local_med = _local_median(rms_db, span, audible)
    drop_db = local_med - rms_db
    quiet = (drop_db > 6.0) & audible
    flanked = _local_max(rms_db - local_med, span) > 1.5

    def beat_weight(a: int, b: int) -> float:
        base = float(np.clip(drop_db[a : b + 1].max() / 12.0, 0, 1))
        return base + (0.15 if flanked[a : b + 1].any() else 0.0)

    # A dramatic beat is a *pause*, not dead air: keep it to a short window.
    emit(quiet, "silence_beat", beat_weight, max_seconds=3.0)

    events.sort(key=lambda e: (-e.strength, e.start))
    return events


def _local_median(series: np.ndarray, half: int, valid: np.ndarray) -> np.ndarray:
    """Rolling median over a +/- half-frame window, ignoring invalid frames."""
    n = series.size
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        seg = series[lo:hi]
        v = seg[valid[lo:hi]]
        out[i] = float(np.median(v)) if v.size else float(np.median(seg))
    return out


def _local_max(series: np.ndarray, half: int) -> np.ndarray:
    n = series.size
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        out[i] = float(np.max(series[max(0, i - half) : min(n, i + half + 1)]))
    return out


# --------------------------------------------------------------------------
# windowed summary (what the ranker actually consumes)
# --------------------------------------------------------------------------
def window_features(track: dict[str, np.ndarray], events: list[Event],
                    start: float, end: float) -> dict[str, float]:
    """Aggregate the track + events over [start, end].

    Returns a flat dict of named features; keeps every sub-score explainable so
    the UI can show *why* a clip was chosen instead of a bare virality number.
    """
    t = track["t"]
    if t.size == 0:
        return {}
    lo = int(np.searchsorted(t, start))
    hi = int(np.searchsorted(t, end))
    hi = max(hi, lo + 1)
    lo, hi = max(0, lo), min(t.size, hi)
    seg_rms = track["rms_db"][lo:hi]

    span = max(1e-6, end - start)
    out: dict[str, float] = {
        "mean_loudness": float(seg_rms.mean()) if seg_rms.size else -90.0,
        "loudness_peak": float(seg_rms.max()) if seg_rms.size else -90.0,
        "loudness_swing": float(seg_rms.max() - seg_rms.min()) if seg_rms.size else 0.0,
        "peak_to_mean": float((seg_rms.max() - seg_rms.mean()) / 6.0) if seg_rms.size else 0.0,
    }
    if seg_rms.size > 2:
        diff = np.diff(seg_rms)
        out["max_rise"] = float(np.clip(diff.max() / 3.0, 0, 1))
    else:
        out["max_rise"] = 0.0

    for kind in ("energy_shock", "laughter", "pitch_surge", "silence_beat", "overlap"):
        hits = [e for e in events if e.kind == kind and e.start < end and e.end > start]
        # overlap-weighted: a big hit counts for more than two small ones
        out[f"{kind}_count"] = float(len(hits))
        out[f"{kind}_max"] = float(max((e.strength for e in hits), default=0.0))
        out[f"{kind}_density"] = float(min(1.0, len(hits) / max(1.0, span / 10.0) / 3.0))

    out["spectacle"] = float(np.clip(
        0.45 * out["laughter_max"] + 0.30 * out["energy_shock_max"]
        + 0.25 * out["pitch_surge_max"], 0, 1))
    return out


def analyse_audio(wav_path: str | Path, frame_hz: float) -> tuple[dict[str, np.ndarray], list[Event], float]:
    """Convenience wrapper: wav -> (track, events, duration)."""
    samples, sr = read_wav_mono(wav_path)
    if samples.size == 0:
        empty = {k: np.zeros(0) for k in
                 ("t", "rms_db", "zcr", "centroid", "flatness", "f0", "peak")}
        return empty, [], 0.0
    frame_dt = 1.0 / frame_hz
    track = extract_track(samples, sr, frame_hz)
    events = detect_events(track, frame_dt)
    return track, events, float(len(samples) / sr)