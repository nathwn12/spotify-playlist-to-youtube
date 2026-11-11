@echo off
rem Offline acceptance gate: replays the recorded playlist rows through backend.py,
rem then proves the shipped page script parses.
rem Wrapper only - non-zero if any step fails (0 green, 1 red).
setlocal
cd /d "%~dp0"
python3 -X utf8 tests\acceptance.py
if errorlevel 1 exit /b 1
python3 -X utf8 tests\check_page.py
if errorlevel 1 exit /b 1
python3 -X utf8 tests\test_cookies.py
if errorlevel 1 exit /b 1
python3 -X utf8 tests\test_connect_card.py
if errorlevel 1 exit /b 1
python3 -X utf8 tests\check_release_assets.py
if errorlevel 1 exit /b 1
exit /b 0
