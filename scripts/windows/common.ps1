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
