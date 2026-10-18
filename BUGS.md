# Bug list — real user E2E, 2026-10-18

Found by driving the running engine over real HTTP with real video, and by
exercising the deployed UI in a real browser. Listed before fixing, per
process. Each fix is its own commit.

| # | Bug | Evidence | Status |
|---|---|---|---|
| B1 | Story mode reports a bare HTTP phrase (`Method Not Allowed`) instead of explaining that no engine is configured | `POST` to the static site → 404/405; `planSeason` threw `data.detail \|\| HTTP <status>` | fixed |
| B2 | `graphify` is not on PyPI, so the "Graphify as the brain" integration is dead code that can never activate | `pip index versions graphify` → *No matching distribution found* | fixed |
| B3 | No community-detection library installed, yet the docs claim Leiden clustering. `_cluster()` silently fell back to greedy modularity | `leidenalg`, `python-louvain`, `igraph` all missing; `graph_backend: networkx` | fixed |
| B4 | Duplicate edge insertion in the Graphify clustering path | `momentgraph.py:76-77` — `H.add_edges_from(G.edges())` twice | fixed |
| B5 | `run.py` prints "starting on …" and opens the browser before uvicorn binds, so an occupied port produces a silent death with `WinError 10048` in stderr | observed live: a stale 16:42 server answered health checks while `run.py` had already exited | fixed |
| B6 | No HTTP-level test coverage; every suite called Python directly, missing multipart upload, CORS, polling, the static mount and 404 paths | `tools/e2e_http.py` added | fixed |
| B7 | Analysis artefacts 404: the pipeline writes `work/*.json` but the documented and UI-requested URL was `artifact/<name>` | every job: `artifact/graph.json` → 404 while `work/graph.json` existed on disk | fixed |
| B8 | The caption download link is dead: `.srt` sidecars are stored as `clips/clip_m0000.srt` but requested as `clips/m0000.srt`, and the route used `{name}` which Starlette refuses to match against any name containing a slash | `artifact/clips/m0000.srt` → **no route match at all**; file existed on disk | fixed |
| B9 | A dropped poll killed the run: one failed request ended a multi-minute job with "Failed to fetch" while the work continued server-side | reported from the UI during "writing 10 episodes" | fixed |
| B10 | The E2E fixture contained no speech, so transcription returned zero words and every caption/quote assertion was vacuously passing | `fixtures/test_podcast.mp4` → `backend: "none"`, 0 utterances; faster-whisper logged *"VAD filter removed 01:18.352 of audio"* | fixed |
| B11 | Two bugs of the B1 class survived in clip mode and were missed the first time: the job-start call site used the same bare-status throw, and both polling loops had the B9 flaw | found while fixing B1; `app.js:408`, `poll()`, `sPoll()` | fixed |

## Checked, deliberately not bugs

- `\k` and `\t` in the caption meta are intentional — they document the ASS
  override codes used for karaoke timing and scale pops.
- Seven caption cards against six presets is the deliberate "None" option.
- `graph.json` reporting zero nodes to a naive reader: the moments are at the
  top level and the node/edge tallies are under `summary`. That was a bug in
  my test, not in the code.

## Process note

B5 is the one that mattered most. It made a *stale process* answer as though
it were the current code, which produced failures that looked like defects in
story mode and cost a full round of misdiagnosis. Anything that lets the app
lie about which code is running, or which process is serving, will do the same
thing again.

B10 is the second most instructive. The suite had been green while never once
exercising captions, because the fixture was silent and the assertions did not
notice. A test that cannot fail is worse than no test.