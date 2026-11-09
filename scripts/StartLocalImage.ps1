$ErrorActionPreference = 'Stop'
$botRoot = Split-Path $PSScriptRoot -Parent
$configPath = Join-Path $botRoot 'config\local-image.json'
if (-not (Test-Path -LiteralPath $configPath)) { return }
$pythonExe = Join-Path $botRoot 'runtime\astrbot-venv\Scripts\python.exe'
$pidPath = Join-Path $botRoot 'run\local-image.json'
if (Test-Path -LiteralPath $pidPath) {
    $record = Get-Content -Raw -LiteralPath $pidPath | ConvertFrom-Json
    $running = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)" -ErrorAction SilentlyContinue
    if ($running -and $running.ExecutablePath -eq $pythonExe -and $running.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$record.Created).ToUniversalTime().Ticks) {
        Write-Host 'Local image broker is already running.'
        return
    }
}
if (Get-NetTCPConnection -State Listen -LocalPort 18889 -ErrorAction SilentlyContinue) { throw 'Port 18889 is already in use.' }
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$env:TEMP = Join-Path $botRoot 'temp'
$env:TMP = $env:TEMP
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$child = Start-Process -FilePath $pythonExe -ArgumentList @("$botRoot\scripts\LocalImageService.py") -WorkingDirectory $botRoot -WindowStyle Hidden -RedirectStandardOutput "$botRoot\logs\local-image-$stamp.out.log" -RedirectStandardError "$botRoot\logs\local-image-$stamp.err.log" -PassThru
$info = Get-CimInstance Win32_Process -Filter "ProcessId=$($child.Id)"
@{Id=$child.Id; Exe=$pythonExe; Created=$info.CreationDate.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8
Write-Host 'Local image broker started on 127.0.0.1:18889; GPU model loads only during generation.'
