"""Thin, well-behaved wrappers around ffmpeg / ffprobe.

Every call is a list (never a shell string), so filenames with spaces or quotes are safe.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .config import Config, get_config

log = logging.getLogger("momentclip.ffmpeg")


class FFmpegMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class Probe:
    duration: float
    width: int
    height: int
    fps: float
    has_video: bool
    has_audio: bool
    vcodec: str
    acodec: str
    rotation: int

    @property
    def is_portrait(self) -> bool:
        return self.height > self.width

    @property
    def aspect(self) -> float:
        if not self.height:
            return 16 / 9
        return self.width / self.height


def _resolve(cfg: Config, name: str) -> str:
    path = _discover(name)
    if path is None:
        raise FFmpegMissing(
            f"{name!r} not found on PATH.\n"
            f"Install FFmpeg and restart the server, or set "
            f"FFMPEG_BIN / FFPROBE_BIN to absolute paths."
        )
    return path


_SEARCH_CACHE: dict[str, str | None] = {}


def _discover(name: str) -> str | None:
    """Find a binary on PATH, or in the usual per-user install locations.

    On Windows the winget "command alias" is a reparse point that frequently does
    not resolve inside a service or a non-interactive shell, so we also look in
    the WinGet package store and the standard install roots.
    """
    if name in _SEARCH_CACHE:
        return _SEARCH_CACHE[name]

    found = shutil.which(name)

    if found is None and os.name == "nt":
        exe = name + ".exe"
        roots = [
            os.environ.get("LOCALAPPDATA", "") + r"\Microsoft\WinGet\Packages",
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", "") + r"\Programs",
        ]
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                # prune the noise: only descend a few levels
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                if exe in filenames:
                    found = os.path.join(dirpath, exe)
                    break
                if dirpath.count(os.sep) - root.count(os.sep) > 5:
                    dirnames[:] = []
            if found:
                break

    _SEARCH_CACHE[name] = found
    return found


def available(cfg: Config | None = None) -> bool:
    return _discover(cfg.ffmpeg) is not None and _discover(cfg.ffprobe) is not None


def run(
    args: Sequence[str],
    *,
    timeout: int | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run a binary with an argument list. Never uses a shell."""
    exe = _resolve(get_config(), args[0])
    cmd = [exe, *args[1:]]
    log.debug("exec: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-12:]
        raise RuntimeError(
            f"{args[0]} exited {proc.returncode}\n" + "\n".join(tail)
        )
    return proc


def probe(path: str | Path) -> Probe:
    cfg = get_config()
    args = [
        cfg.ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    exe = _resolve(cfg, cfg.ffprobe)
    proc = subprocess.run([exe, *args[1:]], capture_output=True, text=True, timeout=120,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr.strip()[:500]}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", []) or []
    fmt = data.get("format", {}) or {}

    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for src in (fmt.get("duration"), (v or {}).get("duration"), (a or {}).get("duration")):
        try:
            duration = float(src)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    fps = 0.0
    if v and v.get("avg_frame_rate") and "/" in v["avg_frame_rate"]:
        num, _, den = v["avg_frame_rate"].partition("/")
        try:
            fps = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            fps = 0.0

    rotation = 0
    if v:
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                try:
                    rotation = int(float(sd["rotation"])) % 360
                except (TypeError, ValueError):
                    pass
        tag_rot = (v.get("tags") or {}).get("rotate")
        if not rotation and tag_rot:
            try:
                rotation = int(float(tag_rot)) % 360
            except ValueError:
                pass

    width = int((v or {}).get("width") or 0)
    height = int((v or {}).get("height") or 0)
    if rotation in (90, 270):
        width, height = height, width

    return Probe(
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        has_video=v is not None,
        has_audio=a is not None,
        vcodec=(v or {}).get("codec_name", "") or "",
        acodec=(a or {}).get("codec_name", "") or "",
        rotation=rotation,
    )


def has_encoder(name: str) -> bool:
    """Check whether the local ffmpeg build exposes an encoder."""
    try:
        proc = run([get_config().ffmpeg, "-hide_banner", "-encoders"], check=False)
        return name in (proc.stdout or "")
    except (FFmpegMissing, RuntimeError):
        return False


def pick_h264_encoder() -> str:
    """libx264 if compiled in, else h264_mf / h264_nvenc, else bare h264."""
    try:
        proc = run([get_config().ffmpeg, "-hide_banner", "-encoders"], check=False)
        encoders = proc.stdout or ""
    except (FFmpegMissing, RuntimeError):
        encoders = ""
    for name in ("libx264", "h264_nvenc", "h264_mf", "h264"):
        if name in encoders:
            return name
    return "libx264"


def extract_audio(src: str | Path, dest: str | Path, sr: int | None = None) -> Path:
    """Decode to mono PCM wav. mono 16-bit is what numpy analysis wants."""
    cfg = get_config()
    sr = sr or cfg.analysis_sr
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        cfg.ffmpeg, "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", str(sr),
        "-c:a", "pcm_s16le", str(dest),
    ], timeout=3600)
    return dest


def has_libass(cfg: Config | None = None) -> bool:
    cfg = cfg or get_config()
    try:
        proc = run([cfg.ffmpeg, "-hide_banner", "-version"], check=False)
        return "libass" in (proc.stdout or "")
    except (FFmpegMissing, RuntimeError):
        return False


def version() -> str:
    """First line of `ffmpeg -version`, or "" if unavailable."""
    try:
        proc = run([get_config().ffmpeg, "-hide_banner", "-version"], check=False)
        out = (proc.stdout or "").strip().splitlines()
        return out[0] if out else ""
    except (FFmpegMissing, RuntimeError):
        return ""