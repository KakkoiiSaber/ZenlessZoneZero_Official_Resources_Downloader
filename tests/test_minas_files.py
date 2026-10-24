import collections
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from PIL import Image
from src.downloader import minas_files as files
from src.downloader.MinasDownloader import authenticate_with_password
from tools import build_gallery as gallery

SHARE = "https://minas.mihoyo.com/d/fixtureA/"
SECOND = "https://minas.mihoyo.com/d/fixtureB/"
STAMP = "2025-01-01T12:00:00+08:00"
NOW = 1800000000


def row(post="one", share=SHARE):
    return {"post_name": post + " | " + files.CATEGORY, "post_time": "2025-01-01",
            "post_url": "https://example.com/posts/" + post,
            "minas_link": share, "minas_pwd": "fixture-code"}


def directory(path):
    return {"is_dir": True, "size": 0, "folder_path": path,
            "folder_name": path.strip("/").split("/")[-1], "last_modified": STAMP}


def file_entry(path, body):
    return {"is_dir": False, "file_path": path, "file_name": path.split("/")[-1],
            "size": len(body), "last_modified": STAMP,
            "encoded_thumbnail_src": "thumbnail/fixture/48/placeholder"}


class Response:
    def __init__(self, body=b"", headers=None, fail=False):
        self.body = body
        self.headers = headers or {}
        self.fail = fail
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def raise_for_status(self): pass
    def iter_content(self, size):
        yield self.body[:2]
        if self.fail:
            raise OSError("interrupted fixture stream")
        yield self.body[2:]


class FakeSession:
    def __init__(self):
        self.dirs = {SHARE: {"/": [directory("/version/")], "/version/": []}}
        self.bodies = {SHARE: {}}
        self.etags = {}
        self.failed = set()
        self.list_calls = []
        self.head_calls = []
        self.download_calls = []
        self.extra_listing = {}
        self.wrong_body = {}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def add(self, path, body, share=SHARE):
        if share not in self.dirs:
            self.dirs[share] = {"/": [directory("/version/")], "/version/": []}
            self.bodies[share] = {}
        parent = str(Path(path).parent).replace("\\", "/").rstrip("/") + "/"
        if parent not in self.dirs[share]:
            raise ValueError("Create fixture directory first")
        self.dirs[share][parent] = [e for e in self.dirs[share][parent] if e.get("file_path") != path]
        self.dirs[share][parent].append(file_entry(path, body))
        self.bodies[share][path] = body
        self.etags[(share, path)] = hashlib.sha1(body).hexdigest()  # Opaque fixture validator, not SHA-256.
    def get(self, url, params=None, **kwargs):
        parsed = urlparse(url)
        if "/dirents/" in parsed.path:
            token = parsed.path.split("/")[4]
            share = "https://minas.mihoyo.com/d/" + token + "/"
            path = params["path"]
            self.list_calls.append((share, path))
            data = {"dirent_list": self.dirs[share][path], **self.extra_listing}
            return Response(json.dumps(data).encode())
        share = "https://minas.mihoyo.com" + parsed.path.removesuffix("files/")
        path = parse_qs(parsed.query)["p"][0]
        self.download_calls.append((share, path))
        body = self.bodies[share][path]
        return Response(self.wrong_body.get((share, path), body),
                        {"Content-Length": str(len(body)), "ETag": self.etags[(share, path)],
                         "Content-Type": "application/octet-stream"},
                        fail=(share, path) in self.failed)
    def head(self, url, params, **kwargs):
        share = url.removesuffix("files/")
        path = params["p"]
        self.head_calls.append((share, path))
        return Response(headers={"ETag": self.etags[(share, path)]})


class FileTests(unittest.TestCase):
    def setUp(self):
        self.old = Path.cwd()
        self.temp = tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        self.env = patch.dict(os.environ, {"MINAS_MIN_FREE_BYTES": "1",
                              "MINAS_FILE_STATE_FILE": "state.json"})
        self.env.start()
        original_connect = socket.socket.connect
        def offline_connect(sock, address):
            if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
                return original_connect(sock, address)  # asyncio's local wake-up socket on Windows.
            raise AssertionError("Tests must be offline")
        self.network = patch.object(socket.socket, "connect", new=offline_connect)
        self.network.start()
        self.session = FakeSession()
        self.auth = Mock()
    def tearDown(self):
        self.network.stop(); self.env.stop()
        os.chdir(self.old); self.temp.cleanup()
    def run_files(self, rows=None, now=NOW):
        updater = files.FileUpdater(Path("downloads"), self.auth,
                                    session_factory=lambda: self.session, now=now)
        updater.run(rows or [row()])
        return updater
    def state(self): return json.loads(Path("state.json").read_text(encoding="utf-8"))

    def test_unchanged_link_nested_additions_and_unchanged_reruns(self):
        self.session.add("/version/a.jpg", b"first")
        self.run_files()
        before = len(self.session.download_calls)
        self.run_files(now=NOW+1)
        self.assertEqual(before, len(self.session.download_calls))
        self.session.dirs[SHARE]["/version/"].append(directory("/version/nested/"))
        self.session.dirs[SHARE]["/version/nested/"] = []
        self.session.add("/version/nested/b.jpg", b"second")
        self.run_files(now=NOW+2)
        self.assertEqual(2, len(self.session.download_calls))
        self.assertIn("/version/nested/b.jpg", self.state()["sources"][SHARE]["files"])
        self.assertEqual(3, self.session.list_calls.count((SHARE, "/")))

    def test_repeated_share_scanned_once_distinct_shares_preserved(self):
        self.session.add("/version/a.jpg", b"identical")
        self.session.add("/version/a.jpg", b"identical", share=SECOND)
        self.run_files([row("one"), row("two"), row("three", SECOND)])
        self.assertEqual(1, self.session.list_calls.count((SHARE, "/")))
        self.assertEqual(1, self.session.list_calls.count((SECOND, "/")))
        state = self.state()
        self.assertEqual(2, len(state["sources"]))
        self.assertEqual(2, len(state["sources"][SHARE]["posts"]))
        self.assertEqual(1, len(state["assets"]))
        self.assertEqual(2, len(next(iter(state["assets"].values()))["sources"]))
        self.assertEqual(1, len(list(Path("downloads/_minas-content").glob("*"))))

    def test_same_name_size_timestamp_changed_etag_preserves_old_asset(self):
        self.session.add("/version/a.jpg", b"AAAA")
        self.run_files()
        old_sha = hashlib.sha256(b"AAAA").hexdigest()
        self.session.add("/version/a.jpg", b"BBBB")
        self.run_files(now=NOW+1)
        state = self.state()
        self.assertEqual(2, len(state["assets"]))
        self.assertEqual(b"AAAA", Path(state["assets"][old_sha]["paths"][0]).read_bytes())
        self.assertEqual(hashlib.sha256(b"BBBB").hexdigest(), state["sources"][SHARE]["files"]["/version/a.jpg"]["sha256"])

    def test_periodic_verification_catches_hidden_change_with_same_metadata(self):
        self.session.add("/version/a.jpg", b"AAAA")
        self.run_files()
        old_etag = self.session.etags[(SHARE,"/version/a.jpg")]
        self.session.add("/version/a.jpg", b"BBBB")
        self.session.etags[(SHARE,"/version/a.jpg")] = old_etag
        self.run_files(now=NOW+1)
        self.assertEqual(1, len(self.session.download_calls))
        self.run_files(now=NOW+8*86400)
        self.assertEqual(2, len(self.session.download_calls))
        self.assertEqual(2, len(self.state()["assets"]))

    def test_partial_download_keeps_checkpoint_and_retries(self):
        self.session.add("/version/a.jpg", b"old")
        self.run_files()
        checkpoint = Path("state.json").read_bytes()
        self.session.add("/version/a.jpg", b"replacement")
        self.session.failed.add((SHARE,"/version/a.jpg"))
        with self.assertRaises(RuntimeError): self.run_files(now=NOW+1)
        self.assertEqual(checkpoint, Path("state.json").read_bytes())
        self.assertFalse(list(Path("downloads").glob(".minas-file-*")))
        self.session.failed.clear()
        self.run_files(now=NOW+2)
        self.assertEqual(2, len(self.state()["assets"]))

    def test_run_budget_defers_without_marking_complete(self):
        self.session.add("/version/a.jpg", b"one")
        self.session.add("/version/b.jpg", b"two")
        with patch.dict(os.environ, {"MINAS_FILE_RUN_MAX_DOWNLOADS": "1"}):
            first = self.run_files()
            self.assertEqual(1, first.pending)
            self.assertEqual(1, len(self.state()["sources"][SHARE]["files"]))
            self.run_files(now=NOW+1)
        self.assertEqual(2, len(self.state()["sources"][SHARE]["files"]))
        self.assertEqual(2, len(self.session.download_calls))

    def test_limits_and_unsafe_listing_fail_before_download(self):
        cases = [("MINAS_MAX_FILE_BYTES", "2"), ("MINAS_MAX_FILES", "1"),
                 ("MINAS_FILE_MAX_DIRS", "1"), ("MINAS_FILE_MAX_DEPTH", "2"),
                 ("MINAS_FILE_MAX_LISTING_BYTES", "2"), ("MINAS_FILE_MAX_METADATA_REQUESTS", "1")]
        self.session.add("/version/a.jpg", b"asset")
        for key, value in cases:
            with self.subTest(key=key), patch.dict(os.environ, {key:value}), self.assertRaises(RuntimeError):
                self.run_files()
        self.assertEqual([], self.session.download_calls)
        self.session.extra_listing = {"has_more": True}
        with self.assertRaises(RuntimeError): self.run_files()
        self.session.extra_listing = {}
        self.session.dirs[SHARE]["/version/"][0]["file_path"] = "/version/../escape.jpg"
        with self.assertRaises(RuntimeError): self.run_files()
        self.assertEqual([], self.session.download_calls)

    def test_stream_size_change_and_run_byte_limit_leave_no_asset(self):
        self.session.add("/version/a.jpg", b"four")
        self.session.wrong_body[(SHARE,"/version/a.jpg")] = b"unexpected-long-body"
        with patch.dict(os.environ, {"MINAS_FILE_RUN_MAX_BYTES":"6"}), self.assertRaises(RuntimeError):
            self.run_files()
        self.assertFalse(Path("state.json").exists())
        self.assertFalse(list(Path("downloads").rglob("*.jpg")))

    def test_seeded_pointer_bootstrap_no_hydration_and_later_verification(self):
        body = b"old-known-content"; sha = hashlib.sha256(body).hexdigest()
        self.session.add("/version/a.jpg", body)
        pointer = Path("downloads/old/a.jpg"); pointer.parent.mkdir(parents=True)
        pointer.write_text(f"version https://git-lfs.github.com/spec/v1\noid sha256:{sha}\nsize {len(body)}\n")
        original = pointer.read_bytes()
        state = {"version":1,"posts":{},"sources":{SHARE:{"posts":[],"files":{
            "/version/a.jpg":{"sha256":sha,"size":len(body),"seeded":True,"snapshot_at":NOW}}}},
            "assets":{sha:{"paths":[pointer.as_posix()],"size":len(body),"sources":[{"share":SHARE,"path":"/version/a.jpg"}]}}}
        Path("state.json").write_text(json.dumps(state))
        self.run_files()
        self.assertEqual([],self.session.download_calls)
        self.assertEqual(original,pointer.read_bytes())
        self.run_files(now=NOW+8*86400)
        self.assertEqual(1,len(self.session.download_calls))
        self.assertEqual(original,pointer.read_bytes())

    def test_post_source_change_preserves_prior_associations(self):
        self.session.add("/version/a.jpg", b"old")
        self.run_files()
        self.session.add("/version/new.jpg", b"new",share=SECOND)
        self.run_files([row(share=SECOND)],now=NOW+1)
        state=self.state(); post=state["posts"][row()["post_url"]]
        self.assertEqual([SHARE,SECOND],post["sources"])
        self.assertEqual(SECOND,post["current_source"])
        self.assertEqual(2,len(state["assets"]))

    def test_verification_quota_resumes_oldest_deferred_files(self):
        self.session.add("/version/a.jpg", b"first")
        self.session.add("/version/b.jpg", b"second")
        self.run_files()
        with patch.dict(os.environ, {"MINAS_FILE_RUN_VERIFY_FILES":"1"}):
            first=self.run_files(now=NOW+8*86400)
            self.assertEqual(1,first.verifications)
            self.assertEqual(1,first.pending)
            self.run_files(now=NOW+9*86400)
        self.assertEqual(4,len(self.session.download_calls))
        self.assertGreater(self.state()["sources"][SHARE]["files"]["/version/b.jpg"]["verified_at"],NOW)

    def test_seeded_newer_remote_file_requires_verification(self):
        self.session.add("/version/a.jpg",b"replacement")
        sha=hashlib.sha256(b"old-content").hexdigest()
        state={"version":1,"posts":{},"assets":{sha:{"paths":[],"size":11,"sources":[]}},
               "sources":{SHARE:{"posts":[],"files":{"/version/a.jpg":{"sha256":sha,"size":11,"seeded":True,"snapshot_at":1}}}}}
        Path("state.json").write_text(json.dumps(state))
        self.run_files()
        self.assertEqual(1,len(self.session.download_calls))
        self.assertNotEqual(sha,self.state()["sources"][SHARE]["files"]["/version/a.jpg"]["sha256"])

    def test_download_deadline_does_not_checkpoint_partial_content(self):
        self.session.add("/version/a.jpg",b"asset")
        with patch("src.downloader.asset_io.time.monotonic",side_effect=[0,999]), self.assertRaises(RuntimeError):
            self.run_files()
        self.assertFalse(Path("state.json").exists())
        self.assertFalse(list(Path("downloads").glob(".minas-file-*")))

    def test_listing_removal_does_not_delete_historical_assets(self):
        self.session.add("/version/a.jpg", b"old")
        self.run_files()
        self.session.dirs[SHARE]["/version/"] = []
        self.run_files(now=NOW+1)
        self.assertEqual(1,len(self.state()["assets"]))
        self.assertIn("/version/a.jpg",self.state()["sources"][SHARE]["files"])

    def test_batch_routes_category_checks_separately_from_zip_rows(self):
        import csv
        from unittest.mock import AsyncMock
        from src.downloader import MinasDownloader as downloader
        category=row(); other={**row("ordinary"),"post_name":"ordinary wallpaper"}
        with Path("rows.csv").open("w",encoding="utf8",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=list(category));writer.writeheader();writer.writerows([category,other])
        with patch.object(downloader,"FileUpdater") as updater, patch.object(downloader,"_process_csv_row",new_callable=AsyncMock) as zip_row:
            downloader.run_batch_from_csv(Path("rows.csv"),Path("downloads"))
            updater.return_value.run.assert_called_once_with([category])
            self.assertEqual(other,zip_row.call_args.args[0])
            zip_row.assert_awaited_once()

    def test_relative_auth_form_and_cross_origin_rejection(self):
        session=Mock()
        first=Mock(url=SHARE,text='<form action="/d/fixtureA/" method="post"><input name="password" type="password"></form>')
        session.get.return_value=first
        session.post.return_value=Mock(text='<html>listing</html>')
        authenticate_with_password(session,SHARE,"fixture-code")
        self.assertEqual(SHARE,session.post.call_args.args[0])
        first.text='<form action="https://example.com/other"><input name="password" type="password"></form>'
        session.post.reset_mock()
        with self.assertRaises(RuntimeError):authenticate_with_password(session,SHARE,"fixture-code")
        session.post.assert_not_called()

    def test_gallery_content_dedup_preserves_paths_posts_and_resolutions(self):
        buffer=io.BytesIO();Image.new("RGB",(80,60),"red").save(buffer,format="PNG")
        first=Path("downloads/post-a/a.png");second=Path("downloads/post-b/b.png")
        for path in (first,second):path.parent.mkdir(parents=True);path.write_bytes(buffer.getvalue())
        other=Path("downloads/post-b/large.png");Image.new("RGB",(160,120),"red").save(other)
        sha=hashlib.sha256(buffer.getvalue()).hexdigest()
        state={"version":1,"assets":{sha:{"paths":[first.as_posix(),second.as_posix()],"size":len(buffer.getvalue()),"sources":[{"share":SHARE,"path":"/version/a.png"}]}},
               "sources":{SHARE:{"posts":["one","two"],"files":{}}},
               "posts":{key:{"title":key,"post_url":"https://example.com/"+key} for key in ("one","two")}}
        Path("state.json").write_text(json.dumps(state))
        gallery.main()
        metadata=json.loads(Path("docs/meta.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(2,len(metadata))
        duplicate=next(it for it in metadata if it["identity"]=="sha256:"+sha)
        self.assertEqual(2,len(duplicate["associations"]))
        self.assertEqual(2,len(duplicate["posts"]))
        self.assertEqual(2,len(list(Path("docs/thumbs").rglob("*.jpg"))))
        before=Path("docs/meta.json").read_bytes()
        for path in (first,second):path.write_text(f"version https://git-lfs.github.com/spec/v1\noid sha256:{sha}\nsize 100\n")
        gallery.main()
        self.assertEqual(before,Path("docs/meta.json").read_bytes())


if __name__ == "__main__": unittest.main()
