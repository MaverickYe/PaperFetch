"""One-paper network smoke test in a temporary directory; no project data changed."""
import tempfile
from pathlib import Path

from literature_downloader import core

with tempfile.TemporaryDirectory(prefix='paperfetch_download_smoke_') as directory:
    core.DATA = Path(directory)
    core.DB = core.DATA / 'jobs.sqlite3'
    core.init()
    run = core.create_run('test.txt', b'10.1371/journal.pone.0044289\n')
    core.start(run['id'])
    core._worker.join(timeout=150)
    if core._worker.is_alive():
        core.STOP.set()
        raise SystemExit('FAIL: download timed out')
    row = core.state(run['id'])['papers'][0]
    print('status:', row['status'])
    print('verification:', row['verification'])
    print('source:', row['source_url'])
    print('details:', row['message'])
    if row['status'] != 'downloaded': raise SystemExit('FAIL: full text was not matched')
