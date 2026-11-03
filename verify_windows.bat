@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo 未找到 Python 启动器 py。请安装 Python 3.12 后重试。
    pause
    exit /b 1
)

py -3.12 -c "import sys; print(sys.version)" >nul 2>nul
if errorlevel 1 (
    echo 未找到 Python 3.12。请安装 Python 3.12 后重试。
    pause
    exit /b 1
)

if not exist "dist\PaperFetch\PaperFetch.exe" (
    echo 正在构建 PaperFetch 目录版，首次运行需要联网安装依赖...
    powershell -ExecutionPolicy Bypass -File "literature_downloader\build_windows_release.ps1"
    if errorlevel 1 (
        echo Windows 构建失败。
        pause
        exit /b 1
    )
)

echo 正在运行 Windows 冒烟测试...
py -3.12 "literature_downloader\windows_smoke.py" --executable "dist\PaperFetch\PaperFetch.exe"
if errorlevel 1 (
    echo 冒烟测试失败，请保留窗口中的错误信息。
    pause
    exit /b 1
)

echo 冒烟测试通过，正在启动 PaperFetch...
start "PaperFetch" "%CD%\dist\PaperFetch\PaperFetch.exe"
exit /b 0
