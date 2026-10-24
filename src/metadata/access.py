"""Bounded public metadata and exact-share access-code resolution; no binary fetches."""
from __future__ import annotations
import hashlib
import json
import re
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


def bounded_json(session, url, maximum=32 * 1024 ** 2, **kwargs):
    with session.get(url, stream=True, timeout=(20, 30), **kwargs) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_content(65536):
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


def public_access_code(source, session_factory=requests.Session):
    """Read only the matching official post; never guess a code or mix shares."""
    for post in source['posts'][:8]:
        match = re.fullmatch(r'https://(?:www\.)?miyoushe\.com/zzz/article/(\d+)', post['post_url'])
        if not match:
            continue
        with session_factory() as session:
            data = bounded_json(session, 'https://bbs-api.miyoushe.com/post/wapi/getPostFull',
                                maximum=2 * 1024 ** 2,
                                params={'gids': 8, 'post_id': match[1], 'read': 1},
                                headers={'Referer': 'https://www.miyoushe.com/'})
        content = data.get('data', {}).get('post', {}).get('post', {})
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
    return None
