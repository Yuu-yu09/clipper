#!/usr/bin/env python
"""End-to-end test over real HTTP against a running engine.

Every other suite calls the Python functions directly. That misses the whole
seam where things actually break: request parsing, CORS, multipart upload,
job polling, and the static-file mount that sits in front of everything.

This drives the same endpoints the browser drives, with a real video file,
and asserts the responses a real user would care about.

    python tools/e2e_http.py [--base http://127.0.0.1:8000] [--video PATH]

Run it against a server you started yourself, so a stale process from a
previous session cannot quietly answer for the current code.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Result:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        self.rows.append((bool(ok), name, detail))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if detail:
            line += f"  {detail}"
        print(line, flush=True)
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [n for ok, n, _ in self.rows if not ok]


def get(base: str, path: str, timeout: int = 60) -> tuple[int, bytes, str]:
    """Returns (status, body, content-type). Never raises for HTTP errors."""
    req = urllib.request.Request(base.rstrip("/") + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode(), "connection-error"


def post_json(base: str, path: str, payload: dict, timeout: int = 180
              ) -> tuple[int, bytes]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode()


def post_multipart(base: str, path: str, fields: dict, video: Path | None,
                   timeout: int = 600) -> tuple[int, bytes]:
    """Hand-rolled multipart so this works on any Python, any PowerShell."""
    boundary = f"----clipper{uuid.uuid4().hex}"
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        out += f"{v}\r\n".encode()
    if video is not None:
        ctype = mimetypes.guess_type(video.name)[0] or "application/octet-stream"
        out += f"--{boundary}\r\n".encode()
        out += (f'Content-Disposition: form-data; name="file"; '
                f'filename="{video.name}"\r\n').encode()
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += video.read_bytes()
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        base.rstrip("/") + path, data=bytes(out),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--video", default=str(ROOT / "fixtures" / "test_podcast.mp4"))
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()
    base = a.base.rstrip("/")
    R = Result()
    video = Path(a.video)

    print(f"\n=== E2E over HTTP against {base} ===\n")

    # ---------------------------------------------------------- 1. health
    st, body, _ = get(base, "/api/health", timeout=30)
    if not R.check(st == 200, "GET /api/health", f"status={st}"):
        print("\n  engine is not answering; start it with `python run.py`")
        return 1
    health = json.loads(body)
    R.check(health.get("ok") is True, "health.ok")
    R.check(health.get("can_process") is True, "engine can process")
    R.check(health.get("watermark") is False, "watermark guarantee advertised")
    R.check(health.get("serverless") is False, "not serverless")
    print(f"       graph_backend={health.get('graph_backend')} "
          f"llm={health.get('llm_configured')} "
          f"whisper={health.get('transcription', {}).get('faster_whisper')}")

    # ------------------------------------------------- 2. the static UI
    st, body, ctype = get(base, "/")
    R.check(st == 200, "GET / serves the UI", f"{st} {ctype}")
    st, body, _ = get(base, "/app.js")
    R.check(st == 200 and b"function" in body, "GET /app.js", f"{st}")

    # ------------------------------------------------- 3. story metadata
    st, body, _ = get(base, "/api/story/roles", timeout=30)
    ok = st == 200
    roles = json.loads(body).get("roles", []) if ok else []
    R.check(ok and len(roles) > 0, "GET /api/story/roles",
            f"status={st} roles={len(roles)}")

    # ------------------------------------------------- 4. validation
    st, body = post_json(base, "/api/story/plan", {"premise": "", "characters": []})
    R.check(st == 400, "empty premise rejected with 400", f"got {st}")

    st, body = post_json(base, "/api/jobs", {})
    R.check(st in (400, 422), "job with no file/url rejected", f"got {st}")

    # ------------------------------------------------- 5. the real job
    if not video.exists():
        R.check(False, "fixture video exists", str(video))
        print(f"\n  {len(R.failed)} FAILURES: " + "; ".join(R.failed))
        return 1

    size = video.stat().st_size / 1e6
    print(f"\n  uploading {video.name} ({size:.1f} MB) …", flush=True)
    t0 = time.time()
    st, body = post_multipart(base, "/api/jobs", {
        "persona": "general", "aspect": "9:16", "clip_length": "15-60",
        "caption_style": "pop", "music": "auto",
        "reframe": "true", "autozoom": "true", "sfx": "true",
        "usegraph": "true", "usellm": "true",
    }, video)
    if not R.check(st == 200, "POST /api/jobs accepted", f"status={st} {body[:180]!r}"):
        print(f"\n  {len(R.failed)} FAILURES: " + "; ".join(R.failed))
        return 1
    job = json.loads(body)
    jid = job["id"]
    print(f"       job {jid}")

    # ------------------------------------------------- 6. poll to completion
    deadline = time.time() + a.timeout
    last = ""
    final = None
    while time.time() < deadline:
        time.sleep(4)
        st, body, _ = get(base, f"/api/jobs/{jid}", timeout=60)
        if st != 200:
            R.check(False, "GET /api/jobs/{id} during run", f"status={st}")
            break
        s = json.loads(body)
        line = f"       [{s.get('progress', 0):3}%] {s.get('status')} {s.get('step')}"
        if line != last:
            print(line, flush=True)
            last = line
        if s.get("status") in ("done", "error", "failed"):
            final = s
            break

    if final is None:
        R.check(False, "job finished", f"still running after {a.timeout}s")
    else:
        R.check(final.get("status") == "done", "job completed",
                f"status={final.get('status')} error={str(final.get('error'))[:200]}")

    # ------------------------------------------------- 7. the results
        # Read the transcript FIRST. Whether the source yielded words decides
        # whether a missing .srt sidecar is a bug or simply the correct result
        # for a silent input -- and the first fixture this ran against was
        # silent, which made the caption assertions meaningless.
        has_words = False
        st, body, _ = get(base, f"/api/jobs/{jid}/artifact/transcript.json", timeout=60)
        if st == 200:
            t = json.loads(body)
            utts = t.get("utterances") or []
            words = sum(len(u.get("words") or []) for u in utts)
            has_words = words > 0
            R.check(has_words, "transcript has words",
                    f"backend={t.get('backend')} utterances={len(utts)} words={words}")
            if not has_words:
                print("       WARNING: no speech detected, so captions, quotes "
                      "and transcript-driven moments were NOT exercised")
    if final and final.get("status") == "done":
        res = final.get("result") or {}
        clips = res.get("clips") or []
        R.check(len(clips) > 0, "produced clips", f"{len(clips)} clips")
        print(f"       {len(clips)} clips in {time.time() - t0:.0f}s")

        for i, c in enumerate(clips[:3]):
            cid = c.get("id")
            st, body, ctype = get(base, f"/api/jobs/{jid}/clips/{cid}.mp4", timeout=180)
            R.check(st == 200 and len(body) > 50_000, f"clip {cid} streams",
                    f"status={st} {len(body)}B {ctype}")
            R.check(b"OpusClip" not in body[:4096] and b"opus.pro" not in body[:4096],
                    f"clip {cid} free of vendor strings")

            st, body, _ = get(base, f"/api/jobs/{jid}/artifact/clips/{cid}.srt",
                              timeout=60)
            if has_words:
                R.check(st == 200 and b"-->" in body, f"clip {cid} has an srt sidecar",
                        f"status={st}")
            else:
                print(f"       skip  clip {cid} srt sidecar "
                      f"(source produced no words, so none is written)")

        for art in ("transcript.json", "graph.json", "signals.json"):
            st, body, _ = get(base, f"/api/jobs/{jid}/artifact/{art}", timeout=60)
            R.check(st == 200, f"artifact {art}", f"status={st}")

        # the graph must be real, not an empty shell. NB: graph.json carries the
        # moments at the top level; the node/edge tallies live under "summary",
        # so reading g["nodes"] here silently reports zero and looks like a
        # broken graph when the graph is fine.
        st, body, _ = get(base, f"/api/jobs/{jid}/artifact/graph.json", timeout=60)
        if st == 200:
            try:
                g = json.loads(body)
                summary = g.get("summary") or {}
                nodes = summary.get("nodes", 0)
                edges = summary.get("edges", 0)
                moments = len(g.get("moments") or [])
                R.check(moments > 0, "moment graph has moments", f"moments={moments}")
                R.check(nodes > 0, "moment graph has nodes", f"nodes={nodes}")
                R.check(edges > 0, "moment graph has edges", f"edges={edges}")
                print(f"       graph: {moments} moments, {nodes} nodes, {edges} edges, "
                      f"backend={summary.get('backend')}, "
                      f"communities={summary.get('communities')}")
            except json.JSONDecodeError:
                R.check(False, "graph.json parses as JSON")

    # ------------------------------------------------- 8. 404 handling
    st, _, _ = get(base, "/api/jobs/definitely-not-a-job", timeout=30)
    R.check(st == 404, "unknown job id gives 404, not 500", f"got {st}")

    st, body = post_json(base, "/api/story/render", {"story_id": "nope"})
    R.check(st in (400, 404), "unknown story id rejected", f"got {st}")

    # ------------------------------------------------- summary
    print()
    passed = sum(1 for ok, _, _ in R.rows if ok)
    if R.failed:
        print(f"  {len(R.failed)} FAILURES of {len(R.rows)} checks "
              f"({passed} passed)")
        for f in R.failed:
            print(f"    - {f}")
        return 1
    print(f"  all {len(R.rows)} HTTP checks passed in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())