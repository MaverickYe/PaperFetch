#!/usr/bin/env bash
set -euo pipefail
app_dir="$(cd "$(dirname "$0")" && pwd)"

# 可通过 PAPERFETCH_PYTHON 指定虚拟环境。
python_bin="${PAPERFETCH_PYTHON:-}"
if [ -z "$python_bin" ] && [ -x "$app_dir/.venv/bin/python" ]; then
    python_bin="$app_dir/.venv/bin/python"
fi
if [ -z "$python_bin" ] && command -v python3 >/dev/null 2>&1; then
    python_bin="$(command -v python3)"
fi
if [ ! -x "$python_bin" ]; then
    printf '找不到 Python。请先安装 Python 3，或设置 PAPERFETCH_PYTHON 指向可用环境。\n'
    exit 1
fi
mkdir -p "$app_dir/data"
# Use one stable loopback address so a browser tab can reconnect after the
# local service is restarted. PAPERFETCH_PORT can override it if another program
# already uses this port.
export PAPERFETCH_PORT="${PAPERFETCH_PORT:-43127}"
if [ "${PAPERFETCH_FOREGROUND:-0}" = "1" ]; then
    exec "$python_bin" "$app_dir/server.py" --open "$@"
fi
# Detach the local service from the terminal/desktop launcher. If Python exits
# unexpectedly, restart it after a short delay; normal shutdown and an already
# running instance both terminate the supervisor cleanly.
service_log="$app_dir/data/service.log"
supervisor=(bash -c '
set +e
while true; do
    "$1" "$2" --open
    exit_code=$?
    if [ "$exit_code" -eq 0 ] || [ "$exit_code" -eq 3 ] || [ "$exit_code" -eq 4 ]; then
        exit "$exit_code"
    fi
    printf "[%s] 本地服务异常退出（代码 %s），2 秒后自动重启。\\n" "$(date "+%F %T")" "$exit_code" >> "$3"
    sleep 2
done
')
if command -v setsid >/dev/null 2>&1; then
    nohup setsid "${supervisor[@]}" _ "$python_bin" "$app_dir/server.py" "$service_log" >"$service_log" 2>&1 < /dev/null &
else
    nohup "${supervisor[@]}" _ "$python_bin" "$app_dir/server.py" "$service_log" >"$service_log" 2>&1 < /dev/null &
fi
printf '文献快下已在后台启动；如页面已打开可直接继续上传。\n'
