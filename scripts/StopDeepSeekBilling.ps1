$ErrorActionPreference = 'Stop'
$billingRoot = 'D:\QQBot'
$billingRecordPath = Join-Path $billingRoot 'run\deepseek-billing-portal.json'
if (-not (Test-Path -LiteralPath $billingRecordPath)) { exit 0 }
$billingRecord = Get-Content -LiteralPath $billingRecordPath -Raw | ConvertFrom-Json
$billingProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($billingRecord.Id)" -ErrorAction SilentlyContinue
if (-not $billingProcess) { exit 0 }
$billingExpectedExe = Join-Path $billingRoot 'runtime\astrbot-venv\Scripts\python.exe'
if ($billingProcess.ExecutablePath -ne $billingExpectedExe -or $billingProcess.CreationDate.ToUniversalTime().Ticks -ne ([datetime]$billingRecord.Created).ToUniversalTime().Ticks -or $billingProcess.CommandLine -notlike '*deepseek-billing-portal*server.py*') { throw 'Portal process identity changed.' }
function Stop-VerifiedBillingProcesses {
    # The venv launcher may delegate the listener to one verified base-Python child.
    $billingListeners = @(Get-NetTCPConnection -State Listen -LocalPort 16302 -ErrorAction SilentlyContinue)
    $billingOwners = @($billingListeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($billingOwners.Count -gt 1) { throw 'Billing listener ownership is ambiguous.' }
    if ($billingOwners.Count -eq 1 -and $billingOwners[0] -ne $billingRecord.Id) {
        $billingChild = Get-CimInstance Win32_Process -Filter "ProcessId=$($billingOwners[0])" -ErrorAction Stop
        $billingRuntimeExe = Join-Path $billingRoot 'runtime\python\cpython-3.12-windows-x86_64-none\python.exe'
        if ($billingChild.ParentProcessId -ne $billingRecord.Id -or $billingChild.ExecutablePath -ne $billingRuntimeExe -or $billingChild.CommandLine -notlike '*deepseek-billing-portal*server.py*' -or $billingChild.CreationDate.ToUniversalTime() -lt ([datetime]$billingRecord.Created).ToUniversalTime()) { throw 'Billing listener child identity changed.' }
        $billingChildRecheck = Get-CimInstance Win32_Process -Filter "ProcessId=$($billingChild.ProcessId)" -ErrorAction SilentlyContinue
        if ($billingChildRecheck -and $billingChildRecheck.CreationDate.ToUniversalTime().Ticks -eq $billingChild.CreationDate.ToUniversalTime().Ticks) {
            Stop-Process -Id $billingChild.ProcessId
        }
    }
    $billingRootRecheck = Get-CimInstance Win32_Process -Filter "ProcessId=$($billingRecord.Id)" -ErrorAction SilentlyContinue
    if ($billingRootRecheck -and $billingRootRecheck.ExecutablePath -eq $billingExpectedExe -and $billingRootRecheck.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$billingRecord.Created).ToUniversalTime().Ticks) {
        Stop-Process -Id $billingRecord.Id
    }
}
# A portal launched without --browser owns no browser/profile to close.
# Verify both its command line and live status before stopping that exact generation.
if ($billingProcess.CommandLine -notmatch '(?:^|\s)--browser(?:\s|$)') {
    $billingStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:16302/api/status' -TimeoutSec 3
    if ($billingStatus.browser_state.state -ne 'unavailable') { throw 'Portal browser ownership is not established.' }
    Stop-VerifiedBillingProcesses
    Write-Output 'Static billing portal stopped; no dedicated browser was running.'
    exit 0
}
$billingCommandPath = Join-Path $billingRoot 'data\deepseek-billing\operator-command.json'
$billingPendingPath = Join-Path $billingRoot 'data\deepseek-billing\operator-command.pending.json'
$billingResultPath = Join-Path $billingRoot 'data\deepseek-billing\operator-result.json'
$billingRequestId = [guid]::NewGuid().ToString('N')
$billingCommandJson = @{command='shutdown_worker';request_id=$billingRequestId} | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText($billingPendingPath, $billingCommandJson, (New-Object System.Text.UTF8Encoding($false)))
Move-Item -LiteralPath $billingPendingPath -Destination $billingCommandPath -Force
$billingDeadline = (Get-Date).AddSeconds(40)
$billingClosed = $false
while ((Get-Date) -lt $billingDeadline) {
    try {
        $billingStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:16302/api/status' -TimeoutSec 2
        $billingResult = if (Test-Path -LiteralPath $billingResultPath) { Get-Content -LiteralPath $billingResultPath -Raw | ConvertFrom-Json } else { $null }
        if ($billingResult.request_id -eq $billingRequestId -and $billingResult.state -eq 'done' -and $billingStatus.browser_state.state -eq 'closed') { $billingClosed = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 500
}
if (-not $billingClosed) { throw 'The dedicated browser has not confirmed a clean close; no process terminated.' }
Stop-VerifiedBillingProcesses
Write-Output 'DeepSeek billing service stopped after closing its dedicated browser; profile retained.'
