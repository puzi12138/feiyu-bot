param()
$ErrorActionPreference = 'Stop'
# The same guard supports direct maintenance starts without starting QQ/AstrBot.
. "$PSScriptRoot\IndependentStartup.Common.ps1"
if (Invoke-QQBotIndependentStartup -ScriptPath $PSCommandPath) { return }
$Root = Split-Path $PSScriptRoot -Parent
$configPath = Join-Path $Root 'config\codex-fault-relay.json'
$pausePath = Join-Path $Root 'run\services-paused'
if ((Test-Path -LiteralPath $pausePath) -or -not (Test-Path -LiteralPath $configPath)) { return }

if (-not ('QQBot.FaultRelayStartup.Native' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
namespace QQBot.FaultRelayStartup {
    public static class Native {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr CommandLineToArgvW(string command, out int count);
        [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern uint GetFinalPathNameByHandleW(SafeFileHandle file, StringBuilder path, uint length, uint flags);
        public static string[] Arguments(string command) {
            int count;
            IntPtr memory = CommandLineToArgvW(command, out count);
            if (memory == IntPtr.Zero) throw new InvalidOperationException("argv unavailable");
            try {
                string[] result = new string[count];
                for (int index = 0; index < count; index++)
                    result[index] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory, index * IntPtr.Size));
                return result;
            } finally { LocalFree(memory); }
        }
        public static string FinalPath(string path) {
            using (FileStream file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete)) {
                StringBuilder result = new StringBuilder(32768);
                uint size = GetFinalPathNameByHandleW(file.SafeFileHandle, result, (uint)result.Capacity, 0);
                if (size == 0 || size >= result.Capacity) throw new InvalidOperationException("path unavailable");
                return result.ToString();
            }
        }
    }
}
'@ | Out-Null
}

function Get-CodexFaultRelayPath {
    param([string]$Path)
    return [IO.Path]::GetFullPath($Path).TrimEnd('\').Replace('/', '\').ToLowerInvariant()
}

function Test-CodexFaultRelayCommand {
    param($Process, [string]$Nonce)
    $arguments = @([QQBot.FaultRelayStartup.Native]::Arguments([string]$Process.CommandLine))
    return ($arguments.Count -eq 8 -and $arguments[1] -ceq '-B' -and
        (Get-CodexFaultRelayPath $arguments[2]) -eq (Get-CodexFaultRelayPath $workerPath) -and
        $arguments[3] -ceq '--config' -and
        (Get-CodexFaultRelayPath $arguments[4]) -eq (Get-CodexFaultRelayPath $configPath) -and
        $arguments[5] -ceq 'run' -and $arguments[6] -ceq '--launch-nonce' -and $arguments[7] -ceq $Nonce -and
        $allowedExecutables -contains ([QQBot.FaultRelayStartup.Native]::FinalPath([string]$Process.ExecutablePath)).ToLowerInvariant())
}

function Get-CodexFaultRelayRuntime {
    # Process command lines stay in memory; no process/config contents are logged.
    $processes = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction Stop)
    $candidates = @()
    foreach ($process in $processes) {
        if (-not $process.CommandLine) { continue }
        $arguments = @([QQBot.FaultRelayStartup.Native]::Arguments([string]$process.CommandLine))
        foreach ($argument in $arguments) {
            if ([IO.Path]::IsPathRooted($argument) -and
                (Get-CodexFaultRelayPath $argument) -eq (Get-CodexFaultRelayPath $workerPath)) {
                $candidates += $process
                break
            }
        }
    }
    $record = $null
    if (Test-Path -LiteralPath $runtimePath) {
        try { $record = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json } catch { }
    }
    if ($candidates.Count -eq 0) {
        # A stale worker-owned record is retained and may be replaced by run.
        return [pscustomobject]@{State='absent'; ProcessId=$null; Nonce=$null}
    }
    if (-not $record -or $record.schema -ne 1 -or $record.pid -isnot [ValueType] -or
        [string]$record.nonce -cnotmatch '^[0-9a-f]{32}$' -or
        $record.process_creation_filetime -isnot [string] -or $record.process_creation_filetime -cnotmatch '^[0-9]{17,20}$' -or
        $record.script_sha256 -cne $workerHash -or $record.config_sha256 -cne $configHash) {
        return [pscustomobject]@{State='unconfirmed'; ProcessId=$null; Nonce=$null}
    }
    $actual = @($candidates | Where-Object { $_.ProcessId -eq $record.pid })
    if ($actual.Count -ne 1 -or $actual[0].ParentProcessId -ne $record.ppid -or
        -not (Test-CodexFaultRelayCommand $actual[0] ([string]$record.nonce))) {
        return [pscustomobject]@{State='unconfirmed'; ProcessId=$null; Nonce=$null}
    }
    # .NET reads the real process FILETIME without CIM's timestamp truncation.
    $live = [Diagnostics.Process]::GetProcessById([int]$record.pid)
    try { $created = $live.StartTime.ToUniversalTime().ToFileTimeUtc().ToString([Globalization.CultureInfo]::InvariantCulture) }
    finally { $live.Dispose() }
    if ($created -cne $record.process_creation_filetime) {
        return [pscustomobject]@{State='unconfirmed'; ProcessId=$null; Nonce=$null}
    }
    # A venv redirector can stay alive above the actual interpreter. Every other
    # matching process must be one of that worker's verified Python ancestors.
    $lineage = @([int]$record.pid)
    $parentId = [int]$record.ppid
    for ($depth = 0; $depth -lt 8; $depth++) {
        $parent = @($candidates | Where-Object { $_.ProcessId -eq $parentId })
        if ($parent.Count -eq 0) { break }
        if ($parent.Count -ne 1 -or $lineage -contains $parentId -or
            -not (Test-CodexFaultRelayCommand $parent[0] ([string]$record.nonce))) {
            return [pscustomobject]@{State='unconfirmed'; ProcessId=$null; Nonce=$null}
        }
        $lineage += $parentId
        $parentId = [int]$parent[0].ParentProcessId
    }
    if (@($candidates | Where-Object { $lineage -notcontains [int]$_.ProcessId }).Count -ne 0) {
        return [pscustomobject]@{State='unconfirmed'; ProcessId=$null; Nonce=$null}
    }
    return [pscustomobject]@{State=[string]$record.status; ProcessId=[int]$record.pid; Nonce=[string]$record.nonce}
}

$lease = $null
$acquired = $false
try {
    $workerPath = Join-Path $PSScriptRoot 'CodexFaultRelay.py'
    $pythonPath = Join-Path $Root 'runtime\astrbot-venv\Scripts\python.exe'
    $statePath = Join-Path $Root 'data\codex-fault-relay'
    $runtimePath = Join-Path $statePath 'runtime.json'
    $settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $keys = @($settings.PSObject.Properties.Name | Sort-Object)
    $expectedKeys = @('codex_exe','codex_sha256','parser_path','schema','source_dir','state_dir')
    if (($keys -join '|') -cne ($expectedKeys -join '|') -or $settings.schema -ne 1 -or
        $settings.state_dir -isnot [string] -or
        (Get-CodexFaultRelayPath $settings.state_dir) -ne (Get-CodexFaultRelayPath $statePath)) {
        throw 'config_invalid'
    }
    # Startup never initializes a baseline or changes a reservation/receipt.
    if ([IO.File]::ReadAllText((Join-Path $statePath 'initialized')) -cne "codex-fault-event-relay-v1`n" -or
        -not (Test-Path -LiteralPath (Join-Path $statePath 'state.json') -PathType Leaf)) { throw 'not_initialized' }
    $workerHash = (Get-FileHash -LiteralPath $workerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $configHash = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $venvConfig = Get-Content -LiteralPath (Join-Path $Root 'runtime\astrbot-venv\pyvenv.cfg')
    $homeRows = @($venvConfig | Where-Object { $_ -match '^home\s*=\s*(.+)$' })
    if ($homeRows.Count -ne 1) { throw 'interpreter_unavailable' }
    $homePath = ($homeRows[0] -replace '^home\s*=\s*', '').Trim()
    if (-not [IO.Path]::IsPathRooted($homePath)) { throw 'interpreter_unavailable' }
    $allowedExecutables = @(
        ([QQBot.FaultRelayStartup.Native]::FinalPath($pythonPath)).ToLowerInvariant(),
        ([QQBot.FaultRelayStartup.Native]::FinalPath((Join-Path $homePath 'python.exe'))).ToLowerInvariant()
    ) | Select-Object -Unique
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes((Get-CodexFaultRelayPath $Root))) }
    finally { $sha.Dispose() }
    $lease = New-Object Threading.Mutex($false, ('Local\QQBotCodexFaultRelayStartup_' + [BitConverter]::ToString($digest).Replace('-', '')))
    try { $acquired = $lease.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { throw 'startup_busy' }
    if (Test-Path -LiteralPath $pausePath) { return }
    $current = Get-CodexFaultRelayRuntime
    if ($current.State -eq 'watching') {
        Write-Host "Codex fault relay is already watching (PID $($current.ProcessId))."
        return
    }
    if ($current.State -eq 'source_blocked') {
        Write-Warning 'Codex fault relay is running but its source is blocked; no second worker was started.'
        return
    }
    if ($current.State -ne 'absent') { throw 'worker_unconfirmed' }
    if (Test-Path -LiteralPath $pausePath) { return }
    $nonce = [Guid]::NewGuid().ToString('N')
    # Windows paths cannot contain quotes. Explicit quoting preserves spaces.
    $arguments = '-B "{0}" --config "{1}" run --launch-nonce {2}' -f $workerPath, $configPath, $nonce
    $logPath = Join-Path $Root 'logs'
    if (-not (Test-Path -LiteralPath $logPath -PathType Container)) { throw 'logs_unavailable' }
    $logStem = Join-Path $logPath ('codex-fault-relay-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $nonce)
    # Never trust this redirector's PID; only the worker-owned runtime below.
    $null = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput ($logStem + '.out.log') -RedirectStandardError ($logStem + '.err.log') -PassThru
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    try {
        # Finite bootstrap handshake only. Runtime business work has no timer.
        while ($deadline.Elapsed.TotalSeconds -lt 45) {
            $current = Get-CodexFaultRelayRuntime
            if ($current.Nonce -ceq $nonce -and $current.State -eq 'watching') {
                Write-Host "Started Codex fault relay (PID $($current.ProcessId))."
                return
            }
            if ($current.Nonce -ceq $nonce -and $current.State -in @('source_blocked','stopped')) { throw 'worker_not_ready' }
            Start-Sleep -Milliseconds 200
        }
        throw 'readiness_unconfirmed'
    } finally { $deadline.Stop() }
} catch {
    # Never retry, kill, auto-init, clear pause, acknowledge or print raw errors.
    throw 'Codex fault relay startup was not confirmed. No retry or process termination was performed; inspect its protected runtime records before another start.'
} finally {
    if ($lease) {
        if ($acquired) { $lease.ReleaseMutex() }
        $lease.Dispose()
    }
}
