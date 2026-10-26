"""Offline HTTP checks for playback, local catalog refresh and media isolation."""
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import subprocess
import sys

import requests

from src.download import digest
from src.wiki import Catalog, Handler


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / 'downloads'
        self.media.mkdir()
        self.manifest = self.root / 'wiki.json'
        self.document = {'schema_version': 2, 'source': 'wiki', 'categories': {'98': '壁纸收录'},
                         'entries': {'1': {'title': '主题 <script>', 'category_ids': [98],
                           'folders': [['壁纸收录', '主题', '剧情壁纸']], 'resources': [
                            {'id': 'image-1', 'kind': 'image', 'filename': '001.jpg', 'folder': 0,
                             'url': 'https://example.invalid/private?auth_key=secret'}]}}}
        self.manifest.write_text(json.dumps(self.document), encoding='utf-8')
        self.catalog = Catalog(self.manifest, self.media)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, catalog=self.catalog))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def finish_file(self):
        path = self.media / '壁纸收录' / '主题' / '剧情壁纸' / '001.jpg'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'0123456789')
        return path

    def test_catalog_updates_when_file_finishes_and_omits_remote_urls(self):
        first = requests.get(self.url + '/api/catalog', timeout=5)
        self.assertNotIn('example.invalid', first.text)
        self.assertNotIn('secret', first.text)
        resource = first.json()['entries'][0]['groups'][0]['resources'][0]
        self.assertFalse(resource['available'])
        self.finish_file()
        second = requests.get(self.url + '/api/catalog', timeout=5).json()
        self.assertTrue(second['entries'][0]['groups'][0]['resources'][0]['available'])

    def test_video_style_ranges_and_head(self):
        self.finish_file()
        url = self.url + '/media/' + digest('image-1')
        for value, body, expected in [('bytes=2-5', b'2345', 'bytes 2-5/10'),
                                      ('bytes=7-', b'789', 'bytes 7-9/10'),
                                      ('bytes=-3', b'789', 'bytes 7-9/10')]:
            response = requests.get(url, headers={'Range': value}, timeout=5)
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, body)
            self.assertEqual(response.headers['Content-Range'], expected)
        head = requests.head(url, timeout=5)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b'')
        self.assertEqual(head.headers['Content-Length'], '10')
        self.assertEqual(requests.get(url, headers={'Range': 'bytes=11-'}, timeout=5).status_code, 416)

    def test_only_manifest_media_and_static_assets_are_exposed(self):
        self.finish_file()
        (self.root / 'secret.txt').write_text('private', encoding='utf-8')
        for route in ['/state/downloader.sqlite', '/.env', '/media/secret.txt', '/%2e%2e/secret.txt']:
            self.assertEqual(requests.get(self.url + route, timeout=5).status_code, 404)
        home = requests.get(self.url, timeout=5)
        self.assertEqual(home.status_code, 200)
        self.assertIn("default-src 'self'", home.headers['Content-Security-Policy'])

    def test_partial_is_never_available(self):
        partials = self.media / '.zzz-partials'
        partials.mkdir()
        (partials / (digest('image-1') + '.part')).write_bytes(b'partial')
        response = requests.get(self.url + '/media/' + digest('image-1'), timeout=5)
        self.assertEqual(response.status_code, 404)

    def test_git_manifest_update_is_visible_without_restart(self):
        self.catalog.read()
        self.document['entries']['1']['title'] = 'Updated topic'
        temporary = self.root / 'new.json'
        temporary.write_text(json.dumps(self.document), encoding='utf-8')
        temporary.replace(self.manifest)
        response = requests.get(self.url + '/api/catalog', timeout=5)
        self.assertEqual(response.json()['entries'][0]['title'], 'Updated topic')

    def test_downloader_snapshot_appears_without_git_sync(self):
        snapshot = self.root / 'state-wiki.json'
        catalog = Catalog(self.manifest, self.media, snapshot)
        self.assertEqual(catalog.read()[0]['entries'][0]['title'], '主题 <script>')
        self.document['entries']['1']['title'] = 'New download catalog'
        snapshot.write_text(json.dumps(self.document), encoding='utf-8')
        self.assertEqual(catalog.read()[0]['entries'][0]['title'], 'New download catalog')

    def test_download_run_persists_validated_snapshot(self):
        document = {'schema_version': 2, 'source': 'wiki', 'entries': {}}
        self.manifest.write_text(json.dumps(document), encoding='utf-8')
        state = self.root / 'state'
        result = subprocess.run([sys.executable, '-m', 'src.download', '--manifest-file',
            str(self.manifest), '--state-dir', str(state), '--download-dir', str(self.media)],
            capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(json.loads((state / 'wiki.json').read_text(encoding='utf-8')), document)
