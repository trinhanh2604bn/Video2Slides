@echo off
rem Video2Slides: start (see docs\HUONG_DAN_SU_DUNG_VI.md)
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\start.ps1" %*
set "code=%ERRORLEVEL%"
if not "%code%"=="0" pause
exit /b %code%
