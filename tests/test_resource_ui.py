import functools
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright=None


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass


@unittest.skipIf(sync_playwright is None,'Playwright unavailable')
class ResourceBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();root=Path(cls.temp.name)
        source=Path(__file__).resolve().parents[1]/'docs'
        for name in ['resources.html','resources.js']:shutil.copyfile(source/name,root/name)
        def work(ident,title,category,tags):
            return {'wiki_id':ident,'title':title,'categories':[category],'tags':tags,'media_ids':[],
                    'wiki_url':'https://baike.mihoyo.com/zzz/wiki/content/'+ident+'/detail','links':[],
                    'resolution_status':'link_only','official_match':None}
        works=[work('1','洛克茜角色展示 <script>throw new Error("xss")</script>','角色图片',
                    [{'kind':'character','value':'洛克茜','label':'洛克茜','aliases':['洛克茜·伊芙莉塔·普莱斯']},
                     {'kind':'version','value':'2.10','label':'版本 2.10 · 测试版本','aliases':['测试版本']}]),
               work('2','2026年3月月历','月历',[{'kind':'version','value':'2.9','label':'版本 2.9 · 九号版本'}]),
               work('3','未知来源条目','未分类',[])]
        (root/'resources.json').write_text(json.dumps({'works':works,'media':{},'status':{'failures':[]}}),encoding='utf8')
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Quiet,directory=str(root)))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.runtime=sync_playwright().start()
        try:cls.browser=cls.runtime.chromium.launch(headless=True)
        except Exception:
            cls.runtime.stop();cls.server.shutdown();cls.temp.cleanup();raise unittest.SkipTest('Chromium unavailable')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.runtime.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()

    def setUp(self):
        self.page=self.browser.new_page(viewport={'width':1280,'height':900});self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.goto('http://127.0.0.1:'+str(self.server.server_port)+'/resources.html')
        self.page.wait_for_function('document.querySelectorAll("article").length===3')

    def tearDown(self):self.page.close()

    def capture(self,name):
        directory=os.getenv('GALLERY_SCREENSHOT_DIR')
        if directory:Path(directory).mkdir(parents=True,exist_ok=True);self.page.screenshot(path=str(Path(directory)/name),full_page=True)

    def test_combined_filters_chinese_alias_numeric_version_and_safe_titles(self):
        p=self.page
        self.assertEqual(['','2.10','2.9'],p.locator('#resourceVersion option').evaluate_all('(nodes)=>nodes.map(n=>n.value)'))
        p.select_option('#resourceCategory','角色图片');p.fill('#resourceSearch','洛克茜 2.10')
        self.assertEqual(1,p.locator('article').count());self.assertIn('<script>',p.locator('h2').inner_text())
        p.fill('#resourceSearch','洛克茜·伊芙莉塔·普莱斯 测试版本');self.assertEqual(1,p.locator('article').count())
        p.select_option('#resourceVersion','2.9');self.assertEqual(0,p.locator('article').count())
        p.click('#resourceClear');self.assertEqual(3,p.locator('article').count());self.assertEqual([],self.errors)
        self.capture('resources-desktop.png')

    def test_mobile_controls_unknown_category_and_no_horizontal_overflow(self):
        p=self.page;p.set_viewport_size({'width':390,'height':844})
        p.select_option('#resourceCategory','未分类');self.assertEqual(1,p.locator('article').count())
        self.assertLessEqual(p.evaluate('document.documentElement.scrollWidth'),390)
        self.assertEqual('https://baike.mihoyo.com/zzz/wiki/content/3/detail',p.get_by_text('Wiki 出处').get_attribute('href'))
        self.assertEqual([],self.errors);self.capture('resources-mobile.png')


if __name__=='__main__':unittest.main()
