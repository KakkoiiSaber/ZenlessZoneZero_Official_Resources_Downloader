"""Offline, read-only media Wiki. Never fetches media or metadata from the Internet."""
import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
from math import gcd
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from src.image_info import load_size_index, read_image_size
from src.thumbnails import preview_order

from src.download import LOG_DATE_FORMAT, LOG_FORMAT, cover_plan, digest, plan, safe_path

WEB = Path(__file__).resolve().parent.parent / 'web'
STATIC = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js',
          '/style.css': 'style.css', '/assets/background.png': 'assets/background.png',
          '/assets/zzz-logo.svg': 'assets/zzz-logo.svg'}


def wallpaper_variants(groups):
    """Combine display groups only; download folders and resource identities stay intact."""
    candidates = {}
    for index, group in enumerate(groups):
        match = re.fullmatch(r'(.+?)[(（]\s*(\d{2,5})\s*[xX×]\s*(\d{2,5})\s*[)）]', group['name'])
        if match and group['resources'] and all(r['kind'] == 'image' for r in group['resources']):
            name, width, height = match[1].strip(), int(match[2]), int(match[3])
            if width and height:
                candidates.setdefault(name, []).append((index, width, height))
    replacements, removed = {}, set()
    for name, versions in candidates.items():
        if len({(w, h) for _, w, h in versions}) < 2:
            continue
        combined = {'name': name, 'resources': [], 'variants': []}
        for index, width, height in versions:
            ident = f'{width}x{height}'
            divisor = gcd(width, height)
            ratio = '21:9' if abs(width / height - 21 / 9) < .05 else f'{width // divisor}:{height // divisor}'
            variant = {'id': ident, 'label': ratio, 'dimensions': f'{width}×{height}'}
            if not any(v['id'] == ident for v in combined['variants']):
                combined['variants'].append(variant)
            combined['resources'].extend({**r, 'variant': ident} for r in groups[index]['resources'])
            removed.add(index)
        # Prefer the larger pixel count, retaining every composition as an option.
        combined['variants'].sort(key=lambda v: int(v['id'].split('x')[0]) * int(v['id'].split('x')[1]), reverse=True)
        replacements[versions[0][0]] = combined
    return [replacements.get(index, group) for index, group in enumerate(groups)
            if index not in removed or index in replacements]


class Catalog:
    def __init__(self, manifest, media_root, snapshot=None, covers='state/covers', *, debug=False):
        self.debug = debug
        self.manifest = Path(manifest)
        self.media_root = Path(media_root).resolve()
        self.lock = threading.Lock()
        self.signature = None
        self.document = None
        self.resources = None
        self.paths = None
        self.snapshot = Path(snapshot) if snapshot else None
        self.cover_root = Path(covers).resolve()
        self.cover_paths = {}
        self.image_sizes = {}
        self.sizes_file = self.cover_root.parent / 'image-sizes.json'
        self.persisted_sizes = {}
        self.thumbnail_root = self.cover_root.parent / 'thumbnails'

    def thumbnails(self):
        _, resources, _ = self.index()
        cached = load_size_index(self.thumbnail_root / 'index.json')
        return {ident: item for ident, item in cached.items()
                if ident in resources and isinstance(item, dict) and item.get('fingerprint') == resources[ident]['fingerprint']
                and re.fullmatch(r'[0-9a-f]{64}\.(?:png|jpg)', str(item.get('filename', '')))}

    def locate_thumbnail(self, filename):
        if not any(item['filename'] == filename for item in self.thumbnails().values()):
            return None
        return self.thumbnail_file(filename)

    def thumbnail_file(self, filename):
        path = safe_path(self.thumbnail_root, filename)
        return path if path.is_file() and path.stat().st_size > 0 else None

    def image_size(self, path, resource_id=None):
        """Read only the local image header; cache until the file changes."""
        if path is None:
            return None
        try:
            facts = path.stat()
            signature = (facts.st_mtime_ns, facts.st_size)
            key = str(path)
            with self.lock:
                cached = self.image_sizes.get(key)
                if cached and cached[0] == signature:
                    return cached[1]
            saved = self.persisted_sizes.get(resource_id, {})
            result = saved.get('size') if saved.get('signature') == list(signature) else None
            if not result:
                result = read_image_size(path)
            with self.lock:
                self.image_sizes[key] = (signature, result)
            return result
        except OSError:
            return None

    def index(self):
        with self.lock:
            manifest = self.snapshot if self.snapshot and self.snapshot.is_file() else self.manifest
            facts = manifest.stat()
            signature = (str(manifest), facts.st_mtime_ns, facts.st_size, facts.st_ino)
            if signature != self.signature:
                document = json.loads(manifest.read_text(encoding='utf-8'))
                resources = {r['id']: r for r in plan(document)}
                self.cover_paths = {digest(r['id']): r['paths'][0] for r in cover_plan(document)}
                self.paths = {digest(r['id']): r['paths'] for r in resources.values()}
                self.document, self.resources, self.signature = document, resources, signature
            return self.document, self.resources, self.paths

    def locate(self, key):
        _, _, paths = self.index()
        for relative in paths.get(key, []):
            path = safe_path(self.media_root, relative)
            if path.is_file() and path.stat().st_size > 0:
                return path
        return None

    def locate_cover(self, key):
        self.index()
        relative = self.cover_paths.get(key)
        if relative:
            path = safe_path(self.cover_root, relative)
            if path.is_file() and path.stat().st_size > 0:
                return path
        return None

    def read(self):
        # Read each time so Git updates and newly finished downloads appear on refresh.
        document, resources, _ = self.index()
        self.persisted_sizes = load_size_index(self.sizes_file)
        thumbnails = self.thumbnails()
        if document is not None:
            media, entries = {}, []
            for ident, entry in document['entries'].items():
                preview_path = None
                preview_id = None
                groups = [{'name': ' / '.join(folder[2:]) or entry['title'], 'resources': []}
                          for folder in entry['folders']]
                for raw in entry['resources']:
                    resource = resources[raw['id']]
                    key = digest(raw['id'])
                    available = False
                    for relative in resource['paths']:
                        path = safe_path(self.media_root, relative)
                        if path.is_file() and path.stat().st_size > 0:
                            media[key] = relative
                            available = True
                            if raw['kind'] == 'image' and preview_path is None:
                                preview_path = path
                                preview_id = raw['id']
                            break
                    value = {'id': key, 'kind': raw['kind'], 'filename': raw['filename'],
                             'available': available, 'url': '/media/' + key,
                             'size': path.stat().st_size if available else raw.get('size_bytes'),
                             'source': raw.get('source', 'wiki')}
                    if raw.get('caption'):
                        value['caption'] = raw['caption']
                    thumbnail = thumbnails.get(raw['id'])
                    if thumbnail and self.thumbnail_file(thumbnail['filename']):
                        value['poster'] = '/thumbnails/' + thumbnail['filename']
                    for index in ([raw['folder']] if 'folder' in raw else raw['folders']):
                        groups[index]['resources'].append(value)
                value = {'id': ident, 'title': entry['title'],
                         'categories': [str(c) for c in entry['category_ids']], 'groups': wallpaper_variants(groups)}
                cover_key = digest(f'wiki:{ident}:cover')
                cover_path = self.locate_cover(cover_key)
                if cover_path:
                    value['cover'] = '/covers/' + cover_key
                thumbnail_size = None
                if not cover_path and preview_path is None:
                    for video in preview_order(entry):
                        thumbnail = thumbnails.get(video['id'])
                        if thumbnail and self.thumbnail_file(thumbnail['filename']):
                            value['cover'] = '/thumbnails/' + thumbnail['filename']
                            thumbnail_size = thumbnail.get('size')
                            break
                original = resources.get(preview_id, {}) if not cover_path else {}
                preview_size = thumbnail_size or [original.get('width'), original.get('height')]
                if not all(type(value) is int and value > 0 for value in preview_size):
                    preview_size = self.image_size(cover_path or preview_path,
                                                   f'wiki:{ident}:cover' if cover_path else preview_id)
                if preview_size:
                    value['preview_size'] = preview_size
                entries.append(value)
            return {'generated_at': document.get('generated_at'), 'categories': document['categories'],
                    'debug': self.debug, 'entries': entries}, media


def byte_range(value, size):
    """Parse one HTTP byte range; native video seeking needs 206 responses."""
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
    if not match or not any(match.groups()) or size == 0:
        raise ValueError('Invalid byte range')
    left, right = match.groups()
    if not left:
        length = int(right)
        if length <= 0:
            raise ValueError('Invalid suffix')
        return max(0, size - length), size - 1
    start, end = int(left), min(int(right), size - 1) if right else size - 1
    if start >= size or start > end:
        raise ValueError('Unsatisfiable range')
    return start, end


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, catalog, **kwargs):
        self.catalog = catalog
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        # Video chunk requests are deliberately quiet; report actual errors only.
        if args and str(args[1] if len(args) > 1 else '').startswith(('4', '5')):
            logging.warning(fmt, *args)

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self'; media-src 'self'; "
                         "script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; "
                         "base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def do_HEAD(self):
        self.respond(head=True)

    def do_GET(self):
        self.respond()

    def respond(self, head=False):
        route = urlsplit(self.path).path
        try:
            if route == '/api/catalog':
                catalog, _ = self.catalog.read()
                data = json.dumps(catalog, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                if not head:
                    self.wfile.write(data)
            elif route in STATIC:
                self.serve_file(WEB / STATIC[route], head)
            elif re.fullmatch(r'/media/[0-9a-f]{64}', route):
                path = self.catalog.locate(route.rsplit('/', 1)[1])
                if path is None:
                    self.send_error(404, 'Media has not been downloaded')
                    return
                self.serve_file(path, head)
            elif re.fullmatch(r'/covers/[0-9a-f]{64}', route):
                path = self.catalog.locate_cover(route.rsplit('/', 1)[1])
                if path is None:
                    self.send_error(404, 'Cover has not been downloaded')
                    return
                self.serve_file(path, head)
            elif re.fullmatch(r'/thumbnails/[0-9a-f]{64}\.(?:png|jpg)', route):
                path = self.catalog.locate_thumbnail(route.rsplit('/', 1)[1])
                if path is None:
                    self.send_error(404, 'Thumbnail has not been generated')
                    return
                self.serve_file(path, head)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except FileNotFoundError:
            self.send_error(404, 'Local file not found')
        except (ValueError, KeyError, OSError):
            logging.exception('Cannot read local Wiki data')
            self.send_error(503, 'Local Wiki data is unavailable')

    def serve_file(self, path, head):
        with path.open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            start, end, status = 0, size - 1, 200
            if self.headers.get('Range'):
                try:
                    start, end = byte_range(self.headers['Range'], size)
                    status = 206
                except ValueError:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
            self.send_response(status)
            self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-cache')
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            if not head:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = stream.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=8080, type=int)
    parser.add_argument('--manifest', default='metafiles/wiki.json')
    parser.add_argument('--snapshot', default='state/wiki.json', help='Prefer the downloader local manifest snapshot')
    parser.add_argument('--covers', default='state/covers', help='Read-only local cover cache directory')
    parser.add_argument('--downloads', default=os.environ.get('DOWNLOAD_PATH', 'downloads'))
    parser.add_argument('--debug', action='store_true',
                        default=os.environ.get('WIKI_DEBUG', '').lower() in ('1', 'true', 'yes'),
                        help='Show resource and download counts in the Wiki interface')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    catalog = Catalog(args.manifest, args.downloads, args.snapshot, args.covers, debug=args.debug)
    document, _ = catalog.read()
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, catalog=catalog))
    logging.info('Local media Wiki: http://%s:%s (%s entries)', args.host, args.port, len(document['entries']))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
