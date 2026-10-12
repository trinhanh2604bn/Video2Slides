@echo off
rem Video2Slides: install (see docs\HUONG_DAN_SU_DUNG_VI.md)
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\install.ps1" %*
set "code=%ERRORLEVEL%"
if not "%code%"=="0" pause
exit /b %code%
