"""FastAPI application: upload, analyse, render, review, download.

No watermark is added anywhere in this project. See momentclip/render.py.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import asr, captions, ffmpeg, momentgraph, pipeline, rank, render, soundtrack
from .config import CLIP_LENGTHS, get_config
from .jobs import get_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("momentclip.server")

cfg = get_config()
cfg.ensure_dirs()

app = FastAPI(
    title="Clipper",
    description="Free, open-source, local video clipper. Finds funny, candid and "
                "surprising moments. No watermark, no subscription, no upload.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_UPLOAD_BYTES = int(20 * 1024 * 1024 * 1024)  # 20 GB
ALLOWED_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv", ".mp3", ".m4a", ".wav"}
manager = get_manager()


def _job_dir(job_id: str) -> Path:
    d = cfg.jobs_dir / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _guard(job_id: str) -> Path:
    """Resolve a path inside a job dir, refusing traversal."""
    base = cfg.jobs_dir.resolve()
    target = (base / job_id).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(400, "invalid job id")
    return target


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    caps = asr.capability()
    return {
        "ok": True,
        "ffmpeg": ffmpeg.available(cfg),
        "ffmpeg_version": ffmpeg.version(),
        "transcription": caps,
        "graph_backend": "graphify" if momentgraph.graphify_available() else "networkx",
        "libass": ffmpeg.has_libass(cfg),
        "opencv": render._load_cv2() is not False,
        "face_tracking": render.face_tracking_available(),
        "yt_dlp": shutil.which("yt-dlp") is not None,
        "llm_configured": cfg.has_llm,
        "watermark": False,
        "aspects": list(render.ASPECTS),
        "personas": list(rank.PERSONAS),
        "caption_presets": list(captions.PRESETS),
        "clip_lengths": [{"min": lo, "max": hi, "label": lbl}
                         for lo, hi, lbl in CLIP_LENGTHS],
        "moods": list(soundtrack.MOODS),
        "user_music": [p.name for p in soundtrack.user_tracks(cfg)],
        "defaults": {
            "min_clip": cfg.min_clip, "max_clip": cfg.max_clip,
            "max_clips": cfg.max_clips,
            "music": cfg.music, "auto_zoom": cfg.auto_zoom,
            "auto_sfx": cfg.auto_sfx, "auto_hook": cfg.auto_hook,
        },
    }


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------
@app.post("/api/jobs")
async def create_job(
    file: UploadFile | None = File(default=None),
    url: str | None = Form(default=None),
    persona: str = Form(default="general"),
    aspect: str = Form(default="9:16"),
    caption_style: str = Form(default="pop"),
    clip_length: str = Form(default="auto"),
    captions_on: bool = Form(default=True),
    reframe: bool = Form(default=True),
    auto_zoom: bool = Form(default=True),
    music: str = Form(default="upbeat"),
    auto_sfx: bool = Form(default=True),
    auto_hook: bool = Form(default=False),
    use_llm: bool = Form(default=True),
    use_graph: bool = Form(default=True),
    max_clips: int | None = Form(default=None),
) -> dict:
    if file is None and not (url or "").strip():
        raise HTTPException(400, "provide either a video file or a url")

    persona = persona if persona in ("funny", "candid", "surprising", "general") else "general"
    aspect = aspect if aspect in render.ASPECTS else "9:16"

    # The job id is minted here so the upload can be streamed to disk before the
    # worker thread starts. No id race, and no shared mutable closure state.
    job_id = uuid.uuid4().hex[:12]
    jobdir = _job_dir(job_id)
    src_dir = jobdir / "source"
    src_dir.mkdir(parents=True, exist_ok=True)

    # --- stage 1: acquire the source bytes, here on the event loop ---
    staged: Path
    if file is not None:
        name = Path(file.filename or "upload.mp4").name or "upload.mp4"
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            suffix = ".mp4"
        staged = src_dir / f"input{suffix}"
        size = 0
        try:
            with staged.open("wb") as fh:
                while chunk := await file.read(4 * 1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "file exceeds the 20 GB limit")
                    fh.write(chunk)
        finally:
            await file.close()
        if size == 0:
            staged.unlink(missing_ok=True)
            raise HTTPException(400, "uploaded file is empty")
        staged_note = f"uploaded {size / 1e6:.1f} MB"
    else:
        staged = src_dir / "pending.url"
        staged.write_text(url, encoding="utf-8")
        staged_note = "queued for download"

    # --- stage 2: the real work, on a worker thread ---
    def work(progress: Callable[[int, str], None]) -> dict:
        if url:
            progress(1, "downloading source")
            src = pipeline.download(url, src_dir, cfg)
            staged.unlink(missing_ok=True)
        else:
            src = staged
        progress(2, f"source ready ({staged_note})")

        result = pipeline.run(
            src, jobdir / "work", jobdir / "clips",
            persona=persona, aspect=aspect, caption_style=caption_style,
            clip_length=clip_length,
            burn_captions=captions_on, reframe=reframe, auto_zoom=auto_zoom,
            music=music, auto_sfx=auto_sfx, auto_hook=auto_hook,
            use_llm=use_llm, use_graph=use_graph,
            cfg=cfg, progress=progress,
        )
        result["job_id"] = job_id
        result["options"] = {
            "persona": persona, "aspect": aspect,
            "caption_style": caption_style, "clip_length": clip_length,
            "captions": captions_on, "reframe": reframe,
            "auto_zoom": auto_zoom, "music": music, "auto_sfx": auto_sfx,
            "auto_hook": auto_hook,
            "use_llm": use_llm, "use_graph": use_graph,
            "max_clips": max_clips or cfg.max_clips,
        }
        (jobdir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    return manager.submit("clip", work, job_id=job_id).to_dict()


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": [j.to_dict() for j in manager.list()]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    if not manager.cancel(job_id):
        raise HTTPException(409, "job is not cancellable")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    if not manager.delete(job_id):
        raise HTTPException(404, "no such job")
    return {"ok": True}


# --------------------------------------------------------------------------
# artefacts
# --------------------------------------------------------------------------
@app.get("/api/jobs/{job_id}/artifact/{name}")
def artifact(job_id: str, name: str):
    base = _guard(job_id)
    if "/" in name or "\\" in name or ".." in name:
        # allow one nested level, e.g. work/transcript.json
        parts = name.split("/")
        if len(parts) > 2 or any(p in {"..", ""} for p in parts):
            raise HTTPException(400, "invalid artifact name")
    path = (base / name).resolve()
    if not str(path).startswith(str(base.resolve())) or not path.exists():
        raise HTTPException(404, "no such artifact")
    return FileResponse(path)


@app.get("/api/jobs/{job_id}/clips/{clip_id}.mp4")
def clip_video(job_id: str, clip_id: str):
    base = _guard(job_id)
    if not clip_id.startswith("m"):
        raise HTTPException(400, "invalid clip id")
    path = (base / "clips" / f"clip_{clip_id}.mp4").resolve()
    if not path.exists():
        raise HTTPException(404, "clip not rendered")
    return FileResponse(path, media_type="video/mp4",
                        headers={"Accept-Ranges": "bytes"})


@app.get("/api/jobs/{job_id}/source")
def job_source(job_id: str):
    base = _guard(job_id)
    src_dir = base / "source"
    files = [p for p in src_dir.iterdir() if p.is_file()] if src_dir.exists() else []
    if not files:
        raise HTTPException(404, "source not found")
    return FileResponse(files[0], media_type="application/octet-stream")


# --------------------------------------------------------------------------
# static
# --------------------------------------------------------------------------
_web = Path(__file__).resolve().parent / "web"
if _web.exists():
    app.mount("/", StaticFiles(directory=str(_web), html=True), name="web")


@app.exception_handler(pipeline.PipelineError)
def pipeline_error_handler(_request, exc: pipeline.PipelineError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})