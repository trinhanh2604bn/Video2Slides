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

if (-not (Test-Path (Join-Path $Root '.env'))) {
    Copy-Item (Join-Path $Root '.env.example') (Join-Path $Root '.env')
    Write-Warn 'Đã tạo tệp .env mặc định từ .env.example.'
}
$envText = Get-Content (Join-Path $Root '.env') -Raw -Encoding UTF8

# Xử lý video: trên Modal (FFmpeg CPU + Whisper GPU) hoặc trên máy này
$hasProxy = ((Get-EnvSetting $envText 'MODAL_PROXY_TOKEN_ID') -ne '') -and ((Get-EnvSetting $envText 'MODAL_PROXY_TOKEN_SECRET') -ne '')
if (Test-CloudMedia $envText) {
    if ($hasProxy) { Write-Ok 'Xử lý video trên Modal (FFmpeg + Whisper GPU); không cần FFmpeg trên máy' }
    else { Write-Warn 'MEDIA_PROCESSING dùng Modal nhưng thiếu MODAL_PROXY_TOKEN_* trong .env.' }
} elseif (-not (Find-FFmpeg)) {
    Fail 'Không tìm thấy FFmpeg. Chạy lại install.bat, cài bằng lệnh: winget install Gyan.FFmpeg, hoặc cấu hình xử lý trên Modal (docs\MODAL_CLOUD_SETUP_VI.md).'
} else {
    Write-Ok 'FFmpeg (xử lý video trên máy)'
}

# AI viết nội dung slide: mặc định modal_ollama = Ollama trên Modal (bỏ qua Gemini); không cần Ollama trên máy
$provider = (Get-EnvSetting $envText 'LLM_PROVIDER' (Get-EnvSetting $envText 'AI_PROVIDER' 'modal_ollama')).ToLower()
$hasLlmWorker = (Get-EnvSetting $envText 'MODAL_LLM_WORKER_URL') -ne ''
if ($provider -in @('auto', 'modal_ollama')) {
    $hasGemini = (Get-EnvSetting $envText 'GEMINI_API_KEY') -ne ''
    if ($provider -eq 'auto' -and $hasGemini) { Write-Ok 'LLM: Gemini trước, tự chuyển sang Ollama trên Modal khi Gemini lỗi' }
    elseif ($provider -eq 'auto') { Write-Ok 'LLM: chưa có GEMINI_API_KEY -> dùng Ollama (Qwen3) trên Modal GPU' }
    else { Write-Ok 'LLM: Ollama (Qwen3) trên Modal GPU (mặc định, bỏ qua Gemini)' }
    if ($hasLlmWorker -and $hasProxy) { Write-Ok 'Ollama trên Modal đã cấu hình (GPU chỉ khởi động khi tạo bài)' }
    else { Write-Warn 'Thiếu MODAL_LLM_WORKER_URL / MODAL_PROXY_TOKEN_* - chưa có dự phòng Ollama trên Modal.' }
} elseif ($provider -eq 'ollama') {
    $base = (Get-EnvSetting $envText 'OLLAMA_BASE_URL' 'http://127.0.0.1:11434').TrimEnd('/')
    $model = Get-EnvSetting $envText 'OLLAMA_MODEL' 'qwen3:4b'
    $exe = Find-Ollama
    $ver = Start-OllamaIfNeeded $exe $base
    if (-not $ver) {
        if (-not $exe) {
            Write-Warn 'Chưa cài Ollama. Chạy lại install.bat, hoặc cài tại https://ollama.com/download'
        } else {
            Write-Warn "Ollama không phản hồi tại $base. Mở ứng dụng Ollama rồi tạo bài lại."
        }
    } else {
        Write-Ok "Ollama $ver tại $base"
        if (Test-OllamaModel $base $model) {
            Write-Ok "Model $model"
        } else {
            Write-Warn "Chưa có model $model. Chạy install.bat, hoặc lệnh: ollama pull $model"
        }
    }
} elseif ($provider -eq 'gemini') {
    if ((Get-EnvSetting $envText 'GEMINI_API_KEY') -ne '') { Write-Ok 'LLM: Gemini API (đã chọn rõ ràng, có thể tính phí)' }
    else { Write-Warn 'LLM_PROVIDER=gemini nhưng chưa có GEMINI_API_KEY trong .env.' }
} else {
    Write-Warn "LLM_PROVIDER=$provider"
}

# Google Slides: chỉ cần khi chọn xuất Google Slides
if (Test-Path (Join-Path $Root 'data\google\token.json')) { Write-Ok 'Google Slides đã được ủy quyền' }
elseif (Test-Path (Join-Path $Root 'data\google\client_secret.json')) { Write-Warn 'Google Slides: chưa kết nối (bấm "Kết nối Google" trong ứng dụng khi cần).' }
else { Write-Warn 'Google Slides: chưa cấu hình (xem docs\GOOGLE_SLIDES_SETUP_VI.md); vẫn tạo được .pptx.' }

# Ảnh minh họa AI (Modal SDXL): chỉ kiểm tra có cấu hình, không in giá trị bí mật
if ((Get-EnvSetting $envText 'AI_IMAGES_ENABLED' 'false') -eq 'true') {
    $hasUrl = (Get-EnvSetting $envText 'MODAL_IMAGE_WORKER_URL') -ne ''
    $hasTok = ((Get-EnvSetting $envText 'MODAL_PROXY_TOKEN_ID') -ne '') -and ((Get-EnvSetting $envText 'MODAL_PROXY_TOKEN_SECRET') -ne '')
    if ($hasUrl -and $hasTok) { Write-Ok 'Ảnh minh họa AI (Modal SDXL) đang bật' }
    else { Write-Warn 'AI_IMAGES_ENABLED=true nhưng thiếu MODAL_IMAGE_WORKER_URL / MODAL_PROXY_TOKEN_* trong .env.' }
}

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
& $py (Join-Path $Root 'scripts\run_local.py') @args
exit $LASTEXITCODE
