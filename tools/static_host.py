"""Static-host emulator for testing the UI without an engine.

Serves momentclip/web exactly as a CDN would, and answers /api/ the way a
static host does -- 404 on GET, 405 on POST. That is the exact situation the
user hit: a working interface with no backend behind it.

    python tools/static_host.py [port]
"""

from __future__ import annotations

import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "momentclip" / "web"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8811


class Handler(SimpleHTTPRequestHandler):
    """A CDN, not an engine."""

    def log_message(self, *args) -> None:  # keep the test output readable
        pass

    def _no_backend(self) -> None:
        body = (b"The page could not be found\nNOT_FOUND")
        self.send_response(405 if self.command == "POST" else 404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?")[0].startswith("/api/"):
            self._no_backend()
            return
        super().do_GET()

    def do_POST(self) -> None:
        self._no_backend()

    def translate_path(self, path: str) -> str:
        rel = path.split("?", 1)[0].split("#", 1)[0].lstrip("/") or "index.html"
        return str(ROOT / rel)


if __name__ == "__main__":
    os.chdir(ROOT)
    print(f"static host on http://127.0.0.1:{PORT} serving {ROOT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()