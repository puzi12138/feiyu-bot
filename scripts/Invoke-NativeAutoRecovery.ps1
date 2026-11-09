param([switch]$Execute)
$ErrorActionPreference='Stop'
. "$PSScriptRoot\NativeAutoRecovery.Common.ps1"
Invoke-NativeAutoRecovery -Root 'D:\QQBot' -Execute:$Execute
