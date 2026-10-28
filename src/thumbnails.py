"""Incremental local video thumbnails; the Wiki never decodes video on requests."""
import hashlib
import logging
from pathlib import Path
import re
import shutil
import subprocess

from src.image_info import load_size_index, read_image_size
from src.scrape.http import atomic_json

LOG = logging.getLogger('download')
RECIPE = 'first-frame-native-png-v2'


def targets(document):
    return {r['id'] for entry in document['entries'].values()
            if 99 in entry.get('category_ids', []) or '动态壁纸' in entry.get('title', '')
            for r in entry['resources'] if r['kind'] == 'video'}


def preview_order(entry):
    # Official Wiki lists newest wallpaper sections first. Upload dates provide
    # a stable chronological key when a new section is appended instead.
    def date(resource):
        match = re.search(r'/(20\d{2})/(\d{2})/(\d{2})/', resource['url'])
        return tuple(map(int, match.groups())) if match else (0, 0, 0)
    videos = [r for r in entry['resources'] if r['kind'] == 'video']
    return sorted(videos, key=date, reverse=True) if all(date(r) != (0, 0, 0) for r in videos) else videos


def update_thumbnails(document, resources, state_root, records, locate, stop, *, verify_files=False):
    root = state_root / 'thumbnails'
    index_path = root / 'index.json'
    index = load_size_index(index_path)
    wanted = targets(document)
    generated = failed = 0
    executable = None
    for resource in resources:
        if stop.is_set():
            break
        ident = resource['id']
        if ident not in wanted:
            continue
        record = records.get(ident, {})
        if not record.get('complete') or record.get('fingerprint') != resource['fingerprint']:
            continue
        source = hashlib.sha256((RECIPE + resource['fingerprint'] + record['sha256']).encode()).hexdigest()
        filename = hashlib.sha256((ident + source).encode()).hexdigest() + '.png'
        old = index.get(ident, {})
        if (old.get('source') == source and old.get('filename') == filename and old.get('size')
                and (not verify_files or (root / filename).is_file())):
            continue
        video = locate(resource)
        if video is None:
            continue
        executable = executable or shutil.which('ffmpeg')
        if not executable:
            LOG.warning('Thumbnail generation requires FFmpeg; existing downloads are preserved')
            return {'generated': generated, 'failed': failed + 1}
        root.mkdir(parents=True, exist_ok=True)
        output = root / (filename + '.tmp.png')
        try:
            # One local first frame only; no seeking, sampling or network access.
            subprocess.run([executable, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-threads', '1', '-protocol_whitelist', 'file,pipe', '-i', str(video), '-an', '-sn',
                '-frames:v', '1', '-c:v', 'png', '-threads', '1', str(output)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            size = read_image_size(output)
            if not size:
                raise ValueError('No video frame')
            output.replace(root / filename)
            index[ident] = {'source': source, 'fingerprint': resource['fingerprint'],
                            'filename': filename, 'size': size}
            # Persist each result so shutdowns do not lose a completed batch.
            atomic_json(index_path, index)
            generated += 1
            LOG.info('Generated thumbnail: %s', resource.get('label', ident))
        except (OSError, ValueError, subprocess.SubprocessError):
            failed += 1
            LOG.warning('Thumbnail generation failed: %s; retry on the next run', resource.get('label', ident))
        finally:
            output.unlink(missing_ok=True)
    LOG.info('Thumbnails ready: %d generated, %d failed', generated, failed)
    return {'generated': generated, 'failed': failed}
