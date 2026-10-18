#!/usr/bin/env bash
# ---------------------------------------------------------------------
#  Clipper -- one command, macOS and Linux.
#
#  Installs everything into a local virtualenv, checks that ffmpeg is
#  present, starts the engine and opens your browser at it. Nothing is
#  uploaded anywhere, nothing is paid for, no account is needed.
#
#      ./install.sh
#
#  Safe to run twice. If something goes wrong the script says what to do
#  next instead of printing a stack trace.
# ---------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; OFF=$'\033[0m'

echo
echo "  ${BOLD}Clipper${OFF}"
echo "  ======"
echo

fail() {
    echo "  ${RED}Setup did not finish.${OFF} Nothing outside this folder was changed."
    echo
    exit 1
}

# ---- 1. python --------------------------------------------------------------
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    echo "  ${RED}[x]${OFF} Python 3.10 or newer is required and was not found."
    echo
    echo "      macOS:  brew install python@3.12"
    echo "      Debian: sudo apt install python3.12 python3.12-venv"
    echo
    fail
fi
echo "  ${GRN}[ok]${OFF} $($PY -c 'import platform;print(platform.python_version())')"

# ---- 2. virtualenv ----------------------------------------------------------
if [ ! -x ".venv/bin/python" ]; then
    echo "  ${DIM}[..]${OFF} Creating a private environment in .venv"
    "$PY" -m venv .venv || fail
fi
VENV_PY="$PWD/.venv/bin/python"
echo "  ${GRN}[ok]${OFF} Environment ready"

# ---- 3. dependencies --------------------------------------------------------
echo "  ${DIM}[..]${OFF} Installing dependencies. First run takes a minute or two."
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r requirements.txt || fail
"$VENV_PY" -m pip install --quiet faster-whisper "av<19" \
    "opencv-python-headless<5" yt-dlp 2>&1 | grep -v "already satisfied" || true
echo "  ${GRN}[ok]${OFF} Dependencies installed"

# ---- 4. ffmpeg --------------------------------------------------------------
if "$VENV_PY" -c "import sys;from momentclip import ffmpeg;from momentclip.config import get_config;sys.exit(0 if ffmpeg.available(get_config()) else 1)" 2>/dev/null; then
    echo "  ${GRN}[ok]${OFF} ffmpeg found"
else
    echo "  ${YLW}[!]${OFF} ffmpeg was not found. Clipper needs it to cut and stitch video."
    echo
    echo "      macOS:  brew install ffmpeg"
    echo "      Debian: sudo apt install ffmpeg"
    echo
    echo "      ...then run ./install.sh again. Nothing else is missing."
    echo
fi

# ---- 5. go ------------------------------------------------------------------
echo "  ${GRN}[ok]${OFF} Starting Clipper. Your browser will open in a moment."
echo
echo "      Press Ctrl+C to stop it."
echo
exec "$VENV_PY" run.py "$@"