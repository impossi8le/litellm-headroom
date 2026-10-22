@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
set "STOP_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %STOP_RESULT%
