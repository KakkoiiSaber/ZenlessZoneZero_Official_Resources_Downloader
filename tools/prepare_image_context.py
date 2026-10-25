"""Create an allowlisted program-only Docker context; never copy a checkout wholesale."""
from pathlib import Path
import argparse
import shutil

DIRECTORIES = ('src/downloader', 'src/metadata', 'src/nas')
FILES = ('requirements-nas.txt', 'deploy/synology/Dockerfile', 'src/metadata/knowledge.json',
         'tools/build_gallery.py', 'tools/deduplicate_images.py',
         'tools/gallery_tags.py', 'tools/gallery_tag_rules.py')


def prepare(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.exists():
        raise ValueError('Build context must be a new directory')
    paths = list(FILES)
    for directory in DIRECTORIES:
        paths.extend(path.relative_to(source).as_posix()
                     for path in sorted((source / directory).rglob('*.py')))
    for name in paths:
        path = source / name
        if not path.is_file() or any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('Missing or symlinked program source: ' + name)
    target.mkdir(parents=True)
    for name in paths:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
    return sorted(paths)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target')
    args = parser.parse_args()
    paths = prepare(Path(__file__).resolve().parents[1], args.target)
    print('Program-only context: ' + str(len(paths)) + ' files')
