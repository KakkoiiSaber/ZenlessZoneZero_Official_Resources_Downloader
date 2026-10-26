"""Small, bounded metadata client with resumable response caching."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.parse import urlencode

import requests


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def atomic_json(path, value, *, record_maps=()):
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf8', newline='\n') as stream:
            if record_maps:
                # One compact record per line keeps Git diffs useful without nested whitespace.
                encode = lambda item: json.dumps(item, ensure_ascii=False, separators=(',', ':'))
                stream.write('{\n')
                for index, (key, item) in enumerate(value.items()):
                    stream.write('  '+encode(key)+':')
                    if key in record_maps:
                        stream.write('{\n')
                        for position, (ident, record) in enumerate(item.items()):
                            stream.write('    '+encode(ident)+':'+encode(record)
                                         + (',' if position+1<len(item) else '')+'\n')
                        stream.write('  }')
                    else:
                        stream.write(encode(item))
                    stream.write((',' if index+1<len(value) else '')+'\n')
                stream.write('}\n')
            else:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Antivirus/indexer handles can briefly prevent Windows file replacement.
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(.05 * (attempt+1))
    finally:
        Path(temporary).unlink(missing_ok=True)


class FetchError(RuntimeError):
    pass


class Client:
    def __init__(self, cache, started_at, interval=.15, transport='auto'):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.started_at = started_at
        self.interval = interval
        self.lock = threading.Lock()
        self.last_request = 0.
        self.requests = 0
        self.cache_hits = 0
        self.local = threading.local()
        self.curl = shutil.which('curl.exe' if os.name == 'nt' else 'curl')
        self.transport = ('curl' if os.name == 'nt' else 'requests') if transport == 'auto' else transport

    def get(self, url, params=None, cache=True):
        full = url + ('?' + urlencode(params) if params else '')
        path = self.cache / (digest(full) + '.json')
        if cache and path.exists() and path.stat().st_mtime >= self.started_at:
            with self.lock:
                self.cache_hits += 1
            return json.loads(path.read_text(encoding='utf8'))
        last = None
        for attempt in range(3):
            try:
                with self.lock:
                    delay = self.interval - (time.monotonic() - self.last_request)
                    if delay > 0:
                        time.sleep(delay)
                    self.last_request = time.monotonic()
                    self.requests += 1
                if self.transport == 'curl':
                    if not self.curl:
                        raise FetchError('curl_unavailable')
                    result = subprocess.run([self.curl, '-4', '-sS', '--fail', '--connect-timeout', '4',
                        '--max-time', '25', '--max-filesize', str(16*1024**2),
                        '-H', 'User-Agent: Mozilla/5.0', '-H', 'Referer: https://baike.mihoyo.com/',
                        '-H', 'x-rpc-wiki_app: zzz', full], capture_output=True, timeout=30)
                    if result.returncode:
                        raise FetchError('http_transport_' + str(result.returncode))
                    raw = result.stdout
                else:
                    if not hasattr(self.local, 'session'):
                        self.local.session = requests.Session()
                        self.local.session.headers.update({'User-Agent': 'Mozilla/5.0',
                            'Referer': 'https://baike.mihoyo.com/', 'x-rpc-wiki_app': 'zzz'})
                    with self.local.session.get(full, timeout=(4, 25), stream=True) as response:
                        response.raise_for_status()
                        raw = bytearray()
                        for chunk in response.iter_content(65536):
                            raw.extend(chunk)
                            if len(raw) > 16*1024**2:
                                raise FetchError('response_size_limit')
                if len(raw) > 16*1024**2:
                    raise FetchError('response_size_limit')
                body = json.loads(raw)
                if not isinstance(body, dict) or body.get('retcode') != 0 or not isinstance(body.get('data'), dict):
                    raise FetchError('api_' + str(body.get('retcode') if isinstance(body, dict) else 'invalid'))
                atomic_json(path, body['data'])
                return body['data']
            except (requests.RequestException, ValueError, FetchError, subprocess.SubprocessError) as error:
                last = error
                if isinstance(error, FetchError) and str(error) == 'api_1034':
                    break  # Respect validation/rate control; do not repeatedly challenge it.
                time.sleep(.5 * (attempt + 1))
        raise FetchError(str(last) if isinstance(last, FetchError) else type(last).__name__)
