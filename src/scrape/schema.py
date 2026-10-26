"""Minimal persisted manifests; parsing and matching use an expanded in-memory model."""
from copy import deepcopy
import re

from src.scrape.http import now

VERSION = 2
URL_TEMPLATES = {
    'official': 'https://zzz.mihoyo.com/news/{id}',
    'miyoushe': 'https://www.miyoushe.com/zzz/article/{id}',
    'wiki': 'https://baike.mihoyo.com/zzz/wiki/content/{id}/detail',
}
VIDEO_PAGE = 'https://zzz.mihoyo.com/video'
SCAN_FIELDS = ('index_signature', 'checked_at')
VIDEO_FIELDS = ('url','extension','width','height','size_bytes','bitrate','average_bitrate','codec','expires_at')


def fields(value, keys):
    return {key: value[key] for key in keys if key in value and value[key] not in (None, '', [], {})}


def post_url(source, ident, kind='news'):
    return VIDEO_PAGE if source == 'official' and kind == 'video' else URL_TEMPLATES[source].format(id=ident)


def pack_video(video):
    chosen = video.get('selected') or {}
    result = {'id': video['id'], **fields(video, ('caption',)), **fields(chosen, VIDEO_FIELDS)}
    if video.get('duration_ms') is not None:
        result['duration_ms'] = video['duration_ms']
    facts = chosen.get('http_metadata') or {}
    if facts.get('status') == 200 and str(facts.get('content_type','')).split(';')[0].lower() in {
            'video/mp4','video/webm','video/quicktime','application/octet-stream'}:
        result.update(fields(facts, ('etag','size_bytes')))
        if facts.get('checked_at'):
            result['media_checked_at'] = facts['checked_at']
        profile = chosen.get('video_metadata') or {}
        if (profile and profile.get('source_etag') == facts.get('etag')
                and profile.get('source_size_bytes') == facts.get('size_bytes')):
            result.update(fields(profile, ('width','height','codec','average_bitrate','duration_ms')))
            result['profile_verified'] = True
    return result


def expand_video(value):
    chosen = fields(value, VIDEO_FIELDS)
    if value.get('media_checked_at'):
        facts = {'status':200, 'content_type':'application/octet-stream',
                 'checked_at':value['media_checked_at'], **fields(value, ('etag','size_bytes'))}
        chosen['http_metadata'] = facts
        if value.get('profile_verified'):
            profile = fields(value, ('width','height','codec','average_bitrate','duration_ms'))
            if len(profile) != 5 or not facts.get('etag') or not facts.get('size_bytes'):
                raise ValueError('Incomplete verified video profile')
            chosen['video_metadata'] = {**profile, 'source_etag':facts['etag'],
                'source_size_bytes':facts['size_bytes'], 'checked_at':facts['checked_at']}
    return {'id':value['id'], **fields(value, ('caption',)), 'duration_ms':value.get('duration_ms'), 'selected':chosen, 'variants':[chosen]}


def common(document, source, records):
    result = {'schema_version':VERSION, 'source':source, 'snapshot_id':document['snapshot_id'],
              'generated_at':document.get('generated_at') or now(),
              'parser_version':document.get('parser_version') or max(
                  (record.get('parser_version', 3) for record in records.values()), default=3),
              'url_template':URL_TEMPLATES[source]}
    if document.get('status', 'complete') != 'complete':
        result['status'] = document['status']
    return result


def pack_posts(document, source):
    result = common(document, source, document['posts'])
    posts = {}
    for ident, post in document['posts'].items():
        value = fields(post, ('title','published_at', *SCAN_FIELDS))
        if source == 'official' and post.get('source_kind') == 'video':
            value['source_kind'] = 'video'
        if post.get('url') != post_url(source, ident, post.get('source_kind','news')):
            value['url'] = post['url']
        if post.get('images'):
            value['images'] = list(dict.fromkeys(image['url'] for image in post['images']))
        if post.get('videos'):
            value['videos'] = [pack_video(video) for video in post['videos']]
        if post.get('minas_links'):
            value['minas_links'] = [fields(share, ('url','password')) for share in post['minas_links']]
        if post.get('status','ok') != 'ok':
            value.update(status=post['status'], **fields(post, ('error',)))
        if post.get('parser_version',result['parser_version']) != result['parser_version']:
            value['parser_version'] = post['parser_version']
        posts[ident] = value
    result['posts'] = posts
    return result


def expand_posts(document, source):
    result = {**document, 'status':document.get('status','complete'), 'posts':{}}
    for ident, value in document['posts'].items():
        kind = value.get('source_kind','news')
        post = {**value, 'id':ident, 'url':value.get('url') or post_url(source,ident,kind),
                'status':value.get('status','ok'), 'parser_version':value.get('parser_version',document['parser_version']),
                'images':[{'url':url} for url in value.get('images',[])],
                'videos':[expand_video(video) for video in value.get('videos',[])],
                'minas_links':[{'url':share['url'],'password':share.get('password')} for share in value.get('minas_links',[])]}
        if source == 'official':
            post.update(source_kind=kind, channel_id=1330 if kind=='video' else 273)
        result['posts'][ident] = post
    return result


def pack_wiki(document):
    result = common(document, 'wiki', document['entries'])
    result['categories'] = {ident:category['name'] for ident,category in document['categories'].items()}
    entries = {}
    for ident, entry in document['entries'].items():
        value = {**fields(entry, ('title','category_ids','cover_url', *SCAN_FIELDS)), 'folders':[], 'resources':[]}
        links = list(dict.fromkeys([*([entry['miyoushe_url']] if entry.get('miyoushe_url') else []),
                                   *entry.get('detail_links',[]), *entry.get('miyoushe_posts',[])]))
        mi_ids, other_links = [], []
        for url in links:
            match = re.fullmatch(r'https://(?:www\.)?miyoushe\.com/zzz/article/(\d+)', url)
            if match:
                if match[1] not in mi_ids:
                    mi_ids.append(match[1])
            else:
                other_links.append(url)
        if mi_ids:
            value['miyoushe_ids'] = mi_ids
        if other_links:
            value['links'] = other_links
        folder_ids = {}

        def resource(raw):
            item = fields(raw, ('id','kind','url','filename','size_bytes','expires_at'))
            caption = (raw.get('caption') or '').strip()
            if caption and not caption.isdecimal():
                item['caption'] = caption
            directories = raw.get('directories')
            if not directories:
                from src.scrape.parse import safe_name
                directories = [[safe_name(part) for part in [result['categories'][str(cid)], entry['title'],
                                *raw.get('section_path',[])] if part] for cid in entry['category_ids']]
            positions = []
            for directory in directories:
                key = tuple(directory)
                if key not in folder_ids:
                    folder_ids[key] = len(value['folders'])
                    value['folders'].append(list(directory))
                positions.append(folder_ids[key])
            item['folder' if len(positions)==1 else 'folders'] = positions[0] if len(positions)==1 else positions
            provider = raw.get('selected_source','wiki')
            if provider != 'wiki':
                item['source'] = provider
                match = re.fullmatch(r'wiki:[^:]+:video:([^:]+):(.+)', raw['id'])
                if not match:
                    raise ValueError('Missing selected resource source identity')
                item['post_id'], item['video_id'] = match.groups()
            etag = raw.get('etag') or (raw.get('http_metadata') or {}).get('etag')
            if etag:
                item['etag'] = etag
            return item

        value['resources'] = [resource(raw) for raw in entry.get('resources',[])]
        retained = {(raw['id'],raw['url']) for raw in entry.get('resources',[]) if raw.get('selected_source','wiki')=='wiki'}
        fallbacks = [resource(raw) for raw in entry.get('wiki_resources',[]) if (raw['id'],raw['url']) not in retained]
        if fallbacks:
            value['wiki_fallbacks'] = fallbacks
        match = entry.get('official_match')
        if match:
            value['match'] = {'status':match['status']}
            if match.get('post_id'):
                value['match']['official_id'] = match['post_id']
            if match.get('method'):
                value['match']['method'] = match['method']
        if entry.get('official_unavailable'):
            value['official_unavailable'] = True
        if entry.get('status','ok') != 'ok':
            value.update(status=entry['status'], **fields(entry, ('error',)))
        if entry.get('parser_version',result['parser_version']) != result['parser_version']:
            value['parser_version'] = entry['parser_version']
        entries[ident] = value
    result['entries'] = entries
    return result


def expand_wiki(document, sources):
    result = {**document, 'status':document.get('status','complete'), 'categories':{}, 'entries':{}}
    for ident, name in document['categories'].items():
        result['categories'][ident] = {'id':int(ident), 'name':name, 'entry_ids':[]}
    for ident, value in document['entries'].items():
        links = [URL_TEMPLATES['miyoushe'].format(id=post_id) for post_id in value.get('miyoushe_ids',[])]
        entry = {**fields(value, ('title','category_ids','cover_url', *SCAN_FIELDS, 'error','official_unavailable')),
                 'id':ident, 'url':URL_TEMPLATES['wiki'].format(id=ident), 'status':value.get('status','ok'),
                 'parser_version':value.get('parser_version',document['parser_version']),
                 'miyoushe_url':links[0] if links else None,
                 'detail_links':[*links,*value.get('links',[])], 'miyoushe_posts':links}

        def resource(item):
            positions = [item['folder']] if 'folder' in item else item['folders']
            try:
                directories = [value['folders'][position] for position in positions
                               if isinstance(position,int) and not isinstance(position,bool) and position>=0]
            except IndexError as error:
                raise ValueError('Invalid resource folder reference') from error
            if len(directories)!=len(positions) or not directories:
                raise ValueError('Invalid resource folder reference')
            provider = item.get('source','wiki')
            raw = {**fields(item, ('id','kind','url','filename','size_bytes','expires_at','etag','caption')),
                   'directories':directories, 'section_path':directories[0][2:], 'selected_source':provider}
            if provider != 'wiki':
                post = sources[provider]['posts'].get(item['post_id'])
                if post is None:
                    raise ValueError('Missing selected resource source post')
                raw['source_post'] = post['url']
            return raw

        entry['resources'] = [resource(item) for item in value['resources']]
        entry['wiki_resources'] = [deepcopy(raw) for raw in entry['resources'] if raw['selected_source']=='wiki']
        entry['wiki_resources'].extend(resource(item) for item in value.get('wiki_fallbacks',[]))
        match = value.get('match')
        if match:
            entry['official_match'] = {'status':match['status'], **fields(match, ('method',))}
            if match.get('official_id'):
                post = sources['official']['posts'].get(match['official_id'])
                if post is None:
                    raise ValueError('Missing matched official post')
                entry['official_match'].update(post_id=post['id'], url=post['url'])
        providers = {raw['selected_source'] for raw in entry['resources']}
        if any(cid in (72,73) for cid in entry['category_ids']):
            entry['selection_status'] = ('official' if 'official' in providers else 'miyoushe_fallback'
                if 'miyoushe' in providers else 'wiki_direct' if any(raw['kind']=='video' for raw in entry['resources']) else 'unresolved')
        else:
            entry['selection_status'] = 'wiki' if entry['resources'] else 'no_direct_resources'
        for cid in entry['category_ids']:
            result['categories'][str(cid)]['entry_ids'].append(ident)
        result['entries'][ident] = entry
    return result


def pack_bundle(bundle):
    return {**{source:pack_posts(bundle[source],source) for source in ('official','miyoushe')},
            'wiki':pack_wiki(bundle['wiki'])}


def expand_bundle(bundle):
    result = {}
    for source in ('official','miyoushe'):
        document = bundle.get(source) or {}
        result[source] = expand_posts(document,source) if document.get('schema_version')==VERSION else document
    document = bundle.get('wiki') or {}
    result['wiki'] = expand_wiki(document,result) if document.get('schema_version')==VERSION else document
    return result
