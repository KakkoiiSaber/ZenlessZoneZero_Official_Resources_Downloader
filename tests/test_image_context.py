import tempfile
from pathlib import Path
import unittest

from tools.prepare_image_context import DIRECTORIES, FILES, prepare


class ImageContextTests(unittest.TestCase):
    def fixture(self, root):
        for name in FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# source fixture\n', encoding='utf8')
        for directory in DIRECTORIES:
            path = root / directory / 'worker.py'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# worker\n', encoding='utf8')

    def test_context_excludes_secrets_assets_git_and_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'
            self.fixture(root)
            excluded = ['.git/config', 'deploy/synology/.env', 'private-config/share-codes.json',
                        'downloads/original.jpg', 'docs/thumbs/preview.webp', 'metafiles/catalog.json',
                        'posts/private.json', 'src/config/user.py', 'src/nas/__pycache__/worker.pyc',
                        'src/nas/share-codes.json', 'src/nas/.env']
            for name in excluded:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('private-fixture', encoding='utf8')
            target = Path(temporary) / 'build'
            copied = prepare(root, target)
            self.assertEqual(set(FILES) | {d + '/worker.py' for d in DIRECTORIES}, set(copied))
            self.assertFalse(any((target / name).exists() for name in excluded))
            self.assertEqual({p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}, set(copied))

    def test_existing_target_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'; self.fixture(root)
            target = Path(temporary) / 'data'; target.mkdir()
            sentinel = target / 'existing'; sentinel.write_bytes(b'keep')
            with self.assertRaises(ValueError): prepare(root, target)
            self.assertEqual(b'keep', sentinel.read_bytes())

    def test_missing_source_fails_before_creating_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'; self.fixture(root)
            (root / FILES[0]).unlink()
            target = Path(temporary) / 'build'
            with self.assertRaises(ValueError): prepare(root, target)
            self.assertFalse(target.exists())

    def test_actual_program_context_is_small_and_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(__file__).resolve().parents[1]
            target = Path(temporary) / 'build'
            copied = prepare(root, target)
            self.assertIn('src/nas/sync.py', copied)
            self.assertIn('src/metadata/catalog.py', copied)
            self.assertIn('src/downloader/asset_io.py', copied)
            self.assertLess(sum(p.stat().st_size for p in target.rglob('*') if p.is_file()), 1024 * 1024)


if __name__ == '__main__': unittest.main()
