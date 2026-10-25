from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import requests

from src.metadata import wiki,resources,knowledge,pipeline,matching
from src.metadata.access import PublicPostUnavailable,public_access_code,stable_id
from src.nas.sync import Sync

FIXTURE=json.loads((Path(__file__).parent/'fixtures/wiki-observed.json').read_text(encoding='utf8'))


def empty_legacy():
    from src.metadata.catalog import revision
    data={'version':1,'sources':{},'items':{}};data['revision']=revision(data);return data


class FakeClient:
    def __init__(self,broken=()):self.broken=set(broken);self.calls=[]
    def tree(self):return deepcopy(FIXTURE['tree']['data'])
    def channel(self,ident):return deepcopy(FIXTURE['media_index' if ident==13 else 'roster']['data'])
    def detail(self,ident):
        self.calls.append(ident)
        if ident in self.broken:raise wiki.WikiError('wiki_fixture_failure')
        page=deepcopy(FIXTURE['affection']['data']['page']);page['id']=str(ident)
        return page


class WikiTests(unittest.TestCase):
    def test_observed_taxonomy_roster_and_direct_video_shape(self):
        entries,channels=wiki.index_entries(FIXTURE['media_index']['data'])
        self.assertEqual({73,72,98,99},{int(cid) for cid in channels})
        self.assertEqual(8,len(entries))
        media=wiki.extract_media(FIXTURE['affection']['data']['page'])
        self.assertEqual(1,len(media['media']));self.assertEqual('video',media['media'][0]['media_type'])
        self.assertEqual('published_source',media['media'][0]['quality']['status'])
        self.assertFalse(media['media'][0]['quality']['original_verified'])

    def test_real_link_entries_and_wallpaper_image_list(self):
        for key in ['character_video','game_video']:
            parsed=wiki.extract_media(FIXTURE[key]['data']['page'])
            self.assertEqual([],parsed['media']);self.assertEqual(1,len(parsed['links']))
            self.assertIn('miyoushe.com/zzz/article/',parsed['links'][0])
        parsed=wiki.extract_media(FIXTURE['wallpaper']['data']['page'])
        self.assertEqual(75,len(parsed['media']))
        self.assertTrue(any('箱庭之内' in s['section'] for i in parsed['media'] for s in i['sections']))

    def test_bad_api_bodies_and_media_route_fail_closed(self):
        client=wiki.WikiClient(session=Mock())
        for body in [None,[],{'retcode':0,'data':None},{'retcode':1034,'data':None}]:
            with self.subTest(body=body),patch('src.metadata.wiki.bounded_json',return_value=body):
                with self.assertRaises(wiki.WikiError):client.tree()
        with self.assertRaises(wiki.WikiError):client.get('https://act-upload.mihoyo.com/fixture.png')

    def test_images_keep_per_section_context_and_untrusted_urls_excluded(self):
        page=deepcopy(FIXTURE['affection']['data']['page'])
        page['modules'][0]['components'][0]['data']=json.dumps({'rich_text':
            '<h2>洛克茜角色展示</h2><img src="https://act-upload.mihoyo.com/fixture/a.png" alt="洛克茜">'
            '<h2>诺姆EP</h2><img src="https://act-upload.mihoyo.com/fixture/b.jpg">'
            '<img src="https://evil.example/private.png"><img src="https://act-upload.mihoyo.com/x.png?token=private">'
            '<iframe src="https://player.bilibili.com/player.html?bvid=BVFixture123"></iframe>'})
        result=wiki.extract_media(page)
        self.assertEqual(2,len(result['media']));self.assertIn('https://www.bilibili.com/video/BVFixture123',result['links'])
        self.assertEqual('洛克茜角色展示',result['media'][0]['sections'][0]['section'])
        self.assertEqual('诺姆EP',result['media'][1]['sections'][0]['section'])

    def test_media_url_blocks_credentials_ports_signed_queries_and_private_hosts(self):
        for value in ['http://act-upload.mihoyo.com/x.png','https://u:p@act-upload.mihoyo.com/x.png',
                      'https://act-upload.mihoyo.com:443/x.png','https://127.0.0.1/x.png',
                      'https://act-upload.mihoyo.com/x.png?Expires=1&Signature=x']:
            self.assertIsNone(wiki.media_url(value))
        self.assertIsNotNone(wiki.media_url('https://act-upload.mihoyo.com/x.png?x-oss-process=image/resize,w_800'))

    def test_metadata_revision_is_not_asset_release_version(self):
        parsed=wiki.extract_media(FIXTURE['affection']['data']['page'])
        self.assertTrue(parsed['entry_version'].isdigit())
        self.assertNotIn('versions',parsed)


class KnowledgeTests(unittest.TestCase):
    def test_sourced_roster_update_retains_aliases_forms_and_missing_old_entries(self):
        seed=knowledge.load_knowledge();result=knowledge.update_roster(FIXTURE['roster']['data'],seed)
        self.assertEqual(seed['revision'],result['revision'])
        names={char['name']:char for char in result['characters'].values()}
        self.assertIn('洛克茜',names);self.assertIn('星徽·比利',names);self.assertIn('比利',names)
        self.assertNotEqual(names['星徽·比利']['wiki_id'],names['比利']['wiki_id'])
        sparse=deepcopy(FIXTURE['roster']['data']);sparse['list'][0]['children'][0]['list']=sparse['list'][0]['children'][0]['list'][:1]
        self.assertGreaterEqual(len(knowledge.update_roster(sparse,result)['characters']),len(result['characters']))

    def test_new_character_can_be_added_without_python_rule_changes(self):
        raw=deepcopy(FIXTURE['roster']['data'])
        raw['list'][0]['children'][0]['list'].append({'content_id':999999,'title':'测试新角色·全名','alias_name':'测试新角色','ext':'{}'})
        updated=knowledge.update_roster(raw,knowledge.load_knowledge())
        self.assertEqual('测试新角色',updated['characters']['wiki:999999']['name'])
        self.assertNotIn('测试新角色',updated['debuts'])

    def test_version_titles_are_sourced_and_conflicts_not_silently_replaced(self):
        rows=[{'post_name':'3.4版本「测试标题」更新公告','post_url':'https://zzz.mihoyo.com/news/999999'},
              {'post_name':'3.4版本「冲突名称」更新公告','post_url':'https://zzz.mihoyo.com/news/999998'}]
        result=knowledge.update_versions(rows,knowledge.load_knowledge())
        self.assertEqual('测试标题',result['versions']['3.4']['name'])
        self.assertEqual('冲突名称',result['versions']['3.4']['conflicts'][0]['name'])

    def test_short_aliases_do_not_match_common_words(self):
        from tools.gallery_tags import characters
        self.assertEqual([],characters('简单说明这张照片的版本'))
        self.assertIn('照',characters('壁纸分享 | 照主题壁纸'))
        self.assertIn('简',characters('代理人简角色PV'))
        self.assertIn('星徽·比利',characters('/星徽·比利角色展示/1.png'))
        self.assertNotIn('比利',characters('/星徽·比利角色展示/1.png'))

    def test_matching_rejects_language_type_version_form_and_ambiguity(self):
        rows=[{'post_name':'洛克茜角色展示 | 神秘负棺人','post_url':'https://zzz.mihoyo.com/news/1'}]
        self.assertEqual('matched',matching.match_official('洛克茜角色展示丨神秘负棺人',rows)['status'])
        for title in ['洛克茜EP | 神秘负棺人','比利角色展示 | 神秘负棺人','3.2版本PV | 神秘负棺人']:
            self.assertEqual('unmatched',matching.match_official(title,rows)['status'])
        self.assertEqual('unmatched',matching.match_official('洛克茜角色展示 | 神秘负棺人',rows,'en-us')['status'])
        rows.append({'post_name':rows[0]['post_name'],'post_url':'https://zzz.mihoyo.com/news/2'})
        self.assertEqual('ambiguous',matching.match_official(rows[0]['post_name'],rows)['status'])


class PipelineTests(unittest.TestCase):
    def test_offline_reclassification_uses_cache_without_requests(self):
        first,state,k,legacy,_=pipeline.collect(empty_legacy(),client=FakeClient(),max_details=4,now=100)
        client=Mock()
        second,_,_,_,report=pipeline.collect(legacy,state,k,client=client,max_details=0,now=101,offline=True)
        self.assertEqual(first['revision'],second['revision']);self.assertEqual('offline_cache',report['mode'])
        client.tree.assert_not_called();client.channel.assert_not_called();client.detail.assert_not_called()

    def test_preference_only_groups_observed_same_object_transforms(self):
        from src.metadata.resources import combine
        def found(url):return {'url':url,'media_type':'image','sections':[{'section':'角色展示','caption':'','module_id':'1'}],
                              'quality':{'status':'published_source','original_verified':False,'transformed':'?' in url}}
        work={'wiki_id':'123','wiki_url':'https://baike.mihoyo.com/zzz/wiki/content/123/detail','title':'资源',
              'channels':[98],'categories':['角色图片'],'links':[],'official_match':None,
              'media':[found('https://act-upload.mihoyo.com/fixture/a.png'),found('https://act-upload.mihoyo.com/fixture/a.png?x-oss-process=image/resize,w_800'),found('https://act-upload.mihoyo.com/fixture/b.png')]}
        data=combine(empty_legacy(),{stable_id('wiki-work','123'):work})
        self.assertEqual(3,len(data['items']));self.assertEqual(2,len(data['variant_groups']))
        self.assertEqual(2,sum(i['preferred'] for i in data['items'].values()))
        group=next(g for g in data['variant_groups'].values() if len(g['item_ids'])==2)
        self.assertNotIn('?',data['items'][group['preferred_item_id']]['url'])
        self.assertFalse(group['original_quality_verified'])

    def test_removed_remote_media_retains_history_and_associations(self):
        first,state,k,legacy,_=pipeline.collect(empty_legacy(),client=FakeClient(),max_details=8,now=100)
        client=FakeClient()
        def removed(ident):
            page=deepcopy(FIXTURE['character_video']['data']['page']);page['id']=ident;return page
        client.detail=removed
        second,_,_,_,_=pipeline.collect(legacy,state,k,client=client,max_details=8,now=100+8*86400)
        self.assertEqual(set(first['items']),set(second['items']))
        self.assertFalse(next(iter(second['items'].values()))['available'])
        self.assertTrue(all(work['media_ids'] for work in second['works'].values()))

    def test_unchanged_rerun_no_detail_fetch_and_stable_catalog(self):
        first,state,k,legacy,report=pipeline.collect(empty_legacy(),client=FakeClient(),max_details=4,now=100)
        fake=FakeClient();second,state,_,_,again=pipeline.collect(legacy,state,k,client=fake,max_details=4,now=101)
        self.assertEqual(4,report['detail_completed'])
        # Remaining unresolved entries are visited first; after that, an unchanged
        # third pass should make no detail calls.
        third_client=FakeClient();third,*_=pipeline.collect(legacy,state,k,client=third_client,max_details=4,now=102)
        self.assertEqual([],third_client.calls);self.assertEqual(second['revision'],third['revision'])

    def test_partial_detail_failure_retains_previous_media(self):
        first,state,k,legacy,_=pipeline.collect(empty_legacy(),client=FakeClient(),max_details=8,now=100)
        bad=FakeClient(broken=state['entries'])
        second,_,_,_,report=pipeline.collect(legacy,state,k,client=bad,max_details=8,now=100+8*86400)
        self.assertEqual(set(first['items']),set(second['items']));self.assertEqual(8,len(report['failures']))

    def test_same_url_aggregates_origins_not_duplicate_items(self):
        result,_,_,_,_=pipeline.collect(empty_legacy(),client=FakeClient(),max_details=8,now=100)
        self.assertEqual(1,len(result['items']))
        item=next(iter(result['items'].values()));self.assertEqual(8,len(item['origins']))
        self.assertTrue(all(len(work['media_ids'])==1 for work in result['works'].values()))
        self.assertIsNone(item['size'])


class NullResponseTests(unittest.TestCase):
    def test_reported_null_data_1034_is_actionable_not_attribute_error(self):
        source={'share':'https://minas.mihoyo.com/d/fixtureA/','posts':[{'post_url':'https://www.miyoushe.com/zzz/article/1'}]}
        with patch('src.metadata.access.bounded_json',return_value={'retcode':1034,'data':None}):
            with self.assertRaises(PublicPostUnavailable) as context:public_access_code(source)
        self.assertEqual('public_post_api_error',context.exception.reason);self.assertEqual(1034,context.exception.api_code)

    def test_null_nested_post_is_rejected_without_exception_text_leaks(self):
        source={'share':'https://minas.mihoyo.com/d/fixtureA/','posts':[{'post_url':'https://www.miyoushe.com/zzz/article/1'}]}
        with patch('src.metadata.access.bounded_json',return_value={'retcode':0,'data':{'post':None}}):
            with self.assertRaises(PublicPostUnavailable) as context:public_access_code(source)
        self.assertEqual('public_post_invalid_payload',context.exception.reason)


class DirectNASDownloads(unittest.TestCase):
    def data(self):return pipeline.collect(empty_legacy(),client=FakeClient(),max_details=4,now=100)[0]
    def test_official_downloads_skip_share_code_lookup_and_are_idempotent(self):
        from test_nas_metadata import Response
        session=Mock();session.get.return_value=Response(b'fixture-video')
        with tempfile.TemporaryDirectory() as root:
            worker=Sync(root,session_factory=lambda:session,authenticate=Mock(),code_resolver=Mock(side_effect=AssertionError('must not resolve a code')),now=100)
            first=worker.run(self.data());self.assertEqual(1,first['completed']);worker.authenticate.assert_not_called()
            again=Sync(root,session_factory=lambda:session,now=101).run(self.data())
            self.assertEqual(1,again['unchanged']);self.assertEqual(1,session.get.call_count)

    def test_oversize_unknown_size_and_partial_file_are_never_committed(self):
        from test_nas_metadata import Response
        session=Mock();session.get.return_value=Response(b'large',headers={'Content-Length':'999999999'})
        with tempfile.TemporaryDirectory() as root:
            worker=Sync(root,session_factory=lambda:session);worker.max_bytes=50
            report=worker.run(self.data());self.assertEqual(0,report['completed']);self.assertEqual([],report['failures'])
            self.assertEqual(1,report['deferred']);self.assertEqual({},worker.state['objects'])
            self.assertFalse(list(Path(root).glob('.nas-*')))

    def test_official_host_guard_applies_before_send_and_to_redirect_targets(self):
        session=resources.OfficialMediaSession()
        request=requests.Request('GET','https://evil.example/file.mp4').prepare()
        with self.assertRaises(ValueError):session.send(request)

    def test_partial_direct_stream_retries_safely_and_logs_no_secret(self):
        from test_nas_metadata import Response
        session=Mock();session.get.return_value=Response(b'fixture-video',broken=True)
        with tempfile.TemporaryDirectory() as root,patch('sys.stderr',new_callable=io.StringIO) as output:
            worker=Sync(root,session_factory=lambda:session)
            first=worker.run(self.data());self.assertEqual(0,first['completed']);self.assertEqual({},worker.state['items'])
            self.assertFalse(list(Path(root).glob('.nas-*')));self.assertNotIn('private URL',output.getvalue())
            session.get.return_value=Response(b'fixture-video')
            second=Sync(root,session_factory=lambda:session).run(self.data());self.assertEqual(1,second['completed'])

    def test_time_budget_defers_without_network_or_failure(self):
        import time
        session=Mock()
        with tempfile.TemporaryDirectory() as root:
            worker=Sync(root,session_factory=lambda:session);worker.deadline=time.monotonic()-1
            report=worker.run(self.data());self.assertEqual(1,report['deferred']);self.assertEqual([],report['failures'])
            session.get.assert_not_called()


if __name__=='__main__':unittest.main()
