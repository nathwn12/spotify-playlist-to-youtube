@echo off
rem Offline acceptance gate: replays the recorded owner data through backend.py,
rem then proves the shipped page script parses (node --check on the inline script).
rem Wrapper only - non-zero if any step fails (0 green, 1 red).
setlocal
cd /d "%~dp0"
python3 -X utf8 tests\acceptance.py
set "RC=%errorlevel%"
if not "%RC%"=="0" exit /b %RC%
python3 -X utf8 -c "import os,re,sys; h=open('index.html',encoding='utf-8').read(); m=re.search(r'<script>(.*?)</script>',h,re.S); p=os.path.join(os.environ.get('TEMP',os.getcwd()),'sp2yt-page.js'); open(p,'w',encoding='utf-8').write(m.group(1)) if m else sys.exit(1)"
if errorlevel 1 (
  echo PAGE SCRIPT RED — no inline script in index.html
  exit /b 1
)
node --check "%TEMP%\sp2yt-page.js"
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo PAGE SCRIPT RED — index.html inline script does not parse
  exit /b 1
)
exit /b 0
