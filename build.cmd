@echo off
rem sp2yt PROD build: compile backend+UI to a single portable sp2yt.exe, smoke-test it.
rem Usage: build.cmd  (run from the repo root, needs python3 + pyinstaller)
setlocal
cd /d "%~dp0"
if exist sp2yt.exe (
  echo [1/5] backing up current exe...
  copy /y sp2yt.exe sp2yt.prev.exe >nul
)
echo [2/5] killing running instances...
taskkill /f /im sp2yt.exe >nul 2>&1
timeout /t 1 /nobreak >nul
echo [3/5] compiling...
python3 -m PyInstaller --noconsole --onefile --name sp2yt --icon assets\icon.ico --add-data "index.html;." backend.py > build.log 2>&1
if errorlevel 1 (
  echo BUILD FAILED — see build.log
  exit /b 1
)
move /y dist\sp2yt.exe sp2yt.exe >nul
echo [4/5] smoke test...
start "" /min sp2yt.exe
python3 -c "import json,urllib.request,time;time.sleep(3);h=json.loads(urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=15).read());p=urllib.request.urlopen('http://127.0.0.1:8787/',timeout=15).read().decode();assert h.get('ok') and 'cookie_files' in p and 'lastSession' in p;print('smoke: health ok, UI ok')"
if errorlevel 1 (
  echo SMOKE FAILED — restoring previous exe...
  if exist sp2yt.prev.exe move /y sp2yt.prev.exe sp2yt.exe >nul
  taskkill /f /im sp2yt.exe >nul 2>&1
  exit /b 1
)
del sp2yt.prev.exe >nul 2>&1
echo [5/5] BUILD GREEN — prod exe live at %CD%\sp2yt.exe
