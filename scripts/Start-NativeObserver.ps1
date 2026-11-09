param([switch]$Execute)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
# This helper only starts the reviewed observation script. It never controls QQ.
$observerRoot='D:\QQBot'
$observerScript=Join-Path $observerRoot 'scripts\Watch-QQProcessLifecycle.ps1'
$observerScriptSha='81F9A85AC97AA3B774C2A52E393FAF3DA40B074AF7C7D21916808E944BCBE338'
$observerOutput=Join-Path $observerRoot 'logs'
$observerConfig=Join-Path $observerRoot 'config\qq-native-auto-observer.json'
$observerState=Join-Path $observerRoot 'run\native-observer-launch.json'
. (Join-Path $observerRoot 'scripts\NativeStartup.Common.ps1')
. (Join-Path $observerRoot 'staging\napcat-trusted-native-bridge-review-v4.18.41\daily-tools\NativeDaily.Common.ps1')

function Test-ObserverPid($Value) {return ($Value -is [int] -or $Value -is [long] -or $Value -is [uint32]) -and $Value -gt 0 -and $Value -le [int]::MaxValue}
function Assert-ObserverLocalPath([string]$Path) {
 $full=[IO.Path]::GetFullPath($Path)
 if(!$full.StartsWith($observerRoot+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'observer_helper_path_outside_bot_root'}
 for($p=$full;$p -and $p.Length -ge $observerRoot.Length;$p=Split-Path $p -Parent){
   if((Test-Path -LiteralPath $p) -and ((Get-Item -LiteralPath $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'observer_helper_reparse_rejected'}
   if($p -eq $observerRoot){break}
 }
}
function Assert-ObserverTime($Value) {
 if($Value -isnot [string] -and $Value -isnot [datetime] -and $Value -isnot [datetimeoffset]){throw 'observer_helper_timestamp_invalid'}
 $v=[datetimeoffset]$Value;if($v.Year -lt 2026 -or $v.Year -gt 2100){throw 'observer_helper_timestamp_invalid'}
}
function Assert-ObserverIdentity($Value,[string]$PowerShell) {
 $fields=@('Id','Parent','Exe','CreatedUtc','Name')
 if($Value -isnot [pscustomobject] -or @($Value.PSObject.Properties).Count -ne 5 -or @($Value.PSObject.Properties.Name|Where-Object {$_ -notin $fields}).Count -or
   !(Test-ObserverPid $Value.Id) -or !(Test-ObserverPid $Value.Parent) -or $Value.Exe -ne $PowerShell -or $Value.Name -ne 'pwsh.exe'){throw 'observer_helper_identity_invalid'}
 Assert-ObserverTime $Value.CreatedUtc
}
function Test-ObserverIdentity($Actual,$Expected) {
 if(!$Actual -or !$Expected){return $false}
 return (Test-ObserverPid $Actual.Id) -and $Actual.Id -eq $Expected.Id -and $Actual.Parent -eq $Expected.Parent -and
   $Actual.Exe -eq $Expected.Exe -and $Actual.Name -eq $Expected.Name -and
   ([datetimeoffset]$Actual.CreatedUtc).UtcDateTime.Ticks -eq ([datetimeoffset]$Expected.CreatedUtc).UtcDateTime.Ticks
}
function Assert-ObserverRun([string]$RunId,[string]$LogPath) {
 if($RunId -notmatch '^auto-recovery-[A-Za-z0-9_-]{1,70}$' -or $LogPath -ne (Join-Path $observerOutput ('qq-process-lifecycle-'+$RunId+'.jsonl'))){throw 'observer_helper_run_invalid'}
 Assert-ObserverLocalPath $LogPath
}
function Get-ObserverArgumentLine([string]$RunId) {
 '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$observerScript+'" -OutputDirectory "'+$observerOutput+'" -RunId '+$RunId+' -DurationSeconds 0 -Mode Poll'
}
function Write-ObserverAtomic([string]$Path,$Value) {
 if($Path -notin @($observerConfig,$observerState)){throw 'observer_helper_write_target_invalid'}
 Assert-ObserverLocalPath $Path
 $temporary=$Path+'.new-'+[guid]::NewGuid().ToString('N');$backup=$Path+'.old-'+[guid]::NewGuid().ToString('N');$stream=$null
 try {
   $stream=[IO.FileStream]::new($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
   $bytes=[Text.Encoding]::UTF8.GetBytes(($Value|ConvertTo-Json -Depth 10));$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true);$stream.Dispose();$stream=$null
   Protect-PilotPath $temporary
   if([IO.File]::Exists($Path)){[IO.File]::Replace($temporary,$Path,$backup)}else{[IO.File]::Move($temporary,$Path)}
   Assert-PilotPrivateAcl $Path
 } finally {
   if($stream){$stream.Dispose()}
   if([IO.File]::Exists($temporary)){[IO.File]::Delete($temporary)}
   # Keep the previous original metadata bytes as a private sibling audit.
   if([IO.File]::Exists($backup)){Assert-PilotPrivateAcl $backup}
 }
}
function Read-ObserverConfig([string]$PowerShell) {
 if(!(Test-Path -LiteralPath $observerConfig)){return $null}
 Assert-ObserverLocalPath $observerConfig;Assert-PilotPrivateAcl $observerConfig
 $value=Read-PilotJson $observerConfig;$fields=@('schema','run_id','observer','log_path')
 if(@($value.PSObject.Properties).Count -ne 4 -or @($value.PSObject.Properties.Name|Where-Object {$_ -notin $fields}).Count -or
   ($value.schema -isnot [int] -and $value.schema -isnot [long]) -or $value.schema -ne 1){throw 'observer_helper_configuration_invalid'}
 Assert-ObserverRun $value.run_id $value.log_path;Assert-ObserverIdentity $value.observer $PowerShell
 $value
}
function Read-ObserverState([string]$PowerShell) {
 if(!(Test-Path -LiteralPath $observerState)){return [pscustomobject]@{schema=1;phase='idle';run_id=$null;log_path=$null;observer=$null;attempts=@();last_attempt_utc=$null}}
 Assert-ObserverLocalPath $observerState;Assert-PilotPrivateAcl $observerState
 $value=Read-PilotJson $observerState;$fields=@('schema','phase','run_id','log_path','observer','attempts','last_attempt_utc')
 if(@($value.PSObject.Properties).Count -ne 7 -or @($value.PSObject.Properties.Name|Where-Object {$_ -notin $fields}).Count -or
   ($value.schema -isnot [int] -and $value.schema -isnot [long]) -or $value.schema -ne 1 -or $value.phase -notin @('idle','launch_claimed','waiting_metadata','ready') -or
   $value.attempts -isnot [array] -or $value.attempts.Count -gt 64){throw 'observer_helper_launch_state_invalid'}
 foreach($a in @($value.attempts)){Assert-ObserverTime $a}
 if($null -ne $value.last_attempt_utc){Assert-ObserverTime $value.last_attempt_utc}
 if($value.phase -ne 'idle'){Assert-ObserverRun $value.run_id $value.log_path}
 if($null -ne $value.observer){Assert-ObserverIdentity $value.observer $PowerShell}
 if($value.phase -in @('waiting_metadata','ready') -and $null -eq $value.observer){throw 'observer_helper_launch_identity_missing'}
 $value
}
function Get-ObserverReady($Expected,[string]$RunId,[string]$LogPath,[switch]$ProtectFiles) {
 $actual=Get-PilotProcess $Expected.Id
 if(!(Test-ObserverIdentity $actual $Expected)){return $null}
 $row=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$Expected.Id) -ErrorAction Stop
 if(!$row -or !$row.CommandLine.EndsWith((Get-ObserverArgumentLine $RunId),[StringComparison]::OrdinalIgnoreCase)){throw 'observer_helper_command_not_reviewed'}
 $base=$LogPath.Substring(0,$LogPath.Length-6);$meta=$base+'.observer.json'
 if((Test-Path -LiteralPath ($base+'.completed.json')) -or (Test-Path -LiteralPath ($base+'.stop'))){return $null}
 if(!(Test-Path -LiteralPath $meta) -or !(Test-Path -LiteralPath $LogPath)){return $null}
 Assert-ObserverLocalPath $meta;Assert-ObserverLocalPath $LogPath
 if($ProtectFiles){Protect-PilotPath $meta;Protect-PilotPath $LogPath}
 Assert-PilotPrivateAcl $meta;Assert-PilotPrivateAcl $LogPath
 $record=Read-PilotJson $meta
 if($record.schema -ne 1 -or $record.pid -ne $Expected.Id -or $record.executable -ne $Expected.Exe -or
   ([datetimeoffset]$record.created_utc).UtcDateTime.Ticks -ne ([datetimeoffset]$Expected.CreatedUtc).UtcDateTime.Ticks -or
   $record.run_id -ne $RunId -or $record.log_path -ne $LogPath -or $record.requested_mode -ne 'Poll' -or
   $record.observes_only -isnot [bool] -or !$record.observes_only -or $record.stores_command_line -isnot [bool] -or $record.stores_command_line -or
   $record.stores_window_titles -isnot [bool] -or $record.stores_window_titles){throw 'observer_helper_metadata_mismatch'}
 # Re-query the same exact generation after reading its claimed metadata.
 if(!(Test-ObserverIdentity (Get-PilotProcess $Expected.Id) $Expected)){return $null}
 [pscustomobject]@{schema=1;run_id=$RunId;observer=$Expected;log_path=$LogPath}
}
function Invoke-NativeObserverEnsure([switch]$Execute) {
 $settings=Read-NativeStartupSettings $observerRoot
 if(!$settings.auto_recover){return [pscustomobject]@{Mode='WAIT';Reason='automatic_recovery_disabled'}}
 if(Test-Path -LiteralPath (Join-Path $observerRoot 'run\services-paused')){return [pscustomobject]@{Mode='WAIT';Reason='services_paused'}}
 Assert-ObserverLocalPath $observerScript;Assert-ObserverLocalPath $observerConfig;Assert-ObserverLocalPath $observerState;Assert-ObserverLocalPath $observerOutput
 $hashStream=[IO.FileStream]::new($observerScript,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
 $hashAlgo=[Security.Cryptography.SHA256]::Create()
 try {$actualHash=([BitConverter]::ToString($hashAlgo.ComputeHash($hashStream))).Replace('-','')}
 finally {$hashStream.Dispose();$hashAlgo.Dispose()}
 if($actualHash -ne $observerScriptSha){throw 'observer_helper_reviewed_script_changed'}
 if($PSVersionTable.PSVersion.Major -lt 7 -and $Execute){
   & $settings.powershell7 -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $PSCommandPath -Execute
   if($LASTEXITCODE -ne 0){throw 'observer_helper_powershell7_failed'}
   return
 }
 $mutex=$null;$owned=$false
 try {
   if($Execute){
     $mutex=[Threading.Mutex]::new($false,'Local\FeiyuNativeAutoObserverEnsure')
     try{$owned=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$owned=$true}
     if(!$owned){return [pscustomobject]@{Mode='WAIT';Reason='observer_ensure_in_progress'}}
     # Recheck both authorization gates after acquiring the exclusive claim.
     $settings=Read-NativeStartupSettings $observerRoot
     if(!$settings.auto_recover -or (Test-Path -LiteralPath (Join-Path $observerRoot 'run\services-paused'))){return [pscustomobject]@{Mode='WAIT';Reason='recovery_paused'}}
   }
   $config=Read-ObserverConfig $settings.powershell7
   if($config){
     $ready=Get-ObserverReady $config.observer $config.run_id $config.log_path
     if($ready){return [pscustomobject]@{Mode='READY';Reason='verified_observer_reused';Observer=$ready.observer;LogPath=$ready.log_path}}
     if(Test-ObserverIdentity (Get-PilotProcess $config.observer.Id) $config.observer){return [pscustomobject]@{Mode='WAIT';Reason='configured_exact_observer_not_ready'}}
   }
   $state=Read-ObserverState $settings.powershell7
   if($state.phase -eq 'launch_claimed' -and $null -eq $state.observer){return [pscustomobject]@{Mode='WAIT';Reason='previous_launch_outcome_unknown'}}
   if($null -ne $state.observer){
     $actual=Get-PilotProcess $state.observer.Id
     if(Test-ObserverIdentity $actual $state.observer){
       $ready=Get-ObserverReady $state.observer $state.run_id $state.log_path -ProtectFiles:$Execute
       if($ready){
         if($Execute){Write-ObserverAtomic $observerConfig $ready;$state.phase='ready';Write-ObserverAtomic $observerState $state}
         return [pscustomobject]@{Mode='READY';Reason='pending_observer_verified';Observer=$ready.observer;LogPath=$ready.log_path}
       }
       return [pscustomobject]@{Mode='WAIT';Reason='exact_observer_waiting_metadata'}
     }
     # A dead or reused PID is never adopted or stopped. A new watcher is allowed.
   }
   $now=[datetimeoffset]::UtcNow
   $recent=@($state.attempts|Where-Object {([datetimeoffset]$_) -gt $now.AddHours(-1)})
   if($recent.Count -ge 3){return [pscustomobject]@{Mode='WAIT';Reason='observer_launch_hour_limit'}}
   if($null -ne $state.last_attempt_utc -and $now -lt ([datetimeoffset]$state.last_attempt_utc).AddMinutes(2)){return [pscustomobject]@{Mode='WAIT';Reason='observer_launch_backoff'}}
   if(!$Execute){return [pscustomobject]@{Mode='PLAN';Reason='start_reviewed_infinite_observer';StartsQq=$false;StopsProcesses=$false;ScriptSha256=$observerScriptSha}}
   $runId='auto-recovery-'+[datetime]::UtcNow.ToString('yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8)
   $logPath=Join-Path $observerOutput ('qq-process-lifecycle-'+$runId+'.jsonl')
   $state=[pscustomobject]@{schema=1;phase='launch_claimed';run_id=$runId;log_path=$logPath;observer=$null;attempts=@($recent)+@($now.ToString('o'));last_attempt_utc=$now.ToString('o')}
   Write-ObserverAtomic $observerState $state
   $child=Start-Process -FilePath $settings.powershell7 -ArgumentList (Get-ObserverArgumentLine $runId) -WorkingDirectory $observerRoot -WindowStyle Hidden -PassThru
   $expected=Get-PilotProcess $child.Id
   if(!$expected -or $expected.Parent -ne $PID -or $expected.Exe -ne $settings.powershell7 -or $expected.Name -ne 'pwsh.exe' -or
     [Math]::Abs(([datetimeoffset]$expected.CreatedUtc).UtcDateTime.Ticks-$child.StartTime.ToUniversalTime().Ticks) -ge 10){throw 'observer_helper_started_generation_unknown'}
   $state.observer=$expected;$state.phase='waiting_metadata';Write-ObserverAtomic $observerState $state
   $deadline=[datetimeoffset]::UtcNow.AddSeconds(12)
   do {
     $ready=Get-ObserverReady $expected $runId $logPath -ProtectFiles
     if($ready){Write-ObserverAtomic $observerConfig $ready;$state.phase='ready';Write-ObserverAtomic $observerState $state;return [pscustomobject]@{Mode='READY';Reason='reviewed_observer_started';Observer=$expected;LogPath=$logPath}}
     Start-Sleep -Milliseconds 250
   }while([datetimeoffset]::UtcNow -lt $deadline)
   [pscustomobject]@{Mode='WAIT';Reason='started_exact_observer_waiting_metadata';Observer=$expected}
 } finally {if($owned){$mutex.ReleaseMutex()};if($mutex){$mutex.Dispose()}}
}

Invoke-NativeObserverEnsure -Execute:$Execute
