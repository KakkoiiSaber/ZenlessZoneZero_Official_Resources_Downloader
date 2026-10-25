"""Read-only, bounded validation of four representative Wiki media categories."""
from pathlib import Path
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.metadata.wiki import WikiClient,extract_media,index_entries


def main():
    client=WikiClient(seconds=120)
    result={'version':1,'checks':{},'failures':[],'media_requests':0}
    try:
        for label,operation in [('taxonomy',client.tree),('media_index',lambda:client.channel(13)),('roster',lambda:client.channel(2))]:
            print(json.dumps({'event':'wiki_preflight','stage':label}),flush=True)
            try:
                data=operation()
                if label=='media_index':
                    entries,channels=index_entries(data);result['channels']=channels
                result['checks'][label]='passed'
            except Exception as error:
                result['failures'].append({'stage':label,'reason':getattr(error,'reason',type(error).__name__)})
        for label,ident in [('character_video',2236),('game_video',2255),('wallpaper',685),('affection',1890)]:
            print(json.dumps({'event':'wiki_preflight','stage':label,'wiki_id':ident}),flush=True)
            try:
                parsed=extract_media(client.detail(ident))
                result['checks'][label]={'wiki_id':ident,'media':len(parsed['media']),'links':len(parsed['links']),'entry_version':parsed['entry_version']}
            except Exception as error:
                result['failures'].append({'stage':label,'wiki_id':ident,'reason':getattr(error,'reason',type(error).__name__)})
    finally:client.close()
    print(json.dumps(result),flush=True)
    return 1 if result['failures'] else 0


if __name__=='__main__':raise SystemExit(main())
