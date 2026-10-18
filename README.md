# ✂️ Clipper

**Long video in. The funny, candid and surprising parts out.**
Free · open source · runs entirely on your machine · **no watermark, ever.**

Drop in a two-hour podcast. Get back ten vertical clips with burned-in captions,
ranked by how likely they are to actually get shared — and a visible breakdown of
*why* each one was chosen.

```
python run.py          →  http://127.0.0.1:8000
```

Your video never leaves your machine. No account, no subscription, no
processing-minute cap, no upload to anyone's cloud.

---

## The idea

Every other open-source clipper does the same thing: transcribe the whole video,
hand the transcript to one LLM prompt, cut wherever it says.

That approach **cannot find the best moments**, because the best moments aren't
in the transcript:

| Moment | Where it actually lives |
|---|---|
| The reaction shot after the punchline | the *previous* sentence ends it — the payoff is visual |
| The "ohhh" | a pitch spike + energy surge, not words |
| The beat of silence before a reveal | a gap in loudness |
| Genuinely candid unscripted talk | barge-in and overlap — text can't see it |
| Applause / big laugh | broadband noise, not speech |

So Clipper **finds** moments with signal processing and **ranks** them with a
model. In that order, never the reverse.

> The LLM is a **ranker over a shortlist**, never the **finder**.
> It can refine the ordering. It can never invent a clip that isn't in your video,
> and it can never miss one because the model got bored halfway through.

---

## How it works

```
 video in
    │
    ├─ ffmpeg      normalise, decode audio to 16 kHz mono
    │
    ├─ SIGNALS     per 0.25 s frame: loudness, zero-crossing, spectral centroid,
    │              spectral flatness, F0 (octave-corrected autocorrelation)
    │              → 5 event classes: laughter · energy_shock · pitch_surge
    │                            silence_beat · overlap
    ├─ ASR         faster-whisper, word-level timestamps  (transcript optional)
    ├─ VISION      OpenCV Haar face track + median-centred crop  → speaker-aware 9:16
    │
    ▼
 CANDIDATES    3 generators (sentence / event / exhaustive) merged and de-overlapped,
              snapped to word edges, filtered for complete thoughts
    │
    ▼
 ★ MOMENT GRAPH      nodes = moments + content terms · edges = shares_term · precedes · echoes
    │                Leiden communities → the episode's natural arcs (non-overlapping cuts)
    │                god nodes → recurring bits, running jokes, catchphrases
    │                cross-community edges → moments that bridge unrelated threads
    │                every edge tagged EXTRACTED or INFERRED, surfaced in the UI
    ▼
 RANKING       deterministic weighted score, then MMR diversity selection so you get
               ten different clips instead of ten variations of the same joke
    │
    ▼  ← optional LLM judge re-ranks the top ~24 only
 RENDER        ffmpeg cut → speaker-aware 9:16 crop → ASS karaoke captions → h264
              ★ nothing else is ever drawn on a frame
```

---

## Install

### 1. FFmpeg (required)

```powershell
winget install Gyan.FFmpeg        # Windows
brew install ffmpeg               # macOS
sudo apt install ffmpeg           # Debian/Ubuntu
```

Clipper also auto-discovers FFmpeg inside the Windows package store if the
`winget` command alias doesn't resolve. Or set `FFMPEG_BIN` / `FFPROBE_BIN`.

### 2. Python deps

```powershell
python -m pip install -r requirements.txt
python -m pip install "av<19" faster-whisper opencv-python-headless yt-dlp
```

Only `fastapi`, `uvicorn`, `numpy` and `networkx` are strictly required.
Everything else is optional and degrades gracefully.

> **The `av<19` pin matters.** faster-whisper calls `av.open(metadata_errors=...)`,
> which PyAV removed in 19.0. Install the latest PyAV and transcription fails
> silently — you still get signal-only clips, just no transcript or captions.
> See `python run.py --check`, which reports the transcription backend.

### 3. Run

```powershell
python run.py --check      # verify the environment
python run.py              # start the server
```

---

## Graceful degradation

Clipper never hard-fails on a missing optional component. It tells you what it
lost and carries on.

| Missing | What happens |
|---|---|
| `faster-whisper` | no transcript — clips are still found from audio signals, no captions |
| `opencv` | reframing falls back to centre crop |
| `libass` in ffmpeg | no burned captions; `.srt` sidecars still written |
| Graphify | `networkx` computes the identical graph schema and structural features |
| LLM configured | heuristic ranking only |
| `yt-dlp` | upload tab still works, link tab disabled |
| ffmpeg | **hard requirement** — the app refuses to start |

---

## What you get for free

| | OpusClip | Clipper |
|---|---|---|
| Price | ~$19–29/mo | **free, forever** |
| Watermark | on free tier | **none, guaranteed** |
| Your video | uploaded to their cloud | **never leaves your disk** |
| Processing cap | yes | none |
| Ranking | proprietary model | your features, shown to you |
| Works offline | no | yes |

Runtime cost is roughly **$0** with everything local, against ~$0.36/hour of
video for cloud pipelines.

---

## Configuration

Copy `.env.example` and set what you need. Everything is optional.

An LLM judge is optional and only reorders an existing shortlist:

```powershell
$env:CLIPPER_LLM_BASE_URL = "http://localhost:11434/v1"   # Ollama
$env:CLIPPER_LLM_MODEL    = "llama3.1:8b"
```

Or fully offline with no key at all — leave it unset.

---

## API

```
GET  /api/health                      capabilities of this install
POST /api/jobs                        upload a file or pass a url
GET  /api/jobs/{id}                   progress and result
GET  /api/jobs/{id}/clips/{cid}.mp4   stream a rendered clip
GET  /api/jobs/{id}/artifact/{path}   transcript.json, graph.json, .srt, signals.json
POST /api/jobs/{id}/cancel
```

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/jobs `
  -F "file=@podcast.mp4" -F "persona=funny" -F "aspect=9:16"
```

---

## Project layout

```
momentclip/
├─ signals.py      feature extraction + event detection  ← the differentiator
├─ candidates.py   boundary generation, complete-thought filtering
├─ momentgraph.py  the graph; Graphify when present, networkx otherwise
├─ rank.py         deterministic scoring + optional LLM judge + MMR selection
├─ render.py       cut, speaker-aware reframe, caption burn. no watermark. ever.
├─ captions.py     .srt always, styled karaoke .ass when we have word timings
├─ asr.py          faster-whisper with an HTTP fallback; never raises
├─ pipeline.py     the orchestration
├─ server.py       FastAPI
└─ web/            the UI (no build step)
tools/
├─ make_fixture.py  synthesises a video with known planted moments
├─ selftest.py      checks the detector finds those planted moments
└─ e2e_test.py      full pipeline, asserts clips render and stay watermark-free
```

---

## Tests

```powershell
python tools\make_fixture.py    # build fixtures/test_podcast.mp4
python tools\selftest.py        # 5/5 planted signal events must be detected
python tools\e2e_test.py        # full pipeline, 0 failures expected
```

`selftest.py` plants a laughter burst, an energy shock, a pitch sweep, a silent
beat and an overlapping-voice section at known timestamps, then asserts the
detector finds each one. If a threshold change breaks the detector, this fails.

---

## Licence

MIT.

Built for people who don't want to upload their video to a stranger's cloud to
find out where the funny bit is.