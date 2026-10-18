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

from . import captions, ffmpeg, soundtrack
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


def _face_center(src: Path, start: float, end: float,
                 sample_fps: float = 5.0) -> tuple[float | None, float, str]:
    """Median horizontal face centre as a fraction of frame width.

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

        cascade_file = _cascade_path()
        if cascade_file is None:
            return None, 0.0, (
                "no face cascade (OpenCV 5 dropped it -- install "
                "opencv-python-headless<5 for speaker tracking), centred crop"
            )
        cascade = cv2.CascadeClassifier(str(cascade_file))
        if cascade.empty():
            return None, 0.0, "face cascade failed to load"

        centres: list[tuple[float, float]] = []
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


def _video_filter(src_w: int, src_h: int, out_w: int, out_h: int,
                  centre_x: float | None, frames: int,
                  zoom_start: float, zoom_end: float) -> str:
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

    # --- step 1: cover the output box exactly ---
    if src_ar > target_ar:
        cw = max(2, min(src_w, int(round(src_h * target_ar))))
        x = 0 if centre_x is None else int(round(
            min(max(0, src_w - cw), max(0.0, centre_x * src_w - cw / 2))))
        base = f"scale=-2:{out_h},crop={out_w}:{out_h}:{x}:0"
    else:
        ch = max(2, min(src_h, int(round(src_w / target_ar))))
        y = int(round((src_h - ch) * 0.3))
        base = f"scale={out_w}:-2,crop={out_w}:{out_h}:0:{y}"

    if zoom_end <= 1.001 and zoom_start <= 1.001:
        return base + ",setsar=1"

    # --- step 2: animated zoom crop out of the covered frame ---
    z = _zoom_expr(zoom_start, zoom_end, frames)
    # z is >= 1.02 by construction, so the denominator is never zero and the
    # window always fits inside the covered frame
    crop_w = f"{out_w}/{z}"
    crop_h = f"{out_h}/{z}"
    slack_x = f"({out_w}-{crop_w})"
    slack_y = f"({out_h}-{crop_h})"

    if centre_x is None:
        x_expr = f"{slack_x}/2"
    else:
        # bias the window toward the speaker, then clamp inside the frame
        x_expr = f"clip({centre_x:.4f}*{out_w}-{crop_w}/2\\,0\\,{slack_x})"
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
    chain: list[str] = []

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
                f"[mus][0:a]sidechaincompress=threshold=0.035:ratio=9:"
                f"attack=15:release=350[duck]"
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
    centre_x, face_frac = None, 0.0
    reframed = False
    need_crop = (out_w / out_h) != info.aspect
    if need_crop and (want_reframe or want_zoom):
        centre_x, face_frac, note = _face_center(src, start, end)
        if note:
            notes.append(note)
        reframed = True

    # a face gets a stronger push-in: it is the subject, so we move in on it
    if want_zoom:
        if face_frac > 0:
            z0, z1 = 1.04, 1.14
        else:
            z0, z1 = 1.02, 1.07
    else:
        z0 = z1 = 1.0

    frames = max(1, int(round(duration * (info.fps or 30.0))))
    vf = _video_filter(info.width, info.height, out_w, out_h,
                       centre_x, frames, z0, z1)

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

    log.info("rendered %s (%.1fs, %s, zoom %.2f->%.2f, encoder=%s)",
             dest.name, duration, aspect, z0, z1, encoder)
    return RenderResult(
        path=dest, width=out_w, height=out_h, duration=duration,
        burned_captions=burned, reframed=reframed, encoder=encoder, notes=notes,
        music=music.music.name if (music and music.music) else None,
        sfx=[k for _t, k in stingers], zoom=round(z1, 3),
    )