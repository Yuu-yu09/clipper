# Deploying

## Short version

The **interface** deploys to Vercel fine. The **engine** cannot, and this is
not a configuration problem.

```
Vercel        momentclip/web/        static UI      ✅ deploys, loads, works
Vercel        api/index.py           FastAPI        ⚠️  builds, but cannot clip
Your machine  python run.py          the engine     ✅ this is what does the work
```

## Why the engine can't run on Vercel

Vercel functions are request-scoped containers. The engine needs four things
they do not have:

| Needs | Vercel gives |
|---|---|
| `ffmpeg`, `ffprobe`, `yt-dlp` binaries | no arbitrary binaries in the runtime |
| `data/jobs/<id>/…` that survives between requests | ephemeral filesystem |
| minutes of uninterrupted CPU per render | a short execution ceiling |
| `opencv` + `founder faster-whisper` weights (~500 MB+) | can't install or download per invocation |

Deploying anyway works — the build succeeds and the page loads. Clipper detects
the environment and tells you plainly that it cannot process video, rather than
accepting your upload and failing three minutes in. `GET /api/health` reports
`"serverless": true, "can_process": false`.

## Deploying the UI

```powershell
npm i -g vercel
vercel            # from this directory
```

Or use the deploy button after adding one to the README.

The UI reads the engine location from `window.CLIPPER_API` in
`momentclip/web/config.js`. **That is the one file you edit** for a hosted
deployment:

```js
window.CLIPPER_API = "https://clipper-engine.example.com";
```

Or leave it empty and let each user set it from the **⚙ engine** button, which
saves to `localStorage`. Handy when the engine runs on someone's own machine —
which, given nothing uploads anywhere, is the normal case.

The engine already sends `Access-Control-Allow-Origin: *`, so the UI and the
engine do not need to be on the same host.

## Running the engine somewhere real

Anything with a filesystem and ffmpeg. In rough order of how much work it is:

**Your own machine** — `python run.py`. Then either open
`http://127.0.0.1:8000`, or expose it with a tunnel:

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8000
```

That gives a public HTTPS URL pointing at your machine, no port forwarding.
Good for a demo; note the machine is now doing the work.

**A small VPS** — Hetzner, DigitalOcean, Vultr. Cheapest thing with predictable
performance. Dockerise it and it is a few lines.

**Railway / Render** — both accept a Dockerfile, both give you ffmpeg-capable
containers with a disk. Slower to cold-start than a VPS.

**Hugging Face Spaces (Docker)** — free CPU tier, ships ffmpeg, and suits a
demo. Expect it to be slow.

## The honest alternative

A fully client-side build — `ffmpeg.wasm` for cutting and `whisper.cpp` in WASM
for transcription — would genuinely run as static files on Vercel, with no
backend at all. That is a real option and it would work. It is also a
substantial rewrite, capped at much smaller files, and would cost you the
server-side signal pipeline this project is built around. Say the word if that
trade is worth it.