# Offline fixture checks only. No QQ enumeration, startup, injection or network.
. "$PSScriptRoot\NativeDaily.Common.ps1"
$testRoot='D:\QQBot\staging\daily-launcher-offline-tests-'+[guid]::NewGuid().ToString('N')
$script:PilotStage=$testRoot; $script:DailyRoot=Join-Path $testRoot 'daily-runtime'
$null=New-Item -ItemType Directory -Path $script:DailyRoot -Force
Protect-PilotPath $testRoot; Protect-PilotPath $script:DailyRoot
$script:DailyChecks=0
function Assert-DailyTest([bool]$Condition,[string]$Name) { if (!$Condition) { throw ('failed: '+$Name) }; $script:DailyChecks++ }
function Assert-DailyThrows([scriptblock]$Action,[string]$Name) {
    $failed=$false; try { & $Action } catch { $failed=$true }
    Assert-DailyTest $failed $Name
}
$good=[pscustomobject]@{enabled=$true;expected_self_id='0';private_target='0';port=16199;token=('a'*43);receipt_dir=(Join-Path $testRoot 'receipts')}
Assert-DailyConfigValue $good; $script:DailyChecks++
foreach ($change in @(@{enabled='true'},@{expected_self_id=0},@{port='16199'},@{port=16199.5},@{token=('a'*42)},@{token=('a'*129)},@{receipt_dir='relative'})) {
    $bad=$good|ConvertTo-Json|ConvertFrom-Json
    foreach ($key in $change.Keys) {$bad.$key=$change[$key]}
    Assert-DailyThrows {Assert-DailyConfigValue $bad} ('strict configuration '+@($change.Keys)[0])
}
$bad=$good|ConvertTo-Json|ConvertFrom-Json; $bad|Add-Member -NotePropertyName extra -NotePropertyValue $true
Assert-DailyThrows {Assert-DailyConfigValue $bad} 'extra configuration key'
$receipts=$good.receipt_dir; $null=New-Item -ItemType Directory -Path $receipts; Protect-PilotPath $receipts
$pending=Join-Path $receipts 'unresolved-operation-fixture.json'
[IO.File]::WriteAllText($pending,'{"state":"unknown","do_not_retry":true}')
$pendingHash=(Get-FileHash -LiteralPath $pending -Algorithm SHA256).Hash
$raw='{"pid":424242,"nonce":"0123456789abcdef0123456789abcdef"}'
$lockPath=Join-Path $receipts '.bridge-lock'; [IO.File]::WriteAllText($lockPath,$raw)
$writer=[IO.FileStream]::new($lockPath,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
try {
    $shared=Get-DailyReceiptLock $receipts
    Assert-DailyTest ($shared.Pid -eq 424242 -and $shared.Hash -eq (Get-DailyHash $raw)) 'active Node-style writer lock metadata readable without restart'
} finally {$writer.Dispose()}
$lock=Get-DailyReceiptLock $receipts
$configuration=[pscustomobject]@{Fingerprint=('b'*64);ReceiptDirectory=$receipts;Port=16199}
$binding=[pscustomobject]@{Schema=1;ConfigurationFingerprint=$configuration.Fingerprint;ReceiptDirectory=$receipts;
    NativeProcess=[pscustomobject]@{Id=424242;CreatedUtc='2026-10-26T04:00:00.0000000Z';Exe='D:\QQ\QQ.exe';Name='QQ.exe'};ReceiptLock=$lock}
Assert-DailyKnownLock $lock $binding $configuration; $script:DailyChecks++
$changed=[pscustomobject]@{Hash=('c'*64);Pid=$lock.Pid;Nonce=$lock.Nonce}
Assert-DailyThrows {Assert-DailyKnownLock $changed $binding $configuration} 'changed lock refused'
Assert-DailyThrows {Assert-DailyKnownLock $lock $null $configuration} 'missing persistent owner refused'
$other=$binding|ConvertTo-Json -Depth 7|ConvertFrom-Json; $other.NativeProcess.CreatedUtc='invalid'
Assert-DailyThrows {Assert-DailyKnownLock $lock $other $configuration} 'invalid generation refused'
# Replace all live probes before exercising lock recovery.
$script:TestPidPresent=$true; $script:TestListener=$false; $script:TestHolder=$false
function Get-PilotProcess([int]$ProcessId) { if ($script:TestPidPresent) {[pscustomobject]@{Id=$ProcessId}} }
function Get-DailyListeners { if ($script:TestListener) {[pscustomobject]@{LocalPort=16199}} }
function Get-BotHolderPids { if ($script:TestHolder) {424243} }
function Assert-DailyConfigurationUnchanged($Expected) { if ($Expected.Fingerprint -ne ('b'*64)) {throw 'test_configuration_changed'} }
Assert-DailyThrows {Move-DailyKnownExitedLock $configuration $binding} 'existing or reused PID prevents recovery'
Assert-DailyTest ([IO.File]::ReadAllText($lockPath) -eq $raw) 'refusal leaves lock untouched'
$script:TestPidPresent=$false; $script:TestListener=$true
Assert-DailyThrows {Move-DailyKnownExitedLock $configuration $binding} 'listener prevents recovery'
$script:TestListener=$false; $script:TestHolder=$true
Assert-DailyThrows {Move-DailyKnownExitedLock $configuration $binding} 'bot holder prevents recovery'
$script:TestHolder=$false
Move-DailyKnownExitedLock $configuration $binding
Assert-DailyTest (!(Test-Path -LiteralPath $lockPath)) 'known old lock moved'
$retired=@(Get-ChildItem -LiteralPath (Join-Path $script:DailyRoot 'retired-locks') -File | Where-Object Name -NotLike '*.audit.json')
Assert-DailyTest ($retired.Count -eq 1 -and [IO.File]::ReadAllText($retired[0].FullName) -eq $raw) 'quarantine retains exact lock bytes'
Assert-DailyTest ((Get-FileHash -LiteralPath $pending -Algorithm SHA256).Hash -eq $pendingHash) 'unknown receipt unchanged'
$statePath=Join-Path $script:DailyRoot 'atomic-state.json'
Write-DailyJson $statePath @{Sequence=1;Process=$binding.NativeProcess}
Write-DailyJson $statePath @{Sequence=2;Process=$binding.NativeProcess}
$state=Read-PilotJson $statePath
Assert-DailyTest ($state.Sequence -eq 2 -and (Test-PilotProcessIdentity $state.Process $binding.NativeProcess)) 'atomic state retains process generation'
Assert-DailyTest (@(Get-ChildItem -LiteralPath $script:DailyRoot -File -Filter '*.new-*').Count -eq 0) 'atomic writes leave no temporary file'
$row=[pscustomobject]@{ProcessId=42;ParentProcessId=11;ExecutablePath='D:\QQ\QQ.exe';CreationDate=[datetime]'2026-10-26T04:00:00Z';Name='QQ.exe'}
$expected=Convert-DailyCimIdentity $row
$same=$expected|ConvertTo-Json|ConvertFrom-Json
Assert-DailyCandidateRefresh $expected $same; $script:DailyChecks++
$reused=$expected|ConvertTo-Json|ConvertFrom-Json; $reused.CreatedUtc='2026-10-26T04:01:00Z'; $reused.Exe='D:\Other\QQ.exe'; $reused.Parent=99
Assert-DailyThrows {Assert-DailyCandidateRefresh $expected $reused} 'PID reuse cannot become cleanup candidate'
Assert-DailyTest ($expected.Exe -eq 'D:\QQ\QQ.exe' -and $expected.Parent -eq 11) 'original filtered identity remains unchanged'
$pendingValue=[pscustomobject]@{Schema=1;Active=$true;StartId='20261026-122046-40bd601d';ConfigurationFingerprint=$configuration.Fingerprint;
    ReceiptDirectory=$receipts;NativeProcess=$expected}
Assert-DailyPendingValue $pendingValue $configuration; $script:DailyChecks++
foreach($change in @(@{Active='true'},@{ConfigurationFingerprint=('f'*64)},@{ReceiptDirectory='D:\Other\receipts'},@{StartId='old-unsafe-id'})) {
    $bad=$pendingValue|ConvertTo-Json -Depth 8|ConvertFrom-Json
    foreach($key in $change.Keys){$bad.$key=$change[$key]}
    Assert-DailyThrows {Assert-DailyPendingValue $bad $configuration} ('pending binding '+@($change.Keys)[0])
}
Write-Output ($script:DailyChecks.ToString()+' offline daily-launch rules passed. Fixture directory: '+$testRoot)
