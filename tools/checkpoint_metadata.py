"""Checkpoint only public metadata, never originals, previews or private CSVs."""
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.checkpoint_update import checkpoint
from src.metadata.catalog import validate
import json

if __name__=='__main__':
    validate(json.loads(Path('metafiles/catalog.json').read_text(encoding='utf8')))
    resources=Path('metafiles/resource-catalog.json')
    if resources.exists():validate(json.loads(resources.read_text(encoding='utf8')))
    evidence=Path('metafiles/post-evidence.json')
    if evidence.exists():
        from src.metadata.catalog import public_values
        public_values(json.loads(evidence.read_text(encoding='utf8')))
    result=checkpoint(Path.cwd(),True,os.environ['GITHUB_RUN_ID']+'-'+os.getenv('GITHUB_RUN_ATTEMPT','1'),
                      scopes=('posts','metafiles/catalog.json','metafiles/catalog-status.json',
                              'metafiles/resource-catalog.json','metafiles/resource-status.json','metafiles/wiki-state.json',
                              'metafiles/knowledge.json','metafiles/post-evidence.json','docs/meta.json','docs/resources.json'))
    print(json.dumps(result))
    if result['mode']=='recovery':raise SystemExit('Metadata retained on recovery branch; main advanced')
