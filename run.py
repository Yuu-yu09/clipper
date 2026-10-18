#!/usr/bin/env python
"""Entry point.  python run.py [--port 8000] [--host 127.0.0.1]"""

from __future__ import annotations

import argparse
import sys
import webbrowser


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