param(
    [string]$OutputDirectory = 'D:\QQBot\logs',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$')]
    [string]$RunId = ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)),
    [ValidateRange(0, 86400)]
    [int]$DurationSeconds = 0,
    [ValidateSet('Auto', 'Trace', 'Poll')]
    [string]$Mode = 'Auto'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Observation only: this script never starts, stops, loads into, or calls QQ.
$resolvedOutput = [IO.Path]::GetFullPath($OutputDirectory)
if (-not [IO.Path]::IsPathRooted($OutputDirectory)) { throw 'observer_absolute_output_required' }
if (-not (Test-Path -LiteralPath $resolvedOutput -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $resolvedOutput
}
$base = Join-Path $resolvedOutput ('qq-process-lifecycle-' + $RunId)
$logPath = $base + '.jsonl'
$pidPath = $base + '.observer.json'
$stopPath = $base + '.stop'
$donePath = $base + '.completed.json'
$sourcePrefix = 'FeiyuQQProcessObserver.' + [guid]::NewGuid().ToString('N')
$startSource = $sourcePrefix + '.Start'
$stopSource = $sourcePrefix + '.Stop'
$startRegistered = $false
$stopRegistered = $false
$writer = $null
$status = 'failed'
$snapshotCount = 0
$startCount = 0
$stopCount = 0
$eventCount = 0
$activeMode = $Mode
$tracked = @{}
$pollIntervalMilliseconds = 500
$started = [datetime]::UtcNow
$observer = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
if (-not $observer) { throw 'observer_identity_unavailable' }
$observerIdentity = [ordered]@{
    schema = 1
    pid = $PID
    executable = [string]$observer.ExecutablePath
    created_utc = $observer.CreationDate.ToUniversalTime().ToString('o')
    started_utc = $started.ToString('o')
    run_id = $RunId
    log_path = $logPath
    stop_request_path = $stopPath
    protected_personal_qq_pid = 20164
    observes_only = $true
    stores_command_line = $false
    stores_window_titles = $false
    requested_mode = $Mode
    polling_interval_ms = $pollIntervalMilliseconds
}

function Write-NewJsonFile([string]$Path, $Value) {
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 6))
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}

function Write-Observation($Value) {
    $script:writer.WriteLine(($Value | ConvertTo-Json -Depth 6 -Compress))
    $script:writer.Flush()
}

function Get-SafeProcessRole([string]$CommandLine) {
    # The raw command line stays local and is never serialized or logged.
    $type = 'browser_root'
    $match = [regex]::Match($CommandLine, '(?i)(?:^|\s)--type(?:=|\s+)([a-z0-9_-]+)(?:\s|$)')
    if ($match.Success) {
        $candidate = $match.Groups[1].Value.ToLowerInvariant()
        if ($candidate -in @('renderer', 'gpu-process', 'utility', 'zygote', 'broker', 'crashpad-handler')) {
            $type = $candidate
        } else { $type = 'other_type' }
    }
    $service = $null
    if ($type -eq 'utility') {
        $utilityMatch = [regex]::Match($CommandLine, '(?i)(?:^|\s)--utility-sub-type(?:=|\s+)([a-z0-9_.-]+)(?:\s|$)')
        if ($utilityMatch.Success) {
            switch ($utilityMatch.Groups[1].Value) {
                'network.mojom.NetworkService' { $service = 'network_service' }
                'audio.mojom.AudioService' { $service = 'audio_service' }
                'video_capture.mojom.VideoCaptureService' { $service = 'video_capture_service' }
                'storage.mojom.StorageService' { $service = 'storage_service' }
                default { $service = 'other_utility' }
            }
        }
    }
    return [ordered]@{
        process_type = $type
        utility_service = $service
        relaunch = [regex]::IsMatch($CommandLine, '(?i)(?:^|\s)--relaunch(?:\s|$)')
    }
}

function Get-SafeProcessMetadata($Process, [datetime]$EventUtc = [datetime]::MinValue) {
    if (-not $Process) { return $null }
    if ([string]$Process.Name -notin @('QQ.exe', 'napimain.exe')) { return $null }
    $created = $Process.CreationDate.ToUniversalTime()
    # A late PID lookup must not attach a reused generation to an older event.
    if ($EventUtc -ne [datetime]::MinValue -and [Math]::Abs(($created - $EventUtc).TotalSeconds) -gt 10) {
        return $null
    }
    return [ordered]@{
        pid = [uint32]$Process.ProcessId
        parent_pid = [uint32]$Process.ParentProcessId
        name = [string]$Process.Name
        executable = [string]$Process.ExecutablePath
        created_utc = $created.ToString('o')
        role = Get-SafeProcessRole ([string]$Process.CommandLine)
        protected_personal_qq = ([uint32]$Process.ProcessId -eq 20164)
    }
}

function Get-TraceUtc($Trace) {
    try { return [datetime]::FromFileTimeUtc([int64]$Trace.TIME_CREATED) } catch { return [datetime]::UtcNow }
}

function Get-ProcessKey($Safe) { return ([string]$Safe.pid + ':' + [string]$Safe.created_utc) }

function Add-TrackedProcess($Safe) {
    $key = Get-ProcessKey $Safe
    if ($script:tracked.ContainsKey($key)) { return }
    $handle = $null
    $handleVerified = $false
    try {
        $handle = [Diagnostics.Process]::GetProcessById([int]$Safe.pid)
        # Keep the OS handle open to bind an eventual ExitCode to this exact
        # generation. No process-control method is used by this script.
        $null = $handle.Handle
        $expectedTicks = ([datetimeoffset]$Safe.created_utc).UtcDateTime.Ticks
        if ($handle.HasExited -or
            [Math]::Abs($handle.StartTime.ToUniversalTime().Ticks - $expectedTicks) -ge 10 -or
            [string]$handle.MainModule.FileName -ne [string]$Safe.executable) {
            throw 'observer_process_generation_changed'
        }
        $refresh = Get-CimInstance Win32_Process -Filter "ProcessId=$([uint32]$Safe.pid)" -ErrorAction Stop
        $refreshed = Get-SafeProcessMetadata $refresh
        if ($null -eq $refreshed -or (Get-ProcessKey $refreshed) -ne $key -or
            $refreshed.executable -ne $Safe.executable) { throw 'observer_process_generation_changed' }
        $handleVerified = $true
    } catch {
        if ($null -ne $handle) { $handle.Dispose() }
        $handle = $null
    }
    $script:tracked[$key] = @{ safe = $Safe; handle = $handle; handle_verified = $handleVerified }
}

function Get-TrackedExitEvidence($Entry) {
    $result = [ordered]@{ exit_status = $null; exit_status_hex = $null; exit_code_verified = $false }
    if ($Entry.handle_verified -and $null -ne $Entry.handle) {
        try {
            if ($Entry.handle.HasExited) {
                $signed = [int]$Entry.handle.ExitCode
                $unsigned = [BitConverter]::ToUInt32([BitConverter]::GetBytes($signed), 0)
                $result.exit_status = $unsigned
                $result.exit_status_hex = ('0x{0:x8}' -f $unsigned)
                $result.exit_code_verified = $true
            }
        } catch { }
    }
    return $result
}

try {
    if (Test-Path -LiteralPath $stopPath) { throw 'observer_preexisting_stop_request' }
    $logStream = [IO.File]::Open($logPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $writer = New-Object IO.StreamWriter($logStream, (New-Object Text.UTF8Encoding($false)))
    $writer.AutoFlush = $true
    Write-NewJsonFile $pidPath $observerIdentity
    Write-Observation ([ordered]@{ event = 'observer_started'; at_utc = [datetime]::UtcNow.ToString('o'); observer_pid = $PID; run_id = $RunId })

    if ($Mode -ne 'Poll') {
        try {
            $null = Register-CimIndicationEvent -Namespace 'root/cimv2' -Query "SELECT * FROM Win32_ProcessStartTrace WHERE ProcessName='QQ.exe' OR ProcessName='napimain.exe'" -SourceIdentifier $startSource
            $startRegistered = $true
            $null = Register-CimIndicationEvent -Namespace 'root/cimv2' -Query "SELECT * FROM Win32_ProcessStopTrace WHERE ProcessName='QQ.exe' OR ProcessName='napimain.exe'" -SourceIdentifier $stopSource
            $stopRegistered = $true
            $activeMode = 'Trace'
            Write-Observation ([ordered]@{ event = 'subscriptions_registered'; at_utc = [datetime]::UtcNow.ToString('o'); start_trace = $true; stop_trace = $true })
        } catch {
            if ($startRegistered) { Unregister-Event -SourceIdentifier $startSource -ErrorAction SilentlyContinue; $startRegistered = $false }
            if ($stopRegistered) { Unregister-Event -SourceIdentifier $stopSource -ErrorAction SilentlyContinue; $stopRegistered = $false }
            Write-Observation ([ordered]@{ event = 'subscriptions_unavailable'; at_utc = [datetime]::UtcNow.ToString('o'); exception_type = $_.Exception.GetType().Name; hresult = ('0x{0:x8}' -f [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$_.Exception.HResult), 0)); fallback = ($Mode -eq 'Auto') })
            if ($Mode -eq 'Trace') { throw }
            $activeMode = 'Poll'
        }
    }
    if ($activeMode -eq 'Poll') {
        Write-Observation ([ordered]@{ event = 'polling_started'; at_utc = [datetime]::UtcNow.ToString('o'); interval_ms = $pollIntervalMilliseconds; can_miss_short_lived_processes = $true; timestamps_are_observations = $true })
    }

    foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name='QQ.exe' OR Name='napimain.exe'")) {
        $safe = Get-SafeProcessMetadata $process
        if ($null -ne $safe) {
            Write-Observation ([ordered]@{ event = 'initial_process'; at_utc = [datetime]::UtcNow.ToString('o'); process = $safe })
            if ($activeMode -eq 'Poll') { Add-TrackedProcess $safe }
            $snapshotCount++
        }
    }

    $status = 'completed_duration'
    while ($true) {
        if ($activeMode -eq 'Trace') { foreach ($source in @($startSource, $stopSource)) {
            foreach ($queued in @(Get-Event -SourceIdentifier $source -ErrorAction SilentlyContinue)) {
                try {
                    $trace = $queued.SourceEventArgs.NewEvent
                    $traceUtc = Get-TraceUtc $trace
                    if ([string]$trace.ProcessName -notin @('QQ.exe', 'napimain.exe')) { continue }
                    $isStart = $source -eq $startSource
                    $value = [ordered]@{
                        event = $(if ($isStart) { 'process_start' } else { 'process_stop' })
                        at_utc = $traceUtc.ToString('o')
                        observed_utc = [datetime]::UtcNow.ToString('o')
                        pid = [uint32]$trace.ProcessID
                        parent_pid = [uint32]$trace.ParentProcessID
                        name = [string]$trace.ProcessName
                        protected_personal_qq = ([uint32]$trace.ProcessID -eq 20164)
                    }
                    if ($isStart) {
                        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$([uint32]$trace.ProcessID)" -ErrorAction SilentlyContinue
                        $value.process = Get-SafeProcessMetadata $process $traceUtc
                        $startCount++
                    } else {
                        $value.exit_status = [uint32]$trace.ExitStatus
                        $value.exit_status_hex = ('0x{0:x8}' -f [uint32]$trace.ExitStatus)
                        $stopCount++
                    }
                    Write-Observation $value
                    $eventCount++
                } finally {
                    Remove-Event -EventIdentifier $queued.EventIdentifier -ErrorAction SilentlyContinue
                }
            }
        } } else {
            $currentKeys = @{}
            foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name='QQ.exe' OR Name='napimain.exe'")) {
                $safe = Get-SafeProcessMetadata $process
                if ($null -eq $safe) { continue }
                $key = Get-ProcessKey $safe
                $currentKeys[$key] = $true
                if (-not $tracked.ContainsKey($key)) {
                    Add-TrackedProcess $safe
                    Write-Observation ([ordered]@{ event = 'process_observed_started'; observed_utc = [datetime]::UtcNow.ToString('o'); process = $safe; source = 'polling'; timing_exact = $false; generation_handle_verified = $tracked[$key].handle_verified })
                    $startCount++
                }
            }
            foreach ($key in @($tracked.Keys)) {
                if ($currentKeys.ContainsKey($key)) { continue }
                $entry = $tracked[$key]
                $exitEvidence = Get-TrackedExitEvidence $entry
                Write-Observation ([ordered]@{ event = 'process_observed_disappeared'; observed_utc = [datetime]::UtcNow.ToString('o'); process = $entry.safe; source = 'polling'; timing_exact = $false; exit_evidence = $exitEvidence })
                if ($null -ne $entry.handle) { $entry.handle.Dispose() }
                $tracked.Remove($key)
                $stopCount++
            }
        }
        if (Test-Path -LiteralPath $stopPath) { $status = 'stopped_by_request'; break }
        if ($DurationSeconds -gt 0 -and ([datetime]::UtcNow - $started).TotalSeconds -ge $DurationSeconds) { break }
        # No event actions or job callbacks: only drain this observer's queues.
        Start-Sleep -Milliseconds $(if ($activeMode -eq 'Poll') { $pollIntervalMilliseconds } else { 250 })
    }
} catch {
    $status = 'failed'
    if ($null -ne $writer) {
        Write-Observation ([ordered]@{ event = 'observer_failed'; at_utc = [datetime]::UtcNow.ToString('o'); exception_type = $_.Exception.GetType().Name })
    }
    throw
} finally {
    if ($startRegistered) { Unregister-Event -SourceIdentifier $startSource -ErrorAction SilentlyContinue }
    if ($stopRegistered) { Unregister-Event -SourceIdentifier $stopSource -ErrorAction SilentlyContinue }
    foreach ($source in @($startSource, $stopSource)) {
        Get-Event -SourceIdentifier $source -ErrorAction SilentlyContinue | ForEach-Object {
            Remove-Event -EventIdentifier $_.EventIdentifier -ErrorAction SilentlyContinue
        }
    }
    foreach ($entry in $tracked.Values) { if ($null -ne $entry.handle) { $entry.handle.Dispose() } }
    if ($null -ne $writer) {
        Write-Observation ([ordered]@{ event = 'observer_stopped'; at_utc = [datetime]::UtcNow.ToString('o'); status = $status; start_events = $startCount; stop_events = $stopCount })
        $writer.Dispose()
    }
    $completed = [ordered]@{
        schema = 1; run_id = $RunId; observer_pid = $PID; status = $status
        started_utc = $started.ToString('o'); completed_utc = [datetime]::UtcNow.ToString('o')
        initial_processes = $snapshotCount; start_events = $startCount; stop_events = $stopCount
        qq_processes_modified = $false; command_lines_logged = $false; window_titles_logged = $false
        start_subscription_registered = $startRegistered; stop_subscription_registered = $stopRegistered
        active_mode = $activeMode; polling_can_miss_short_lived_processes = ($activeMode -eq 'Poll')
        log_path = $logPath; stop_request_path = $stopPath
    }
    Write-NewJsonFile $donePath $completed
    $completed | ConvertTo-Json -Depth 4 -Compress
}
