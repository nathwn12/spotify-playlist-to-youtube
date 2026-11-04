@echo off
rem Offline acceptance gate: replays the recorded owner data through backend.py.
rem Wrapper only - returns the python exit code unchanged (0 green, 1 red).
setlocal
cd /d "%~dp0"
python3 -X utf8 tests\acceptance.py
set "RC=%errorlevel%"
exit /b %RC%
