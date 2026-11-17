@echo off
rem Refresh the Desktop shortcut and Windows icon cache after an icon change.
rem Lives in scripts\; the repo root is one level up, resolved to a full path.
for %%I in ("%~dp0..") do set "REPO=%%~fI"
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $sc=$ws.CreateShortcut(\"$env:USERPROFILE\Desktop\sp2yt.lnk\"); $sc.TargetPath=\"%REPO%\sp2yt.exe\"; $sc.WorkingDirectory=\"%REPO%\"; $sc.IconLocation=\"%REPO%\assets\icon.ico,0\"; $sc.Description=\"sp2yt — keyless Spotify to YT Music transfer\"; $sc.Save()"
ie4uinit.exe -show
echo Shortcut rebuilt + icon cache refreshed.
