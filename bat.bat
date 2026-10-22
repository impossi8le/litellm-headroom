@echo off
setlocal
title Headroom + LiteLLM Launcher
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Startup failed. See the error and logs above.
pause
exit /b %RESULT%
