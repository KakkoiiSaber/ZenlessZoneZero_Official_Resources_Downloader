"""NAS-only bounded content-addressed media sync. Never changes the Git checkout."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
from urllib.parse import urlencode, urlparse
import requests
from src.downloader.asset_io import atomic_json, check_space, download_bounded
from src.downloader.MinasDownloader import authenticate_with_password
from src.metadata.access import MinasSession, bounded_json, public_access_code
from src.metadata.catalog import validate
from tools.deduplicate_images import POINTER


def positive(name, default):
    value=int(os.getenv(name,str(default)))
    if value<=0:raise ValueError('Invalid positive configuration: '+name)
    return value


def secure_path(root, relative):
    if not isinstance(relative,str) or not re.fullmatch(r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.[a-z0-9]{1,8}',relative):
        raise ValueError('Invalid local object path')
    path=root/relative
    if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError('Symlink data path')
    if root.resolve() not in path.resolve().parents:raise ValueError('Object escaped data root')
    return path


@contextmanager
def data_lock(root):
    root.mkdir(parents=True,exist_ok=True)
    if any(p.is_symlink() for p in [root,*root.parents]):raise ValueError('Symlink data root')
    path=root/'sync.lock'
    if path.is_symlink():raise ValueError('Symlink lock')
    with path.open('a+b') as stream:
        stream.seek(0);stream.write(b'0');stream.flush();stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise RuntimeError('NAS sync already running') from None
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream,fcntl.LOCK_UN)


def load_json(path, default):
    if path.is_symlink():raise ValueError('Symlink state file')
    return json.loads(path.read_text(encoding='utf8')) if path.exists() else default


def item_fingerprint(item):
    return hashlib.sha256(json.dumps({k:item[k] for k in ('source_id','path','size','last_modified')},sort_keys=True).encode()).hexdigest()


def safe_reason(error):
    messages={
        'Download exceeds size limit':'download_size_limit',
        'Streamed download exceeds size limit':'download_size_limit',
        'Download duration limit exceeded':'download_timeout',
        'Remote size changed during scan':'remote_size_changed',
        'Incomplete asset download':'incomplete_download','Incomplete download':'incomplete_download',
        'Unexpected HTML instead of asset':'unexpected_html',
        'Insufficient free disk space (including reserve)':'disk_reserve',
        'NAS byte budget exceeded':'byte_budget','Existing object hash mismatch':'local_hash_mismatch',
        'Orphan object hash mismatch':'local_hash_mismatch',
        'access_code_required':'access_code_required','source_auth_failed':'source_auth_failed'}
    return messages.get(str(error),type(error).__name__)


class Sync:
    def __init__(self, root, session_factory=MinasSession, authenticate=authenticate_with_password,
                 code_resolver=public_access_code, now=None):
        self.root=Path(root);self.session_factory=session_factory;self.authenticate=authenticate
        self.code_resolver=code_resolver;self.now=time.time() if now is None else now
        self.max_sources=positive('NAS_RUN_MAX_SOURCES',32)
        self.max_files=positive('NAS_RUN_MAX_FILES',128);self.max_bytes=positive('NAS_RUN_MAX_BYTES',2*1024**3)
        self.image_max=positive('NAS_IMAGE_MAX_BYTES',256*1024**2);self.video_max=positive('NAS_VIDEO_MAX_BYTES',1024**3)
        self.verify_max=positive('NAS_RUN_VERIFY_FILES',4);self.refresh=positive('NAS_VERIFY_DAYS',7)*86400
        state_dir=self.root/'state'
        if state_dir.is_symlink():raise ValueError('Symlink state directory')
        state_dir.mkdir(parents=True,exist_ok=True)
        self.state_path=state_dir/'sync.json'
        self.state=load_json(self.state_path,{'version':1,'items':{},'objects':{}})
        if self.state.get('version')!=1 or not all(isinstance(self.state.get(k),dict) for k in ['items','objects']):
            raise ValueError('Invalid NAS state')
        self.report={'version':1,'observed_at':datetime.fromtimestamp(self.now,timezone.utc).isoformat(),'completed':0,'attempted':0,'unchanged':0,'deferred':0,'missing':0,
                     'bytes':0,'new_objects':0,'failures':[]}
        self.verifications=0

    def save(self):
        atomic_json(self.state_path,self.state)
        atomic_json(self.root/'state'/'last-sync.json',self.report)

    def local_object(self, old):
        if not old or not re.fullmatch('[a-f0-9]{64}',str(old.get('sha256',''))):return False
        obj=self.state['objects'].get(old['sha256'])
        if not obj:return False
        path=secure_path(self.root,obj['path'])
        if not path.is_file() or path.stat().st_size!=obj['size']:return False
        with path.open('rb') as stream:
            if stream.read(128).startswith(b'version https://git-lfs.github.com/spec/v1'):return False
        return True

    def receive(self, session, source, item):
        maximum=self.image_max if item['media_type']=='image' else self.video_max
        if item['size']>maximum or item['size']>self.max_bytes:
            return 'oversize'
        if self.report['attempted']>=self.max_files or self.report['bytes']+item['size']>self.max_bytes:
            return 'budget'
        check_space(self.root,item['size']);self.report['attempted']+=1
        with tempfile.TemporaryDirectory(prefix='.nas-',dir=self.root) as work:
            staged=Path(work)/'asset.part'
            def consume(n):
                self.report['bytes']+=n
                if self.report['bytes']>self.max_bytes:raise ValueError('NAS byte budget exceeded')
            result=download_bounded(session,source['share']+'files/?'+urlencode({'p':item['path'],'dl':'1'}),
                                    staged,maximum,expected_size=item['size'],consume=consume)
            sha=result['sha256'];obj=self.state['objects'].get(sha)
            if obj:
                destination=secure_path(self.root,obj['path'])
                if destination.exists():
                    with destination.open('rb') as stream:
                        head=stream.read(1024);stream.seek(0)
                        pointer=POINTER.fullmatch(head)
                        matches_pointer=bool(pointer and pointer[1].decode()==sha and int(pointer[2])==result['bytes'])
                        if not matches_pointer and hashlib.file_digest(stream,'sha256').hexdigest()!=sha:raise ValueError('Existing object hash mismatch')
                    if matches_pointer:os.replace(staged,destination)
                else:destination.parent.mkdir(parents=True,exist_ok=True);os.replace(staged,destination)
            else:
                suffix=PurePosixPath(item['path']).suffix.lower()
                relative='objects/'+sha[:2]+'/'+sha+suffix
                destination=secure_path(self.root,relative);destination.parent.mkdir(parents=True,exist_ok=True)
                if destination.exists():
                    with destination.open('rb') as stream:
                        if hashlib.file_digest(stream,'sha256').hexdigest()!=sha:raise ValueError('Orphan object hash mismatch')
                else:os.replace(staged,destination)
                obj={'path':relative,'size':result['bytes'],'items':[]};self.state['objects'][sha]=obj
                self.report['new_objects']+=1
            if item['id'] not in obj['items']:obj['items'].append(item['id'])
            prior=self.state['items'].get(item['id'],{})
            history=list(prior.get('history',[]))
            if prior.get('sha256') and prior['sha256']!=sha:
                history.append({'sha256':prior['sha256'],'verified_at':prior.get('verified_at')})
            self.state['items'][item['id']]={'history':history,'sha256':sha,'fingerprint':item_fingerprint(item),
                'verified_at':self.now,'checked_at':self.now,'etag':result.get('etag')}
            self.report['completed']+=1;self.save()
        return None

    def run(self, catalog, codes=None):
        validate(catalog);self.report['catalog_revision']=catalog['revision'];codes=codes or {};sessions={};failed_sources={};attempted_sources=set()
        # Persist complete associations separately, including changes with identical bytes.
        atomic_json(self.root/'state'/'catalog.json',catalog)
        try:
            ordered=sorted(catalog['items'].values(),key=lambda item:(self.state['items'].get(item['id'],{}).get('checked_at',0),item['id']))
            for item in ordered:
                if not item['available']:self.report['missing']+=1;continue
                old=self.state['items'].get(item['id']);local=self.local_object(old)
                changed=not local or old.get('fingerprint')!=item_fingerprint(item)
                due=bool(old and self.now-old.get('verified_at',0)>=self.refresh)
                if not changed and not due:
                    self.report['unchanged']+=1;continue
                if due and not changed and self.verifications>=self.verify_max:
                    self.report['deferred']+=1;continue
                # Check budgets before resolving codes or making transfer requests.
                maximum=self.image_max if item['media_type']=='image' else self.video_max
                if item['size']>maximum or item['size']>self.max_bytes:
                    self.report['deferred']+=1
                    self.report['failures'].append({'item_id':item['id'],'source_id':item['source_id'],'reason':'oversize'});continue
                if self.report['attempted']>=self.max_files or self.report['bytes']+item['size']>self.max_bytes:
                    self.report['deferred']+=1;continue
                if item['source_id'] in failed_sources:
                    self.report['deferred']+=1;continue
                try:
                    source=catalog['sources'][item['source_id']]
                    if item['source_id'] not in sessions:
                        if len(attempted_sources)>=self.max_sources:
                            self.report['deferred']+=1;continue
                        attempted_sources.add(item['source_id'])
                        failed_sources[item['source_id']]=True
                        code=codes.get(item['source_id'],codes.get(source['share']))
                        if code is None and self.code_resolver:code=self.code_resolver(source)
                        if code is None:raise RuntimeError('access_code_required')
                        session=self.session_factory()
                        try:self.authenticate(session,source['share'],str(code))
                        except Exception:
                            session.close();failed_sources[item['source_id']]=True;raise
                        sessions[item['source_id']]=session
                        failed_sources.pop(item['source_id'],None)
                    if due and not changed:self.verifications+=1
                    reason=self.receive(sessions[item['source_id']],source,item)
                    if reason:self.report['deferred']+=1
                except Exception as error:
                    reason=safe_reason(error)
                    response=getattr(error,'response',None)
                    if getattr(response,'status_code',None) in (401,403):failed_sources[item['source_id']]=True
                    self.report['failures'].append({'item_id':item['id'],'source_id':item['source_id'],'reason':reason,'http_status':getattr(response,'status_code',None)})
                finally:self.save()
        finally:
            for session in sessions.values():session.close()
            self.save()
        return self.report


def read_catalog(url, session_factory=requests.Session):
    p=urlparse(url)
    if p.scheme!='https' or p.netloc not in ('raw.githubusercontent.com','kakkoiisaber.github.io') or p.query or p.fragment:
        raise ValueError('Unsupported catalog URL')
    with session_factory() as session:
        return validate(bounded_json(session,url,allow_redirects=False))


def once(root, url=None, manifest=None, codes_path=None):
    with data_lock(Path(root)):
        catalog=validate(json.loads(Path(manifest).read_text(encoding='utf8'))) if manifest else read_catalog(url)
        config=load_json(Path(codes_path),{'version':1,'shares':{}}) if codes_path else {'version':1,'shares':{}}
        if set(config)!={'version','shares'} or config['version']!=1 or not isinstance(config['shares'],dict):raise ValueError('Invalid private code configuration')
        return Sync(root).run(catalog,config['shares'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',default=os.getenv('NAS_DATA_DIR','/data'))
    parser.add_argument('--url',default=os.getenv('NAS_MANIFEST_URL','https://raw.githubusercontent.com/KakkoiiSaber/zzz_scraper/main/metafiles/catalog.json'))
    parser.add_argument('--manifest');parser.add_argument('--codes',default=os.getenv('NAS_CODES_FILE'))
    parser.add_argument('--interval',type=int,default=0)
    args=parser.parse_args()
    if args.interval and args.interval<3600:parser.error('interval must be at least 3600 seconds')
    while True:
        try:
            report=once(args.data,args.url,args.manifest,args.codes);print(json.dumps(report),flush=True)
            code=1 if report['failures'] else 0
        except Exception as error:
            print(json.dumps({'status':'failed','reason':type(error).__name__}),flush=True);code=1
        if not args.interval:raise SystemExit(code)
        time.sleep(args.interval)

if __name__=='__main__':main()
