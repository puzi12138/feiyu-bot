param([switch]$PreviewOnly)
$ErrorActionPreference = 'Stop'
$botRoot = Split-Path $PSScriptRoot -Parent
$powershellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$notepadExe = Join-Path $env:SystemRoot 'System32\notepad.exe'

function Invoke-MenuAction([string]$Selection) {
    $scriptName = switch ($Selection) {
        '1' { 'Start.ps1' }
        '2' { 'Stop.ps1' }
        '3' { 'Status.ps1' }
        '4' { 'Backup.ps1' }
        '7' { 'ConnectQQ.ps1' }
    }
    if ($scriptName) {
        $scriptPath = Join-Path $PSScriptRoot $scriptName
        if ($PreviewOnly) {
            Write-Output ('PREVIEW_ONLY action={0} script={1} confirm_bot={2}' -f $Selection, $scriptName, ($Selection -eq '7'))
            return
        }
        if (!(Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw 'Required management script is missing.' }
        if ($Selection -eq '7') {
            Write-Host '正在调用肥鱼专用 QQ 连接入口；如出现登录窗口，请手动登录 0。'
            & $powershellExe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $scriptPath -ConfirmBotAccount
        } else {
            & $powershellExe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $scriptPath
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Host ('操作未完成（退出码 {0}）。请保留上方提示，不要反复启动 QQ。' -f $LASTEXITCODE) -ForegroundColor Yellow
        }
        return
    }
    if ($PreviewOnly) {
        Write-Output ('PREVIEW_ONLY action={0} external_action_not_run=True' -f $Selection)
        return
    }
    switch ($Selection) {
        '5' {
            Start-Process 'http://127.0.0.1:6185'
            Start-Process 'http://127.0.0.1:5099'
        }
        '6' { & $notepadExe (Join-Path $botRoot 'config\面板登录信息.txt') }
        '8' { & $notepadExe (Join-Path $botRoot '使用说明.txt') }
    }
}

while ($true) {
    Write-Host ''
    Write-Host ('肥鱼机器人管理 — {0}' -f $botRoot)
    Write-Host '1. 启动 AstrBot 和 SnowLuma'
    Write-Host '2. 停止机器人服务'
    Write-Host '3. 查看运行状态'
    Write-Host '4. 备份数据和配置（会短暂停机）'
    Write-Host '5. 打开管理面板'
    Write-Host '6. 查看面板登录信息'
    Write-Host '7. 打开或连接肥鱼专用 QQ（0）'
    Write-Host '8. 打开使用说明'
    Write-Host '0. 退出菜单'
    try { $selection = Read-Host '请选择 1–8 或 0' }
    catch { Write-Host '无法读取键盘输入，没有执行任何操作。'; exit 1 }
    if ([string]::IsNullOrWhiteSpace($selection)) {
        Write-Host '没有选择操作，已退出菜单。'
        exit 0
    }
    $selection = $selection.Trim()
    if ($selection -eq '0') { exit 0 }
    if ($selection -notmatch '^[1-8]$') {
        Write-Host '输入无效，请输入 1–8 或 0。' -ForegroundColor Yellow
        continue
    }
    try { Invoke-MenuAction $selection }
    catch { Write-Host ('操作失败：{0}' -f $_.Exception.Message) -ForegroundColor Red }
    if (!$PreviewOnly) {
        try { $null = Read-Host '按回车返回菜单（上方错误提示会保留）' }
        catch { exit 1 }
    }
}
