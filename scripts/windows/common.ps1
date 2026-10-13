# Shared helpers for the Windows scripts (dot-sourced).
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Title([string]$text) { Write-Host ''; Write-Host "== $text ==" -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Warn([string]$text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Fail([string]$text) {
    Write-Host ''
    Write-Host "  [LỖI] $text" -ForegroundColor Red
    Write-Host ''
    exit 1
}

# FFmpeg on PATH, or the winget install location (added to PATH for this session only).
function Find-FFmpeg {
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
        if (Get-Command ffprobe -ErrorAction SilentlyContinue) { return $true }
    }
    $pkgs = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    if (Test-Path $pkgs) {
        $exe = Get-ChildItem $pkgs -Recurse -Filter ffprobe.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($exe) {
            $env:PATH = "$($exe.DirectoryName);$env:PATH"
            return $true
        }
    }
    return $false
}

# First value of NAME in .env (the app's loader also keeps the first occurrence); $default when absent/empty.
function Get-EnvSetting([string]$envText, [string]$name, [string]$default = '') {
    $m = [regex]::Match($envText, "(?m)^\s*$([regex]::Escape($name))\s*=\s*(.*?)\s*$")
    if ($m.Success) {
        $v = $m.Groups[1].Value.Trim().Trim('"').Trim("'")
        if ($v) { return $v }
    }
    $fromEnv = [Environment]::GetEnvironmentVariable($name)
    if ($fromEnv) { return $fromEnv }
    return $default
}

# ollama.exe on PATH or in the default per-user install folder.
function Find-Ollama {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $exe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path $exe) { return $exe }
    return $null
}

# Version string when the Ollama API answers at $baseUrl, otherwise $null.
function Get-OllamaVersion([string]$baseUrl) {
    try { return (Invoke-RestMethod -Uri "$baseUrl/api/version" -TimeoutSec 3).version } catch { return $null }
}

# Reuse a running Ollama; otherwise start one hidden 'ollama serve' bound to localhost and wait for it.
function Start-OllamaIfNeeded([string]$exe, [string]$baseUrl) {
    $ver = Get-OllamaVersion $baseUrl
    if ($ver) { return $ver }
    if (-not $exe) { return $null }
    $uri = [Uri]$baseUrl
    if ($uri.Host -notin @('127.0.0.1', 'localhost')) { return $null }  # never start a remote/LAN listener
    $env:OLLAMA_HOST = "127.0.0.1:$($uri.Port)"
    Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden | Out-Null
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        $ver = Get-OllamaVersion $baseUrl
        if ($ver) { return $ver }
    }
    return $null
}

# True when video processing (FFmpeg + Whisper) runs on the Modal media worker instead of this computer.
function Test-CloudMedia([string]$envText) {
    $mode = (Get-EnvSetting $envText 'MEDIA_PROCESSING' 'auto').ToLower()
    if ($mode -eq 'modal') { return $true }
    if ($mode -eq 'local') { return $false }
    return (Get-EnvSetting $envText 'MODAL_MEDIA_WORKER_URL') -ne ''
}

function Test-OllamaModel([string]$baseUrl, [string]$model) {
    try {
        $names = (Invoke-RestMethod -Uri "$baseUrl/api/tags" -TimeoutSec 5).models | ForEach-Object { $_.name }
    } catch { return $false }
    return ($names -contains $model) -or (($model -notmatch ':') -and ($names -contains "$model`:latest"))
}
