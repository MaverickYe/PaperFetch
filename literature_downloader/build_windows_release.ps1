$ErrorActionPreference = 'Stop'
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $AppDir
$Python = if ($env:PAPERFETCH_BUILD_PYTHON) { $env:PAPERFETCH_BUILD_PYTHON } else { 'py' }
$Spec = Join-Path $AppDir 'paperfetch_onedir.spec'
$Dist = Join-Path $ProjectDir 'dist'
$Build = Join-Path $ProjectDir 'build_windows'

& $Python -m pip install -r (Join-Path $AppDir 'requirements.txt') -r (Join-Path $AppDir 'build-requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败。' }

& $Python -m PyInstaller --clean --noconfirm --distpath $Dist --workpath $Build $Spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败。' }

Write-Host "Windows 可执行目录已生成：$(Join-Path $Dist 'PaperFetch')"
