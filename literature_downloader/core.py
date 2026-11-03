from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import openpyxl
import pymupdf
import requests

ROOT = Path(__file__).resolve().parent
RUN_ID_RE = re.compile(r'^\d{8}_\d{6}_[0-9a-f]{8}$')


def default_data_root():
    """Return a writable per-user data directory for source and frozen builds."""
    configured = (os.environ.get('PAPERFETCH_DATA') or os.environ.get('APPJ_DOWNLOADER_DATA') or '').strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        if sys.platform.startswith('win'):
            # The Windows verification build is portable: keep its local
            # database beside the executable so startup does not depend on
            # a stale or locked per-user service directory.
            return Path(sys.executable).resolve().parent / 'data'
        if sys.platform == 'darwin':
            return Path.home() / 'Library' / 'Application Support' / 'PaperFetch'
        return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share')) / 'paperfetch'
    return ROOT / 'data'


DATA = default_data_root().expanduser().resolve()
DB = DATA / "jobs.sqlite3"
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
MAX_PDF = 70 * 1024 * 1024
UA = "PaperFetch/1.1 (local academic literature retrieval)"
STOP = threading.Event()
TITLE_STOP = threading.Event()
_lock = threading.Lock()
_host_lock = threading.Lock()
_host_times: dict[str, float] = {}
_download_worker: threading.Thread | None = None
_title_worker: threading.Thread | None = None
# Compatibility alias used by the smoke test and older callers.
_worker: threading.Thread | None = None
_browser_thread: threading.Thread | None = None
_browser_commands: dict[str, str] = {}
_end_requested: set[str] = set()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def settings_path():
    return DATA / 'settings.json'


def default_save_dir():
    return DATA / 'runs'


def load_settings():
    try:
        data = json.loads(settings_path().read_text(encoding='utf-8'))
        value = str(data.get('save_dir') or '').strip()
        return {'save_dir': str(Path(value).expanduser().resolve()) if value else ''}
    except (OSError, ValueError, TypeError):
        return {'save_dir': ''}


def configured_save_dir():
    value = load_settings().get('save_dir')
    return Path(value) if value else default_save_dir()


def public_settings():
    configured = bool(load_settings().get('save_dir'))
    return {
        'configured': configured,
        'status': '已选择自定义保存文件夹' if configured else '使用应用默认保存文件夹',
    }


def set_save_dir(value):
    path = Path(str(value or '')).expanduser().resolve()
    if not path.is_absolute():
        raise ValueError('保存文件夹必须是本机绝对路径。')
    path.mkdir(parents=True, exist_ok=True)
    if not os.access(path, os.W_OK):
        raise ValueError('保存文件夹不可写，请选择其他文件夹。')
    settings_path().write_text(json.dumps({'save_dir': str(path)}, ensure_ascii=False, indent=2), encoding='utf-8')
    return public_settings()


def choose_save_dir():
    """Open a native folder picker without putting the chosen path in the web UI."""
    selected = ''
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        selected = filedialog.askdirectory(title='选择文献保存文件夹', mustexist=True)
        root.destroy()
    except Exception:
        # Frozen Linux builds may not contain Tk. Prefer the desktop's native
        # picker before falling back to the browser's paste-path prompt.
        if shutil.which('zenity'):
            result = subprocess.run(
                ['zenity', '--file-selection', '--directory', '--title=选择文献保存文件夹'],
                capture_output=True, text=True, check=False,
            )
            selected = result.stdout.strip() if result.returncode == 0 else ''
        elif shutil.which('kdialog'):
            result = subprocess.run(
                ['kdialog', '--getexistingdirectory', str(Path.home()), '--title', '选择文献保存文件夹'],
                capture_output=True, text=True, check=False,
            )
            selected = result.stdout.strip() if result.returncode == 0 else ''
        else:
            raise ValueError('无法打开系统文件夹选择器；请确认桌面环境可用。')
    if not selected:
        return public_settings()
    return set_save_dir(selected)


def reset_save_dir():
    try:
        settings_path().unlink()
    except FileNotFoundError:
        pass
    return public_settings()


def open_save_dir():
    path = configured_save_dir()
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith('win'):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)])
    return public_settings()


def default_browser_profile():
    configured = (os.environ.get('PAPERFETCH_BROWSER_PROFILE') or os.environ.get('APPJ_BROWSER_PROFILE') or '').strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform.startswith('win'):
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or Path.home()
        return Path(base) / 'PaperFetch' / 'browser-profile'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'PaperFetch' / 'browser-profile'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share')) / 'paperfetch' / 'browser-profile'


def browser_executable():
    """Find a locally installed Chromium-family browser, if available."""
    configured = (os.environ.get('PAPERFETCH_BROWSER_EXECUTABLE') or os.environ.get('APPJ_BROWSER_EXECUTABLE') or '').strip()
    candidates = [Path(configured)] if configured else []
    if sys.platform.startswith('win'):
        for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            base = os.environ.get(key)
            if base:
                root = Path(base)
                candidates.extend((
                    root / 'Google/Chrome/Application/chrome.exe',
                    root / 'Microsoft/Edge/Application/msedge.exe',
                    root / 'Chromium/Application/chrome.exe',
                ))
    elif sys.platform == 'darwin':
        candidates.extend((
            Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
            Path('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'),
            Path('/Applications/Chromium.app/Contents/MacOS/Chromium'),
        ))
    else:
        candidates.extend(Path(p) for p in (
            '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
            '/usr/bin/chromium', '/usr/bin/chromium-browser',
        ))
    return next((path for path in candidates if path.is_file()), None)


def migrate_legacy_data():
    """Copy data from the previous branded frozen install on first launch."""
    if not getattr(sys, 'frozen', False) or (DATA / 'jobs.sqlite3').exists():
        return False
    if sys.platform.startswith('win'):
        base = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or Path.home())
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))
    override = (os.environ.get('PAPERFETCH_LEGACY_DATA') or '').strip()
    candidates = [Path(override).expanduser()] if override else []
    candidates.extend((base / 'appj-literature-downloader', base / 'APPJ Literature Downloader'))
    candidates = [path for path in candidates if path.resolve() != DATA.resolve()]
    legacy = next((path for path in candidates if (path / 'jobs.sqlite3').is_file()), None)
    if not legacy:
        return False
    DATA.mkdir(parents=True, exist_ok=True)
    for name in ('jobs.sqlite3', 'settings.json'):
        source = legacy / name
        if source.is_file():
            shutil.copy2(source, DATA / name)
    for name in ('runs', 'browser-profile'):
        source = legacy / name
        if source.is_dir():
            shutil.copytree(source, DATA / name, dirs_exist_ok=True)
    return True


def run_folder(run_id):
    with db() as c:
        row = c.execute('SELECT save_dir FROM runs WHERE id=?', (run_id,)).fetchone()
    base = Path(row['save_dir']) if row and row['save_dir'] else default_save_dir()
    return base.expanduser().resolve() / run_id


def init():
    migrate_legacy_data()
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created TEXT, filename TEXT, sha256 TEXT, state TEXT, message TEXT, save_dir TEXT);
        CREATE TABLE IF NOT EXISTS papers(id INTEGER PRIMARY KEY, run_id TEXT, row_no INTEGER, doi TEXT, title TEXT, urls TEXT, manual_links TEXT NOT NULL DEFAULT '[]',
            status TEXT, selected INTEGER NOT NULL DEFAULT 1, message TEXT, landing TEXT, pdf_url TEXT, source_url TEXT,
            path TEXT, sha256 TEXT, verification TEXT, manual_state TEXT NOT NULL DEFAULT 'none',
            manual_completed_at TEXT, manual_source TEXT, updated TEXT, FOREIGN KEY(run_id) REFERENCES runs(id), UNIQUE(run_id, doi));
        CREATE INDEX IF NOT EXISTS papers_run ON papers(run_id, id);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at TEXT, run_id TEXT, paper_id INTEGER, event TEXT, detail TEXT);
        """)
        run_columns = {row[1] for row in c.execute('PRAGMA table_info(runs)')}
        if 'save_dir' not in run_columns:
            c.execute('ALTER TABLE runs ADD COLUMN save_dir TEXT')
        c.execute("UPDATE runs SET save_dir=? WHERE COALESCE(save_dir,'')=''", (str(default_save_dir().resolve()),))
        columns = {row[1] for row in c.execute('PRAGMA table_info(papers)')}
        if 'selected' not in columns:
            c.execute("ALTER TABLE papers ADD COLUMN selected INTEGER NOT NULL DEFAULT 1")
        if 'manual_state' not in columns:
            c.execute("ALTER TABLE papers ADD COLUMN manual_state TEXT NOT NULL DEFAULT 'none'")
        if 'manual_completed_at' not in columns:
            c.execute("ALTER TABLE papers ADD COLUMN manual_completed_at TEXT")
        if 'manual_source' not in columns:
            c.execute("ALTER TABLE papers ADD COLUMN manual_source TEXT")
        if 'manual_links' not in columns:
            c.execute("ALTER TABLE papers ADD COLUMN manual_links TEXT NOT NULL DEFAULT '[]'")
        c.execute("UPDATE papers SET manual_links='[]' WHERE COALESCE(manual_links,'')=''")
        c.execute("UPDATE papers SET status='manual', manual_state='pending', message='历史候选 PDF；请人工确认是否为目标文章' WHERE status='review'")
        c.execute("UPDATE papers SET manual_state='pending' WHERE status='manual' AND COALESCE(manual_state,'none')='none'")
        c.execute("UPDATE papers SET status='queued',message='上次运行中断，可继续' WHERE status='working'")
        c.execute("UPDATE runs SET state='paused',message='上次运行中断，可继续' WHERE state IN ('running','stopping','browser')")


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    return c


def update_paper(pid, **fields):
    fields['updated'] = now()
    with db() as c:
        c.execute("UPDATE papers SET " + ','.join(k + '=?' for k in fields) + " WHERE id=?", (*fields.values(), pid))


def update_run(run_id, state, message=''):
    with db() as c:
        c.execute("UPDATE runs SET state=?,message=? WHERE id=?", (state, message, run_id))


def event(run_id, pid, kind, detail=''):
    with db() as c:
        c.execute("INSERT INTO events(at,run_id,paper_id,event,detail) VALUES(?,?,?,?,?)", (now(), run_id, pid, kind, str(detail)[:1500]))


def normalize_doi(value):
    if value is None:
        return ''
    m = DOI_RE.search(str(value))
    return m.group(0).rstrip('.,;:)]}，；。').lower() if m else ''


def is_title_header(header):
    h = re.sub(r'[\s_\-]+', '', str(header or '').strip().lower())
    return h in {
        'title', 'titleen', 'titlecn', 'articletitle', 'papertitle',
        '论文标题', '文章标题', '论文题目', '文章名称', '题名', '标题',
        '中文标题', '英文标题', '中文题目', '英文题目'
    } or 'title' in h or '标题' in h or '题目' in h


def public_url(url):
    """Reject local targets, including redirects; no credentials in URLs."""
    u = urlsplit(url)
    if u.scheme not in ('https', 'http') or not u.hostname or u.username or u.password:
        raise ValueError('非公开 HTTP(S) 链接')
    try:
        literal = ipaddress.ip_address(u.hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError('链接指向非公网地址')
    addresses = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    # Local transparent DNS proxies may synthesize benchmark addresses for public
    # domains. Only allow these as DNS results, never as user-supplied literals.
    fake_dns_ranges = (ipaddress.ip_network('198.18.0.0/15'), ipaddress.ip_network('2001:2::/48'))
    def allowed(raw):
        addr = ipaddress.ip_address(raw)
        return addr.is_global or (literal is None and any(addr in block for block in fake_dns_ranges))
    if not addresses or any(not allowed(x[4][0]) for x in addresses):
        raise ValueError('链接指向非公网地址')
    return url


def safe_log_url(url):
    try:
        u = urlsplit(url)
        # Query strings may contain temporary access tokens; keep the origin/path only.
        return urlunsplit((u.scheme, u.netloc, u.path, '', ''))
    except Exception:
        return ''


def _display_link(url, keep_query=False):
    """Return a safe HTTP(S) link for the local UI, never credentials."""
    try:
        u = urlsplit(str(url or '').strip())
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
            return ''
        if keep_query:
            return urlunsplit((u.scheme, u.netloc, u.path, u.query, u.fragment))
        return safe_log_url(url)
    except Exception:
        return ''


def _candidate_link_records(entries):
    """Serialize discovered candidates without storing access-token queries."""
    records, seen = [], set()
    for item in entries or []:
        if isinstance(item, dict):
            url, label, kind = item.get('url'), item.get('label'), item.get('kind')
        elif isinstance(item, (list, tuple)) and item:
            url = item[0]
            label = item[1] if len(item) > 1 else ''
            kind = item[2] if len(item) > 2 else ''
        else:
            continue
        # Search links contain the DOI in a deterministic query; preserve that
        # query so the link remains useful and can be deduplicated on refresh.
        url = _display_link(url, keep_query=str(kind or '').lower() == 'search')
        if not url or url in seen:
            continue
        seen.add(url)
        low = url.lower()
        records.append({'label': str(label or '候选入口')[:80], 'url': url,
                        'kind': kind or ('pdf' if '.pdf' in low or '/pdf' in low else 'page')})
    return records


def public_manual_links(paper):
    """Build the visible alternate-entry list for one paper."""
    records, seen = [], set()

    def add(label, url, kind='page', keep_query=False):
        url = _display_link(url, keep_query=keep_query)
        if not url or url in seen:
            return
        seen.add(url)
        records.append({'label': label, 'url': url, 'kind': kind})

    doi = str(paper.get('doi') or '').strip()
    if doi:
        add('DOI 页面', 'https://doi.org/' + quote(doi, safe='/'))
    add('出版社页面', paper.get('landing'))
    add('已发现 PDF 直链', paper.get('pdf_url'), 'pdf')
    try:
        input_urls = json.loads(paper.get('urls') or '[]')
    except (TypeError, ValueError):
        input_urls = []
    for index, url in enumerate(input_urls):
        add('输入表链接' if len(input_urls) == 1 else f'输入表链接 {index + 1}', url,
            'pdf' if '.pdf' in str(url).lower() or '/pdf' in str(url).lower() else 'page')
    try:
        stored = json.loads(paper.get('manual_links') or '[]')
    except (TypeError, ValueError):
        stored = []
    for item in _candidate_link_records(stored):
        add(item['label'], item['url'], item['kind'], keep_query=True)
    if doi:
        query = quote(f'DOI:"{doi}"', safe='')
        add('Europe PMC 检索', 'https://europepmc.org/search?query=' + query, 'search', keep_query=True)
        add('Semantic Scholar 检索', 'https://www.semanticscholar.org/search?q=' + quote(doi, safe=''), 'search', keep_query=True)
        add('Google Scholar 检索', 'https://scholar.google.com/scholar?q=' + quote(doi, safe=''), 'search', keep_query=True)
    return records


def manual_link_text(paper):
    return '\n'.join(f"{item['label']}: {item['url']}" for item in public_manual_links(paper))


def initial_manual_links(doi, urls):
    """Create immediately available manual-entry links at import time."""
    paper = {
        'doi': doi,
        'landing': 'https://doi.org/' + quote(doi, safe='/'),
        'pdf_url': '',
        'urls': json.dumps(urls or [], ensure_ascii=False),
        'manual_links': '[]',
    }
    return json.dumps(public_manual_links(paper), ensure_ascii=False)


def request(url, **kwargs):
    headers = {'User-Agent': UA, 'Accept': '*/*'}
    headers.update(kwargs.pop('headers', {}))
    for _ in range(6):
        public_url(url)
        host = urlsplit(url).hostname
        with _host_lock:
            wait = max(0, _host_times.get(host, 0) - time.monotonic())
            _host_times[host] = max(time.monotonic(), _host_times.get(host, 0)) + 0.5
        if wait:
            time.sleep(wait)
        response = requests.get(url, headers=headers, timeout=(8, 25), allow_redirects=False, **kwargs)
        if response.status_code in (429, 503):
            try: cooldown = min(60, max(2, int(response.headers.get('Retry-After', '5'))))
            except ValueError: cooldown = 5
            with _host_lock:
                _host_times[host] = max(_host_times.get(host, 0), time.monotonic() + cooldown)
        if response.status_code in (301, 302, 303, 307, 308):
            nxt = response.headers.get('Location')
            response.close()
            if not nxt:
                raise ValueError('重定向缺少地址')
            url = urljoin(url, nxt)
            continue
        return response
    raise ValueError('重定向次数过多')


def parse_input(filename, content):
    suffix = Path(filename).suffix.lower()
    if suffix == '.xlsx':
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        tables = []
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            for row_index, row in enumerate(rows, 1):
                headers = [str(v or '').strip().lower() for v in row]
                doi_cols = [i for i, v in enumerate(headers) if v in ('doi', 'doi号', 'doi号码', '论文doi', '文章doi', 'digital object identifier')]
                if doi_cols:
                    tables.append((ws.title, row_index, headers, list(rows)))
                    break
                if row_index >= 20:
                    break
        wb.close()
        if not tables:
            raise ValueError('未找到 DOI 列，请将列名设为 DOI。')
        # Prefer the worksheet with the most DOI-bearing rows, avoiding instruction sheets.
        _, first, headers, rows = max(tables, key=lambda t: sum(bool(normalize_doi(r[headers_index(t[2], 'doi')])) for r in t[3] if len(r) > headers_index(t[2], 'doi')))
        pairs = [(first + i + 1, headers, row) for i, row in enumerate(rows)]
    elif suffix in ('.csv', '.tsv'):
        decoded = None
        for enc in ('utf-8-sig', 'gb18030'):
            try:
                decoded = content.decode(enc)
                break
            except UnicodeDecodeError:
                pass
        if decoded is None:
            raise ValueError('无法识别表格编码，请保存为 UTF-8。')
        records = list(csv.reader(io.StringIO(decoded), delimiter='\t' if suffix == '.tsv' else ','))
        if not records:
            raise ValueError('表格为空。')
        headers = [str(v).strip().lower() for v in records[0]]
        if headers_index(headers, 'doi') < 0:
            raise ValueError('未找到 DOI 列，请将列名设为 DOI。')
        pairs = [(i + 2, headers, row) for i, row in enumerate(records[1:])]
    elif suffix == '.txt':
        text = content.decode('utf-8-sig')
        pairs = [(i, ['doi'], [line]) for i, line in enumerate(text.splitlines(), 1)]
    else:
        raise ValueError('支持 .xlsx、.csv、.tsv 或每行一个 DOI 的 .txt。')
    results, seen, invalid, duplicates = [], set(), 0, 0
    for row_no, headers, row in pairs:
        idx = headers_index(headers, 'doi')
        val = row[idx] if idx >= 0 and len(row) > idx else ''
        doi = normalize_doi(val)
        if not doi:
            if str(val or '').strip().lower() in ('doi', 'doi号', 'doi号码', '论文doi', '文章doi', 'digital object identifier'):
                continue
            if val and str(val).strip(): invalid += 1
            continue
        if doi in seen:
            duplicates += 1
            continue
        seen.add(doi)
        title_values, urls = [], []
        for h, v in zip(headers, row):
            if not v: continue
            v = str(v).strip()
            if is_title_header(h) and v not in title_values:
                title_values.append(v)
            if ('url' in h or '网址' in h or '链接' in h or '入口' in h) and 'doi' not in h:
                for url in re.findall(r'https?://[^\s;，；<>]+', v):
                    if 'scholar.google' not in url and 'europepmc.org/search' not in url:
                        urls.append(url.rstrip('.,)'))
        title = ' / '.join(title_values)[:1000]
        results.append((row_no, doi, title, list(dict.fromkeys(urls))[:8]))
    if not results:
        raise ValueError('未发现有效 DOI。')
    if len(results) > 10000:
        raise ValueError('单次最多导入 10000 篇，请拆分表格。')
    return results, invalid, duplicates


def headers_index(headers, key):
    if key == 'doi':
        return next((i for i, h in enumerate(headers) if h in ('doi', 'doi号', 'doi号码', '论文doi', '文章doi', 'digital object identifier')), -1)
    return -1


def create_run(filename, content):
    rows, invalid, duplicates = parse_input(filename, content)
    run_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    base = configured_save_dir()
    base.mkdir(parents=True, exist_ok=True)
    if not os.access(base, os.W_OK):
        raise ValueError('保存文件夹不可写，请在设置中选择其他文件夹。')
    folder = base / run_id
    folder.mkdir(parents=True, exist_ok=False)
    (folder / 'pdf').mkdir()
    suffix = Path(filename).suffix.lower()
    source = folder / ('原始输入' + suffix)
    with source.open('xb') as f:
        f.write(content)
    with db() as c:
        c.execute('INSERT INTO runs(id,created,filename,sha256,state,message,save_dir) VALUES(?,?,?,?,?,?,?)', (run_id, now(), Path(filename).name, hashlib.sha256(content).hexdigest(), 'ready', f'有效 {len(rows)}；重复 {duplicates}；无效 {invalid}', str(base.resolve())))
        c.executemany('INSERT INTO papers(run_id,row_no,doi,title,urls,manual_links,status,selected,message,landing,pdf_url,source_url,path,sha256,verification,manual_state,manual_completed_at,manual_source,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      [(run_id, n, d, t, json.dumps(u, ensure_ascii=False), initial_manual_links(d, u), 'queued', 1, '', 'https://doi.org/' + quote(d, safe='/'), '', '', '', '', '', 'none', '', '', now()) for n, d, t, u in rows])
    event(run_id, None, 'import', f'{len(rows)} 条；重复 {duplicates}；无效 {invalid}；输入 SHA256 {hashlib.sha256(content).hexdigest()}')
    return {'id': run_id, 'count': len(rows), 'duplicates': duplicates, 'invalid': invalid}


def fetch_json(url):
    r = request(url, headers={'Accept': 'application/json'})
    try:
        r.raise_for_status()
        if len(r.content) > 3_000_000:
            raise ValueError('元数据过大')
        return r.json()
    finally:
        r.close()


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = {str(k).lower(): str(v or '').strip() for k, v in attrs}
        if tag.lower() == 'meta':
            key = (attrs.get('name') or attrs.get('property') or '').lower()
            if key in ('citation_title', 'dc.title', 'og:title', 'twitter:title') and attrs.get('content'):
                self.values.append(attrs['content'])
        elif tag.lower() == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title and data.strip():
            self.values.append(data.strip())


def metadata_for(paper):
    """Get title/landing metadata without downloading a PDF."""
    doi = paper['doi']
    title = paper['title'] or ''
    landing = paper['landing'] or 'https://doi.org/' + quote(doi, safe='/')
    candidates, errors = [], []
    try:
        data = fetch_json('https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=' + quote('DOI:"' + doi + '"') + '&format=json&resultType=core&pageSize=10')
        items = data.get('resultList', {}).get('result', [])
        for x in items:
            if str(x.get('doi', '')).lower() != doi.lower():
                continue
            title = title or x.get('title', '')
            for entry in x.get('fullTextUrlList', {}).get('fullTextUrl', []):
                url = entry.get('url', '')
                if url and ('pdf' in (entry.get('documentStyle') or '').lower() or '.pdf' in url.lower()):
                    candidates.append((url, 'Europe PMC'))
            if x.get('pmcid'):
                candidates.append(('https://pmc.ncbi.nlm.nih.gov/articles/' + x['pmcid'] + '/pdf/', 'PMC'))
    except Exception as e:
        errors.append('Europe PMC: ' + type(e).__name__)
    try:
        data = fetch_json('https://api.crossref.org/works/' + quote(doi, safe='/')).get('message', {})
        title = title or next(iter(data.get('title', [])), '')
        landing = data.get('URL') or landing
        for x in data.get('link', []):
            if 'pdf' in x.get('content-type', '').lower() and x.get('URL'):
                candidates.append((x['URL'], 'Crossref'))
    except Exception as e:
        errors.append('Crossref: ' + type(e).__name__)
    try:
        openalex_url = 'https://api.openalex.org/works/' + quote('https://doi.org/' + doi, safe='')
        data = fetch_json(openalex_url)
        title = title or data.get('title', '')
        locations = []
        if data.get('best_oa_location'):
            locations.append(data['best_oa_location'])
        locations.extend(data.get('locations') or [])
        for location in locations:
            pdf = location.get('pdf') or {}
            pdf_url = location.get('pdf_url') or pdf.get('url')
            landing_url = location.get('landing_page_url')
            if pdf_url:
                candidates.append((pdf_url, 'OpenAlex'))
            elif landing_url:
                candidates.append((landing_url, 'OpenAlex 开放获取页'))
    except Exception as e:
        errors.append('OpenAlex: ' + type(e).__name__)
    try:
        semantic_url = 'https://api.semanticscholar.org/graph/v1/paper/DOI:' + quote(doi, safe='') + '?fields=title,openAccessPdf,url'
        data = fetch_json(semantic_url)
        title = title or data.get('title', '')
        oa = data.get('openAccessPdf') or {}
        if oa.get('url'):
            candidates.append((oa['url'], 'Semantic Scholar'))
        if data.get('url'):
            candidates.append((data['url'], 'Semantic Scholar 页面'))
    except Exception as e:
        errors.append('Semantic Scholar: ' + type(e).__name__)
    try:
        response = request(landing, headers={'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.5'})
        try:
            if response.status_code == 200 and len(response.content) <= 2_000_000:
                parser = MetadataParser()
                parser.feed(response.content.decode(response.encoding or 'utf-8', errors='replace'))
                title = title or next((v for v in parser.values if v), '')
                links = PDFLinks(response.url or landing)
                links.feed(response.content.decode(response.encoding or 'utf-8', errors='replace'))
                candidates.extend((link, '论文页面 PDF') for link in links.links)
            landing = response.url or landing
        finally:
            response.close()
    except Exception as e:
        errors.append('论文页面: ' + type(e).__name__)
    return title.strip(), landing, candidates, errors


def discover(paper):
    title, landing, metadata_candidates, errors = metadata_for(paper)
    candidates = [(u, '输入表') for u in json.loads(paper['urls'] or '[]')]
    candidates.extend(metadata_candidates)
    # The publisher landing page may advertise a PDF even when metadata does not.
    candidates.append((landing, 'DOI 页面'))
    candidates.sort(key=lambda x: 0 if '.pdf' in x[0].lower() or '/pdf' in x[0].lower() else 1)
    return title, landing, list(dict.fromkeys(candidates)), errors


def pdf_check(content, doi, title):
    if not content.startswith(b'%PDF-') or len(content) > MAX_PDF:
        raise ValueError('响应不是有效 PDF 或文件过大')
    try:
        pdf = pymupdf.open(stream=content, filetype='pdf')
        if pdf.needs_pass or not len(pdf):
            raise ValueError('PDF 加密或没有页面')
        text = ' '.join(pdf[i].get_text() for i in range(min(2, len(pdf)))).lower()
        page_count = len(pdf)
        pdf.close()
    except Exception as e:
        raise ValueError('PDF 无法打开：' + str(e)[:150]) from e
    if re.search(r'(?<![a-z0-9_])' + re.escape(doi.lower()) + r'(?![a-z0-9_/-]|\.[a-z0-9])', text):
        return 'DOI_MATCH', page_count
    words = [w for w in re.findall(r'[a-z0-9]+', title.lower()) if len(w) >= 4]
    hits = sum(1 for w in set(words) if re.search(r'\b' + re.escape(w) + r'\b', text))
    if len(set(words)) >= 4 and hits / len(set(words)) >= 0.8:
        return 'TITLE_MATCH_DOI_NOT_FOUND', page_count
    return 'IDENTITY_UNCONFIRMED', page_count


class PDFLinks(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.links = base, []
    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        tag_name = tag.lower()
        if tag_name == 'meta' and (attr.get('name', '').lower() in ('citation_pdf_url', 'dc.identifier.pdf', 'eprints.document_url') or attr.get('property', '').lower() == 'citation_pdf_url'):
            if attr.get('content'): self.links.append(urljoin(self.base, attr['content']))
        elif tag_name == 'link' and ('alternate' in attr.get('rel', '').lower() or attr.get('type', '').lower() == 'application/pdf') and attr.get('href'):
            self.links.append(urljoin(self.base, attr['href']))
        elif tag_name in ('a', 'button'):
            href = attr.get('href') or attr.get('data-pdf-url') or attr.get('data-download-url') or attr.get('data-fulltext-url')
            if href and ('.pdf' in href.lower() or re.search(r'/pdf(?:/|\?|$)', href, re.I) or re.search(r'(download|fulltext)', href, re.I) and 'pdf' in href.lower()):
                self.links.append(urljoin(self.base, href))


def persist_pdf(paper, content, source):
    verification, pages = pdf_check(content, paper['doi'], paper['title'] or '')
    digest = hashlib.sha256(content).hexdigest()
    name = re.sub(r'[^a-zA-Z0-9_.-]+', '_', paper['doi'])[:130] + '_' + digest[:12] + '.pdf'
    folder = run_folder(paper['run_id']) / 'pdf'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    try:
        with path.open('xb') as f:
            f.write(content)
    except FileExistsError:
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest: raise
    state = 'downloaded'
    manual_work = paper.get('status') in ('manual', 'review') or paper.get('manual_state') in ('pending', 'in_progress', 'pending_review')
    message = f'{pages} 页；' + ('DOI 一致' if verification == 'DOI_MATCH' else '标题匹配，未发现 DOI' if verification == 'TITLE_MATCH_DOI_NOT_FOUND' else 'PDF 已下载，未匹配 DOI/标题')
    fields = {'status': state, 'message': message, 'source_url': safe_log_url(source), 'path': str(path), 'sha256': digest, 'verification': verification}
    if manual_work:
        fields.update(manual_state='completed', manual_completed_at=now(), manual_source='manual-upload' if source == 'manual-upload' else 'browser')
        fields['message'] = '人工处理已完成；' + message
    update_paper(paper['id'], **fields)
    event(paper['run_id'], paper['id'], 'manual_completed' if manual_work else state, f'{verification}; SHA256={digest}; {safe_log_url(source)}')
    return state


def try_candidate(url, paper, stop_event=None):
    stop_event = stop_event or STOP
    r = request(url, stream=True, headers={'Accept': 'application/pdf,text/html;q=0.8,*/*;q=0.5'})
    try:
        if r.status_code != 200:
            return None, f'HTTP {r.status_code}', []
        if int(r.headers.get('Content-Length', 0) or 0) > MAX_PDF:
            return None, '文件过大', []
        chunks, size = [], 0
        for chunk in r.iter_content(65536):
            if stop_event.is_set(): return None, '已暂停', []
            if chunk:
                size += len(chunk)
                if size > MAX_PDF: return None, '文件过大', []
                chunks.append(chunk)
                if size > 2 * 1024 * 1024 and not chunks[0].startswith(b'%PDF-'):
                    return None, '链接返回过大的网页', []
        content = b''.join(chunks)
        if content.startswith(b'%PDF-'):
            return (content, r.url), '', []
        if 'html' in r.headers.get('Content-Type', '').lower() or content.lstrip().lower().startswith((b'<!doctype html', b'<html')):
            parser = PDFLinks(r.url)
            parser.feed(content.decode(r.encoding or 'utf-8', errors='replace'))
            return None, '链接返回网页', list(dict.fromkeys(parser.links))[:8]
        return None, '链接返回非PDF', []
    finally:
        r.close()


def process_paper(pid, stop_event=None):
    stop_event = stop_event or STOP
    with db() as c:
        paper = dict(c.execute('SELECT * FROM papers WHERE id=?', (pid,)).fetchone())
    if stop_event.is_set(): return
    update_paper(pid, status='working', message='正在查找全文')
    try:
        title, landing, candidates, errors = discover(paper)
        paper['title'] = title
        update_paper(pid, title=title, landing=safe_log_url(landing),
                     manual_links=json.dumps(_candidate_link_records(candidates), ensure_ascii=False))
        problems = []
        seen = set()
        candidate_index = 0
        while candidate_index < len(candidates) and candidate_index < 16:
            url, label = candidates[candidate_index]
            candidate_index += 1
            if url in seen: continue
            seen.add(url)
            if stop_event.is_set():
                update_paper(pid, status='queued', message='已暂停，可继续')
                return
            try:
                event(paper['run_id'], pid, 'candidate', label + ': ' + safe_log_url(url))
                if not paper.get('pdf_url') and ('.pdf' in url.lower() or '/pdf' in url.lower()):
                    paper['pdf_url'] = safe_log_url(url)
                    update_paper(pid, pdf_url=paper['pdf_url'])
                result, error, links = try_candidate(url, paper, stop_event)
                candidates.extend((link, label + '页面') for link in links if link not in seen)
                if links:
                    update_paper(pid, manual_links=json.dumps(_candidate_link_records(candidates), ensure_ascii=False))
                if result:
                    update_paper(pid, pdf_url=safe_log_url(url))
                    persist_pdf(paper, result[0], result[1])
                    return
                problems.append(label + ': ' + error)
            except Exception as e:
                problems.append(label + ': ' + str(e)[:100])
        if stop_event.is_set():
            update_paper(pid, status='queued', message='已暂停，可继续')
            return
        message = '; '.join((problems + errors)[:5]) or '未找到可直接获取的 PDF'
        update_paper(pid, status='manual', manual_state='pending', message=message[:900])
        event(paper['run_id'], pid, 'manual', message)
    except Exception as e:
        update_paper(pid, status='manual', manual_state='pending', message='查询异常：' + str(e)[:500])
        event(paper['run_id'], pid, 'error', str(e)[:500])


def update_title(pid):
    with db() as c:
        row = c.execute('SELECT * FROM papers WHERE id=?', (pid,)).fetchone()
    if not row:
        return False
    paper = dict(row)
    try:
        title, landing, candidates, errors = metadata_for(paper)
        fields = {
            'landing': safe_log_url(landing),
            'manual_links': json.dumps(_candidate_link_records(candidates), ensure_ascii=False),
        }
        if title:
            fields.update(title=title, message='标题已获取')
        elif errors:
            fields.update(message='；'.join(errors[:3]))
        else:
            fields.update(message='未获取到论文标题')
        update_paper(pid, **fields)
        event(paper['run_id'], pid, 'title', fields.get('message', '标题查询完成'))
        return bool(title)
    except Exception as e:
        update_paper(pid, message='标题查询失败：' + str(e)[:300])
        event(paper['run_id'], pid, 'title_error', str(e)[:300])
        return False


def manual_complete(run_id, paper_id):
    with db() as c:
        row = c.execute('SELECT * FROM papers WHERE id=? AND run_id=?', (int(paper_id), run_id)).fetchone()
    if not row:
        raise ValueError('论文不存在')
    paper = dict(row)
    if paper['status'] not in ('manual', 'review'):
        raise ValueError('只有待人工处理或待核验的论文可以标记为已完成。')
    message = '已手动确认下载完成；程序未接收或校验文件。'
    update_paper(paper['id'], status='downloaded', manual_state='completed', manual_completed_at=now(), manual_source='manual-confirmation', message=message, verification='MANUAL_CONFIRMED')
    event(run_id, paper['id'], 'manual_confirmed', message)
    return 'completed'


def start_titles(run_id):
    global _title_worker, _worker
    with _lock:
        if (_title_worker and _title_worker.is_alive()) or (_browser_thread and _browser_thread.is_alive()):
            raise ValueError('标题获取或机构处理任务正在运行。')
        with db() as c:
            if not c.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
                raise ValueError('批次不存在')
            ids = [r[0] for r in c.execute("SELECT id FROM papers WHERE run_id=? AND (title IS NULL OR TRIM(title)='') ORDER BY id", (run_id,))]
        if not ids:
            raise ValueError('没有缺少标题的论文。')
        TITLE_STOP.clear()
        _end_requested.discard(run_id)
        download_active = _download_worker and _download_worker.is_alive()
        if not download_active:
            update_run(run_id, 'metadata', f'正在获取 {len(ids)} 篇论文标题')
        else:
            update_run(run_id, 'running', f'自动下载与标题获取并行进行：标题剩余 {len(ids)} 篇')

        def work():
            try:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    for pos in range(0, len(ids), 3):
                        if TITLE_STOP.is_set():
                            break
                        futures = [pool.submit(update_title, pid) for pid in ids[pos:pos + 3]]
                        for f in as_completed(futures):
                            f.result()
                ended = run_id in _end_requested
                _end_requested.discard(run_id)
                if _download_worker and _download_worker.is_alive():
                    update_run(run_id, 'running', '标题获取已结束；自动下载仍在进行')
                else:
                    update_run(run_id, 'ended' if ended else ('paused' if TITLE_STOP.is_set() else 'ready'), '标题获取已结束；未处理条目保留在列表' if ended else ('标题获取已暂停，可继续' if TITLE_STOP.is_set() else '标题获取完成，可开始下载'))
            except Exception as e:
                update_run(run_id, 'paused', '标题获取异常：' + str(e)[:300])

        _title_worker = threading.Thread(target=work, daemon=True)
        _worker = _title_worker
        _title_worker.start()


def set_selection(run_id, mode='all', start=None, end=None, paper_id=None, selected=None):
    with _lock:
        with db() as c:
            if not c.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
                raise ValueError('批次不存在')
            run = c.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone()
            if run and run[0] in ('running', 'stopping', 'browser', 'ending'):
                raise ValueError('任务运行中，暂时不能修改勾选范围。')
            rows = list(c.execute('SELECT id,status,selected,manual_state FROM papers WHERE run_id=? ORDER BY id', (run_id,)))
            if paper_id is not None:
                wanted = {int(paper_id): bool(selected)}
            else:
                if mode == 'all':
                    wanted = {row['id']: True for row in rows}
                elif mode == 'none':
                    wanted = {row['id']: False for row in rows}
                elif mode == 'range':
                    try:
                        first, last = int(start), int(end)
                    except (TypeError, ValueError) as e:
                        raise ValueError('范围必须是数字。') from e
                    if first < 1 or last < first or last > len(rows):
                        raise ValueError(f'范围应在 1 到 {len(rows)} 之间。')
                    wanted = {row['id']: first <= i <= last for i, row in enumerate(rows, 1)}
                else:
                    raise ValueError('无效的勾选操作。')
            for row in rows:
                if row['id'] not in wanted:
                    continue
                value = int(wanted[row['id']])
                fields = {'selected': value}
                if not value and row['status'] == 'queued':
                    fields.update(status='excluded', message='未选中，暂不处理')
                elif value and row['status'] == 'excluded':
                    fields.update(status='queued', message='等待处理')
                update_paper(row['id'], **fields)
            event(run_id, None, 'selection', f'更新勾选范围：{mode}')


def start(run_id, retry=False):
    global _download_worker, _worker
    with _lock:
        if (_download_worker and _download_worker.is_alive()) or (_browser_thread and _browser_thread.is_alive()):
            raise ValueError('自动下载或机构处理任务正在运行。')
        with db() as c:
            if not c.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone(): raise ValueError('批次不存在')
            if retry:
                c.execute("UPDATE papers SET status='queued',manual_state='none',manual_completed_at='',manual_source='',message='等待重新查询' WHERE run_id=? AND status='manual' AND selected=1", (run_id,))
            ids = [r[0] for r in c.execute("SELECT id FROM papers WHERE run_id=? AND status='queued' AND selected=1 ORDER BY id", (run_id,))]
        if not ids: raise ValueError('没有已勾选且待自动处理的论文。')
        STOP.clear()
        _end_requested.discard(run_id)
        update_run(run_id, 'running', f'自动处理 {len(ids)} 篇')
        def work():
            try:
                # Limit outstanding tasks so pause is responsive and resume is reliable.
                with ThreadPoolExecutor(max_workers=3) as pool:
                    for pos in range(0, len(ids), 3):
                        if STOP.is_set(): break
                        futures = [pool.submit(process_paper, i) for i in ids[pos:pos+3]]
                        for f in as_completed(futures): f.result()
                ended = run_id in _end_requested
                _end_requested.discard(run_id)
                update_run(run_id, 'ended' if ended else ('paused' if STOP.is_set() else 'finished'), '已结束；未处理条目保留在列表' if ended else ('已暂停，可继续' if STOP.is_set() else '自动阶段结束，请查看待人工处理'))
            except Exception as e:
                update_run(run_id, 'paused', '运行异常：' + str(e)[:300])
        _download_worker = threading.Thread(target=work, daemon=True)
        _worker = _download_worker
        _download_worker.start()


def stop(run_id):
    STOP.set()
    TITLE_STOP.set()
    _browser_commands[run_id] = 'stop'
    with db() as c:
        r = c.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone()
    if r and r[0] in ('running', 'browser', 'metadata'):
        update_run(run_id, 'stopping', '正在结束当前请求')


def finish(run_id):
    STOP.set()
    TITLE_STOP.set()
    _end_requested.add(run_id)
    _browser_commands[run_id] = 'stop'
    with db() as c:
        if not c.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
            raise ValueError('批次不存在')
    update_run(run_id, 'ending', '正在结束批次；未处理条目将保留')


def delete_run(run_id, delete_files=False):
    """Remove one batch from the history, optionally removing its files."""
    global _download_worker, _title_worker, _browser_thread
    with _lock:
        if any(worker and worker.is_alive() for worker in (_download_worker, _title_worker, _browser_thread)):
            raise ValueError('任务运行中，请先暂停或结束当前任务后再清除列表。')
        with db() as c:
            row = c.execute('SELECT id,save_dir FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise ValueError('批次不存在。')
        folder = None
        if delete_files:
            if not RUN_ID_RE.fullmatch(str(run_id)):
                raise ValueError('批次编号无效，已拒绝删除文件。')
            base = Path(row['save_dir'] or default_save_dir()).expanduser().resolve()
            folder = (base / str(run_id)).resolve()
            if folder == base or folder.name != str(run_id) or not folder.is_relative_to(base):
                raise ValueError('批次文件夹路径无效，已拒绝删除文件。')
            if folder.exists():
                shutil.rmtree(folder)
        with db() as c:
            c.execute('DELETE FROM events WHERE run_id=?', (run_id,))
            c.execute('DELETE FROM papers WHERE run_id=?', (run_id,))
            c.execute('DELETE FROM runs WHERE id=?', (run_id,))
    return {'ok': True, 'files_deleted': bool(delete_files), 'files_preserved': not bool(delete_files)}


def browser_command(run_id, command):
    if command not in ('next', 'retry', 'institution', 'stop'): raise ValueError('无效操作')
    _browser_commands[run_id] = command


def start_browser(run_id):
    global _browser_thread
    with _lock:
        if (_download_worker and _download_worker.is_alive()) or (_title_worker and _title_worker.is_alive()) or (_browser_thread and _browser_thread.is_alive()):
            raise ValueError('已有下载或机构处理任务正在运行。')
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise ValueError('缺少 Playwright，请查看使用说明安装浏览器自动化依赖。') from e
        with db() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM papers WHERE run_id=? AND status IN ('manual','review') AND selected=1 ORDER BY id", (run_id,))]
        if not ids: raise ValueError('没有待机构或人工处理的论文。')
        STOP.clear()
        _browser_commands[run_id] = ''
        update_run(run_id, 'browser', '正在启动机构访问窗口')
        def work():
            current = {'paper': None, 'downloaded': False}
            try:
                with sync_playwright() as p:
                    profile = default_browser_profile().expanduser()
                    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
                    launch_options = {'headless': False, 'accept_downloads': True}
                    executable = browser_executable()
                    if executable:
                        launch_options['executable_path'] = str(executable)
                    context = p.chromium.launch_persistent_context(str(profile), **launch_options)
                    page = context.pages[0] if context.pages else context.new_page()
                    def downloaded(download):
                        paper = current['paper']
                        if not paper: return
                        temp = None
                        try:
                            temp = DATA / 'runs' / run_id / ('浏览器临时_' + uuid.uuid4().hex)
                            download.save_as(str(temp))
                            if temp.stat().st_size > MAX_PDF: raise ValueError('文件过大')
                            content = temp.read_bytes()
                            persist_pdf(paper, content, download.url)
                            current['downloaded'] = True
                        except Exception as e:
                            update_paper(paper['id'], status='manual', manual_state='pending', message='浏览器下载未接收：' + str(e)[:250])
                        finally:
                            if temp and temp.exists(): temp.unlink()
                    def register(tab): tab.on('download', downloaded)
                    context.on('page', register)
                    for tab in context.pages: register(tab)
                    def try_pdf():
                        active = context.pages[-1]
                        label = re.compile(r'^(download\s*)?(full[ -]?text\s*)?pdf$|^view\s+pdf$', re.I)
                        for role in ('link', 'button'):
                            locator = active.get_by_role(role, name=label).first
                            if locator.count() and locator.is_visible():
                                locator.click(timeout=5000)
                                return True
                        return False
                    for pid in ids:
                        if STOP.is_set(): break
                        with db() as c:
                            paper = dict(c.execute('SELECT * FROM papers WHERE id=?', (pid,)).fetchone())
                        if paper['status'] not in ('manual', 'review'): continue
                        current.update(paper=paper, downloaded=False)
                        _browser_commands[run_id] = ''
                        update_paper(pid, manual_state='in_progress', message='人工处理中；请在机构浏览器完成登录并获取 PDF。')
                        update_run(run_id, 'browser', f"机构处理：{paper['doi']}。请在浏览器登录并点击 PDF。")
                        try:
                            page.goto(paper['landing'] or ('https://doi.org/' + quote(paper['doi'], safe='/')), wait_until='domcontentloaded', timeout=45000)
                            if not current['downloaded']:
                                try: try_pdf()
                                except Exception: pass
                        except Exception as e:
                            update_paper(pid, message='打开页面失败：' + str(e)[:180])
                        while not STOP.is_set() and not current['downloaded']:
                            command = _browser_commands.get(run_id, '')
                            _browser_commands[run_id] = ''
                            if command == 'next': break
                            if command == 'stop': STOP.set(); break
                            if command == 'institution':
                                try:
                                    active = context.pages[-1]
                                    link = active.get_by_text(re.compile(r'^(access through your institution|sign in through your institution|institutional (login|access)|机构登录|通过机构登录)$', re.I)).first
                                    if link.count() and link.is_visible():
                                        link.click(timeout=5000)
                                        active.wait_for_timeout(800)
                                        active = context.pages[-1]
                                    box = active.get_by_placeholder(re.compile(r'institution|university|organization|机构|大学', re.I)).first
                                    if box.count() and box.is_visible():
                                        box.fill('Huazhong University of Science and Technology', timeout=5000)
                                        active.wait_for_timeout(1200)
                                    choice = active.get_by_text(re.compile(r'^(Huazhong University of Science and Technology|华中科技大学)$', re.I)).first
                                    if choice.count() and choice.is_visible():
                                        choice.click(timeout=5000)
                                        update_run(run_id, 'browser', '已尝试选择华中科技大学；请完成页面后续确认并下载 PDF。')
                                    else:
                                        update_run(run_id, 'browser', '未识别到机构选择，请在浏览器窗口手动完成。')
                                except Exception as e:
                                    update_run(run_id, 'browser', '机构自动选择未完成，请手动继续：' + str(e)[:120])
                            if command == 'retry':
                                try:
                                    if try_pdf(): pass
                                    else: update_run(run_id, 'browser', '未发现可自动点击的 PDF 链接，请在机构窗口手动点击。')
                                except Exception as e:
                                    update_run(run_id, 'browser', '自动点击未完成，请手动点击 PDF：' + str(e)[:120])
                            if not context.pages: raise ValueError('机构浏览器窗口已关闭')
                            context.pages[-1].wait_for_timeout(300)
                        if not current['downloaded']:
                            update_paper(pid, manual_state='pending', message='人工处理未完成；可继续处理或手动导入 PDF。')
                    context.close()
                ended = run_id in _end_requested
                _end_requested.discard(run_id)
                update_run(run_id, 'ended' if ended else 'finished', '机构处理已结束；仍未获得的论文保留在待人工列表。')
            except Exception as e:
                if current.get('paper') and not current.get('downloaded'):
                    update_paper(current['paper']['id'], manual_state='pending', message='机构处理已中断；可继续处理或手动导入 PDF。')
                update_run(run_id, 'paused', '机构浏览器未能运行：' + str(e)[:300])
        _browser_thread = threading.Thread(target=work, daemon=True)
        _browser_thread.start()


def state(run_id=None):
    with db() as c:
        runs = [dict(x) for x in c.execute('SELECT * FROM runs ORDER BY created DESC LIMIT 100')]
        if run_id:
            run = c.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run: raise ValueError('批次不存在')
            papers = [dict(x) for x in c.execute('SELECT * FROM papers WHERE run_id=? ORDER BY id', (run_id,))]
        else:
            run, papers = None, []
    # 页面端不需要知道本机数据目录，避免把用户名、挂载点或项目路径暴露到界面/API。
    # 保留空字段仅用于兼容旧页面脚本；绝不返回真实本机路径。
    return {
        'runs': runs,
        'run': dict(run) if run else None,
        'papers': papers,
        'data_root': '',
        'title_active': bool(_title_worker and _title_worker.is_alive()),
        'download_active': bool(_download_worker and _download_worker.is_alive()),
    }


def export_xlsx(run_id):
    snapshot = state(run_id)
    out = io.BytesIO()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '下载清单'
    columns = [('输入行','row_no'),('DOI','doi'),('标题','title'),('状态','status'),('勾选','selected'),('人工处理状态','manual_state'),('人工完成时间 UTC','manual_completed_at'),('人工处理来源','manual_source'),('说明','message'),('论文页面','landing'),('候选PDF地址','pdf_url'),('手动下载入口','manual_links_text'),('来源地址','source_url'),('保存路径','path'),('SHA256','sha256'),('身份校验','verification'),('更新时间 UTC','updated')]
    ws.append([a for a,b in columns])
    for paper in snapshot['papers']:
        # Prevent spreadsheet formula injection from publisher metadata.
        vals = [manual_link_text(paper) if b == 'manual_links_text' else paper.get(b) or '' for a,b in columns]
        ws.append(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for v in vals])
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    events = wb.create_sheet('处理日志')
    events.append(['时间 UTC', '论文 ID', '事件', '详情'])
    with db() as c:
        for row in c.execute('SELECT at,paper_id,event,detail FROM events WHERE run_id=? ORDER BY id', (run_id,)):
            events.append(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for v in row])
    events.freeze_panes = 'A2'
    events.auto_filter.ref = events.dimensions
    wb.save(out)
    return out.getvalue()
