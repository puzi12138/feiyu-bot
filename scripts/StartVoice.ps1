$ErrorActionPreference = 'Stop'
$botRoot = Split-Path $PSScriptRoot -Parent
$pythonExe = "$botRoot\runtime\gsv-venv\Scripts\python.exe"
$pidPath = "$botRoot\run\gsv.json"
if (Test-Path -LiteralPath $pidPath) {
    $record = Get-Content -Raw -LiteralPath $pidPath | ConvertFrom-Json
    $running = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)" -ErrorAction SilentlyContinue
    if ($running -and $running.ExecutablePath -eq $pythonExe -and $running.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$record.Created).ToUniversalTime().Ticks) {
        Write-Host "Local voice is already running (PID $($record.Id))."
        return
    }
}
if (Get-NetTCPConnection -State Listen -LocalPort 9880 -ErrorAction SilentlyContinue) { throw 'Port 9880 is already in use.' }
$env:HF_HOME = "$botRoot\cache\huggingface"
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$env:TEMP = "$botRoot\temp"
$env:TMP = "$botRoot\temp"
$env:PATH = "$botRoot\runtime\ffmpeg\bin;" + $env:PATH
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$child = Start-Process -FilePath $pythonExe -ArgumentList @('api_v2.py','-a','127.0.0.1','-p','9880','-c',"$botRoot\config\gsv-inference.yaml") -WorkingDirectory "$botRoot\apps\GPT-SoVITS" -WindowStyle Hidden -RedirectStandardOutput "$botRoot\logs\gsv-$stamp.out.log" -RedirectStandardError "$botRoot\logs\gsv-$stamp.err.log" -PassThru
$info = Get-CimInstance Win32_Process -Filter "ProcessId=$($child.Id)"
@{Id=$child.Id; Exe=$pythonExe; Created=$info.CreationDate.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8
Write-Host "Started local character voice (PID $($child.Id)), http://127.0.0.1:9880"
