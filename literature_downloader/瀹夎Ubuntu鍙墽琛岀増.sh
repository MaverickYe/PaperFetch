#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$app_dir/.." && pwd)"
binary="$project_dir/dist/PaperFetch"
if [ ! -x "$binary" ]; then
    printf '尚未找到可执行文件，请先运行：%s\n' "$app_dir/构建Ubuntu版本.sh" >&2
    exit 1
fi

user_home="$(getent passwd "$(id -u)" | cut -d: -f6)"
data_home="${XDG_DATA_HOME:-$user_home/.local/share}"
install_dir="$data_home/paperfetch"
applications_dir="$data_home/applications"
icons_dir="$data_home/icons/hicolor/scalable/apps"
mkdir -p "$install_dir" "$applications_dir" "$icons_dir"
cp "$binary" "$install_dir/PaperFetch"
cp "$app_dir/assets/paperfetch.svg" "$icons_dir/paperfetch.svg"
chmod +x "$install_dir/PaperFetch"

desktop_file="$applications_dir/paperfetch.desktop"
{
    printf '%s\n' '[Desktop Entry]' 'Type=Application' 'Name=文献快下'
    printf '%s\n' 'Comment=从 DOI 表查询并下载文献'
    printf 'Exec=%q --open\n' "$install_dir/PaperFetch"
    printf '%s\n' 'Icon=paperfetch' 'Terminal=false' 'Categories=Science;'
} > "$desktop_file"
chmod +x "$desktop_file"
printf 'Ubuntu 本地版本已安装到应用菜单：文献快下\n'
printf '可执行文件：%s\n' "$install_dir/PaperFetch"
