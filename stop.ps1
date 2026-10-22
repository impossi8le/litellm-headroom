param([switch]$LiteLLMOnly)
$ErrorActionPreference = 'Stop'
$rootPattern = [regex]::Escape($PSScriptRoot) + '(?:[\\/"\s]|$)'
$targets = @(Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -match $rootPattern) -and (
        ($_.CommandLine -match '\blitellm\.proxy\.proxy_cli\b') -or
        (!$LiteLLMOnly -and (($_.CommandLine -match '[\\/]headroom\.exe') -or
                            ($_.CommandLine -match '[\\/]editor\.py')))
    ) -and ($_.Name -match '^(python(w)?|headroom|litellm)\.exe$')
})
foreach ($target in $targets) {
    try {
        Stop-Process -Id $target.ProcessId -Force -ErrorAction Stop
    } catch {
        if (Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue) { throw }
    }
    Write-Host "Stopped $($target.Name) (PID $($target.ProcessId))"
}
if ($targets.Count) {
    $ids = @($targets | ForEach-Object { [int]$_.ProcessId })
    Wait-Process -Id $ids -Timeout 10 -ErrorAction SilentlyContinue
    if (Get-Process -Id $ids -ErrorAction SilentlyContinue) {
        throw 'Some workspace processes did not stop.'
    }
}
Write-Host 'Workspace services stopped.'
