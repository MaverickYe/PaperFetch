#!/usr/bin/env python3
from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import sys
import threading
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from literature_downloader import core

TOKEN = secrets.token_urlsafe(32)
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
INDEX = RESOURCE_ROOT / 'index.html'
if not INDEX.is_file():
    INDEX = Path(__file__).with_name('index.html')
MAX_BODY = 100 * 1024 * 1024
DEFAULT_PORT = 43127
STATUS_FILE = core.DATA / 'service.url'


class LocalHTTPServer(ThreadingHTTPServer):
    # The service is restarted by the launcher after an unexpected exit. Allow
    # the stable loopback port to be rebound without a long TIME_WAIT delay.
    allow_reuse_address = True
    daemon_threads = True


def service_error(message, exc=None):
    """Write a compact, local-only diagnostic without exposing it in the UI."""
    core.DATA.mkdir(parents=True, exist_ok=True)
    detail = f'{message}: {exc}' if exc else message
    with (core.DATA / 'service-error.log').open('a', encoding='utf-8') as fh:
        fh.write(detail + '\n')
        if exc:
            traceback.print_exc(file=fh)


def open_default_browser(url):
    """Open the URL through the operating system's default browser."""
    try:
        if os.name == 'nt':
            os.startfile(url)  # type: ignore[attr-defined]
            return
        if webbrowser.open_new(url):
            return
    except Exception:
        pass


def should_open_browser():
    """Open a browser for interactive launches, but not for headless CI smoke tests."""
    disabled = (os.environ.get('PAPERFETCH_NO_BROWSER') or '').strip().lower()
    return disabled not in {'1', 'true', 'yes', 'on'} and ('--open' in sys.argv or getattr(sys, 'frozen', False))


def healthy_existing_url():
    """Return the URL of a live instance without reading the locked file."""
    try:
        url = STATUS_FILE.read_text(encoding='utf-8').strip()
        if not url.startswith('http://127.0.0.1:'):
            return ''
        with urllib.request.urlopen(url, timeout=1.5) as response:
            if response.status < 500:
                return url
    except Exception:
        pass
    return ''
    try:
        webbrowser.open_new(url)
    except Exception:
        pass


def acquire_instance_lock():
    """Keep one local service instance across Linux, macOS and Windows."""
    lock = (core.DATA / 'service.lock').open('a+', encoding='utf-8')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(' ')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        # On Windows, reading the already-locked handle can itself raise
        # PermissionError. A second launch must not show an unhandled error.
        try:
            lock.seek(0)
            existing_url = lock.read().strip()
        except OSError:
            existing_url = ''
        if not existing_url:
            existing_url = healthy_existing_url()
        if should_open_browser() and existing_url.startswith('http://127.0.0.1:'):
            open_default_browser(existing_url)
        try:
            lock.close()
        except OSError:
            pass
        raise SystemExit(3)
    return lock


def configured_port():
    raw = (os.environ.get('PAPERFETCH_PORT') or os.environ.get('APPJ_PORT') or str(DEFAULT_PORT)).strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError('PAPERFETCH_PORT 必须是 0 到 65535 之间的端口号') from exc
    if not 0 <= port <= 65535:
        raise RuntimeError('PAPERFETCH_PORT 必须是 0 到 65535 之间的端口号')
    return port


def bind_server(requested_port):
    """Bind the requested loopback port, falling back around the default port."""
    ports = [requested_port]
    if requested_port == DEFAULT_PORT:
        ports.extend(range(DEFAULT_PORT + 1, DEFAULT_PORT + 11))
    last_error = None
    for port in ports:
        try:
            return LocalHTTPServer(('127.0.0.1', port), Handler)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError('无法绑定本地服务端口')


def public_state(run_id=None):
    """Return only UI-safe state; never expose local filenames or absolute paths."""
    snapshot = core.state(run_id)
    snapshot['data_root'] = ''
    for run in snapshot['runs']:
        run['filename'] = ''
        run['save_dir'] = ''
    if snapshot['run']:
        snapshot['run']['filename'] = ''
        snapshot['run']['save_dir'] = ''
    for paper in snapshot['papers']:
        paper['path'] = bool(paper.get('path'))
        paper['manual_links'] = core.public_manual_links(paper)
        # The UI only needs the curated links above; keep the raw import field
        # out of the browser payload.
        paper['urls'] = []
    return snapshot


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, status, content, content_type='application/json; charset=utf-8', headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def json(self, obj, status=200):
        self.send(status, json.dumps(obj, ensure_ascii=False).encode())

    def auth(self):
        return hmac.compare_digest(self.headers.get('X-Local-Token', ''), TOKEN)

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == '/':
            self.send(200, INDEX.read_text(encoding='utf-8').replace('__LOCAL_TOKEN__', TOKEN).encode(), 'text/html; charset=utf-8')
            return
        if not self.auth():
            self.json({'error':'请求未授权'}, 403); return
        try:
            q = parse_qs(u.query)
            run_id = q.get('run', [None])[0]
            if u.path == '/api/state': self.json(public_state(run_id))
            elif u.path == '/api/settings': self.json(core.public_settings())
            elif u.path == '/api/export':
                content = core.export_xlsx(run_id)
                self.send(200, content, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', {'Content-Disposition':f'attachment; filename="literature_{run_id}.xlsx"'})
            elif u.path == '/api/pdf':
                paper_id = int(q.get('paper', ['0'])[0])
                with core.db() as c:
                    paper = c.execute('SELECT path FROM papers WHERE id=? AND run_id=?', (paper_id, run_id)).fetchone()
                if not paper or not paper['path']: raise ValueError('没有可查看的 PDF')
                file = Path(paper['path']).resolve()
                if not file.is_relative_to(core.run_folder(run_id).resolve()) or not file.is_file(): raise ValueError('PDF 路径无效')
                if file.stat().st_size > core.MAX_PDF: raise ValueError('文件过大')
                self.send(200, file.read_bytes(), 'application/pdf', {'Content-Disposition':'inline; filename="paper.pdf"'})
            else: self.json({'error':'页面不存在'}, 404)
        except Exception as e: self.json({'error':str(e)}, 400)

    def do_POST(self):
        if not self.auth():
            self.json({'error':'请求未授权'}, 403); return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > MAX_BODY: raise ValueError('请求大小超出限制')
            data = json.loads(self.rfile.read(length))
            path = urlsplit(self.path).path
            if path == '/api/import':
                content = base64.b64decode(data.get('content', ''), validate=True)
                if len(content) > 20 * 1024 * 1024: raise ValueError('输入表不能超过20 MB')
                result = core.create_run(str(data.get('filename', '')), content)
            elif path == '/api/start':
                core.start(data['run'], bool(data.get('retry'))); result = {'ok':True}
            elif path == '/api/titles':
                core.start_titles(data['run']); result = {'ok':True}
            elif path == '/api/selection':
                core.set_selection(data['run'], data.get('mode', 'all'), data.get('start'), data.get('end'), data.get('paper_id'), data.get('selected'))
                result = {'ok':True}
            elif path == '/api/stop':
                core.stop(data['run']); result = {'ok':True}
            elif path == '/api/finish':
                core.finish(data['run']); result = {'ok':True}
            elif path == '/api/delete-run':
                result = core.delete_run(data['run'], bool(data.get('delete_files')))
            elif path == '/api/browser':
                core.start_browser(data['run']); result = {'ok':True}
            elif path == '/api/browser-command':
                core.browser_command(data['run'], data['command']); result = {'ok':True}
            elif path == '/api/manual-pdf':
                raw = base64.b64decode(data['content'], validate=True)
                with core.db() as c:
                    paper = c.execute('SELECT * FROM papers WHERE id=? AND run_id=?', (int(data['paper_id']), data['run'])).fetchone()
                if not paper: raise ValueError('论文不存在')
                if paper['status'] not in ('manual','review'): raise ValueError('此条目当前状态不可导入 PDF')
                status = core.persist_pdf(dict(paper), raw, 'manual-upload')
                result = {'status':status}
            elif path == '/api/manual-complete':
                result = {'status': core.manual_complete(data['run'], data['paper_id'])}
            elif path == '/api/settings/choose':
                result = core.choose_save_dir()
            elif path == '/api/settings/set':
                result = core.set_save_dir(data.get('folder', ''))
            elif path == '/api/settings/reset':
                result = core.reset_save_dir()
            elif path == '/api/settings/open':
                result = core.open_save_dir()
            elif path == '/api/shutdown':
                core.STOP.set()
                result = {'ok':True}
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else: self.json({'error':'接口不存在'}, 404); return
            self.json(result)
        except Exception as e: self.json({'error':str(e)}, 400)


def main():
    core.DATA.mkdir(parents=True, exist_ok=True)
    # Exit code 3 is reserved for the launcher: it means another healthy
    # instance owns the lock, so a watchdog must not restart in a loop.
    lock = acquire_instance_lock()
    try:
        STATUS_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    core.init()
    port = configured_port()
    try:
        server = bind_server(port)
    except OSError as exc:
        service_error(f'无法绑定本地端口 {port}', exc)
        print(f'文献快下无法使用端口 {port}：{exc}', file=sys.stderr, flush=True)
        raise SystemExit(4)
    url = f'http://127.0.0.1:{server.server_port}/'
    lock.seek(0)
    lock.truncate()
    lock.write(url)
    lock.flush()
    STATUS_FILE.write_text(url, encoding='utf-8')
    print(f'文献快下运行中：{url}', flush=True)
    if should_open_browser():
        threading.Timer(0.5, lambda: open_default_browser(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        service_error('本地服务主循环异常', exc)
        raise
    finally:
        core.STOP.set()
        server.server_close()
        try:
            if STATUS_FILE.read_text(encoding='utf-8').strip() == url:
                STATUS_FILE.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == '__main__': main()
