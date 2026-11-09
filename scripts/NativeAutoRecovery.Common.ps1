# Controlled automatic QQ recovery. Importing this file performs no I/O.
$script:NativeAutoStage='D:\QQBot\staging\napcat-trusted-native-bridge-review-v4.18.41'
$script:NativeAutoExe='D:\QQBot\apps\QQ\QQ.exe'
$script:NativeAutoFingerprint='3b15e3e1772f3ce72148544c0266d41cfd8bd56c83fae2f2dc62d74cfab0f4dd'
$script:NativeAutoPersonal=[pscustomobject]@{Id=20164;Parent=11828;Exe='D:\QQ\QQ.exe';CreatedUtc='2026-10-22T09:57:34.566284Z';Name='QQ.exe'}

function Test-NativeAutoIdentity($Actual,$Expected) {
 if(!$Actual -or !$Expected){return $false}
 try { return (Test-NativeAutoPid $Actual.Id) -and (Test-NativeAutoPid $Expected.Id) -and $Actual.Id -eq $Expected.Id -and
   $Actual.Exe -eq $Expected.Exe -and $Actual.Name -eq 'QQ.exe' -and $Expected.Name -eq 'QQ.exe' -and
   $Actual.Parent -eq $Expected.Parent -and
   ([datetimeoffset]$Actual.CreatedUtc).UtcDateTime.Ticks -eq ([datetimeoffset]$Expected.CreatedUtc).UtcDateTime.Ticks
 } catch { return $false }
}
function Test-NativeAutoPid($Value) {return ($Value -is [int] -or $Value -is [long] -or $Value -is [uint32]) -and $Value -gt 0 -and $Value -le [int]::MaxValue}
function Get-NativeAutoGenerationKey($Process) {
 if(!$Process){return ''}; ('{0}:{1}' -f $Process.Id,([datetimeoffset]$Process.CreatedUtc).UtcDateTime.Ticks)
}
function New-NativeAutoDecision([string]$Action,[string]$Reason,$Target=$null,[string]$EventKind='health_probe') {
 [pscustomobject]@{Action=$Action;Reason=$Reason;Target=$Target;EventKind=$EventKind}
}
function Get-NativeAutoDecision($Context,$State,[datetimeoffset]$Now) {
 if($Context.Allowed -isnot [bool] -or !$Context.Allowed){return New-NativeAutoDecision 'Wait' $Context.GateReason}
 $roots=@($Context.Roots);$holders=@($Context.Holders);$rootIds=@($roots|ForEach-Object {$_.Id})
 if($roots.Count -gt 1){return New-NativeAutoDecision 'Attention' 'multiple_dedicated_qq_roots'}
 if($holders.Count -gt 1 -or @($holders|Where-Object {$_ -notin $rootIds}).Count){return New-NativeAutoDecision 'Attention' 'unrelated_bot_database_holder'}
 if($Context.NativeOnline -is [bool] -and $Context.NativeOnline){
   if($Context.Pending -and $Context.Pending.Active -eq $true){return New-NativeAutoDecision 'Reconnect' 'pending_native_binding_finalization' $Context.NativeProcess 'snow_reconnect'}
   if($Context.SnowOnline -is [bool] -and $Context.SnowOnline){return New-NativeAutoDecision 'Healthy' 'native_and_snow_verified'}
   return New-NativeAutoDecision 'Reconnect' 'snow_same_generation_not_online' $Context.NativeProcess 'snow_reconnect'
 }
 if($Context.Pending -and $Context.Pending.Active -eq $true){
   if($Context.PendingAlive){return New-NativeAutoDecision 'Wait' 'exact_native_generation_waiting_for_login'}
   return New-NativeAutoDecision 'Attention' 'unconfirmed_native_generation_exited'
 }
 if(!$Context.BindingProcess){return New-NativeAutoDecision 'Attention' 'verified_binding_missing'}
 if($Context.BoundAlive){return New-NativeAutoDecision 'Wait' 'known_qq_alive_native_bridge_unavailable'}
 if(!$roots.Count){
   if($holders.Count){return New-NativeAutoDecision 'Attention' 'bot_holder_without_known_root'}
   return New-NativeAutoDecision 'Start' 'verified_bound_generation_exited' $Context.BindingProcess 'known_bound_exit'
 }
 if($Context.RelaunchRoot -and (Test-NativeAutoIdentity $roots[0] $Context.RelaunchRoot)){
   return New-NativeAutoDecision 'Restart' 'verified_qq_internal_relaunch' $roots[0] 'known_relaunch'
 }
 New-NativeAutoDecision 'Attention' 'unverified_dedicated_qq_root'
}
function Get-NativeAutoBudget($State,[datetimeoffset]$Now) {
 if($null -ne $State.ActiveAttempt){return [pscustomobject]@{Allowed=$false;Reason='previous_attempt_outcome_unknown'}}
 if($State.NextEligibleUtc -and $Now -lt [datetimeoffset]$State.NextEligibleUtc){return [pscustomobject]@{Allowed=$false;Reason='durable_backoff_active'}}
 $recent=@($State.Attempts|Where-Object {([datetimeoffset]$_.AtUtc) -gt $Now.AddHours(-1)})
 if($recent.Count -ge 6){return [pscustomobject]@{Allowed=$false;Reason='rolling_hour_attempt_limit'}}
 [pscustomobject]@{Allowed=$true;Reason='attempt_allowed'}
}
function Find-NativeAutoRelaunch($BindingProcess,$Root,$Records) {
 if(!$BindingProcess -or !$Root -or $Root.Exe -ne $BindingProcess.Exe -or $Root.Relaunch -ne $true){return $null}
 $cursor=$Root;$visited=@{};$boundTicks=([datetimeoffset]$BindingProcess.CreatedUtc).UtcDateTime.Ticks
 $rootTicks=([datetimeoffset]$Root.CreatedUtc).UtcDateTime.Ticks
 $rootEvents=@($Records|Where-Object {$_.event -in @('process_observed_started','process_start') -and $_.process -and $_.process.pid -eq $Root.Id -and
   $_.process.parent_pid -eq $Root.Parent -and $_.process.executable -eq $Root.Exe -and $_.process.name -eq 'QQ.exe' -and $_.process.role.relaunch -eq $true -and
   ([datetimeoffset]$_.process.created_utc).UtcDateTime.Ticks -eq $rootTicks})
 if($rootEvents.Count -ne 1){return $null}
 $reusedBound=@($Records|Where-Object {$_.event -in @('process_observed_started','process_start') -and $_.process -and $_.process.pid -eq $BindingProcess.Id -and
   ([datetimeoffset]$_.process.created_utc).UtcDateTime.Ticks -gt $boundTicks -and ([datetimeoffset]$_.process.created_utc).UtcDateTime.Ticks -le $rootTicks})
 if($reusedBound.Count){return $null}
 $disappeared=@($Records|Where-Object {$_.event -in @('process_observed_disappeared','process_stop') -and $_.process -and
   $_.process.pid -eq $BindingProcess.Id -and $_.process.executable -eq $BindingProcess.Exe -and
   ([datetimeoffset]$_.process.created_utc).UtcDateTime.Ticks -eq $boundTicks})
 if(!$disappeared.Count){return $null}
 $boundExitTicks=($disappeared|ForEach-Object {if($_.PSObject.Properties['observed_utc']){([datetimeoffset]$_.observed_utc).UtcDateTime.Ticks}else{([datetimeoffset]$_.at_utc).UtcDateTime.Ticks}}|Measure-Object -Minimum).Minimum
 for($depth=0;$depth -lt 4;$depth++){
   $key=Get-NativeAutoGenerationKey $cursor
   if($visited.ContainsKey($key)){return $null};$visited[$key]=$true
   if($cursor.Parent -eq $BindingProcess.Id){
     $childTicks=([datetimeoffset]$cursor.CreatedUtc).UtcDateTime.Ticks
     if($childTicks -gt $boundTicks -and $childTicks -le $boundExitTicks){return $Root};return $null
   }
   $parents=@($Records|Where-Object {$_.event -in @('process_observed_started','process_start') -and $_.process -and
     $_.process.pid -eq $cursor.Parent -and $_.process.executable -eq $BindingProcess.Exe -and $_.process.name -eq 'QQ.exe' -and
     $_.process.role.relaunch -eq $true -and
     ([datetimeoffset]$_.process.created_utc).UtcDateTime.Ticks -gt $boundTicks -and
     ([datetimeoffset]$_.process.created_utc).UtcDateTime.Ticks -le ([datetimeoffset]$cursor.CreatedUtc).UtcDateTime.Ticks})
   if($parents.Count -ne 1){return $null}
   $p=$parents[0].process
   $parentExit=@($Records|Where-Object {$_.event -eq 'process_observed_disappeared' -and $_.process -and $_.process.pid -eq $p.pid -and
     $_.process.executable -eq $p.executable -and ([datetimeoffset]$_.process.created_utc).UtcDateTime.Ticks -eq ([datetimeoffset]$p.created_utc).UtcDateTime.Ticks})
   if(!$parentExit.Count -or ([datetimeoffset]$cursor.CreatedUtc).UtcDateTime.Ticks -gt (($parentExit|ForEach-Object {([datetimeoffset]$_.observed_utc).UtcDateTime.Ticks}|Measure-Object -Minimum).Minimum)){return $null}
   $cursor=[pscustomobject]@{Id=[int]$p.pid;Parent=[int]$p.parent_pid;Exe=[string]$p.executable;CreatedUtc=([datetimeoffset]$p.created_utc).ToUniversalTime().ToString('o');Name='QQ.exe';Relaunch=$true}
 }
 return $null
}
function Read-NativeAutoSharedBytes([string]$Path,[int]$Maximum=16777216) {
 $item=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
 if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'auto_evidence_path_invalid'}
 $stream=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite)
 try {
   if($stream.Length -gt $Maximum){throw 'auto_evidence_bound_exceeded'}
   $bytes=[byte[]]::new($stream.Length);$offset=0
   while($offset -lt $bytes.Length){$n=$stream.Read($bytes,$offset,$bytes.Length-$offset);if(!$n){throw 'auto_evidence_short_read'};$offset+=$n}
   return ,$bytes
 } finally {$stream.Dispose()}
}
function Get-NativeAutoEvidence($BindingProcess,$Root) {
 $configPath='D:\QQBot\config\qq-native-auto-observer.json';Assert-PilotPrivateAcl $configPath
 $config=Read-PilotJson $configPath
 $fields=@('schema','run_id','observer','log_path')
 if(@($config.PSObject.Properties).Count -ne 4 -or @($config.PSObject.Properties.Name|Where-Object {$_ -notin $fields}).Count -or
   $config.schema -ne 1 -or $config.run_id -isnot [string] -or $config.run_id -notmatch '^auto-recovery-[A-Za-z0-9_-]{1,70}$' -or
   $config.log_path -ne ('D:\QQBot\logs\qq-process-lifecycle-'+$config.run_id+'.jsonl') -or
   $config.observer.Exe -notin @('C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe','C:\Users\LOCAL_USER\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe') -or
   $config.observer.Name -notin @('powershell.exe','pwsh.exe') -or !(Test-NativeAutoPid $config.observer.Id) -or !(Test-NativeAutoPid $config.observer.Parent)){throw 'auto_observer_configuration_invalid'}
 $base=$config.log_path.Substring(0,$config.log_path.Length-6)
 Assert-PilotPrivateAcl ($base+'.observer.json');Assert-PilotPrivateAcl $config.log_path
 $observer=Read-PilotJson ($base+'.observer.json')
 $actual=Get-PilotProcess ([int]$observer.pid)
 if(!$actual -or $observer.observes_only -isnot [bool] -or !$observer.observes_only -or $observer.stores_command_line -isnot [bool] -or $observer.stores_command_line -or
   $observer.pid -ne $config.observer.Id -or $observer.log_path -ne $config.log_path -or $observer.run_id -ne $config.run_id -or
   $actual.Id -ne $config.observer.Id -or $actual.Exe -ne $config.observer.Exe -or $actual.Parent -ne $config.observer.Parent -or $actual.Name -ne $config.observer.Name -or
   ([datetimeoffset]$actual.CreatedUtc).UtcDateTime.Ticks -ne ([datetimeoffset]$config.observer.CreatedUtc).UtcDateTime.Ticks -or
   ([datetimeoffset]$actual.CreatedUtc).UtcDateTime.Ticks -ne ([datetimeoffset]$observer.created_utc).UtcDateTime.Ticks -or
   (Test-Path -LiteralPath ($base+'.completed.json')) -or (Test-Path -LiteralPath ($base+'.stop'))){throw 'auto_continuous_observer_unverified'}
 $bytes=Read-NativeAutoSharedBytes ($base+'.jsonl')
 $sha=[Security.Cryptography.SHA256]::Create()
 try {$hash=[Convert]::ToHexString($sha.ComputeHash($bytes)).ToLowerInvariant()}finally{$sha.Dispose()}
 $text=[Text.UTF8Encoding]::new($false,$true).GetString($bytes)
 $lines=$text.Split([char]10);$records=@()
 for($i=0;$i -lt $lines.Length-1;$i++){if($lines[$i].Trim()){$records+=@($lines[$i]|ConvertFrom-Json)}}
 [pscustomobject]@{Root=(Find-NativeAutoRelaunch $BindingProcess $Root $records);Sha256=$hash;Bytes=$bytes}
}
function Assert-NativeAutoPersonal {
 $actual=Get-PilotProcess $script:NativeAutoPersonal.Id
 # A personal QQ closed by its owner (or before a new Windows login) is not an
 # automatic recovery target. A present but changed protected PID blocks action.
 if(!$actual){return}
 if(!(Test-NativeAutoIdentity $actual $script:NativeAutoPersonal)){throw 'auto_protected_personal_generation_changed'}
}
function Get-NativeAutoContext([string]$Root) {
 $settings=Read-NativeStartupSettings $Root
 if(!$settings.auto_recover){return [pscustomobject]@{Allowed=$false;GateReason='automatic_recovery_disabled'}}
 if(Test-Path -LiteralPath (Join-Path $Root 'run\services-paused')){return [pscustomobject]@{Allowed=$false;GateReason='services_paused'}}
 if(Test-Path -LiteralPath (Join-Path $Root 'config\qq-account-mismatch.json')){return [pscustomobject]@{Allowed=$false;GateReason='account_confirmation_required'}}
 Assert-PilotStage; Assert-NativeAutoPersonal
 $configuration=Get-DailyConfiguration
 if($configuration.Fingerprint -ne $script:NativeAutoFingerprint -or $configuration.ReceiptDirectory -ne (Join-Path $script:NativeAutoStage 'pilot-runs\20261026-122046-40bd601d\receipts')){throw 'auto_delivery_namespace_changed'}
 $client=Read-PilotJson (Join-Path $Root 'config\qq-client.json')
 if($client.executable -ne $script:NativeAutoExe -or $client.expectedUin -ne '0' -or $client.arguments){throw 'auto_client_configuration_changed'}
 $ports=@(Get-NetTCPConnection -State Listen -ErrorAction Stop)
 $snowPorts=@($ports|Where-Object LocalPort -eq 5099)
 if($snowPorts.Count -ne 1 -or $snowPorts[0].LocalAddress -ne '127.0.0.1'){return [pscustomobject]@{Allowed=$false;GateReason='snow_control_unavailable'}}
 $listeners=@($ports|Where-Object LocalPort -eq 16199)
 if($listeners.Count -gt 1 -or ($listeners.Count -and $listeners[0].LocalAddress -ne '127.0.0.1')){throw 'auto_native_listener_conflict'}
 $bindingPath=Join-Path $script:DailyRoot 'binding.json';Assert-PilotPrivateAcl $bindingPath
 $binding=Read-PilotJson $bindingPath
 if($binding.Schema -ne 1 -or $binding.ConfigurationFingerprint -ne $configuration.Fingerprint -or $binding.ReceiptDirectory -ne $configuration.ReceiptDirectory -or
   $binding.NoHistoricalReplay -ne $true -or $binding.NativeProcess.Exe -ne $script:NativeAutoExe -or $binding.NativeProcess.Name -ne 'QQ.exe'){throw 'auto_verified_binding_invalid'}
 $pending=$null;$pendingAlive=$false;$pendingPath=Join-Path $script:DailyRoot 'pending.json'
 if(Test-Path -LiteralPath $pendingPath){
   Assert-PilotPrivateAcl $pendingPath;$pending=Read-PilotJson $pendingPath;Assert-DailyPendingValue $pending $configuration
   if($pending.NativeProcess.Exe -ne $script:NativeAutoExe){throw 'auto_pending_installation_changed'}
   $p=Get-PilotProcess $pending.NativeProcess.Id
   if($p){if(!(Test-NativeAutoIdentity $p $pending.NativeProcess)){throw 'auto_pending_pid_reused'};$pendingAlive=$true}
 }
 $bound=Get-PilotProcess $binding.NativeProcess.Id
 if($bound -and !(Test-NativeAutoIdentity $bound $binding.NativeProcess)){throw 'auto_bound_pid_reused'}
 $rows=@(Get-CimInstance Win32_Process -Filter "Name='QQ.exe'" -ErrorAction Stop|Where-Object ExecutablePath -eq $script:NativeAutoExe)
 $roots=@($rows|Where-Object {$_.CommandLine -notmatch '(?i)(?:^|\s)--type(?:=|\s)'}|ForEach-Object {
   [pscustomobject]@{Id=[int]$_.ProcessId;Parent=[int]$_.ParentProcessId;Exe=[string]$_.ExecutablePath;CreatedUtc=$_.CreationDate.ToUniversalTime().ToString('o');Name='QQ.exe';Relaunch=($_.CommandLine -match '(?i)(?:^|\s)--relaunch(?:\s|$)')}
 })
 if($roots.Count -eq 1){
   $tree=@(Get-PilotTree $roots[0])
   if(@($rows|Where-Object ProcessId -notin @($tree.Id)).Count){throw 'auto_unbound_dedicated_children_present'}
 } elseif($rows.Count -and !$roots.Count){throw 'auto_orphan_dedicated_children_present'}
 $holders=@(Get-BotHolderPids);$nativeOnline=$false;$snowOnline=$false;$native=$null
 if($listeners.Count){
   $native=Get-PilotProcess ([int]$listeners[0].OwningProcess)
   $expected=$binding.NativeProcess
   if($pending -and $pending.Active){$expected=$pending.NativeProcess}
   if(!(Test-NativeAutoIdentity $native $expected)){throw 'auto_native_owner_not_known_generation'}
   $null=Assert-PilotIdentityBinding $native $configuration.ReceiptDirectory;$nativeOnline=$true
   $headers=Get-SnowPilotHeaders
   try {$list=(Invoke-RestMethod 'http://127.0.0.1:5099/api/processes' -Headers $headers -TimeoutSec 5).list}finally{$headers=$null}
   $target=@($list|Where-Object {($_.pid -is [int] -or $_.pid -is [long]) -and $_.pid -eq $native.Id})
   if($target.Count -ne 1){throw 'auto_snow_exact_process_missing'}
   if([string]$target[0].uin -notin @('0','','0')){throw 'auto_snow_other_account_reported'}
   $snowOnline=[string]$target[0].uin -eq '0' -and $target[0].injected -is [bool] -and $target[0].injected -and
     $target[0].connected -is [bool] -and $target[0].connected -and $target[0].loggedIn -is [bool] -and $target[0].loggedIn -and $target[0].status -eq 'online'
 }
 $evidence=$null
 if(!$bound -and $roots.Count -eq 1 -and !($pending -and $pending.Active)){$evidence=Get-NativeAutoEvidence $binding.NativeProcess $roots[0]}
 [pscustomobject]@{Allowed=$true;GateReason='allowed';Configuration=$configuration;Binding=$binding;BindingProcess=$binding.NativeProcess;
   Pending=$pending;PendingAlive=$pendingAlive;BoundAlive=[bool]$bound;Roots=@($roots);Holders=@($holders);NativeOnline=$nativeOnline;
   SnowOnline=$snowOnline;NativeProcess=$native;RelaunchRoot=$(if($evidence){$evidence.Root}else{$null});Evidence=$evidence}
}
function New-NativeAutoState {
 [pscustomobject]@{Schema=1;Attempts=@();NextEligibleUtc=$null;ActiveAttempt=$null;Failures=0;RestartedGenerations=@();LastResult=$null}
}
function Test-NativeAutoContinuation($Context,$State) {
 if(!$State.ActiveAttempt -or !$State.ActiveAttempt.PSObject.Properties['ContinuationProcess'] -or !$State.ActiveAttempt.ContinuationProcess){return $false}
 return $Context.NativeOnline -eq $true -and (Test-NativeAutoIdentity $Context.NativeProcess $State.ActiveAttempt.ContinuationProcess)
}
function Get-NativeAutoPendingContinuation($Attempt) {
 $path=Join-Path $script:DailyRoot 'pending.json'
 if(!(Test-Path -LiteralPath $path)){return $null}
 $pending=Read-PilotJson $path
 if(!$pending.Active -or $pending.StartId -notmatch '^[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$' -or $pending.NativeProcess.Exe -ne $script:NativeAutoExe){return $null}
 $actual=Get-PilotProcess $pending.NativeProcess.Id
 if(!(Test-NativeAutoIdentity $actual $pending.NativeProcess)){return $null}
 $start=Read-PilotJson (Join-Path $script:DailyRoot ('starts\'+$pending.StartId+'\state.json'))
 if($start.StartId -ne $pending.StartId -or !(Test-NativeAutoIdentity $start.NativeProcess $pending.NativeProcess) -or !$start.Launcher -or
   $start.Launcher.Parent -ne $PID -or ([datetimeoffset]$start.StartedUtc) -lt ([datetimeoffset]$Attempt.AtUtc)){return $null}
 return $pending.NativeProcess
}
function Assert-NativeAutoState($State) {
 $fields=@('Schema','Attempts','NextEligibleUtc','ActiveAttempt','Failures','RestartedGenerations','LastResult')
 if(!$State -or @($State.PSObject.Properties).Count -ne $fields.Count -or @($State.PSObject.Properties.Name|Where-Object {$_ -notin $fields}).Count -or
   ($State.Schema -isnot [int] -and $State.Schema -isnot [long]) -or $State.Schema -ne 1 -or $State.Attempts -isnot [array] -or $State.RestartedGenerations -isnot [array] -or
   @($State.Attempts).Count -gt 128 -or @($State.RestartedGenerations).Count -gt 128 -or ($State.Failures -isnot [int] -and $State.Failures -isnot [long]) -or $State.Failures -lt 0 -or $State.Failures -gt 20){throw 'auto_durable_state_invalid'}
 foreach($a in @($State.Attempts)){Assert-NativeAutoAttempt $a}
 if(@($State.Attempts|ForEach-Object {$_.Id}|Sort-Object -Unique).Count -ne $State.Attempts.Count){throw 'auto_duplicate_attempt_id'}
 if($null -ne $State.ActiveAttempt){
   Assert-NativeAutoAttempt $State.ActiveAttempt
   $linked=@($State.Attempts|Where-Object Id -eq $State.ActiveAttempt.Id)
   if($linked.Count -ne 1 -or !(Test-NativeAutoIdentity $linked[0].Target $State.ActiveAttempt.Target) -or !(Test-NativeAutoIdentity $linked[0].SourceBinding $State.ActiveAttempt.SourceBinding) -or
     $linked[0].Origin -ne $State.ActiveAttempt.Origin -or $linked[0].EventKind -ne $State.ActiveAttempt.EventKind -or
     ([datetimeoffset]$linked[0].AtUtc).UtcDateTime.Ticks -ne ([datetimeoffset]$State.ActiveAttempt.AtUtc).UtcDateTime.Ticks){throw 'auto_claim_not_linked_to_attempt'}
 }
 foreach($key in @($State.RestartedGenerations)){if($key -isnot [string] -or $key -notmatch '^[1-9][0-9]{0,9}:[1-9][0-9]{16,18}$'){throw 'auto_restart_generation_invalid'}}
 if(@($State.RestartedGenerations|Sort-Object -Unique).Count -ne $State.RestartedGenerations.Count){throw 'auto_duplicate_restart_generation'}
 if($null -ne $State.NextEligibleUtc){Assert-NativeAutoTime $State.NextEligibleUtc}
 if($null -ne $State.LastResult){
   if($State.LastResult -isnot [pscustomobject] -or $State.LastResult.Origin -ne 'automatic' -or
     $State.LastResult.Stage -notin @('verified_native_and_snow','attention_or_pending_verification') -or $State.LastResult.UnknownOperationsRetried -ne $false){throw 'auto_last_result_invalid'}
   Assert-NativeAutoTime $State.LastResult.AtUtc
 }
}
function Assert-NativeAutoTime($Value) {
 if($Value -isnot [string] -and $Value -isnot [datetime] -and $Value -isnot [datetimeoffset]){throw 'auto_timestamp_type_invalid'}
 $v=[datetimeoffset]$Value;if($v.Year -lt 2026 -or $v.Year -gt 2100){throw 'auto_timestamp_range_invalid'}
}
function Assert-NativeAutoAttempt($Attempt) {
 $fields=@('Id','AtUtc','Origin','EventKind','Stage','Target','SourceBinding','ConfigurationFingerprint','ReceiptDirectory','EvidenceSha256','ContinuationProcess','ParentAttemptId','MessagesSent','UnknownOperationsRetried')
 if($Attempt -isnot [pscustomobject] -or @($Attempt.PSObject.Properties).Count -ne $fields.Count -or @($Attempt.PSObject.Properties.Name|Where-Object {$_ -notin $fields}).Count -or
   $Attempt.Id -isnot [string] -or $Attempt.Id -notmatch '^[a-f0-9]{32}$' -or $Attempt.Origin -ne 'automatic' -or
   $Attempt.EventKind -notin @('known_bound_exit','known_relaunch','snow_reconnect') -or $Attempt.Stage -notin @('claimed_before_action','stopping_verified_relaunch','invoking_reviewed_daily','attention_or_pending_verification') -or
   $Attempt.ConfigurationFingerprint -ne $script:NativeAutoFingerprint -or $Attempt.ReceiptDirectory -ne (Join-Path $script:NativeAutoStage 'pilot-runs\20261026-122046-40bd601d\receipts') -or
   ($Attempt.MessagesSent -isnot [int] -and $Attempt.MessagesSent -isnot [long]) -or $Attempt.MessagesSent -ne 0 -or $Attempt.UnknownOperationsRetried -isnot [bool] -or $Attempt.UnknownOperationsRetried){throw 'auto_attempt_record_invalid'}
 Assert-NativeAutoTime $Attempt.AtUtc
 foreach($p in @($Attempt.Target,$Attempt.SourceBinding)){
   if($p -isnot [pscustomobject] -or !(Test-NativeAutoPid $p.Id) -or !(Test-NativeAutoPid $p.Parent) -or $p.Name -ne 'QQ.exe' -or $p.Exe -ne $script:NativeAutoExe){throw 'auto_attempt_process_invalid'}
   Assert-NativeAutoTime $p.CreatedUtc
 }
 if($null -ne $Attempt.EvidenceSha256 -and ($Attempt.EvidenceSha256 -isnot [string] -or $Attempt.EvidenceSha256 -notmatch '^[a-f0-9]{64}$')){throw 'auto_evidence_hash_invalid'}
 if($null -ne $Attempt.ParentAttemptId -and ($Attempt.ParentAttemptId -isnot [string] -or $Attempt.ParentAttemptId -notmatch '^[a-f0-9]{32}$')){throw 'auto_parent_attempt_invalid'}
 if($null -ne $Attempt.ContinuationProcess){
   $p=$Attempt.ContinuationProcess
   if($p -isnot [pscustomobject] -or !(Test-NativeAutoPid $p.Id) -or !(Test-NativeAutoPid $p.Parent) -or $p.Exe -ne $script:NativeAutoExe -or $p.Name -ne 'QQ.exe'){throw 'auto_continuation_process_invalid'}
   Assert-NativeAutoTime $p.CreatedUtc
 }
}
function Invoke-NativeAutoSnowReconnect($Context) {
 # This path cannot start or stop QQ even if its bridge vanishes mid-check.
 $mutex=$null
 try {
   $mutex=[IO.FileStream]::new((Join-Path $script:DailyRoot '.launcher-lock'),[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
   Assert-NativeAutoPersonal;Assert-DailyConfigurationUnchanged $Context.Configuration
   $null=Assert-PilotIdentityBinding $Context.NativeProcess $Context.Configuration.ReceiptDirectory
   $null=Connect-SnowPilotProcess $Context.NativeProcess
   $startId=$Context.Binding.StartId
   if($Context.Pending){$startId=$Context.Pending.StartId}
   $null=Save-DailyBinding $Context.NativeProcess $Context.Configuration $startId
   Write-DailyPending $Context.NativeProcess $Context.Configuration $startId $false 'native_and_snow_ready'
 } finally {if($mutex){$mutex.Dispose()}}
}
function Invoke-NativeAutoRecovery([string]$Root,[switch]$Execute) {
 # Helpers stay scoped to this invocation. No other transport is selected.
 . (Join-Path $script:NativeAutoStage 'daily-tools\NativeDaily.Common.ps1')
 . (Join-Path $Root 'scripts\NativeStartup.Common.ps1')
 $statePath=Join-Path $script:DailyRoot 'native-auto-state.json';$mutex=$null
 try {
   if($Execute){
     $lockPath=Join-Path $script:DailyRoot '.native-auto-recovery-lock';$null=Assert-DailyStagePath $lockPath
     $mutex=[IO.FileStream]::new($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None);Protect-PilotPath $lockPath
   }
   $state=New-NativeAutoState
   if(Test-Path -LiteralPath $statePath){Assert-PilotPrivateAcl $statePath;$state=Read-PilotJson $statePath;Assert-NativeAutoState $state}
   $now=[datetimeoffset]::UtcNow;$context=Get-NativeAutoContext $Root;$decision=Get-NativeAutoDecision $context $state $now
   if($decision.Action -eq 'Healthy'){
     if($Execute -and $context.Allowed){
       $state.ActiveAttempt=$null;$state.Failures=0;$state.LastResult=[pscustomobject]@{AtUtc=$now.ToString('o');Origin='automatic';EventKind='verified_health';Stage='verified_native_and_snow';Process=$context.NativeProcess;SelfId='0';UnknownOperationsRetried=$false}
       Write-DailyJson $statePath $state
       # A separate heartbeat may rebind its pinned monitor only from this result.
       Write-DailyJson (Join-Path $script:DailyRoot 'native-auto-verified-generation.json') $state.LastResult
     }
     return $decision
   }
   if($decision.Action -notin @('Start','Restart','Reconnect')){return $decision}
   $continuing=($decision.Action -eq 'Reconnect') -and (Test-NativeAutoContinuation $context $state)
   $budgetState=$state
   if($continuing){$budgetState=$state|ConvertTo-Json -Depth 16|ConvertFrom-Json;$budgetState.ActiveAttempt=$null}
   $budget=Get-NativeAutoBudget $budgetState $now
   if(!$budget.Allowed){return New-NativeAutoDecision 'Wait' $budget.Reason}
   $key=Get-NativeAutoGenerationKey $decision.Target
   if($decision.Action -in @('Start','Restart') -and $key -in @($state.RestartedGenerations)){return New-NativeAutoDecision 'Attention' 'generation_restart_already_claimed'}
   if(!$Execute){return [pscustomobject]@{Mode='PLAN';Decision=$decision;Budget=$budget;Origin='automatic';MessagesSent=0;UnknownOperationsRetried=$false;ProductionModified=$false}}
   Assert-NativeAutoPersonal;Assert-DailyConfigurationUnchanged $context.Configuration
   $operationId=[guid]::NewGuid().ToString('N');$audit=Join-Path $script:DailyRoot ('automatic-recovery-'+$operationId)
   $null=New-Item -ItemType Directory -Path $audit;Protect-PilotPath $audit
   $script:PilotEventPath=Join-Path $audit 'process-events.jsonl'
   $attempt=[pscustomobject]@{Id=$operationId;AtUtc=$now.ToString('o');Origin='automatic';EventKind=$decision.EventKind;Stage='claimed_before_action';Target=$decision.Target;SourceBinding=$context.BindingProcess;ConfigurationFingerprint=$context.Configuration.Fingerprint;ReceiptDirectory=$context.Configuration.ReceiptDirectory;EvidenceSha256=$(if($context.Evidence){$context.Evidence.Sha256}else{$null});ContinuationProcess=$(if($continuing){$context.NativeProcess}else{$null});ParentAttemptId=$(if($continuing){$state.ActiveAttempt.Id}else{$null});MessagesSent=0;UnknownOperationsRetried=$false}
   $state.Attempts=@($state.Attempts|Where-Object {([datetimeoffset]$_.AtUtc) -gt $now.AddHours(-1)})+@($attempt)
   $state.ActiveAttempt=$attempt;$state.NextEligibleUtc=$now.AddMinutes(2).ToString('o')
   if($decision.Action -in @('Start','Restart')){$state.RestartedGenerations=@($state.RestartedGenerations|Select-Object -Last 127)+@($key)}
   Write-DailyJson $statePath $state;Write-DailyJson (Join-Path $audit 'attempt.json') $attempt
   if($context.Evidence){[IO.File]::WriteAllBytes((Join-Path $audit 'lineage.original.jsonl'),$context.Evidence.Bytes);Protect-PilotPath (Join-Path $audit 'lineage.original.jsonl')}
   try {
     # Re-sample immediately before a stop; any changed PID or namespace aborts.
     $fresh=Get-NativeAutoContext $Root;$freshDecision=Get-NativeAutoDecision $fresh $state ([datetimeoffset]::UtcNow)
     if($freshDecision.Action -ne $decision.Action -or !(Test-NativeAutoIdentity $freshDecision.Target $decision.Target)){throw 'auto_snapshot_changed_before_action'}
     if($decision.Action -eq 'Restart'){
       $state.ActiveAttempt.Stage='stopping_verified_relaunch';Write-DailyJson $statePath $state
       Stop-PilotTree $decision.Target;Assert-NativeAutoPersonal;Assert-DailyConfigurationUnchanged $context.Configuration
     }
     $state.ActiveAttempt.Stage='invoking_reviewed_daily';Write-DailyJson $statePath $state
     if($decision.Action -eq 'Reconnect') {Invoke-NativeAutoSnowReconnect $fresh}
     else {& (Join-Path $script:NativeAutoStage 'daily-tools\Start-NativeDaily.ps1') -Execute -RecoverKnownExitedLock -NoOrdinaryRecovery -LoginWaitSeconds 45}
     $after=Get-NativeAutoContext $Root;$afterDecision=Get-NativeAutoDecision $after $state ([datetimeoffset]::UtcNow)
     if($afterDecision.Action -ne 'Healthy'){throw 'auto_native_generation_not_yet_verified'}
     $state.ActiveAttempt=$null;$state.Failures=0
     $state.LastResult=[pscustomobject]@{AtUtc=[datetimeoffset]::UtcNow.ToString('o');Origin='automatic';EventKind=$decision.EventKind;Stage='verified_native_and_snow';OperationId=$operationId;Process=$after.NativeProcess;SelfId='0';UnknownOperationsRetried=$false}
     Write-DailyJson $statePath $state;Write-DailyJson (Join-Path $script:DailyRoot 'native-auto-verified-generation.json') $state.LastResult
     Write-DailyJson (Join-Path $audit 'result.json') $state.LastResult
     return New-NativeAutoDecision 'Healthy' 'automatic_recovery_verified' $after.NativeProcess $decision.EventKind
   } catch {
     # The claimed operation remains unresolved. Polling may observe late login,
     # but no next invocation repeats this stop or starts another generation.
     $state.Failures=[Math]::Min(20,$state.Failures+1);$delay=[Math]::Min(30,2*[Math]::Pow(2,[Math]::Min(4,$state.Failures-1)))
     $state.NextEligibleUtc=[datetimeoffset]::UtcNow.AddMinutes($delay).ToString('o')
     $state.ActiveAttempt.Stage='attention_or_pending_verification'
     if(!$state.ActiveAttempt.ContinuationProcess){try{$state.ActiveAttempt.ContinuationProcess=Get-NativeAutoPendingContinuation $attempt}catch{}}
     $state.LastResult=[pscustomobject]@{AtUtc=[datetimeoffset]::UtcNow.ToString('o');Origin='automatic';EventKind=$decision.EventKind;Stage='attention_or_pending_verification';OperationId=$operationId;ErrorType=$_.Exception.GetType().Name;MessagesSent=0;UnknownOperationsRetried=$false}
     Write-DailyJson $statePath $state;Write-DailyJson (Join-Path $audit 'result.json') $state.LastResult
     return New-NativeAutoDecision 'Wait' 'claimed_attempt_waiting_for_verification'
   }
 } finally {if($mutex){$mutex.Dispose()}}
}
