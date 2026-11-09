param([switch]$ConfirmBotAccount,[switch]$AutomaticRecovery,[switch]$Plan)
$ErrorActionPreference='Stop'
$nativeRoot=Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\NativeStartup.Common.ps1"
try {
if ($AutomaticRecovery) {
    $settings=Read-NativeStartupSettings $nativeRoot
    if (!$settings.auto_recover) { return }
    $entry=Join-Path $nativeRoot 'scripts\Invoke-NativeAutoRecovery.ps1'
    if (!(Test-Path -LiteralPath $entry -PathType Leaf)) { throw 'Native automatic recovery entry is unavailable.' }
    $arguments=@('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$entry)
    if (!$Plan) { $arguments+='-Execute' }
    & $settings.powershell7 @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Native automatic recovery did not complete. Check its protected recovery records.' }
    return
}
Invoke-ConfiguredNativeStartup $nativeRoot -ConfirmBotAccount:$ConfirmBotAccount -AutomaticRecovery:$AutomaticRecovery -Plan:$Plan
} finally {
    if (!$Plan -and (Test-Path -LiteralPath "$PSScriptRoot\BusinessHealth.Common.ps1")) {
        try {
            . "$PSScriptRoot\BusinessHealth.Common.ps1"
            Invoke-BusinessHealthCheck -Root $nativeRoot
        } catch { Write-Warning 'Business health check unavailable; original connection result is unchanged.' }
    }
}
