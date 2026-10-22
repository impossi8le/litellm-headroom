param([string]$DesktopPath = [Environment]::GetFolderPath('Desktop'))
$ErrorActionPreference = 'Stop'
if (!(Test-Path -LiteralPath $DesktopPath -PathType Container)) {
    throw "Desktop directory not found: $DesktopPath"
}
$backup = Join-Path $PSScriptRoot ('backups\desktop_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
foreach ($name in @('bat.bat', 'stop-litellm-headroom.bat')) {
    $destination = Join-Path $DesktopPath $name
    if (Test-Path -LiteralPath $destination) {
        Copy-Item -LiteralPath $destination -Destination (Join-Path $backup $name)
    }
    $source = Join-Path $PSScriptRoot $name
    if (!(Test-Path -LiteralPath $source -PathType Leaf)) { throw "Launcher not found: $source" }
    $content = "@echo off`r`ncall `"$source`" %*`r`nexit /b %ERRORLEVEL%`r`n"
    [System.IO.File]::WriteAllText($destination, $content, [System.Text.Encoding]::Default)
    Write-Host "Updated $destination"
}
Write-Host "Previous desktop launchers: $backup"
