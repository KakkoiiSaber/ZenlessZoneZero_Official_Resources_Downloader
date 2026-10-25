"""Incremental three-source manifests. No media downloads or NAS operations."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from src.scrape.http import Client, FetchError, atomic_json, digest, now
from src.scrape.parse import article, directory_tree, official_index

OFFICIAL = 'https://api-takumi.mihoyo.com/content_v2_user/app/706fd13a87294881/'
MIYOUSHE = 'https://bbs-api.mihoyo.com/post/wapi/'
WIKI = 'https://act-api-takumi.mihoyo.com/'
CATEGORY_IDS = {73, 72, 98, 99}
PARSER_VERSION = 3


def emit(stage, **fields):
    print(json.dumps({'stage': stage, **fields}, ensure_ascii=True), flush=True)


def read_json(path, default):
    return json.loads(Path(path).read_text(encoding='utf8')) if Path(path).exists() else default


def read_bundle(output):
    from src.scrape.schema import expand_bundle
    output = Path(output)
    return expand_bundle({source:read_json(output/(source+'.json'), {}) for source in ('official','miyoushe','wiki')})


def discover_official(client):
    records = {}
    for channel_id, kind in [(273, 'news'), (1330, 'video')]:
        channel_records = set()
        total = None
        for page in range(1, 1001):
            data = client.get(OFFICIAL + 'getContentList',
                              {'iPageSize': 100, 'iPage': page, 'sLangKey': 'zh-cn', 'iChanId': channel_id})
            items = data.get('list')
            if not isinstance(items, list) or not isinstance(data.get('iTotal'), int):
                raise FetchError('invalid_official_listing')
            total = max(total or 0, data['iTotal'])
            for item in items:
                ident = str(item['iInfoId'])
                channel_records.add(ident)
                records[ident] = {'id': ident, 'title': item['sTitle'], 'channel_id': channel_id,
                    'source_kind': kind,
                    'url': 'https://zzz.mihoyo.com/news/' + ident if kind == 'news' else 'https://zzz.mihoyo.com/video',
                    'published_at': item.get('dtStartTime', '').replace(' ', 'T') + '+08:00',
                    'index_signature': digest({k: item.get(k) for k in
                        ('sTitle', 'sUrl', 'dtStartTime', 'sExt', 'sSign')})}
            emit('official_index', channel=kind, page=page, found=len(channel_records), expected=total)
            if len(channel_records) >= total:
                break
            if not items:
                raise FetchError('incomplete_official_listing')
        else:
            raise FetchError('official_pagination_limit')
    return records


def discover_miyoushe(client):
    records = {}
    cursor = ''
    seen_cursors = set()
    for page in range(1, 1001):
        params = {'gids': 8, 'forum_id': 58, 'sort_type': 3, 'page_size': 50}
        if cursor:
            params['last_id'] = cursor
        data = client.get(MIYOUSHE + 'getForumPostList', params)
        items = data.get('list')
        if not isinstance(items, list):
            raise FetchError('invalid_miyoushe_listing')
        for wrapper in items:
            post = wrapper['post']
            ident = str(post['post_id'])
            relevant = {k: post.get(k) for k in ('subject','updated_at','content','structured_content','images','post_status')}
            from src.scrape.parse import stable_listing_value, operations
            relevant['structured_content'] = stable_listing_value(operations(post.get('structured_content')))
            relevant['videos'] = stable_listing_value([{k: v.get(k) for k in ('id','duration','resolutions')}
                                  for v in wrapper.get('vod_list') or []])
            records[ident] = {'id': ident, 'title': post['subject'],
                'url': 'https://www.miyoushe.com/zzz/article/' + ident,
                'published_at': datetime.fromtimestamp(int(post['created_at']), timezone.utc).isoformat(),
                'index_signature': digest(relevant), '_listing': wrapper}
        emit('miyoushe_index', page=page, found=len(records))
        if data.get('is_last') is True or not items:
            return records
        next_cursor = str(data.get('last_id') or items[-1]['post']['post_id'])
        if next_cursor in seen_cursors:
            raise FetchError('miyoushe_pagination_loop')
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise FetchError('miyoushe_pagination_limit')


def discover_wiki(client):
    data = client.get(WIKI + 'common/blackboard/zzz_wiki/v1/home/content/list',
                      {'app_sn': 'zzz_wiki', 'lang': 'zh-cn', 'channel_id': 13})
    categories = {}
    entries = {}
    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            if node.get('id') in CATEGORY_IDS and isinstance(node.get('list'), list):
                cid = node['id']
                categories[str(cid)] = {'id': cid, 'name': node['name'], 'entry_ids': []}
                for raw in node['list']:
                    ident = str(raw['content_id'])
                    categories[str(cid)]['entry_ids'].append(ident)
                    entry = entries.setdefault(ident, {'id': ident, 'title': raw['title'].strip(),
                        'url': 'https://baike.mihoyo.com/zzz/wiki/content/' + ident + '/detail',
                        'category_ids': [], 'miyoushe_url': raw.get('bbs_url') or None,
                        'index_signature': digest(raw)})
                    if cid not in entry['category_ids']:
                        entry['category_ids'].append(cid)
            for key in ('children', 'list'):
                visit(node.get(key))
    visit(data.get('list'))
    if {int(k) for k in categories} != CATEGORY_IDS or not entries:
        raise FetchError('incomplete_wiki_index')
    emit('wiki_index', entries=len(entries), categories=len(categories))
    return entries, categories


def fetch_post(client, source, entry):
    if source == 'official':
        data = client.get(OFFICIAL + 'getContent', {'iInfoId': entry['id'], 'iChanId': entry.get('channel_id',273), 'sLangKey': 'zh-cn'})
        if 'sContent' not in data:
            raise FetchError('missing_official_body')
        from src.scrape.parse import official_article
        parsed = official_article(data)
    else:
        from src.scrape.parse import miyoushe_listing_article
        parsed = miyoushe_listing_article(entry['_listing']) if entry.get('_listing') else None
        if parsed is not None:
            return {**{k:v for k,v in entry.items() if k!='_listing'}, **parsed,
                'status': 'ok', 'checked_at': now(), 'parser_version': PARSER_VERSION}
        data = client.get(MIYOUSHE + 'getPostFull', {'gids': 8, 'post_id': entry['id']})
        wrapper = data.get('post') or {}
        post = wrapper.get('post') or {}
        if str(post.get('post_id')) != entry['id']:
            raise FetchError('missing_miyoushe_body')
        parsed = article(post.get('content') or '', post.get('structured_content') or '', wrapper)
        entry = {**entry, 'title': post.get('subject') or entry['title']}
    return {**{k:v for k,v in entry.items() if k!='_listing'}, **parsed, 'status': 'ok', 'checked_at': now(), 'parser_version': PARSER_VERSION}


def needs_refresh(old, entry, age_days):
    if not old or old.get('status') != 'ok' or old.get('parser_version') != PARSER_VERSION:
        return True
    if old.get('index_signature') != entry['index_signature']:
        return True
    for video in old.get('videos', []):
        expires = (video.get('selected') or {}).get('expires_at')
        if expires and datetime.fromisoformat(expires).timestamp() <= time.time() + 86400:
            return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(old['checked_at'])
        return age.total_seconds() >= age_days * 86400
    except (KeyError, ValueError):
        return True


def update_records(source, entries, previous, progress_path, fetch, workers, age_days):
    resumed = read_json(progress_path, {})
    old = {**previous, **resumed}
    result = {}
    due = []
    for ident, entry in entries.items():
        cached = old.get(ident)
        if needs_refresh(cached, entry, age_days):
            due.append(entry)
        else:
            result[ident] = {**cached, **{k:v for k,v in entry.items() if k!='_listing'}}
    emit(source + '_details', total=len(entries), reused=len(result), pending=len(due))
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = {pool.submit(fetch, entry): entry for entry in due}
        for done, future in enumerate(as_completed(tasks), 1):
            entry = tasks[future]
            try:
                result[entry['id']] = future.result()
            except Exception as error:
                failures += 1
                # Retain last-known resource data, but never label a failed refresh successful.
                result[entry['id']] = {**old.get(entry['id'], {}), **{k:v for k,v in entry.items() if k!='_listing'}, 'status': 'error',
                    'error': str(error) if isinstance(error, FetchError) else type(error).__name__}
            if done % 25 == 0 or done == len(due):
                atomic_json(progress_path, result)
                emit(source + '_progress', completed=done, total=len(due), failures=failures)
    return {ident: result[ident] for ident in entries}, failures, len(due)


def validate_bundle(bundle):
    if set(bundle) != {'official', 'miyoushe', 'wiki'}:
        raise ValueError('Three manifests required')
    if len({value['snapshot_id'] for value in bundle.values()}) != 1:
        raise ValueError('Mismatched manifest snapshots')
    for source in ('official', 'miyoushe'):
        if not bundle[source]['posts']:
            raise ValueError('Empty post index')
        for ident, post in bundle[source]['posts'].items():
            if ident != post['id'] or post['status'] not in ('ok', 'error'):
                raise ValueError('Invalid post record')
    categories = bundle['wiki']['categories']
    seen_paths = {}
    for ident, entry in bundle['wiki']['entries'].items():
        if ident != entry['id'] or any(str(cid) not in categories for cid in entry['category_ids']):
            raise ValueError('Invalid Wiki category reference')
        for resource in entry.get('resources', []):
            from src.scrape.parse import media_url
            if media_url(resource.get('url'), resource['kind']) != resource['url']:
                raise ValueError('Invalid selected media URL')
            for directory in resource.get('directories', []):
                key = '/'.join([*directory, resource['filename']])
                if key in seen_paths and seen_paths[key] != resource['id']:
                    raise ValueError('Colliding Wiki output path: ' + key)
                seen_paths[key] = resource['id']
    return bundle


def publish(output, bundle):
    """Stage and validate all three files before replacing the old metadata directory."""
    import os
    import shutil
    from src.scrape.schema import pack_bundle, expand_bundle
    validate_bundle(bundle)
    compact = pack_bundle(bundle)
    validate_bundle(expand_bundle(compact))
    output = Path(output)
    stage = output.with_name('.' + output.name + '-next')
    stage.mkdir(exist_ok=True, parents=True)
    for source, data in compact.items():
        atomic_json(stage / (source + '.json'), data, record_maps=('posts','entries'))
    validate_bundle(read_bundle(stage))
    backup = output.with_name('.' + output.name + '-previous')
    if backup.exists():
        # This task-owned backup is always a sibling inside the output parent.
        if backup.resolve().parent != output.resolve().parent:
            raise ValueError('Unsafe backup path')
        shutil.rmtree(backup)

    if output.exists():
        os.replace(output, backup)
    try:
        os.replace(stage, output)
    except BaseException:
        if backup.exists():
            os.replace(backup, output)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def review_official_sources(official, mi, wiki):
    from src.scrape.parse import match_official, signature
    from src.scrape.probe import review_duplicates
    index = official_index(official)
    profile_ids = set()
    for entry in wiki.values():
        if not any(cid in (72,73) for cid in entry['category_ids']):
            continue
        urls = [entry.get('miyoushe_url'), *entry.get('detail_links', [])]
        linked = [mi[url.rsplit('/',1)[-1]] for url in dict.fromkeys(urls)
                  if url and url.rsplit('/',1)[-1] in mi]
        if match_official(entry, linked, index)['status'] == 'ambiguous':
            for title in [entry['title'], *[post['title'] for post in linked]]:
                profile_ids.update(post['id'] for post in index.get(signature(title), []))
    return review_duplicates(official, profile_post_ids=profile_ids)

def run(args):
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    run_file = cache/'run.json'
    state = read_json(run_file, {})
    if args.restart or state.get('complete') or not state:
        state = {'started_at': time.time(), 'complete': False}
        # Parsed progress belongs to this run. Previous manifests provide incremental reuse.
        for source in ('official', 'miyoushe', 'wiki'):
            (cache/(source+'-progress.json')).unlink(missing_ok=True)
        atomic_json(run_file, state)
    client = Client(cache/'responses', state['started_at'], args.interval, args.transport)
    output = Path(args.output)
    existing = {} if args.fresh else read_bundle(output)
    previous = {source: {} if args.fresh else existing[source].get(
                'entries' if source == 'wiki' else 'posts', {}) for source in ('official','miyoushe','wiki')}
    def collect_source(source):
        entries = discover_official(client) if source == 'official' else discover_miyoushe(client)
        return update_records(source, entries, previous[source], cache/(source+'-progress.json'),
            lambda e: fetch_post(client, source, e), args.workers, args.refresh_days)
    def collect_wiki():
        entries, categories = discover_wiki(client)
        def fetch_wiki(entry):
            data = client.get(WIKI + 'hoyowiki/zzz/wapi/entry_page',
                              {'app_sn': 'zzz_wiki', 'lang': 'zh-cn', 'entry_page_id': entry['id']})
            page = data.get('page')
            if not isinstance(page, dict) or str(page.get('id')) != entry['id'] or not isinstance(page.get('modules'), list):
                raise FetchError('invalid_wiki_detail')
            from src.scrape.parse import wiki_media
            media, links = wiki_media(page)
            return {**entry, 'wiki_resources': media, 'detail_links': links, 'status': 'ok',
                    'checked_at': now(), 'parser_version': PARSER_VERSION, 'entry_revision': page.get('version')}
        records, failures, changed = update_records('wiki', entries, previous['wiki'], cache/'wiki-progress.json',
            fetch_wiki, args.workers, args.refresh_days)
        return records, failures, changed, categories
    # Sources are independent until video matching; all requests share one rate limiter.
    with ThreadPoolExecutor(max_workers=3) as sources:
        official_job = sources.submit(collect_source, 'official')
        mi_job = sources.submit(collect_source, 'miyoushe')
        wiki_job = sources.submit(collect_wiki)
        official, ofail, ochanged = official_job.result()
        mi, mfail, mchanged = mi_job.result()
        wiki, wfail, wchanged, categories = wiki_job.result()
    wiki_entries = wiki
    # Wiki links can point outside the forum listing; collect those exact IDs too.
    def ensure_linked(urls):
        additions = {}
        import re
        for url in urls:
            match = re.fullmatch(r'https://(?:www\.)?miyoushe\.com/zzz/article/(\d+)', url or '')
            if match and match[1] not in mi:
                additions[match[1]] = {'id': match[1], 'url': url, 'title': '', 'published_at': '',
                                        'index_signature': digest({'wiki_link': url})}
        if additions:
            resolved, errors, changed = update_records('linked_miyoushe', additions, previous['miyoushe'],
                cache/'linked-progress.json', lambda e: fetch_post(client, 'miyoushe', e), args.workers, args.refresh_days)
            mi.update(resolved)
            return errors, changed
        return 0, 0
    extra_failures, extra_changed = ensure_linked([e.get('miyoushe_url') for e in wiki_entries.values()])
    emit('official_duplicate_review', **review_official_sources(official, mi, wiki))
    index = official_index(official)
    extra2, changed2 = ensure_linked([url for e in wiki.values() for url in e.get('detail_links', [])])
    from src.scrape.parse import resolve_parsed_wiki
    for ident, entry in list(wiki.items()):
        wiki[ident] = resolve_parsed_wiki(entry, mi, official, index)
    tree = directory_tree(wiki, categories)
    stamp = now()
    snapshot = digest({'generated_at': stamp, 'official': official, 'miyoushe': mi, 'wiki': wiki})
    base = {'schema_version': 1, 'snapshot_id': snapshot, 'generated_at': stamp}
    def document(source, records, failures, changed):
        return {**base, 'source': source, 'status': 'partial' if failures else 'complete',
                'statistics': {'records': len(records), 'refreshed': changed, 'reused': len(records)-changed,
                               'failures': failures}, 'posts': records}
    bundle = {'official': document('official', official, ofail, ochanged),
              'miyoushe': document('miyoushe', mi, mfail+extra_failures+extra2, mchanged+extra_changed+changed2),
              'wiki': {**base, 'source': 'wiki', 'status': 'partial' if wfail else 'complete',
                'statistics': {'entries': len(wiki), 'resources': sum(len(e.get('resources', [])) for e in wiki.values()),
                    'refreshed': wchanged, 'failures': wfail,
                    'video_selection': {key: sum(e.get('selection_status') == key for e in wiki.values())
                        for key in ('official','miyoushe_fallback','wiki_direct','unresolved')}},
                'categories': categories, 'entries': wiki, 'directory_tree': tree}}
    validate_bundle(bundle)
    failures = ofail + mfail + wfail + extra_failures + extra2
    if failures and not args.allow_partial:
        emit('not_published', failures=failures, requests=client.requests,
             message='Progress saved; rerun to retry failures. Existing metafiles unchanged.')
        return 1
    publish(output, bundle)
    state['complete'] = not failures
    atomic_json(run_file, state)
    emit('published', snapshot_id=snapshot, failures=failures, requests=client.requests, cache_hits=client.cache_hits,
         official=len(official), miyoushe=len(mi), wiki=len(wiki), wiki_statistics=bundle['wiki']['statistics'])
    return 1 if failures else 0


def resolve_only(args):
    """Review and rematch existing manifests without fetching indexes or article bodies."""
    from src.scrape.parse import resolve_parsed_wiki
    output = Path(args.output)
    bundle = read_bundle(output)
    validate_bundle(bundle)
    official, mi = bundle['official']['posts'], bundle['miyoushe']['posts']
    emit('official_duplicate_review', **review_official_sources(official, mi, bundle['wiki']['entries']))
    index = official_index(official)
    wiki = {ident: resolve_parsed_wiki(entry, mi, official, index)
            for ident, entry in bundle['wiki']['entries'].items()}
    bundle['wiki']['entries'] = wiki
    bundle['wiki']['directory_tree'] = directory_tree(wiki, bundle['wiki']['categories'])
    statistics = bundle['wiki'].setdefault('statistics', {'entries':len(wiki),'failures':0})
    statistics['resources'] = sum(len(entry.get('resources', [])) for entry in wiki.values())
    statistics['video_selection'] = {key: sum(entry.get('selection_status') == key for entry in wiki.values())
        for key in ('official','miyoushe_fallback','wiki_direct','unresolved')}
    stamp = now()
    snapshot = digest({'generated_at': stamp, 'official': official, 'miyoushe': mi, 'wiki': wiki})
    for document in bundle.values():
        document.update(snapshot_id=snapshot, generated_at=stamp)
    validate_bundle(bundle)
    publish(output, bundle)
    emit('resolved', **statistics)
    return 0


def compact_only(args):
    bundle = read_bundle(args.output)
    validate_bundle(bundle)
    publish(args.output, bundle)
    emit('compacted', files={source: (Path(args.output)/(source+'.json')).stat().st_size
                            for source in bundle})
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='metafiles')
    parser.add_argument('--cache', default='.cache/scrape')
    parser.add_argument('--fresh', action='store_true', help='Rebuild from source; do not reuse old manifests')
    parser.add_argument('--resolve-only', action='store_true', help='Review duplicate videos and rematch existing manifests only')
    parser.add_argument('--compact-only', action='store_true', help='Migrate existing manifests to the minimal schema without network requests')
    parser.add_argument('--restart', action='store_true', help='Start a new crawl rather than resume an interrupted one')
    parser.add_argument('--allow-partial', action='store_true', help='Explicitly publish records with visible error statuses')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--interval', type=float, default=.15, help='Minimum interval between request starts')
    parser.add_argument('--refresh-days', type=int, default=7)
    parser.add_argument('--transport', choices=('auto','curl','requests'), default='auto')
    args = parser.parse_args()
    if not 1 <= args.workers <= 12 or args.interval < 0 or args.refresh_days < 1:
        parser.error('Invalid concurrency, interval or refresh age')
    try:
        return compact_only(args) if args.compact_only else resolve_only(args) if args.resolve_only else run(args)
    except (FetchError, ValueError, OSError) as error:
        emit('failed', reason=str(error) if isinstance(error, FetchError) else type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
