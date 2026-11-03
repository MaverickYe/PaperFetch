import csv
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pymupdf

from literature_downloader import core


def make_pdf(text):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 80), text)
    raw = doc.tobytes()
    doc.close()
    return raw


class Response:
    def __init__(self, body, url, status=200, content_type='application/pdf'):
        self.body, self.url, self.status_code = body, url, status
        self.headers = {'Content-Type': content_type}
        self.encoding = 'utf-8'
    def iter_content(self, n):
        yield self.body
    def close(self): pass


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data, self.old_db = core.DATA, core.DB
        core.DATA = Path(self.temp.name)
        core.DB = core.DATA / 'jobs.sqlite3'
        core.STOP.clear()
        core.TITLE_STOP.clear()
        core.init()
    def tearDown(self):
        core.DATA, core.DB = self.old_data, self.old_db
        self.temp.cleanup()

    def test_import_excel_selects_doi_sheet_and_deduplicates(self):
        wb = openpyxl.Workbook()
        wb.active.title = '说明'
        wb.active.append(['提示', '请输入 DOI'])
        ws = wb.create_sheet('文章')
        ws.append(['DOI', '中文标题', '英文标题', 'PDF URL'])
        ws.append(['https://doi.org/10.1234/Example', '中文标题', 'Example title', 'https://example.org/article.pdf'])
        ws.append(['10.1234/example', 'duplicate', ''])
        ws.append(['bad DOI', 'invalid', ''])
        output = io.BytesIO(); wb.save(output)
        run = core.create_run('list.xlsx', output.getvalue())
        self.assertEqual((run['count'], run['duplicates'], run['invalid']), (1,1,1))
        p = core.state(run['id'])['papers'][0]
        self.assertEqual(p['doi'], '10.1234/example')
        self.assertEqual(p['title'], '中文标题 / Example title')
        self.assertIn('example.org', p['urls'])
        self.assertEqual(p['selected'], 1)
        self.assertEqual((core.DATA/'runs'/run['id']/'原始输入.xlsx').read_bytes(), output.getvalue())

    def test_selection_all_none_range_and_single_toggle(self):
        run = core.create_run('list.txt', b'10.1234/a\n10.1234/b\n10.1234/c\n')
        core.set_selection(run['id'], mode='range', start=2, end=3)
        papers = core.state(run['id'])['papers']
        self.assertEqual([p['selected'] for p in papers], [0, 1, 1])
        self.assertEqual(papers[0]['status'], 'excluded')
        core.set_selection(run['id'], paper_id=papers[0]['id'], selected=True)
        self.assertEqual(core.state(run['id'])['papers'][0]['status'], 'queued')
        core.set_selection(run['id'], mode='none')
        self.assertEqual(sum(p['selected'] for p in core.state(run['id'])['papers']), 0)

    def test_public_manual_links_include_alternate_sources(self):
        run = core.create_run('list.csv', b'DOI,PDF URL\n10.1234/example,https://input.example/article.pdf\n')
        paper = core.state(run['id'])['papers'][0]
        core.update_paper(
            paper['id'],
            landing='https://publisher.example/article?temporary_token=secret',
            pdf_url='https://publisher.example/article.pdf?temporary_token=secret',
            manual_links=json.dumps([{'label': 'OpenAlex', 'url': 'https://openalex.example/work', 'kind': 'page'}]),
        )
        links = core.public_manual_links(core.state(run['id'])['papers'][0])
        labels = [item['label'] for item in links]
        urls = [item['url'] for item in links]
        self.assertIn('DOI 页面', labels)
        self.assertIn('输入表链接', labels)
        self.assertIn('OpenAlex', labels)
        self.assertIn('Europe PMC 检索', labels)
        self.assertIn('Semantic Scholar 检索', labels)
        self.assertTrue(any(item['kind'] == 'pdf' for item in links))
        self.assertTrue(all('temporary_token' not in url for url in urls))

    def test_import_prepares_manual_links_before_any_automatic_download(self):
        run = core.create_run('list.txt', b'10.1234/ready-for-manual\n')
        paper = core.state(run['id'])['papers'][0]
        links = json.loads(paper['manual_links'])
        labels = [item['label'] for item in links]
        self.assertIn('DOI 页面', labels)
        self.assertIn('Europe PMC 检索', labels)
        self.assertEqual(len({item['url'] for item in links}), len(links))
        self.assertEqual(paper['status'], 'queued')

    def test_title_lookup_updates_missing_titles_without_download(self):
        run = core.create_run('list.txt', b'10.1234/a\n10.1234/b\n')
        def metadata(paper):
            return ('Title for ' + paper['doi'], 'https://example.org/article', [], [])
        with patch.object(core, 'metadata_for', side_effect=metadata):
            core.start_titles(run['id'])
            core._worker.join(timeout=10)
        papers = core.state(run['id'])['papers']
        self.assertEqual([p['title'] for p in papers], ['Title for 10.1234/a', 'Title for 10.1234/b'])
        self.assertEqual(core.state(run['id'])['run']['state'], 'ready')

    def test_selection_remains_available_during_title_lookup(self):
        run = core.create_run('list.txt', b'10.1234/a\n10.1234/b\n10.1234/c\n')
        core.update_run(run['id'], 'metadata', '正在获取标题')
        core.set_selection(run['id'], mode='range', start=2, end=2)
        papers = core.state(run['id'])['papers']
        self.assertEqual([p['selected'] for p in papers], [0, 1, 0])
        self.assertEqual([p['status'] for p in papers], ['excluded', 'queued', 'excluded'])

    def test_restart_recovers_interrupted_batch(self):
        run = core.create_run('list.txt', b'10.1234/restart\n')
        paper = core.state(run['id'])['papers'][0]
        core.update_paper(paper['id'], status='working', message='下载中')
        core.update_run(run['id'], 'running', '下载中')
        core.init()
        recovered = core.state(run['id'])
        self.assertEqual(recovered['run']['state'], 'paused')
        self.assertIn('上次运行中断', recovered['run']['message'])
        self.assertEqual(recovered['papers'][0]['status'], 'queued')
        self.assertIn('上次运行中断', recovered['papers'][0]['message'])

    def test_delete_run_can_keep_or_remove_batch_files(self):
        keep = core.create_run('keep.txt', b'10.1234/keep\n')
        keep_folder = core.run_folder(keep['id'])
        self.assertTrue(keep_folder.exists())
        result = core.delete_run(keep['id'])
        self.assertTrue(result['files_preserved'])
        self.assertTrue(keep_folder.exists())
        self.assertFalse(any(row['id'] == keep['id'] for row in core.state()['runs']))

        remove = core.create_run('remove.txt', b'10.1234/remove\n')
        remove_folder = core.run_folder(remove['id'])
        self.assertTrue(remove_folder.exists())
        result = core.delete_run(remove['id'], delete_files=True)
        self.assertTrue(result['files_deleted'])
        self.assertFalse(remove_folder.exists())
        self.assertFalse(any(row['id'] == remove['id'] for row in core.state()['runs']))

    def test_manual_processing_record_survives_deselect_and_records_completion(self):
        run = core.create_run('list.txt', b'10.1234/manual\n')
        paper = core.state(run['id'])['papers'][0]
        core.update_paper(paper['id'], status='manual', manual_state='pending', message='需要人工处理')
        core.set_selection(run['id'], mode='none')
        pending = core.state(run['id'])['papers'][0]
        self.assertEqual((pending['selected'], pending['status'], pending['manual_state']), (0, 'manual', 'pending'))
        self.assertEqual(core.persist_pdf(pending, make_pdf('doi 10.1234/manual'), 'manual-upload'), 'downloaded')
        completed = core.state(run['id'])['papers'][0]
        self.assertEqual(completed['manual_state'], 'completed')
        self.assertEqual(completed['manual_source'], 'manual-upload')
        self.assertTrue(completed['manual_completed_at'])
        self.assertIn('人工处理已完成', completed['message'])

    def test_manual_confirmation_moves_item_to_completed_status_without_pdf_import(self):
        run = core.create_run('list.txt', b'10.1234/manual-confirm\n')
        paper = core.state(run['id'])['papers'][0]
        core.update_paper(paper['id'], status='manual', manual_state='pending', message='需要人工处理')
        self.assertEqual(core.manual_complete(run['id'], paper['id']), 'completed')
        completed = core.state(run['id'])['papers'][0]
        self.assertEqual((completed['status'], completed['manual_state'], completed['manual_source']), ('downloaded', 'completed', 'manual-confirmation'))
        self.assertEqual(completed['verification'], 'MANUAL_CONFIRMED')

    def test_custom_save_dir_is_used_for_new_batches(self):
        target = Path(self.temp.name) / 'chosen-folder'
        core.set_save_dir(str(target))
        run = core.create_run('list.txt', b'10.1234/custom-save\n')
        self.assertEqual(Path(core.state(run['id'])['run']['save_dir']), target.resolve())
        paper = core.state(run['id'])['papers'][0]
        core.persist_pdf(paper, make_pdf('doi 10.1234/custom-save'), 'https://example.org/paper.pdf')
        self.assertTrue((target / run['id'] / 'pdf').exists())
        core.reset_save_dir()

    def test_title_lookup_can_run_while_download_is_active(self):
        run = core.create_run('list.txt', b'10.1234/title-one\n10.1234/title-two\n')
        entered = threading.Event()
        release = threading.Event()

        def metadata(paper):
            entered.set()
            release.wait(5)
            return ('Title ' + paper['doi'], 'https://example.org/article', [], [])

        def discover(paper):
            return paper['title'], 'https://example.org/landing', [('https://example.org/landing', 'test')], []

        def request(url, **kwargs):
            return Response(make_pdf('doi 10.1234/title-one'), url)

        with patch.object(core, 'metadata_for', side_effect=metadata), patch.object(core, 'discover', side_effect=discover), patch.object(core, 'request', side_effect=request):
            core.start_titles(run['id'])
            self.assertTrue(entered.wait(3))
            core.start(run['id'])
            core._download_worker.join(timeout=10)
            release.set()
            core._title_worker.join(timeout=10)
        self.assertFalse(core._download_worker.is_alive())
        self.assertFalse(core._title_worker.is_alive())
        self.assertEqual(core.state(run['id'])['papers'][0]['status'], 'downloaded')

    def test_pdf_validation_accepts_unmatched_and_rejects_html(self):
        run = core.create_run('list.txt', b'10.1234/example\n')
        paper = core.state(run['id'])['papers'][0]
        self.assertEqual(core.persist_pdf(paper, make_pdf('A different article'), 'https://example.org/secret?token=x'), 'downloaded')
        saved = core.state(run['id'])['papers'][0]
        self.assertEqual(saved['verification'], 'IDENTITY_UNCONFIRMED')
        self.assertIn('/pdf/', saved['path'])
        self.assertNotIn('待核验', saved['path'])
        self.assertNotIn('token', saved['source_url'])
        with self.assertRaises(ValueError): core.persist_pdf(paper, b'<html>login</html>', 'https://example.org')
        self.assertEqual(core.pdf_check(make_pdf('doi 10.1234/example2'), '10.1234/example', '')[0], 'IDENTITY_UNCONFIRMED')

    def test_automatic_queue_follows_pdf_metadata_and_keeps_failures_for_manual(self):
        run = core.create_run('list.csv', b'DOI,Title\n10.1234/good,My Article\n10.1234/bad,Other Article\n')
        def discover(p):
            return p['title'], 'https://example.org/landing', [('https://example.org/landing', 'test')], []
        def request(url, **kwargs):
            if url.endswith('/landing'):
                return Response(b'<html><meta name="citation_pdf_url" content="/article.pdf"></html>',url,content_type='text/html')
            return Response(make_pdf('doi:10.1234/good'),url)
        with patch.object(core,'discover',side_effect=discover),patch.object(core,'request',side_effect=request):
            core.start(run['id'])
            core._worker.join(timeout=10)
        self.assertFalse(core._worker.is_alive())
        papers = core.state(run['id'])['papers']
        self.assertEqual(papers[0]['status'], 'downloaded')
        self.assertEqual(papers[0]['verification'], 'DOI_MATCH')
        self.assertEqual(papers[1]['status'], 'downloaded')
        self.assertEqual(papers[1]['verification'], 'IDENTITY_UNCONFIRMED')
        self.assertTrue(Path(papers[0]['path']).is_file())
        out = openpyxl.load_workbook(io.BytesIO(core.export_xlsx(run['id'])),read_only=True)
        self.assertEqual(len(list(out.active.values)), 3)
        out.close()

    def test_public_url_rejects_loopback_and_userinfo(self):
        for url in ('http://127.0.0.1/private', 'http://198.18.0.14/private', 'http://user:password@example.org/x'):
            with self.assertRaises(ValueError): core.public_url(url)
        with patch.object(core.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('198.18.0.14', 443))]):
            self.assertEqual(core.public_url('https://example.org/'), 'https://example.org/')

    def test_import_actual_input_sheet(self):
        p = Path(__file__).resolve().parents[2] / '文献下载' / '833篇文献免费全文获取链接.xlsx'
        if not p.exists(): self.skipTest('项目样本表不可用')
        rows, invalid, duplicates = core.parse_input(p.name, p.read_bytes())
        self.assertEqual(len(rows), 833)
        self.assertEqual(duplicates, 0)
        self.assertTrue(rows[0][1].startswith('10.'))


if __name__ == '__main__': unittest.main()
