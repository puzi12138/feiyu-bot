# This module defines startup wiring only. Importing it does not touch QQ.
function Read-NativeStartupSettings([string]$Root) {
    $path=Join-Path $Root 'config\qq-native-startup.json'
    $value=Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    $names=@($value.PSObject.Properties.Name)
    $allowed=@('schema','enabled','expected_uin','daily_launcher','powershell7','recover_known_exited_lock','auto_recover')
    if ($names.Count -ne $allowed.Count -or @($names|Where-Object {$_ -notin $allowed}).Count -or
        ($value.schema -isnot [int] -and $value.schema -isnot [long]) -or $value.schema -ne 1 -or $value.enabled -isnot [bool] -or !$value.enabled -or
        $value.expected_uin -isnot [string] -or $value.expected_uin -ne '0' -or
        $value.recover_known_exited_lock -isnot [bool] -or $value.auto_recover -isnot [bool]) {
        throw 'Native startup configuration is disabled or invalid; ordinary QQ fallback is not enabled.'
    }
    $expected=Join-Path $Root 'staging\napcat-trusted-native-bridge-review-v4.18.41\daily-tools\Start-NativeDaily.ps1'
    if ($value.daily_launcher -isnot [string] -or [IO.Path]::GetFullPath($value.daily_launcher) -ne $expected -or
        !(Test-Path -LiteralPath $value.daily_launcher -PathType Leaf)) { throw 'The reviewed daily launcher is missing or changed.' }
    if ($value.powershell7 -isnot [string] -or ![IO.Path]::IsPathRooted($value.powershell7) -or
        [IO.Path]::GetFileName($value.powershell7) -ne 'pwsh.exe' -or
        !(Test-Path -LiteralPath $value.powershell7 -PathType Leaf)) { throw 'The configured PowerShell 7 runtime is missing.' }
    $value
}

function Get-NativeStartupArguments($Settings,[switch]$Plan) {
    $values=@('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$Settings.daily_launcher)
    if (!$Plan) {
        $values+='-Execute'
        $values+='-NoOrdinaryRecovery'
        if ($Settings.recover_known_exited_lock) { $values+='-RecoverKnownExitedLock' }
    }
    # Existing ordinary QQ is never stopped by the automatic/daily wiring.
    # An explicit migration remains a separate reviewed operation.
    $values
}

function Invoke-NativeStartupChild($Settings,[string[]]$Arguments) {
    # PowerShell 7 is required by the reviewed ACL and path APIs. Invoke an
    # absolute executable directly; no command strings or credentials in argv.
    & $Settings.powershell7 @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Native QQ startup did not complete. Check its protected daily-runtime records. Ordinary QQ may have been recovered, but native delivery remains unavailable and unknown messages were not retried.' }
}

function Invoke-ConfiguredNativeStartup([string]$Root,[switch]$ConfirmBotAccount,[switch]$AutomaticRecovery,[switch]$Plan) {
    $settings=Read-NativeStartupSettings $Root
    if ($AutomaticRecovery -and !$settings.auto_recover) { return }
    $guard=Join-Path $Root 'config\qq-account-mismatch.json'
    if ((Test-Path -LiteralPath $guard) -and !$ConfirmBotAccount) {
        throw 'QQ account needs verification. Use the bot connection menu after logging into 0.'
    }
    $arguments=@(Get-NativeStartupArguments $settings -Plan:$Plan)
    Invoke-NativeStartupChild $settings $arguments
    # Only a completed native+Snow identity verification can clear the marker.
    if (!$Plan -and $ConfirmBotAccount -and (Test-Path -LiteralPath $guard)) { Remove-Item -LiteralPath $guard -ErrorAction Stop }
}
