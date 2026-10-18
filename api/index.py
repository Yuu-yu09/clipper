"""Vercel FastAPI entrypoint.

Vercel's Python preset looks for an ASGI app in a default location and its
autodetector reports this file as "a potential entrypoint" it cannot use. This
wrapper puts the app where it looks.

Read the DEPLOYMENT.md note before expecting video processing to work here:
this makes the deployment BUILD, not make it able to clip video. Serverless
functions have no ffmpeg, no persistent disk and a short execution ceiling,
which is exactly what the engine needs and exactly what they do not have.
"""

from momentclip.server import app

__all__ = ["app"]