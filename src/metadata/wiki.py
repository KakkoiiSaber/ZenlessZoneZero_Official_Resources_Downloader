"""Bounded, public Wiki metadata. Never request image/video URLs here."""
from __future__ import annotations
import hashlib
import json
import re
import time
import sys
from urllib.parse import urlparse, parse_qsl
import requests
from bs4 import BeautifulSoup
from src.metadata.access import bounded_json

BLACKBOARD = 'https://act-api-takumi-static.mihoyo.com/common/blackboard/zzz_wiki'
EDITOR = 'https://act-api-takumi-static.mihoyo.com/hoyowiki/zzz'
MEDIA_CHANNELS = {73: '角色视频', 72: '游戏视频', 98: '壁纸收录', 99: '好感壁纸'}
MEDIA_HOSTS = {'act-upload.mihoyo.com', 'upload-bbs.miyoushe.com', 'upload-bbs.mihoyo.com',
               'webstatic.mihoyo.com', 'webstatic.hoyoverse.com', 'fastcdn.mihoyo.com'}
PAGE_HOSTS = {'baike.mihoyo.com', 'www.miyoushe.com', 'miyoushe.com', 'zzz.mihoyo.com', 'zenless.hoyoverse.com'}
EXTERNAL_VIDEO_HOSTS = {'www.bilibili.com', 'player.bilibili.com', 'v.qq.com', 'v.youku.com', 'www.youtube.com', 'youtu.be'}


class WikiError(RuntimeError):
    def __init__(self, reason, api_code=None):
        self.reason = reason
        self.api_code = api_code if isinstance(api_code, int) and not isinstance(api_code, bool) else None
        super().__init__(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def safe_text(value, maximum=240):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if re.search(r'https?://|提取码|访问码|密码|password|passcode|cookie|token=', text, re.I):
        return ''
    return ''.join(c for c in text if ord(c) >= 32)[:maximum]


def media_url(value):
    """Accept only persistent, explicitly observed official CDN URLs."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith('//'):
        value = 'https:' + value
    p = urlparse(value)
    if p.scheme != 'https' or p.netloc not in MEDIA_HOSTS or p.username or p.fragment:
        return None
    if any(key != 'x-oss-process' for key, _ in parse_qsl(p.query, keep_blank_values=True)):
        return None
    if not re.search(r'\.(?:png|jpe?g|webp|gif|avif|bmp|tiff?|mp4|webm|mov|mkv|m4v|avi)$', p.path, re.I):
        return None
    return value


def public_link(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith('//'):
        value = 'https:' + value
    p = urlparse(value)
    if p.scheme != 'https' or p.username or p.fragment:
        return None
    if p.netloc=='minas.mihoyo.com':
        return value.rstrip('/')+'/' if not p.query and re.fullmatch(r'/d/[A-Za-z0-9]+/?',p.path) else None
    if p.netloc == 'player.bilibili.com':
        values = dict(parse_qsl(p.query))
        bvid = values.get('bvid', '')
        if re.fullmatch(r'BV[A-Za-z0-9]+', bvid):return 'https://www.bilibili.com/video/' + bvid
        aid=values.get('aid','')
        return 'https://www.bilibili.com/video/av'+aid if aid.isdigit() else None
    if p.netloc not in PAGE_HOSTS | EXTERNAL_VIDEO_HOSTS or p.query:
        return None
    return value


def nodes(value):
    if isinstance(value, dict):
        yield value
        for key in ('children', 'list'):
            if isinstance(value.get(key), list):
                for child in value[key]:
                    yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


class WikiClient:
    def __init__(self, session=None, seconds=120, maximum=2 * 1024**2):
        self.session = session or requests.Session()
        self.deadline = time.monotonic() + seconds
        self.maximum = maximum
        self.requests = 0

    def close(self):
        self.session.close()

    def get(self, path, **params):
        allowed = {BLACKBOARD + '/v1/getChannelTree', BLACKBOARD + '/v1/home/content/list', EDITOR + '/wapi/entry_page'}
        if path not in allowed:
            raise WikiError('metadata_route_forbidden')
        self.requests += 1
        print(json.dumps({'event':'wiki_metadata_request','route':path.rsplit('/',1)[-1],
                          'entry_id':params.get('entry_page_id'),'channel_id':params.get('channel_id')}),file=sys.stderr,flush=True)
        body = bounded_json(self.session, path, maximum=self.maximum, deadline=self.deadline,
                            params={'app_sn': 'zzz_wiki', 'lang': 'zh-cn', **params},
                            headers={'Referer': 'https://baike.mihoyo.com/', 'x-rpc-wiki_app': 'zzz'}, allow_redirects=False)
        if not isinstance(body, dict) or body.get('retcode') != 0 or not isinstance(body.get('data'), dict):
            raise WikiError('wiki_api_error', body.get('retcode') if isinstance(body, dict) else None)
        return body['data']

    def tree(self):
        data = self.get(BLACKBOARD + '/v1/getChannelTree')
        if not isinstance(data.get('children'), list):
            raise WikiError('wiki_invalid_tree')
        return data

    def channel(self, ident):
        if ident not in {2, 13}:
            raise WikiError('wiki_channel_out_of_scope')
        data = self.get(BLACKBOARD + '/v1/home/content/list', channel_id=ident)
        if not isinstance(data.get('list'), list) or not data['list']:
            raise WikiError('wiki_invalid_channel')
        return data

    def detail(self, ident):
        if not str(ident).isdigit():
            raise WikiError('wiki_invalid_id')
        data = self.get(EDITOR + '/wapi/entry_page', entry_page_id=str(ident))
        page = data.get('page')
        if not isinstance(page, dict) or str(page.get('id')) != str(ident) or not isinstance(page.get('modules'), list):
            raise WikiError('wiki_invalid_entry')
        return page


def index_entries(data, allowed=MEDIA_CHANNELS):
    entries = {}
    channels = {}
    for channel in nodes(data.get('list', [])):
        if channel.get('id') not in allowed or not isinstance(channel.get('list'), list):
            continue
        cid = int(channel['id'])
        channels[str(cid)] = {'id': cid, 'name': safe_text(channel.get('name')), 'count': len(channel['list'])}
        for item in channel['list']:
            if not isinstance(item, dict) or not isinstance(item.get('content_id'), int) or not safe_text(item.get('title')):
                raise WikiError('wiki_invalid_index_item')
            ident = str(item['content_id'])
            entry = entries.setdefault(ident, {'id': ident, 'title': safe_text(item['title']), 'alias': safe_text(item.get('alias_name')),
                                               'wiki_url': 'https://baike.mihoyo.com/zzz/wiki/content/' + ident + '/detail',
                                               'channels': [], 'post_url': public_link(item.get('bbs_url')), 'summary': safe_text(item.get('summary'), 1000)})
            if cid not in entry['channels']:
                entry['channels'].append(cid)
    if not entries or not set(allowed) <= {int(k) for k in channels}:
        raise WikiError('wiki_incomplete_channels')
    return dict(sorted(entries.items())), channels


def extract_media(page):
    """Parse only explicit rich-text media, not UI icons or every URL in a page."""
    media = {}; links = set()
    layouts = {}
    layout = page.get('template_layout') or {}
    if not isinstance(layout,dict):
        raise WikiError('wiki_invalid_layout')
    for tab in layout.get('tab', []) or []:
        for group in tab.get('module_group', []):
            for module in group.get('module', []):
                layouts[str(module.get('id'))] = safe_text(group.get('name'))
    for module in page.get('modules', []):
        if not isinstance(module,dict) or not isinstance(module.get('components'),list):
            raise WikiError('wiki_invalid_module')
        if module.get('is_hidden'):
            continue
        section = safe_text(module.get('name')) or layouts.get(str(module.get('id')), '')
        for component in module.get('components', []):
            raw = component.get('data', {})
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                raise WikiError('wiki_invalid_component') from None
            if not isinstance(data, dict):
                raise WikiError('wiki_invalid_component')
            if component.get('component_id')=='map_desc':
                rows=data.get('list',[])
                if not isinstance(rows,list):raise WikiError('wiki_invalid_image_list')
                for row in rows:
                    if not isinstance(row,dict):raise WikiError('wiki_invalid_image_row')
                    value=media_url(row.get('image'))
                    if not value:continue
                    item=media.setdefault(value,{'url':value,'media_type':'image','sections':[],
                        'quality':{'status':'published_source','original_verified':False,'transformed':bool(urlparse(value).query)}})
                    evidence={'section':section,'caption':safe_text(row.get('tab_name')),'module_id':str(module.get('id',''))}
                    if evidence not in item['sections']:item['sections'].append(evidence)
            rich = data.get('rich_text')
            if not isinstance(rich, str):
                continue
            soup = BeautifulSoup(rich, 'html.parser')
            heading = section
            for element in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'img', 'video', 'source', 'iframe', 'a']):
                if element.name.startswith('h') and element.name != 'html':
                    heading = safe_text(element.get_text(' ', strip=True)) or heading
                    continue
                if element.name in ('a', 'iframe'):
                    link = public_link(element.get('href') or element.get('src'))
                    if link:
                        links.add(link)
                    continue
                value = media_url(element.get('src'))
                if not value:
                    continue
                kind = 'video' if re.search(r'\.(mp4|webm|mov|mkv|m4v|avi)$', urlparse(value).path, re.I) else 'image'
                item = media.setdefault(value, {'url': value, 'media_type': kind, 'sections': [],
                                               'quality': {'status': 'published_source', 'original_verified': False,
                                                           'transformed': bool(urlparse(value).query)}})
                caption = safe_text(element.get('alt') or element.get('title'))
                evidence = {'section': heading, 'caption': caption, 'module_id': str(module.get('id', ''))}
                if evidence not in item['sections']:
                    item['sections'].append(evidence)
                # HTML display dimensions are not asserted to be original pixel dimensions.
    ext = page.get('ext', {})
    post_ext = ext.get('post_ext') if isinstance(ext,dict) else None
    link = public_link(post_ext.get('url')) if isinstance(post_ext,dict) else None
    if link:
        links.add(link)
    return {'media': [media[url] for url in sorted(media)], 'links': sorted(links), 'entry_version': str(page.get('version', ''))[:80]}
