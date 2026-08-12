# fix-opencode.ps1 — 修复 opencode 桌面版 bash 工具故障，并一键跑完 Options Radar 实测
#
# 用法：在普通 PowerShell 窗口里执行
#   powershell -ExecutionPolicy Bypass -File fix-opencode.ps1
# 或直接双击同目录的 修复opencode.cmd
#
# 注意：会结束 opencode 桌面进程并清除其状态库（本次对话历史会丢失），
# 项目文件与 git 仓库完全不受影响。

$ErrorActionPreference = 'Continue'
$OpencodeData = "$env:USERPROFILE\.local\share\opencode"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Project '.venv\Scripts\python.exe'

Write-Host "`n=== 1/3 结束 opencode 桌面进程 ===" -ForegroundColor Cyan
Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -like '*opencode*' -or $_.Path -like '*@opencode-aidesktop*' } |
    ForEach-Object {
        Write-Host "结束进程: $($_.ProcessName) ($($_.Id))" -ForegroundColor Yellow
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 2

Write-Host "`n=== 2/3 备份并清除 opencode 状态库 ===" -ForegroundColor Cyan
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
foreach ($name in @('opencode.db', 'opencode.db-wal', 'opencode.db-shm')) {
    $path = Join-Path $OpencodeData $name
    if (Test-Path -LiteralPath $path) {
        $backup = "$path.bak-$stamp"
        Copy-Item -LiteralPath $path -Destination $backup -Force
        Write-Host "已备份: $path -> $backup" -ForegroundColor Green
        Remove-Item -LiteralPath $path -Force
        Write-Host "已清除: $path" -ForegroundColor Green
    }
}

Write-Host "`n=== 3/3 运行 Options Radar 实测 ===" -ForegroundColor Cyan
if (Test-Path -LiteralPath $Python) {
    Set-Location $Project
    Write-Host "`n[1] 单元测试:" -ForegroundColor Magenta
    & $Python -m pytest tests -q
    Write-Host "`n[2] 一键实测（探测 API -> 昨日分析 -> 一月回测 -> 飞书发送）:" -ForegroundColor Magenta
    & $Python live_test.py
} else {
    Write-Host "未找到 $Python ，跳过实测（可稍后手动运行 live_test.py）" -ForegroundColor Red
}

Write-Host "`n=== 修复完成 ===" -ForegroundColor Green
Write-Host "接下来：重新打开 opencode 桌面版，新建一个会话（不要恢复旧会话），"
Write-Host "进入目录 $Project ，对助手说「继续」即可。"
Write-Host "如果 bash 仍然报错，说明状态存在应用缓存里，请更新或重装 opencode 桌面版。"
Read-Host "按回车退出"
