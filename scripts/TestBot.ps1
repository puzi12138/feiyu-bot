$ErrorActionPreference = 'Stop'
$botRoot = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = "$botRoot\data\astrbot\plugins;$botRoot\apps\AstrBot"
$env:TEMP = "$botRoot\temp"
$env:TMP = "$botRoot\temp"
$env:PYTHONUTF8 = '1'
& "$botRoot\runtime\astrbot-venv\Scripts\python.exe" -m unittest discover -s "$PSScriptRoot\tests" -p 'test_steward_*.py'
exit $LASTEXITCODE
