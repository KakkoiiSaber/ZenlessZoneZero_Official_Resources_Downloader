import asyncio
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch, Mock
import zipfile

from PIL import Image
from src.downloader import asset_io as assets
from src.downloader import MinasDownloader as downloader
from tools import build_gallery as gallery


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = Path.cwd()
        os.chdir(self.temp.name)
        self.env = patch.dict(os.environ, {"MINAS_MIN_FREE_BYTES": "1"})
        self.env.start()
        self.state = patch.object(downloader, "STATE_FILE", Path("state.json"))
        self.state.start()

    def tearDown(self):
        self.state.stop()
        self.env.stop()
        os.chdir(self.old)
        self.temp.cleanup()

    def image(self, path="downloads/source/2025.01.01 post/a.png", color="red"):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (120, 100), color).save(path)
        return path

    def build(self):
        gallery.main()
        return (Path("docs/meta.json").read_bytes(), Path("docs/cached.json").read_bytes())

    def archive(self, entries):
        path = Path("input.zip")
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return path


class GalleryTests(Workspace):
    def test_unchanged_checkout_mtime_new_and_changed(self):
        original = self.image()
        before = self.build()
        thumb = Path("docs/thumbs/source/2025.01.01 post/a.jpg")
        stamp = thumb.stat().st_mtime_ns
        os.utime(original, (1234567890, 1234567890))
        self.assertEqual(before, self.build())
        self.assertEqual(stamp, thumb.stat().st_mtime_ns)
        self.image(color="blue")
        self.build()
        self.assertNotEqual(before[1], Path("docs/cached.json").read_bytes())
        self.image("downloads/source/2025.02.01 post/b.png")
        self.build()
        self.assertEqual(2, len(json.loads(Path("docs/meta.json").read_text())["items"]))

    def test_pointer_and_absent_original_preserve_preview_metadata(self):
        original = self.image()
        before = self.build()
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        original.write_text(f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize 999\n")
        with patch("PIL.Image.open", side_effect=AssertionError("Pointer must not be decoded")):
            self.assertEqual(before, self.build())
        original.unlink()
        self.assertEqual(before, self.build())
        Path("docs/cached.json").unlink()
        self.assertEqual(before[0], self.build()[0])

    def test_legacy_cache_migration_and_pointer_without_preview(self):
        original = self.image()
        before = self.build()
        cache = json.loads(before[1])
        for entry in cache.values():
            entry.pop("identity")
            entry.pop("settings")
        Path("docs/cached.json").write_text(json.dumps(cache))
        original.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:" + "a"*64 + "\nsize 99\n")
        self.assertEqual(before[0], self.build()[0])
        self.assertEqual(before[0], self.build()[0])
        unknown = original.with_name("unknown.jpg")
        unknown.write_bytes(original.read_bytes())
        self.assertEqual(before[0], self.build()[0])

    def test_changed_pointer_rebuilds_when_original_returns(self):
        original = self.image()
        self.build()
        thumb = Path("docs/thumbs/source/2025.01.01 post/a.jpg")
        old_preview = thumb.read_bytes()
        self.image(color="blue")
        updated = original.read_bytes()
        digest = hashlib.sha256(updated).hexdigest()
        original.write_bytes(f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize {len(updated)}\n".encode())
        self.build()
        self.assertEqual(old_preview, thumb.read_bytes())
        original.write_bytes(updated)
        self.build()
        self.assertNotEqual(old_preview, thumb.read_bytes())

    def test_thumbnail_edge_and_bytes_and_oversize_pixels(self):
        original = self.image()
        Image.effect_noise((1800, 1600), 100).convert("RGB").save(original)
        with patch.object(gallery, "MAX_BYTES", 2500), patch.object(gallery, "MAX_EDGE", 600):
            self.build()
            thumb = Path("docs/thumbs/source/2025.01.01 post/a.jpg")
            self.assertLessEqual(thumb.stat().st_size, 2500)
            with Image.open(thumb) as image:
                self.assertLessEqual(max(image.size), 600)
        before = Path("docs/meta.json").read_bytes()
        with patch.object(gallery, "MAX_PIXELS", 1), self.assertRaises(RuntimeError):
            self.build()
        self.assertEqual(before, Path("docs/meta.json").read_bytes())


class DownloaderTests(Workspace):
    row = {"post_name": "post", "post_time": "2025-01-01", "minas_link": "https://example.com/share/", "minas_pwd": "test"}

    def test_new_unchanged_changed_and_failed_rows(self):
        out = Path("downloads")
        out.mkdir()
        with patch.object(downloader, "_zip_current_path", new_callable=AsyncMock) as fetch:
            asyncio.run(downloader._process_csv_row(self.row, out))
            asyncio.run(downloader._process_csv_row(self.row, out))
            self.assertEqual(1, fetch.await_count)
            changed = dict(self.row, minas_link="https://example.com/changed/")
            fetch.side_effect = RuntimeError("interrupted")
            before = Path("state.json").read_bytes()
            with self.assertRaises(RuntimeError):
                asyncio.run(downloader._process_csv_row(changed, out))
            self.assertEqual(before, Path("state.json").read_bytes())
            fetch.side_effect = None
            asyncio.run(downloader._process_csv_row(changed, out))
            self.assertNotEqual(before, Path("state.json").read_bytes())

    def test_existing_incomplete_directory_retries(self):
        Path("downloads/2025.01.01 post").mkdir(parents=True)
        with patch.object(downloader, "_zip_current_path", new_callable=AsyncMock) as fetch:
            asyncio.run(downloader._process_csv_row(self.row, Path("downloads")))
            fetch.assert_awaited_once()

    def response(self, chunks, length="0"):
        response = Mock()
        response.headers = {"Content-Length": length}
        response.iter_content.return_value = iter(chunks)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        session = Mock()
        session.get.return_value = response
        return session

    def test_interrupted_and_oversize_downloads_remove_partial(self):
        def interrupted():
            yield b"first"
            raise OSError("connection lost")
        for chunks, length, limit in [(interrupted(), "0", "100"), ([b"12345"], "0", "4"), ([b"x"], "101", "100"), ([b"x"], "3", "100")]:
            with self.subTest(length=length, limit=limit):
                with patch.dict(os.environ, {"MINAS_MAX_ARCHIVE_BYTES": limit}):
                    with self.assertRaises((OSError, ValueError)):
                        assets.download_zip(self.response(chunks, length), "https://example.com", Path("download.zip"))
                self.assertFalse(Path("download.zip").exists())

    def test_archive_limits_and_traversal(self):
        for entries, limits in [([("../escape", b"x")], {}), ([("/absolute", b"x")], {}),
                                ([("a", b"12345")], {"MINAS_MAX_FILE_BYTES": "4"}),
                                ([("a", b"123"), ("b", b"123")], {"MINAS_MAX_EXTRACTED_BYTES": "5"}),
                                ([("a", b"x"), ("b", b"x")], {"MINAS_MAX_FILES": "1"}),
                                ([], {}), ([("a", b"x")], {"MINAS_MAX_ARCHIVE_BYTES": "1"})]:
            with self.subTest(entries=entries, limits=limits):
                archive = self.archive(entries)
                Path("stage").mkdir(exist_ok=True)
                with patch.dict(os.environ, limits), self.assertRaises(ValueError):
                    assets.extract_zip(archive, Path("stage"))
                self.assertEqual([], list(Path("stage").rglob("*")))

    def test_batch_failure_propagates(self):
        import csv
        with Path("rows.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(self.row))
            writer.writeheader()
            writer.writerow(self.row)
        with patch.object(downloader, "_process_csv_row", new_callable=AsyncMock, side_effect=ValueError("bad ZIP")):
            with self.assertRaises(RuntimeError):
                downloader.run_batch_from_csv(Path("rows.csv"), Path("downloads"))

    def test_forced_refresh(self):
        out = Path("downloads")
        out.mkdir()
        with patch.object(downloader, "_zip_current_path", new_callable=AsyncMock) as fetch:
            asyncio.run(downloader._process_csv_row(self.row, out))
            with patch.dict(os.environ, {"MINAS_REFRESH_POSTS": "2025.01.01 post"}):
                asyncio.run(downloader._process_csv_row(self.row, out))
            self.assertEqual(2, fetch.await_count)

    def test_download_does_not_delete_preexisting_file(self):
        Path("download.zip").write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            assets.download_zip(self.response([b"new"], "3"), "https://example.com", Path("download.zip"))
        self.assertEqual(b"existing", Path("download.zip").read_bytes())

    def test_valid_stream_download(self):
        assets.download_zip(self.response([b"abc", b"def"], "6"), "https://example.com", Path("download.zip"))
        self.assertEqual(b"abcdef", Path("download.zip").read_bytes())

    def test_symlink_and_duplicate_archives(self):
        import stat
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile("input.zip", "w") as archive:
            archive.writestr(info, "../../elsewhere")
        Path("stage").mkdir()
        with self.assertRaises(ValueError):
            assets.extract_zip(Path("input.zip"), Path("stage"))
        archive = self.archive([("A", b"x"), ("a", b"y")])
        with self.assertRaises(ValueError):
            assets.extract_zip(archive, Path("stage"))
        self.assertEqual([], list(Path("stage").rglob("*")))

    def test_corrupt_archive_never_installs(self):
        Path("bad.zip").write_bytes(b"truncated ZIP")
        Path("stage").mkdir()
        Path("target").mkdir()
        Path("target/old").write_bytes(b"keep")
        with self.assertRaises(zipfile.BadZipFile):
            assets.extract_zip(Path("bad.zip"), Path("stage"))
        self.assertEqual(b"keep", Path("target/old").read_bytes())

    def test_disk_reserve(self):
        with patch.object(assets.shutil, "disk_usage", return_value=Mock(free=0)):
            with self.assertRaises(ValueError):
                assets.check_space(Path("."))

    def test_extract_merge_preserves_extras_and_unchanged_files(self):
        archive = self.archive([("root/a.jpg", b"same"), ("root/b.jpg", b"new")])
        stage = Path("stage")
        stage.mkdir()
        assets.extract_zip(archive, stage)
        target = Path("target")
        target.mkdir()
        (target / "a.jpg").write_bytes(b"same")
        (target / "extra.jpg").write_bytes(b"keep")
        stamp = (target / "a.jpg").stat().st_mtime_ns
        assets.install_tree(stage, target)
        self.assertEqual(stamp, (target / "a.jpg").stat().st_mtime_ns)
        self.assertEqual(b"keep", (target / "extra.jpg").read_bytes())
        self.assertEqual(b"new", (target / "b.jpg").read_bytes())

    def test_install_failure_restores_existing_assets(self):
        stage, target = Path("stage"), Path("target")
        stage.mkdir(); target.mkdir()
        (stage / "a").write_bytes(b"new")
        (stage / "b").write_bytes(b"new")
        (target / "a").write_bytes(b"old")
        original_replace = os.replace
        def fail(source, dest):
            if Path(source) == stage / "b":
                raise OSError("interrupted install")
            return original_replace(source, dest)
        with patch.object(assets.os, "replace", side_effect=fail), self.assertRaises(OSError):
            assets.install_tree(stage, target)
        self.assertEqual(b"old", (target / "a").read_bytes())
        self.assertFalse((target / "b").exists())


if __name__ == "__main__":
    unittest.main()
