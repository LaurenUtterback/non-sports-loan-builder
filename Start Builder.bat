@echo off
title South River Non-Sports Loan Builder
cd /d "%~dp0"
echo ============================================================
echo   South River - Non-Sports Loan Builder
echo ============================================================
echo.
echo Starting the backend and frontend (two minimized windows)...
start "SRC NS Backend"  /min cmd /k "cd backend && .venv\Scripts\python.exe run_local.py"
start "SRC NS Frontend" /min cmd /k "cd frontend && npm run dev"
echo Waiting for the servers to come up...
timeout /t 12 /nobreak >nul
start "" "http://localhost:5174"
echo.
echo Opened http://localhost:5174 in your browser.
echo If it didn't open, type that address into Chrome/Edge yourself.
echo.
echo IMPORTANT: leave the two minimized windows (SRC NS Backend / SRC NS Frontend)
echo running while you use the app. Close them when you're done.
echo You can close THIS window now.
echo.
pause
