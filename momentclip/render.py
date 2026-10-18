"""Clip rendering: cut, reframe, burn captions.

Explicit guarantee: this module never draws a watermark, badge, logo or any
persistent overlay onto a frame. The only overlay it will ever add is the
speaker's own caption text, and even that is disableable via CLIPPER_CAPTIONS=0.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import captions, ffmpeg
from .asr import Word
from .config import Config, get_config

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


# --------------------------------------------------------------------------
# framing
# --------------------------------------------------------------------------
def _face_center(src: Path, start: float, end: float,
                 sample_fps: float = 4.0) -> tuple[float | None, float, str]:
    """Mean horizontal face centre as a fraction of frame width.

    Returns (centre, face_fraction, note). Falls back to the frame centre.
    """
    cv2 = _load_cv2()
    if cv2 is False:
        return None, 0.0, "opencv unavailable, centred crop"

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
            return None, 0.0, "no frames sampled"

        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        if not cascade_path.exists():
            return None, 0.0, "no face cascade available"
        cascade = cv2.CascadeClassifier(str(cascade_path))
        if cascade.empty():
            return None, 0.0, "face cascade failed to load"

        centres: list[tuple[float, float]] = []
        for fp in frames:
            img = cv2.imread(str(fp))
            if img is None:
                continue
            h, w = img.shape[:2]
            grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            grey = cv2.equalizeHist(grey)
            faces = cascade.detectMultiScale(grey, scaleFactor=1.15,
                                             minNeighbors=5, minSize=(48, 48))
            if len(faces):
                # biggest face == whoever is closest to camera == the speaker
                x, y, fw, fh = max(faces, key=lambda r: r[2] * r[3])
                centres.append(((x + fw / 2.0) / w, fw / w))

        if not centres:
            return None, 0.0, "no face found, centred crop"

        arr = np.asarray(centres, dtype=np.float64)
        return float(np.median(arr[:, 0])), float(np.median(arr[:, 1])), ""
    except Exception as exc:  # noqa: BLE001
        log.debug("face detection failed: %s", exc)
        return None, 0.0, f"face detection failed ({type(exc).__name__}), centred crop"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _crop_filter(src_w: int, src_h: int, out_w: int, out_h: int,
                 centre_x: float | None, face_frac: float) -> str:
    """Build a scale+crop filter chain to reach out_w x out_h from src.

    Strategy: cover the target box with a scale, then crop. For vertical output
    from a landscape source this necessarily throws away the sides -- the crop
    window is biased toward the detected speaker, never toward a fixed edge.
    """
    target_ar = out_w / out_h
    src_ar = src_w / src_h if src_h else target_ar

    if src_ar > target_ar:
        # source is wider -> scale by height, crop horizontally
        scaled_w = int(round(src_h * target_ar))
        scaled_w = max(2, min(scaled_w, src_w))
        x_expr = "(iw-{})/2".format(scaled_w)
        if centre_x is not None:
            # keep the speaker near the middle third, clamp so we never over-crop
            desired = centre_x * src_w - scaled_w / 2.0
            lo, hi = 0.0, float(src_w - scaled_w)
            x_expr = f"min({hi},max({lo},{desired:.2f}))"
        return (f"scale=-2:{out_h},crop={out_w}:{out_h}:'{x_expr}':0",
                f"scaled_w={scaled_w}")

    # source is taller or equal -> scale by width, crop vertically (bias to upper third)
    scaled_h = int(round(src_w / target_ar))
    scaled_h = max(2, min(scaled_h, src_h))
    y_bias = int(round((src_h - scaled_h) * (0.25 if face_frac > 0 else 0.5)))
    return (f"scale={out_w}:-2,crop={out_w}:{out_h}:0:{y_bias}",
            f"scaled_h={scaled_h}")


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
    want_captions: bool | None = None,
    want_reframe: bool | None = None,
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

    info = ffmpeg.probe(src)
    if not info.has_video:
        raise RuntimeError("source has no video stream")

    duration = max(0.2, end - start)

    # ---- framing ------------------------------------------------------
    centre_x, face_frac = None, 0.0
    reframed = False
    need_crop = (out_w / out_h) != info.aspect and want_reframe
    if need_crop:
        centre_x, face_frac, note = _face_center(src, start, end)
        if note:
            notes.append(note)
        reframed = True

    vf, _ = _crop_filter(info.width, info.height, out_w, out_h, centre_x, face_frac)
    if not need_crop:
        # still normalise geometry without cropping content
        vf = f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease," \
             f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    else:
        vf += ",setsar=1"

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
            notes.append("ffmpeg built without libass; captions not burned")
        # sidecar .srt is always written for the user to reuse
        try:
            captions.write_srt(words, dest.with_suffix(".srt"))
        except Exception:  # noqa: BLE001
            pass

    if ass_path is not None:
        escaped = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        vf += f",subtitles='{escaped}'"
        burned = True

    # ---- encode -------------------------------------------------------
    encoder = ffmpeg.pick_h264_encoder()
    cmd = [
        cfg.ffmpeg, "-y",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(src),
        "-vf", vf,
        "-c:v", encoder,
        "-pix_fmt", "yuv420p",
    ]
    # Only pass encoder flags the chosen encoder actually understands --
    # -preset means something different (or nothing) on each of them.
    if encoder == "libx264":
        cmd += ["-preset", "veryfast", "-crf", "20"]
    elif encoder == "h264_nvenc":
        cmd += ["-preset", "p5", "-cq", "21", "-b:v", "0"]
    elif encoder == "h264_mf":
        cmd += ["-b:v", "6M"]
    else:
        cmd += ["-q:v", "3"]
    cmd += [
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "160k",
        str(dest),
    ]
    ffmpeg.run(cmd, timeout=1800)

    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError("render produced an empty file")

    log.info("rendered %s (%.1fs, %s, encoder=%s)", dest.name, duration, aspect, encoder)
    return RenderResult(dest, out_w, out_h, duration, burned, reframed, encoder, notes)