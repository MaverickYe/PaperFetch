"""Run a real one-paper smoke test against the frozen Windows executable.

This script is intended for a Windows runner (for example GitHub Actions),
not for the Linux source checkout. It uses a temporary data directory and
does not touch project data.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from openpyxl import Workbook


DOI = "10.1371/journal.pone.0044289"


def http_json(base: str, token: str, path: str, method: str = "GET", payload=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"X-Local-Token": token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_service(base: str, process: subprocess.Popen) -> str:
    deadline = time.time() + 30
    last_error = ""
    while time.time() < deadline:
        if process.poll() is not None:
            output = process.stdout.read().decode("utf-8", errors="replace") if process.stdout else ""
            raise RuntimeError(f"可执行文件提前退出，退出码={process.returncode}；{output[-1000:]}")
        try:
            with urllib.request.urlopen(base + "/", timeout=5) as response:
                html = response.read().decode("utf-8")
            match = re.search(r"const token='([^']+)'", html)
            if match:
                return match.group(1)
            last_error = "首页中未找到本地授权令牌"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"等待本地服务超时：{last_error}")


def make_workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["DOI", "英文标题"])
    sheet.append([DOI, ""])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--port", type=int, default=43139)
    args = parser.parse_args()
    executable = args.executable.resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)

    base = f"http://127.0.0.1:{args.port}"
    with tempfile.TemporaryDirectory(prefix="paperfetch_windows_smoke_") as directory:
        env = os.environ.copy()
        env["PAPERFETCH_PORT"] = str(args.port)
        env["PAPERFETCH_DATA"] = directory
        process = subprocess.Popen(
            [str(executable)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            token = wait_for_service(base, process)
            imported = http_json(
                base,
                token,
                "/api/import",
                "POST",
                {"filename": "windows-smoke.xlsx", "content": base64.b64encode(make_workbook()).decode("ascii")},
            )
            run_id = imported["id"]
            http_json(base, token, "/api/titles", "POST", {"run": run_id})
            http_json(base, token, "/api/start", "POST", {"run": run_id})

            deadline = time.time() + 150
            snapshot = None
            while time.time() < deadline:
                snapshot = http_json(base, token, "/api/state?run=" + run_id)
                if not snapshot["title_active"] and not snapshot["download_active"]:
                    break
                time.sleep(2)
            if not snapshot or snapshot["title_active"] or snapshot["download_active"]:
                raise RuntimeError("标题或下载任务在 150 秒内未结束")

            paper = snapshot["papers"][0]
            labels = {item.get("label") for item in paper.get("manual_links", [])}
            required_labels = {"DOI 页面", "Europe PMC 检索", "Semantic Scholar 检索", "Google Scholar 检索"}
            if not (paper.get("title") or "").strip():
                raise RuntimeError("Windows 可执行版未获取论文标题")
            if paper.get("status") != "downloaded" or not paper.get("path"):
                raise RuntimeError(f"Windows 可执行版未完成自动下载：{paper.get('status')} / {paper.get('message')}")
            if not required_labels.issubset(labels):
                raise RuntimeError(f"备用下载入口不完整：{sorted(labels)}")
            if paper.get("verification") not in {"DOI_MATCH", "TITLE_MATCH", "UNMATCHED"}:
                raise RuntimeError(f"PDF 校验字段异常：{paper.get('verification')}")
            print("Windows executable smoke test: PASS")
            print(f"title: {paper['title']}")
            print(f"verification: {paper['verification']}")
            print(f"manual_links: {len(paper['manual_links'])}")
            return 0
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Windows executable smoke test: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
