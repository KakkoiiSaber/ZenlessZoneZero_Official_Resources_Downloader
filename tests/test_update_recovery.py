import asyncio
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile
import requests

from src.downloader import MinasDownloader as downloader
from src.downloader import asset_io
from tools import checkpoint_update, publish_pages


class ZipRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_finishes_abort_before_return_and_filters_headers(self):
        flags = []
        request = Mock(url="https://minas.example/seafhttp/zip/once")
        request.all_headers = AsyncMock(return_value={"cookie":"secret", "referer":"https://minas.example/d/fixture/", "accept-encoding":"gzip"})
        route = Mock(request=request)
        async def abort():
            await asyncio.sleep(.01)
            flags.append("aborted")
        route.abort = abort
        page = Mock()
        async def register(pattern, handler): page.handler = handler
        page.route = register
        page.unroute = AsyncMock()
        button = Mock()
        async def click(**kwargs): asyncio.create_task(page.handler(route))
        button.click = click
        url, headers = await downloader.capture_zip_request(page,button,"https://minas.example/d/fixture/")
        self.assertEqual(["aborted"], flags)
        self.assertEqual(request.url,url)
        self.assertEqual({"referer":"https://minas.example/d/fixture/"},headers)

    async def test_capture_rejects_cross_origin_before_transfer(self):
        request=Mock(url="https://other.example/seafhttp/zip/once")
        route=Mock(request=request,abort=AsyncMock())
        page=Mock(unroute=AsyncMock())
        async def register(pattern,handler):page.handler=handler
        page.route=register
        button=Mock()
        async def click(**kwargs):await page.handler(route)
        button.click=click
        with self.assertRaisesRegex(ValueError,"Cross-origin"):
            await downloader.capture_zip_request(page,button,"https://minas.example/d/fixture/")
        route.abort.assert_awaited_once()

    async def test_one_use_url_is_never_consumed_by_browser(self):
        from playwright.async_api import async_playwright
        consumed=[]
        async with async_playwright() as runtime:
            try:browser=await runtime.chromium.launch(headless=True)
            except Exception as error:raise unittest.SkipTest(str(error).splitlines()[0])
            try:
                context=await browser.new_context()
                async def network(route):
                    if route.request.url=="https://minas.example/d/fixture/":
                        await route.fulfill(content_type="text/html",body='<button onclick="location.href=\'/seafhttp/zip/once\'">ZIP</button>')
                    else:
                        consumed.append(route.request.url)
                        await route.abort()
                await context.route("**/*",network)
                page=await context.new_page();await page.goto("https://minas.example/d/fixture/")
                url,_=await downloader.capture_zip_request(page,page.locator("button"),page.url)
                self.assertEqual([],consumed)
                try:
                    await page.goto(url)  # A browser retry must remain blocked too.
                except Exception:
                    pass
                self.assertEqual([],consumed)
                data=io.BytesIO()
                with zipfile.ZipFile(data,"w") as archive:archive.writestr("asset.txt",b"complete")
                def receive(*args,**kwargs):
                    if consumed:raise requests.HTTPError("one-use URL already consumed")
                    consumed.append(url)
                    response=Mock(headers={"Content-Length":str(len(data.getvalue()))})
                    response.iter_content.return_value=iter([data.getvalue()]);response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
                    return response
                with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{"MINAS_MIN_FREE_BYTES":"1"}):
                    session=Mock();session.get.side_effect=receive
                    path=Path(temp)/"archive.zip";asset_io.download_zip(session,url,path)
                    stage=Path(temp)/"stage";stage.mkdir();asset_io.extract_zip(path,stage)
                    self.assertEqual(b"complete",(stage/"asset.txt").read_bytes())
                self.assertEqual([url],consumed)
            finally:await browser.close()


class PartialBatchTests(unittest.TestCase):
    def test_success_survives_failure_and_retry_skips_completed_row(self):
        with tempfile.TemporaryDirectory() as temp:
            prior=Path.cwd();os.chdir(temp)
            try:
                rows=[{"post_name":name,"post_time":"2025-01-01","minas_link":"https://minas.example/d/fixture/","minas_pwd":"fixture-private"} for name in ("good","bad")]
                with Path("rows.csv").open("w",newline="") as stream:
                    writer=csv.DictWriter(stream,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
                async def fetch(url,password,path,out_dir,force_extract_dir=None):
                    if force_extract_dir.name.endswith("bad"):raise requests.HTTPError("private-token",response=Mock(status_code=403))
                    force_extract_dir.mkdir(parents=True,exist_ok=True);(force_extract_dir/"a.jpg").write_bytes(b"complete")
                with patch.object(downloader,"STATE_FILE",Path("metafiles/state.json")),patch.object(downloader,"REPORT_FILE",Path("metafiles/report.json")),patch.object(downloader,"_zip_current_path",side_effect=fetch):
                    with self.assertRaises(RuntimeError):downloader.run_batch_from_csv(Path("rows.csv"),Path("downloads"))
                    state=json.loads(Path("metafiles/state.json").read_text());self.assertEqual(["2025.01.01 good"],list(state))
                    raw=Path("metafiles/report.json").read_text();report=json.loads(raw)
                    self.assertEqual("partial_failure",report["status"]);self.assertEqual(1,report["zip"]["completed"]);self.assertEqual(1,report["zip"]["failed"])
                    self.assertNotIn("fixture-private",raw);self.assertNotIn("private-token",raw)
                    with patch.object(downloader,"_zip_current_path",new_callable=AsyncMock) as retry:
                        result=downloader.run_batch_from_csv(Path("rows.csv"),Path("downloads"))
                        retry.assert_awaited_once();self.assertEqual(1,result["zip"]["unchanged"])
                        self.assertEqual("completed",result["status"])
            finally:os.chdir(prior)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.repo=self.root/"work";self.remote=self.root/"remote.git"
        subprocess.run(["git","init","--bare",str(self.remote)],check=True,capture_output=True)
        subprocess.run(["git","init","-b","main",str(self.repo)],check=True,capture_output=True)
        for key,value in [("user.name","fixture"),("user.email","fixture@example.com")]:self.g("config",key,value)
        self.g("remote","add","origin",str(self.remote))
        (self.repo/"metafiles").mkdir();(self.repo/"downloads").mkdir();(self.repo/"docs").mkdir()
        (self.repo/"metafiles/state.json").write_text('{}');(self.repo/"docs/meta.json").write_text('{"items":[]}')
        (self.repo/"README.md").write_text('baseline');(self.repo/".gitignore").write_text('downloads/**/.minas-*/\ndownloads/**/.backup-*/\n')
        self.g("add",".");self.g("commit","-m","baseline");self.g("push","origin","main");self.base=self.g("rev-parse","HEAD")
    def tearDown(self):self.temp.cleanup()
    def g(self,*args):return checkpoint_update.git(self.repo,*args).stdout.strip()
    def remote_sha(self,ref="main"):return self.g("ls-remote","origin","refs/heads/"+ref).split()[0]
    def new_asset(self):
        (self.repo/"downloads/new.jpg").write_bytes(b"complete")
        staging=self.repo/"downloads/.minas-fixture";staging.mkdir();(staging/"asset.part").write_bytes(b"partial")
    def test_completed_assets_commit_excludes_staging_and_unrelated_work(self):
        self.new_asset();(self.repo/"README.md").write_text('user work')
        result=checkpoint_update.checkpoint(self.repo,True,"123-1")
        self.assertEqual("main",result["mode"]);self.assertEqual(result["commit"],self.remote_sha())
        self.assertIn("user work",(self.repo/"README.md").read_text())
        self.assertNotIn("asset.part",self.g("ls-tree","-r","--name-only","HEAD"))
        self.assertEqual("baseline",self.g("show","HEAD:README.md"))
        self.assertEqual("unchanged",checkpoint_update.checkpoint(self.repo,True,"123-1")["mode"])
    def test_main_advance_preserves_data_on_recovery_branch(self):
        self.new_asset()
        other=self.root/"other";subprocess.run(["git","clone","-b","main",str(self.remote),str(other)],check=True,capture_output=True)
        (other/"human.txt").write_text('preserve')
        for args in [("add","."),("-c","user.name=human","-c","user.email=human@example.com","commit","-m","human"),("push","origin","main")]:checkpoint_update.git(other,*args)
        advanced=self.remote_sha();result=checkpoint_update.checkpoint(self.repo,True,"124-1")
        self.assertEqual("recovery",result["mode"]);self.assertEqual(advanced,self.remote_sha());self.assertEqual(result["commit"],self.remote_sha(result["branch"]))
    def test_gallery_failure_or_corrupt_metadata_never_publishes_main(self):
        for allow,corrupt,key in [(False,False,"125-1"),(True,True,"126-1")]:
            self.g("reset","--hard",self.base)
            (self.repo/"downloads").mkdir(exist_ok=True)
            (self.repo/"downloads/new.jpg").write_bytes(b"complete")
            if corrupt:(self.repo/"metafiles/state.json").write_text('broken')
            result=checkpoint_update.checkpoint(self.repo,allow,key)
            self.assertEqual("recovery",result["mode"]);self.assertEqual(self.base,self.remote_sha())
    def test_truncated_csv_is_kept_only_for_recovery(self):
        path=self.repo/"metafiles/posts.csv"
        path.write_text("post_url,title\nhttps://example.com/one,one\nhttps://example.com/two,two\n")
        self.g("add","metafiles/posts.csv");self.g("commit","-m","posts");self.g("push","origin","main")
        before=self.remote_sha();path.write_text("post_url,title\nhttps://example.com/one,one\n")
        result=checkpoint_update.checkpoint(self.repo,True,"129-1")
        self.assertEqual("recovery",result["mode"]);self.assertEqual(before,self.remote_sha())

    def test_post_index_is_checkpointed_and_cannot_drop_history(self):
        folder=self.repo/"posts";folder.mkdir()
        path=folder/"official.csv"
        path.write_text("post_url,title\nhttps://example.com/one,one\n")
        first=checkpoint_update.checkpoint(self.repo,True,"131-1")
        self.assertEqual("main",first["mode"])
        self.assertIn("https://example.com/one",self.g("show","HEAD:posts/official.csv"))
        path.write_text("post_url,title\nhttps://example.com/one,one\nhttps://example.com/two,two\n")
        second=checkpoint_update.checkpoint(self.repo,True,"132-1")
        self.assertEqual("main",second["mode"])
        self.assertEqual("unchanged",checkpoint_update.checkpoint(self.repo,True,"132-1")["mode"])
        path.write_text("post_url,title\nhttps://example.com/two,two\n")
        third=checkpoint_update.checkpoint(self.repo,True,"133-1")
        self.assertEqual("recovery",third["mode"])
        self.assertEqual(second["commit"],self.remote_sha())

    def test_partial_download_gallery_and_checkpoint_preserve_success(self):
        from PIL import Image
        from tools import build_gallery
        old=Path.cwd();os.chdir(self.repo)
        try:
            rows=[{"post_name":name,"post_time":"2025-01-01","minas_link":"https://minas.example/d/fixture/","minas_pwd":"fixture-private"} for name in ("good","bad")]
            with Path("rows.csv").open("w",newline="") as stream:
                writer=csv.DictWriter(stream,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
            async def transfer(url,password,path,out_dir,force_extract_dir=None):
                if force_extract_dir.name.endswith("bad"):raise requests.HTTPError("refused",response=Mock(status_code=403))
                force_extract_dir.mkdir(parents=True,exist_ok=True);Image.new("RGB",(32,24),"red").save(force_extract_dir/"complete.png")
            with patch.object(downloader,"STATE_FILE",Path("metafiles/state.json")),patch.object(downloader,"REPORT_FILE",Path("metafiles/report.json")),patch.object(downloader,"_zip_current_path",side_effect=transfer):
                with self.assertRaises(RuntimeError):downloader.run_batch_from_csv(Path("rows.csv"),Path("downloads"))
            build_gallery.main()
            self.assertEqual(1,len(json.loads(Path("docs/meta.json").read_text(encoding="utf8"))["items"]))
            result=checkpoint_update.checkpoint(self.repo,True,"130-1")
            self.assertEqual("main",result["mode"])
            report=json.loads(self.g("show","HEAD:metafiles/report.json"))
            self.assertEqual("partial_failure",report["status"])
            self.assertEqual(1,report["zip"]["completed"]);self.assertEqual(1,report["zip"]["failed"])
            self.assertEqual(result["commit"],self.remote_sha())
        finally:os.chdir(old)

    def test_preexisting_staged_user_work_is_rejected(self):
        (self.repo/"README.md").write_text('user');self.g("add","README.md")
        with self.assertRaisesRegex(RuntimeError,"pre-existing staged"):checkpoint_update.checkpoint(self.repo,True,"127-1")
        self.assertEqual("README.md",self.g("diff","--cached","--name-only"))
    def test_uncertain_push_result_is_verified_without_duplicate_recovery(self):
        self.new_asset();real=checkpoint_update.git
        def uncertain(repo,*args,**kwargs):
            result=real(repo,*args,**kwargs)
            if args==("push","origin","HEAD:refs/heads/main"):result.returncode=1
            return result
        with patch.object(checkpoint_update,"git",side_effect=uncertain):result=checkpoint_update.checkpoint(self.repo,True,"128-1")
        self.assertEqual("main",result["mode"]);self.assertNotIn("asset-recovery",self.g("ls-remote","origin"))


class PagesTests(unittest.TestCase):
    def response(self,value,status=200):
        response=Mock(status_code=status);response.json.return_value=value
        if status>=400:response.raise_for_status.side_effect=requests.HTTPError(str(status))
        return response
    def test_publish_exact_commit_and_existing_build_is_idempotent(self):
        session=Mock();session.get.side_effect=[self.response({"object":{"sha":"new"}}),self.response({"commit":"old","status":"built"}),self.response({"commit":"new","status":"building"}),self.response({"commit":"new","status":"built"})]
        self.assertEqual("built",publish_pages.publish(session,"owner/repo","new",sleep=Mock()))
        session.post.assert_called_once();self.assertTrue(session.post.call_args.args[0].endswith('/pages/builds'))
        session=Mock();session.get.side_effect=[self.response({"object":{"sha":"new"}}),self.response({"commit":"new","status":"built"})]
        self.assertEqual("already_built",publish_pages.publish(session,"owner/repo","new"));session.post.assert_not_called()
    def test_pending_build_is_observed_without_duplicate_publication(self):
        session=Mock();session.get.side_effect=[self.response({"object":{"sha":"new"}}),self.response({"commit":"new","status":"building"}),self.response({"commit":"new","status":"built"})]
        self.assertEqual("built",publish_pages.publish(session,"owner/repo","new",sleep=Mock()));session.post.assert_not_called()

    def test_timeout_does_not_repeat_build_request(self):
        session=Mock();session.get.side_effect=[self.response({"object":{"sha":"new"}}),self.response({"commit":"old","status":"built"})]
        with self.assertRaises(TimeoutError):publish_pages.publish(session,"owner/repo","new",clock=Mock(side_effect=[0,601]))
        session.post.assert_called_once()

    def test_advanced_main_failed_build_and_token_rejection_fail_closed(self):
        session=Mock();session.get.return_value=self.response({"object":{"sha":"advanced"}})
        with self.assertRaisesRegex(RuntimeError,"Main advanced"):publish_pages.publish(session,"owner/repo","new")
        session.post.assert_not_called()
        session=Mock();session.get.side_effect=[self.response({"object":{"sha":"new"}}),self.response({"commit":"old","status":"built"}),self.response({"commit":"new","status":"errored"})]
        with self.assertRaisesRegex(RuntimeError,"Pages build failed"):publish_pages.publish(session,"owner/repo","new")
        session=Mock();session.get.side_effect=[self.response({"object":{"sha":"new"}}),self.response({"commit":"old","status":"built"})];session.post.return_value=self.response({},403)
        with self.assertRaises(requests.HTTPError):publish_pages.publish(session,"owner/repo","new")
        session.post.assert_called_once()


if __name__=="__main__":unittest.main()
