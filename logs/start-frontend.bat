@echo off
cd /d "%~dp0..\frontend"
"C:\Program Files\nodejs\node.exe" "node_modules\vite\bin\vite.js" --host > "%~dp0frontend.out.log" 2>&1
