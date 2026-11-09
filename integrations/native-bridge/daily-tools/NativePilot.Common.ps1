Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:PilotStage = 'D:\QQBot\staging\napcat-trusted-native-bridge-review-v4.18.41'
$script:BotRoot = 'D:\QQBot'
$script:PilotEventPath = $null

function Write-PilotEvent([string]$Stage, [string]$Code, [int]$ProcessId=0) {
    if ($Stage -notmatch '^[a-z0-9_]+$' -or $Code -notmatch '^[a-z0-9_]+$') { throw 'Invalid internal pilot event.' }
    if (!$script:PilotEventPath) { return }
    $value=@{Utc=(Get-Date).ToUniversalTime().ToString('o');Stage=$Stage;Code=$Code;ProcessId=$ProcessId}
    [System.IO.File]::AppendAllText($script:PilotEventPath, ($value|ConvertTo-Json -Compress)+[Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
}
function Throw-PilotFault([string]$Stage, [string]$Code, [int]$ProcessId=0) {
    Write-PilotEvent $Stage $Code $ProcessId
    $fault=[InvalidOperationException]::new($Code)
    $fault.Data['PilotStage']=$Stage; $fault.Data['PilotCode']=$Code
    throw $fault
}
function Get-PilotFailureInfo($ErrorRecord, [string]$FallbackStage) {
    $exception=$ErrorRecord.Exception
    $stage=$FallbackStage; $code='unclassified_failure'
    for ($current=$exception; $null -ne $current; $current=$current.InnerException) {
        if ($current.Data.Contains('PilotCode')) { $code=[string]$current.Data['PilotCode']; $stage=[string]$current.Data['PilotStage']; break }
    }
    [pscustomobject]@{FailureType=$exception.GetType().Name;Stage=$stage;ErrorCode=$code}
}

function Write-PilotJson([string]$Path, $Value) {
    [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 14), [System.Text.UTF8Encoding]::new($false))
}
function Read-PilotJson([string]$Path) { Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
function Get-PilotPrivateSids { @([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544') }
function Test-PilotPrivateAcl([string]$Path) {
    $item=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $false }
    $acl=Get-Acl -LiteralPath $Path -ErrorAction Stop
    if (!$acl.AreAccessRulesProtected) { return $false }
    $allowed=@(Get-PilotPrivateSids)
    $owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin $allowed) { return $false }
    $rules=@($acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne $allowed.Count) { return $false }
    foreach ($rule in $rules) {
        if ($rule.IdentityReference.Value -notin $allowed -or $rule.AccessControlType -ne 'Allow' -or $rule.IsInherited -or
            $rule.FileSystemRights -ne [System.Security.AccessControl.FileSystemRights]::FullControl -or $rule.PropagationFlags -ne 'None') { return $false }
        $expectedInheritance=if ($item.PSIsContainer) { [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' } else { [System.Security.AccessControl.InheritanceFlags]::None }
        if ($rule.InheritanceFlags -ne $expectedInheritance) { return $false }
    }
    @($rules.IdentityReference.Value | Sort-Object -Unique).Count -eq $allowed.Count
}
function Assert-PilotPrivateAcl([string]$Path) {
    if (!(Test-PilotPrivateAcl $Path)) { Throw-PilotFault 'storage_acl_verify' 'private_acl_not_verified' }
}
function Protect-PilotPath([string]$Path) {
    if (Test-PilotPrivateAcl $Path) { Write-PilotEvent 'storage_acl' 'existing_private_acl_reused'; return }
    $item=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { Throw-PilotFault 'storage_acl' 'reparse_path_rejected' }
    # Fresh security objects carry only DACL changes. Never submit existing
    # owner/group/audit descriptors via Set-Acl, which can require privileges.
    $isDirectory=$item.PSIsContainer
    $acl=if ($isDirectory) { [System.Security.AccessControl.DirectorySecurity]::new() } else { [System.Security.AccessControl.FileSecurity]::new() }
    $acl.SetAccessRuleProtection($true, $false)
    $identities=@(Get-PilotPrivateSids)
    foreach ($sid in $identities) {
        $identity = [System.Security.Principal.SecurityIdentifier]::new($sid)
        if ($isDirectory) {
            $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        } else { $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow') }
        $acl.AddAccessRule($rule)
    }
    try {
        if ($isDirectory) { [System.IO.FileSystemAclExtensions]::SetAccessControl([System.IO.DirectoryInfo]::new($Path),$acl) }
        else { [System.IO.FileSystemAclExtensions]::SetAccessControl([System.IO.FileInfo]::new($Path),$acl) }
    } catch {
        $_.Exception.Data['PilotStage']='storage_acl_write'; $_.Exception.Data['PilotCode']='private_dacl_write_failed'
        throw
    }
    Assert-PilotPrivateAcl $Path
    Write-PilotEvent 'storage_acl' 'private_dacl_written_and_verified'
}
function Test-PilotDirectoryWrite([string]$Directory) {
    $file=Join-Path $Directory ('.storage-probe-'+[guid]::NewGuid().ToString('N'))
    $bytes=[System.Text.Encoding]::UTF8.GetBytes('feiyu-native-pilot-storage-probe')
    $stream=$null
    try {
        $stream=[System.IO.FileStream]::new($file,[System.IO.FileMode]::CreateNew,[System.IO.FileAccess]::Write,[System.IO.FileShare]::None)
        $stream.Write($bytes,0,$bytes.Length); $stream.Flush($true); $stream.Dispose(); $stream=$null
        if ([System.IO.File]::ReadAllText($file) -ne 'feiyu-native-pilot-storage-probe') { throw 'storage_probe_readback_failed' }
    } finally {
        if ($stream) { $stream.Dispose() }
        if ([System.IO.File]::Exists($file)) { [System.IO.File]::Delete($file) }
    }
}
function Initialize-PilotStorage($Run, [switch]$EnableConfig) {
    $phase='storage_run_acl'; $configFile="$script:PilotStage\config\feiyu-native-bridge.json"
    try {
        Protect-PilotPath $Run.Path
        $phase='storage_config_acl'; Protect-PilotPath $configFile
        $phase='storage_receipt_acl'; $receipts=Join-Path $Run.Path 'receipts'
        if (!(Test-Path -LiteralPath $receipts)) { $null=New-Item -ItemType Directory -Path $receipts }
        Protect-PilotPath $receipts
        $phase='storage_directory_write_probe'; Test-PilotDirectoryWrite $Run.Path; Test-PilotDirectoryWrite $receipts
        $phase='storage_token_create'
        $bytes=New-Object byte[] 32; $rng=[System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $token=[Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
        $config=@{enabled=[bool]$EnableConfig;expected_self_id='0';private_target='0';port=16199;token=$token;receipt_dir=$receipts}
        $phase='storage_config_write'; Write-PilotJson $configFile $config
        $phase='storage_config_readback'; $observed=Read-PilotJson $configFile
        if ($observed.token -ne $token -or $observed.enabled -ne [bool]$EnableConfig -or $observed.receipt_dir -ne $receipts) { throw 'config_readback_mismatch' }
        Assert-PilotPrivateAcl $configFile
        if (!$EnableConfig) { $phase='storage_config_disable'; Disable-PilotBridge }
        $token=$null; $bytes=$null; $config=$null; $observed=$null
        Write-PilotJson (Join-Path $Run.Path 'storage-preflight.json') @{RunId=$Run.Plan.RunId;VerifiedUtc=(Get-Date).ToUniversalTime().ToString('o');
            PrivateRunAcl=$true;PrivateConfigAcl=$true;PrivateReceiptAcl=$true;TokenCreatedAndReadback=$true;WriteAndReadback=$true;ConfigEnabled=[bool]$EnableConfig;
            OwnerOrAuditChanged=$false;AllChecksBeforeOriginalStop=$true}
        Write-PilotEvent 'storage_preflight' 'complete'
    } catch {
        if (!$_.Exception.Data.Contains('PilotCode')) { $_.Exception.Data['PilotStage']=$phase; $_.Exception.Data['PilotCode']='storage_preflight_failed' }
        throw
    }
}
function Get-PilotProcess([int]$ProcessId) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
    if (!$p) { return $null }
    [pscustomobject]@{ Id=[int]$p.ProcessId; Parent=[int]$p.ParentProcessId; Exe=$p.ExecutablePath;
        CreatedUtc=$p.CreationDate.ToUniversalTime().ToString('o'); Name=$p.Name }
}
function Assert-PilotProcess($Expected) {
    $actual = Get-PilotProcess $Expected.Id
    if (!(Test-PilotProcessIdentity $actual $Expected)) {
        throw 'Process identity changed; no process was terminated.'
    }
    $actual
}
function Test-PilotProcessIdentity($Actual, $Expected) {
    if (!$Actual -or !$Expected -or $Actual.Id -ne $Expected.Id -or $Actual.Exe -ne $Expected.Exe) { return $false }
    ([datetimeoffset]$Actual.CreatedUtc).UtcDateTime.Ticks -eq ([datetimeoffset]$Expected.CreatedUtc).UtcDateTime.Ticks
}
function Test-PilotProcessGeneration($Actual, $Expected) {
    if (!$Actual -or !$Expected -or $Actual.Id -ne $Expected.Id) { return $false }
    ([datetimeoffset]$Actual.CreatedUtc).UtcDateTime.Ticks -eq ([datetimeoffset]$Expected.CreatedUtc).UtcDateTime.Ticks
}
function Test-PilotProcessCompatibleSnapshot($Actual, $Expected) {
    (Test-PilotProcessGeneration $Actual $Expected) -and (!$Actual.Exe -or $Actual.Exe -eq $Expected.Exe)
}
function Open-PilotNativeProcess([int]$ProcessId) { Get-Process -Id $ProcessId -ErrorAction Stop }
function Invoke-ExactPilotProcessAction($Expected, [ValidateSet('close','kill')][string]$Action) {
    $stage=if ($Action -eq 'close') {'process_close'} else {'process_kill'}
    $before=Get-PilotProcess $Expected.Id
    if (!$before) { Write-PilotEvent $stage 'already_exited' $Expected.Id; return }
    if (!(Test-PilotProcessCompatibleSnapshot $before $Expected)) { Throw-PilotFault $stage 'process_identity_changed' $Expected.Id }
    if (!$before.Exe) { Write-PilotEvent $stage 'cim_path_unavailable_probe_handle_only' $Expected.Id }
    $native=$null
    try {
        $native=Open-PilotNativeProcess $Expected.Id
        # Force acquisition of the original process handle before any action.
        # Kill uses this pinned handle, never a second PID lookup.
        $null=$native.Handle
        if ($native.HasExited) { Write-PilotEvent $stage 'handle_already_exited' $Expected.Id; return }
        $delta=[Math]::Abs($native.StartTime.ToUniversalTime().Ticks-([datetimeoffset]$Expected.CreatedUtc).UtcDateTime.Ticks)
        if ($delta -ge 10) { Throw-PilotFault $stage 'process_handle_identity_changed' $Expected.Id }
        $handlePath=$native.Path
        if ($handlePath -ne $Expected.Exe) {
            if (!$handlePath -and ($native.HasExited -or $native.WaitForExit(1000))) { Write-PilotEvent $stage 'exited_while_binding_path' $Expected.Id; return }
            Throw-PilotFault $stage 'process_handle_identity_changed' $Expected.Id
        }
        $current=Get-PilotProcess $Expected.Id
        if (!$current) { Write-PilotEvent $stage 'exited_before_action' $Expected.Id; return }
        if (!(Test-PilotProcessCompatibleSnapshot $current $Expected)) { Throw-PilotFault $stage 'process_identity_changed_before_action' $Expected.Id }
        if ($native.HasExited) { Write-PilotEvent $stage 'handle_exited_before_action' $Expected.Id; return }
        if ($Action -eq 'close') { $null=$native.CloseMainWindow() } else { $native.Kill() }
        Write-PilotEvent $stage 'action_requested' $Expected.Id
    } catch {
        if ($_.Exception.Data.Contains('PilotCode')) { throw }
        $current=Get-PilotProcess $Expected.Id
        if ($current -and !(Test-PilotProcessCompatibleSnapshot $current $Expected)) { Throw-PilotFault $stage 'process_identity_changed_during_action' $Expected.Id }
        # A racing normal exit is the only ignored action exception. A still
        # live exact process retains its original exception and fixed code.
        $handleExited=$false
        if ($native) { try { $handleExited=$native.HasExited } catch {} }
        if (!$current -or $handleExited) { Write-PilotEvent $stage 'exit_race_resolved' $Expected.Id; return }
        $_.Exception.Data['PilotStage']=$stage; $_.Exception.Data['PilotCode']='process_action_failed_still_alive'
        Write-PilotEvent $stage 'process_action_failed_still_alive' $Expected.Id
        throw
    } finally { if ($native) { $native.Dispose() } }
}
function Get-BotHolderPids {
    $holderJson = & "$script:BotRoot\apps\SnowLuma\node.exe" "$script:BotRoot\scripts\FindBotQQ.cjs"
    if ($LASTEXITCODE -ne 0) { throw 'Could not identify the bot database holder without injection.' }
    @($holderJson | ConvertFrom-Json | ForEach-Object { [int]$_ })
}
function Get-SnowPilotHeaders {
    # Runtime-only access: never copy credentials into stage or print a token.
    $credential=Read-PilotJson "$script:BotRoot\config\bootstrap.json"
    try {
        $login=Invoke-RestMethod 'http://127.0.0.1:5099/api/login' -Method Post -ContentType 'application/json' -Body (@{password=$credential.snowluma_password}|ConvertTo-Json) -TimeoutSec 5
        if (!$login.success -or !$login.token) { throw 'Snow authentication was not accepted.' }
        @{ Authorization="Bearer $($login.token)" }
    } finally { $credential=$null; $login=$null }
}
function Assert-SnowPilotOnline($ExpectedProcess) {
    $null=Assert-PilotProcess $ExpectedProcess
    $headers=Get-SnowPilotHeaders
    $list=(Invoke-RestMethod 'http://127.0.0.1:5099/api/processes' -Headers $headers -TimeoutSec 5).list
    $target=@($list | Where-Object { [int]$_.pid -eq $ExpectedProcess.Id })
    if ($target.Count -ne 1 -or [string]$target[0].uin -ne '0' -or !$target[0].injected -or
        !$target[0].connected -or !$target[0].loggedIn -or $target[0].status -ne 'online') { throw 'Current Snow bot identity and online state are not healthy; it will not be stopped.' }
    [pscustomobject]@{ProcessId=$ExpectedProcess.Id;SelfId='0';Injected=$true;Connected=$true;LoggedIn=$true;Status='online'}
}
function Connect-SnowPilotProcess($ExpectedProcess, [switch]$RequireFreshInjection) {
    $null=Assert-PilotProcess $ExpectedProcess
    $headers=Get-SnowPilotHeaders
    $list=(Invoke-RestMethod 'http://127.0.0.1:5099/api/processes' -Headers $headers -TimeoutSec 5).list
    $target=@($list|Where-Object { [int]$_.pid -eq $ExpectedProcess.Id })
    if ($target.Count -ne 1) { throw 'Snow did not identify the exact expected QQ main process.' }
    if ($RequireFreshInjection -and $target[0].injected) { throw 'Snow injected before the controlled load step; pilot stopped without a test message.' }
    if (!$target[0].injected) {
        $result=Invoke-RestMethod "http://127.0.0.1:5099/api/processes/$($ExpectedProcess.Id)/load" -Method Post -ContentType 'application/json' -Body '{}' -Headers $headers -TimeoutSec 15
        if (!$result.success) { throw 'Snow module load was not accepted.' }
    }
    $deadline=(Get-Date).AddSeconds(25)
    do {
        $null=Assert-PilotProcess $ExpectedProcess
        $list=(Invoke-RestMethod 'http://127.0.0.1:5099/api/processes' -Headers $headers -TimeoutSec 5).list
        $target=@($list|Where-Object { [int]$_.pid -eq $ExpectedProcess.Id })
        if ($target.Count -ne 1) { throw 'Snow lost the selected process.' }
        $uin=[string]$target[0].uin
        if ($uin -and $uin -ne '0' -and $uin -ne '0') { throw 'Snow reports a different account; rollback is required.' }
        if ($target[0].injected -and $target[0].connected -and $target[0].loggedIn -and $uin -eq '0' -and $target[0].status -eq 'online') {
            return [pscustomobject]@{ProcessId=$ExpectedProcess.Id;Uin=$uin;Injected=$true;Connected=$true;LoggedIn=$true}
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw 'Snow same-process online identity was not confirmed.'
}
function Get-PilotTree($RootProcess) {
    $rootNow=Get-PilotProcess $RootProcess.Id
    if (!$rootNow) { Write-PilotEvent 'tree_snapshot' 'root_already_exited' $RootProcess.Id; return }
    if (!(Test-PilotProcessCompatibleSnapshot $rootNow $RootProcess)) { Throw-PilotFault 'tree_snapshot' 'root_identity_changed' $RootProcess.Id }
    if ($RootProcess.Name -notmatch '^[A-Za-z0-9_.-]+$') { Throw-PilotFault 'tree_snapshot' 'unexpected_process_name' }
    $all = @(Get-CimInstance Win32_Process -Filter "Name='$($RootProcess.Name)'")
    $ids = @([int]$RootProcess.Id); $out = @($RootProcess)
    do {
        $new = @($all | Where-Object { $_.ParentProcessId -in $ids -and $_.ProcessId -notin $ids -and $_.ExecutablePath -eq $RootProcess.Exe })
        foreach ($p in $new) {
            $expected=[pscustomobject]@{Id=[int]$p.ProcessId;Parent=[int]$p.ParentProcessId;Exe=$p.ExecutablePath;CreatedUtc=$p.CreationDate.ToUniversalTime().ToString('o');Name=$p.Name}
            $child=Get-PilotProcess $p.ProcessId
            if (!$child) { continue }
            if (!(Test-PilotProcessGeneration $child $expected)) { Write-PilotEvent 'tree_snapshot' 'child_generation_changed_no_action' $p.ProcessId; continue }
            $ids += [int]$p.ProcessId; $out += $expected
        }
        if (!$new.Count -or !@($new|Where-Object {$_.ProcessId -in $ids}).Count) { break }
    } while ($new.Count -gt 0)
    $out
}
function Stop-PilotTree($RootProcess) {
    $rootNow=Get-PilotProcess $RootProcess.Id
    if (!$rootNow) {
        Write-PilotEvent 'stop_tree' 'root_already_exited' $RootProcess.Id
        if ($RootProcess.Name -eq 'QQ.exe' -and @(Get-BotHolderPids).Count -gt 0) { Throw-PilotFault 'stop_tree' 'bot_account_still_held_after_root_exit' $RootProcess.Id }
        return
    }
    if (!(Test-PilotProcessCompatibleSnapshot $rootNow $RootProcess)) { Throw-PilotFault 'stop_tree' 'root_identity_changed' $RootProcess.Id }
    $tree = @(Get-PilotTree $RootProcess)
    Write-PilotEvent 'stop_tree' 'begin' $RootProcess.Id
    $native=$null; $rootExited=$false
    try {
        $native=Open-PilotNativeProcess $RootProcess.Id
        $null=$native.Handle
        if ($native.HasExited) { $rootExited=$true } else {
            $delta=[Math]::Abs($native.StartTime.ToUniversalTime().Ticks-([datetimeoffset]$RootProcess.CreatedUtc).UtcDateTime.Ticks)
            if ($delta -ge 10) { Throw-PilotFault 'stop_root' 'process_handle_identity_changed' $RootProcess.Id }
            $handlePath=$native.Path
            if ($handlePath -ne $RootProcess.Exe) {
                if (!$handlePath -and ($native.HasExited -or $native.WaitForExit(1000))) { $rootExited=$true }
                else { Throw-PilotFault 'stop_root' 'process_handle_identity_changed' $RootProcess.Id }
            }
            # Keep this exact handle across close, wait and the single possible
            # force stop. No CIM path lookup occurs during process teardown.
            if (!$rootExited) {
                try { $null=$native.CloseMainWindow(); Write-PilotEvent 'stop_root' 'close_requested' $RootProcess.Id }
                catch { if (!$native.HasExited) { $_.Exception.Data['PilotStage']='stop_root_close'; $_.Exception.Data['PilotCode']='root_close_failed_still_alive'; throw } }
                $rootExited=$native.WaitForExit(8000)
                if (!$rootExited) {
                    try { $native.Kill(); Write-PilotEvent 'stop_root' 'single_kill_requested' $RootProcess.Id }
                    catch { if (!$native.HasExited) { $_.Exception.Data['PilotStage']='stop_root_kill'; $_.Exception.Data['PilotCode']='root_kill_failed_still_alive'; throw } }
                    $rootExited=$native.WaitForExit(10000)
                }
            }
        }
    } catch {
        if ($_.Exception.Data.Contains('PilotCode')) { throw }
        $current=Get-PilotProcess $RootProcess.Id
        if ($current -and !(Test-PilotProcessCompatibleSnapshot $current $RootProcess)) { Throw-PilotFault 'stop_root' 'process_generation_changed_before_binding' $RootProcess.Id }
        if (!$current -or ($native -and $native.HasExited)) { $rootExited=$true; Write-PilotEvent 'stop_root' 'exit_race_resolved' $RootProcess.Id }
        else { $_.Exception.Data['PilotStage']='stop_root'; $_.Exception.Data['PilotCode']='root_stop_failed_still_alive'; throw }
    } finally { if ($native) { $native.Dispose() } }
    if (!$rootExited) { Throw-PilotFault 'stop_root' 'root_exit_not_confirmed' $RootProcess.Id }
    Write-PilotEvent 'stop_root' 'original_handle_exit_confirmed' $RootProcess.Id
    # Children receive no close/kill at all. Wait for their natural shutdown;
    # a missing executable path is an exiting/unverifiable row, not PID reuse.
    $children=@($tree | Where-Object {$_.Id -ne $RootProcess.Id} | Sort-Object Id -Unique)
    $deadline=(Get-Date).AddSeconds(10); $remaining=@()
    do {
        $remaining=@()
        foreach ($record in $children) {
            $child=Get-PilotProcess $record.Id
            if (!$child) { continue }
            if (!(Test-PilotProcessGeneration $child $record)) { Write-PilotEvent 'wait_children' 'reused_pid_ignored_no_action' $record.Id; continue }
            $remaining+=$record
        }
        if (!$remaining.Count) { break }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    if ($remaining.Count) { Throw-PilotFault 'wait_children' 'descendants_not_exited_no_kill' $RootProcess.Id }
    if ($RootProcess.Name -eq 'QQ.exe' -and @(Get-BotHolderPids).Count -gt 0) {
        Throw-PilotFault 'stop_tree' 'bot_account_still_held_after_root_exit' $RootProcess.Id
    }
    Write-PilotEvent 'stop_tree' 'complete' $RootProcess.Id
}
function Assert-PilotStage {
    $manifest = Read-PilotJson "$script:PilotStage\REVIEW-STATUS.json"
    $media = $manifest.mode -eq 'UNLAUNCHED_DISABLED_OFFLINE_MEDIA_REVIEW'
    if (($manifest.mode -ne 'UNLAUNCHED_DISABLED_OFFLINE_REVIEW' -and !$media) -or
        $manifest.production_modified -isnot [bool] -or $manifest.production_modified) { throw 'Unreviewed stage manifest.' }
    if ($media) {
        if ($manifest.protocol_version -ne 2 -or $manifest.media_manifest_schema -ne 1 -or
            $manifest.enabled_voice -isnot [bool] -or $manifest.enabled_voice) { throw 'Unreviewed media protocol.' }
        $reviewed = @{
            'trusted_native_bridge.mjs'='3620156e7007d7e6da10bc7651ea332d11b156a42d5d993fd88aaa1af2833ad9'
            'trusted_native_media.mjs'='7b3ec256a8618e8e6bc6edc534d569615be4bea6d161bac743658b36c0adcf67'
            'trusted_native_manifest.mjs'='7df8b04cb7008fa068782f12cd6118f07f71ac798e570e279abb07515aabd637'
            'trusted_native_voice.mjs'='f9622ab924633d32d1265a2e22e104600b76bd616fa9efaa82d37f321c6a3647'
            'Assert-NativeMediaSpool.ps1'='52bc833e8eaf1bbb90c04ea49037cdd3b7fc553b3edf1c4e5aaf88cff60db0d0'
        }
        $modules = $manifest.native_modules_sha256
        if (!$modules -or @($modules.PSObject.Properties.Name).Count -ne $reviewed.Count) { throw 'Incomplete reviewed media modules.' }
        foreach ($name in $reviewed.Keys) {
            if ($name -cnotin @($modules.PSObject.Properties.Name) -or $modules.$name -cne $reviewed[$name]) { throw 'Unreviewed media module hash.' }
            $actual = (Get-FileHash -LiteralPath (Join-Path $script:PilotStage $name) -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -cne $reviewed[$name]) { throw 'Reviewed media module changed.' }
        }
    }
    # Only this named, reviewed lifecycle variant can replace the official JS loader.
    # The original media/bridge checks above and binary checks below remain active.
    $variantProperty = $manifest.PSObject.Properties['lifecycle_variant']
    $diagnostic = $null -ne $variantProperty
    $diagnosticHashes = @{
        'napcat.mjs'='69840e44705938ebd7ced190759561ac0e1c21715736e579de2a7f5daf032d22'
        'nativeLoader.cjs'='23b166ec96ce72923e4fd4f655a86f026aead88a4caa80e0919dfcf2dec94d9f'
        'qq_lifecycle.cjs'='d385c5c0744b29949294690f9036fbd99869eacb1c8aa299cdb56caf7eb9fcde'
    }
    if ($diagnostic) {
        $variant = $variantProperty.Value
        if (!$media -or $manifest.mode -isnot [string] -or $manifest.mode -cne 'UNLAUNCHED_DISABLED_OFFLINE_MEDIA_REVIEW' -or
            $null -eq $variant -or $variant -isnot [pscustomobject]) { throw 'Unreviewed lifecycle variant.' }
        $variantNames = @($variant.PSObject.Properties.Name)
        if ($variantNames.Count -ne 3 -or 'id' -cnotin $variantNames -or 'schema' -cnotin $variantNames -or
            'modules_sha256' -cnotin $variantNames) { throw 'Unreviewed lifecycle variant fields.' }
        if ($variant.id -isnot [string] -or $variant.id -cne 'native-framework-lifecycle-20261026-v1' -or
            ($variant.schema -isnot [int] -and $variant.schema -isnot [long]) -or $variant.schema -ne 1) { throw 'Unreviewed lifecycle variant.' }
        $variantModules = $variant.modules_sha256
        if ($null -eq $variantModules -or $variantModules -isnot [pscustomobject] -or
            @($variantModules.PSObject.Properties.Name).Count -ne $diagnosticHashes.Count) { throw 'Incomplete reviewed lifecycle modules.' }
        foreach ($name in $diagnosticHashes.Keys) {
            if ($name -cnotin @($variantModules.PSObject.Properties.Name) -or $variantModules.$name -isnot [string] -or
                $variantModules.$name -cne $diagnosticHashes[$name]) { throw 'Unreviewed lifecycle module hash.' }
            $actual = (Get-FileHash -LiteralPath (Join-Path $script:PilotStage $name) -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -cne $diagnosticHashes[$name]) { throw 'Reviewed lifecycle module changed.' }
        }
        if ($manifest.patched_bundle_sha256 -isnot [string] -or
            $manifest.patched_bundle_sha256 -cne $diagnosticHashes['napcat.mjs']) { throw 'Lifecycle bundle manifest mismatch.' }
    }
    foreach ($entry in @(@('napcat.mjs','patched_bundle_sha256'), @('trusted_native_bridge.mjs','bridge_module_sha256'))) {
        $actual = (Get-FileHash -LiteralPath (Join-Path $script:PilotStage $entry[0]) -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $manifest.($entry[1])) { throw 'Reviewed stage hash mismatch.' }
    }
    foreach ($file in @('napimain.exe','napiloader.dll','nativeLoader.cjs')) {
        if (!(Test-Path -LiteralPath (Join-Path $script:PilotStage $file) -PathType Leaf)) { throw 'Stage loader missing.' }
        $reviewInput="D:\QQBot\downloads\qq-native-sync-review\napcat-v4.18.41\NapCat.Framework-review\$file"
        $expectedLoaderHash = if ($diagnostic -and $file -ceq 'nativeLoader.cjs') {
            $diagnosticHashes['nativeLoader.cjs']
        } else {
            (Get-FileHash -LiteralPath $reviewInput -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        $actualLoaderHash = (Get-FileHash -LiteralPath (Join-Path $script:PilotStage $file) -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualLoaderHash -cne $expectedLoaderHash) { throw 'Stage loader differs from the reviewed official input or exact lifecycle variant.' }
    }
}
function Assert-PilotRun([string]$RunDirectory) {
    $resolved = (Resolve-Path -LiteralPath $RunDirectory).Path.TrimEnd('\')
    $allowed = (Join-Path $script:PilotStage 'pilot-runs').TrimEnd('\') + '\'
    if (!$resolved.StartsWith($allowed, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Run directory is outside the isolated stage.' }
    $plan = Read-PilotJson (Join-Path $resolved 'plan.json')
    if ($plan.ExpectedSelf -ne '0' -or $plan.OwnerTarget -ne '0' -or $plan.Stage -ne $script:PilotStage) { throw 'Unexpected pilot plan.' }
    [pscustomobject]@{ Path=$resolved; Plan=$plan }
}
function Disable-PilotBridge {
    $path = "$script:PilotStage\config\feiyu-native-bridge.json"
    $phase='storage_config_disable_write'
    try {
        Write-PilotJson $path @{enabled=$false;expected_self_id='0';private_target='0';port=16199;token='';receipt_dir="$script:PilotStage\bridge-receipts"}
        $phase='storage_config_disable_acl_verify'; Assert-PilotPrivateAcl $path
        $phase='storage_config_disable_readback'; $observed=Read-PilotJson $path
        if ($observed.enabled -or $observed.token) { Throw-PilotFault $phase 'disabled_config_readback_failed' }
    } catch {
        if (!$_.Exception.Data.Contains('PilotCode')) { $_.Exception.Data['PilotStage']=$phase; $_.Exception.Data['PilotCode']='disabled_config_cleanup_failed' }
        throw
    }
}
function Get-PilotBridgeStatus {
    $config = Read-PilotJson "$script:PilotStage\config\feiyu-native-bridge.json"
    if (!$config.enabled -or !$config.token) { throw 'Pilot bridge is disabled.' }
    $result = Invoke-RestMethod "http://127.0.0.1:$($config.port)/v1/status" -Headers @{ Authorization="Bearer $($config.token)" } -TimeoutSec 3
    if (!$result.ok -or $result.data.self_id -ne '0' -or !$result.data.online) { throw 'Pilot identity is not the expected bot.' }
    $result.data
}
function Assert-PilotIdentityBinding($ExpectedProcess, [string]$ReceiptDirectory) {
    $null=Assert-PilotProcess $ExpectedProcess
    $config=Read-PilotJson "$script:PilotStage\config\feiyu-native-bridge.json"
    if ($config.receipt_dir -ne $ReceiptDirectory) { throw 'Active bridge belongs to another isolated run.' }
    $ports=@(Get-NetTCPConnection -State Listen -LocalPort $config.port -ErrorAction SilentlyContinue)
    if ($ports.Count -ne 1 -or $ports[0].LocalAddress -ne '127.0.0.1' -or $ports[0].OwningProcess -ne $ExpectedProcess.Id) { throw 'Native listener is not owned by the exact pilot QQ PID.' }
    $holders=@(Get-BotHolderPids)
    if ($holders.Count -ne 1 -or $holders[0] -ne $ExpectedProcess.Id) { throw 'Pilot is not the unique bot database holder.' }
    $wrapper=@((Get-Process -Id $ExpectedProcess.Id -ErrorAction Stop).Modules | Where-Object { $_.ModuleName -eq 'wrapper.node' })
    $expectedWrapper=Join-Path (Split-Path $ExpectedProcess.Exe -Parent) 'versions\9.9.36-53644\resources\app\wrapper.node'
    if ($wrapper.Count -ne 1 -or $wrapper[0].FileName -ne $expectedWrapper) { throw 'Loaded native QQ wrapper is not the reviewed runtime under the configured QQ installation.' }
    $status=Get-PilotBridgeStatus
    if ($status.transport -ne 'framework_real_session' -or $status.receive_subscriptions -ne 0) { throw 'Unexpected native bridge mode.' }
    [pscustomobject]@{ProcessId=$ExpectedProcess.Id;SelfId=$status.self_id;Online=$status.online;WrapperPath=$wrapper[0].FileName;PortOwnerVerified=$true;BotHolderVerified=$true}
}
