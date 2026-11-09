$ErrorActionPreference = 'Stop'
$portalRoot = 'D:\QQBot'
$portalPython = Join-Path $portalRoot 'runtime\astrbot-venv\Scripts\python.exe'
$portalScript = Join-Path $portalRoot 'apps\deepseek-billing-portal\server.py'
$portalPidPath = Join-Path $portalRoot 'run\deepseek-billing-portal.json'
$portalPort = 16302
if (Test-Path -LiteralPath $portalPidPath) {
    $portalRecord = Get-Content -LiteralPath $portalPidPath -Raw | ConvertFrom-Json
    $portalProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($portalRecord.Id)" -ErrorAction SilentlyContinue
    if ($portalProcess -and $portalProcess.ExecutablePath -eq $portalPython -and $portalProcess.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$portalRecord.Created).ToUniversalTime().Ticks) {
        Write-Output 'DeepSeek billing portal is already running: http://127.0.0.1:16302/login'
        exit 0
    }
}
if (Get-NetTCPConnection -State Listen -LocalPort $portalPort -ErrorAction SilentlyContinue) { throw 'Billing portal port is already in use.' }
$portalArguments = @($portalScript)
$portalPreferencePath = Join-Path $portalRoot 'config\token-query-cockpit-only.json'
$portalBrowserEnabled = $false
$portalReconcileEnabled = $false
$portalScheduleMode = 'on_demand'
if (Test-Path -LiteralPath $portalPreferencePath -PathType Leaf) {
    $portalPreference = Get-Content -LiteralPath $portalPreferencePath -Raw | ConvertFrom-Json
    $browserSetting = $portalPreference.PSObject.Properties['deepseek_automatic_browser_enabled']
    $portalBrowserEnabled = if ($null -ne $browserSetting) { $browserSetting.Value -eq $true } else {
        $portalPreference.query_source -in @('official_website_only', 'official_default_with_explicit_cockpit')
    }
    $portalReconcileEnabled = $portalPreference.deepseek_automatic_reconciliation_enabled -eq $true
    if ($portalPreference.official_browser_mode -in @('on_demand', 'verified_yesterday', 'current_official_snapshot')) {
        $portalScheduleMode = $portalPreference.official_browser_mode
    }
    if ($portalPreference.deepseek_periodic_reconciliation_enabled -eq $true) {
        if (!$portalBrowserEnabled -or $portalReconcileEnabled -or $portalScheduleMode -ne 'on_demand' -or
            $portalPreference.deepseek_periodic_reconciliation_interval_seconds -ne 7200) {
            throw 'DeepSeek periodic reconciliation requires the on-demand browser, a two-hour interval and the legacy automatic reconciliation to be disabled.'
        }
    }
}
if ($portalBrowserEnabled) {
    $portalArguments += @('--browser','--open-browser','--scheduled-mode',$portalScheduleMode)
    if ($portalReconcileEnabled) { $portalArguments += '--reconcile' }
} elseif (!(Test-Path -LiteralPath $portalPreferencePath -PathType Leaf)) {
    # Missing preferences must not silently start the legacy automatic export.
    $portalArguments += @('--browser','--open-browser','--scheduled-mode','on_demand')
}
$portalChild = Start-Process -FilePath $portalPython -ArgumentList $portalArguments -WorkingDirectory (Split-Path $portalScript -Parent) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $portalRoot 'logs\deepseek-billing-portal.out.log') -RedirectStandardError (Join-Path $portalRoot 'logs\deepseek-billing-portal.err.log') -PassThru
$portalInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($portalChild.Id)"
@{Id=$portalChild.Id; Exe=$portalPython; Created=$portalInfo.CreationDate.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $portalPidPath -Encoding UTF8
Write-Output 'DeepSeek billing portal: http://127.0.0.1:16302/login'


