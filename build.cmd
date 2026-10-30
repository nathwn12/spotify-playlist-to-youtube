@echo off
rem sp2yt PROD build: compile app+backend+UI to single portable sp2yt-app.exe (WebView2 window).
rem DEV stays source-only: sp2yt-start.cmd / sp2yt-dev shortcut (localhost server, no build).
rem Usage: build.cmd  (run from repo root, needs python3 + pyinstaller + pywebview)
setlocal
cd /d "%~dp0"
if exist sp2yt-app.exe (
  echo [1/5] backing up current exe...
  copy /y sp2yt-app.exe sp2yt-app.prev.exe >nul
)
echo [2/5] killing running instances...
taskkill /f /im sp2yt-app.exe >nul 2>&1
taskkill /f /im sp2yt.exe >nul 2>&1
timeout /t 1 /nobreak >nul
echo [3/5] compiling...
python3 -m PyInstaller --noconsole --onefile --name sp2yt-app --icon assets\icon.ico --add-data "index.html;." app.py > build.log 2>&1
if errorlevel 1 (
  echo BUILD FAILED — see build.log
  exit /b 1
)
move /y dist\sp2yt-app.exe sp2yt-app.exe >nul
echo [4/5] smoke test...
start "" /min sp2yt-app.exe
python3 -c "import json,urllib.request,time;time.sleep(5);h=json.loads(urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=15).read());assert h.get('ok');print('smoke: health ok (webview shell serving)')"
if errorlevel 1 (
  echo SMOKE FAILED — restoring previous exe...
  if exist sp2yt-app.prev.exe move /y sp2yt-app.prev.exe sp2yt-app.exe >nul
  taskkill /f /im sp2yt-app.exe >nul 2>&1
  exit /b 1
)
taskkill /f /im sp2yt-app.exe >nul 2>&1
del sp2yt-app.prev.exe >nul 2>&1
echo [5/5] BUILD GREEN — prod exe at %CD%\sp2yt-app.exe
