@echo off
setlocal
set "LAUNCHER=%~dp0dsh-control-launcher.ps1"
set "PSH=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%LAUNCHER%" exit /b 1
title DSH Control
if "%~1"=="" set "LAUNCHER=%~dp0start-dsh-control-tui.ps1"
"%PSH%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%LAUNCHER%" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
