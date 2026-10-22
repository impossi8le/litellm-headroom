param([int]$StartupTimeoutSeconds = 120, [switch]$Restart)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$headroom = Join-Path $PSScriptRoot '.venv\Scripts\headroom.exe'
$litellm = Join-Path $PSScriptRoot '.litellm-venv\Scripts\python.exe'
$config = Join-Path $PSScriptRoot 'litellm-config.yaml'
$models = Join-Path $PSScriptRoot 'models.yaml'
$envFile = Join-Path $PSScriptRoot '.env'
$logDir = Join-Path $PSScriptRoot 'logs'
$started = @()

function Import-EnvFile([string]$Path) {
    if (!(Test-Path -LiteralPath $Path)) { return }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (!$trimmed -or $trimmed.StartsWith('#')) { continue }
        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }
        $name = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim().Trim('"').Trim("'")
        if (!(Test-Path "Env:$name")) { Set-Item -Path "Env:$name" -Value $value }
    }
}

function Stop-LiteLLM {
    $targets = @(Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -match '^python(w)?\.exe$') -and ($_.CommandLine -match '\blitellm\.proxy\.proxy_cli\b')
    })
    foreach ($target in $targets) {
        $running = Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue
        if ($running) { Stop-Process -InputObject $running -Force -ErrorAction SilentlyContinue }
    }
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        if (!(Test-Service 'http://127.0.0.1:8001/health/liveliness')) { return }
        Start-Sleep -Milliseconds 300
    }
}

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
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    Import-EnvFile $envFile
    # models.yaml is the source of truth; litellm-config.yaml is generated and
    # gitignored, so render it first and only then require it to exist.
    if (Test-Path -LiteralPath $models) {
        & $litellm (Join-Path $PSScriptRoot 'render.py') --root $PSScriptRoot
        if ($LASTEXITCODE -ne 0) { throw 'Config render failed: see the errors above' }
    }
    if (!(Test-Path -LiteralPath $config)) {
        if (Test-Path -LiteralPath $models) {
            throw "Config was not generated from models.yaml: $config"
        }
        throw "Config is missing: $config (and models.yaml was not found either)"
    }
    if ($Restart) { Stop-LiteLLM }
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
    Write-Host 'API key: LITELLM_MASTER_KEY from .env (or the environment).'
    Write-Host "Logs: $logDir"
    exit 0
} catch {
    foreach ($process in $started) {
        if (!$process.HasExited) { Stop-Process -Id $process.Id -ErrorAction SilentlyContinue }
    }
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
