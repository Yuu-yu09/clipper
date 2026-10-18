## Deploying

## Short version

**The whole deployment is the interface, and it is static.** There is no
Python function on Vercel, so nothing there can fail to find an entrypoint,
install dependencies, or run out of memory.

```
Vercel          momentclip/web/     static UI      ✅ the whole deployment
Your machine    python run.py        the engine     ✅ does the clipping
```

The interface loads on Vercel, then asks where the engine is. That is the
intended state, not an error — see "Without an engine configured" below.

## Why there is no serverless function here

An earlier version shipped `api/index.py` and tried to run the FastAPI engine
on Vercel. It failed with:

```
[FASTAPI_ENTRYPOINT_NOT_FOUND] No FastAPI entrypoint found in default
locations, but found potential entrypoints: momentclip/server.py
```

and, once that was resolved, would have failed anyway. A serverless function
has none of the four things the engine needs:

| Needs | Serverless gives |
|---|---|
| `ffmpeg`, `ffprobe`, `yt-dlp` binaries | no arbitrary binaries in the runtime |
| `data/jobs/<id>/…` surviving between requests | ephemeral filesystem |
| minutes of uninterrupted CPU per render | a short execution ceiling |
| `opencv` + faster-whisper weights (~500 MB+) | can't install or download per invocation |

So the function was removed rather than worked around. `pyproject.toml`
deliberately declares **no** `[tool.vercel] entrypoint`, which is what stops
Vercel's FastAPI autodetector from picking `momentclip/server.py` up again.

## Deploying the UI

```powershell
npm i -g vercel
vercel
```

or **Import** the repository in the Vercel dashboard. Two things to check:

- **Framework Preset: Other**
- **Root Directory:** repository root (leave blank)

`vercel.json` sets `outputDirectory` to `momentclip/web` and disables the build
command, so there is nothing to install. No Node, no bundler.

> If the build log mentions a commit that is not on `main`, Vercel is pointed
> at a different repository or an old revision. Check **Settings → Git** and
> redeploy. The commit for this code is on `main`.

## Pointing the interface at an engine

Either edit **`momentclip/web/config.js`** for the whole deployment:

```js
window.CLIPPER_API = "https://your-engine.example.com";
```

or let each user set it from the **⚙ engine** button, which saves to
`localStorage`. That is better for this project in practice: the engine runs on
a real machine, so the operator points their own browser at it and nothing is
uploaded anywhere.

The engine already sends `Access-Control-Allow-Origin: *`, so the interface
and the engine do not have to share a host.

## Running the engine somewhere real

Anything with a filesystem and ffmpeg, in rough order of effort:

**Your own machine** — `python run.py`, then either open
`http://127.0.0.1:8000` or expose it:

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8000
```

A public HTTPS URL with no port forwarding. Fine for a demo, but your machine
is now doing the work.

**A small VPS** — Hetzner, DigitalOcean, Vultr. Cheapest with predictable
performance.

**Railway / Render** — accept a Dockerfile and give you ffmpeg-capable
containers with a disk. Slower cold starts than a VPS.

**Hugging Face Spaces (Docker)** — free CPU tier and ships ffmpeg. Expect it
to be slow.

## Without an engine configured

If you deploy the interface and open it before an engine exists, it says so
and opens the engine panel with setup instructions. It does not accept an
upload it cannot finish.

## The honest alternative

A fully client-side build — `ffmpeg.wasm` for cutting and `whisper.cpp` in WASM
for transcription — would run as static files with no backend at all. That is a
real option and it would work on Vercel. It is also a substantial rewrite,
capped at much smaller files, and it would cost the server-side signal pipeline
this project is built around. Say the word if that trade is worth it.