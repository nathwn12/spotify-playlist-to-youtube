@echo off
rem sp2yt PROD build: compile app+backend+UI to single portable sp2yt.exe (WebView2 window).
rem DEV stays source-only: sp2yt-start.cmd / sp2yt-dev shortcut (localhost server, no build).
rem Usage: build.cmd  (run from repo root, needs python3 3.12+ + pyinstaller + pywebview)
setlocal
cd /d "%~dp0"
echo [1/6] acceptance gate...
call acceptance.cmd
if errorlevel 1 (
  echo ACCEPTANCE RED — not building
  exit /b 1
)
if exist sp2yt.exe (
  echo [2/6] backing up current exe...
  copy /y sp2yt.exe sp2yt.prev.exe >nul
)
echo [3/6] killing running instances...
rem dev never runs as an exe (sp2yt-start.cmd runs backend.py), so only the prod exe is killed
taskkill /f /im sp2yt.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul
echo [4/6] compiling...
python3 -m PyInstaller --noconsole --onefile --name sp2yt --icon assets\icon.ico --add-data "index.html;." app.py > build.log 2>&1
if errorlevel 1 (
  echo BUILD FAILED — see build.log
  exit /b 1
)
move /y dist\sp2yt.exe sp2yt.exe >nul
echo [5/6] smoke test...
start "" /min sp2yt.exe
python3 -c "import json,urllib.request,time;time.sleep(5);h=json.loads(urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=15).read());assert h.get('ok') and h.get('version');print('smoke: health ok', h['version'], '(webview shell serving)')"
if errorlevel 1 (
  echo SMOKE FAILED — restoring previous exe...
  if exist sp2yt.prev.exe move /y sp2yt.prev.exe sp2yt.exe >nul
  taskkill /f /im sp2yt.exe >nul 2>&1
  exit /b 1
)
taskkill /f /im sp2yt.exe >nul 2>&1
del sp2yt.prev.exe >nul 2>&1
echo [6/6] BUILD GREEN — prod exe at %CD%\sp2yt.exe
