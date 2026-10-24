#!/usr/bin/env python3
"""Metadata-only collection; private link/code CSVs exist only in a temporary directory."""
import argparse
import contextlib
import csv
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.downloader.asset_io import atomic_json
from src.metadata.catalog import collect, validate, public_post
from src.scraper.PostScraper import PostScraper
from src.scraper.MinasScraper import MinasScraper


def read_rows(path):
    with path.open(encoding='utf-8-sig',newline='') as stream:return list(csv.DictReader(stream))


def refresh_rows():
    tasks=json.loads(Path('src/config/task.json').read_text(encoding='utf8'))
    quiet=logging.getLogger('metadata-private');quiet.handlers=[logging.NullHandler()];quiet.propagate=False
    with tempfile.TemporaryDirectory(prefix='zzz-metadata-') as private:
        private=Path(private)
        for entry in tasks:
            if entry.get('type')=='post':
                print('[metadata] Refreshing public post index',flush=True)
                PostScraper(entry['name'],logger=quiet).run()
        rows=[]
        for entry in tasks:
            if entry.get('type')!='minas':continue
            scraper=MinasScraper(entry['name'],logger=quiet)
            output=private/(entry['name']+'.csv')
            if scraper.out_csv.exists():shutil.copyfile(scraper.out_csv,output)
            scraper.out_csv=output
            with open(os.devnull,'w') as silent, contextlib.redirect_stdout(silent):
                prior=os.environ.get('MINAS_POST_EVIDENCE_FILE')
                os.environ['MINAS_POST_EVIDENCE_FILE']=str(private/'evidence.json')
                try:scraper.run()
                finally:
                    if prior is None:os.environ.pop('MINAS_POST_EVIDENCE_FILE',None)
                    else:os.environ['MINAS_POST_EVIDENCE_FILE']=prior
            rows.extend(read_rows(output))
        # Only public index fields may enter the metadata commit.
        secrets={str(row.get('minas_pwd') or '').strip().strip('\"\'') for row in rows}
        for path in Path('posts').glob('*.csv'):
            safe=[]
            for row in read_rows(path):
                post=public_post(row,secrets)
                safe.append({'post_time':post['post_time'],'post_name':post['title'],'post_url':post['post_url']})
            with path.open('w',encoding='utf8',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=['post_time','post_name','post_url']);writer.writeheader();writer.writerows(safe)
        return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--refresh-posts',action='store_true')
    args=parser.parse_args();output=Path('metafiles/catalog.json');status=Path('metafiles/catalog-status.json')
    try:
        rows=refresh_rows() if args.refresh_posts else [row for path in Path('metafiles').glob('*.csv') for row in read_rows(path)]
        previous=validate(json.loads(output.read_text(encoding='utf8'))) if output.exists() else None
        catalog,report=collect(rows,previous)
        report['observed_at']=datetime.now(timezone.utc).isoformat();report['revision']=catalog['revision']
        atomic_json(output,catalog);atomic_json(status,report)
        print(json.dumps(report),flush=True)
        return 1 if report['failures'] else 0
    except Exception as error:
        report={'version':1,'status':'failed','reason':type(error).__name__,
                'observed_at':datetime.now(timezone.utc).isoformat()}
        atomic_json(status,report)
        print(json.dumps(report),flush=True)
        return 1

if __name__=='__main__':raise SystemExit(main())
