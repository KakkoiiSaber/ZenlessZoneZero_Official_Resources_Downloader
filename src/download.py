"""Download the compact Wiki manifest; all operational state stays on the NAS."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from src.scrape.http import atomic_json

LOG = logging.getLogger('download')
DEFAULT_MANIFEST = 'https://raw.githubusercontent.com/KakkoiiSaber/ZenlessZoneZero_Official_Resources_Downloader/main/metafiles/wiki.json'
MAX_MANIFEST = 32 * 1024 * 1024
LOG_FORMAT = '[%(asctime)s][%(levelname)s] %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


def persistent_log_handler(state_dir):
    directory = state_dir / 'logs'
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / 'downloader.log', maxBytes=5 * 1024 * 1024,
                                  backupCount=4, encoding='utf-8')
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    return handler


def format_size(size):
    value = float(size)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if round(value, 2) < 1000 or unit == 'TB':
            return f'{int(value)} B' if unit == 'B' else f'{value:.2f} {unit}'
        value /= 1000


def format_progress(done, total=None):
    if not total:
        return f'{format_size(done)} (total unknown)'
    percent = min(100.0, max(0.0, done * 100 / total))
    if done < total:
        percent = min(percent, 99.9)  # Rounding must not imply completion before the last byte.
    return f'{format_size(done)} / {format_size(total)} ({percent:.1f}%)'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_url(url):
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if parts.hostname == 'prod-vod-sign.miyoushe.com':
        query = [(k, v) for k, v in query if k != 'auth_key']
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(query)), ''))


def fingerprint(resource):
    return digest(json.dumps([canonical_url(resource['url']), resource.get('size_bytes'),
                              resource.get('etag')], ensure_ascii=False))


def select_resources(resources, ids):
    if not ids:
        return resources
    indexed = {resource['id']: resource for resource in resources}
    if any(ident not in indexed for ident in ids):
        raise ValueError('Requested test resource is absent from the manifest')
    return [indexed[ident] for ident in dict.fromkeys(ids)]


def safe_path(root, relative):
    """Reject traversal and symlink escapes, including paths loaded from local state."""
    parts = relative.split('/')
    if any(not part or part in {'.', '..'} or re.search(r'[\\:\x00-\x1f]', part) for part in parts):
        raise ValueError('Unsafe download path')
    target = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symlinks are not allowed in managed download paths')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Download path escapes mounted directory')
    return target


def plan(document):
    if (document.get('schema_version') != 2 or document.get('source') != 'wiki'
            or document.get('status', 'complete') != 'complete'):
        raise ValueError('Expected a complete schema_version 2 Wiki manifest')
    result, claims = {}, {}
    for entry in document['entries'].values():
        if entry.get('status', 'ok') != 'ok':
            raise ValueError('Incomplete Wiki entry')
        for resource in entry['resources']:
            ident, url = resource['id'], resource['url']
            if not isinstance(ident, str) or not ident or ident in result:
                raise ValueError('Duplicate or invalid resource ID')
            parsed_url = urlsplit(url)
            if (parsed_url.scheme not in {'http', 'https'} or not parsed_url.hostname
                    or parsed_url.username or parsed_url.password or parsed_url.fragment):
                raise ValueError('Invalid resource URL')
            if resource['kind'] not in {'image', 'video'}:
                raise ValueError('Unknown resource kind')
            if resource.get('expires_at'):
                expiry = datetime.fromisoformat(resource['expires_at'].replace('Z', '+00:00'))
                if expiry.tzinfo is None:
                    raise ValueError('Media expiry must include a timezone')
            if 'size_bytes' in resource and (type(resource['size_bytes']) is not int or resource['size_bytes'] <= 0):
                raise ValueError('Invalid resource size')
            positions = [resource['folder']] if 'folder' in resource else resource['folders']
            paths = []
            if not positions:
                raise ValueError('Missing resource folders')
            for index in positions:
                if type(index) is not int or not 0 <= index < len(entry['folders']):
                    raise ValueError('Invalid resource folder index')
                directory = entry['folders'][index]
                if not isinstance(directory, list) or not directory:
                    raise ValueError('Invalid resource directory')
                segments = [*directory, resource['filename']]
                if not all(isinstance(s, str) and s and '/' not in s for s in segments):
                    raise ValueError('Invalid directory segments')
                relative = '/'.join(segments)
                safe_path(Path.cwd(), relative)
                # Reserve internal storage and reject case-insensitive collisions for NAS shares.
                key = relative.casefold()
                if key.split('/')[0] == '.zzz-partials' or (key in claims and claims[key] != ident):
                    raise ValueError('Conflicting download paths')
                claims[key] = ident
                if relative not in paths:
                    paths.append(relative)
            directory = entry['folders'][positions[0]]
            title = entry.get('title') or directory[min(1, len(directory) - 1)]
            label = ' / '.join([title, *directory[2:], resource['filename']])
            label = ' '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', label).split())
            result[ident] = {**resource, 'paths': paths, 'fingerprint': fingerprint(resource), 'label': label}
    return list(result.values())


def cover_plan(document, resource_ids=None):
    """Keep presentation covers outside the media plan and NAS media directories."""
    result = []
    wanted = set(resource_ids or [])
    for ident, entry in document['entries'].items():
        url = entry.get('cover_url')
        if not url or (wanted and not any(r['id'] in wanted for r in entry['resources'])):
            continue
        parsed = urlsplit(url)
        suffix = Path(parsed.path).suffix.lower()
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or suffix not in {'.jpg','.jpeg','.png','.webp','.gif','.avif'}):
            raise ValueError('Invalid cover URL')
        filename = digest(str(ident) + ':' + canonical_url(url)) + suffix
        resource = {'id': f'wiki:{ident}:cover', 'kind': 'image', 'url': url,
                    'paths': [filename], 'label': entry['title'] + ' / 封面'}
        result.append({**resource, 'fingerprint': fingerprint(resource)})
    return result


@contextmanager
def exclusive_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / 'downloader.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            handle.write(b'0')
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('Another downloader is using this state directory') from None
    try:
        yield
    finally:
        handle.close()


class State:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / 'downloads.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS resources (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS paths (path TEXT PRIMARY KEY, id TEXT NOT NULL)')
        self.db.commit()

    def get(self, ident):
        row = self.db.execute('SELECT data FROM resources WHERE id=?', (ident,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, ident, data):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO resources VALUES (?,?)',
                            (ident, json.dumps(data, ensure_ascii=False)))

    def owner(self, relative):
        row = self.db.execute('SELECT id FROM paths WHERE path=?', (relative.casefold(),)).fetchone()
        return row[0] if row else None

    def claim(self, relative, ident):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO paths VALUES (?,?)', (relative.casefold(), ident))

    def close(self):
        self.db.close()


def strong_etag(etag):
    return etag if etag and not etag.startswith('W/') else None


def file_hash(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


class Downloader:
    def __init__(self, root, state, *, retries=3, verify_days=7, rate_limit=0, progress_interval=60, stop=None):
        self.root, self.state = root.resolve(), state
        self.root.mkdir(parents=True, exist_ok=True)
        self.retries, self.verify_seconds = retries, verify_days * 86400
        self.rate_limit = rate_limit
        self.progress_interval = progress_interval
        self.stop = stop or threading.Event()
        self.http = requests.Session()
        self.http.headers.update({'User-Agent': 'zzz-scraper-downloader/1', 'Accept-Encoding': 'identity'})
        self.active_paths = {}

    def run(self, resources, *, quiet_skips=False):
        self.active_paths = {p.casefold(): r['id'] for r in resources for p in r['paths']}
        for resource in resources:
            for relative in resource['paths']:
                safe_path(self.root, relative)
        counts = {'downloaded': 0, 'skipped': 0, 'failed': 0}
        for resource in resources:
            if self.stop.is_set():
                break
            ident = resource['id']
            label = resource.get('label', ident)
            for attempt in range(self.retries):
                try:
                    action = self.sync(resource)
                    counts[action] += 1
                    if action == 'downloaded':
                        size = self.state.get(ident)['size']
                        LOG.info('downloaded %s %s', label, format_progress(size, size))
                    elif not quiet_skips:
                        LOG.info('%s %s', action, label)
                    break
                except InterruptedError:
                    self.stop.set()
                    break
                except (OSError, ValueError, requests.RequestException) as error:
                    # Never print signed URLs or request exceptions containing credentials.
                    detail = type(error).__name__
                    if isinstance(error, requests.RequestException):
                        if error.response is not None:
                            detail += f' HTTP {error.response.status_code}'
                    elif isinstance(error, ValueError):
                        detail = str(error)
                    LOG.warning('%s attempt %d/%d: %s', label, attempt + 1, self.retries,
                                detail)
                    if attempt + 1 == self.retries:
                        counts['failed'] += 1
                    elif self.stop.wait(min(2 ** attempt, 30)):
                        break
        LOG.info('Cycle finished: %s', counts)
        return counts

    def partial(self, resource):
        return safe_path(self.root, '.zzz-partials/' + digest(resource['id']) + '.part')

    def existing(self, record, ident):
        for relative in record.get('paths', []):
            path = safe_path(self.root, relative)
            if (self.state.owner(relative) == ident and path.is_file()
                    and path.stat().st_size == record.get('size')):
                return path
        return None

    def sync(self, resource):
        ident = resource['id']
        label = resource.get('label', ident)
        record = self.state.get(ident)
        source = self.existing(record, ident) if record.get('fingerprint') == resource['fingerprint'] else None
        if source and record.get('complete'):
            if time.time() - record.get('checked_at', 0) >= self.verify_seconds:
                if file_hash(source) != record['sha256']:
                    source = None
                else:
                    # A failed/expired HEAD is retried later; do not silently certify freshness.
                    self.check_expiry(resource)
                    with self.http.head(resource['url'], timeout=(15, 60), allow_redirects=True) as response:
                        if response.status_code in {405, 501}:
                            LOG.info('HEAD unavailable for %s; verified local SHA-256 only', label)
                        else:
                            response.raise_for_status()
                            remote_size = response.headers.get('Content-Length')
                            remote_etag = response.headers.get('ETag')
                            if ((remote_size and int(remote_size) != record['size'])
                                    or (remote_etag and record.get('etag') and remote_etag != record['etag'])
                                    or (response.headers.get('Last-Modified') and record.get('last_modified')
                                        and response.headers['Last-Modified'] != record['last_modified'])):
                                source = None
                            else:
                                record['etag'] = remote_etag or record.get('etag')
                                record['last_modified'] = response.headers.get('Last-Modified') or record.get('last_modified')
                    record['checked_at'] = time.time()
            if source:
                self.place(source, resource, record, verify_copies=time.time() - record.get('copies_checked_at', 0) >= self.verify_seconds)
                return 'skipped'
        self.check_expiry(resource)
        if record.get('fingerprint') != resource['fingerprint'] or record.get('complete'):
            self.partial(resource).unlink(missing_ok=True)
            record = {'fingerprint': resource['fingerprint'], 'complete': False, 'paths': record.get('paths', [])}
            self.state.save(ident, record)
        self.transfer(resource, record)
        part = self.partial(resource)
        record.update(size=part.stat().st_size, sha256=file_hash(part), complete=True, checked_at=time.time())
        self.place(part, resource, record)
        return 'downloaded'

    @staticmethod
    def check_expiry(resource):
        if resource.get('expires_at'):
            expiry = datetime.fromisoformat(resource['expires_at'].replace('Z', '+00:00'))
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                raise ValueError('Expired media URL; refresh the GitHub manifest first')

    def place(self, source, resource, record, verify_copies=False):
        ident = resource['id']
        downloaded = source == self.partial(resource)
        for relative in resource['paths']:
            destination = safe_path(self.root, relative)
            owner = self.state.owner(relative)
            if destination == source:
                continue
            if (not downloaded and destination.is_file() and owner == ident and record.get('complete')
                    and relative in self.state.get(ident).get('paths', [])
                    and self.state.get(ident).get('fingerprint') == resource['fingerprint']
                    and destination.stat().st_size == record['size']
                    and (not verify_copies or file_hash(destination) == record['sha256'])):
                continue
            if destination.exists() and owner is None:
                raise ValueError('Refusing to overwrite a file not managed by this downloader')
            if owner and owner != ident and self.active_paths.get(relative.casefold()) != ident:
                raise ValueError('Destination owned by another active resource')
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Record ownership first, so a crash after rename can be recovered safely.
            self.state.claim(relative, ident)
            if source == self.partial(resource):
                os.replace(source, destination)
                source = destination
            else:
                # Copy rather than hardlink: editing one placement must not corrupt another.
                temporary = self.partial(resource).with_suffix('.copy')
                temporary.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, temporary)
                with temporary.open('r+b') as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
        # Remove only old placements of this resource after all new placements succeed.
        old = self.state.get(ident)
        for relative in old.get('paths', []):
            if relative not in resource['paths'] and self.active_paths.get(relative.casefold()) is None:
                if self.state.owner(relative) == ident:
                    safe_path(self.root, relative).unlink(missing_ok=True)
        record['paths'] = resource['paths']
        if downloaded or verify_copies:
            record['copies_checked_at'] = time.time()
        self.state.save(ident, record)

    def transfer(self, resource, record):
        label = resource.get('label', resource['id'])
        part = self.partial(resource)
        part.parent.mkdir(parents=True, exist_ok=True)
        offset = part.stat().st_size if part.exists() else 0
        validator = strong_etag(record.get('etag'))
        # Without a strong validator, restart to avoid combining different representations.
        if offset and not validator:
            part.unlink()
            offset = 0
        headers = {'Range': f'bytes={offset}-', 'If-Range': validator} if offset else {}
        LOG.info('%s %s at %s', label, 'resume' if offset else 'start',
                 format_progress(offset, resource.get('size_bytes') or record.get('total')))
        with self.http.get(resource['url'], headers=headers, stream=True, timeout=(15, 60)) as response:
            if response.status_code == 416 and offset:
                match = re.fullmatch(r'bytes \*/(\d+)', response.headers.get('Content-Range', ''))
                if (match and int(match[1]) == offset and record.get('total') == offset
                        and response.headers.get('ETag') == validator):
                    return
                part.unlink(missing_ok=True)
                raise ValueError('Unsatisfied range; restarting download')
            response.raise_for_status()
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError('Unexpected compressed media response')
            content_type = response.headers.get('Content-Type', '').split(';')[0].lower()
            if content_type.startswith('text/') or content_type in {'application/json', 'application/xml'}:
                raise ValueError('Server returned a document instead of media')
            length = response.headers.get('Content-Length')
            length = int(length) if length is not None else None
            if response.status_code == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                if not match:
                    raise ValueError('Invalid Content-Range')
                start, end, total = map(int, match.groups())
                if (start != offset or end < start or end >= total
                        or (length is not None and length != end - start + 1)
                        or (offset and (response.headers.get('ETag') != validator
                                        or record.get('total') != total))):
                    part.unlink(missing_ok=True)
                    raise ValueError('Range or validator mismatch; restarting download')
            elif response.status_code == 200:
                if offset:
                    LOG.info('%s server restarted body; writing from zero', label)
                offset, total = 0, length  # Server ignored Range / If-Range detected a new version.
            else:
                raise ValueError('Expected HTTP 200 or 206')
            expected = resource.get('size_bytes')
            if expected and total is not None and expected != total:
                part.unlink(missing_ok=True)
                raise ValueError('Media size differs from manifest; refresh metadata')
            if resource.get('etag') and response.headers.get('ETag') and resource['etag'] != response.headers['ETag']:
                part.unlink(missing_ok=True)
                raise ValueError('Media ETag differs from manifest; refresh metadata')
            with part.open('ab' if offset else 'wb') as handle:
                # Truncate an obsolete body before saving the replacement's validator.
                record.update(etag=response.headers.get('ETag'), last_modified=response.headers.get('Last-Modified'), total=total)
                self.state.save(resource['id'], record)
                if offset:
                    LOG.info('%s validated HTTP 206; continuing at %s', label,
                             format_progress(offset, total))
                started, transferred, reported = time.monotonic(), 0, time.monotonic()
                chunk_size = min(256 * 1024, self.rate_limit) if self.rate_limit else 256 * 1024
                for chunk in response.iter_content(chunk_size):
                    if self.stop.is_set():
                        raise InterruptedError()
                    handle.write(chunk)
                    transferred += len(chunk)
                    if self.rate_limit:
                        delay = transferred / self.rate_limit - (time.monotonic() - started)
                        if delay > 0 and self.stop.wait(delay):
                            raise InterruptedError()
                    if self.progress_interval and time.monotonic() - reported >= self.progress_interval:
                        LOG.info('%s progress %s', label,
                                 format_progress(offset + transferred, total or expected))
                        reported = time.monotonic()
                handle.flush()
                os.fsync(handle.fileno())
            size = part.stat().st_size
            if not size or (total is not None and size != total) or (expected and size != expected):
                raise ValueError('Incomplete media body')


def load_manifest(url, filename=None, progress_interval=60):
    if filename:
        LOG.info('Reading local manifest')
        if filename.stat().st_size > MAX_MANIFEST:
            raise ValueError('Manifest exceeds 32 MiB')
        return json.loads(filename.read_text(encoding='utf-8'))
    LOG.info('Fetching download manifest')
    with requests.get(url, stream=True, timeout=(15, 60), headers={'Accept-Encoding': 'identity'}) as response:
        response.raise_for_status()
        length = response.headers.get('Content-Length', '')
        total = int(length) if length.isdigit() else None
        LOG.info('Manifest response received; reading body')
        data = bytearray()
        reported = time.monotonic()
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_MANIFEST:
                raise ValueError('Manifest exceeds 32 MiB')
            if progress_interval and time.monotonic() - reported >= progress_interval:
                LOG.info('Manifest progress %s', format_progress(len(data), total))
                reported = time.monotonic()
    LOG.info('Manifest received: %s', format_size(len(data)))
    return json.loads(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest-url', default=os.getenv('MANIFEST_URL', DEFAULT_MANIFEST))
    parser.add_argument('--manifest-file', type=Path)
    parser.add_argument('--download-dir', type=Path, default=Path(os.getenv('DOWNLOAD_DIR', '/downloads')))
    parser.add_argument('--state-dir', type=Path, default=Path(os.getenv('STATE_DIR', '/state')))
    parser.add_argument('--verify-days', type=float, default=float(os.getenv('VERIFY_DAYS', '7')))
    parser.add_argument('--retries', type=int, default=3)
    parser.add_argument('--resource-id', action='append', help='Only download these exact IDs (repeat for a small deployment test)')
    parser.add_argument('--rate-limit', type=int, default=0, help='Maximum streamed media bytes/second per transfer; 0 disables throttling')
    parser.add_argument('--progress-interval', type=float, default=float(os.getenv('PROGRESS_INTERVAL', '60')),
                        help='Seconds between progress messages; 0 disables intermediate progress')
    parser.add_argument('--once', action='store_true', help=argparse.SUPPRESS)  # Older deployment commands remain valid.
    parser.add_argument('--dry-run', action='store_true', help='Validate and count manifest resources without downloading')
    args = parser.parse_args()
    if args.verify_days < 0 or args.retries < 1 or args.rate_limit < 0 or args.progress_interval < 0:
        parser.error('retries must be positive; verify-days, rate-limit and progress-interval must be nonnegative')
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    LOG.info('Downloader started')
    file_handler = None
    try:
        if args.dry_run:
            document = load_manifest(args.manifest_url, args.manifest_file, args.progress_interval)
            resources = select_resources(plan(document), args.resource_id)
            covers = cover_plan(document, args.resource_id)
            LOG.info('Valid manifest: %d resources, %d placements', len(resources), sum(len(r['paths']) for r in resources))
            LOG.info('Valid cover cache: %d covers', len(covers))
            return 0
        LOG.info('Preparing local download state')
        with exclusive_lock(args.state_dir):
            file_handler = persistent_log_handler(args.state_dir)
            logging.getLogger().addHandler(file_handler)
            LOG.info('Download run started; persistent log enabled')
            state = State(args.state_dir)
            worker = Downloader(args.download_dir, state, retries=args.retries, verify_days=args.verify_days,
                                rate_limit=args.rate_limit, progress_interval=args.progress_interval, stop=stop)
            try:
                LOG.info('Local download state ready')
                document = load_manifest(args.manifest_url, args.manifest_file, args.progress_interval)
                resources = select_resources(plan(document), args.resource_id)
                covers = cover_plan(document, args.resource_id)
                # Share the validated catalog with the offline Wiki, even without a Git pull.
                atomic_json(args.state_dir / 'wiki.json', document, record_maps=('entries',))
                LOG.info('Manifest ready: %d resources, %d placements', len(resources),
                         sum(len(resource['paths']) for resource in resources))
                failed = 0
                if covers:
                    cover_root = args.state_dir / 'covers'
                    cover_state = State(cover_root)
                    cover_worker = Downloader(cover_root, cover_state, retries=args.retries,
                        verify_days=args.verify_days, rate_limit=args.rate_limit,
                        progress_interval=args.progress_interval, stop=stop)
                    try:
                        LOG.info('Updating local cover cache: %d covers', len(covers))
                        failed += cover_worker.run(covers, quiet_skips=True)['failed']
                    finally:
                        cover_worker.http.close()
                        cover_state.close()
                failed += worker.run(resources)['failed']
                return int(bool(failed or stop.is_set()))
            except (OSError, ValueError, KeyError, TypeError, requests.RequestException) as error:
                LOG.error('Download run failed: %s', type(error).__name__)
                return 1
            finally:
                worker.http.close()
                state.close()
    except (OSError, RuntimeError) as error:
        LOG.error('%s', error)
        return 1
    finally:
        if file_handler is not None:
            logging.getLogger().removeHandler(file_handler)
            file_handler.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
