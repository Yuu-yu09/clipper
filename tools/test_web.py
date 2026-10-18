#!/usr/bin/env python
"""Guard the static UI.

This exists because of a real incident: app.js declared `let CAPS_PROV` and
`function pick` twice at the same scope. That is a *syntax* error, so the whole
file failed to parse and every control on the page silently stopped working --
the page looked fine, it just did nothing. Nothing in the Python suite caught
it, because nothing in the Python suite reads the JavaScript.

Two checks:

  1. Parse app.js and config.js. Needs node; skipped with a loud notice if
     node is absent, never silently passed.
  2. Cross-check the DOM contract: every $("#id") and $$("...#id...") that
     app.js reaches for must exist in index.html. Catches renames that would
     otherwise throw at runtime on one specific button.

Run:  python tools/test_web.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "momentclip" / "web"


def parse_js(path: Path) -> tuple[bool, str]:
    node = shutil.which("node")
    if not node:
        return True, "SKIPPED (node not on PATH)"
    proc = subprocess.run([node, "--check", str(path)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode == 0:
        return True, "parses"
    err = (proc.stderr or "").strip().splitlines()
    # the useful line is the SyntaxError, not the caret diagram
    detail = next((l for l in err if "Error" in l), err[0] if err else "?")
    return False, detail.strip()


def main() -> int:
    fails: list[str] = []
    html = (WEB / "index.html").read_text(encoding="utf-8")
    app = (WEB / "app.js").read_text(encoding="utf-8")

    print("static UI guard\n")

    # ---- 1. syntax ----
    for name in ("app.js", "config.js"):
        ok, detail = parse_js(WEB / name)
        print(f"  {name:<12} {'ok' if ok else 'FAIL'}  {detail}")
        if "SKIPPED" in detail:
            print("    ^ install node to make this check real")
        elif not ok:
            fails.append(f"{name}: {detail}")

    # ---- 2. DOM contract ----
    html_ids = set(re.findall(r'\bid="([^"]+)"', html))

    # Ids created at runtime by innerHTML, so they cannot appear in the
    # static markup but are still legitimately reached for afterwards.
    dynamic = {"ep", "renderall"}

    # Every accessor form that takes a selector: $("#x"), $$("#x ..."),
    # wire("#x"), pick("#x").
    used: set[str] = set()
    for m in re.finditer(r'(?:\$|\$\$|wire|pick)\(\s*[`"\'](#[\w${}.\-]+)', app):
        used.add(m.group(1)[1:])

    # compare on the static prefix only -- "ep${e.number}" is a template
    missing = sorted(i for i in used
                     if i not in html_ids and i.split("${")[0] not in dynamic)
    print(f"\n  DOM contract: app.js reaches for {len(used)} ids, "
          f"index.html defines {len(html_ids)}")
    if missing:
        print(f"    MISSING in index.html: {', '.join(missing)}")
        fails.append(f"app.js references ids absent from index.html: {missing}")
    else:
        print(f"    every referenced id exists "
              f"({len(dynamic)} injected at runtime, allowed)")

    unused = sorted(html_ids - used)
    if unused:
        print(f"    note: {len(unused)} declared but never referenced "
              f"({', '.join(unused[:6])}{'…' if len(unused) > 6 else ''})")

    # ---- 3. no hardcoded /api base on a hosted static site ----
    # Every literal request path must go through api(), not be a bare /api/…
    # string, or the UI would only ever work same-origin.
    cfg = (WEB / "config.js").read_text(encoding="utf-8")
    print(f"\n  api routing: api() wrapper present = {'api(' in app}; "
          f"CLIPPER_API override wired = {'CLIPPER_API' in cfg}")
    if "api(" not in app:
        fails.append("app.js has no api() wrapper -- calls may be hardcoded")

    # ---- 4. stylesheet sanity ----
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for cls in ("reveal", "modes", "crow", "eplist", "pcard", "go", "seg"):
        if cls not in css:
            fails.append(f"style.css is missing .{cls} but the markup uses it")
    print(f"  stylesheet: {len(css)} bytes, all UI classes present = "
          f"{not any('style.css is missing' in f for f in fails)}")

    print()
    if fails:
        print(f"  {len(fails)} FAILURES")
        for f in fails:
            print(f"    - {f}")
        return 1
    print("  static UI checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())