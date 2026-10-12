# Start Video2Slides locally (called by start.bat). Checks the environment, then runs the server
# in this window and opens the browser when it is ready. Nothing is reinstalled or redownloaded.
$ErrorActionPreference = 'Stop'
$Root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $Root
. (Join-Path $PSScriptRoot 'common.ps1')

Write-Title 'Video2Slides - khởi động'

$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Fail 'Chưa cài đặt. Hãy chạy install.bat trước (chỉ cần làm một lần).'
}

if (-not (Find-FFmpeg)) {
    Fail 'Không tìm thấy FFmpeg. Chạy lại install.bat, hoặc cài bằng lệnh: winget install Gyan.FFmpeg'
}
Write-Ok 'FFmpeg'

if (-not (Test-Path (Join-Path $Root '.env'))) {
    Copy-Item (Join-Path $Root '.env.example') (Join-Path $Root '.env')
    Write-Warn 'Đã tạo tệp .env mặc định. Mở .env để thêm GEMINI_API_KEY (xem docs\HUONG_DAN_SU_DUNG_VI.md).'
}
$envText = Get-Content (Join-Path $Root '.env') -Raw -Encoding UTF8
if ($envText -notmatch '(?m)^\s*GEMINI_API_KEY\s*=\s*\S+') {
    Write-Warn 'Chưa có GEMINI_API_KEY trong .env: dàn ý sẽ được tạo bằng chế độ offline đơn giản.'
} else {
    Write-Ok 'Gemini API key'
}
if ($envText -match '(?m)^\s*AI_IMAGES_ENABLED\s*=\s*true') { Write-Ok 'Ảnh minh họa AI (Modal GPU) đang bật' }

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
& $py (Join-Path $Root 'scripts\run_local.py') @args
exit $LASTEXITCODE
