"""Clipper — free, open-source, local video clipper.

Finds funny, candid and surprising moments in long video and renders short
vertical clips. No watermark, no subscription, no upload. Your video stays
on your machine.

Quickstart:
    python run.py
    open http://127.0.0.1:8000
"""

__version__ = "0.1.0"
__all__ = [
    "asr", "candidates", "captions", "config", "ffmpeg", "jobs",
    "momentgraph", "pipeline", "rank", "render", "signals",
]