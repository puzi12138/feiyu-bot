$ErrorActionPreference = 'Stop'
$botRoot = Split-Path $PSScriptRoot -Parent
$pythonExe = "$botRoot\runtime\cover-venv\Scripts\python.exe"
$pidPath = "$botRoot\run\cover.json"
if (Test-Path -LiteralPath $pidPath) {
    $record = Get-Content -Raw -LiteralPath $pidPath | ConvertFrom-Json
    $running = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)" -ErrorAction SilentlyContinue
    if ($running -and $running.ExecutablePath -eq $pythonExe -and $running.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$record.Created).ToUniversalTime().Ticks) {
        Write-Host "Cover service is already running (PID $($record.Id))."
        return
    }
}
if (Get-NetTCPConnection -State Listen -LocalPort 18888 -ErrorAction SilentlyContinue) { throw 'Port 18888 is already in use.' }
$env:HF_HOME = "$botRoot\cache\huggingface"
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$env:TEMP = "$botRoot\temp"
$env:TMP = "$botRoot\temp"
$env:PATH = "$botRoot\runtime\ffmpeg\bin;" + $env:PATH
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$child = Start-Process -FilePath $pythonExe -ArgumentList @("$botRoot\scripts\CoverService.py") -WorkingDirectory "$botRoot\apps\RVC-AI-Cover-Service" -WindowStyle Hidden -RedirectStandardOutput "$botRoot\logs\cover-$stamp.out.log" -RedirectStandardError "$botRoot\logs\cover-$stamp.err.log" -PassThru
$info = Get-CimInstance Win32_Process -Filter "ProcessId=$($child.Id)"
@{Id=$child.Id; Exe=$pythonExe; Created=$info.CreationDate.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8
Write-Host "Started local cover service (PID $($child.Id)), http://127.0.0.1:18888"
