"""Offline, read-only media Wiki. Never fetches media or metadata from the Internet."""
import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from src.download import LOG_DATE_FORMAT, LOG_FORMAT, cover_plan, digest, plan, safe_path

WEB = Path(__file__).resolve().parent.parent / 'web'
STATIC = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js',
          '/style.css': 'style.css', '/assets/background.png': 'assets/background.png',
          '/assets/zzz-logo.svg': 'assets/zzz-logo.svg'}


class Catalog:
    def __init__(self, manifest, media_root, snapshot=None, covers='state/covers'):
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
        if document is not None:
            media, entries = {}, []
            for ident, entry in document['entries'].items():
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
                            break
                    value = {'id': key, 'kind': raw['kind'], 'filename': raw['filename'],
                             'available': available, 'url': '/media/' + key,
                             'size': path.stat().st_size if available else raw.get('size_bytes'),
                             'source': raw.get('source', 'wiki')}
                    if raw.get('caption'):
                        value['caption'] = raw['caption']
                    for index in ([raw['folder']] if 'folder' in raw else raw['folders']):
                        groups[index]['resources'].append(value)
                value = {'id': ident, 'title': entry['title'],
                         'categories': [str(c) for c in entry['category_ids']], 'groups': groups}
                cover_key = digest(f'wiki:{ident}:cover')
                if self.locate_cover(cover_key):
                    value['cover'] = '/covers/' + cover_key
                entries.append(value)
            return {'generated_at': document.get('generated_at'), 'categories': document['categories'],
                    'entries': entries}, media


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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    catalog = Catalog(args.manifest, args.downloads, args.snapshot, args.covers)
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
