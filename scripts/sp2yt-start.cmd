@echo off
for %%I in ("%~dp0..") do set "REPO=%%~fI"
set PORT=8787
curl -s -m 3 http://127.0.0.1:%PORT%/health >nul 2>&1
if errorlevel 1 (
  echo Starting sp2yt server...
  start "sp2yt" /min python3 "%REPO%\backend.py"
  timeout /t 3 /nobreak >nul
)
start "" http://127.0.0.1:%PORT%/
