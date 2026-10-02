@echo off
rem Double-clickable entry point for stopping the server. The "LANShare Stop"
rem shortcut points here.
rem
rem -ExecutionPolicy Bypass is scoped to this one process, for the same reason
rem it is in LANShare.cmd: it works on a machine that has never allowed
rem scripts, without changing a machine-wide setting on the user's behalf.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0lanshare-stop.ps1" %*
if errorlevel 1 pause
