#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$app_dir/.." && pwd)"
python_bin="${PAPERFETCH_BUILD_PYTHON:-}"
if [ -z "$python_bin" ]; then
    python_bin="$(command -v python3 || true)"
fi
if [ ! -x "$python_bin" ]; then
    printf '找不到 Python 3。请设置 PAPERFETCH_BUILD_PYTHON 后重试。\n' >&2
    exit 1
fi

"$python_bin" -m pip install -r "$app_dir/requirements.txt" -r "$app_dir/build-requirements.txt"
cd "$project_dir"
"$python_bin" -m PyInstaller --clean --noconfirm "$app_dir/paperfetch.spec"
printf '\nUbuntu 可执行文件已生成：%s\n' "$project_dir/dist/PaperFetch"
printf '首次使用可运行：%s\n' "$app_dir/安装Ubuntu可执行版.sh"
