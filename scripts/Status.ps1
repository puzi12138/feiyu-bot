$Root = Split-Path $PSScriptRoot -Parent
foreach ($port in @(6185,6199,5099,9880,18888,18889,16300)) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($listener) { $listener | Select-Object LocalAddress,LocalPort,OwningProcess } else { Write-Host "Port $port is offline." }
}
Write-Host "Credentials: $Root\config\面板登录信息.txt"
if (Test-Path -LiteralPath "$PSScriptRoot\BusinessHealth.Common.ps1") {
    try {
        . "$PSScriptRoot\BusinessHealth.Common.ps1"
        Invoke-BusinessHealthCheck -Root $Root -Manual
    } catch { Write-Warning 'Business health check unavailable; state remains unverified.' }
}

