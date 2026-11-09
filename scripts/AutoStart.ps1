$ErrorActionPreference = 'Stop'
$botRoot = Split-Path $PSScriptRoot -Parent
$logPath = Join-Path $botRoot 'logs\autostart.log'
Start-Sleep -Seconds 25
for ($attempt = 1; $attempt -le 6; $attempt++) {
    try {
        & "$PSScriptRoot\Start.ps1" -SkipQQConnection -AutomaticServiceRecovery *>> $logPath
        exit 0
    } catch {
        "[$(Get-Date -Format o)] attempt=$attempt $($_.Exception.Message)" | Add-Content -LiteralPath $logPath
        if ($attempt -lt 6) { Start-Sleep -Seconds 20 }
    }
}
exit 1
