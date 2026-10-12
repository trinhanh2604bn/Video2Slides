# One-time setup of Video2Slides on Windows (called by install.bat). Safe to run again:
# existing .venv, .env and downloaded models are reused, never deleted.
$ErrorActionPreference = 'Stop'
$Root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $Root
. (Join-Path $PSScriptRoot 'common.ps1')

Write-Title 'Video2Slides - cài đặt'

# 1. Python 3.11/3.12 virtual environment
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    Write-Ok "Môi trường Python đã có: $(& $venvPy --version)"
} else {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        & uv venv --python 3.11 .venv
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $ver = (& py -3.11 --version 2>$null)
        if (-not $ver) { $ver = (& py -3.12 --version 2>$null); $sel = '-3.12' } else { $sel = '-3.11' }
        if (-not $ver) { Fail 'Cần Python 3.11 hoặc 3.12. Cài tại https://www.python.org/downloads/ rồi chạy lại.' }
        & py $sel -m venv .venv
    } else {
        Fail 'Không tìm thấy Python. Cài Python 3.11 (https://www.python.org/downloads/) hoặc uv rồi chạy lại.'
    }
    if (-not (Test-Path $venvPy)) { Fail 'Không tạo được môi trường .venv.' }
    Write-Ok 'Đã tạo môi trường .venv'
}

# 2. Python packages (only installs what is missing)
Write-Host '  Đang cài thư viện Python (lần đầu có thể mất vài phút)...'
if (Get-Command uv -ErrorAction SilentlyContinue) {
    & uv pip install --python $venvPy -e '.[dev]'
} else {
    & $venvPy -m pip install --disable-pip-version-check -q -e '.[dev]'
}
if ($LASTEXITCODE -ne 0) { Fail 'Cài thư viện thất bại. Kiểm tra kết nối mạng rồi chạy lại.' }
Write-Ok 'Thư viện Python'

# 3. FFmpeg
if (Find-FFmpeg) {
    Write-Ok 'FFmpeg'
} else {
    Write-Warn 'Chưa có FFmpeg (cần để đọc video).'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $answer = Read-Host '  Cài FFmpeg bằng winget (Gyan.FFmpeg) ngay bây giờ? [Y/n]'
        if ($answer -eq '' -or $answer -match '^[YyCc]') {
            & winget install --id Gyan.FFmpeg -e --accept-package-agreements
            if (-not (Find-FFmpeg)) { Write-Warn 'Đã cài nhưng cần mở lại cửa sổ để nhận PATH mới.' }
        }
    } else {
        Write-Warn 'Tải FFmpeg tại https://www.gyan.dev/ffmpeg/builds/ và thêm thư mục bin vào PATH.'
    }
}

# 4. Configuration
if (-not (Test-Path (Join-Path $Root '.env'))) {
    Copy-Item (Join-Path $Root '.env.example') (Join-Path $Root '.env')
    Write-Ok 'Đã tạo .env từ .env.example (hãy thêm GEMINI_API_KEY)'
} else {
    Write-Ok '.env đã có (giữ nguyên)'
}

# 5. Whisper model (downloaded once into .cache\models)
$env:PYTHONIOENCODING = 'utf-8'
& $venvPy (Join-Path $Root 'scripts\download_model.py')
if ($LASTEXITCODE -ne 0) { Write-Warn 'Chưa tải được model Whisper; ứng dụng sẽ thử tải lại ở lần chạy đầu.' }
else { Write-Ok 'Model nhận dạng giọng nói' }

Write-Title 'Hoàn tất. Chạy start.bat để mở ứng dụng.'
