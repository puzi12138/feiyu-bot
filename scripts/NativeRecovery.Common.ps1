# Importing this module only defines helpers. Recovery uses the reviewed daily
# launcher, which verifies the exact QQ generation before connecting Snow.
. "$PSScriptRoot\NativeStartup.Common.ps1"

function Get-NativeRecoveryHealthDecision($Listeners,$Pending,$SnowProcesses) {
    $ports=@($Listeners)
    if (!$ports.Count) { return [pscustomobject]@{Required=$true;Reason='native_listener_missing'} }
    if ($ports.Count -ne 1 -or $ports[0].LocalAddress -ne '127.0.0.1') { throw 'native_recovery_listener_conflict' }
    if ($Pending) {
        if ($Pending.Active -isnot [bool]) { throw 'native_recovery_pending_invalid' }
        if ($Pending.Active) { return [pscustomobject]@{Required=$true;Reason='pending_native_generation'} }
    }
    $owner=$ports[0].OwningProcess
    if (($owner -isnot [int] -and $owner -isnot [long] -and $owner -isnot [uint32]) -or $owner -le 0) {
        throw 'native_recovery_listener_owner_invalid'
    }
    $target=@($SnowProcesses | Where-Object {
        ($_.pid -is [int] -or $_.pid -is [long] -or $_.pid -is [uint32]) -and $_.pid -eq $owner
    })
    $healthy=$target.Count -eq 1 -and [string]$target[0].uin -eq '0' -and
        $target[0].injected -is [bool] -and $target[0].injected -and
        $target[0].connected -is [bool] -and $target[0].connected -and
        $target[0].loggedIn -is [bool] -and $target[0].loggedIn -and $target[0].status -eq 'online'
    if ($healthy) { return [pscustomobject]@{Required=$false;Reason='native_and_snow_healthy'} }
    [pscustomobject]@{Required=$true;Reason='snow_binding_not_online'}
}

function Get-NativeRecoverySnowProcesses([string]$Root) {
    $credential=$null; $login=$null; $headers=$null
    try {
        $credential=Get-Content -LiteralPath (Join-Path $Root 'config\bootstrap.json') -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        $login=Invoke-RestMethod 'http://127.0.0.1:5099/api/login' -Method Post -ContentType 'application/json' -Body (@{password=$credential.snowluma_password}|ConvertTo-Json) -TimeoutSec 5
        if (!$login.success -or !$login.token) { throw 'native_recovery_snow_authentication_failed' }
        $headers=@{Authorization="Bearer $($login.token)"}
        $result=Invoke-RestMethod 'http://127.0.0.1:5099/api/processes' -Headers $headers -TimeoutSec 5
        if (!$result.PSObject.Properties['list']) { throw 'native_recovery_snow_process_metadata_unavailable' }
        @($result.list | Select-Object pid,uin,injected,connected,loggedIn,status)
    } finally { $credential=$null; $login=$null; $headers=$null }
}

function Get-NativeRecoverySnapshot([string]$Root) {
    if (!(Test-Path -LiteralPath (Join-Path $Root 'config\qq-native-startup.json'))) {
        return [pscustomobject]@{Required=$false;Reason='native_startup_not_configured'}
    }
    $settings=Read-NativeStartupSettings $Root
    if (!$settings.auto_recover) { return [pscustomobject]@{Required=$false;Reason='automatic_recovery_disabled'} }
    if (Test-Path -LiteralPath (Join-Path $Root 'config\qq-account-mismatch.json')) {
        return [pscustomobject]@{Required=$false;Reason='account_confirmation_required'}
    }
    if (!(Get-NetTCPConnection -State Listen -LocalPort 5099 -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{Required=$false;Reason='snow_control_unavailable'}
    }
    $listeners=@(Get-NetTCPConnection -State Listen -LocalPort 16199 -ErrorAction SilentlyContinue)
    $pending=$null
    if ($listeners.Count) {
        $stage=Split-Path (Split-Path $settings.daily_launcher -Parent) -Parent
        $pendingPath=Join-Path $stage 'daily-runtime\pending.json'
        if (Test-Path -LiteralPath $pendingPath) {
            $pending=Get-Content -LiteralPath $pendingPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        }
    }
    # An active pending generation must be rechecked after a late login. The
    # daily launcher validates its identity and configuration before any load.
    if (!$listeners.Count -or ($pending -and $pending.Active -is [bool] -and $pending.Active)) {
        return Get-NativeRecoveryHealthDecision $listeners $pending @()
    }
    $processes=@(Get-NativeRecoverySnowProcesses $Root)
    Get-NativeRecoveryHealthDecision $listeners $pending $processes
}

