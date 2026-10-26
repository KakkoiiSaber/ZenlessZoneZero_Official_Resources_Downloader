"""HEAD-only checks for duplicate official media; never transfer video bodies."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import os
import re
import shutil
import subprocess
import struct
import tempfile
from pathlib import Path

import requests

from src.scrape.http import now
from src.scrape.parse import media_url, official_index, media_identity


def parse_headers(raw):
    text = raw.decode('latin1').replace('\r', '') if isinstance(raw, bytes) else raw.replace('\r', '')
    blocks = text.strip().split('\n\n')
    block = next((b for b in reversed(blocks) if b.startswith('HTTP/')), text)
    lines = block.splitlines()
    headers = {key.lower(): value.strip() for line in lines[1:] if ':' in line
               for key, value in [line.split(':', 1)]}
    return {'status': int(lines[0].split()[1]),
            'size_bytes': int(headers['content-length']) if headers.get('content-length', '').isdigit() else None,
            'etag': headers.get('etag'), 'content_type': headers.get('content-type'),
            'last_modified': headers.get('last-modified'), 'checked_at': now()}


def head(url):
    if media_url(url, 'video') != url:
        raise ValueError('Untrusted media URL')
    curl = shutil.which('curl.exe' if os.name == 'nt' else 'curl')
    if curl:
        # Let the system choose IPv4/IPv6. This CDN is not reachable over IPv4 everywhere.
        result = subprocess.run([curl, '-sS', '-I', '--connect-timeout', '4', '--max-time', '15',
            '-H', 'Accept-Encoding: identity', url], capture_output=True, timeout=20)
        if result.returncode:
            raise ValueError('media_head_transport_' + str(result.returncode))
        return parse_headers(result.stdout)
    with requests.head(url, headers={'Accept-Encoding': 'identity'}, timeout=(4, 15), allow_redirects=False) as response:
        raw = 'HTTP/1.1 ' + str(response.status_code) + '\n' + '\n'.join(
            key + ': ' + value for key, value in response.headers.items())
        return parse_headers(raw)


def fresh(metadata):
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(metadata['checked_at'])
        return metadata['status'] == 200 and age.total_seconds() < 7*86400
    except (KeyError, TypeError, ValueError):
        return False


def parse_mp4_metadata(data):
    result = {}
    for tag in (b'mvhd', b'tkhd', b'avc1', b'hvc1', b'hev1'):
        offset = 0
        while True:
            index = data.find(tag, offset)
            if index < 0:
                break
            offset = index + 4
            if index < 4 or index + 32 > len(data):
                continue
            size = struct.unpack_from('>I', data, index-4)[0]
            if tag == b'mvhd' and size in (108, 120):
                version = data[index+4]
                position = index+24 if version == 1 else index+16
                scale = struct.unpack_from('>I', data, position)[0]
                duration = struct.unpack_from('>Q' if version == 1 else '>I', data, position+4)[0]
                if scale and 0 < duration/scale < 7*86400:
                    result['duration_ms'] = round(duration*1000/scale)
            elif tag == b'tkhd' and size in (92, 104) and index-4+size <= len(data):
                width, height = struct.unpack_from('>II', data, index-4+size-8)
                width, height = round(width/65536), round(height/65536)
                if 0 < width <= 16384 and 0 < height <= 16384 and width*height > result.get('width',0)*result.get('height',0):
                    result.update(width=width, height=height)
            elif tag in (b'avc1',b'hvc1',b'hev1') and 86 <= size <= len(data)-index+4:
                width, height = struct.unpack_from('>HH', data, index+28)
                if 0 < width <= 16384 and 0 < height <= 16384:
                    result['codec'] = tag.decode()
    return result


def video_profile(url, metadata):
    """Read at most 1 MiB at each end, only when HTTP validators cannot resolve a duplicate."""
    curl = shutil.which('curl.exe' if os.name == 'nt' else 'curl')
    if not curl or media_url(url, 'video') != url or not metadata.get('size_bytes'):
        raise ValueError('video_profile_unavailable')
    maximum = 1024**2
    size = metadata['size_bytes']
    result = {}
    with tempfile.TemporaryDirectory(prefix='zzz-mp4-metadata-') as work:
        for start, end in [(0, min(size, maximum)-1), (max(0,size-maximum), size-1)]:
            body, headers = Path(work)/'header.bin', Path(work)/'response.txt'
            response = subprocess.run([curl, '-sS', '--fail', '--range', f'{start}-{end}',
                '--max-filesize', str(maximum), '--connect-timeout', '4', '--max-time', '25',
                '-D', str(headers), '-o', str(body), url], capture_output=True, timeout=30)
            if response.returncode:
                raise ValueError('video_profile_transport')
            facts = parse_headers(headers.read_bytes())
            if facts['status'] != 206 or facts.get('etag') != metadata.get('etag'):
                raise ValueError('video_profile_changed_or_not_partial')
            data = body.read_bytes()
            if len(data) > maximum:
                raise ValueError('video_profile_size_limit')
            result.update(parse_mp4_metadata(data))
            if all(key in result for key in ('duration_ms','width','height','codec')):
                break
    if not all(key in result for key in ('duration_ms','width','height','codec')):
        raise ValueError('video_profile_incomplete')
    result.update(source_etag=metadata['etag'], source_size_bytes=size, checked_at=now(),
                  average_bitrate=round(size*8000/result['duration_ms']))
    return result


def review_duplicates(posts, probe=head, workers=4, profile_probe=video_profile, profile_post_ids=None):
    urls = {}
    for candidates in official_index(posts).values():
        playable = [post for post in candidates if post.get('status') == 'ok' and post.get('videos')]
        if len(playable) < 2:
            continue
        for post in playable:
            for video in post['videos']:
                chosen = video.get('selected') or {}
                if chosen.get('url') and not fresh(chosen.get('http_metadata') or {}):
                    urls.setdefault(chosen['url'], []).append(video)
    result = {'requested': len(urls), 'checked': 0, 'failures': 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(probe, url): url for url in urls}
        for future in as_completed(jobs):
            url = jobs[future]
            try:
                metadata = future.result()
                if metadata.get('status') != 200:
                    raise ValueError('media_head_status')
                for video in urls[url]:
                    for value in [video['selected'], *video.get('variants', [])]:
                        if value['url'] == url:
                            value['http_metadata'] = metadata
                            if metadata.get('size_bytes') is not None:
                                value['size_bytes'] = metadata['size_bytes']
                result['checked'] += 1
            except (ValueError, requests.RequestException, subprocess.SubprocessError):
                result['failures'] += 1
    profiles = {}
    for candidates in official_index(posts).values():
        playable = [post for post in candidates if post.get('status') == 'ok' and post.get('videos')]
        identities = [media_identity(post) for post in playable]
        if len(playable) < 2 or (all(identities) and len(set(identities)) == 1):
            continue
        if profile_post_ids is not None and not any(post['id'] in profile_post_ids for post in playable):
            continue
        for post in playable:
            for video in post['videos']:
                value = video['selected']
                facts = value.get('http_metadata') or {}
                previous = value.get('video_metadata') or {}
                if facts.get('status') != 200 or previous.get('source_etag') == facts.get('etag'):
                    continue
                profiles.setdefault(value['url'], []).append(video)
    result.update(profiles_checked=0, profile_failures=0)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(profile_probe, url, videos[0]['selected']['http_metadata']): url
                for url, videos in profiles.items()}
        for future in as_completed(jobs):
            url = jobs[future]
            try:
                profile = future.result()
                for video in profiles[url]:
                    video['duration_ms'] = profile['duration_ms']
                    for value in [video['selected'], *video.get('variants', [])]:
                        if value['url'] == url:
                            value['video_metadata'] = profile
                            value.update({key: profile[key] for key in ('width','height','codec','average_bitrate')})
                result['profiles_checked'] += 1
            except (ValueError, requests.RequestException, subprocess.SubprocessError):
                result['profile_failures'] += 1
    return result
