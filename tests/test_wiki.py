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
from unittest.mock import patch

import requests
from PIL import Image

from src.download import cover_plan, digest, plan, update_preview_sizes
from src.image_info import read_image_size
from src.wiki import Catalog, Handler, wallpaper_variants


class WikiTests(unittest.TestCase):
    def test_wallpaper_ratios_merge_only_matching_image_themes(self):
        def group(name, ident, kind='image'):
            return {'name':name, 'resources':[{'id':ident,'kind':kind,'url':'/media/'+ident}]}
        original = [group('Roxy (2560x1080)','wide'), group('Roxy（3840×2160）','normal'),
                    group('Roxy EP (3840x2160)','ep'), group('Roxy EP (2560x1080)','ep-wide'),
                    group('Another theme','other'), group('Film (3840x2160)','video','video')]
        before = json.dumps(original)
        merged = wallpaper_variants(original)
        self.assertEqual(['Roxy','Roxy EP','Another theme','Film (3840x2160)'], [g['name'] for g in merged])
        self.assertEqual(['16:9','21:9'], [v['label'] for v in merged[0]['variants']])
        self.assertEqual(['3840x2160','2560x1080'], [v['id'] for v in merged[0]['variants']])
        self.assertEqual(['wide','normal'], [r['id'] for r in merged[0]['resources']])
        self.assertEqual(before, json.dumps(original))
        self.assertEqual(original[2:3], wallpaper_variants(original[2:3]))

    def test_catalog_groups_ratios_without_changing_download_paths(self):
        document = json.loads(self.manifest.read_text(encoding='utf8'))
        entry = document['entries']['1']
        entry['folders'] = [['wallpapers','theme','Roxy (3840x2160)'], ['wallpapers','theme','Roxy (2560x1080)']]
        entry['resources'] = [{**entry['resources'][0], 'id':f'image-{i}', 'folder':i} for i in range(2)]
        self.manifest.write_text(json.dumps(document),encoding='utf8')
        catalog = requests.get(self.url+'/api/catalog',timeout=5).json()
        groups = catalog['entries'][0]['groups']
        self.assertEqual(['Roxy'], [g['name'] for g in groups])
        self.assertEqual(2,len(groups[0]['variants']))
        self.assertEqual(['3840x2160','2560x1080'],[r['variant'] for r in groups[0]['resources']])
        self.assertEqual(document,json.loads(self.manifest.read_text(encoding='utf8')))

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

    def test_debug_mode_is_opt_in_and_preserves_browsing_data(self):
        normal = requests.get(self.url + '/api/catalog', timeout=5).json()
        self.assertIs(normal['debug'], False)
        debug_catalog = Catalog(self.manifest, self.media, debug=True)
        debug, _ = debug_catalog.read()
        self.assertIs(debug['debug'], True)
        self.assertEqual(normal['entries'], debug['entries'])

    def test_preview_dimensions_follow_local_image_ratio_and_file_updates(self):
        path = self.finish_file()
        Image.new('RGB', (30, 90)).save(path, format='PNG')
        first = requests.get(self.url + '/api/catalog', timeout=5).json()
        self.assertEqual(first['entries'][0]['preview_size'], [30, 90])
        Image.new('RGB', (90, 30)).save(path, format='PNG')
        second = requests.get(self.url + '/api/catalog', timeout=5).json()
        self.assertEqual(second['entries'][0]['preview_size'], [90, 30])

    def test_exif_rotation_and_invalid_images_do_not_break_catalog(self):
        path = self.finish_file()
        self.assertIsNone(self.catalog.image_size(path))
        exif = Image.Exif()
        exif[274] = 6
        Image.new('RGB', (30, 90)).save(path, format='JPEG', exif=exif)
        self.assertEqual(self.catalog.image_size(path), [90, 30])

    def test_png_dimensions_do_not_decode_pixel_data(self):
        path = self.finish_file()
        Image.new('RGB', (30, 90)).save(path, format='PNG')
        with patch.object(Image.Image, 'load', side_effect=AssertionError('pixel decoding forbidden')):
            self.assertEqual(read_image_size(path), [30, 90])

    def test_persisted_sizes_skip_reopening_images_after_server_restart(self):
        path = self.finish_file()
        Image.new('RGB', (30, 90)).save(path, format='PNG')
        state = self.root / 'state'
        resources = plan(self.document)
        update_preview_sizes(self.document, resources, [], self.media, state, threading.Event())
        before = (state / 'image-sizes.json').read_bytes()
        with patch('src.download.read_image_size', side_effect=AssertionError('unchanged image reopened')):
            update_preview_sizes(self.document, resources, [], self.media, state, threading.Event())
        self.assertEqual(before, (state / 'image-sizes.json').read_bytes())
        fresh = Catalog(self.manifest, self.media, covers=state / 'covers')
        with patch('src.wiki.read_image_size', side_effect=AssertionError('cache not reused')):
            self.assertEqual(fresh.read()[0]['entries'][0]['preview_size'], [30, 90])

    def test_source_dimensions_are_used_without_reading_image(self):
        self.finish_file()
        self.document['entries']['1']['resources'][0].update(width=30, height=90)
        self.manifest.write_text(json.dumps(self.document), encoding='utf-8')
        with patch('src.wiki.read_image_size', side_effect=AssertionError('source size ignored')):
            self.assertEqual(self.catalog.read()[0]['entries'][0]['preview_size'], [30, 90])

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

    def test_local_cover_is_served_without_exposing_cache_database_or_remote_url(self):
        self.document['entries']['1']['cover_url'] = 'https://example.invalid/cover.jpg'
        self.manifest.write_text(json.dumps(self.document), encoding='utf-8')
        cover_root = self.root / 'covers'
        cover_root.mkdir()
        self.catalog.cover_root = cover_root
        response = requests.get(self.url + '/api/catalog', timeout=5).json()
        self.assertNotIn('cover', response['entries'][0])
        resource = cover_plan(self.document)[0]
        (cover_root / resource['paths'][0]).write_bytes(b'local-image')
        (cover_root / 'downloads.sqlite3').write_bytes(b'private')
        response = requests.get(self.url + '/api/catalog', timeout=5)
        self.assertNotIn('example.invalid', response.text)
        url = response.json()['entries'][0]['cover']
        self.assertEqual(requests.get(self.url + url, timeout=5).content, b'local-image')
        self.assertEqual(requests.get(self.url + '/covers/downloads.sqlite3', timeout=5).status_code, 404)

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
