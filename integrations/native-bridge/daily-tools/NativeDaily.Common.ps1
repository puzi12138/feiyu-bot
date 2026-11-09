# Offline daily-start candidate. Dot-sourcing defines helpers only.
. "$PSScriptRoot\NativePilot.Common.ps1"
$script:DailyRoot=Join-Path $script:PilotStage 'daily-runtime'

function Get-DailyHash([string]$Text) {
    $sha=[System.Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Convert-DailyCimIdentity($Row) {
    [pscustomobject]@{Id=[int]$Row.ProcessId;Parent=[int]$Row.ParentProcessId;Exe=[string]$Row.ExecutablePath;
        CreatedUtc=$Row.CreationDate.ToUniversalTime().ToString('o');Name=[string]$Row.Name}
}
function Assert-DailyCandidateRefresh($Expected,$Refreshed) {
    if (!(Test-PilotProcessIdentity $Refreshed $Expected) -or $Refreshed.Parent -ne $Expected.Parent -or $Refreshed.Name -ne $Expected.Name) { throw 'daily_candidate_generation_changed' }
}
function Get-DailyFailureInfo($ErrorRecord,[string]$Stage) {
    $value=Get-PilotFailureInfo $ErrorRecord $Stage
    if ($value.ErrorCode -eq 'unclassified_failure' -and $ErrorRecord.Exception.Message -match '^daily_[a-z0-9_]+$') { $value.ErrorCode=$ErrorRecord.Exception.Message }
    $value
}
function Assert-DailyPendingValue($Value,$Configuration) {
    if (!$Value -or $Value.Schema -ne 1 -or $Value.Active -isnot [bool] -or
        $Value.ConfigurationFingerprint -ne $Configuration.Fingerprint -or $Value.ReceiptDirectory -ne $Configuration.ReceiptDirectory -or
        $Value.StartId -notmatch '^[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$' -or !$Value.NativeProcess -or
        $Value.NativeProcess.Name -ne 'QQ.exe' -or ![IO.Path]::IsPathFullyQualified([string]$Value.NativeProcess.Exe)) { throw 'daily_pending_state_invalid' }
    try { $created=[datetimeoffset]$Value.NativeProcess.CreatedUtc; if ($created.UtcDateTime.Ticks -le 0) {throw 'invalid'} }
    catch {throw 'daily_pending_state_invalid'}
}
function Write-DailyPending($Process,$Configuration,[string]$StartId,[bool]$Active,[string]$Phase) {
    Write-DailyJson (Join-Path $script:DailyRoot 'pending.json') @{Schema=1;Active=$Active;StartId=$StartId;
        UpdatedUtc=(Get-Date).ToUniversalTime().ToString('o');ConfigurationFingerprint=$Configuration.Fingerprint;
        ReceiptDirectory=$Configuration.ReceiptDirectory;NativeProcess=$Process;Phase=$Phase;UnknownMessagesRetried=$false}
}
function Wait-DailyNativeReady($Process,$Configuration,[int]$Seconds) {
    $deadline=(Get-Date).AddSeconds($Seconds)
    do {
        $null=Assert-PilotProcess $Process
        try { $null=Assert-PilotIdentityBinding $Process $Configuration.ReceiptDirectory; return $true } catch {}
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    $false
}
function Assert-DailyStagePath([string]$Path) {
    $full=[IO.Path]::GetFullPath($Path).TrimEnd('\')
    $root=[IO.Path]::GetFullPath($script:PilotStage).TrimEnd('\')
    if (!$full.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'daily_path_outside_stage' }
    for ($current=$full; $current -and $current.Length -ge $root.Length; $current=Split-Path $current -Parent) {
        if ((Test-Path -LiteralPath $current) -and ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'daily_reparse_path_rejected' }
        if ($current -eq $root) { break }
    }
    $full
}
function Write-DailyJson([string]$Path,$Value) {
    $null=Assert-DailyStagePath $Path
    $temporary=$Path+'.new-'+[guid]::NewGuid().ToString('N')
    $backup=$Path+'.old-'+[guid]::NewGuid().ToString('N')
    $stream=$null
    try {
        $stream=[IO.FileStream]::new($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        $bytes=[Text.Encoding]::UTF8.GetBytes(($Value|ConvertTo-Json -Depth 16))
        $stream.Write($bytes,0,$bytes.Length); $stream.Flush($true); $stream.Dispose(); $stream=$null
        Protect-PilotPath $temporary
        # PowerShell's binder converts a null backup path to an empty string
        # on this runtime; a same-directory backup keeps Replace atomic.
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary,$Path,$backup) } else { [IO.File]::Move($temporary,$Path) }
        Assert-PilotPrivateAcl $Path
    } finally {
        if ($stream) { $stream.Dispose() }
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
        if ([IO.File]::Exists($backup)) { [IO.File]::Delete($backup) }
    }
}
function Assert-DailyConfigValue($Value) {
    $allowed=@('enabled','expected_self_id','private_target','port','token','receipt_dir')
    $names=@($Value.PSObject.Properties.Name)
    $numeric=($Value.port -is [int] -or $Value.port -is [long] -or $Value.port -is [double] -or $Value.port -is [decimal])
    if ($names.Count -ne 6 -or @($names|Where-Object {$_ -notin $allowed}).Count -or
        $Value.enabled -isnot [bool] -or !$Value.enabled -or $Value.expected_self_id -isnot [string] -or $Value.expected_self_id -ne '0' -or
        $Value.private_target -isnot [string] -or $Value.private_target -ne '0' -or !$numeric -or $Value.port -ne 16199 -or
        $Value.token -isnot [string] -or $Value.token -notmatch '^[A-Za-z0-9_-]{43,128}$' -or
        $Value.receipt_dir -isnot [string] -or ![IO.Path]::IsPathFullyQualified($Value.receipt_dir)) { throw 'daily_bridge_configuration_invalid' }
}
function Get-DailyConfiguration {
    $path=Join-Path $script:PilotStage 'config\feiyu-native-bridge.json'
    $null=Assert-DailyStagePath $path; Assert-PilotPrivateAcl $path
    $value=Read-PilotJson $path
    Assert-DailyConfigValue $value
    $receipts=Assert-DailyStagePath $value.receipt_dir
    if (!(Test-Path -LiteralPath $receipts -PathType Container)) { throw 'daily_receipts_missing' }
    Assert-PilotPrivateAcl $receipts
    # This public fingerprint binds the existing key without copying it into
    # run state. Key rotation requires deliberate migration, never a restart.
    $projection='0|0|16199|'+$receipts+'|'+(Get-DailyHash $value.token)
    $value=$null
    [pscustomobject]@{Fingerprint=(Get-DailyHash $projection);ReceiptDirectory=$receipts;Port=16199}
}
function Assert-DailyConfigurationUnchanged($Expected) {
    $current=Get-DailyConfiguration
    if ($current.Fingerprint -ne $Expected.Fingerprint -or $current.ReceiptDirectory -ne $Expected.ReceiptDirectory) { throw 'daily_configuration_changed' }
}
function Get-DailyListeners {
    # Query errors must not masquerade as an unused port.
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object LocalPort -eq 16199)
}
function Read-DailySharedLockText([string]$Path) {
    $stream=$null
    try {
        # Node keeps the wx descriptor open for the lifetime of the bridge.
        # Permit that existing writer while reading immutable lock metadata.
        $stream=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        $buffer=New-Object byte[] 1025; $count=0
        do {
            $read=$stream.Read($buffer,$count,$buffer.Length-$count); $count+=$read
            if ($count -gt 1024) { throw 'daily_receipt_lock_too_large' }
        } while ($read -gt 0)
        [Text.UTF8Encoding]::new($false,$true).GetString($buffer,0,$count)
    } finally {if($stream){$stream.Dispose()}}
}
function Get-DailyReceiptLock([string]$Directory) {
    $path=Join-Path $Directory '.bridge-lock'
    if (!(Test-Path -LiteralPath $path)) { return $null }
    $item=Get-Item -LiteralPath $path -Force
    if ($item.PSIsContainer -or $item.Length -gt 1024 -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'daily_receipt_lock_invalid' }
    $raw=Read-DailySharedLockText $path
    $value=$raw|ConvertFrom-Json
    if (@($value.PSObject.Properties).Count -ne 2 -or [string]$value.pid -notmatch '^[1-9][0-9]{0,8}$' -or $value.nonce -notmatch '^[0-9a-f]{32}$') { throw 'daily_receipt_lock_invalid' }
    [pscustomobject]@{Hash=(Get-DailyHash $raw);Pid=[int]$value.pid;Nonce=[string]$value.nonce}
}
function Assert-DailyKnownLock($Lock,$Binding,$Configuration) {
    if (!$Lock -or !$Binding -or $Binding.Schema -ne 1 -or $Binding.ConfigurationFingerprint -ne $Configuration.Fingerprint -or
        $Binding.ReceiptDirectory -ne $Configuration.ReceiptDirectory -or !$Binding.NativeProcess -or !$Binding.ReceiptLock -or
        $Binding.NativeProcess.Id -ne $Lock.Pid -or $Binding.ReceiptLock.Hash -ne $Lock.Hash -or $Binding.ReceiptLock.Nonce -ne $Lock.Nonce) { throw 'daily_receipt_lock_not_previously_bound' }
    if ($Binding.NativeProcess.Name -ne 'QQ.exe' -or ![IO.Path]::IsPathFullyQualified([string]$Binding.NativeProcess.Exe)) { throw 'daily_receipt_lock_generation_invalid' }
    try { $created=[datetimeoffset]$Binding.NativeProcess.CreatedUtc; if ($created.UtcDateTime.Ticks -le 0) { throw 'invalid' } }
    catch { throw 'daily_receipt_lock_generation_invalid' }
}
function Save-DailyBinding($Process,$Configuration,[string]$StartId) {
    $null=Assert-PilotIdentityBinding $Process $Configuration.ReceiptDirectory
    Assert-DailyConfigurationUnchanged $Configuration
    $lock=Get-DailyReceiptLock $Configuration.ReceiptDirectory
    if (!$lock -or $lock.Pid -ne $Process.Id) { throw 'daily_active_lock_not_bound' }
    $binding=[ordered]@{Schema=1;StartId=$StartId;BoundUtc=(Get-Date).ToUniversalTime().ToString('o');
        ConfigurationFingerprint=$Configuration.Fingerprint;ReceiptDirectory=$Configuration.ReceiptDirectory;
        NativeProcess=$Process;ReceiptLock=$lock;NoHistoricalReplay=$true}
    Write-DailyJson (Join-Path $script:DailyRoot 'binding.json') $binding
    $binding
}
function Move-DailyKnownExitedLock($Configuration,$Binding) {
    $lock=Get-DailyReceiptLock $Configuration.ReceiptDirectory
    if (!$lock) { return }
    Assert-DailyKnownLock $lock $Binding $Configuration
    # Conservatively refuse even a reused PID. No action targets a process
    # inferred from lock.pid; recovery only moves the one matched file.
    if (Get-PilotProcess $lock.Pid) { throw 'daily_lock_pid_still_present' }
    if (@(Get-DailyListeners).Count -or @(Get-BotHolderPids).Count) { throw 'daily_lock_recovery_runtime_conflict' }
    Assert-DailyConfigurationUnchanged $Configuration
    $again=Get-DailyReceiptLock $Configuration.ReceiptDirectory
    Assert-DailyKnownLock $again $Binding $Configuration
    if ($again.Hash -ne $lock.Hash -or (Get-PilotProcess $lock.Pid)) { throw 'daily_lock_recovery_race' }
    $archive=Join-Path $script:DailyRoot 'retired-locks'
    if (!(Test-Path -LiteralPath $archive)) { $null=New-Item -ItemType Directory -Path $archive }
    Protect-PilotPath $archive
    $destination=Join-Path $archive ((Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N')+'.json')
    if (@(Get-DailyListeners).Count -or @(Get-BotHolderPids).Count -or (Get-PilotProcess $lock.Pid)) { throw 'daily_lock_recovery_runtime_changed' }
    # Move within the same D: volume; retain the original bytes for audit.
    [IO.File]::Move((Join-Path $Configuration.ReceiptDirectory '.bridge-lock'),$destination)
    $moved=[IO.File]::ReadAllText($destination)
    if ((Get-DailyHash $moved) -ne $lock.Hash -or (Test-Path -LiteralPath (Join-Path $Configuration.ReceiptDirectory '.bridge-lock')) -or
        @(Get-DailyListeners).Count -or @(Get-BotHolderPids).Count) { throw 'daily_lock_quarantine_readback_changed' }
    Write-DailyJson ($destination+'.audit.json') @{Schema=1;RecoveredUtc=(Get-Date).ToUniversalTime().ToString('o');
        OldProcess=$Binding.NativeProcess;LockHash=$lock.Hash;ReceiptDirectory=$Configuration.ReceiptDirectory;ReceiptsModified=$false;UnknownMessagesRetried=$false}
}
