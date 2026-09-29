@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0install.ps1" goto incomplete
if not exist "%~dp0MANIFEST.json" goto incomplete
if not exist "%~dp0scripts\install-control.py" goto incomplete
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "install_code=%errorlevel%"
if not "%DSH_CONTROL_NO_PAUSE%"=="1" pause
exit /b %install_code%
:incomplete
echo Please extract ALL files from the ZIP to a folder first.
echo Then run "Install DSH Control.cmd" inside that folder.
echo Do not run this file from inside WinRAR or an archive preview.
if not "%DSH_CONTROL_NO_PAUSE%"=="1" pause
exit /b 2
