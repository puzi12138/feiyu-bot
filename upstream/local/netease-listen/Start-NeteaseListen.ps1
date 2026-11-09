$ErrorActionPreference = 'Stop'
$botRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$pidFile = Join-Path $botRoot 'run\netease-listen.json'
$nodeExe = Join-Path $botRoot 'apps\SnowLuma\node.exe'
$entryFile = Join-Path $PSScriptRoot 'service.cjs'
$settings = Get-Content -Raw -LiteralPath (Join-Path $botRoot 'config\netease-listen\settings.json') | ConvertFrom-Json
if (-not $settings.auto_start) { return }
if (Test-Path -LiteralPath $pidFile) {
    $record = Get-Content -Raw -LiteralPath $pidFile | ConvertFrom-Json
    $running = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)" -ErrorAction SilentlyContinue
    if ($running -and $running.ExecutablePath -eq $nodeExe -and $running.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$record.Created).ToUniversalTime().Ticks) {
        Write-Host 'Netease listen bridge is already running.'
        return
    }
}
if (Get-NetTCPConnection -State Listen -LocalPort 16300 -ErrorAction SilentlyContinue) {
    throw 'Netease listen bridge port 16300 is already occupied.'
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$env:QQBOT_ROOT = $botRoot
$child = Start-Process -FilePath $nodeExe -ArgumentList @($entryFile) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $botRoot "logs\netease-listen\$stamp.out.log") -RedirectStandardError (Join-Path $botRoot "logs\netease-listen\$stamp.err.log") -PassThru
$info = Get-CimInstance Win32_Process -Filter "ProcessId=$($child.Id)"
@{Id=$child.Id;Exe=$nodeExe;Created=$info.CreationDate.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8
Write-Host "Started Netease listen bridge (PID $($child.Id))."
