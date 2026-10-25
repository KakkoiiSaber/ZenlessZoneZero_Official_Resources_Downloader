"""Versioned public source/path catalog. Credentials and download tokens stay private."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
from urllib.parse import urlparse
from src.downloader.minas_files import FileUpdater, remote_path
from src.downloader.MinasDownloader import authenticate_with_password, _norm_cell
from src.metadata.access import MetadataSession, share_url, stable_id, public_access_code
from tools.build_gallery import image_versions
from tools.gallery_tags import broad_categories, build_tags

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.avif', '.tif', '.tiff'}
VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.mkv', '.m4v', '.avi'}


def media(path):
    ext = PurePosixPath(path).suffix.lower()
    kind = 'image' if ext in IMAGE_EXTS else 'video' if ext in VIDEO_EXTS else None
    mime = {'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.gif':'image/gif',
            '.webp':'image/webp','.bmp':'image/bmp','.avif':'image/avif','.tif':'image/tiff','.tiff':'image/tiff',
            '.mp4':'video/mp4','.webm':'video/webm','.mov':'video/quicktime','.mkv':'video/x-matroska',
            '.m4v':'video/mp4','.avi':'video/x-msvideo'}.get(ext,'application/octet-stream')
    return kind, mime


def public_post(row, secrets=()):
    url = _norm_cell(row.get('post_url'))
    p = urlparse(url)
    if p.scheme != 'https' or p.netloc not in ('www.miyoushe.com', 'miyoushe.com', 'zzz.mihoyo.com', 'zenless.hoyoverse.com') or p.query or p.fragment:
        raise ValueError('Unsupported official post URL')
    title = _norm_cell(row.get('post_name'))[:240]
    title = ''.join(c for c in title if ord(c) >= 32)
    if any(secret and secret in title for secret in secrets) or re.search('提取码|访问码|密码|password|passcode|https?://', title, re.I):
        title = '[title withheld]'
    return {'post_id': url, 'post_url': url, 'title': title, 'post_time': _norm_cell(row.get('post_time'))[:32]}


def revision(catalog):
    payload = {k: catalog[k] for k in ('version', 'sources', 'items')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def collect(rows, previous=None, session_factory=MetadataSession, authenticate=authenticate_with_password, listing=None, code_resolver=public_access_code):
    previous = previous or {'version': 1, 'sources': {}, 'items': {}}
    if previous.get('version') != 1:
        raise ValueError('Unsupported catalog version')
    sources = json.loads(json.dumps(previous['sources']))
    items = json.loads(json.dumps(previous['items']))
    groups = {}; errors = []; skipped = 0
    for row in rows:
        if not _norm_cell(row.get('minas_link')):
            skipped += 1; continue
        try:
            share = share_url(row['minas_link']); code = _norm_cell(row.get('minas_pwd'))
            sid = stable_id(share); post = public_post(row, [code])
            group = groups.setdefault(sid, {'share': share, 'code': code, 'posts': []})
            if group['code'] and code and group['code'] != code:
                raise ValueError('Conflicting share access codes')
            group['code'] = group['code'] or code
            if post not in group['posts']:group['posts'].append(post)
        except (ValueError, TypeError, KeyError):
            errors.append({'phase': 'source', 'reason': 'invalid_source'})
    scanner = FileUpdater.__new__(FileUpdater); scanner.metadata_requests = 0
    scanner.listing_max_file_bytes = int(os.getenv("CATALOG_MAX_MEDIA_BYTES", str(10 * 1024 ** 3)))
    for sid, group in sorted(groups.items()):
        old = sources.get(sid, {'posts': []})
        posts = sorted({p['post_id']:p for p in old['posts'] + group['posts']}.values(), key=lambda p:p['post_id'])
        source = {'share': group['share'], 'posts': posts}
        sources[sid] = source
        try:
            with session_factory() as session:
                try:authenticate(session, group['share'], group['code'])
                except Exception:
                    refreshed=code_resolver(source) if code_resolver else None
                    if refreshed is None or refreshed==group['code']:raise
                    group['code']=refreshed
                    authenticate(session,group['share'],refreshed)
                files = listing(session, group['share']) if listing else scanner.listing(session, group['share'])
            seen = set();updates={};new_count=0
            for path, info in sorted(files.items()):
                kind, mime = media(path)
                if not kind:continue
                if group['code'] and group['code'] in path:raise ValueError('Private value in source path')
                iid = stable_id(sid, path); seen.add(iid)
                entry = {'associations': [{'original_path': path}],
                         'posts': [{**p, 'share': group['share'], 'remote_path':path} for p in posts]}
                entry['versions'] = image_versions(entry)
                item = {'id':iid, 'source_id':sid, 'path':path, 'size':info['size'],
                        'last_modified':info.get('last_modified'), 'media_type':kind, 'mime_type':mime,
                        'versions':entry['versions'], 'categories':broad_categories(entry),
                        'tags':build_tags(entry), 'available':True}
                if iid not in items:new_count+=1
                if len(items)+new_count>50000:raise ValueError('Catalog item limit exceeded')
                updates[iid] = item
            items.update(updates)
            for iid, item in items.items():
                if item['source_id']==sid and iid not in seen:item['available']=False
        except Exception as error:
            # Keep prior entries, never infer removal from an incomplete/failed scan.
            errors.append({'source_id':sid,'phase':'listing','reason':type(error).__name__})
    catalog={'version':1,'sources':dict(sorted(sources.items())), 'items':dict(sorted(items.items()))}
    catalog['revision']=revision(catalog)
    validate(catalog)
    return catalog, {'version':1,'sources_checked':len(groups),'media_items':len(items),'skipped_rows':skipped,'failures':errors}


def public_values(value, depth=0):
    if depth>16:raise ValueError('Catalog nesting limit exceeded')
    forbidden={'password','minas_pwd','pwd','cookie','cookies','authorization','access_token',
               'temporary_url','download_url','download','headers','secret','token'}
    if isinstance(value,dict):
        for key,child in value.items():
            if not isinstance(key,str) or key.lower() in forbidden:raise ValueError('Private field in catalog')
            public_values(child,depth+1)
    elif isinstance(value,list):
        for child in value:public_values(child,depth+1)
    elif isinstance(value,str):
        if '/seafhttp/' in value:raise ValueError('Temporary transfer URL in catalog')
        for url in re.findall(r'https?://[^\s<>]+',value):
            p=urlparse(url)
            if (p.scheme!='https' or p.query or p.fragment or p.netloc not in
                ('minas.mihoyo.com','www.miyoushe.com','miyoushe.com','zzz.mihoyo.com','zenless.hoyoverse.com')):
                raise ValueError('Unsupported public evidence URL')
            if p.netloc=='minas.mihoyo.com':share_url(url)


def validate(catalog):
    if isinstance(catalog,dict) and catalog.get('version')==2:
        from src.metadata.resources import validate as validate_resources
        return validate_resources(catalog)
    if len(json.dumps(catalog,ensure_ascii=False).encode())>32*1024**2:
        raise ValueError('Catalog exceeds size limit')
    public_values(catalog)
    if not isinstance(catalog,dict) or set(catalog)!={'version','revision','sources','items'} or catalog['version']!=1:
        raise ValueError('Invalid catalog schema')
    if not isinstance(catalog['sources'],dict) or not isinstance(catalog['items'],dict):
        raise ValueError('Invalid catalog maps')
    if len(catalog['sources'])>1000 or len(catalog['items'])>50000 or revision(catalog)!=catalog['revision']:
        raise ValueError('Invalid catalog revision or size')
    for sid, source in catalog['sources'].items():
        if not isinstance(source,dict) or set(source)!={'share','posts'} or sid!=stable_id(share_url(source['share'])):
            raise ValueError('Invalid catalog source')
        if not isinstance(source['posts'],list) or len(source['posts'])>1000:raise ValueError('Invalid catalog posts')
        for post in source['posts']:
            if set(post)!={'post_id','post_url','title','post_time'} or public_post({'post_url':post['post_url'],'post_name':post['title']})['post_url']!=post['post_id']:
                raise ValueError('Invalid catalog post')
    allowed={'id','source_id','path','size','last_modified','media_type','mime_type','versions','categories','tags','available'}
    for iid,item in catalog['items'].items():
        if set(item)!=allowed or item['source_id'] not in catalog['sources']:
            raise ValueError('Invalid catalog item')
        path=item['path'];parent=PurePosixPath(path).parent.as_posix()
        if remote_path(path,parent)!=path or iid!=stable_id(item['source_id'],path) or item['id']!=iid:
            raise ValueError('Invalid catalog path identity')
        if isinstance(item['size'],bool) or not isinstance(item['size'],int) or item['size']<0:
            raise ValueError('Invalid catalog size')
        if item['last_modified'] is not None and (not isinstance(item['last_modified'],str) or not re.fullmatch(r'[0-9TtZz:+. -]{1,64}',item['last_modified'])):
            raise ValueError('Invalid listing timestamp')
        if media(path)[1]!=item['mime_type']:raise ValueError('Invalid catalog MIME type')
        if media(path)[0]!=item['media_type'] or item['media_type'] not in ('image','video') or not isinstance(item['available'],bool):
            raise ValueError('Invalid catalog media type')
    return catalog
