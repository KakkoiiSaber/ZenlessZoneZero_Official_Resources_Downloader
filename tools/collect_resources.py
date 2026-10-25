"""Publish metadata-only Wiki catalog and retag cached gallery entries; no media IO."""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.downloader.asset_io import atomic_json
from src.metadata.pipeline import collect,retag_gallery
from src.metadata.knowledge import read_post_rows,load_knowledge
from src.metadata.catalog import validate


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-details',type=int,default=12)
    parser.add_argument('--offline',action='store_true',help='Rebuild only from existing public metadata caches; no network')
    args=parser.parse_args()
    legacy=validate(json.loads(Path('metafiles/catalog.json').read_text(encoding='utf8')))
    state_path=Path('metafiles/wiki-state.json')
    state=json.loads(state_path.read_text(encoding='utf8')) if state_path.exists() else None
    resources,state,knowledge,legacy,report=collect(legacy,state,load_knowledge(),read_post_rows(),max_details=args.max_details,offline=args.offline)
    # Validate all computed outputs before any file replacement/checkpoint.
    validate(resources);validate(legacy)
    meta_path=Path('docs/meta.json')
    meta=retag_gallery(json.loads(meta_path.read_text(encoding='utf8')),knowledge) if meta_path.exists() else None
    if meta is not None:
        old=json.loads(meta_path.read_text(encoding='utf8'))
        assert len(old['items'])==len(meta['items'])
        for before,after in zip(old['items'],meta['items']):
            assert {k:v for k,v in before.items() if k not in {'tags','versions','categories'}}=={k:v for k,v in after.items() if k not in {'tags','versions','categories'}}
    atomic_json(Path('metafiles/resource-catalog.json'),resources)
    atomic_json(state_path,state)
    atomic_json(Path('metafiles/knowledge.json'),knowledge)
    atomic_json(Path('metafiles/catalog.json'),legacy)
    atomic_json(Path('metafiles/resource-status.json'),report)
    if meta is not None:atomic_json(meta_path,meta)
    # Site index includes curated metadata and links, never downloads originals.
    public={'version':1,'revision':resources['revision'],'works':list(resources['works'].values()),'status':report,
            'media':{iid:{k:item[k] for k in ('id','url','media_type','quality','tags','versions','origins')} for iid,item in resources['items'].items() if 'url' in item}}
    atomic_json(Path('docs/resources.json'),public)
    print(json.dumps(report),flush=True)
    return 1 if report['failures'] else 0


if __name__=='__main__':raise SystemExit(main())
