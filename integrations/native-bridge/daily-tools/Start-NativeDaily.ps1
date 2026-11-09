param([switch]$Execute,[switch]$RestartExistingBot,[switch]$RecoverKnownExitedLock,[switch]$NoOrdinaryRecovery,[ValidateRange(10,120)][int]$LoginWaitSeconds=45)
. "$PSScriptRoot\NativeDaily.Common.ps1"
Assert-PilotStage
if (!$Execute) {
    [pscustomobject]@{Mode='PLAN';Action='adopt healthy native bot or start one fresh generation, then connect Snow to that PID';
        ReplaceExistingBot=[bool]$RestartExistingBot;RecoverKnownExitedReceiptLock=[bool]$RecoverKnownExitedLock;
        StableBridgeConfiguration=$true;StableReceiptNamespace=$true;SendsMessages=$false;ProductionFilesModified=$false}
    return
}
if (!(Test-Path -LiteralPath $script:DailyRoot)) { $null=New-Item -ItemType Directory -Path $script:DailyRoot }
$null=Assert-DailyStagePath $script:DailyRoot; Protect-PilotPath $script:DailyRoot
$mutex=$null; $launcher=$null; $launchRecord=$null; $launchAttempted=$false; $launcherExit=$null; $launchedUtc=$null
$newBot=$null; $candidates=@(); $original=$null; $stopAttempted=$false; $state=$null; $attemptPath=$null; $preserveNative=$false
try {
    # OS handle releases on command exit/crash; the empty file may persist.
    $mutex=[IO.FileStream]::new((Join-Path $script:DailyRoot '.launcher-lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    $configuration=Get-DailyConfiguration
    $client=Read-PilotJson "$script:BotRoot\config\qq-client.json"
    if ($client.expectedUin -ne '0' -or $client.arguments -or !(Test-Path -LiteralPath $client.executable -PathType Leaf)) { throw 'daily_qq_configuration_changed' }
    $startId=(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8)
    $attempts=Join-Path $script:DailyRoot 'starts'
    if (!(Test-Path -LiteralPath $attempts)) { $null=New-Item -ItemType Directory -Path $attempts }
    Protect-PilotPath $attempts
    $attemptPath=Join-Path $attempts $startId; $null=New-Item -ItemType Directory -Path $attemptPath; Protect-PilotPath $attemptPath
    $script:PilotEventPath=Join-Path $attemptPath 'process-events.jsonl'
    $state=[ordered]@{Schema=1;StartId=$startId;StartedUtc=(Get-Date).ToUniversalTime().ToString('o');Phase='preflight';
        NativeProcess=$null;NativeCandidates=@();Launcher=$null;OriginalProcess=$null;OriginalStopAttempted=$false;ConfigurationFingerprint=$configuration.Fingerprint;
        ReceiptDirectory=$configuration.ReceiptDirectory;MessagesSent=0;UnknownMessagesRetried=$false;ConfigurationChanged=$false}
    Write-DailyJson (Join-Path $attemptPath 'state.json') $state
    $prior=$null; $bindingPath=Join-Path $script:DailyRoot 'binding.json'
    if (Test-Path -LiteralPath $bindingPath) { Assert-PilotPrivateAcl $bindingPath; $prior=Read-PilotJson $bindingPath }
    if ($prior -and ($prior.ConfigurationFingerprint -ne $configuration.Fingerprint -or $prior.ReceiptDirectory -ne $configuration.ReceiptDirectory)) { throw 'daily_binding_configuration_changed' }
    $pending=$null; $pendingPath=Join-Path $script:DailyRoot 'pending.json'
    if (Test-Path -LiteralPath $pendingPath) {
        Assert-PilotPrivateAcl $pendingPath; $pending=Read-PilotJson $pendingPath
        Assert-DailyPendingValue $pending $configuration
        if ($pending.Active) {
            if ($pending.NativeProcess.Exe -ne $client.executable) {throw 'daily_pending_executable_changed'}
            $observed=Get-PilotProcess $pending.NativeProcess.Id
            if ($observed) {
                Assert-DailyCandidateRefresh $pending.NativeProcess $observed
                $newBot=$pending.NativeProcess; $preserveNative=$true
                $state.NativeProcess=$newBot; $state.Phase='resuming_exact_native_generation'
                Write-DailyJson (Join-Path $attemptPath 'state.json') $state
            } else {
                # A crashed pending process can leave an unbound receipt lock.
                # Do not replace it or hide the login/crash behind ordinary QQ.
                throw 'daily_pending_process_exited_requires_review'
            }
        }
    }
    $listeners=@(Get-DailyListeners)
    if ($listeners.Count) {
        if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') { throw 'daily_bridge_port_conflict' }
        $active=Get-PilotProcess $listeners[0].OwningProcess
        if (!$active -or $active.Exe -ne $client.executable -or $active.Name -ne 'QQ.exe') { throw 'daily_bridge_process_mismatch' }
        if ($preserveNative -and !(Test-PilotProcessIdentity $active $newBot)) {throw 'daily_pending_listener_generation_conflict'}
        $null=Assert-PilotIdentityBinding $active $configuration.ReceiptDirectory
        $null=Connect-SnowPilotProcess $active
        $null=Save-DailyBinding $active $configuration $startId
        Write-DailyPending $active $configuration $startId $false 'native_and_snow_ready'
        $state.NativeProcess=$active; $state.Phase='existing_native_and_snow_ready'
        Write-DailyJson (Join-Path $attemptPath 'state.json') $state
        Write-Output 'Existing native QQ and Snow verified on the same bot process. No restart or message.'
        return
    }
    if ($preserveNative) {
        # This is the exact native process launched by a prior invocation.
        # Wait for its login instead of treating it as ordinary QQ or starting
        # another instance; a timeout leaves it intact for the next check.
        if (!(Wait-DailyNativeReady $newBot $configuration $LoginWaitSeconds)) {throw 'daily_native_login_or_startup_required'}
        $null=Save-DailyBinding $newBot $configuration $startId
        $null=Connect-SnowPilotProcess $newBot
        $null=Save-DailyBinding $newBot $configuration $startId
        Write-DailyPending $newBot $configuration $startId $false 'native_and_snow_ready'
        $state.Phase='pending_native_and_snow_ready'; Write-DailyJson (Join-Path $attemptPath 'state.json') $state
        Write-Output 'Pending native QQ login completed; Snow verified on that exact saved process. No restart or message.'
        return
    }
    $holders=@(Get-BotHolderPids)
    if ($holders.Count -gt 1) { throw 'daily_multiple_bot_holders' }
    if ($holders.Count -eq 1) {
        $original=Get-PilotProcess $holders[0]
        if (!$original -or $original.Exe -ne $client.executable -or $original.Name -ne 'QQ.exe') { throw 'daily_existing_bot_mismatch' }
        if (!$RestartExistingBot) { throw 'daily_existing_bot_requires_explicit_restart' }
        $null=Assert-SnowPilotOnline $original
    }
    $lock=Get-DailyReceiptLock $configuration.ReceiptDirectory
    if ($lock) {
        if (!$RecoverKnownExitedLock) { throw 'daily_receipt_lock_requires_review' }
        Assert-DailyKnownLock $lock $prior $configuration
        $owner=Get-PilotProcess $lock.Pid
        if ($owner -and (!$original -or !(Test-PilotProcessIdentity $owner $original) -or !(Test-PilotProcessIdentity $owner $prior.NativeProcess))) { throw 'daily_receipt_lock_owner_conflict' }
    }
    Test-PilotDirectoryWrite $attemptPath
    Assert-DailyConfigurationUnchanged $configuration
    $beforeQq=@(Get-CimInstance Win32_Process -Filter "Name='QQ.exe'" | ForEach-Object {[int]$_.ProcessId})
    if ($original) {
        $state.OriginalProcess=$original; $state.OriginalStopAttempted=$true; $state.Phase='stopping_verified_bot'; $stopAttempted=$true
        Write-DailyJson (Join-Path $attemptPath 'state.json') $state
        Stop-PilotTree $original
    }
    if ($lock) { Move-DailyKnownExitedLock $configuration $prior }
    if (@(Get-BotHolderPids).Count -or @(Get-DailyListeners).Count) { throw 'daily_prelaunch_runtime_conflict' }
    Assert-DailyConfigurationUnchanged $configuration
    $args=@($client.executable,"$script:PilotStage\napiloader.dll","$script:PilotStage\nativeLoader.cjs")
    $argumentLine=($args|ForEach-Object {'"'+$_.Replace('"','')+'"'}) -join ' '
    $state.Phase='native_launch_requested'; Write-DailyJson (Join-Path $attemptPath 'state.json') $state
    $launchAttempted=$true
    $launcher=Start-Process -FilePath "$script:PilotStage\napimain.exe" -ArgumentList $argumentLine -WorkingDirectory $script:PilotStage -WindowStyle Hidden -PassThru
    $launchedUtc=$launcher.StartTime.ToUniversalTime().ToString('o')
    $launchRecord=[pscustomobject]@{Id=$launcher.Id;Exe="$script:PilotStage\napimain.exe";CreatedUtc=$launchedUtc;Name='napimain.exe';Parent=$PID}
    $launcherCim=Get-PilotProcess $launcher.Id
    if ($launcherCim -and $launcherCim.Exe -eq $launchRecord.Exe -and [Math]::Abs(([datetimeoffset]$launcherCim.CreatedUtc).UtcDateTime.Ticks-([datetimeoffset]$launchedUtc).UtcDateTime.Ticks) -lt 10) { $launchRecord=$launcherCim }
    $state.Launcher=$launchRecord; $state.Phase='launcher_started'; Write-DailyJson (Join-Path $attemptPath 'state.json') $state
    $deadline=(Get-Date).AddSeconds(30)
    do {
        if ($launcher.HasExited) { $launcherExit=$launcher.ExitTime.ToUniversalTime().ToString('o') } else { $null=Assert-PilotProcess $launchRecord }
        $roots=@(Get-CimInstance Win32_Process -Filter "Name='QQ.exe'" | Where-Object {
            $_.ExecutablePath -eq $client.executable -and $_.ProcessId -notin $beforeQq -and $_.ParentProcessId -eq $launchRecord.Id -and
            $_.CreationDate.ToUniversalTime().Ticks -ge ([datetimeoffset]$launchedUtc).UtcDateTime.Ticks -and
            (!$launcherExit -or $_.CreationDate.ToUniversalTime().Ticks -le ([datetimeoffset]$launcherExit).UtcDateTime.Ticks) -and
            $_.CommandLine -notmatch '(?i)(?:^|\s)--type(?:=|\s)'
        })
        if ($roots.Count) {
            $bound=[Collections.Generic.List[object]]::new()
            foreach ($row in $roots) {
                $expected=Convert-DailyCimIdentity $row
                $refresh=Get-PilotProcess $expected.Id
                if (!$refresh) { continue }
                Assert-DailyCandidateRefresh $expected $refresh
                # Store the original filtered generation, never the second
                # PID lookup that might already name a reused process.
                $bound.Add($expected)
            }
            $candidates=@($bound.ToArray())
            $state.NativeCandidates=$candidates; Write-DailyJson (Join-Path $attemptPath 'state.json') $state
        }
        if ($roots.Count -gt 1) { throw 'daily_multiple_new_qq_roots' }
        if ($candidates.Count -eq 1) { $newBot=$candidates[0]; break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if (!$newBot) { throw 'daily_new_qq_root_not_bound' }
    $preserveNative=$true
    $state.NativeProcess=$newBot; $state.Phase='waiting_for_native_login'; Write-DailyJson (Join-Path $attemptPath 'state.json') $state
    Write-DailyPending $newBot $configuration $startId $true 'waiting_for_native_login'
    if (!(Wait-DailyNativeReady $newBot $configuration $LoginWaitSeconds)) { throw 'daily_native_login_or_startup_required' }
    # Persist the exact proven lock owner before Snow connection can fail.
    $null=Save-DailyBinding $newBot $configuration $startId
    $null=Connect-SnowPilotProcess $newBot
    $null=Save-DailyBinding $newBot $configuration $startId
    Write-DailyPending $newBot $configuration $startId $false 'native_and_snow_ready'
    $state.Phase='new_native_and_snow_ready'; Write-DailyJson (Join-Path $attemptPath 'state.json') $state
    Write-Output 'Native QQ and Snow started on one verified bot process. Existing key and receipts preserved; no message sent.'
} catch {
    $failure=Get-DailyFailureInfo $_ 'daily_start'
    $recoveryErrors=[Collections.Generic.List[string]]::new()
    if ($state -and $attemptPath) {
        $state.Phase='attention_required'; $state['Failure']=$failure
        try { Write-DailyJson (Join-Path $attemptPath 'state.json') $state } catch { $recoveryErrors.Add('failure_state_write_failed') }
    }
    if ($preserveNative -and $newBot) {
        # No cleanup, ordinary fallback or reconnect to another generation.
        # Keep the login window and exact identity for a later invocation.
        try { Write-DailyPending $newBot $configuration $startId $true 'login_or_connection_attention_required' } catch {$recoveryErrors.Add('pending_state_write_failed')}
        try { Write-DailyJson (Join-Path $attemptPath 'failure-and-recovery.json') @{FailedUtc=(Get-Date).ToUniversalTime().ToString('o');
            OriginalFailure=$failure;NativeProcess=$newBot;PreservedPendingNative=$true;RecoveryCodes=@($recoveryErrors.ToArray());
            OrdinaryQqFallbackAttempted=$false;UnknownMessagesRetried=$false} } catch {}
        throw 'Native QQ is kept pending for login or connection. The next daily check will verify this exact process; no ordinary QQ fallback or message retry.'
    }
    if ($NoOrdinaryRecovery) {
        # Controlled cutovers leave ambiguous new launchers/QQ instances for
        # exact manual inspection. No ordinary QQ, second launch or cleanup.
        if ($attemptPath) {
            try { Write-DailyJson (Join-Path $attemptPath 'failure-and-recovery.json') @{
                FailedUtc=(Get-Date).ToUniversalTime().ToString('o');OriginalFailure=$failure;
                NoOrdinaryRecovery=$true;NativeCandidates=$candidates;Launcher=$launchRecord;
                OrdinaryQqFallbackAttempted=$false;UnknownMessagesRetried=$false} } catch {}
        }
        throw 'Controlled native startup stopped without ordinary QQ recovery. Inspect exact recorded generations; no message was retried.'
    }
    # Preserve config, token, receipts and unknown operations. Recovery never
    # changes the delivery transport of an already attempted operation.
    $cleanupProven=$true
    if ($launchAttempted -and !$launchRecord) { $cleanupProven=$false; $recoveryErrors.Add('launcher_generation_not_bound_no_process_guess') }
    if ($launchRecord) {
        try {
            # Stop the exact launcher first so it cannot create a further QQ
            # root after the final proven-descendant snapshot.
            if (Get-PilotProcess $launchRecord.Id) { Invoke-ExactPilotProcessAction $launchRecord 'kill' }
            if ($launcher) {
                if (!$launcher.WaitForExit(5000)) { throw 'launcher_exit_not_confirmed' }
                $launcherExit=$launcher.ExitTime.ToUniversalTime().ToString('o')
            }
            $lateRows=@(Get-CimInstance Win32_Process -Filter "Name='QQ.exe'" | Where-Object {
                $_.ExecutablePath -eq $client.executable -and $_.ProcessId -notin $beforeQq -and $_.ParentProcessId -eq $launchRecord.Id -and
                $_.CreationDate.ToUniversalTime().Ticks -ge ([datetimeoffset]$launchedUtc).UtcDateTime.Ticks -and
                $launcherExit -and $_.CreationDate.ToUniversalTime().Ticks -le ([datetimeoffset]$launcherExit).UtcDateTime.Ticks -and
                $_.CommandLine -notmatch '(?i)(?:^|\s)--type(?:=|\s)'
            })
            $late=[Collections.Generic.List[object]]::new()
            foreach ($row in $lateRows) {
                $expected=Convert-DailyCimIdentity $row
                $refresh=Get-PilotProcess $expected.Id
                if (!$refresh) { continue }
                Assert-DailyCandidateRefresh $expected $refresh
                $late.Add($expected)
            }
            $all=@($candidates)+@($late.ToArray())
            if ($newBot) { $all+=@($newBot) }
            $candidates=@($all|Where-Object {$null -ne $_}|Sort-Object Id,CreatedUtc -Unique)
            if ($state) { $state.NativeCandidates=$candidates }
        } catch { $cleanupProven=$false; $recoveryErrors.Add('launcher_cleanup_or_descendant_binding_failed') }
    }
    foreach ($candidate in $candidates) {
        try { Stop-PilotTree $candidate } catch { $recoveryErrors.Add('native_candidate_cleanup_requires_check') }
    }
    foreach ($candidate in $candidates) {
        try {
            if (Get-PilotProcess $candidate.Id) { $cleanupProven=$false; $recoveryErrors.Add('native_candidate_still_present_or_reused') }
        } catch { $cleanupProven=$false; $recoveryErrors.Add('native_candidate_exit_query_failed') }
    }
    if (($stopAttempted -or $launchAttempted) -and $cleanupProven) {
        try {
            if (@(Get-DailyListeners).Count) { throw 'native_listener_still_present' }
            $holders=@(Get-BotHolderPids)
            if ($holders.Count -gt 1) { throw 'multiple_bot_holders' }
            if (!$holders.Count) {
                $null=Start-Process -FilePath $client.executable -WorkingDirectory (Split-Path $client.executable -Parent) -WindowStyle Hidden -PassThru
                $deadline=(Get-Date).AddSeconds(40)
                do { $holders=@(Get-BotHolderPids); if ($holders.Count) {break}; Start-Sleep -Seconds 1 } while ((Get-Date) -lt $deadline)
            }
            if ($holders.Count -eq 1) {
                $restored=Get-PilotProcess $holders[0]
                if (!$restored -or $restored.Exe -ne $client.executable -or $restored.Name -ne 'QQ.exe') { throw 'recovery_process_identity_unverified' }
                $null=Connect-SnowPilotProcess $restored
                Write-DailyJson (Join-Path $attemptPath 'recovery.json') @{RecoveredUtc=(Get-Date).ToUniversalTime().ToString('o');NormalBot=$restored;SnowVerified=$true;NativeRoutingFallbackAuthorized=$false;UnknownMessagesRetried=$false}
            } else { throw 'normal_bot_login_required' }
        } catch { $recoveryErrors.Add('normal_qq_or_snow_recovery_requires_attention') }
    } elseif ($stopAttempted -or $launchAttempted) { $recoveryErrors.Add('normal_relaunch_blocked_until_pilot_cleanup_proven') }
    if ($attemptPath) {
        try {
            Write-DailyJson (Join-Path $attemptPath 'failure-and-recovery.json') @{FailedUtc=(Get-Date).ToUniversalTime().ToString('o');
                OriginalFailure=$failure;CleanupProven=$cleanupProven;NativeCandidates=$candidates;Launcher=$launchRecord;
                RecoveryCodes=@($recoveryErrors.ToArray());UnknownMessagesRetried=$false;ConfigurationChanged=$false}
        } catch {
            # Fixed message preserves the failure; never print native or HTTP
            # exception bodies that may contain account or request details.
        }
    }
    throw 'Daily native startup needs attention. Exact-process recovery was attempted where applicable; no message was sent or replayed.'
} finally {
    if ($launcher) { $launcher.Dispose() }
    if ($mutex) { $mutex.Dispose() }
}
