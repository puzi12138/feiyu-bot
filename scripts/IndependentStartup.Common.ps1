# Package identity is inherited by ordinary children of packaged desktop apps.
# WMI creates a new same-user process outside that parent chain. Never substitute
# Start-Process here: it would preserve the package identity this guard removes.
function Get-QQBotCurrentPackageState {
    if (-not ('QQBot.IndependentStartup.NativeMethods' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
namespace QQBot.IndependentStartup {
    public static class NativeMethods {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        public static extern int GetCurrentPackageFullName(ref uint length, StringBuilder name);
        [DllImport("kernel32.dll", EntryPoint = "SetEnvironmentVariableW", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool SetProcessEnvironmentVariable(string name, string value);
    }
}
'@ -ErrorAction Stop | Out-Null
    }
    [uint32]$length = 0
    $code = [QQBot.IndependentStartup.NativeMethods]::GetCurrentPackageFullName([ref]$length, $null)
    if ($code -eq 15700) { return $false } # APPMODEL_ERROR_NO_PACKAGE
    if ($code -ne 122 -or $length -lt 2 -or $length -gt 32768) {
        throw 'Cannot verify process package identity; startup is blocked.'
    }
    $name = New-Object System.Text.StringBuilder ([int]$length)
    $code = [QQBot.IndependentStartup.NativeMethods]::GetCurrentPackageFullName([ref]$length, $name)
    if ($code -ne 0 -or $name.Length -eq 0) {
        throw 'Cannot verify process package identity; startup is blocked.'
    }
    return $true
}

function ConvertTo-QQBotPowerShellLiteral {
    param([Parameter(Mandatory=$true)][string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function New-QQBotPrivateReceiptDirectory {
    param([string]$Path, [System.Security.Principal.SecurityIdentifier]$OwnerSid)
    if (Test-Path -LiteralPath $Path) { throw 'Independent startup receipt directory already exists.' }
    $security = New-Object System.Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner($OwnerSid)
    $inheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($OwnerSid, 'FullControl', $inheritance, 'None', 'Allow')
    $security.AddAccessRule($rule)
    $systemSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
    $systemRule = New-Object System.Security.AccessControl.FileSystemAccessRule($systemSid, 'FullControl', $inheritance, 'None', 'Allow')
    $security.AddAccessRule($systemRule)
    # Create the final directory with its private DACL, rather than fixing a
    # broadly inherited DACL after publishing a nonce-bearing process command.
    if ('System.IO.FileSystemAclExtensions' -as [type]) {
        [System.IO.FileSystemAclExtensions]::CreateDirectory($security, $Path) | Out-Null
    } else {
        [System.IO.Directory]::CreateDirectory($Path, $security) | Out-Null
    }
}

function Get-QQBotPrivateReceiptRoot {
    param([System.Security.Principal.SecurityIdentifier]$OwnerSid)
    $path = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if (-not $path -or -not [IO.Path]::IsPathRooted($path)) { throw 'A private startup receipt location is unavailable.' }
    # Runtime run/ inherits broad write access on this installation. Receipts
    # belong below the user's private LocalAppData instead, not below run/.
    $trustedSids = @($OwnerSid.Value, 'S-1-5-18', 'S-1-5-32-544')
    $writeRights = [Security.AccessControl.FileSystemRights]'Write,Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
    foreach ($segment in @('', 'QQBot', 'IndependentStartup')) {
        if ($segment) { $path = Join-Path $path $segment }
        if (-not (Test-Path -LiteralPath $path)) { New-QQBotPrivateReceiptDirectory -Path $path -OwnerSid $OwnerSid }
        $directory = Get-Item -LiteralPath $path -Force
        if (-not $directory.PSIsContainer -or ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Private startup receipt path is unsafe; packaged startup is blocked.'
        }
        $acl = Get-Acl -LiteralPath $path
        if ($trustedSids -notcontains $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value) {
            throw 'Private startup receipt directory has an unexpected owner.'
        }
        foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and ($rule.FileSystemRights -band $writeRights) -and
                $trustedSids -notcontains $rule.IdentityReference.Value) {
                throw 'Private startup receipt directory permits another account to write.'
            }
        }
    }
    return $path
}

function Enter-QQBotStartupLease {
    param([Parameter(Mandatory=$true)][string]$Root)
    $normalizedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\').ToLowerInvariant()
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalizedRoot)) } finally { $sha.Dispose() }
    $name = 'Local\QQBotStartup_' + ([BitConverter]::ToString($digest).Replace('-',''))
    $lease = New-Object System.Threading.Mutex($false, $name)
    try {
        try { $acquired = $lease.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { throw 'A QQBot startup is already running; do not start another copy.' }
        return $lease
    } catch { $lease.Dispose(); throw }
}

function New-QQBotIndependentStartupCommand {
    param(
        [Parameter(Mandatory=$true)][string]$ScriptPath,
        [Parameter(Mandatory=$true)][string]$HelperPath,
        [Parameter(Mandatory=$true)][string]$ReceiptDirectory,
        [Parameter(Mandatory=$true)][string]$Nonce,
        [Parameter(Mandatory=$true)][string]$OwnerSid,
        [Parameter(Mandatory=$true)][int]$SessionId,
        [Parameter(Mandatory=$true)][string]$EnvironmentMarker,
        [switch]$SkipQQConnection,
        [switch]$AutomaticServiceRecovery
    )
    $switchArguments = @()
    if ($SkipQQConnection) { $switchArguments += '-SkipQQConnection' }
    if ($AutomaticServiceRecovery) { $switchArguments += '-AutomaticServiceRecovery' }
    # This template contains only paths, a nonce, the expected identity and the
    # two public switches. Environment variables never enter the command line.
    $template = @'
$ErrorActionPreference = 'Stop'
$receiptDirectory = __RECEIPT__
$receipt = [ordered]@{
    SchemaVersion = 1
    Nonce = __NONCE__
    ProcessId = $PID
    CreatedUtc = ([System.Diagnostics.Process]::GetCurrentProcess()).StartTime.ToUniversalTime().ToString('o')
    OwnerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    SessionId = ([System.Diagnostics.Process]::GetCurrentProcess()).SessionId
    PackageIndependent = $false
    Status = 'blocked'
    Reason = 'bootstrap_failed'
    ExitCode = 1
    WarningCount = 0
    FinishedUtc = $null
}
function Write-QQBotStartupReceipt {
    param([string]$Name)
    $target = Join-Path $receiptDirectory $Name
    $temporary = $target + '.' + $PID + '.tmp'
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($temporary, ($receipt | ConvertTo-Json -Depth 4), $encoding)
    [System.IO.File]::Move($temporary, $target)
}
try {
    if ($receipt.OwnerSid -ne __SID__) {
        $receipt.Reason = 'owner_mismatch'
        throw 'Startup identity verification failed.'
    }
    if ($receipt.SessionId -ne __SESSION__) {
        $receipt.Reason = 'session_mismatch'
        throw 'Startup session verification failed.'
    }
    . __HELPER__
    if (Get-QQBotCurrentPackageState) {
        $receipt.Reason = 'package_identity_retained'
        throw 'Independent startup retained a package identity.'
    }
    $receipt.PackageIndependent = $true
    # PowerShell may rewrite policy/module-path variables during host startup.
    # Restore the caller's complete environment from WMI-only metadata, then
    # remove all helper-only variables before invoking the original script.
    $environmentMarker = __ENVIRONMENT_MARKER__
    $startupMetadata = [Environment]::GetEnvironmentVariable($environmentMarker, 'Process') | ConvertFrom-Json
    $originalVariables = @($startupMetadata.Environment.PSObject.Properties)
    $originalNames = @($originalVariables | ForEach-Object { $_.Name })
    $currentEnvironment = [Environment]::GetEnvironmentVariables('Process')
    foreach ($name in @($currentEnvironment.Keys)) {
        if ($originalNames -notcontains $name -and
            -not [QQBot.IndependentStartup.NativeMethods]::SetProcessEnvironmentVariable([string]$name, [NullString]::Value)) {
            throw 'Cannot restore the startup environment.'
        }
    }
    foreach ($variable in $originalVariables) {
        $value = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string]$variable.Value))
        # The native API preserves empty values on both .NET Framework and .NET.
        if (-not [QQBot.IndependentStartup.NativeMethods]::SetProcessEnvironmentVariable([string]$variable.Name, $value)) {
            throw 'Cannot restore the startup environment.'
        }
    }
    Set-Location -LiteralPath ([string]$startupMetadata.PowerShellLocation)
    [Environment]::CurrentDirectory = [string]$startupMetadata.ProcessCurrentDirectory
    $receipt.Status = 'running'
    $receipt.Reason = 'startup_running'
    Write-QQBotStartupReceipt -Name 'started.json'
    $receipt.Reason = 'startup_failed'
    # Preserve Start.ps1's existing environment/config setup and service checks.
    # Output is intentionally discarded; receipts never store config/errors.
    & __SCRIPT__ __SWITCHES__ *>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.WarningRecord]) { $receipt.WarningCount++ }
    }
    if (-not $?) { throw 'Independent startup returned an error.' }
    $receipt.Status = 'completed'
    $receipt.Reason = 'startup_completed'
    $receipt.ExitCode = 0
} catch {
    if ($receipt.Status -eq 'running') { $receipt.Status = 'failed' }
} finally {
    $receipt.FinishedUtc = [datetime]::UtcNow.ToString('o')
    Write-QQBotStartupReceipt -Name 'result.json'
}
exit $receipt.ExitCode
'@
    $source = $template.Replace('__RECEIPT__', (ConvertTo-QQBotPowerShellLiteral $ReceiptDirectory)).
        Replace('__NONCE__', (ConvertTo-QQBotPowerShellLiteral $Nonce)).
        Replace('__SID__', (ConvertTo-QQBotPowerShellLiteral $OwnerSid)).
        Replace('__SESSION__', ([string]$SessionId)).
        Replace('__ENVIRONMENT_MARKER__', (ConvertTo-QQBotPowerShellLiteral $EnvironmentMarker)).
        Replace('__HELPER__', (ConvertTo-QQBotPowerShellLiteral $HelperPath)).
        Replace('__SCRIPT__', (ConvertTo-QQBotPowerShellLiteral $ScriptPath)).
        Replace('__SWITCHES__', ($switchArguments -join ' '))
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($source))
}

function Wait-QQBotIndependentStartupReceipt {
    param(
        [Parameter(Mandatory=$true)][string]$ReceiptDirectory,
        [Parameter(Mandatory=$true)][string]$Nonce,
        [Parameter(Mandatory=$true)][uint32]$ProcessId,
        [Parameter(Mandatory=$true)][string]$OwnerSid,
        [Parameter(Mandatory=$true)][int]$SessionId,
        [ValidateRange(1,600)][int]$TimeoutSeconds = 120
    )
    $resultPath = Join-Path $ReceiptDirectory 'result.json'
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
            try {
                $result = Get-Content -Raw -LiteralPath $resultPath -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            } catch {
                throw "Independent startup receipt is unreadable; do not retry automatically. Receipt: $ReceiptDirectory"
            }
            if ($result.SchemaVersion -ne 1 -or $result.Nonce -cne $Nonce -or
                $result.ProcessId -ne $ProcessId -or $result.OwnerSid -cne $OwnerSid -or
                $result.SessionId -ne $SessionId) {
                throw "Independent startup receipt identity mismatch; do not retry automatically. Receipt: $ReceiptDirectory"
            }
            if ($result.Status -eq 'completed' -and $result.ExitCode -eq 0 -and $result.PackageIndependent -eq $true) {
                return $result
            }
            # Only fixed machine-readable reasons may appear in a receipt.
            $knownReasons = @('bootstrap_failed','owner_mismatch','session_mismatch','package_identity_retained','startup_failed')
            $reason = if ($knownReasons -contains $result.Reason) { $result.Reason } else { 'invalid_result' }
            throw "Independent startup failed ($reason); do not retry automatically. Receipt: $ReceiptDirectory"
        }
        Start-Sleep -Milliseconds 250
    }
    # The independent process may still be running. Do not kill it, start a
    # replacement or fall through into the packaged process's original body.
    throw "Independent startup completion is unconfirmed after $TimeoutSeconds seconds; do not retry automatically. Receipt: $ReceiptDirectory"
}

function Invoke-QQBotIndependentStartup {
    param(
        [Parameter(Mandatory=$true)][string]$ScriptPath,
        [switch]$SkipQQConnection,
        [switch]$AutomaticServiceRecovery
    )
    if (-not (Get-QQBotCurrentPackageState)) { return $false }
    $resolvedScript = (Resolve-Path -LiteralPath $ScriptPath -ErrorAction Stop).ProviderPath
    $scriptDirectory = Split-Path $resolvedScript -Parent
    $root = Split-Path $scriptDirectory -Parent
    $helper = Join-Path $scriptDirectory 'IndependentStartup.Common.ps1'
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
        throw 'Independent startup helper is missing; packaged startup is blocked.'
    }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $ownerSid = $identity.User.Value
    $sessionId = ([System.Diagnostics.Process]::GetCurrentProcess()).SessionId
    $location = Get-Location
    if ($location.Provider.Name -ne 'FileSystem') {
        throw 'Independent startup requires a filesystem location; packaged startup is blocked.'
    }
    $nonce = [guid]::NewGuid().ToString('N')
    $environmentMarker = 'QQBOT_INDEPENDENT_ENV_' + $nonce
    $receiptRoot = Get-QQBotPrivateReceiptRoot -OwnerSid $identity.User
    $receiptDirectory = Join-Path $receiptRoot $nonce
    New-QQBotPrivateReceiptDirectory -Path $receiptDirectory -OwnerSid $identity.User
    $encoded = New-QQBotIndependentStartupCommand -ScriptPath $resolvedScript -HelperPath $helper `
        -ReceiptDirectory $receiptDirectory -Nonce $nonce -OwnerSid $ownerSid -SessionId $sessionId -EnvironmentMarker $environmentMarker `
        -SkipQQConnection:$SkipQQConnection -AutomaticServiceRecovery:$AutomaticServiceRecovery
    $powerShellExecutable = ([System.Diagnostics.Process]::GetCurrentProcess()).MainModule.FileName
    if ((Split-Path $powerShellExecutable -Leaf) -notin @('powershell.exe','pwsh.exe') -or
        -not (Test-Path -LiteralPath $powerShellExecutable -PathType Leaf)) {
        throw 'A supported PowerShell executable is unavailable; packaged startup is blocked.'
    }
    $commandLine = '"' + $powerShellExecutable + '" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand ' + $encoded
    # WMI receives the environment out of band. Preserve the caller's complete
    # environment without putting credentials in a command line or receipt.
    $environment = [System.Environment]::GetEnvironmentVariables('Process')
    $environmentBlock = @($environment.Keys | ForEach-Object { ([string]$_) + '=' + ([string]$environment[$_]) })
    $encodedEnvironment = @{}
    foreach ($name in $environment.Keys) {
        # Encoding keeps date-shaped values as strings in every PS JSON parser.
        $encodedEnvironment[[string]$name] = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$environment[$name]))
    }
    $startupMetadata = @{
        Environment=$encodedEnvironment
        PowerShellLocation=$location.ProviderPath
        ProcessCurrentDirectory=[Environment]::CurrentDirectory
    }
    $serializedEnvironment = $startupMetadata | ConvertTo-Json -Compress -Depth 4
    if ($serializedEnvironment.Length -gt 30000) {
        throw 'The startup environment exceeds the safe WMI transfer limit; packaged startup is blocked.'
    }
    $environmentBlock += $environmentMarker + '=' + $serializedEnvironment
    try {
        $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{
            ShowWindow = [uint16]0
            EnvironmentVariables = [string[]]$environmentBlock
        } -ErrorAction Stop
        $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine = $commandLine
            CurrentDirectory = [Environment]::CurrentDirectory
            ProcessStartupInformation = $startup
        } -ErrorAction Stop
    } catch {
        # Do not print the WMI invocation/exception: it may contain env values.
        throw "Independent startup launch failed; no retry was attempted. Receipt: $receiptDirectory"
    }
    if ($created.ReturnValue -ne 0 -or -not $created.ProcessId) {
        throw "Independent startup launch was rejected (WMI code $($created.ReturnValue)); no retry was attempted. Receipt: $receiptDirectory"
    }
    $result = Wait-QQBotIndependentStartupReceipt -ReceiptDirectory $receiptDirectory -Nonce $nonce `
        -ProcessId ([uint32]$created.ProcessId) -OwnerSid $ownerSid -SessionId $sessionId
    Write-Host "Independent startup completed (PID $($result.ProcessId)). Receipt: $receiptDirectory"
    if ($result.WarningCount -gt 0) {
        Write-Warning "Independent startup reported $($result.WarningCount) warning(s); check the normal protected service records. Completion does not prove service or business health."
    }
    return $true
}
