import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs,urlparse
from src.metadata import catalog as cat
from src.metadata.access import public_access_code,stable_id
from src.nas import sync

SHARE='https://minas.mihoyo.com/d/fixtureA/'
SECOND='https://minas.mihoyo.com/d/fixtureB/'
CODE='private-fixture-code'
STAMP='2025-01-01T00:00:00+00:00'
NOW=1800000000


def row(title='影像档案记录分享 2.3版本',share=SHARE,post='1'):
    return {'minas_link':share,'minas_pwd':CODE,'post_name':title,
            'post_url':'https://www.miyoushe.com/zzz/article/'+post,'post_time':'2025-01-01'}


def catalog(paths=None,rows=None,previous=None):
    paths=paths or {'/v2.2/a.jpg':b'original-image'}
    listing=lambda session,share:{p:{'size':len(b),'last_modified':STAMP} for p,b in paths.items()}
    return cat.collect(rows or [row()],previous,session_factory=lambda:Session(paths),authenticate=Mock(),listing=listing)[0]


class Response:
    def __init__(self,body,broken=False,headers=None):
        self.body=body;self.broken=broken;self.headers=headers or {'Content-Length':str(len(body)),'Content-Type':'application/octet-stream'}
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def raise_for_status(self):pass
    def iter_content(self,size):
        yield self.body[:2]
        if self.broken:raise OSError('private URL and code must never be reported')
        yield self.body[2:]


class Session:
    def __init__(self,paths,broken=()):self.paths=paths;self.broken=broken;self.calls=[]
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def close(self):pass
    def get(self,url,**kwargs):
        path=parse_qs(urlparse(url).query)['p'][0];self.calls.append(path)
        return Response(self.paths[path],path in self.broken)


class CatalogTests(unittest.TestCase):
    def test_stable_ids_rerun_nested_video_and_secret_free(self):
        paths={'/v2.2/a.jpg':b'photo','/v2.2/clip.mp4':b'video','/Thumbs.db':b'ignore'}
        first=catalog(paths);second=catalog(paths,previous=first)
        self.assertEqual(first,second);self.assertEqual(2,len(first['items']))
        self.assertEqual({'image','video'},{v['media_type'] for v in first['items'].values()})
        self.assertNotIn(CODE,json.dumps(first));self.assertNotIn('minas_pwd',json.dumps(first))
        for item in first['items'].values():self.assertEqual(['2.2'],item['versions'])
    def test_unchanged_share_additions_and_missing_keep_history(self):
        first=catalog({'/a.jpg':b'a'})
        second=catalog({'/a.jpg':b'a','/new/clip.webm':b'video'},previous=first)
        third=catalog({'/new/clip.webm':b'video'},previous=second)
        self.assertEqual(2,len(third['items']));self.assertEqual(1,sum(not x['available'] for x in third['items'].values()))
    def test_repeated_links_share_one_listing_and_keep_distinct_shares(self):
        listing=Mock(return_value={'/a.jpg':{'size':1,'last_modified':STAMP}})
        result,_=cat.collect([row(post='1'),row(post='2'),row(share=SECOND,post='3')],session_factory=lambda:Session({}),authenticate=Mock(),listing=listing)
        self.assertEqual(2,listing.call_count);self.assertEqual(2,len(result['sources']))
        self.assertEqual([1,2],sorted(len(v['posts']) for v in result['sources'].values()))
    def test_failed_listing_retains_existing_items(self):
        first=catalog()
        listing=Mock(side_effect=OSError('private-token'))
        second,report=cat.collect([row()],first,session_factory=lambda:Session({}),authenticate=Mock(),listing=listing)
        self.assertEqual(first,second);self.assertEqual('OSError',report['failures'][0]['reason'])
        self.assertNotIn('private-token',json.dumps(report))
    def test_query_credentials_and_unsafe_paths_rejected(self):
        bad=catalog();iid=next(iter(bad['items']));bad['items'][iid]['path']='/../a.jpg';bad['revision']=cat.revision(bad)
        with self.assertRaises(ValueError):cat.validate(bad)
        from src.metadata.access import share_url
        with self.assertRaises(ValueError):share_url(SHARE+'?token=private')
    def test_explicit_version_is_per_cumulative_subfolder_not_post(self):
        result=catalog({'/v1.7/a.jpg':b'a','/unknown/b.jpg':b'b'})
        items={i['path']:i for i in result['items'].values()}
        self.assertEqual(['1.7'],items['/v1.7/a.jpg']['versions'])
        self.assertEqual(['未分类'],items['/unknown/b.jpg']['versions'])
    def test_same_name_replacement_keeps_id_changes_revision(self):
        first=catalog({'/a.jpg':b'one'});second=catalog({'/a.jpg':b'replacement'})
        self.assertEqual(set(first['items']),set(second['items']));self.assertNotEqual(first['revision'],second['revision'])
    def test_matching_public_post_code_only_and_ambiguity_fails_closed(self):
        source={'share':SHARE,'posts':[{'post_url':row()['post_url']}]}
        body={'data':{'post':{'post':{'content':SHARE+' 提取码：ABCD','structured_content':'[]'}}}}
        response=Response(json.dumps(body).encode())
        session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock();session.get.return_value=response
        self.assertEqual('ABCD',public_access_code(source,lambda:session))
        body['data']['post']['post']['content']+=SECOND
        session.get.return_value=Response(json.dumps(body).encode())
        self.assertIsNone(public_access_code(source,lambda:session))
    def test_file_level_debut_rule_requires_matching_character_role_directory(self):
        rows=[row('壁纸分享 | 薇薇安主题壁纸')]
        data=catalog({'/薇薇安角色展示/a.jpg':b'a','/薇薇安EP/b.jpg':b'b'},rows=rows)
        by_path={item['path']:item for item in data['items'].values()}
        role=by_path['/薇薇安角色展示/a.jpg']['tags']
        ep=by_path['/薇薇安EP/b.jpg']['tags']
        self.assertTrue(any(t['confidence']=='rule_supported' and t['value']=='1.7' for t in role))
        self.assertFalse(any(t['confidence']=='rule_supported' for t in ep))

    def test_mutated_or_extra_manifest_fields_rejected(self):
        data=catalog();data['minas_pwd']='private'
        with self.assertRaises(ValueError):cat.validate(data)


class NASTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'data'
        self.env=patch.dict(os.environ,{'MINAS_MIN_FREE_BYTES':'1'});self.env.start()
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def worker(self,paths,now=NOW,broken=()):
        self.session=Session(paths,broken);self.auth=Mock()
        return sync.Sync(self.root,session_factory=lambda:self.session,authenticate=self.auth,code_resolver=lambda s:CODE,now=now)
    def test_images_video_and_unchanged_rerun(self):
        paths={'/a.jpg':b'photo','/clip.mp4':b'video'};data=catalog(paths)
        first=self.worker(paths).run(data);self.assertEqual(2,first['completed'])
        second=self.worker(paths).run(data);self.assertEqual(2,second['unchanged']);self.assertEqual([],self.session.calls)
        self.assertEqual(2,len(list((self.root/'objects').rglob('*.*'))))
    def test_content_dedup_preserves_multi_post_and_source_associations(self):
        paths={'/a.jpg':b'same','/hires/a.png':b'different','/copy.jpg':b'same'}
        data=catalog(paths,rows=[row(post='1'),row(share=SECOND,post='2')])
        worker=self.worker(paths);report=worker.run(data)
        self.assertEqual(2,len(worker.state['objects']));self.assertEqual(6,report['completed'])
        self.assertEqual(6,sum(len(o['items']) for o in worker.state['objects'].values()))
        stored=json.loads((self.root/'state/catalog.json').read_text(encoding='utf8'));self.assertEqual(2,len(stored['sources']))
    def test_changed_same_name_keeps_prior_object_and_history(self):
        first=catalog({'/a.jpg':b'one'});w=self.worker({'/a.jpg':b'one'});w.run(first)
        second=catalog({'/a.jpg':b'longer'});w=self.worker({'/a.jpg':b'longer'},NOW+1);w.run(second)
        self.assertEqual(2,len(w.state['objects']));self.assertEqual(1,len(next(iter(w.state['items'].values()))['history']))
    def test_invisible_replacement_is_found_by_bounded_refresh(self):
        data=catalog({'/a.jpg':b'one'});self.worker({'/a.jpg':b'one'}).run(data)
        worker=self.worker({'/a.jpg':b'two'},NOW+8*86400);worker.run(data)
        self.assertEqual(2,len(worker.state['objects']))
    def test_partial_failure_checkpoint_retry_does_not_redownload_success(self):
        paths={'/a.jpg':b'photo','/b.mp4':b'video'};data=catalog(paths)
        first=self.worker(paths,broken={'/b.mp4'}).run(data)
        self.assertEqual(1,first['completed']);self.assertEqual(1,len(first['failures']))
        self.assertNotIn('private URL',json.dumps(first));self.assertFalse(list(self.root.glob('.nas-*')))
        second=self.worker(paths).run(data);self.assertEqual(1,second['completed']);self.assertEqual(['/b.mp4'],self.session.calls)
    def test_budget_deferred_and_oversize_preflight_no_network(self):
        paths={'/a.jpg':b'photo','/b.jpg':b'other'};data=catalog(paths)
        w=self.worker(paths);w.max_files=1;report=w.run(data);self.assertEqual(1,report['deferred'])
        w=self.worker(paths,NOW+1);w.image_max=1;report=w.run(catalog({'/new.jpg':b'large'}))
        self.assertEqual(1,report['deferred']);self.assertEqual([],self.session.calls);self.auth.assert_not_called()
    def test_pointer_only_existing_object_is_not_trusted(self):
        paths={'/a.jpg':b'x'*140};data=catalog(paths);w=self.worker(paths);w.run(data)
        obj=next(iter(w.state['objects'].values()));p=self.root/obj['path']
        pointer=b'version https://git-lfs.github.com/spec/v1\noid sha256:'+b'0'*64+b'\nsize 140\n'
        p.write_bytes(pointer.ljust(140,b' '))
        w=self.worker(paths)
        # Existing object corruption fails closed rather than silently blessing the pointer.
        report=w.run(data);self.assertEqual(0,report['unchanged']);self.assertEqual(1,len(self.session.calls));self.assertEqual(1,len(report['failures']))
    def test_valid_pointer_is_rehydrated_only_after_matching_hash_and_size(self):
        paths={'/a.jpg':b'photo'};data=catalog(paths);w=self.worker(paths);w.run(data)
        sha,obj=next(iter(w.state['objects'].items()))
        pointer=f'version https://git-lfs.github.com/spec/v1\noid sha256:{sha}\nsize 5\n'.encode()
        (self.root/obj['path']).write_bytes(pointer)
        report=self.worker(paths).run(data)
        self.assertEqual(1,report['completed']);self.assertEqual(b'photo',(self.root/obj['path']).read_bytes())
    def test_unprotected_share_can_use_an_explicit_empty_code(self):
        paths={'/a.jpg':b'photo'};w=self.worker(paths);w.code_resolver=lambda source:''
        report=w.run(catalog(paths));self.assertEqual(1,report['completed'])
        self.auth.assert_called_once_with(self.session,SHARE,'')
    def test_source_budget_prevents_extra_auth_or_transfers(self):
        paths={'/a.jpg':b'photo'};data=catalog(paths,rows=[row(),row(share=SECOND,post='2')])
        w=self.worker(paths);w.max_sources=1;report=w.run(data)
        self.assertEqual(1,report['completed']);self.assertEqual(1,report['deferred']);self.assertEqual(1,self.auth.call_count)

    def test_missing_manifest_paths_do_not_delete_prior_objects(self):
        paths={'/a.jpg':b'photo'};data=catalog(paths);w=self.worker(paths);w.run(data)
        for item in data['items'].values():item['available']=False
        data['revision']=cat.revision(data);w=self.worker(paths);report=w.run(data)
        self.assertEqual(1,report['missing']);self.assertEqual(1,len(w.state['objects']));self.assertEqual([],self.session.calls)
    def test_refresh_quota_is_bounded_and_fair(self):
        paths={f'/{i}.jpg':str(i).encode() for i in range(4)};data=catalog(paths);self.worker(paths).run(data)
        w=self.worker(paths,NOW+8*86400);w.verify_max=1;report=w.run(data)
        self.assertEqual(1,report['completed']);self.assertEqual(3,report['deferred'])
        first=set(self.session.calls);w=self.worker(paths,NOW+8*86400+1);w.verify_max=1;w.run(data)
        self.assertNotEqual(first,set(self.session.calls))
    def test_exclusive_lock_and_no_duplicate_concurrent_sync(self):
        with sync.data_lock(self.root):
            with self.assertRaises(RuntimeError):
                with sync.data_lock(self.root):pass
        with sync.data_lock(self.root):pass
    def test_untrusted_catalog_host_and_revision_rejected(self):
        with self.assertRaises(ValueError):sync.read_catalog('http://127.0.0.1/private')
        data=catalog();data['revision']='bad'
        with self.assertRaises(ValueError):self.worker({}).run(data)
    def test_chunked_partial_stream_cannot_exceed_budget_or_commit(self):
        paths={'/a.mp4':b'123456'};data=catalog(paths)
        item=next(iter(data['items'].values()));item['size']=4;data['revision']=cat.revision(data)
        w=self.worker(paths);report=w.run(data)
        self.assertEqual(0,report['completed']);self.assertEqual({},w.state['items'])
        self.assertFalse(list((self.root/'objects').glob('**/*')))
    def test_failed_source_auth_is_attempted_once_and_recovers_next_run(self):
        paths={'/a.jpg':b'a','/b.jpg':b'b','/c.mp4':b'c'};data=catalog(paths)
        worker=self.worker(paths);worker.authenticate=Mock(side_effect=RuntimeError('private auth response'))
        report=worker.run(data)
        worker.authenticate.assert_called_once();self.assertEqual([],self.session.calls)
        self.assertEqual(1,len(report['failures']));self.assertEqual(2,report['deferred'])
        self.assertNotIn('private auth response',json.dumps(report))
        report=self.worker(paths).run(data);self.assertEqual(3,report['completed'])

    def test_metadata_workflow_has_no_asset_or_pages_operations(self):
        workflow=(Path(__file__).resolve().parents[1]/'.github/workflows/update.yml').read_text(encoding='utf-8-sig')
        for command in ['src/main.py','build_gallery.py','deduplicate_images.py','publish_pages.py','git add -A','schedule:','pages: write']:
            self.assertNotIn(command,workflow)
        self.assertIn('checkpoint_metadata.py',workflow)


class MetadataBoundaryTests(unittest.TestCase):
    def test_nested_secret_fields_and_temporary_transfer_urls_are_rejected(self):
        for tag in [{'password':'secret'}, {'evidence':[{'source_url':'https://minas.mihoyo.com/seafhttp/files/temp/'}]}]:
            data=catalog();next(iter(data['items'].values()))['tags']=[tag];data['revision']=cat.revision(data)
            with self.assertRaises(ValueError):cat.validate(data)
    def test_metadata_checkpoint_cannot_stage_originals_or_private_csv(self):
        import subprocess
        from tools.checkpoint_update import checkpoint
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);repo=root/'work';remote=root/'remote.git'
            subprocess.run(['git','init','--bare',str(remote)],capture_output=True,check=True)
            subprocess.run(['git','init','-b','main',str(repo)],capture_output=True,check=True)
            def g(*args):return subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,check=True).stdout
            g('config','user.name','fixture');g('config','user.email','fixture@example.com');g('remote','add','origin',str(remote))
            (repo/'README.md').write_text('baseline');g('add','.');g('commit','-m','baseline');g('push','origin','main')
            (repo/'metafiles').mkdir();(repo/'downloads').mkdir();(repo/'posts').mkdir()
            (repo/'metafiles/catalog.json').write_text(json.dumps(catalog()),encoding='utf8')
            (repo/'metafiles/private.csv').write_text('minas_pwd\nsecret\n')
            (repo/'downloads/a.jpg').write_bytes(b'not-staged')
            (repo/'posts/public.csv').write_text('post_url,title\nhttps://www.miyoushe.com/zzz/article/1,public\n')
            result=checkpoint(repo,True,'999-1',scopes=('posts','metafiles/catalog.json','metafiles/catalog-status.json'))
            tree=g('ls-tree','-r','--name-only','HEAD')
            self.assertEqual('main',result['mode']);self.assertIn('catalog.json',tree)
            self.assertNotIn('private.csv',tree);self.assertNotIn('a.jpg',tree)
    def test_nas_container_source_subset_imports_without_repository_config(self):
        import shutil,subprocess,sys
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)
            for name in ['src/downloader','src/metadata','src/nas']:
                shutil.copytree(root/name,target/name,ignore=shutil.ignore_patterns('__pycache__'))
            (target/'tools').mkdir()
            for name in ['build_gallery.py','deduplicate_images.py','gallery_tags.py','gallery_tag_rules.py']:
                shutil.copyfile(root/'tools'/name,target/'tools'/name)
            result=subprocess.run([sys.executable,'-m','src.nas.sync','--help'],cwd=target,capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stderr);self.assertIn('--data',result.stdout)
    def test_metadata_session_rejects_media_routes_and_allows_listing_only(self):
        import requests
        from src.metadata.access import MetadataSession
        session=MetadataSession()
        with patch('requests.Session.send',return_value=Mock()) as network:
            for url in [SHARE+'files/?p=/a.jpg&dl=1','https://minas.mihoyo.com/seafhttp/zip/fixture/']:
                request=session.prepare_request(requests.Request('GET',url))
                with self.assertRaises(ValueError):session.send(request)
            network.assert_not_called()
            for url in [SHARE,'https://minas.mihoyo.com/api/v2.1/share-links/fixtureA/dirents/?path=/']:
                session.send(session.prepare_request(requests.Request('GET',url)))
            self.assertEqual(2,network.call_count)

    def test_minassession_rejects_cross_origin_before_network(self):
        import requests
        from src.metadata.access import MinasSession
        session=MinasSession();request=session.prepare_request(requests.Request('GET','https://unrelated.example/private'))
        with self.assertRaises(ValueError):session.send(request)

class MetadataPrivacyTests(unittest.TestCase):
    def test_private_refresh_csv_is_temporary_and_codes_not_logged(self):
        import contextlib,csv,io
        from tools import collect_metadata as cli
        with tempfile.TemporaryDirectory() as temp:
            prior=Path.cwd();os.chdir(temp)
            try:
                Path('src/config').mkdir(parents=True);Path('metafiles').mkdir();Path('posts').mkdir()
                Path('src/config/task.json').write_text(json.dumps([{'name':'posts','type':'post'},{'name':'shares','type':'minas'}]))
                fields=['post_time','post_name','post_url','minas_link','minas_pwd']
                with Path('metafiles/shares.csv').open('w',encoding='utf8',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerow(row())
                original=Path('metafiles/shares.csv').read_bytes()
                with Path('posts/posts.csv').open('w',encoding='utf8',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=fields[:3]);writer.writeheader();writer.writerow({k:row()[k] for k in fields[:3]})
                scraper=Mock();scraper.out_csv=Path('metafiles/shares.csv')
                scraper.run.side_effect=lambda:print(CODE)
                output=io.StringIO()
                with patch.object(cli,'PostScraper'),patch.object(cli,'MinasScraper',return_value=scraper),contextlib.redirect_stdout(output):
                    rows=cli.refresh_rows()
                self.assertEqual(CODE,rows[0]['minas_pwd']);self.assertNotIn(CODE,output.getvalue())
                self.assertEqual(original,Path('metafiles/shares.csv').read_bytes())
                self.assertFalse(scraper.out_csv.exists())
                self.assertEqual(['shares.csv'],[p.name for p in Path('metafiles').iterdir()])
            finally:os.chdir(prior)
    def test_observed_listing_shape_can_describe_large_video_without_transfer(self):
        body={'dirent_list':[{'is_dir':False,'file_path':'/video.mp4','file_name':'video.mp4',
                             'size':600*1024**2,'last_modified':STAMP}]}
        session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock()
        session.get.return_value=Response(json.dumps(body).encode())
        data,report=cat.collect([row()],session_factory=lambda:session,authenticate=Mock())
        self.assertEqual(1,report['media_items']);self.assertEqual('video',next(iter(data['items'].values()))['media_type'])
        self.assertEqual(1,session.get.call_count)
        self.assertIn('/dirents/',session.get.call_args.args[0])




class SourceRevisionTests(unittest.TestCase):
    def test_changed_access_code_is_refreshed_only_for_the_matching_source(self):
        auth=Mock(side_effect=[RuntimeError('old private code refused'),None])
        resolver=Mock(return_value='new-private-code')
        listing=Mock(return_value={'/a.jpg':{'size':1,'last_modified':STAMP}})
        data,report=cat.collect([row()],session_factory=lambda:Session({}),authenticate=auth,
                                listing=listing,code_resolver=resolver)
        self.assertEqual([],report['failures']);self.assertEqual(2,auth.call_count)
        self.assertEqual(SHARE,resolver.call_args.args[0]['share'])
        self.assertNotIn('new-private-code',json.dumps(data));self.assertNotIn(CODE,json.dumps(data))




class PublicRepositoryPrivacyTests(unittest.TestCase):
    def test_public_environment_contains_only_runtime_placeholders(self):
        root=Path(__file__).resolve().parents[1]
        env=(root/'deploy/synology/.env.example').read_text(encoding='utf8')
        values=dict(line.split('=',1) for line in env.splitlines() if '=' in line and not line.startswith('#'))
        for key in ['NAS_UID','NAS_GID','NAS_DATA_DIR','NAS_CONFIG_DIR']:self.assertEqual('',values[key])
        import re
        self.assertNotRegex((root/'docs/NAS_SETUP.md').read_text(encoding='utf8'),r'/volume\d+/')
    def test_current_public_csv_and_cli_do_not_contain_share_codes(self):
        import ast,csv
        root=Path(__file__).resolve().parents[1]
        for path in (root/'metafiles').glob('*.csv'):
            with path.open(encoding='utf-8-sig',newline='') as stream:
                for row in csv.DictReader(stream):self.assertFalse(row.get('minas_pwd'))
        tree=ast.parse((root/'src/downloader/MinasDownloader.py').read_text(encoding='utf8'))
        for node in ast.walk(tree):
            if not isinstance(node,ast.Assign) or not any(isinstance(t,ast.Name) and ('PWD' in t.id.upper() or 'PASSWORD' in t.id.upper()) for t in node.targets):continue
            if isinstance(node.value,ast.Call) and len(node.value.args)>1 and isinstance(node.value.args[1],ast.Constant):
                self.assertFalse(node.value.args[1].value)

if __name__=='__main__':unittest.main()
