@echo off
setlocal
cd /d "%~dp0.."
if not exist "dist\PaperFetch\PaperFetch.exe" (
    echo 未找到 dist\PaperFetch\PaperFetch.exe，请先运行 build_windows_release.ps1。
    pause
    exit /b 1
)
start "PaperFetch" "%CD%\dist\PaperFetch\PaperFetch.exe"
