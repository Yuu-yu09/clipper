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

**Hosting it?** The interface is four static files with no build step, so it
deploys to Vercel, Netlify or GitHub Pages as-is. The engine needs ffmpeg and a
persistent disk, so it runs on a machine or container rather than on a
serverless function. The two are configured independently — see
**[DEPLOYMENT.md](DEPLOYMENT.md)**.

---

## The look

Built to match what people actually ship on short-form, using the same ideas as
OpusClip (whose docs are the reference — see *Feature parity* below).

**Animated captions.** Not subtitles — word-by-word. Each word gets its own
`\k` karaoke timing, a colour change while it is being spoken, and a `\t` scale
pop as it lands. Six presets, including the karaoke look OpusClip ships as
`preset-fancy-Karaoke`:

| Preset | Look |
|---|---|
| `hormozi` | bold yellow, all caps, heavy stroke |
| `pop` | white with a bright yellow active word |
| `gradient` | soft green sweep, Montserrat |
| `boxed` | white on an opaque box |
| `clean` | restrained, thin stroke |
| `podcast` | 6 words a line, low margin |

3–4 words per line, broken on pauses and punctuation — not whole sentences.

**Auto-zoom that reacts.** A constant push-in reads as a still photo with a
slow crop — technically motion, visually dead. What makes short-form feel
*edited* is the frame moving **because something happened**:

- Baseline drift (`1.03 → 1.09` with a face, `1.01 → 1.05` without)
- A **punch-in** at each strong reaction — a laugh, an energy shock, a pitch
  surge — up to `1.18`, easing back down afterwards
- The crop **follows the speaker**, sampled at 5 points across the clip and
  interpolated, so it tracks someone who moves or cuts between two people

All one `crop` expression in the frame number `n` rather than a keyframes file:
`crop`'s `w`/`h` are evaluated at configure time, where `t` does not exist yet.

**Music that matches the vibe.** One global bed scores every clip identically,
which is exactly why it feels wrong — a calm explanation and a heated argument
end up on the same track. Per clip instead:

- **Mood from that clip's own signals** — 2+ laughs → `funny`, 3+ overlaps →
  `tense`, 4+ energy shocks → `upbeat`, flat delivery with pauses → `chill`
- **Key from the speaker's own pitch** — the clip's median F0 is snapped to
  equal temperament and used as the root, so the bed sits *in the key they are
  speaking in* rather than fighting it

Shown per clip: *"music: funny in C# — 2 laughter burst(s)"*. Still
synthesised, so no audio asset and nothing to license. The bed is
sidechain-compressed against the voice, so it ducks under speech and swells in
the gaps with no manual mixing.

Set music to a fixed mood to override, or drop your own tracks in
`assets/music/` and they take priority.

**Auto SFX.** Short comedy hits — brass stab, drum roll, pop, slide, womp —
placed automatically on the *same* detected peaks that scored each clip. At
most two per clip, never in the first two seconds, spaced apart. This is the
open-source equivalent of OpusClip's Auto SFX.

**Clip length bands**, mirroring OpusClip's dropdown:

`Auto` · `10–30s` · `15–60s` · `30–90s` · `1–3m`

**Hook headline** (opt-in, off by default — OpusClip also disables it by default
on API projects): a short hook pulled from the clip's own opening words, faded
over the first few seconds.

---

## Free-text prompts (the ClipAnything equivalent)

OpusClip's **ClipAnything** takes a natural-language prompt and runs it through
a cloud multimodal model. Clipper has the same feature with a different
mechanism, and it works with **no API key at all**.

Switch **Clip model** to *Anything (prompt)* and type what you want:

```
find the funniest moments
where they argue
the most surprising revelations
audience cheering and applause
the most quotable lines
```

The prompt is parsed along the same five axes OpusClip documents:

| Axis | Example | How it's matched |
|---|---|---|
| **Subject** | "the jaguar", "Patrick Mahomes" | transcript keywords, boosted by graph centrality |
| **Sentiment** | "funny", "awkward", "inspiring" | re-weights the signal sub-scores |
| **Sound** | "cheering", "arguing", "a horn" | re-weights the detected audio event classes |
| **Action** | "throws a touchdown" | treated as topics; signal events back them up |
| **Intent** | "compile **all** X" vs "**the** moment where X" | breadth vs single best, and clip count |
| **Visual** | "the woman in the yellow dress" | **not supported — no vision model** |

The UI shows you **exactly what it understood** before you run anything:
topics, mood, sound cues, intent, and any part of your prompt it can't honour.
You should never have to guess whether it parsed you the way you meant.

### Why it can't lie to you

The prompt **re-ranks moments that already exist**. It cannot invent a
timestamp. `test_prompt.py` asserts this directly: after applying a prompt,
every returned moment id must be one the pipeline actually found, and every
start/end must be byte-identical to before. Only scores move.

### What it genuinely can't do

- **Visual understanding.** "Find the jaguar killing the snake" works if they
  *say* it, not if they only *show* it. There is no vision model.
- **Clip length via prompt.** Use the Clip length control.
- **Audio/video editing via prompt.** Captions, music, zoom and aspect are
  their own controls.
- **Silent or wordless sources.** Without a transcript, spoken topics can't be
  matched — the prompt says so rather than pretending.

If you configure an LLM, your **actual prompt text** is passed to the judge
instead of a fixed persona label, so free-text prompts get a real semantic
reading on top of the deterministic compile.

---

## Story mode

The second tab. Give it a premise and a cast marked by role, and it writes a
**ten-episode season**, then assembles each episode.

**Planning** (`story.py`) produces a season bible — logline, theme, tone,
setting — then ten episodes that each have a *different job* in the arc:

```
1  The Ordinary Life      establishes the world and the want
2  Crossing the Line      the first irreversible act
3  The First Cost         you win something and lose something larger
4  False Victory          the setup pays off perfectly
5  The Betrayal           an ally becomes a liability
6  Midpoint Revelation    the truth that recontextualises everything
7  The Price of Everything consequences compound
8  Descent                the antagonist's plan works
9  The Turn               the wound and the want finally collide
10 Aftermath              resolution, and an epilogue that proves the change
```

Because each episode has a distinct function, the fallback season still reads
as a season rather than ten variations on the same idea — which matters,
because the fallback is what runs when no model is reachable. Roles are real
dramatic positions, not labels: protagonist, antagonist, mentor, ally, rival,
love interest, trickster. The prompt tells the model to reuse your names
verbatim, and every beat is resolved back to an actual cast member, so it
cannot quietly invent a fifth character.

**Assembly** (`assembly.py`) renders each beat as a shot: the character's image
(or a generated plate), Ken Burns motion, an animated caption of the beat text,
and a punch-in on high-tension beats — deaths, betrayals, revelations. Music
runs the whole episode, adapted per episode so the arc escalates, with
stingers on the tense beats.

### What this is not

This is a **motion storyboard**: the frame moves around a still, the
characters do not move. Real generative animation needs a video model
(Dreamina/Seedance, Kling, Runway), and those are either paid APIs or web-UIs
with no programmatic free tier — there is no free local option at any usable
quality. The visual slot is an interface on purpose: when a BytePlus key
exists, generated clips drop into the same place and nothing else changes.

---

## Using AI for free

Clipper runs fully without a model — the deterministic path is the default, not
a fallback. But if you want a model in the loop, these are genuinely free
options. All of them speak the OpenAI protocol, so it is one code path and
usually just a base URL.

| Provider | Cost | Card | Vision | Notes |
|---|---|---|---|---|
| **Ollama** (local) | unlimited | none | with a vision model | **Default.** Nothing leaves your machine |
| **Groq** | standing free tier | no | no | fastest hosted; ~30 rpm / 1000 day |
| **Google AI Studio** | standing free tier | no | **yes** | cheapest way to get vision |
| **OpenRouter** | `:free` models | no | yes | ~50 req/day until you top up once |
| OpenAI | **paid** | yes | yes | listed because it works, not because it's free |

New OpenAI accounts get a small **expiring** credit, then it is pay-per-token —
it is not free. Limits change often; check the provider's dashboard for what is
actually left today.

### Zero configuration

If a ready provider exists, Clipper finds it. With Ollama running and a model
pulled, nothing to configure:

```powershell
ollama serve                       # if not already running
ollama pull qwen2.5:7b             # or llama3.1:8b, gemma3:12b
```

`GET /api/health` reports what was found:

```json
{"providers": [
  {"key": "ollama", "status": "ready", "free": "unlimited · runs on your machine · private"},
  {"key": "groq",   "status": "needs GROQ_API_KEY"}
]}
```

### Forcing a provider

```powershell
$env:CLIPPER_LLM_PROVIDER = "groq"
$env:GROQ_API_KEY = "gsk_..."
```

Or point at anything OpenAI-compatible:

```powershell
$env:CLIPPER_LLM_BASE_URL = "http://localhost:11434/v1"
$env:CLIPPER_LLM_MODEL = "qwen2.5:7b"
```

### What the model is actually for

It **re-ranks**, it does not find. The heuristic + graph pass locates real
moments; the model orders them and explains why. A model can never make a clip
appear at a timestamp that is not in your video — `test_prompt.py` asserts
this.

If a model is unreachable, the run continues on the deterministic path and
logs why, rather than failing.

### Vision: matching what is *seen*

This is the one place a model genuinely adds something the signal processing
cannot do. `momentclip/vision.py` samples frames — evenly, plus densely around
every detected reaction — asks a multimodal model to describe each one, and
feeds the descriptions back into prompt matching.

That is what makes *"find the woman in the yellow dress"* work when nobody
says it. Without a vision provider the prompt compiler says so out loud rather
than pretending.

```powershell
pip install requests                     # nothing else needed
$env:GEMINI_API_KEY = "..."               # or CLIPPER_LLM_PROVIDER=ollama + a vision model
```

---

## Feature parity

Built against the [OpusClip API reference](https://help.opus.pro/api-reference/overview),
not against screenshots.

| OpusClip | Clipper | Notes |
|---|---|---|
| `curationPref.clipDurations: [[0, 180]]` | `clip_length` bands | Auto · 10–30s · 15–60s · 30–90s · 1–3m |
| `brandTemplateId: "preset-fancy-Karaoke"` | 6 caption presets | incl. an equivalent karaoke look |
| `renderPref.layoutAspectRatio` | `aspect` | 9:16 · 1:1 · 4:5 · 16:9 |
| Subject Tracking | auto-reframe + auto-zoom | Haar face track, median-centred |
| Auto SFX (Pro+) | `soundtrack.pick_stingers` | 5 synthesised hit types, placed on detected peaks |
| Add Music | `soundtrack.generate_bed` | synthesised, sidechain-ducked |
| `enableAutoHook` | `auto_hook` | **off by default here, same as theirs on API** |
| `range: {startSec, endSec}` | — | not implemented |
| `genre` | `persona` | funny · candid · surprising · general |
| `topicKeywords` / `customPrompt` | free-text prompts | deterministic compile + optional LLM judge |
| Virality Score | `rank.py` score | different method, and we show the breakdown |
| Watermark (lower plans) | **none** | structural guarantee, see `render.py` |
| — | caption style preview | see `GET /api/preview/caption.mp4` |

Clipper also has a mode OpusClip does not: **📖 Make a story** — give it a
premise and a cast, and it writes a ten-episode season (titles, loglines, beat
sheets) then assembles each episode as a motion storyboard with its own
soundtrack, captions and auto-zoom. See [Story mode](#story-mode).

**Not implemented**, and honestly so:

- **Visual understanding.** ClipAnything reasons over objects, actions and
  clothing. We have no vision model, so "find the jaguar killing the snake"
  only works if someone *says* it. The UI tells you when a prompt asks for
  something we can't honour.
- **Filler-word removal** (`enableRemoveFillerWords`). Our word-level ASR has
  the data for it; it just isn't wired up.
- **`range`** — clip only a time window of the source.
- **Social posting / scheduler / collections.** All cloud-account features.

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

Story mode:

```
POST /api/story/plan                     premise + cast -> a written season
POST /api/story/render                   assemble the planned episodes
GET  /api/story/roles                    role list, tones, default episode count
GET  /api/story/{id}/episode/{n}.mp4     stream one episode
GET  /api/story/{id}/season.md           the season as a markdown document
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
├─ render.py       cut, auto-zoom reframe, caption burn, music + SFX mix.
│                   no watermark. ever.
├─ captions.py     6 animated ASS presets, .srt sidecars, headline overlay
├─ soundtrack.py   adaptive per-clip music beds + comedy stingers (no audio assets)
├─ prompt.py       free-text prompt compiler (the ClipAnything equivalent)
├─ story.py        season planning: cast, arc functions, 10 episodes, beats
├─ assembly.py     storyboard assembly: Ken Burns, captions, zoom, score
├─ asr.py          faster-whisper with an HTTP fallback; never raises
├─ pipeline.py     the orchestration
├─ server.py       FastAPI
└─ web/            the UI (no build step)
tools/
├─ make_fixture.py  synthesises a tone video with known planted moments
├─ make_speech_fixture.ps1  real spoken audio via Windows TTS
├─ selftest.py      the detector must find those planted moments
├─ e2e_test.py      full pipeline, asserts clips render and stay watermark-free
├─ test_transcript.py     ASR, word alignment, karaoke captions
└─ test_render_features.py  zoom, caption presets, music, SFX
assets/
└─ music/        drop your own tracks here (optional)
```

---

## Tests

Four suites, each covering a layer that the others can't reach.

```powershell
# 1. signal detection — synthetic video with known planted moments
python tools\make_fixture.py
python tools\selftest.py

# 2. full pipeline on tones: graph, ranking, render, watermark guarantee
python tools\e2e_test.py

# 3. transcript path — needs REAL words, so it uses Windows TTS
powershell -File tools\make_speech_fixture.ps1
python tools\test_transcript.py
```

| Suite | Covers | Needs |
|---|---|---|
| `selftest.py` | 5 planted signal events must each be detected | `make_fixture.py` output |
| `e2e_test.py` | graph, ranking, render, 9:16 geometry, no overlay | fixture 1 |
| `test_transcript.py` | ASR, word-timestamp integrity, word-aligned cuts, karaoke `.ass`, burned captions | fixture 3 |
| `run.py --check` | reports which optional backends are live | — |

`selftest.py` is the important one: it plants a laughter burst, an energy shock,
a pitch sweep, a silent beat and an overlapping-voice section at known
timestamps, then asserts the detector finds each. Thresholds in `signals.py`
are tuned to keep it at 5/5 — if a tweak breaks the detector, this fails.

Fixture 3 exists because the tone fixture has **no recognisable speech by
design**, so whisper correctly reports "no speech" on it. Testing the ASR and
caption path needs actual words, hence TTS.

---

## Development

See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the commit workflow and the
checks to run before pushing. In short:

```powershell
python -m pyflakes momentclip run.py tools
python tools\selftest.py && python tools\test_transcript.py
python tools\test_prompt.py && python tools\test_render_features.py
python tools\e2e_test.py
```

---

## Licence

MIT.

Built for people who don't want to upload their video to a stranger's cloud to
find out where the funny bit is.