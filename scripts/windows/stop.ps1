# Stop a Video2Slides server started by start.bat (uses data\server.pid written by run_local.py).
$ErrorActionPreference = 'Stop'
$Root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
. (Join-Path $PSScriptRoot 'common.ps1')

$pidFile = Join-Path $Root 'data\server.pid'
if (-not (Test-Path $pidFile)) {
    Write-Ok 'Video2Slides không chạy.'
    exit 0
}
$info = Get-Content $pidFile -Raw | ConvertFrom-Json
$proc = Get-Process -Id $info.pid -ErrorAction SilentlyContinue
if ($proc -and $proc.ProcessName -like 'python*') {
    Stop-Process -Id $info.pid -Force
    Write-Ok "Đã dừng Video2Slides (cổng $($info.port))."
} else {
    Write-Ok 'Video2Slides không chạy (đã dọn tệp PID cũ).'
}
Remove-Item $pidFile -ErrorAction SilentlyContinue
