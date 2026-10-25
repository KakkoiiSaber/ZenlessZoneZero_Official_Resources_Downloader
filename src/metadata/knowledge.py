"""Sourced, additive classification knowledge; a failed fetch never empties the roster."""
from __future__ import annotations
from copy import deepcopy
import csv
import json
from pathlib import Path
import re
from src.metadata.wiki import nodes, safe_text, digest, WikiError


def normal(value):
    return re.sub(r'[\s「」『』“”"\']', '', value)


def validate(data):
    if not isinstance(data, dict) or set(data) != {'version','revision','characters','groups','versions','debuts'} or data['version'] != 1:
        raise ValueError('Invalid knowledge schema')
    if data['revision'] != digest({k:v for k,v in data.items() if k!='revision'}):
        raise ValueError('Invalid knowledge revision')
    if not all(isinstance(data[key], dict) for key in ('characters','groups','versions','debuts')):
        raise ValueError('Invalid knowledge maps')
    if not 1 <= len(data['characters']) <= 1000:
        raise ValueError('Invalid roster size')
    for ident, char in data['characters'].items():
        if not isinstance(ident,str) or not char.get('name') or not isinstance(char.get('aliases'),list) or not char.get('source_url'):
            raise ValueError('Invalid sourced character')
    return data


def finish(data):
    data['revision'] = digest({k:v for k,v in data.items() if k!='revision'})
    return validate(data)


def load_knowledge():
    local = Path('metafiles/knowledge.json')
    path = local if local.is_file() else Path(__file__).with_name('knowledge.json')
    return validate(json.loads(path.read_text(encoding='utf8')))


def update_roster(channel, previous):
    data = deepcopy(validate(previous))
    roster = next((node for node in nodes(channel.get('list',[])) if node.get('id')==43 and isinstance(node.get('list'),list)),None)
    if roster is None or not roster['list']:
        raise WikiError('wiki_missing_roster')
    for entry in roster['list']:
        if not isinstance(entry.get('content_id'),int) or not safe_text(entry.get('title')):
            raise WikiError('wiki_invalid_character')
        full = normal(safe_text(entry['title']))
        alias = normal(safe_text(entry.get('alias_name')))
        ident = 'wiki:'+str(entry['content_id'])
        url = 'https://baike.mihoyo.com/zzz/wiki/content/'+str(entry['content_id'])+'/detail'
        matches = [key for key,value in data['characters'].items() if key.startswith('legacy:') and
                   ({normal(value['name']),*(normal(a) for a in value['aliases'])}&{full,alias})]
        # A duo is a group, not an automatically invented third individual.
        if '&' in full:
            members = [value['name'] for value in data['characters'].values() if value['name'] in full]
            data['groups'][full] = {'name':full,'members':sorted(set(members)),'source_url':url}
            continue
        prior = data['characters'].get(ident) or (data['characters'][matches[0]] if len(matches)==1 else {})
        name = prior.get('name') or alias or full
        aliases = sorted({full,entry['title'],alias,name,*prior.get('aliases',[])}-{''})
        char = {'name':name,'full_name':full,'aliases':aliases,'wiki_id':str(entry['content_id']),
                'source_url':url,'status':'indexed','evidence_kind':'wiki_roster'}
        # No release version is inferred from Wiki order, revision or appearance date.
        data['characters'][ident] = char
        for key in matches:
            del data['characters'][key]
        try:
            filters = json.loads(entry.get('ext') or '{}').get('c_43',{}).get('filter',{}).get('text','[]')
            filters = json.loads(filters) if isinstance(filters,str) else filters
        except (ValueError,TypeError):
            filters = []
        for value in filters if isinstance(filters,list) else []:
            if isinstance(value,str) and value.startswith('阵营/'):
                faction = safe_text(value.split('/',1)[1])
                if not faction:continue
                group = data['groups'].setdefault(faction,{'name':faction,'members':[],'source_url':url})
                if name not in group['members']:
                    group['members'].append(name);group['members'].sort()
                char['faction'] = faction
    # Preserve forms as separate IDs. Link only uniquely evidenced full-name suffixes.
    for ident,char in data['characters'].items():
        full = char.get('full_name','')
        bases = [(key,value) for key,value in data['characters'].items() if key!=ident and value.get('full_name') and full.endswith('·'+value['full_name'])]
        if len(bases)==1:
            key,base = bases[0];char['form_of']=key;char['form_name']=full[:-len(base['full_name'])-1]
    return finish(data)


def update_versions(rows, previous):
    data = deepcopy(previous)
    for row in rows:
        url = row.get('post_url','')
        if not re.fullmatch(r'https://(?:zzz\.mihoyo\.com|zenless\.hoyoverse\.com/zh-cn)/news/\d+',url):
            continue
        match = re.search(r'(?<![\d.])(\d{1,2}\.\d{1,2})版本[「“]([^」”]+)[」”]',row.get('post_name',''))
        if not match:
            continue
        version,name = match.groups();known=data['versions'].get(version)
        if known and known['name']!=name:
            conflict={'name':name,'source_url':url}
            if conflict not in known.setdefault('conflicts',[]):known['conflicts'].append(conflict)
            continue  # Conflicting names require review; never replace a verified name silently.
        data['versions'].setdefault(version,{'name':name,'source_url':url,'evidence_kind':'official_news_title'})
    return finish(data)


def read_post_rows(root=Path('posts')):
    rows=[]
    for path in sorted(root.glob('*.csv')):
        with path.open(encoding='utf-8-sig',newline='') as stream:
            rows.extend(csv.DictReader(stream))
    return rows


def tables(data):
    chars={char['name']:char['aliases'] for char in data['characters'].values()}
    debuts={name:{'version':value['version'],'source_url':value['source_url']} for name,value in data['debuts'].items()}
    versions={number:(value['name'],value['source_url']) for number,value in data['versions'].items()}
    return chars,debuts,versions
