@echo off
rem Offline acceptance gate: replays the recorded playlist rows through backend.py,
rem then runs every other offline gate below.
rem Wrapper only - non-zero if any step fails (0 green, 1 red).
rem Lives in scripts\ but runs from the repo root: cd one level up so every
rem relative path below (tests\..., index.html) means the same as it always did.
setlocal
cd /d "%~dp0.."

rem --- isolated state -------------------------------------------------------
rem backend.py derives STATE (server.log, created.json, settings.json and the
rem three cookie files) from APPDATA at import time, and every python gate below
rem imports it. Without this the run wrote into the live app's state dir.
rem Every python child also goes through tests\state_guard.py, which refuses to
rem run its gate when a backend state path - or backend's own fallback to this
rem repository when STATE cannot be created - would write outside the root.
rem
rem SP2YT_ACCEPTANCE_SCRATCH pins the root name. It exists so the collision and
rem cleanup failure paths can be exercised under test; a normal run never sets it.
set "SP2YT_OWNED=0"
set "SP2YT_TRIES=0"
set "RC=1"
if not defined SP2YT_ACCEPTANCE_SCRATCH goto make_root
set "SP2YT_ROOT=%SP2YT_ACCEPTANCE_SCRATCH%"
mkdir "%SP2YT_ROOT%" >nul 2>nul
if errorlevel 1 goto root_taken
goto root_owned

:make_root
set /a SP2YT_TRIES+=1
set "SP2YT_ROOT=%TEMP%\sp2yt-acceptance-%RANDOM%%RANDOM%%RANDOM%"
mkdir "%SP2YT_ROOT%" >nul 2>nul
if not errorlevel 1 goto root_owned
if %SP2YT_TRIES% GEQ 5 goto root_failed
goto make_root

rem mkdir is the exclusive claim: it fails on an existing path, so a directory
rem this run did not create is never adopted and never deleted.
:root_taken
echo FAIL isolation: "%SP2YT_ROOT%" already exists or cannot be created - refusing to use a directory this run did not make
endlocal & exit /b 1

:root_failed
echo FAIL isolation: no exclusive scratch root under "%TEMP%" after %SP2YT_TRIES% attempts
endlocal & exit /b 1

:root_owned
set "SP2YT_OWNED=1"
set "SP2YT_APPDATA=%SP2YT_ROOT%\appdata"
set "SP2YT_STATE=%SP2YT_APPDATA%\sp2yt"
mkdir "%SP2YT_APPDATA%" "%SP2YT_ROOT%\localappdata" "%SP2YT_ROOT%\userprofile" "%SP2YT_ROOT%\temp" >nul 2>nul
rem Pre-created so backend's own makedirs cannot be what fails. The guard proves
rem STATE still resolves here before any gate code runs.
mkdir "%SP2YT_STATE%" >nul 2>nul
if not exist "%SP2YT_STATE%" goto state_failed
set "APPDATA=%SP2YT_APPDATA%"
set "LOCALAPPDATA=%SP2YT_ROOT%\localappdata"
set "USERPROFILE=%SP2YT_ROOT%\userprofile"
set "HOME=%SP2YT_ROOT%\userprofile"
set "TEMP=%SP2YT_ROOT%\temp"
set "TMP=%SP2YT_ROOT%\temp"
set "SP2YT_SCRATCH=%SP2YT_ROOT%"

rem Resolve the interpreter once: python3 where it exists, plain python on runners
rem that only ship python (GitHub windows-latest).
set "PY=python3"
%PY% -c "import sys" >nul 2>nul || set "PY=python"
%PY% -X utf8 tests\acceptance.py
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\test_lang.py
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\test_counts.py
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\test_editions.py
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\test_order.py
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\test_cover.py
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\check_page.py
if errorlevel 1 goto fail
node tests\check_ui.mjs
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\test_http_guard.py
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\test_fatal_mask.py
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\test_watchdog.py
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\test_cookies.py
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\test_connect_card.py
if errorlevel 1 goto fail
%PY% -X utf8 tests\state_guard.py tests\check_release_assets.py
if errorlevel 1 goto fail
echo acceptance: all gates green
set "RC=0"
goto cleanup

:state_failed
echo FAIL isolation: cannot create the synthetic state dirs under "%SP2YT_ROOT%"
goto fail

:fail
set "RC=1"

:cleanup
rem Only a root this run created is removed, and a root that survives it is a red
rem run: a silent leftover is a leaked state dir the next run could adopt.
if not "%SP2YT_OWNED%"=="1" goto cleanup_done
rd /s /q "%SP2YT_ROOT%" >nul 2>nul
if not exist "%SP2YT_ROOT%" goto cleanup_done
echo FAIL cleanup: the synthetic state dir survived at "%SP2YT_ROOT%" - inspect it before the next run
set "RC=1"

:cleanup_done
endlocal & exit /b %RC%
