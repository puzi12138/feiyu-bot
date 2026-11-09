$ErrorActionPreference = 'Stop'
$botRoot = Split-Path $PSScriptRoot -Parent
$pidPath = "$botRoot\run\cover.json"
if (!(Test-Path -LiteralPath $pidPath)) { return }
$record = Get-Content -Raw -LiteralPath $pidPath | ConvertFrom-Json
$running = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)" -ErrorAction SilentlyContinue
if ($running -and $running.ExecutablePath -eq "$botRoot\runtime\cover-venv\Scripts\python.exe" -and $running.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$record.Created).ToUniversalTime().Ticks) {
    $voiceTree = @($running)
    for ($index = 0; $index -lt $voiceTree.Count; $index++) {
        $voiceTree += @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($voiceTree[$index].ProcessId)" -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith("$botRoot\", [StringComparison]::OrdinalIgnoreCase) })
    }
    [array]::Reverse($voiceTree)
    foreach ($entry in $voiceTree) { Stop-Process -Id $entry.ProcessId -ErrorAction SilentlyContinue }
    Write-Host 'Stopped local cover service.'
}
Remove-Item -LiteralPath $pidPath
