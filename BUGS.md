# Bug list — real user E2E, 2026-10-18

Found by driving the running engine over real HTTP with a real 11 MB video,
and by exercising the deployed UI in a real browser. Listed before fixing, per
process. Each fix is its own commit.

| # | Bug | Evidence | Status |
|---|---|---|---|
| B1 | Story mode reports a bare HTTP phrase (`Method Not Allowed`) instead of explaining that no engine is configured | `POST https://clipper-puce.vercel.app/api/story/plan` → 404/405; `planSeason` throws `data.detail \|\| HTTP <status>` | fixed |
| B2 | `graphify` is not on PyPI, so the "Graphify as the brain" integration is dead code that can never activate | `pip index versions graphify` → *No matching distribution found* | fixed |
| B3 | No community-detection library installed, yet the docs claim Leiden clustering. `_cluster()` silently falls back to greedy modularity | `leidenalg`, `python-louvain`, `igraph` all missing; `graph_backend: networkx` reported | fixed |
| B4 | Duplicate edge insertion in the Graphify clustering path | `momentgraph.py:76-77` — `H.add_edges_from(G.edges())` twice | fixed |
| B5 | `run.py` prints "starting on …" and opens the browser before uvicorn binds, so an occupied port produces a silent death with `WinError 10048` in stderr | observed live: a stale 16:42 server answered health checks while `run.py` had already exited | fixed |
| B6 | No HTTP-level test coverage; every suite called Python directly, missing multipart upload, CORS, polling, the static mount and 404 paths | `tools/e2e_http.py` added | fixed |

## Checked, deliberately not bugs

- `\k` and `\t` rendered in the caption meta are intentional — they document the
  ASS override codes used for karaoke timing and scale pops.
- Seven caption cards against six presets is the deliberate "None" option.

## Process note

B5 is the one that matters most. It made a *stale process* answer as though it
were the current code, which produced failures that looked like defects in
story mode and cost a full round of misdiagnosis. Anything that lets the app
lie about which code is running, or which process is serving, will do the same
thing again.