@echo off
REM ---------------------------------------------------------------------
REM  Clipper -- one command, Windows.
REM
REM  Installs everything into a local virtualenv, checks that ffmpeg is
REM  present, starts the engine and opens your browser at it. Nothing is
REM  uploaded anywhere, nothing is paid for, no account is needed.
REM
REM      install.bat
REM
REM  It is safe to run twice. If anything goes wrong the script stops and
REM  tells you exactly what to do next rather than printing a stack trace.
REM ---------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   Clipper
echo   ======
echo.

REM ---- 1. find python -------------------------------------------------------
set "PY="
for %%C in (py python python3) do (
    if not defined PY (
        %%C -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
        if !errorlevel! equ 0 set "PY=%%C"
    )
)
if not defined PY (
    echo   [x] Python 3.10 or newer is required and was not found.
    echo.
    echo       winget install Python.Python.3.12
    echo.
    goto :fail
)
for /f "tokens=2" %%V in ('%PY% -c "import sys;print(sys.version)"') do set "PYVER=%%V"
echo   [ok] Python !PYVER!

REM ---- 2. virtualenv --------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo   [..] Creating a private environment in .venv
    %PY% -m venv .venv >nul 2>&1
    if errorlevel 1 (
        echo   [x] Could not create the virtualenv.
        goto :fail
    )
)
set "VENV_PY=%CD%\.venv\Scripts\python.exe"
echo   [ok] Environment ready

REM ---- 3. dependencies ------------------------------------------------------
echo   [..] Installing dependencies. First run takes a minute or two.
"%VENV_PY%" -m pip install --quiet --upgrade pip >nul 2>&1
"%VENV_PY%" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo   [x] Installing dependencies failed.
    goto :fail
)
"%VENV_PY%" -m pip install --quiet faster-whisper "av<19" "opencv-python-headless<5" yt-dlp 2>&1 | findstr /V "Requirement already satisfied"
echo   [ok] Dependencies installed

REM ---- 4. ffmpeg ------------------------------------------------------------
"%VENV_PY%" -c "import sys;from momentclip import ffmpeg;from momentclip.config import get_config;sys.exit(0 if ffmpeg.available(get_config()) else 1)" >nul 2>&1
if errorlevel 1 (
    echo   [!] ffmpeg was not found. Clipper needs it to cut and stitch video.
    echo.
    echo       winget install Gyan.FFmpeg
    echo.
    echo       ...then close this window, open a new one, and run install.bat
    echo       again. Nothing else is missing.
    echo.
) else (
    echo   [ok] ffmpeg found
)

REM ---- 5. go ----------------------------------------------------------------
echo   [ok] Starting Clipper. Your browser will open in a moment.
echo.
echo       Press Ctrl+C to stop it.
echo.
"%VENV_PY%" run.py %*
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo.
    echo   Clipper stopped with exit code %RC%.
    goto :fail
)
goto :eof

:fail
echo   Setup did not finish. Nothing has been changed outside this folder.
echo.
pause
exit /b 1