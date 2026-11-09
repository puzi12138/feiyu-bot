$ErrorActionPreference = 'Stop'
$botRoot = Split-Path $PSScriptRoot -Parent
$hostLog = Join-Path $botRoot 'logs\service-host.log'
$pausePath = Join-Path $botRoot 'run\services-paused'
$hostMutex = New-Object System.Threading.Mutex($false, 'Local\FeiyuQQBotServiceHost')
if (-not $hostMutex.WaitOne(0)) { exit 0 }
$recentAttempts = @()
$musicRetryAfter = Get-Date
$imageRetryAfter = Get-Date
$briefRetryAfter = Get-Date
$billingRetryAfter = Get-Date
$nativeRetryAfter = Get-Date
try {
    "[$(Get-Date -Format o)] persistent service host started" | Add-Content -LiteralPath $hostLog -Encoding UTF8
    while ($true) {
        if (-not (Test-Path -LiteralPath $pausePath)) {
            # Task-completion briefs have their own worker. Recovery must not restart QQ.
            if ((Get-Date) -ge $briefRetryAfter) {
                try {
                    & "$PSScriptRoot\StartCodexBrief.ps1"
                } catch {
                    "[$(Get-Date -Format o)] completion brief recovery failed: $($_.Exception.GetType().Name)" | Add-Content -LiteralPath $hostLog -Encoding UTF8
                }
                $briefRetryAfter = (Get-Date).AddMinutes(1)
            }
            # Recover only the billing companion. Its browser and login must not
            # restart QQ/AstrBot or consume the core services' attempt budget.
            if ((Get-Date) -ge $billingRetryAfter) {
                try {
                    if ((Test-Path -LiteralPath "$PSScriptRoot\StartDeepSeekBilling.ps1") -and -not (Get-NetTCPConnection -State Listen -LocalPort 16302 -ErrorAction SilentlyContinue)) {
                        & "$PSScriptRoot\StartDeepSeekBilling.ps1" | Out-Null
                    }
                } catch {
                    "[$(Get-Date -Format o)] optional billing service recovery failed: $($_.Exception.GetType().Name)" | Add-Content -LiteralPath $hostLog -Encoding UTF8
                }
                $billingRetryAfter = (Get-Date).AddMinutes(2)
            }
            $missingPorts = @(6185,5099 | Where-Object {
                -not (Get-NetTCPConnection -State Listen -LocalPort $_ -ErrorAction SilentlyContinue)
            })
            if ($missingPorts.Count -gt 0) {
                $cutoff = (Get-Date).AddHours(-1)
                $recentAttempts = @($recentAttempts | Where-Object { $_ -gt $cutoff })
                if ($recentAttempts.Count -lt 6) {
                    $recentAttempts += Get-Date
                    "[$(Get-Date -Format o)] starting services; missing ports: $($missingPorts -join ',')" | Add-Content -LiteralPath $hostLog -Encoding UTF8
                    try {
                        & "$PSScriptRoot\Start.ps1" -SkipQQConnection -AutomaticServiceRecovery *>&1 | Out-File -LiteralPath $hostLog -Encoding UTF8 -Append
                    } catch {
                        "[$(Get-Date -Format o)] recovery failed: $($_.Exception.GetType().Name)" | Add-Content -LiteralPath $hostLog -Encoding UTF8
                    }
                    # Leave enough time for plugin loading before another check.
                    Start-Sleep -Seconds 45
                }
            }
            # QQ can exit while AstrBot and Snow's control server stay alive.
            # The supervisor owns native health checks and its durable budget.
            # Healthy or pending checks do not consume a second host budget.
            if ((Get-Date) -ge $nativeRetryAfter) {
                $nativeRecoveryStep = 'observer_ensure'
                try {
                    if (Test-Path -LiteralPath "$PSScriptRoot\Start-NativeObserver.ps1") {
                        & "$PSScriptRoot\Start-NativeObserver.ps1" -Execute *>&1 | Out-File -LiteralPath $hostLog -Encoding UTF8 -Append
                    }
                    $nativeRecoveryStep = 'native_connect'
                    & "$PSScriptRoot\ConnectQQNative.ps1" -AutomaticRecovery *>&1 | Out-File -LiteralPath $hostLog -Encoding UTF8 -Append
                } catch {
                    $nativeErrorFile = Split-Path -Leaf $_.InvocationInfo.ScriptName
                    "[$(Get-Date -Format o)] native QQ recovery needs attention: $($_.Exception.GetType().Name); step=$nativeRecoveryStep; source=$nativeErrorFile; line=$($_.InvocationInfo.ScriptLineNumber); command=$($_.InvocationInfo.MyCommand.Name); error_id=$($_.FullyQualifiedErrorId)" | Add-Content -LiteralPath $hostLog -Encoding UTF8
                }
                $nativeRetryAfter = (Get-Date).AddMinutes(2)
            }
            # This optional companion has its own retry path. Its login or
            # availability must never consume the core QQ recovery attempts.
            if ((Get-Date) -ge $musicRetryAfter) {
                try {
                    $musicConfigPath = Join-Path $botRoot 'config\netease-listen\settings.json'
                    if (Test-Path -LiteralPath $musicConfigPath) {
                        $musicSettings = Get-Content -Raw -LiteralPath $musicConfigPath | ConvertFrom-Json
                        if ($musicSettings.auto_start -and -not (Get-NetTCPConnection -State Listen -LocalPort 16300 -ErrorAction SilentlyContinue)) {
                            & "$botRoot\apps\netease-listen\Start-NeteaseListen.ps1" *>&1 | Out-File -LiteralPath $hostLog -Encoding UTF8 -Append
                        }
                    }
                } catch {
                    "[$(Get-Date -Format o)] optional music companion recovery failed: $($_.Exception.GetType().Name)" | Add-Content -LiteralPath $hostLog -Encoding UTF8
                }
                $musicRetryAfter = (Get-Date).AddMinutes(2)
            }
            # Image generation has an independent recovery budget. Its outage
            # must not restart QQ/AstrBot or consume their recovery attempts.
            if ((Get-Date) -ge $imageRetryAfter) {
                try {
                    $imageConfigPath = Join-Path $botRoot 'config\local-image.json'
                    if (Test-Path -LiteralPath $imageConfigPath) {
                        $imageSettings = Get-Content -Raw -LiteralPath $imageConfigPath | ConvertFrom-Json
                        if ($imageSettings.auto_start -and -not (Get-NetTCPConnection -State Listen -LocalPort 18889 -ErrorAction SilentlyContinue)) {
                            & "$PSScriptRoot\StartLocalImage.ps1" *>&1 | Out-File -LiteralPath $hostLog -Encoding UTF8 -Append
                        }
                    }
                } catch {
                    "[$(Get-Date -Format o)] optional image broker recovery failed: $($_.Exception.GetType().Name)" | Add-Content -LiteralPath $hostLog -Encoding UTF8
                }
                $imageRetryAfter = (Get-Date).AddMinutes(2)
            }
        }
        Start-Sleep -Seconds 30
    }
} finally {
    $hostMutex.ReleaseMutex()
    $hostMutex.Dispose()
}
