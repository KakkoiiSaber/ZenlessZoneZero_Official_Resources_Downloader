"""Network-free runtime smoke test, mounted into the image only during CI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from src.metadata.catalog import revision


def main():
    app = Path('/app')
    assert os.getuid() != 0, 'Image must run as non-root'
    assert {p.name for p in app.iterdir()} == {'src', 'tools', 'requirements-nas.txt'}
    allowed_tools = {'build_gallery.py', 'deduplicate_images.py', 'gallery_tags.py', 'gallery_tag_rules.py'}
    assert {p.name for p in (app / 'tools').iterdir()} == allowed_tools
    assert {p.name for p in (app / 'src').iterdir()} == {'nas', 'metadata', 'downloader'}
    for path in app.rglob('*'):
        assert not path.is_symlink(), str(path)
        if path.is_file():
            assert path.suffix in ('.py', '.txt'), str(path)
            assert path.name != 'share-codes.json'
    with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
        manifest = Path(temporary) / 'empty.json'
        catalog = {'version': 1, 'sources': {}, 'items': {}}
        catalog['revision'] = revision(catalog)
        manifest.write_text(json.dumps(catalog), encoding='utf8')
        # Actual CLI one-shot path, no remote catalog or media requests.
        result = subprocess.run([sys.executable, '-m', 'src.nas.sync', '--data', '/data',
                                 '--manifest', str(manifest)], text=True, capture_output=True, check=True)
        report = json.loads(result.stdout)
        assert report['completed'] == 0 and report['attempted'] == 0 and report['bytes'] == 0
        assert report['failures'] == []
        assert json.loads(Path('/data/state/catalog.json').read_text()) == catalog
        assert Path('/data/state/sync.json').is_file()
    print('PASS: non-root, program-only image; offline CLI one-shot; zero media transfers')


if __name__ == '__main__':
    main()
