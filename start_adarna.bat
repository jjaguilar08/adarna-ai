@echo off
rem Double-click launcher for Windows: starts the whole app (wsl_app in
rem WSL, then windows_app's GUI) with one click, instead of needing an
rem open WSL terminal to run start_app.sh directly.
rem
rem This is a thin wrapper, not a reimplementation -- it just runs the
rem existing, already-working start_app.sh inside WSL, which already
rem handles starting wsl_app, waiting for it to be ready, launching
rem windows_app via the established WSL->Windows interop, and stopping
rem wsl_app automatically when windows_app closes (see start_app.sh's own
rem comments). Keeping all of that logic in one script (not duplicated
rem here in batch) means this launcher can never drift out of sync with
rem how the app actually starts.
rem
rem A Desktop shortcut pointing at this file is the real "double-click to
rem open" entry point -- see docs/DEV_PLAN.md's Day 28 entry for how it
rem was created.

title Adarna
echo Starting Adarna (wsl_app + windows_app)...
echo.

wsl.exe -d Ubuntu-22.04 bash -lc "cd /home/jon/projects/adarna-ai && ./start_app.sh"

echo.
echo Adarna has exited.
pause
