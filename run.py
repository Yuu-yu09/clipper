#!/usr/bin/env python
"""Entry point.  python run.py [--port 8000] [--host 127.0.0.1]"""

from __future__ import annotations

import argparse
import sys
import webbrowser


def _port_owner(host: str, port: int) -> str:
    """Describe whatever is already listening on host:port, or "" if free.

    Works without psutil and without shelling out to netstat/lsof, because the
    whole point is to produce a useful message on a machine where the user may
    not have those tools.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # A connect, not a bind: SO_REUSEADDR would let us bind a port that is
        # already listening on Windows, which is exactly the bug we are
        # guarding against.
        s.settimeout(1.0)
        if s.connect_ex((host, port)) != 0:
            return ""

    # Something is there. Try to name it, best effort.
    pid = ""
    try:
        if sys.platform == "win32":
            import subprocess
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"], capture_output=True,
                text=True, timeout=10, encoding="utf-8", errors="replace")
            for line in (out.stdout or "").splitlines():
                if f":{port}" in line and "LISTENING" in line.upper():
                    pid = line.split()[-1].strip()
                    break
        else:
            import subprocess
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}"],
                                 capture_output=True, text=True, timeout=10)
            pid = (out.stdout or "").strip().splitlines()[0].strip() \
                if (out.stdout or "").strip() else ""
    except Exception:  # noqa: BLE001
        pid = ""

    if pid:
        cmd = ""
        try:
            import subprocess
            if sys.platform == "win32":
                r = subprocess.run(["wmic", "process", "where",
                                    f"ProcessId={pid}", "get", "CommandLine"],
                                   capture_output=True, text=True, timeout=10,
                                   encoding="utf-8", errors="replace")
                lines = [l.strip() for l in (r.stdout or "").splitlines()[1:]
                         if l.strip()]
                cmd = lines[0] if lines else ""
            else:
                r = subprocess.run(["ps", "-p", pid, "-o", "args="],
                                   capture_output=True, text=True, timeout=10)
                cmd = (r.stdout or "").strip()
        except Exception:  # noqa: BLE001
            cmd = ""
        if cmd:
            return f"pid {pid} — {cmd[:90]}"
        return f"pid {pid}"
    return "an unidentified process"


def busy_hint(owner: str) -> str:
    """A copy-pasteable way to stop the process described by _port_owner()."""
    parts = owner.split()
    if len(parts) >= 2 and parts[0] == "pid":
        pid = parts[1]
        if sys.platform == "win32":
            return f"taskkill /PID {pid} /F"
        return f"kill {pid}"
    return "# find and stop the process listening on that port"


def main() -> int:
    ap = argparse.ArgumentParser(description="Clipper — local video moment finder")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="verify the environment and exit")
    args = ap.parse_args()

    from momentclip.config import get_config
    from momentclip import asr, ffmpeg, momentgraph, render

    cfg = get_config()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port

    caps = {
        "ffmpeg": ffmpeg.available(cfg),
        "ffmpeg version": ffmpeg.version(),
        "libass (burn captions)": ffmpeg.has_libass(cfg),
        "transcription": asr.capability(),
        "opencv (face tracking)": render._load_cv2() is not False,
        "graph backend": "graphify" if momentgraph.graphify_available() else "networkx",
        "llm judge": cfg.llm_model or "not configured",
        "data dir": str(cfg.data_dir),
        "watermark": False,
    }

    print("\n  Clipper — environment check\n  " + "-" * 44)
    for k, v in caps.items():
        if k == "watermark":
            print(f"  {k:<26} {'never — guaranteed'}  ok ")
            continue
        mark = ""
        if isinstance(v, bool):
            mark = "ok " if v else "MISSING"
        elif isinstance(v, dict):
            mark = "ok " if any(v.values()) else "MISSING"
        print(f"  {k:<26} {v}  {mark}")
    print()

    if args.check:
        optional = {"opencv (face tracking)", "transcription", "llm judge"}
        hard = [k for k, v in caps.items()
                if isinstance(v, bool) and not v and k not in optional | {"watermark"}]
        if hard:
            print("  missing required: " + ", ".join(hard) + "\n")
        return 1 if hard else 0

    try:
        import uvicorn
    except ImportError:
        print("  uvicorn is missing. Install dependencies first:\n"
              "    pip install -r requirements.txt\n", file=sys.stderr)
        return 1

    url = f"http://{cfg.host}:{cfg.port}"

    # Check the port BEFORE announcing the URL and before opening a browser.
    #
    # This is not a nicety. uvicorn binds after we print, so if the port is
    # already taken the process used to print "starting on http://..." and
    # open your browser to whatever *old* server was listening, then exit with
    # a WinError 10048 buried in stderr. The result is that you appear to be
    # running the current code while a stale process answers every request --
    # which looks exactly like a broken app, and cost a full round of
    # misdiagnosis when it happened here.
    busy = _port_owner(cfg.host, cfg.port)
    if busy:
        print(f"\n  Port {cfg.port} is already in use.\n", file=sys.stderr)
        print(f"  Something is already listening there: {busy}\n", file=sys.stderr)
        print("  That process would answer every request, so you would be\n"
              "  testing old code while believing you had restarted.\n",
              file=sys.stderr)
        print(f"  Stop it and start again:\n\n"
              f"    {busy_hint(busy)}\n", file=sys.stderr)
        print("  Or run on a different port:\n\n"
              f"    python run.py --port {cfg.port + 1}\n", file=sys.stderr)
        return 1

    print(f"  starting on {url}\n")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    uvicorn.run("momentclip.server:app", host=cfg.host, port=cfg.port,
                log_level="info", reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())