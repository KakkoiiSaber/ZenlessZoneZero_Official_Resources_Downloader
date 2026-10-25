"""Version-two resource catalog; v1 Minas identities remain byte-for-byte stable."""
from __future__ import annotations
from copy import deepcopy
import json
from urllib.parse import urlparse, parse_qsl
import requests
from src.metadata.access import stable_id
from src.metadata.wiki import MEDIA_HOSTS, PAGE_HOSTS, EXTERNAL_VIDEO_HOSTS, media_url, public_link, digest


class OfficialMediaSession(requests.Session):
    def send(self, request, **kwargs):
        if media_url(request.url) is None:
            raise ValueError('Untrusted official media host or URL')
        return super().send(request, **kwargs)


def revision(catalog):
    return digest({key: value for key, value in catalog.items() if key != 'revision'})


def public_guard(value, depth=0):
    if depth > 24:
        raise ValueError('Resource nesting limit exceeded')
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key.lower() in {'password','minas_pwd','pwd','cookie','cookies','authorization','access_token','token','secret','headers','temporary_url'}:
                raise ValueError('Private resource field')
            public_guard(child, depth+1)
    elif isinstance(value, list):
        for child in value:
            public_guard(child, depth+1)
    elif isinstance(value, str):
        import re
        for url in re.findall(r'https?://[^\s<>]+', value):
            p = urlparse(url)
            if p.scheme != 'https' or p.username or p.fragment or p.netloc not in MEDIA_HOSTS | PAGE_HOSTS | EXTERNAL_VIDEO_HOSTS | {'minas.mihoyo.com'}:
                raise ValueError('Untrusted resource URL')
            if p.query and (p.netloc not in MEDIA_HOSTS or any(key != 'x-oss-process' for key, _ in parse_qsl(p.query, keep_blank_values=True))):
                raise ValueError('Secret or temporary resource query')
            if '/seafhttp/' in p.path:
                raise ValueError('Temporary resource URL')


def validate(catalog):
    if not isinstance(catalog, dict) or set(catalog) != {'version','revision','sources','items','works','knowledge_revision','variant_groups'} or catalog['version'] != 2:
        raise ValueError('Invalid resource catalog schema')
    if len(json.dumps(catalog, ensure_ascii=False).encode()) > 32*1024**2:
        raise ValueError('Resource catalog exceeds limit')
    public_guard(catalog)
    if not all(isinstance(catalog[key], dict) for key in ('sources','items','works','variant_groups')):
        raise ValueError('Invalid resource maps')
    if any(not isinstance(source,dict) for source in catalog['sources'].values()):
        raise ValueError('Invalid source object')
    if len(catalog['items']) > 50000 or len(catalog['works']) > 5000 or revision(catalog) != catalog['revision']:
        raise ValueError('Resource revision or count limit')
    from src.metadata.catalog import validate as validate_v1, revision as revision_v1, media
    legacy_sources = {sid: source for sid, source in catalog['sources'].items() if source.get('provider') != 'official'}
    legacy_items = {}
    for iid, item in catalog['items'].items():
        if not isinstance(item, dict) or item.get('source_id') not in catalog['sources']:
            raise ValueError('Invalid resource source reference')
        if item['source_id'] in legacy_sources:
            legacy_items[iid] = {**item, 'tags': []}
            continue
        source = catalog['sources'][item['source_id']]
        if set(source) != {'provider','host','posts'} or source['host'] not in MEDIA_HOSTS or item['source_id'] != stable_id('official-host', source['host']):
            raise ValueError('Invalid official source')
        if media_url(item.get('url')) is None or urlparse(item['url']).netloc != source['host'] or iid != stable_id('official-media', item['url']) or item.get('id') != iid:
            raise ValueError('Invalid official media identity')
        if item.get('size') is not None and (isinstance(item['size'], bool) or not isinstance(item['size'], int) or item['size'] < 0):
            raise ValueError('Invalid official size')
        if item.get('path') != urlparse(item['url']).path or item.get('media_type') != media(item['path'])[0] or item.get('mime_type') != media(item['path'])[1]:
            raise ValueError('Invalid official type')
        if not isinstance(item.get('origins'), list) or not item['origins'] or not isinstance(item.get('available'), bool):
            raise ValueError('Missing official provenance')
        if not isinstance(item.get('preferred'),bool) or not isinstance(item.get('quality'),dict) or not isinstance(item['quality'].get('original_verified'),bool):
            raise ValueError('Invalid media quality/preference metadata')
        if item['quality']['original_verified'] and not item['quality'].get('verification'):
            raise ValueError('Original quality assertion lacks evidence')
        if not all(isinstance(item.get(key),list) for key in ('tags','versions','categories')):
            raise ValueError('Invalid media labels')
        for origin in item['origins']:
            if not isinstance(origin,dict) or origin.get('source_url')!='https://baike.mihoyo.com/zzz/wiki/content/'+str(origin.get('wiki_id'))+'/detail':
                raise ValueError('Invalid media origin')
    legacy = {'version':1,'sources':deepcopy(legacy_sources),'items':legacy_items}
    legacy['revision'] = revision_v1(legacy)
    validate_v1(legacy)
    for wid, work in catalog['works'].items():
        if not isinstance(work,dict) or not str(work.get('wiki_id','')).isdigit():
            raise ValueError('Invalid Wiki identity')
        if work.get('wiki_url')!='https://baike.mihoyo.com/zzz/wiki/content/'+work['wiki_id']+'/detail':
            raise ValueError('Invalid work URL')
        if any(public_link(url) is None for url in work.get('links',[])) or (work.get('official_match') and urlparse(work['official_match']).netloc not in {'zzz.mihoyo.com','zenless.hoyoverse.com'}):
            raise ValueError('Invalid work source link')
        if wid != stable_id('wiki-work', work.get('wiki_id','')) or not isinstance(work.get('media_ids'), list) or any(mid not in catalog['items'] for mid in work['media_ids']):
            raise ValueError('Invalid work/media association')
    for gid,group in catalog['variant_groups'].items():
        if not isinstance(group,dict) or not isinstance(group.get('item_ids'),list) or group.get('preferred_item_id') not in group['item_ids'] or any(iid not in catalog['items'] for iid in group['item_ids']):
            raise ValueError('Invalid media variant reference')
        for iid in group['item_ids']:
            item=catalog['items'][iid]
            if item.get('variant_group_id')!=gid or gid!=stable_id('cdn-object',urlparse(item['url'])._replace(query='').geturl()) or item['preferred']!=(iid==group['preferred_item_id']):
                raise ValueError('Invalid variant preference')
    return catalog


def combine(legacy, works, knowledge_revision=''):
    from src.metadata.catalog import media
    catalog = {'version':2,'sources':deepcopy(legacy['sources']),'items':deepcopy(legacy['items']),
               'works':deepcopy(works),'knowledge_revision':knowledge_revision,'variant_groups':{}}
    for wid, work in catalog['works'].items():
        mids = []
        for found in work.pop('media', []):
            url = media_url(found['url'])
            if not url:
                continue
            sid = stable_id('official-host', urlparse(url).netloc)
            iid = stable_id('official-media', url)
            catalog['sources'].setdefault(sid, {'provider':'official','host':urlparse(url).netloc,'posts':[]})
            item = catalog['items'].setdefault(iid, {'id':iid,'source_id':sid,'path':urlparse(url).path,'size':None,'last_modified':None,
                'media_type':found['media_type'],'mime_type':media(urlparse(url).path)[1], 'versions':found.get('versions',[]), 'categories':found.get('categories',work.get('categories',[])),
                'tags':deepcopy(found.get('tags',[])), 'available':found.get('available',True),'url':url,'origins':[],'quality':found['quality']})
            item['available']=item['available'] or found.get('available',True)
            item['versions']=sorted(set(item['versions']+found.get('versions',[])))
            item['categories']=sorted(set(item['categories']+found.get('categories',[])))
            for tag in found.get('tags',[]):
                existing=next((t for t in item['tags'] if (t['kind'],t['value'])==(tag['kind'],tag['value'])),None)
                if existing is None:item['tags'].append(deepcopy(tag))
                else:
                    for evidence in tag.get('evidence',[]):
                        if evidence not in existing['evidence']:existing['evidence'].append(deepcopy(evidence))
            for section in found['sections']:
                origin = {'source_url':work['wiki_url'],'wiki_id':work['wiki_id'],'listed':found.get('available',True),**section}
                if origin not in item['origins']:
                    item['origins'].append(origin)
            mids.append(iid)
        work['media_ids'] = sorted(set(mids))
        work['resolution_status'] = 'resolved' if mids else 'pending_scan' if work.get('detail_status')=='not_checked' else 'source_access_required' if any(urlparse(url).netloc=='minas.mihoyo.com' for url in work.get('links',[])) else 'link_only' if work.get('links') or work.get('official_match') else 'no_download_source'
        work['preferred_source'] = work.get('official_match') or work['wiki_url']
        work['preference_evidence'] = 'same_work_official_match' if work.get('official_match') else 'wiki_curated_index'
    catalog['sources'] = dict(sorted(catalog['sources'].items()))
    catalog['items'] = dict(sorted(catalog['items'].items()))
    # Only same-host/path OSS transforms are known variants. Different files,
    # Minas resolutions and merely matching names are never collapsed here.
    groups={}
    for iid,item in catalog['items'].items():
        if 'url' not in item:continue
        parsed=urlparse(item['url']);base=parsed._replace(query='').geturl()
        groups.setdefault(stable_id('cdn-object',base),[]).append(iid)
    for gid,ids in sorted(groups.items()):
        preferred=min(ids,key=lambda iid:(bool(urlparse(catalog['items'][iid]['url']).query),iid))
        reason='observed_untransformed_reference' if not urlparse(catalog['items'][preferred]['url']).query else 'only_processed_references_available'
        catalog['variant_groups'][gid]={'item_ids':sorted(ids),'preferred_item_id':preferred,'evidence':reason,'original_quality_verified':False}
        for iid in ids:
            catalog['items'][iid]['variant_group_id']=gid
            catalog['items'][iid]['preferred']=iid==preferred
    catalog['revision'] = revision(catalog)
    return validate(catalog)
