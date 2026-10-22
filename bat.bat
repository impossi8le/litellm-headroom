@echo off
setlocal
title Headroom + LiteLLM Launcher
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "D:\litellm_headroom\start.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Startup failed. See the error and logs above.
pause
exit /b %RESULT%
