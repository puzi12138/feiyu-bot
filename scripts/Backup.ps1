$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$wasRunning = @(Get-ChildItem -LiteralPath "$Root\run" -Filter '*.json').Count -gt 0
& "$PSScriptRoot\Stop.ps1"
try {
    & "$Root\runtime\astrbot-venv\Scripts\python.exe" "$PSScriptRoot\backup.py"
    if ($LASTEXITCODE -ne 0) { throw 'Backup failed.' }
} finally {
    if ($wasRunning) { & "$PSScriptRoot\Start.ps1" }
}

