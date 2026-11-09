$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
Set-Content -LiteralPath "$Root\run\services-paused" -Value (Get-Date -Format o) -Encoding UTF8
# Let the billing worker close its own browser before stopping the core services.
# If it cannot confirm a clean close, its stop helper deliberately leaves it alone.
if (Test-Path -LiteralPath "$PSScriptRoot\StopDeepSeekBilling.ps1") {
    try {
        & "$PSScriptRoot\StopDeepSeekBilling.ps1" | Out-Null
    } catch {
        Write-Warning "Optional billing service shutdown failed: $($_.Exception.GetType().Name)"
    }
}
foreach ($name in @('local-image','netease-listen','cover','gsv','snowluma','astrbot')) {
    $pidFile = "$Root\run\$name.json"
    if (!(Test-Path -LiteralPath $pidFile)) { continue }
    $record = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $running = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)" -ErrorAction SilentlyContinue
    if ($running -and $running.ExecutablePath -eq $record.Exe -and $running.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$record.Created).ToUniversalTime().Ticks -and $record.Exe.StartsWith("$Root\",[StringComparison]::OrdinalIgnoreCase)) {
        $tree = @($running)
        for ($index = 0; $index -lt $tree.Count; $index++) {
            $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($tree[$index].ProcessId)" -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith("$Root\",[StringComparison]::OrdinalIgnoreCase) })
            $tree += $children
        }
        [array]::Reverse($tree)
        foreach ($entry in $tree) { Stop-Process -Id $entry.ProcessId -ErrorAction SilentlyContinue }
        Wait-Process -Id $record.Id -Timeout 15 -ErrorAction SilentlyContinue
        Write-Host "Stopped $name."
    }
    Remove-Item -LiteralPath $pidFile
}

