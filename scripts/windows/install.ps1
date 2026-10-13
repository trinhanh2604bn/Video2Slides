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

# 3. FFmpeg (not needed when video processing runs on Modal)
$cfgFile = if (Test-Path (Join-Path $Root '.env')) { Join-Path $Root '.env' } else { Join-Path $Root '.env.example' }
$cloudMedia = Test-CloudMedia (Get-Content $cfgFile -Raw -Encoding UTF8)
if (Find-FFmpeg) {
    Write-Ok 'FFmpeg'
} elseif ($cloudMedia) {
    Write-Ok 'FFmpeg không bắt buộc: video được xử lý trên Modal (MEDIA_PROCESSING)'
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
    Write-Ok 'Đã tạo .env từ .env.example (mặc định: Gemini nếu có khóa, nếu không thì Ollama trên Modal)'
} else {
    Write-Ok '.env đã có (giữ nguyên)'
}

# 4b. Ollama trên máy: CHỈ khi chọn rõ LLM_PROVIDER=ollama. Mặc định (modal_ollama) dùng Ollama trên Modal,
#     không cần cài Ollama hay tải model về máy Windows.
$envText = Get-Content (Join-Path $Root '.env') -Raw -Encoding UTF8
$cloudMedia = Test-CloudMedia $envText
$provider = (Get-EnvSetting $envText 'LLM_PROVIDER' (Get-EnvSetting $envText 'AI_PROVIDER' 'modal_ollama')).ToLower()
if ($provider -eq 'ollama') {
    $base = (Get-EnvSetting $envText 'OLLAMA_BASE_URL' 'http://127.0.0.1:11434').TrimEnd('/')
    $model = Get-EnvSetting $envText 'OLLAMA_MODEL' 'qwen3:4b'
    $exe = Find-Ollama
    if (-not $exe -and -not (Get-OllamaVersion $base)) {
        Write-Warn 'Chưa có Ollama (chạy model AI viết slide ngay trên máy).'
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $answer = Read-Host '  Cài Ollama bằng winget (Ollama.Ollama, khoảng 1 GB) ngay bây giờ? [Y/n]'
            if ($answer -eq '' -or $answer -match '^[YyCc]') {
                & winget install --id Ollama.Ollama -e --accept-package-agreements
                $exe = Find-Ollama
            }
        } else {
            Write-Warn 'Tải Ollama tại https://ollama.com/download rồi chạy lại install.bat.'
        }
    }
    $ver = Start-OllamaIfNeeded $exe $base
    if (-not $ver) {
        Write-Warn "Ollama chưa chạy tại $base. Mở ứng dụng Ollama rồi chạy lại install.bat để tải model."
    } else {
        Write-Ok "Ollama $ver"
        if (Test-OllamaModel $base $model) {
            Write-Ok "Model $model đã có"
        } else {
            $size = if ($model -eq 'qwen3:4b') { 'khoảng 2,5 GB' } else { 'dung lượng tùy model' }
            Write-Host "  Sẽ tải model $model ($size) vào thư mục của Ollama. Chỉ cần tải một lần."
            $answer = Read-Host '  Tải ngay bây giờ? [Y/n]'
            if ($answer -eq '' -or $answer -match '^[YyCc]') {
                if ($exe) { & $exe pull $model } else { & ollama pull $model }
                if ($LASTEXITCODE -ne 0) { Write-Warn "Tải model thất bại. Thử lại sau bằng lệnh: ollama pull $model" }
                else { Write-Ok "Đã tải model $model" }
            } else {
                Write-Warn "Bỏ qua. Trước khi tạo bài, chạy: ollama pull $model"
            }
        }
    }
}

# 5. Whisper model (downloaded once into .cache\models) - only for local video processing
$env:PYTHONIOENCODING = 'utf-8'
if ($cloudMedia) {
    Write-Ok 'Nhận dạng giọng nói chạy trên GPU của Modal (không tải model Whisper về máy)'
} else {
    & $venvPy (Join-Path $Root 'scripts\download_model.py')
    if ($LASTEXITCODE -ne 0) { Write-Warn 'Chưa tải được model Whisper; ứng dụng sẽ thử tải lại ở lần chạy đầu.' }
    else { Write-Ok 'Model nhận dạng giọng nói' }
}

Write-Title 'Hoàn tất. Chạy start.bat để mở ứng dụng.'
