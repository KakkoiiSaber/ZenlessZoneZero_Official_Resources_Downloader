"""Pure article/Wiki parsing and conservative cross-source video matching."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
import re
import unicodedata
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup

from src.scrape.http import digest

MEDIA_HOSTS = {'act-upload.mihoyo.com', 'upload-bbs.miyoushe.com', 'upload-bbs.mihoyo.com',
    'webstatic.mihoyo.com', 'webstatic.hoyoverse.com', 'fastcdn.mihoyo.com',
    'vod-static.miyoushe.com', 'vod-static.mihoyo.com', 'prod-vod-sign.miyoushe.com'}
SIGNED_VIDEO_HOSTS = {'prod-vod-sign.miyoushe.com'}
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.bmp', '.tif', '.tiff'}
VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.mkv', '.m4v', '.avi'}


def extension(url):
    path = urlparse(url).path
    ext = '.' + path.rsplit('.', 1)[-1].lower() if '.' in path.rsplit('/', 1)[-1] else ''
    return ext if ext in IMAGE_EXTS | VIDEO_EXTS else ''


def media_url(value, kind=None):
    if not isinstance(value, str):
        return None
    p = urlparse('https:' + value if value.startswith('//') else value)
    if p.scheme != 'https' or p.netloc not in MEDIA_HOSTS or p.username or p.fragment:
        return None
    ext = extension(value)
    signed_video = kind == 'video' and p.netloc in SIGNED_VIDEO_HOSTS and len(p.path) > 1
    if not signed_video and ext not in (IMAGE_EXTS if kind == 'image' else VIDEO_EXTS if kind == 'video' else IMAGE_EXTS | VIDEO_EXTS):
        return None
    # Remove only known image transforms. Preserve all video query parameters.
    if ext in IMAGE_EXTS:
        from urllib.parse import parse_qsl, urlencode
        query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != 'x-oss-process'])
        p = p._replace(query=query)
    return urlunparse(p)


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip().rstrip('. ')
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?', value, re.I):
        value = '_' + value
    return value[:100].rstrip('. ') or '_'


def operations(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def shares(html, structured=''):
    soup = BeautifulSoup(html, 'html.parser')
    chunks = []
    for element in soup.find_all(['p', 'a']):
        chunks.append(element.get_text(' ', strip=True) + ' ' + str(element.get('href') or ''))
    chunks.append(soup.get_text(' ', strip=True))
    for op in operations(structured):
        if isinstance(op, dict):
            if isinstance(op.get('insert'), str):
                chunks.append(op['insert'])
            chunks.append(str((op.get('attributes') or {}).get('link') or ''))
    text = '\n'.join(chunks) + '\n' + html
    matches = list(re.finditer(r'https://minas\.mihoyo\.com/d/[A-Za-z0-9]+/?', text))
    code_re = r'(?:提取码|访问码|密码|口令|passcode|password)\s*[:：]?\s*([A-Za-z0-9]{3,32})'
    all_codes = set(re.findall(code_re, BeautifulSoup(text, 'html.parser').get_text(' ', strip=True), re.I))
    result = {}
    for match in matches:
        url = match[0].rstrip('/') + '/'
        near = text[match.end():match.end()+240]
        next_link = re.search(r'https://minas\.', near)
        if next_link:
            near = near[:next_link.start()]
        codes = set(re.findall(code_re, BeautifulSoup(near, 'html.parser').get_text(' ', strip=True), re.I))
        code = next(iter(codes)) if len(codes) == 1 else next(iter(all_codes)) if len(all_codes) == 1 else None
        item = result.setdefault(url, {'url': url, 'password': None})
        if code:
            item['password'] = code
    return list(result.values())


def variant(value):
    url = media_url(value.get('url'), 'video')
    if not url:
        return None
    result = {'url': url}
    ext = extension(url) or '.' + str(value.get('format') or '').lstrip('.').lower()
    if ext in VIDEO_EXTS:
        result['extension'] = ext
    from urllib.parse import parse_qs
    auth_key = parse_qs(urlparse(url).query).get('auth_key', [''])[0]
    if auth_key and urlparse(url).netloc in SIGNED_VIDEO_HOSTS:
        try:
            result['expires_at'] = datetime.fromtimestamp(int(auth_key.split('-', 1)[0]), timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    for source, destination in [('width','width'),('height','height'),('bitrate','bitrate'),('size','size_bytes')]:
        try:
            number = int(value.get(source) or 0)
            if number > 0:
                result[destination] = number
        except (ValueError, TypeError):
            pass
    for key in ('codec', 'definition'):
        if value.get(key):
            result[key] = value[key]
    return result


def best_variant(values):
    return max(values, key=lambda v: tuple(v.get(k, 0) for k in ('height','width','bitrate','size_bytes'))) if values else None


def article(html, structured='', wrapper=None):
    soup = BeautifulSoup(html, 'html.parser')
    images = {}
    def image(value, caption='', **facts):
        url = media_url(value, 'image')
        if url and url not in images:
            images[url] = {'url': url, 'observed_url': value, 'caption': caption, **facts}
    for img in soup.find_all('img'):
        image(img.get('data-src') or img.get('src'), img.get('alt') or '')
    for op in operations(structured):
        if isinstance(op, dict) and isinstance(op.get('insert'), dict):
            insert = op['insert']
            if isinstance(insert.get('image'), str):
                attrs = op.get('attributes') or {}
                facts = {k: attrs[k] for k in ('width','height') if isinstance(attrs.get(k), int)}
                image(insert['image'], **facts)
    videos = []
    for i, video in enumerate(soup.find_all('video')):
        values = []
        for value in [video.get('src'), *[s.get('src') for s in video.find_all('source')]]:
            url = media_url(value, 'video')
            if url and url not in {v['url'] for v in values}:
                values.append({'url': url})
        if values:
            item = {'id': str(i), 'variants': values, 'selected': best_variant(values)}
            for previous in video.find_all_previous(['p','h1','h2','h3','h4','video']):
                if previous.name == 'video':
                    break
                if previous.find('video'):
                    continue
                text = previous.get_text(' ', strip=True)
                if not text:
                    continue
                ending = re.search(r'结局\s*[「“《]([^」”》]{1,40})[」”》]', text)
                if ending and len(text) <= 120:
                    item['caption'] = '结局：' + ending[1]
                break
            videos.append(item)
    for vod in (wrapper or {}).get('vod_list') or []:
        values = [parsed for raw in vod.get('resolutions', []) if (parsed := variant(raw))]
        if values:
            videos.append({'id': str(vod.get('id', len(videos))), 'duration_ms': vod.get('duration'),
                           'variants': values, 'selected': best_variant(values)})
    return {'images': list(images.values()), 'videos': videos, 'minas_links': shares(html, structured)}


def miyoushe_listing_article(wrapper):
    """The listing carries complete Quill ops; image IDs resolve through image_list."""
    from copy import deepcopy
    post = wrapper['post']
    ops = deepcopy(operations(post.get('structured_content')))
    if not ops:
        return None
    images = {str(image.get('image_id')): image for image in wrapper.get('image_list', [])}
    for op in ops:
        insert = op.get('insert')
        if isinstance(insert, dict) and isinstance(insert.get('image'), str):
            value = insert['image']
            if value.isdigit():
                if value not in images:
                    return None  # A genuinely incomplete list response needs the detail endpoint.
                image = images[value]
                insert['image'] = image['url']
                op['attributes'] = {**op.get('attributes', {}),
                    **{k: image[k] for k in ('width', 'height') if isinstance(image.get(k), int)}}
    parsed = article('', json.dumps(ops, ensure_ascii=False), wrapper)
    parsed['body_source'] = 'listing_structured_content'
    return parsed


def stable_listing_value(value):
    """Rotating CDN signatures and view counters do not mean the video changed."""
    from urllib.parse import parse_qsl, urlencode
    if isinstance(value, dict):
        return {k: stable_listing_value(v) for k, v in value.items() if k not in {'view_num'}}
    if isinstance(value, list):
        return [stable_listing_value(v) for v in value]
    if isinstance(value, str) and value.startswith('https://'):
        p = urlparse(value)
        if p.netloc in SIGNED_VIDEO_HOSTS:
            return urlunparse(p._replace(query=urlencode([(k,v) for k,v in parse_qsl(p.query) if k!='auth_key'])))
    return value


def signature(title):
    text = unicodedata.normalize('NFKC', title).casefold().replace('绝区零', '')
    text = re.sub(r'^\s*【已开奖】', '', text)
    return ''.join(c for c in text if c.isalnum())


def official_article(data):
    parsed = article(data.get('sContent') or '')
    extra = data.get('sExt') or {}
    extra = json.loads(extra) if isinstance(extra, str) else extra
    if not isinstance(extra, dict):
        raise ValueError('Invalid official content extension')
    existing = {v['selected']['url'] for v in parsed['videos']}
    for item in extra.get('video-url', []):
        url = media_url(item.get('url'), 'video')
        if url and url not in existing:
            parsed['videos'].append({'id': str(len(parsed['videos'])),
                'variants': [{'url': url}], 'selected': {'url': url}})
            existing.add(url)
    return parsed


def official_index(posts):
    index = defaultdict(list)
    for post in posts.values():
        index[signature(post['title'])].append(post)
    return index


def media_identity(post):
    """Require server-confirmed strong content validators, never just filename hashes."""
    result = set()
    for video in post.get('videos', []):
        value = video.get('selected') or {}
        facts = value.get('http_metadata') or {}
        etag = facts.get('etag') or ''
        size = facts.get('size_bytes')
        if (facts.get('status') != 200 or not re.fullmatch(r'"[a-fA-F0-9]{32}"', etag)
                or str(facts.get('content_type','')).split(';')[0].lower() not in
                    {'video/mp4','video/webm','video/quicktime','application/octet-stream'}
                or not isinstance(size, int) or isinstance(size, bool) or size <= 0):
            return None
        result.add((urlparse(value['url']).netloc, etag.strip('"').lower(), size))
    return frozenset(result) if result else None


def verified_video_profile(post):
    if len(post.get('videos', [])) != 1:
        return None
    value = post['videos'][0].get('selected') or {}
    profile = value.get('video_metadata') or {}
    facts = value.get('http_metadata') or {}
    if (not profile or profile.get('source_etag') != facts.get('etag')
            or profile.get('source_size_bytes') != facts.get('size_bytes')):
        return None
    if not all(profile.get(key) for key in ('duration_ms','width','height','codec','average_bitrate')):
        return None
    return profile


def match_official(entry, linked_posts, index):
    for title in [*[p['title'] for p in linked_posts], entry['title']]:
        candidates = index.get(signature(title), [])
        if len(candidates) == 1:
            return {'status': 'matched', 'method': 'normalized_title', 'post_id': candidates[0]['id'],
                    'url': candidates[0]['url'], 'candidates': [candidates[0]['url']]}
        if len(candidates) > 1:
            playable = [p for p in candidates if p.get('status') == 'ok' and p.get('videos')]
            identities = [media_identity(p) for p in playable]
            if (len(playable) == len(candidates) and identities and all(identities)
                    and len(set(identities)) == 1):
                chosen = min(playable, key=lambda p: (p.get('source_kind') != 'news', int(p['id'])))
                return {'status': 'matched', 'method': 'same_title_and_http_media_identity',
                        'post_id': chosen['id'], 'url': chosen['url'],
                        'candidates': [p['url'] for p in candidates],
                        'equivalent_post_ids': [p['id'] for p in candidates],
                        'evidence': [{'host': host, 'etag': etag, 'size_bytes': size}
                                     for host, etag, size in sorted(identities[0])]}
            profiles = [verified_video_profile(post) for post in playable]
            linked_durations = [v['duration_ms'] for post in linked_posts for v in post.get('videos', [])
                                if v.get('duration_ms')]
            if (len(playable) == len(candidates) and profiles and all(profiles)
                    and len({profile['codec'] for profile in profiles}) == 1
                    and len(linked_durations) == 1
                    and all(abs(profile['duration_ms']-linked_durations[0]) <= 500 for profile in profiles)):
                chosen = max(zip(playable, profiles), key=lambda pair: (
                    pair[1]['width']*pair[1]['height'], pair[1]['average_bitrate'],
                    pair[0].get('source_kind') == 'news'))[0]
                return {'status': 'matched', 'method': 'same_title_duration_and_video_profile',
                        'post_id': chosen['id'], 'url': chosen['url'],
                        'candidates': [post['url'] for post in candidates],
                        'corresponding_post_ids': [post['id'] for post in candidates],
                        'selection_rule': 'highest_resolution_then_average_bitrate_same_codec',
                        'evidence': [{'post_id': post['id'], **profile} for post, profile in zip(playable, profiles)]}
            if len(playable) == 1:
                return {'status': 'matched', 'method': 'normalized_title_and_unique_video',
                        'post_id': playable[0]['id'], 'url': playable[0]['url'],
                        'candidates': [p['url'] for p in candidates]}
            # Exact duplicate titles may be disambiguated by the linked post date.
            dates = [p.get('published_at', '')[:10] for p in linked_posts]
            dated = [p for p in candidates if p.get('published_at', '')[:10] in dates]
            if len(dated) == 1:
                return {'status': 'matched', 'method': 'normalized_title_and_date', 'post_id': dated[0]['id'],
                        'url': dated[0]['url'], 'candidates': [p['url'] for p in candidates]}
            return {'status': 'ambiguous', 'candidates': [p['url'] for p in candidates]}
    key = signature(linked_posts[0]['title'] if linked_posts else entry['title'])
    nearby = sorted(((SequenceMatcher(None, key, k).ratio(), p['url']) for k, ps in index.items()
                      for p in ps if key and SequenceMatcher(None, key, k).ratio() >= .8), reverse=True)
    return {'status': 'unmatched', 'candidates': [url for _, url in nearby[:5]]}


def wiki_media(page):
    """Use template order and parent groups; never sort pictures by their URL."""
    modules = {str(m['id']): m for m in page.get('modules', [])}
    ordered = []
    seen = set()
    for tab in (page.get('template_layout') or {}).get('tab', []):
        groups = {str(g['module_group_id']): g for g in tab.get('module_group', [])}
        def group_path(group):
            path = []
            visited = set()
            while group:
                ident = str(group['module_group_id'])
                if ident in visited:
                    raise ValueError('Cyclic Wiki module group')
                visited.add(ident)
                if group.get('name'):
                    path.insert(0, group['name'])
                group = groups.get(str(group.get('parent_group_id')))
            return path
        for group in tab.get('module_group', []):
            for ref in group.get('module', []):
                ident = str(ref['id'])
                if ident in modules and ident not in seen:
                    ordered.append((modules[ident], group_path(group)))
                    seen.add(ident)
    ordered.extend((m, [m['name']] if m.get('name') else [])
                   for ident, m in modules.items() if ident not in seen)
    result = []
    links = []
    for module, path in ordered:
        if module.get('is_hidden'):
            continue
        ordinal = 0
        def add(raw, kind, caption='', subpath=None, dimensions=None):
            nonlocal ordinal
            url = media_url(raw, kind)
            if not url:
                return
            ordinal += 1
            result.append({'id': f"wiki:{page['id']}:module:{module['id']}:{ordinal}",
                'kind': kind, 'url': url, 'observed_url': raw, 'caption': caption,
                'module_id': str(module['id']), 'order': ordinal, 'section_path': subpath or path,
                'filename': f'{ordinal:03d}' + extension(url), 'selected_source': 'wiki',
                **{k:v for k,v in (dimensions or {}).items() if k in ('width','height') and type(v) is int and v > 0}})
        for component in module.get('components', []):
            data = component.get('data') or {}
            data = json.loads(data) if isinstance(data, str) else data
            if not isinstance(data, dict):
                raise ValueError('Invalid Wiki component')
            if component.get('component_id') == 'map_desc':
                for row in data.get('list', []):
                    add(row.get('image'), 'image', row.get('tab_name') or '', dimensions=row)
            if isinstance(data.get('rich_text'), str):
                soup = BeautifulSoup(data['rich_text'], 'html.parser')
                heading = path
                for element in soup.find_all(['h1','h2','h3','h4','h5','h6','img','video','source','a','iframe']):
                    if re.fullmatch('h[1-6]', element.name):
                        heading = path + [element.get_text(' ', strip=True)]
                    elif element.name in ('a','iframe'):
                        value = element.get('href') or element.get('src') or ''
                        if re.fullmatch(r'https://(?:www\.)?miyoushe\.com/zzz/article/\d+', value):
                            links.append(value)
                    else:
                        add(element.get('data-src') or element.get('src'),
                            'image' if element.name == 'img' else 'video', element.get('alt') or '', heading)
    ext = (page.get('ext') or {}).get('post_ext') or {}
    if ext.get('url'):
        links.append(ext['url'])
    return result, list(dict.fromkeys(links))


def resolve_parsed_wiki(entry, miyoushe, official, index):
    from copy import deepcopy
    resources = deepcopy(entry.get('wiki_resources', []))
    for resource in resources:
        resource.pop('directories', None)
    links = entry.get('detail_links', [])
    links = list(dict.fromkeys([*([entry['miyoushe_url']] if entry.get('miyoushe_url') else []), *links]))
    linked = [miyoushe[url.rsplit('/', 1)[-1]] for url in links if url.rsplit('/', 1)[-1] in miyoushe]
    result = {**entry, 'miyoushe_posts': links, 'resources': resources}
    if any(cid in (72, 73) for cid in entry['category_ids']):
        match = match_official(entry, linked, index)
        result['official_match'] = match
        source_post = official.get(match.get('post_id')) if match['status'] == 'matched' else None
        selected = source_post if source_post and source_post.get('status') == 'ok' and source_post.get('videos') else None
        provider = 'official' if selected else 'miyoushe'
        posts = [selected] if selected else [p for p in linked if p.get('status') == 'ok']
        videos = []
        for post in posts:
            for i, video in enumerate(post.get('videos', [])):
                if video.get('selected'):
                    chosen = video['selected']
                    videos.append({'id': f"wiki:{entry['id']}:video:{post['id']}:{video['id']}",
                        'kind': 'video', **chosen, 'selected_source': provider, 'source_post': post['url'],
                        **({'caption':video['caption']} if video.get('caption') else {}),
                        'duration_ms': video.get('duration_ms'), 'section_path': [],
                        'filename': f'{i+1:03d}' + (chosen.get('extension') or extension(chosen['url']))})
        if videos:
            result['resources'] = [r for r in resources if r['kind'] != 'video'] + videos
        result['selection_status'] = ('official' if selected else 'miyoushe_fallback' if videos
                                      else 'wiki_direct' if any(r['kind']=='video' for r in resources) else 'unresolved')
        if source_post and source_post.get('status') != 'ok':
            result['official_unavailable'] = True
    else:
        result['selection_status'] = 'wiki' if resources else 'no_direct_resources'
    return result


def directory_tree(entries, categories):
    roots = {}
    paths = {}
    for entry in entries.values():
        for resource in entry.get('resources', []):
            for cid in entry['category_ids']:
                raw_path = [categories[str(cid)]['name'], entry['title'], *resource.get('section_path', [])]
                path = [safe_name(segment) for segment in raw_path if segment]
                filename = resource['filename']
                key = tuple(path + [filename])
                if key in paths and paths[key] != resource['id']:
                    stem, ext = filename.rsplit('.', 1)
                    filename = stem + '-' + digest(resource['id'])[:8] + '.' + ext
                    resource['filename'] = filename
                    key = tuple(path + [filename])
                paths[key] = resource['id']
                resource.setdefault('directories', []).append(path)
                cursor = roots
                for segment in path:
                    cursor = cursor.setdefault(segment, {'children': {}, 'resources': []})['children']
                # Navigate once more to attach to the directory, not its children map.
                cursor = roots
                for segment in path:
                    node = cursor[segment]
                    cursor = node['children']
                node['resources'].append({'entry_id': entry['id'], 'resource_id': resource['id'],
                                          'filename': resource['filename']})
    def flatten(mapping):
        return [{'name': name, 'children': flatten(node['children']), 'resources': node['resources']}
                for name, node in mapping.items()]
    return flatten(roots)
