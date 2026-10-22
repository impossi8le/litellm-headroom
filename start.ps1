param([int]$StartupTimeoutSeconds = 120)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$headroom = Join-Path $PSScriptRoot '.venv\Scripts\headroom.exe'
$litellm = Join-Path $PSScriptRoot '.litellm-venv\Scripts\python.exe'
$config = Join-Path $PSScriptRoot 'litellm-config.yaml'
$logDir = Join-Path $PSScriptRoot 'logs'
$started = @()

function Test-Service([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch { return $false }
}

function Start-ServiceProcess([string]$Name, [string]$Exe, [string[]]$ServiceArgs, [string]$HealthUrl) {
    if (Test-Service $HealthUrl) {
        Write-Host "[OK] $Name is already running: $HealthUrl"
        return
    }
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
    $stdout = Join-Path $logDir "${Name}_${stamp}.out.log"
    $stderr = Join-Path $logDir "${Name}_${stamp}.err.log"
    $process = Start-Process -FilePath $Exe -ArgumentList $ServiceArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $script:started += $process
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $process.Refresh()
        if ($process.HasExited) {
            throw "$Name exited with code $($process.ExitCode). See $stderr and $stdout"
        }
        if (Test-Service $HealthUrl) {
            Write-Host "[OK] $Name is ready (PID $($process.Id)). Logs: $stdout / $stderr"
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name did not become ready in $StartupTimeoutSeconds seconds. See $stderr and $stdout"
}

try {
    if (!(Test-Path -LiteralPath $headroom)) { throw "Headroom is missing: $headroom" }
    if (!(Test-Path -LiteralPath $litellm)) { throw "LiteLLM is missing. Run python -m venv .litellm-venv --system-site-packages, then .litellm-venv\Scripts\python.exe -m pip install -r requirements.txt" }
    if (!(Test-Path -LiteralPath $config)) { throw "Config is missing: $config" }
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    Remove-Item Env:LITELLM_LOG -ErrorAction SilentlyContinue
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    $env:HEADROOM_TELEMETRY = 'off'
    $env:HEADROOM_BEACON = 'off'
    $env:LITELLM_TELEMETRY = 'False'
    Start-ServiceProcess 'headroom' $headroom @('proxy', '--host', '127.0.0.1', '--port', '8787', '--ccr-inline-resolve') 'http://127.0.0.1:8787/health'
    Start-ServiceProcess 'litellm' $litellm @('-m', 'litellm.proxy.proxy_cli', '--config', ('"' + $config + '"'), '--host', '127.0.0.1', '--port', '8001') 'http://127.0.0.1:8001/health/liveliness'
    Write-Host ''
    Write-Host 'Gateway URL: http://127.0.0.1:8001'
    Write-Host 'Models: http://127.0.0.1:8001/v1/models'
    Write-Host 'API key: general_settings.master_key in litellm-config.yaml.'
    Write-Host "Logs: $logDir"
    exit 0
} catch {
    foreach ($process in $started) {
        if (!$process.HasExited) { Stop-Process -Id $process.Id -ErrorAction SilentlyContinue }
    }
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
