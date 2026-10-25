"""Incremental Wiki-first index with cached details and preserved legacy sources."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import time
from src.metadata.access import stable_id
from src.metadata.wiki import WikiClient, MEDIA_CHANNELS, index_entries, extract_media, digest,nodes
from src.metadata.knowledge import load_knowledge, update_roster, update_versions
from src.metadata.matching import match_official
from src.metadata.resources import combine


def retag_legacy(catalog, knowledge):
    from tools.gallery_tags import configure_knowledge, build_tags, broad_categories
    from tools.build_gallery import image_versions
    from src.metadata.catalog import revision, validate
    configure_knowledge(knowledge)
    catalog=deepcopy(catalog)
    for item in catalog['items'].values():
        source=catalog['sources'][item['source_id']]
        entry={'associations':[{'original_path':item['path']}],
               'posts':[{**post,'share':source['share'],'remote_path':item['path']} for post in source['posts']]}
        entry['versions']=image_versions(entry)
        item.update(versions=entry['versions'],categories=broad_categories(entry),tags=build_tags(entry))
    catalog['revision']=revision(catalog)
    return validate(catalog)


def annotate(work):
    from tools.gallery_tags import build_tags,broad_categories
    from tools.build_gallery import image_versions
    title=work['title']
    categories=[]
    if 99 in work['channels'] or 73 in work['channels']:categories.append('角色图片')
    if 72 in work['channels']:categories.append('影像档案')
    entry={'posts':[{'title':title,'post_url':work['wiki_url']}]}
    categories.extend(broad_categories(entry))
    work['categories']=sorted(set(categories)-{'未分类'}) or ['未分类']
    entry['versions']=image_versions(entry)
    work['tags']=build_tags(entry)
    for media in work.get('media',[]):
        # Each image gets its own section/caption. Never copy collection keywords
        # or the entire page's character list into every image.
        associations=[{'original_path':section['section']+'/'+section['caption']} for section in media['sections']]
        item_entry={'associations':associations,'posts':[]}
        if 99 in work['channels']:
            item_entry['posts']=[{'title':title,'post_url':work['wiki_url']}]
        item_entry['versions']=image_versions(item_entry)
        media['versions']=item_entry['versions'];media['tags']=build_tags(item_entry)
        media['categories']=sorted(set(work['categories']+broad_categories(item_entry))-{'未分类'})
    return work


def collect(legacy, previous=None, knowledge=None, rows=(), client=None, max_details=12, now=None, refresh_seconds=7*86400, offline=False):
    if not (0 if offline else 1)<=max_details<=100:
        raise ValueError('Invalid detail request budget')
    now=time.time() if now is None else now
    state=deepcopy(previous or {'version':1,'entries':{},'details':{},'checked_at':{},'channels':{}})
    if state.get('version')!=1 or not all(isinstance(state.get(key),dict) for key in ('entries','details','checked_at','channels')):
        raise ValueError('Invalid Wiki cache')
    knowledge=deepcopy(knowledge or load_knowledge())
    report={'version':1,'mode':'offline_cache' if offline else 'online','sources':{},'detail_attempts':0,'detail_completed':0,'failures':[],
            'observed_at':datetime.fromtimestamp(now,timezone.utc).isoformat()}
    owns_client=client is None;client=client or (WikiClient(seconds=180) if not offline else None)
    if offline and not state['entries']:raise ValueError('No cached Wiki index for offline rebuild')
    try:
        try:
            if offline:raise StopIteration
            tree=client.tree()
            labels={node['id']:node['name'] for node in nodes(tree) if 'id' in node and 'name' in node}
            if any(labels.get(cid)!=name for cid,name in MEDIA_CHANNELS.items()):
                raise ValueError('Wiki media taxonomy changed; review required')
            report['sources']['taxonomy']='verified'
            channel=client.channel(13)
            entries,channels=index_entries(channel)
            # Complete current index can add/change entries, but absence does not
            # destroy historical associations or successfully resolved media.
            for ident,entry in entries.items():
                prior=state['entries'].get(ident,{})
                history=set(prior.get('historical_post_urls',[]))
                if prior.get('post_url') and prior['post_url']!=entry.get('post_url'):history.add(prior['post_url'])
                if history:entry['historical_post_urls']=sorted(history)
            state['entries'].update(entries);state['channels']=channels
            report['sources']['media_index']='verified'
        except StopIteration:report['sources']['media_index']='cached'
        except Exception as error:
            report['failures'].append({'stage':'wiki_index','reason':getattr(error,'reason',type(error).__name__)})
        try:
            if offline:raise StopIteration
            knowledge=update_roster(client.channel(2),knowledge)
            report['sources']['roster']='verified'
        except StopIteration:report['sources']['roster']='cached'
        except Exception as error:
            report['failures'].append({'stage':'wiki_roster','reason':getattr(error,'reason',type(error).__name__)})
        knowledge=update_versions(rows,knowledge)
        from tools.gallery_tags import configure_knowledge
        configure_knowledge(knowledge)
        due=[]
        for ident,entry in state['entries'].items():
            cached=state['details'].get(ident)
            checked=state['checked_at'].get(ident,0)
            if not cached or cached.get('index_revision')!=digest(entry) or now-checked>=refresh_seconds:
                due.append((checked,ident,entry))
        due.sort(key=lambda value:(value[0],-int(value[1])))
        # First request from each media category before filling the remaining budget.
        ordered=[];seen=set()
        for channel_id in MEDIA_CHANNELS:
            candidate=next((row for row in due if channel_id in row[2]['channels'] and row[1] not in seen),None)
            if candidate:ordered.append(candidate);seen.add(candidate[1])
        ordered.extend(row for row in due if row[1] not in seen)
        for _,ident,entry in ordered[:0 if offline else max_details]:
            report['detail_attempts']+=1
            state['checked_at'][ident]=now
            try:
                parsed=extract_media(client.detail(ident))
                parsed['links']=sorted(set(parsed['links']+state['details'].get(ident,{}).get('links',[])))
                old_media=state['details'].get(ident,{}).get('media',[])
                merged={item['url']:{**deepcopy(item),'available':False} for item in old_media}
                for item in parsed['media']:
                    prior=merged.get(item['url'])
                    if prior:
                        item['sections']=[*prior['sections'],*(s for s in item['sections'] if s not in prior['sections'])]
                    merged[item['url']]={**item,'available':True}
                parsed['media']=[merged[url] for url in sorted(merged)]
                state['details'][ident]={'index_revision':digest(entry),**parsed}
                report['detail_completed']+=1
            except Exception as error:
                report['failures'].append({'stage':'wiki_detail','wiki_id':ident,'reason':getattr(error,'reason',type(error).__name__)})
        works={}
        for ident,entry in sorted(state['entries'].items()):
            detail=state['details'].get(ident,{'media':[],'links':[],'entry_version':''})
            match=match_official(entry['title'],rows)
            work={'wiki_id':ident,**deepcopy(entry),**deepcopy(detail),
                  'official_match':match['url'],'match_evidence':match,'lang':'zh-cn'}
            work['detail_status']='not_checked' if ident not in state['details'] else 'stale' if detail.get('index_revision')!=digest(entry) else 'verified'
            work.pop('index_revision',None)
            work['links']=sorted(set(work['links']+entry.get('historical_post_urls',[])+([entry['post_url']] if entry.get('post_url') else [])))
            works[stable_id('wiki-work',ident)]=annotate(work)
        legacy=retag_legacy(legacy,knowledge)
        resources=combine(legacy,works,knowledge['revision'])
        report.update(index_entries=len(works),checked_entries=len(state['details']),resolved_entries=sum(bool(work.get('media')) for work in works.values()),media_items=len(resources['items'])-len(legacy['items']),
                      detail_deferred=max(0,len(ordered)-report['detail_attempts']),catalog_revision=resources['revision'],knowledge_revision=knowledge['revision'])
        return resources,state,knowledge,legacy,report
    finally:
        if owns_client and client is not None:client.close()


def retag_gallery(meta,knowledge):
    from tools.gallery_tags import configure_knowledge,build_tags,broad_categories
    from tools.build_gallery import image_versions
    configure_knowledge(knowledge)
    result=deepcopy(meta)
    for item in result.get('items',[]):
        item['versions']=image_versions(item)
        item['categories']=broad_categories(item)
        item['tags']=build_tags(item)
    return result
