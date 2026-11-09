param([switch]$ConfirmBotAccount)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
# A present native startup configuration owns the whole connection path.
# Failure or disabled/invalid configuration must not fall through to ordinary QQ.
if (Test-Path -LiteralPath "$Root\config\qq-native-startup.json") {
    & "$PSScriptRoot\ConnectQQNative.ps1" -ConfirmBotAccount:$ConfirmBotAccount
    return
}
$accountGuard = "$Root\config\qq-account-mismatch.json"
if ((Test-Path -LiteralPath $accountGuard) -and !$ConfirmBotAccount) {
    Write-Warning 'QQ restored another account. Log into 0, then use menu option 7 to connect it.'
    return
}
if ($ConfirmBotAccount -and (Test-Path -LiteralPath $accountGuard)) { Remove-Item -LiteralPath $accountGuard }
$client = Get-Content -LiteralPath "$Root\config\qq-client.json" -Raw | ConvertFrom-Json
$qqExe = $client.executable
if (!(Test-Path -LiteralPath $qqExe)) { throw 'The configured QQ client is missing.' }
$qqProcesses = @(Get-CimInstance Win32_Process -Filter "Name='QQ.exe'" | Where-Object ExecutablePath -eq $qqExe)
if (!$qqProcesses.Count) {
    if ($client.arguments) {
        Start-Process -FilePath $qqExe -ArgumentList $client.arguments -WorkingDirectory (Split-Path $qqExe -Parent) -WindowStyle Normal
    } else {
        Start-Process -FilePath $qqExe -WorkingDirectory (Split-Path $qqExe -Parent) -WindowStyle Normal
    }
    Write-Host 'Opened the configured QQ client. Scan its QR code with the bot account.'
}
$credential = Get-Content -LiteralPath "$Root\config\bootstrap.json" -Raw | ConvertFrom-Json
$deadline = (Get-Date).AddSeconds(30)
$lastIssue = ''
do {
    try {
        $login = Invoke-RestMethod 'http://127.0.0.1:5099/api/login' -Method Post -ContentType 'application/json' -Body (@{password=$credential.snowluma_password} | ConvertTo-Json) -TimeoutSec 3
        if (!$login.success) { throw 'SnowLuma login failed. If its password was changed, update config/bootstrap.json.' }
        $headers = @{Authorization="Bearer $($login.token)"}
        $processes = (Invoke-RestMethod 'http://127.0.0.1:5099/api/processes' -Headers $headers -TimeoutSec 3).list
        $clientProcesses = @(Get-CimInstance Win32_Process -Filter "Name='QQ.exe'" | Where-Object ExecutablePath -eq $qqExe)
        $target = @($processes | Where-Object { $_.pid -in $clientProcesses.ProcessId })
        if ($target.Count -gt 1) {
            # Prefer a verified existing bot session. Otherwise identify the
            # account database holder without injecting into other accounts.
            $knownBot = @($target | Where-Object { $_.uin -eq $client.expectedUin -or $_.account.uin -eq $client.expectedUin })
            if ($knownBot.Count -eq 1) {
                $target = $knownBot
            } else {
                $holderJson = & "$Root\apps\SnowLuma\node.exe" "$PSScriptRoot\FindBotQQ.cjs"
                if ($LASTEXITCODE -ne 0) { throw 'Cannot safely identify the bot QQ process.' }
                $holderPids = @($holderJson | ConvertFrom-Json)
                $target = @($target | Where-Object { $_.pid -in $holderPids })
                if ($target.Count -ne 1) { throw 'Cannot uniquely identify the bot QQ process; no other account was connected.' }
            }
        }
        if ($target.Count -eq 1) {
            $targetPid = $target[0].pid
            if (!$target[0].injected) {
                $result = Invoke-RestMethod "http://127.0.0.1:5099/api/processes/$targetPid/load" -Method Post -ContentType 'application/json' -Body '{}' -Headers $headers -TimeoutSec 10
                if (!$result.success) { throw "QQ component loading failed: $($result.process.error)" }
            }
            for ($attempt = 0; $attempt -lt 8; $attempt++) {
                $current = (Invoke-RestMethod 'http://127.0.0.1:5099/api/processes' -Headers $headers -TimeoutSec 3).list | Where-Object pid -eq $targetPid
                if ($current.uin -and $current.uin -ne '0') {
                    if ($current.uin -ne '0') {
                        Invoke-RestMethod "http://127.0.0.1:5099/api/processes/$targetPid/unload" -Method Post -ContentType 'application/json' -Body '{}' -Headers $headers -TimeoutSec 10 | Out-Null
                        @{pid=$targetPid; reason='QQ restored another account'; expected='0'} | ConvertTo-Json | Set-Content -LiteralPath $accountGuard -Encoding UTF8
                        Write-Warning 'Disconnected another QQ account. Switch to 0 and use menu option 7.'
                        return
                    }
                    Write-Host 'Verified bot account 0. Check AstrBot logs for the OneBot connection.'
                    return
                }
                Start-Sleep -Seconds 1
            }
            Write-Host 'QQ hook loaded. Waiting for QR login into 0.'
            return
        }
        if ($target.Count -gt 1) { throw 'Multiple dedicated QQ main processes found. Select the bot process in SnowLuma.' }
        $lastIssue = 'Waiting for the dedicated QQ main process.'
    } catch { $lastIssue = $_.Exception.Message }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
throw $lastIssue

