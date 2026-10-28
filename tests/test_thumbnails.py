"""First-frame backfill, journal-only reuse, real extraction and newest previews."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from src.thumbnails import preview_order, update_thumbnails


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'video.mp4'
        self.video.write_bytes(b'local video')
        self.resource = {'id':'video-1','kind':'video','fingerprint':'version-1','label':'Wallpaper'}
        self.document = {'entries':{'1':{'title':'好感壁纸','category_ids':[99], 'resources':[
            {**self.resource,'url':'https://example.invalid/2026/10/01/one.mp4'}]}}}
        self.records = {'video-1':{'complete':True,'fingerprint':'version-1','sha256':'hash-one'}}
        self.stop = threading.Event()

    def extract(self, command, **kwargs):
        self.assertNotIn('-ss', command)
        self.assertNotIn('thumbnail=', ' '.join(command))
        self.assertEqual(command[command.index('-frames:v')+1], '1')
        Image.new('RGB',(320,180),'red').save(command[-1],format='JPEG')

    def run_update(self, locate=None, **kwargs):
        return update_thumbnails(self.document,[self.resource],self.root,self.records,
                                 locate or (lambda resource:self.video),self.stop,**kwargs)

    def test_backfill_once_then_reuse_without_touching_video_or_rewriting_index(self):
        with patch('src.thumbnails.shutil.which',return_value='ffmpeg'), \
                patch('src.thumbnails.subprocess.run',side_effect=self.extract) as process:
            self.assertEqual(self.run_update()['generated'],1)
            self.assertEqual(process.call_count,1)
        path = self.root/'thumbnails/index.json'
        before = path.read_bytes()
        with patch('src.thumbnails.subprocess.run',side_effect=AssertionError('decoded again')), \
                patch.object(Path,'is_file',side_effect=AssertionError('filesystem scanned')):
            self.assertEqual(self.run_update(locate=lambda r:self.fail('video inspected'))['generated'],0)
        self.assertEqual(before,path.read_bytes())

    def test_only_content_changes_generate_new_immutable_thumbnail(self):
        with patch('src.thumbnails.shutil.which',return_value='ffmpeg'), \
                patch('src.thumbnails.subprocess.run',side_effect=self.extract):
            self.run_update()
            first = json.loads((self.root/'thumbnails/index.json').read_text())['video-1']['filename']
            self.records['video-1']['sha256'] = 'replacement-content'
            self.assertEqual(self.run_update()['generated'],1)
        second = json.loads((self.root/'thumbnails/index.json').read_text())['video-1']['filename']
        self.assertNotEqual(first,second)

    def test_failed_or_incomplete_sources_do_not_publish_a_thumbnail(self):
        self.records['video-1']['complete'] = False
        self.assertEqual(self.run_update(locate=lambda r:self.fail('partial inspected'))['generated'],0)
        self.records['video-1']['complete'] = True
        with patch('src.thumbnails.shutil.which',return_value='ffmpeg'), \
                patch('src.thumbnails.subprocess.run',side_effect=subprocess.TimeoutExpired('ffmpeg',60)):
            self.assertEqual(self.run_update()['failed'],1)
        self.assertFalse((self.root/'thumbnails/index.json').exists())
        self.assertEqual(list((self.root/'thumbnails').glob('*.jpg')),[])

    def test_latest_upload_wins_with_source_order_as_tie_breaker(self):
        entry = {'resources':[{'id':'older','kind':'video','url':'https://example.invalid/2025/01/01/v.mp4'},
                              {'id':'latest','kind':'video','url':'https://example.invalid/2026/10/01/v.mp4'},
                              {'id':'same-day','kind':'video','url':'https://example.invalid/2026/10/01/z.mp4'}]}
        self.assertEqual([r['id'] for r in preview_order(entry)],['latest','same-day','older'])
        entry['resources'].insert(0,{'id':'new-no-date','kind':'video','url':'https://example.invalid/new.mp4'})
        self.assertEqual(preview_order(entry)[0]['id'],'new-no-date')

    @unittest.skipUnless(shutil.which('ffmpeg'),'Real extraction requires FFmpeg')
    def test_real_first_frame_extraction_including_short_clips(self):
        for duration in ('4','0.5'):
            with self.subTest(duration=duration):
                subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi',
                    '-i','testsrc2=size=320x180:rate=10','-t',duration,'-c:v','libx264',
                    '-pix_fmt','yuv420p',str(self.video)],check=True,timeout=20)
                self.records['video-1']['sha256'] = hashlib.sha256(self.video.read_bytes()).hexdigest()
                self.assertEqual(self.run_update()['generated'],1)
                item = json.loads((self.root/'thumbnails/index.json').read_text())['video-1']
                with Image.open(self.root/'thumbnails'/item['filename']) as image:
                    self.assertEqual(image.size,(320,180))
