param([switch]$SkipQQConnection,[switch]$AutomaticServiceRecovery)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
if ($AutomaticServiceRecovery) {
    if (Test-Path -LiteralPath "$Root\run\services-paused") { return }
} else {
    Remove-Item -LiteralPath "$Root\run\services-paused" -ErrorAction SilentlyContinue
}
$env:ASTRBOT_ROOT = "$Root\apps\AstrBot"
$env:UV_CACHE_DIR = "$Root\cache\uv"
$env:UV_PYTHON_INSTALL_DIR = "$Root\runtime\python"
$env:UV_PROJECT_ENVIRONMENT = "$Root\runtime\astrbot-venv"
$env:TEMP = "$Root\temp"
$env:TMP = "$Root\temp"
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$existingNoProxy = @($env:NO_PROXY -split ',' | ForEach-Object {
    $entry = $_.Trim()
    # httpx treats this IPv6 CIDR as an invalid port; the plain loopback is equivalent.
    if ($entry -eq '::1/128') { '::1' } elseif ($entry) { $entry }
})
$env:NO_PROXY = ((@('localhost','127.0.0.1','::1','api.deepseek.com') + $existingNoProxy) | Select-Object -Unique) -join ','
$env:PATH = "$Root\runtime\astrbot-venv\Scripts;$Root\runtime\ffmpeg\bin;" + $env:PATH
$env:SNOWLUMA_TELEMETRY = '0'
$env:SNOWLUMA_LOG_DIR = "$Root\logs\snowluma"
$env:SNOWLUMA_LOG_MAX_TOTAL_MB = '256'
$env:SNOWLUMA_LOG_RETAIN_DAYS = '14'
$env:SNOWLUMA_WEBUI_HOST = '127.0.0.1'
$env:SNOWLUMA_ACCEPT_EULA = '1'
$env:SNOWLUMA_ACCEPT_PRIVACY = '1'
$credentials = Get-Content -LiteralPath "$Root\config\bootstrap.json" -Raw | ConvertFrom-Json
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$services = @(
    @{Name='astrbot'; Exe="$Root\runtime\astrbot-venv\Scripts\python.exe"; Args=@('main.py','--webui-dir',"$Root\apps\AstrBot\dashboard-release\dist"); Cwd="$Root\apps\AstrBot"; Port=6185},
    @{Name='snowluma'; Exe="$Root\apps\SnowLuma\node.exe"; Args=@("$Root\apps\SnowLuma\index.mjs"); Cwd="$Root\data\snowluma"; Port=5099}
)
foreach ($service in $services) {
    $pidFile = "$Root\run\$($service.Name).json"
    if (Test-Path -LiteralPath $pidFile) {
        $record = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        $running = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)" -ErrorAction SilentlyContinue
        if ($running -and $running.ExecutablePath -eq $service.Exe -and $running.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$record.Created).ToUniversalTime().Ticks) {
            Write-Host "$($service.Name) is already running (PID $($record.Id))."
            continue
        }
    }
    if (Get-NetTCPConnection -State Listen -LocalPort $service.Port -ErrorAction SilentlyContinue) { throw "Port $($service.Port) is already in use." }
    if ($service.Name -eq 'snowluma') { $env:SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD = $credentials.snowluma_password }
    $child = Start-Process -FilePath $service.Exe -ArgumentList $service.Args -WorkingDirectory $service.Cwd -WindowStyle Hidden -RedirectStandardOutput "$Root\logs\$($service.Name)-$stamp.out.log" -RedirectStandardError "$Root\logs\$($service.Name)-$stamp.err.log" -PassThru
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($child.Id)"
    @{Id=$child.Id; Exe=$service.Exe; Created=$processInfo.CreationDate.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8
    Write-Host "Started $($service.Name) (PID $($child.Id))."
}
Remove-Item Env:\SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD -ErrorAction SilentlyContinue
Write-Host 'AstrBot: http://127.0.0.1:6185   SnowLuma: http://127.0.0.1:5099'
if (Test-Path -LiteralPath "$PSScriptRoot\StartCodexBrief.ps1") {
    & "$PSScriptRoot\StartCodexBrief.ps1"
}
if (!$SkipQQConnection -and (Test-Path -LiteralPath "$Root\config\qq-client.json")) {
    try {
        & "$PSScriptRoot\ConnectQQ.ps1"
    } catch {
        Write-Warning 'QQ connection needs attention. Core services continue; the independent QQ recovery path will check again. See protected native daily-runtime records.'
    }
}


if (Test-Path -LiteralPath "$Root\config\gsv-runtime.json") {
    $voiceSettings = Get-Content -Raw -LiteralPath "$Root\config\gsv-runtime.json" | ConvertFrom-Json
    if ($voiceSettings.auto_start) { & "$PSScriptRoot\StartVoice.ps1" }
}

if (Test-Path -LiteralPath "$Root\config\cover-runtime.json") {
    $coverSettings = Get-Content -Raw -LiteralPath "$Root\config\cover-runtime.json" | ConvertFrom-Json
    if ($coverSettings.auto_start) { & "$PSScriptRoot\StartCover.ps1" }
}

if (Test-Path -LiteralPath "$Root\config\netease-listen\settings.json") {
    $musicSettings = Get-Content -Raw -LiteralPath "$Root\config\netease-listen\settings.json" | ConvertFrom-Json
    if ($musicSettings.auto_start) { & "$Root\apps\netease-listen\Start-NeteaseListen.ps1" }
}

if (Test-Path -LiteralPath "$Root\config\local-image.json") {
    $imageSettings = Get-Content -Raw -LiteralPath "$Root\config\local-image.json" | ConvertFrom-Json
    if ($imageSettings.auto_start) { & "$PSScriptRoot\StartLocalImage.ps1" }
}

# Billing owns its dedicated browser profile and must not block core startup.
if (Test-Path -LiteralPath "$PSScriptRoot\StartDeepSeekBilling.ps1") {
    try {
        & "$PSScriptRoot\StartDeepSeekBilling.ps1" | Out-Null
    } catch {
        Write-Warning "Optional billing service startup failed: $($_.Exception.GetType().Name)"
    }
}

# QQ online is not evidence that independent platform jobs are healthy.
if (Test-Path -LiteralPath "$PSScriptRoot\BusinessHealth.Common.ps1") {
    try {
        . "$PSScriptRoot\BusinessHealth.Common.ps1"
        Invoke-BusinessHealthCheck -Root $Root
    } catch { Write-Warning 'Business health check unavailable; original startup result is unchanged.' }
}
