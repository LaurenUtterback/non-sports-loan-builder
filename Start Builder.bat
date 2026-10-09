@echo off
title South River Builder Launcher
cd /d "%~dp0"
echo ============================================================
echo   South River - Credit Memo / Participation Agreement Builder
echo ============================================================
echo.
echo Starting the backend and frontend (two minimized windows)...
start "SRC Backend"  /min cmd /k "cd backend && .venv\Scripts\python.exe run_local.py"
start "SRC Frontend" /min cmd /k "cd frontend && npm run dev"
echo Waiting for the servers to come up...
timeout /t 12 /nobreak >nul
start "" "http://localhost:5173"
echo.
echo Opened http://localhost:5173 in your browser.
echo If it didn't open, type that address into Chrome/Edge yourself.
echo.
echo IMPORTANT: leave the two minimized windows (SRC Backend / SRC Frontend)
echo running while you use the app. Close them when you're done.
echo You can close THIS window now.
echo.
pause
