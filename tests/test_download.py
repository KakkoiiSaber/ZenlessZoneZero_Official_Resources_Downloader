"""Real loopback HTTP transfers; no external services or full production media."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from src.download import (Downloader, State, canonical_url, cover_plan, exclusive_lock, fingerprint,
                          load_manifest, plan, select_resources, format_size, format_progress)


class MediaHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def handle(self):
        try:
            super().handle()
        except OSError:
            pass  # Tests deliberately close transfers early.

    def do_HEAD(self):
        self.respond(head=True)

    def do_GET(self):
        self.respond()

    def respond(self, head=False):
        server = self.server
        server.calls.append((self.command, self.headers.get('Range'), self.headers.get('If-Range')))
        body, start, status = server.body, 0, 200
        requested = self.headers.get('Range')
        if requested and not server.ignore_range and self.headers.get('If-Range') == server.etag:
            start = int(requested.removeprefix('bytes=').removesuffix('-'))
            status = 416 if start >= len(body) else 206
        self.send_response(status)
        if server.etag:
            self.send_header('ETag', server.etag)
        self.send_header('Content-Type', server.content_type)
        if status == 416:
            self.send_header('Content-Range', f'bytes */{len(body)}')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if status == 206:
            range_start = start + 1 if server.bad_range else start
            self.send_header('Content-Range', f'bytes {range_start}-{len(body)-1}/{len(body)}')
        self.send_header('Content-Length', str(len(body) - start))
        self.end_headers()
        if head:
            return
        try:
            if server.interrupt:
                server.interrupt = False
                self.wfile.write(body[start:start + 600000])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
            else:
                self.wfile.write(body[start:])
        except (BrokenPipeError, ConnectionResetError):
            pass


def manifest(url):
    return {'schema_version': 2, 'source': 'wiki', 'entries': {'1': {
        'folders': [['图片', '测试']], 'resources': [
            {'id': 'wiki:1:image:1', 'kind': 'image', 'url': url, 'filename': '001.jpg', 'folder': 0}
        ]}}}


class ProgressDisplayTests(unittest.TestCase):
    def test_sizes_use_automatic_decimal_units(self):
        for size, expected in [(0, '0 B'), (999, '999 B'), (1000, '1.00 KB'),
                               (1_000_000, '1.00 MB'), (1_500_000_000, '1.50 GB'),
                               (1_000_000_000_000, '1.00 TB')]:
            with self.subTest(size=size):
                self.assertEqual(format_size(size), expected)

    def test_percent_unknown_total_and_rounding_before_completion(self):
        self.assertEqual(format_progress(500_000, 2_000_000), '500.00 KB / 2.00 MB (25.0%)')
        self.assertEqual(format_progress(500_000), '500.00 KB (total unknown)')
        self.assertEqual(format_progress(999_999, 1_000_000), '1.00 MB / 1.00 MB (99.9%)')
        self.assertEqual(format_progress(1_000_000, 1_000_000), '1.00 MB / 1.00 MB (100.0%)')


class DownloadTests(unittest.TestCase):
    def test_cover_cache_downloads_once_without_changing_media_plan(self):
        document = deepcopy(self.doc)
        document['entries']['1'].update(title='视频主题', cover_url=self.url + '/cover.jpg')
        identity = lambda doc: [(r['id'], r['paths'], r['fingerprint']) for r in plan(doc)]
        self.assertEqual(identity(document), identity(self.doc))
        covers = cover_plan(document)
        self.assertEqual(len(covers), 1)
        self.assertEqual(cover_plan(document, ['unknown-id']), [])
        root = self.state_dir / 'covers'
        state = State(root)
        worker = Downloader(root, state, retries=1)
        self.addCleanup(state.close)
        self.addCleanup(worker.http.close)
        self.assertEqual(worker.run(covers)['downloaded'], 1)
        calls = len(self.server.calls)
        self.assertEqual(worker.run(covers, quiet_skips=True)['skipped'], 1)
        self.assertEqual(len(self.server.calls), calls)
        self.assertEqual((root / covers[0]['paths'][0]).read_bytes(), self.server.body)
        self.assertFalse((self.root / covers[0]['paths'][0]).exists())

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), MediaHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/media'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'files'
        self.state_dir = Path(self.temp.name) / 'state'
        self.state = State(self.state_dir)
        self.addCleanup(self.state.close)
        self.worker = Downloader(self.root, self.state, retries=1)
        self.addCleanup(self.worker.http.close)
        self.server.body = b'abcdef0123456789' * 80000
        self.server.etag = '"version1"'
        self.server.calls = []
        self.server.ignore_range = self.server.bad_range = self.server.interrupt = False
        self.server.content_type = 'image/jpeg'
        self.doc = manifest(self.url)
        self.resources = plan(self.doc)
        self.r = self.resources[0]

    def target(self):
        return self.root / self.r['paths'][0]

    def seed_partial(self, size):
        part = self.worker.partial(self.r)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(self.server.body[:size])
        self.state.save(self.r['id'], {'fingerprint': self.r['fingerprint'], 'complete': False,
                                      'etag': self.server.etag, 'total': len(self.server.body)})
        return part

    def test_download_then_skip_without_http_even_after_recreating_worker(self):
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertEqual(self.target().read_bytes(), self.server.body)
        self.server.calls.clear()
        other = Downloader(self.root, self.state, retries=1)
        self.addCleanup(other.http.close)
        self.assertEqual(other.run(self.resources)['skipped'], 1)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(len(self.state.get(self.r['id'])['sha256']), 64)

    def test_real_interruption_then_resume(self):
        self.server.interrupt = True
        self.assertEqual(self.worker.run(self.resources)['failed'], 1)
        offset = self.worker.partial(self.r).stat().st_size
        self.assertGreater(offset, 0)
        self.assertFalse(self.target().exists())
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertIn(('GET', f'bytes={offset}-', '"version1"'), self.server.calls)
        self.assertEqual(self.target().read_bytes(), self.server.body)

    def test_resume_and_completion_logs_show_size_and_percentage(self):
        self.seed_partial(262144)
        with self.assertLogs('download', level='INFO') as logs:
            self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        output = '\n'.join(logs.output)
        self.assertIn('validated HTTP 206; continuing at 262.14 KB / 1.28 MB (20.5%)', output)
        self.assertIn('downloaded 测试 / 001.jpg 1.28 MB / 1.28 MB (100.0%)', output)

    def test_logs_use_theme_section_and_filename_instead_of_resource_ids(self):
        self.doc['entries']['1']['title'] = '3.2 剧情壁纸'
        self.doc['entries']['1']['folders'][0].append('章节一')
        resources = plan(self.doc)
        with self.assertLogs('download', level='INFO') as logs:
            self.assertEqual(self.worker.run(resources)['downloaded'], 1)
        output = '\n'.join(logs.output)
        self.assertIn('3.2 剧情壁纸 / 章节一 / 001.jpg start at', output)
        self.assertIn('downloaded 3.2 剧情壁纸 / 章节一 / 001.jpg', output)
        self.assertNotIn(self.r['id'], output)
        # Failure/retry messages must identify the same readable theme too.
        (self.root / resources[0]['paths'][0]).unlink()
        self.server.content_type = 'text/html'
        with self.assertLogs('download', level='INFO') as errors:
            self.assertEqual(self.worker.run(resources)['failed'], 1)
        self.assertIn('3.2 剧情壁纸 / 章节一 / 001.jpg attempt 1/1', '\n'.join(errors.output))

    def test_display_title_changes_do_not_redownload_or_change_resource_identity(self):
        self.worker.run(self.resources)
        self.server.calls.clear()
        self.doc['entries']['1']['title'] = '新的主题\n名称'
        resources = plan(self.doc)
        self.assertEqual(resources[0]['fingerprint'], self.r['fingerprint'])
        self.assertEqual(resources[0]['id'], self.r['id'])
        with self.assertLogs('download', level='INFO') as logs:
            self.assertEqual(self.worker.run(resources)['skipped'], 1)
        self.assertIn('skipped 新的主题 名称 / 001.jpg', '\n'.join(logs.output))
        self.assertEqual(self.server.calls, [])

    def test_ignored_range_restarts_instead_of_appending(self):
        self.seed_partial(100)
        self.server.ignore_range = True
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertEqual(self.target().read_bytes(), self.server.body)

    def test_changed_etag_restarts_instead_of_mixing_versions(self):
        self.seed_partial(100)
        self.server.etag = '"version2"'
        self.server.body = b'newversion' * 100000
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertEqual(self.target().read_bytes(), self.server.body)

    def test_bad_range_rejected_then_clean_retry(self):
        part = self.seed_partial(100)
        self.server.bad_range = True
        self.assertEqual(self.worker.run(self.resources)['failed'], 1)
        self.assertFalse(part.exists())
        self.server.bad_range = False
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)

    def test_416_completed_partial_is_finalized(self):
        self.seed_partial(len(self.server.body))
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertEqual(self.target().read_bytes(), self.server.body)

    def test_416_wrong_size_restarts(self):
        part = self.seed_partial(len(self.server.body))
        with part.open('ab') as handle:
            handle.write(b'bad')
        self.assertEqual(self.worker.run(self.resources)['failed'], 1)
        self.assertFalse(part.exists())

    def test_no_strong_validator_restarts(self):
        self.seed_partial(100)
        record = self.state.get(self.r['id'])
        record['etag'] = 'W/"weak"'
        self.state.save(self.r['id'], record)
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertEqual(self.server.calls[0], ('GET', None, None))

    def test_wrong_manifest_size_never_publishes(self):
        self.r['size_bytes'] = 5
        self.assertEqual(self.worker.run(self.resources)['failed'], 1)
        self.assertFalse(self.target().exists())

    def test_html_error_never_publishes(self):
        self.server.content_type = 'text/html'
        self.assertEqual(self.worker.run(self.resources)['failed'], 1)
        self.assertFalse(self.target().exists())

    def test_expired_url_does_not_download(self):
        self.r['expires_at'] = '2000-01-01T00:00:00+00:00'
        self.assertEqual(self.worker.run(self.resources)['failed'], 1)
        self.assertEqual(self.server.calls, [])

    def test_missing_file_downloads_again(self):
        self.worker.run(self.resources)
        self.target().unlink()
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)

    def test_signature_rotation_is_not_new_content(self):
        one = {'url': 'https://prod-vod-sign.miyoushe.com/foo?auth_key=one'}
        two = {'url': 'https://prod-vod-sign.miyoushe.com/foo?auth_key=two'}
        self.assertEqual(fingerprint(one), fingerprint(two))
        self.assertNotEqual(canonical_url(self.url + '?version=1'), canonical_url(self.url + '?version=2'))
        self.assertNotEqual(canonical_url(self.url + '?auth_key=1'), canonical_url(self.url + '?auth_key=2'))

    def test_small_test_selection_rejects_unknown_id_and_preserves_requested_order(self):
        other = {**self.r, 'id': 'other'}
        self.assertEqual(select_resources([self.r, other], ['other', self.r['id'], 'other']), [other, self.r])
        with self.assertRaises(ValueError):
            select_resources(self.resources, ['not-in-manifest'])

    def test_throttled_stop_keeps_partial_and_next_start_resumes(self):
        self.worker.rate_limit = 256 * 1024
        timer = threading.Timer(.15, self.worker.stop.set)
        timer.start()
        try:
            self.worker.run(self.resources)
        finally:
            timer.cancel()
        offset = self.worker.partial(self.r).stat().st_size
        self.assertGreater(offset, 0)
        self.assertFalse(self.target().exists())
        self.assertFalse(self.state.get(self.r['id'])['complete'])
        self.worker.stop.clear()
        self.worker.rate_limit = 0
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertIn(('GET', f'bytes={offset}-', '"version1"'), self.server.calls)
        self.assertEqual(self.target().read_bytes(), self.server.body)

    def test_directory_move_without_http(self):
        self.worker.run(self.resources)
        old = self.target()
        self.r['paths'] = ['新目录/壁纸.jpg']
        self.server.calls.clear()
        self.assertEqual(self.worker.run(self.resources)['skipped'], 1)
        self.assertFalse(old.exists())
        self.assertEqual(self.target().read_bytes(), self.server.body)
        self.assertEqual(self.server.calls, [])

    def test_multiple_placements_and_repair_missing_copy(self):
        self.r['paths'].append('其他目录/001.jpg')
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        second = self.root / self.r['paths'][1]
        second.unlink()
        self.server.calls.clear()
        self.assertEqual(self.worker.run(self.resources)['skipped'], 1)
        self.assertEqual(second.read_bytes(), self.server.body)
        self.assertEqual(self.server.calls, [])

    def test_periodic_check_repairs_corrupt_secondary_placement(self):
        self.r['paths'].append('其他目录/001.jpg')
        self.worker.run(self.resources)
        second = self.root / self.r['paths'][1]
        second.write_bytes(b'x' * len(self.server.body))
        self.worker.verify_seconds = 0
        self.assertEqual(self.worker.run(self.resources)['skipped'], 1)
        self.assertEqual(second.read_bytes(), self.server.body)
        self.assertFalse(any(method == 'GET' for method, _, _ in self.server.calls[1:]))

    def test_unchanged_multiple_placements_are_not_rewritten(self):
        self.r['paths'].append('其他目录/001.jpg')
        self.worker.run(self.resources)
        second = self.root / self.r['paths'][1]
        timestamp = second.stat().st_mtime_ns
        self.worker.run(self.resources)
        self.assertEqual(second.stat().st_mtime_ns, timestamp)

    def test_unmanaged_file_never_overwritten(self):
        self.target().parent.mkdir(parents=True)
        self.target().write_bytes(b'personal')
        self.assertEqual(self.worker.run(self.resources)['failed'], 1)
        self.assertEqual(self.target().read_bytes(), b'personal')

    def test_changed_source_id_replaces_only_managed_destination(self):
        self.worker.run(self.resources)
        self.r['id'] = 'wiki:1:video:official:2'
        self.server.body = b'new official version'
        self.server.etag = '"new"'
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertEqual(self.target().read_bytes(), self.server.body)
        self.assertEqual(self.state.owner(self.r['paths'][0]), self.r['id'])

    def test_periodic_check_detects_remote_change(self):
        self.worker.run(self.resources)
        self.worker.verify_seconds = 0
        self.server.etag = '"changed"'
        self.server.body = b'new bytes'
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertEqual(self.target().read_bytes(), self.server.body)
        self.assertTrue(any(method == 'HEAD' for method, _, _ in self.server.calls))

    def test_same_size_remote_update_replaces_existing_bytes(self):
        self.worker.run(self.resources)
        self.worker.verify_seconds = 0
        self.server.etag = '"changed"'
        self.server.body = b'n' * len(self.server.body)
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertEqual(self.target().read_bytes(), self.server.body)

    def test_periodic_hash_detects_same_size_corruption(self):
        self.worker.run(self.resources)
        self.target().write_bytes(b'x' * len(self.server.body))
        self.worker.verify_seconds = 0
        self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertEqual(self.target().read_bytes(), self.server.body)

    def test_manifest_rejects_traversal_collision_partial_and_bad_index(self):
        for mutate in [
            lambda d: d['entries']['1']['folders'][0].append('..'),
            lambda d: d['entries']['1']['resources'][0].update(filename='C:\\outside.jpg'),
            lambda d: d.update(status='partial'),
            lambda d: d['entries']['1']['resources'][0].update(folder=-1),
            lambda d: d['entries']['1']['resources'].append({**d['entries']['1']['resources'][0], 'id': 'other'}),
        ]:
            with self.subTest(mutate=mutate):
                value = deepcopy(self.doc)
                mutate(value)
                with self.assertRaises(ValueError):
                    plan(value)

    def test_manifest_local_load_and_current_real_schema(self):
        local = Path(self.temp.name) / 'wiki.json'
        local.write_text(json.dumps(self.doc), encoding='utf8')
        self.assertEqual(len(plan(load_manifest('', local))), 1)
        self.assertGreater(len(plan(json.loads(Path('metafiles/wiki.json').read_text(encoding='utf8')))), 3000)

    def test_manifest_fetch_reports_network_stages_without_exposing_url(self):
        self.server.body = json.dumps(self.doc).encode()
        self.server.content_type = 'application/json'
        with self.assertLogs('download', level='INFO') as logs:
            self.assertEqual(load_manifest(self.url), self.doc)
        output = '\n'.join(logs.output)
        self.assertIn('Fetching download manifest', output)
        self.assertIn('Manifest response received; reading body', output)
        self.assertIn('Manifest received:', output)
        self.assertNotIn(self.url, output)

    def test_cli_exits_by_default_and_persists_state_across_processes(self):
        local = Path(self.temp.name) / 'wiki.json'
        local.write_text(json.dumps(self.doc), encoding='utf8')
        command = [sys.executable, '-m', 'src.download', '--manifest-file', str(local),
                   '--state-dir', str(self.state_dir), '--download-dir', str(self.root)]
        first = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertRegex(first.stderr, r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\[INFO\]')
        self.assertLess(first.stderr.index('Downloader started'), first.stderr.index('Reading local manifest'))
        self.assertLess(first.stderr.index('Manifest ready:'), first.stderr.index('start at'))
        self.server.calls.clear()
        second = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('skipped', second.stderr)
        self.assertEqual(self.server.calls, [])
        saved_log = (self.state_dir / 'logs/downloader.log').read_text(encoding='utf8')
        self.assertIn('Download run started; persistent log enabled', saved_log)
        self.assertIn('downloaded 测试 / 001.jpg', saved_log)
        self.assertIn('skipped 测试 / 001.jpg', saved_log)
        self.assertEqual(saved_log.count('Download run started; persistent log enabled'), 2)

    def test_progress_interval_reduces_intermediate_logs_and_zero_keeps_completion(self):
        from unittest.mock import patch
        self.worker.progress_interval = 60
        self.worker.rate_limit = 256 * 1024
        # Advance the logging clock deterministically; do not actually wait a minute.
        with patch('src.download.time.monotonic', side_effect=range(0, 10000, 10)), \
                patch.object(self.worker.stop, 'wait', return_value=False), \
                self.assertLogs('download', level='INFO') as logs:
            self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertEqual(sum(' progress ' in line for line in logs.output), 1)
        self.target().unlink()
        self.worker.progress_interval = 0
        self.worker.rate_limit = 0
        with self.assertLogs('download', level='INFO') as quiet:
            self.assertEqual(self.worker.run(self.resources)['downloaded'], 1)
        self.assertFalse(any(' progress ' in line for line in quiet.output))
        self.assertTrue(any('100.0%' in line and 'downloaded ' in line for line in quiet.output))

    def test_exclusive_state_lock(self):
        with exclusive_lock(self.state_dir):
            with self.assertRaises(RuntimeError):
                with exclusive_lock(self.state_dir):
                    self.fail('Second process acquired lock')


if __name__ == '__main__':
    unittest.main()
