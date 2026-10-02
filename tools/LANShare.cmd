@echo off
rem Double-clickable entry point. The shortcut on the desktop points here.
rem
rem -ExecutionPolicy Bypass is scoped to this one process: it lets the launcher
rem run on a machine that has never allowed scripts, without changing a
rem machine-wide setting on the user's behalf.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0lanshare-launcher.ps1" %*
if errorlevel 1 pause
