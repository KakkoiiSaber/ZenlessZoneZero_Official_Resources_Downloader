"""Bounded public metadata and exact-share access-code resolution; no binary fetches."""
from __future__ import annotations
import hashlib
import json
import re
import time
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from src.downloader.minas_files import normalize_share


def stable_id(*parts):
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()


def share_url(value):
    parsed = urlparse(str(value).strip().strip('\"\''))
    if parsed.query or parsed.fragment:
        raise ValueError('Share URL must not contain query credentials')
    return normalize_share(value)


class PublicPostUnavailable(RuntimeError):
    """Actionable public metadata failure without response bodies or secret URLs."""
    def __init__(self, reason, api_code=None):
        self.reason = reason
        self.api_code = api_code if isinstance(api_code, int) and not isinstance(api_code, bool) else None
        super().__init__(reason)


def bounded_json(session, url, maximum=32 * 1024 ** 2, deadline=None, **kwargs):
    remaining = deadline - time.monotonic() if deadline is not None else 30
    if remaining <= 0:
        raise TimeoutError('Metadata deadline exceeded')
    with session.get(url, stream=True, timeout=(min(20, remaining), min(30, remaining)), **kwargs) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_content(65536):
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError('Metadata deadline exceeded')
            body.extend(chunk)
            if len(body) > maximum:
                raise ValueError('Metadata exceeds size limit')
        return json.loads(body)


class MinasSession(requests.Session):
    def send(self, request, **kwargs):
        p = urlparse(request.url)
        if p.scheme != 'https' or p.netloc != 'minas.mihoyo.com':
            raise ValueError('Unexpected Minas redirect host')
        return super().send(request, **kwargs)


class MetadataSession(MinasSession):
    def send(self, request, **kwargs):
        path=urlparse(request.url).path
        if not (re.fullmatch(r'/d/[A-Za-z0-9]+/?',path) or
                re.fullmatch(r'/api/v2\.1/share-links/[A-Za-z0-9]+/dirents/',path)):
            raise ValueError('Media transfer route forbidden in metadata collector')
        return super().send(request,**kwargs)


def public_access_code(source, session_factory=requests.Session, deadline=None):
    """Read only the matching official post; never guess a code or mix shares."""
    failures = []
    for post in source['posts'][:8]:
        match = re.fullmatch(r'https://(?:www\.)?miyoushe\.com/zzz/article/(\d+)', post['post_url'])
        if not match:
            continue
        try:
            with session_factory() as session:
                data = bounded_json(session, 'https://bbs-api.miyoushe.com/post/wapi/getPostFull',
                                    maximum=2 * 1024 ** 2, deadline=deadline,
                                    params={'gids': 8, 'post_id': match[1], 'read': 1},
                                    headers={'Referer': 'https://www.miyoushe.com/'})
        except (requests.RequestException, ValueError):
            failures.append(PublicPostUnavailable('public_post_request_failed'))
            continue
        if not isinstance(data, dict) or data.get('retcode', 0) != 0:
            failures.append(PublicPostUnavailable('public_post_api_error', data.get('retcode') if isinstance(data, dict) else None))
            continue
        wrapper = data.get('data')
        post_wrapper = wrapper.get('post') if isinstance(wrapper, dict) else None
        content = post_wrapper.get('post') if isinstance(post_wrapper, dict) else None
        if not isinstance(content, dict):
            failures.append(PublicPostUnavailable('public_post_invalid_payload'))
            continue
        chunks = [str(content.get('content') or '')]
        try:operations = json.loads(content.get('structured_content') or '[]')
        except (ValueError,TypeError):operations=[]
        if not isinstance(operations,list):operations=[]
        for op in operations:
            if isinstance(op, dict):
                if isinstance(op.get('insert'), str):
                    chunks.append(op['insert'])
                if isinstance(op.get('attributes'), dict):
                    chunks.append(str(op['attributes'].get('link') or ''))
        text = '\n'.join(chunks)
        links = {share_url(m) for m in re.findall(r'https://minas\.mihoyo\.com/d/[A-Za-z0-9]+/?', text)}
        # Ambiguous multi-share posts need an explicit private mapping.
        if links != {source['share']}:
            continue
        plain = BeautifulSoup(text, 'html.parser').get_text(' ', strip=True)
        codes = set(re.findall(r'(?:提取码|访问码|密码|passcode|password)\s*[:：]?\s*([A-Za-z0-9]{3,20})', plain, re.I))
        if len(codes) == 1:
            return next(iter(codes))
        if not codes:
            return ''  # Matching public post; the share may not require a code.
    if failures:
        raise failures[-1]
    return None
