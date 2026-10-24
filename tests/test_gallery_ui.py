"""Local browser smoke tests. Serve only synthetic fixtures; never fetch live assets."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from PIL import Image
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class QuietHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        try:
            super().do_GET()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # Browser navigation can cancel an in-flight local fixture request.
    def log_message(self, *args): pass


@unittest.skipIf(sync_playwright is None,"Playwright is unavailable")
class GalleryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        try:
            cls.browser = cls.runtime.chromium.launch(headless=True)
        except Exception as error:
            cls.runtime.stop()
            raise unittest.SkipTest("Installed Playwright browser unavailable: " + str(error).splitlines()[0])
        cls.temp = tempfile.TemporaryDirectory()
        root=Path(cls.temp.name)
        repo=Path(__file__).resolve().parents[1]
        for name in ("index.html","detail.html","gallery.js"):
            shutil.copy2(repo/"docs"/name,root/name)
        fixture=[]
        definitions=[("风起","2.9","壁纸"),("灯火","2.10","影像档案"),
                     ("双版本","2.10","壁纸"),("未知素材","未分类","其他"),("新篇章","3.0","影像档案")]
        for i,(name,version,category) in enumerate(definitions):
            image=f"image-{i}.png"
            Image.new("RGB",(720,420 if i%2 else 540),["#425f87","#6862a0","#3c7c78","#8a6852","#375674"][i]).save(root/image)
            fixture.append({"identity":"sha256:"+str(i)*64,"name":name,"folder":"fixture","thumb":image,"full":image,
                            "versions":[version,"2.9"] if i==2 else [version],"categories":[category],
                            "posts":[{"post_id":str(i),"title":"北境纪事" if i==1 else name,"post_url":"https://example.com/posts/"+str(i)}]})
        (root/"meta.json").write_text(json.dumps({"items":fixture,"legacy_indices":{"9":"sha256:"+"1"*64}},ensure_ascii=False),encoding="utf-8")
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),functools.partial(QuietHandler,directory=str(root)))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
        cls.browser.close();cls.runtime.stop();cls.temp.cleanup()

    def setUp(self):
        self.page=self.browser.new_page(viewport={"width":1440,"height":1000})
        self.errors=[]
        self.page.on("pageerror",lambda error:self.errors.append(str(error)))
        self.page.route("**/*",lambda route:route.continue_() if route.request.url.startswith(self.base) else route.abort())
        self.page.goto(self.base)
        self.page.wait_for_function("document.querySelectorAll('.card').length === 5")
    def tearDown(self):
        self.page.close()
    def capture(self,name):
        directory=os.getenv("GALLERY_SCREENSHOT_DIR")
        if directory:
            path=Path(directory);path.mkdir(parents=True,exist_ok=True)
            self.page.locator(".card img").evaluate_all("async images => { await Promise.all(images.map(async image => { image.loading = 'eager'; try { await image.decode(); } catch {} })); }")
            self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            self.page.screenshot(path=str(path/name),full_page=True)

    def test_numeric_order_combined_filters_reset_and_detail_indices(self):
        page=self.page
        self.assertEqual(["","3.0","2.10","2.9","未分类"],page.locator("#versionFilter option").evaluate_all("nodes=>nodes.map(node=>node.value)"))
        page.select_option("#versionFilter","2.10")
        self.assertEqual(2,page.locator(".card").count())
        page.select_option("#categoryFilter","影像档案")
        page.fill("#searchFilter","北境")
        self.assertEqual(1,page.locator(".card").count())
        href=page.locator(".card a").get_attribute("href")
        self.assertIn("i=1",href)
        page.fill("#searchFilter","does-not-exist")
        self.assertEqual(0,page.locator(".card").count())
        self.assertTrue(page.locator(".empty").is_visible())
        page.click("#clearFilters")
        self.assertEqual(5,page.locator(".card").count())
        page.select_option("#versionFilter","2.10")
        self.capture("gallery-desktop.png")
        page.select_option("#categoryFilter","影像档案")
        page.locator(".card a").click()
        page.wait_for_function("document.querySelector('#meta').textContent.includes('灯火')")
        self.assertIn("2.10",page.locator("#meta").inner_text())
        self.assertIn("version=2.10",page.locator(".actions a").first.get_attribute("href"))
        page.locator(".actions a").first.click()
        page.wait_for_function("document.querySelectorAll('.card').length === 1")
        self.assertEqual([],self.errors)

    def test_legacy_detail_index_survives_content_deduplication(self):
        self.page.goto(self.base+"detail.html?i=9")
        self.page.wait_for_function("document.querySelector('#meta').textContent.includes('灯火')")
        self.assertIn("2.10",self.page.locator("#meta").inner_text())
        self.assertEqual([],self.errors)

    def test_mobile_layout_and_unknown_version(self):
        page=self.page;page.set_viewport_size({"width":390,"height":844})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),390)
        page.select_option("#versionFilter","未分类")
        self.assertEqual(1,page.locator(".card").count())
        self.assertIn("未知素材",page.locator(".card").inner_text())
        page.click("#clearFilters")
        self.capture("gallery-mobile.png")
        self.assertEqual([],self.errors)


if __name__ == "__main__":unittest.main()
