@echo off
setlocal
title Stop LiteLLM and Headroom
powershell.exe -NoProfile -Command "$ErrorActionPreference = 'Stop'; function Find-ServiceProcesses { Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^(litellm|headroom)\.exe$') -or (($_.Name -match '^python(w)?\.exe$') -and (($_.CommandLine -match '\blitellm\.proxy\.proxy_cli\b') -or ($_.CommandLine -match '[\\/]headroom\.exe')) ) } }; try { $targets = @(Find-ServiceProcesses); if ($targets.Count -eq 0) { Write-Host 'LiteLLM and Headroom are already stopped.'; exit 0 }; foreach ($target in $targets) { $running = Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue; if ($running) { Write-Host ('Stopping {0} (PID {1})' -f $target.Name, $target.ProcessId); Stop-Process -InputObject $running -Force -ErrorAction SilentlyContinue } }; Start-Sleep -Seconds 2; $remaining = @(Find-ServiceProcesses); if ($remaining.Count -gt 0) { $remaining | Select-Object ProcessId, Name | Format-Table; Write-Host 'ERROR: Some processes are still running.'; exit 1 }; Write-Host 'LiteLLM and Headroom stopped.'; exit 0 } catch { Write-Host ('ERROR: ' + $_.Exception.Message); exit 1 }"
set "STOP_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %STOP_RESULT%
