$ErrorActionPreference = 'Stop'
$botRoot = Split-Path $PSScriptRoot -Parent
$settingsPath = Join-Path $botRoot 'config\codex-brief.json'
if (-not (Test-Path -LiteralPath $settingsPath)) { return }
$briefSettings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
if (-not $briefSettings.enabled) { return }
$briefExe = Join-Path $botRoot 'runtime\astrbot-venv\Scripts\pythonw.exe'
$briefScript = Join-Path $botRoot 'scripts\CodexBrief.py'
$briefPidPath = Join-Path $botRoot 'run\codex-brief-watch.json'
if (Test-Path -LiteralPath $briefPidPath) {
    try {
        $briefRecord = Get-Content -LiteralPath $briefPidPath -Raw | ConvertFrom-Json
        $briefProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($briefRecord.Id)" -ErrorAction SilentlyContinue
        if ($briefProcess -and $briefProcess.ExecutablePath -eq $briefExe -and
            $briefProcess.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$briefRecord.Created).ToUniversalTime().Ticks) { return }
    } catch { }
}
$briefChild = Start-Process -FilePath $briefExe -ArgumentList @($briefScript, '--watch') -WorkingDirectory $botRoot -WindowStyle Hidden -PassThru
$briefInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($briefChild.Id)"
if ($briefInfo) {
    @{Id=$briefChild.Id; Exe=$briefExe; Created=$briefInfo.CreationDate.ToUniversalTime().ToString('o')} |
        ConvertTo-Json | Set-Content -LiteralPath $briefPidPath -Encoding UTF8
}
