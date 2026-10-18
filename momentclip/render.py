"""Clip rendering: cut, auto-zoom reframe, captions, music bed, comedy SFX.

Explicit guarantee: this module never draws a watermark, badge, logo or any
persistent overlay onto a frame. The only overlays it will ever add are the
speaker's own caption text and, when asked, the speaker's own headline text.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import captions, ffmpeg
from .asr import Word
from .config import Config, get_config
from .soundtrack import MixSpec

log = logging.getLogger("momentclip.render")

ASPECTS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "16:9": (1920, 1080),
}

_cv2 = None


def _load_cv2():
    global _cv2
    if _cv2 is None:
        try:
            import cv2  # type: ignore
            _cv2 = cv2
        except ImportError:
            _cv2 = False
    return _cv2


@dataclass
class RenderResult:
    path: Path
    width: int
    height: int
    duration: float
    burned_captions: bool
    reframed: bool
    encoder: str
    notes: list[str]
    music: str | None = None
    sfx: list[str] = field(default_factory=list)
    zoom: float = 1.0


# --------------------------------------------------------------------------
# subject detection
# --------------------------------------------------------------------------
def _cascade_path() -> Path | None:
    """Locate the Haar frontal-face cascade.

    OpenCV 5.0 stopped shipping these XML files inside the wheel, so an
    installed-but-useless OpenCV is a real and silent failure mode. Look in
    cv2.data first, then anywhere under the package directory.
    """
    cv2 = _load_cv2()
    if cv2 is False:
        return None

    name = "haarcascade_frontalface_default.xml"
    candidates: list[Path] = []
    try:
        candidates.append(Path(cv2.data.haarcascades) / name)
    except Exception:  # noqa: BLE001
        pass
    try:
        base = Path(cv2.__file__).resolve().parent
        candidates += sorted(base.rglob(name))
    except Exception:  # noqa: BLE001
        pass

    for c in candidates:
        if c.exists():
            return c
    return None


def face_tracking_available() -> bool:
    """True only when face detection can genuinely run."""
    return _load_cv2() is not False and _cascade_path() is not None


def _face_series(src: Path, start: float, end: float, samples: int = 5,
                 sample_fps: float = 5.0) -> tuple[list[float], str]:
    """Face centre sampled across the clip, as fractions of frame width.

    Returned in order, so the crop can follow a speaker who moves rather than
    locking to one averaged position for the whole clip.
    """
    cv2 = _load_cv2()
    if cv2 is False:
        return [], "opencv unavailable, centred crop"
    cascade_file = _cascade_path()
    if cascade_file is None:
        return [], ("no face cascade (install opencv-python-headless<5 "
                    "for speaker tracking), centred crop")

    duration = max(0.1, end - start)
    tmpdir = Path(tempfile.mkdtemp(prefix="clipper-faces-"))
    try:
        pattern = str(tmpdir / "f_%05d.jpg")
        ffmpeg.run([
            get_config().ffmpeg, "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", str(src),
            "-vf", f"fps={sample_fps},scale=640:-2",
            "-q:v", "3", pattern,
        ], timeout=900)

        frames = sorted(tmpdir.glob("f_*.jpg"))
        if not frames:
            return [], "no frames sampled"

        cascade = cv2.CascadeClassifier(str(cascade_file))
        if cascade.empty():
            return [], "face cascade failed to load"

        per_frame: list[float] = []
        for fp in frames:
            img = cv2.imread(str(fp))
            if img is None:
                continue
            h, w = img.shape[:2]
            grey = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
            faces = cascade.detectMultiScale(grey, scaleFactor=1.15,
                                             minNeighbors=5, minSize=(48, 48))
            if len(faces):
                # biggest face == whoever is closest to camera == the speaker
                x, _y, fw, fh = max(faces, key=lambda r: r[2] * r[3])
                per_frame.append((x + fw / 2.0) / w)

        if not per_frame:
            return [], "no face found, centred crop"

        # Haar cascades fire on gradients, rectangles and text as readily as on
        # faces. A lone detection is noise, so require it to hold up over the
        # clip before we let it steer the crop.
        n = len(per_frame)
        coverage = n / max(1, len(frames))
        if coverage < 0.25:
            return [], f"face detected in only {coverage:.0%} of frames, centred crop"

        arr = np.asarray(per_frame, dtype=np.float64)
        med = float(np.median(arr))
        # reject a track that jumps around wildly -- that is noise, not a person
        spread = float(np.std(arr))
        if spread > 0.18:
            kept = [v for v in per_frame if abs(v - med) < spread]
            if kept:
                per_frame = kept
            else:
                return [], "face track too unstable, centred crop"

        # bucket into `samples` sections so the crop can move between speakers
        out: list[float] = []
        n = len(per_frame)
        for i in range(samples):
            lo = int(i * n / samples)
            hi = max(lo + 1, int((i + 1) * n / samples))
            chunk = per_frame[lo:hi]
            out.append(float(np.median(chunk)) if chunk else 0.5)
        return out, ""
    except Exception as exc:  # noqa: BLE001
        log.debug("face detection failed: %s", exc)
        return [], f"face detection failed ({type(exc).__name__}), centred crop"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _face_center(src: Path, start: float, end: float,
                 sample_fps: float = 5.0) -> tuple[float | None, float, str]:
    """Median face centre over the whole clip. Compatibility wrapper."""
    series, note = _face_series(src, start, end, samples=1,
                                 sample_fps=sample_fps)
    return (series[0] if series else None), 0.0, note


# --------------------------------------------------------------------------
# video filter chain
# --------------------------------------------------------------------------
def _zoom_expr(z_start: float, z_end: float, frames: int) -> str:
    """Linear push-in across the clip, as an ffmpeg crop expression.

    Indexed by frame number `n`, not `t`: crop's w/h expressions are evaluated
    at configure time when `t` is not yet defined, and fail outright.
    """
    n = max(1, int(frames))
    return f"({z_start:.5f}+({z_end - z_start:.5f})*min(1,n/{n}))"


def _reactive_zoom_expr(z_start: float, z_end: float, frames: int,
                        reactions: list[tuple[float, float]],
                        fps: float) -> str:
    """Zoom that PUNCHES on reaction moments instead of drifting uniformly.

    A constant push-in reads as a still photo with a slow crop. What makes
    short-form feel edited is the frame moving *because something happened*:
    a laugh lands, the camera moves in, then settles back.

    Each reaction contributes a short triangular pulse centred on its peak,
    raised to a power so the falloff is soft rather than a visible corner.

    Commas inside max()/pow() must be escaped or ffmpeg's filter parser splits
    the argument list in the wrong place.
    """
    base = _zoom_expr(z_start, z_end, frames)
    if not reactions or fps <= 0:
        return base

    z = base
    for t_rel, strength in reactions[:2]:
        c = int(round(max(0.0, t_rel) * fps))
        if c <= 0:
            continue
        w = max(4, int(0.40 * fps))          # pulse half-width, in frames
        amp = 0.05 + 0.09 * max(0.0, min(1.0, strength))
        z += f"+{amp:.4f}*pow(max(0\\,1-abs(n-{c})/{w})\\,1.6)"
    return z


def _pan_expr(cx0: float, cx1: float, frames: int) -> str:
    """Interpolate the subject's horizontal position across the clip.

    Lets the crop follow a speaker who moves, or cut between two people in a
    two-shot, instead of averaging them into the middle for the whole clip.
    """
    n = max(1, int(frames))
    return f"({cx0:.4f}+({cx1 - cx0:.4f})*min(1,n/{n}))"


def _video_filter(src_w: int, src_h: int, out_w: int, out_h: int,
                  centre_x: float | None, frames: int,
                  zoom_start: float, zoom_end: float,
                  reactions: list[tuple[float, float]] | None = None,
                  fps: float = 30.0) -> str:
    """Cover-crop to the target box, then push in over time.

    The zoom is done by cropping an ever-smaller window and scaling it back up,
    which is a true zoom. (Scaling the whole frame by z and then cropping the
    full output size just fails -- a 1280x720 source scaled by 1.04 cannot
    yield a 1080x1920 crop.)

    Uses scale + crop with time expressions rather than zoompan: zoompan has
    long-standing framerate and integer-rounding bugs across ffmpeg builds.
    """
    target_ar = out_w / out_h
    src_ar = src_w / src_h if src_h else target_ar

    # `centre_x` may be a list (speaker-following samples) or a single float.
    # Normalise once: every branch below wants a plain number.
    if isinstance(centre_x, (list, tuple)):
        cx_static = float(centre_x[len(centre_x) // 2]) if len(centre_x) else 0.5
    elif centre_x is None:
        cx_static = None
    else:
        cx_static = float(centre_x)

    # --- step 1: cover the output box exactly ---
    if src_ar > target_ar:
        cw = max(2, min(src_w, int(round(src_h * target_ar))))
        x = 0 if cx_static is None else int(round(
            min(max(0, src_w - cw), max(0.0, cx_static * src_w - cw / 2))))
        base = f"scale=-2:{out_h},crop={out_w}:{out_h}:{x}:0"
    else:
        ch = max(2, min(src_h, int(round(src_w / target_ar))))
        y = int(round((src_h - ch) * 0.3))
        base = f"scale={out_w}:-2,crop={out_w}:{out_h}:0:{y}"

    if zoom_end <= 1.001 and zoom_start <= 1.001:
        return base + ",setsar=1"

    # --- step 2: animated zoom crop out of the covered frame ---
    z = _reactive_zoom_expr(zoom_start, zoom_end, frames, reactions or [], fps)
    # z is >= 1.02 by construction, so the denominator is never zero and the
    # window always fits inside the covered frame
    crop_w = f"{out_w}/{z}"
    crop_h = f"{out_h}/{z}"
    slack_x = f"({out_w}-{crop_w})"
    slack_y = f"({out_h}-{crop_h})"

    if centre_x is None:
        x_expr = f"{slack_x}/2"
    else:
        # follow the subject, then clamp inside the frame
        cx = _pan_expr(centre_x[0], centre_x[-1], frames) \
            if isinstance(centre_x, (list, tuple)) and len(centre_x) >= 2 \
            else f"{float(centre_x):.4f}"
        x_expr = f"clip({cx}*{out_w}-{crop_w}/2\\,0\\,{slack_x})"
    y_expr = f"{slack_y}/2"

    return (f"{base},"
            f"crop=w='{crop_w}':h='{crop_h}':x='{x_expr}':y='{y_expr}':exact=1,"
            f"scale={out_w}:{out_h}:flags=lanczos,setsar=1")


# --------------------------------------------------------------------------
# audio graph
# --------------------------------------------------------------------------
def _audio_graph(mix: MixSpec | None, stingers: list[tuple[float, str]],
                 dest: Path) -> tuple[str, list[str], list[str]]:
    """Build the audio mix.

    Returns (filter_complex, extra_input_args, notes).
    """
    notes: list[str] = []
    if mix is None:
        return "", [], notes

    inputs: list[str] = []
    filters: list[str] = []

    bg_label = "[0:a]"

    if mix.music:
        inputs += ["-i", str(mix.music)]
        idx = len(inputs) // 2
        g = max(0.0, min(1.0, mix.music_gain))
        filters.append(
            f"[{idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"volume={g:.3f}[mus]"
        )
        if mix.duck:
            filters.append(
                "[mus][0:a]sidechaincompress=threshold=0.035:ratio=9:"
                "attack=15:release=350[duck]"
            )
            bg_label = "[duck]"
            notes.append("music ducked under speech")
        else:
            bg_label = "[mus]"

    if stingers and mix.stinger_paths:
        labels = []
        for i, (offset, _kind) in enumerate(stingers):
            if i >= len(mix.stinger_paths):
                break
            inputs += ["-i", str(mix.stinger_paths[i])]
            idx = len(inputs) // 2
            ms = int(max(0.0, offset) * 1000)
            labels.append(f"[s{i}]")
            filters.append(
                f"[{idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                f"volume={mix.stinger_gain:.3f},adelay={ms}|{ms}[s{i}]"
            )
        merged = "".join(labels)
        filters.append(
            f"{merged}amix=inputs={len(labels)}:duration=longest:normalize=0[stx]"
        )
        if bg_label != "[0:a]":
            # bg_label already carries its own brackets: [duck]
            filters.append(f"{bg_label}[stx]amix=inputs=2:duration=first:"
                           f"normalize=0[bg]")
            bg_label = "[bg]"
        else:
            filters.append("[stx]anull[bg]")
            bg_label = "[bg]"

    if bg_label == "[0:a]":
        return "", inputs, notes

    filters.append(
        f"[0:a]{bg_label}amix=inputs=2:duration=first:"
        f"dropout_transition=0,alimiter=limit=0.97[aout]"
    )
    return ";".join(filters), inputs, notes


# --------------------------------------------------------------------------
# main render
# --------------------------------------------------------------------------
def render_clip(
    src: str | Path,
    start: float,
    end: float,
    dest: str | Path,
    *,
    words: list[Word] | None = None,
    aspect: str = "9:16",
    caption_style: str = "pop",
    headline: str = "",
    want_captions: bool | None = None,
    want_reframe: bool | None = None,
    want_zoom: bool | None = None,
    reactions: list[tuple[float, float]] | None = None,
    music: MixSpec | None = None,
    stingers: list[tuple[float, str]] | None = None,
    cfg: Config | None = None,
) -> RenderResult:
    cfg = cfg or get_config()
    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    out_w, out_h = ASPECTS.get(aspect, ASPECTS["9:16"])
    want_captions = cfg.burn_captions if want_captions is None else want_captions
    want_reframe = cfg.reframe if want_reframe is None else want_reframe
    want_zoom = cfg.auto_zoom if want_zoom is None else want_zoom
    stingers = stingers or []

    info = ffmpeg.probe(src)
    if not info.has_video:
        raise RuntimeError("source has no video stream")

    duration = max(0.2, end - start)

    # ---- subject detection ------------------------------------------
    face_series: list[float] = []
    centre_x = None
    reframed = False
    need_crop = (out_w / out_h) != info.aspect
    face_found = False
    if need_crop and (want_reframe or want_zoom):
        face_series, note = _face_series(src, start, end, samples=5)
        if note:
            notes.append(note)
        face_found = bool(face_series)
        if face_found:
            centre_x = face_series      # list -> pan; float -> fixed
        reframed = True

    fps = info.fps if info.fps and info.fps > 1 else 30.0

    # a face gets a stronger baseline push-in: it is the subject, so move in
    if want_zoom:
        if face_found:
            z0, z1 = 1.03, 1.09
        else:
            z0, z1 = 1.01, 1.05
    else:
        z0 = z1 = 1.0

    frames = max(1, int(round(duration * fps)))
    vf = _video_filter(info.width, info.height, out_w, out_h,
                       centre_x, frames, z0, z1,
                       reactions=reactions, fps=fps)

    # ---- captions -----------------------------------------------------
    burned = False
    ass_path: Path | None = None
    if want_captions and words:
        if ffmpeg.has_libass(cfg):
            ass_path = dest.with_suffix(".ass")
            made = captions.write_ass(words, ass_path, caption_style,
                                      offset=start, width=out_w, height=out_h)
            if made is None:
                ass_path = None
                notes.append("no caption groups in this clip")
            else:
                burned = True
        else:
            notes.append("ffmpeg built without libass; captions not burned")
        try:
            captions.write_srt(words, dest.with_suffix(".srt"))
        except Exception:  # noqa: BLE001
            pass

    # ---- headline (the AI hook, OpusClip's "auto headline") -----------
    # Only the speaker's own words are shown; still not a watermark.
    if headline:
        try:
            hp = dest.with_name(dest.stem + "_headline.ass")
            if captions.write_headline(headline, hp, offset=start,
                                       width=out_w, height=out_h):
                esc = str(hp).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
                vf += f",subtitles='{esc}'"
                notes.append("headline overlay added")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"headline skipped: {exc}")

    if ass_path is not None:
        esc = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        vf += f",subtitles='{esc}'"

    # ---- audio --------------------------------------------------------
    af_complex, extra_inputs, anotes = _audio_graph(music, stingers, dest)
    notes.extend(anotes)

    # ---- encode -------------------------------------------------------
    encoder = ffmpeg.pick_h264_encoder()
    cmd = [cfg.ffmpeg, "-y",
           "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src)]
    cmd += extra_inputs
    cmd += ["-filter_complex", af_complex] if af_complex else []
    cmd += ["-vf", vf, "-map", "0:v:0"]

    if af_complex:
        cmd += ["-map", "[aout]"]
    elif info.has_audio:
        cmd += ["-map", "0:a:0?"]

    cmd += ["-c:v", encoder, "-pix_fmt", "yuv420p"]
    if encoder == "libx264":
        cmd += ["-preset", "veryfast", "-crf", "20"]
    elif encoder == "h264_nvenc":
        cmd += ["-preset", "p5", "-cq", "21", "-b:v", "0"]
    elif encoder == "h264_mf":
        cmd += ["-b:v", "6M"]
    else:
        cmd += ["-q:v", "3"]

    if af_complex:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100"]
    elif info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "160k"]

    cmd += ["-movflags", "+faststart", str(dest)]
    ffmpeg.run(cmd, timeout=1800)

    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError("render produced an empty file")

    log.info("rendered %s (%.1fs, %s, zoom %.2f->%.2f, reactions=%d, encoder=%s)",
             dest.name, duration, aspect, z0, z1, len(reactions or []), encoder)
    return RenderResult(
        path=dest, width=out_w, height=out_h, duration=duration,
        burned_captions=burned, reframed=reframed, encoder=encoder, notes=notes,
        music=music.music.name if (music and music.music) else None,
        sfx=[k for _t, k in stingers],
        # report the peak the viewer actually sees, not just the baseline drift
        zoom=round(z1 + sum(0.05 + 0.09 * max(0.0, min(1.0, s))
                            for _t, s in (reactions or [])[:2]), 3),
    )
