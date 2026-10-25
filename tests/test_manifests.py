from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.scrape import collect, parse
from src.scrape.http import Client, FetchError, atomic_json, now

FIXTURE = json.loads((Path(__file__).parent/'fixtures/wiki-observed.json').read_text(encoding='utf8'))
IMAGE = 'https://act-upload.mihoyo.com/fixture/image.jpg'
OFFICIAL_VIDEO = 'https://webstatic.mihoyo.com/fixture/high.mp4'
MI_VIDEO = 'https://vod-static.miyoushe.com/fixture/fallback.mp4'


def post(ident='1', title='《绝区零》首曝PV | 世界全剧终', provider='official'):
    host = 'https://zzz.mihoyo.com/news/' if provider == 'official' else 'https://www.miyoushe.com/zzz/article/'
    return {'id': ident, 'title': title, 'url': host+ident, 'published_at': '2026-10-25T12:00:00+08:00',
        'index_signature': 'unchanged', 'status': 'ok', 'checked_at': now(),
        'parser_version': collect.PARSER_VERSION, 'images': [], 'videos': [], 'minas_links': []}


class ParsingTests(unittest.TestCase):
    def test_complete_listing_resolves_image_ids_and_share_codes_without_detail_request(self):
        wrapper={'post':{'structured_content':json.dumps([
            {'insert':{'image':'123'}},
            {'insert':'https://minas.mihoyo.com/d/abc/\n提取码 abcd'}])},
            'image_list':[{'image_id':'123','url':IMAGE,'width':1920,'height':1080}]}
        parsed=parse.miyoushe_listing_article(wrapper)
        self.assertEqual(IMAGE,parsed['images'][0]['url'])
        self.assertEqual(1920,parsed['images'][0]['width'])
        self.assertEqual('abcd',parsed['minas_links'][0]['password'])
        wrapper['image_list']=[]
        self.assertIsNone(parse.miyoushe_listing_article(wrapper))

    def test_signed_video_url_keeps_authentication_and_has_a_refreshable_expiry(self):
        url='https://prod-vod-sign.miyoushe.com/opaque?auth_key=2000000000-random-0-signature'
        result=parse.variant({'url':url,'format':'MP4','height':1080})
        self.assertEqual(url,result['url'])
        self.assertEqual('.mp4',result['extension'])
        self.assertIn('expires_at',result)
        other=url.replace('2000000000-random-0-signature','2000000100-other-0-rotated')
        self.assertEqual(parse.stable_listing_value({'url':url}),parse.stable_listing_value({'url':other}))

    def test_official_independent_video_channel_resources_are_in_extensions(self):
        result=parse.official_article({'sContent':'','sExt':json.dumps({'video-url':[{'url':OFFICIAL_VIDEO}]})})
        self.assertEqual(OFFICIAL_VIDEO,result['videos'][0]['selected']['url'])

    def test_original_image_keeps_extension_and_other_query_parameters(self):
        self.assertEqual(IMAGE, parse.media_url(IMAGE+'?x-oss-process=image/format,webp', 'image'))
        self.assertEqual(IMAGE+'?v=2', parse.media_url(IMAGE+'?v=2&x-oss-process=image/resize,w_200', 'image'))
        self.assertEqual(OFFICIAL_VIDEO+'?x-oss-process=test', parse.media_url(OFFICIAL_VIDEO+'?x-oss-process=test', 'video'))

    def test_untrusted_media_hosts_and_disguised_urls_are_rejected(self):
        for url in ['https://evil.example/x.jpg', 'http://act-upload.mihoyo.com/x.jpg',
                    'https://user@act-upload.mihoyo.com/x.jpg', 'https://act-upload.mihoyo.com:443/x.jpg']:
            self.assertIsNone(parse.media_url(url))

    def test_share_codes_and_multiple_shares_stay_together(self):
        html = ('<p><a href="https://minas.mihoyo.com/d/aaa/">下载一</a> 提取码：abcd</p>'
                '<p><a href="https://minas.mihoyo.com/d/bbb/">下载二</a> 密码：efgh</p>')
        self.assertEqual([{'url':'https://minas.mihoyo.com/d/aaa/','password':'abcd'},
                          {'url':'https://minas.mihoyo.com/d/bbb/','password':'efgh'}], parse.shares(html))
        self.assertEqual([], parse.shares('<a href="https://zzz.mihoyo.com/news/1">新闻</a>'))

    def test_article_images_and_videos_are_parsed_without_fetching_media(self):
        parsed = parse.article('<img src="'+IMAGE+'?x-oss-process=image/format,webp">'
            '<video src="'+OFFICIAL_VIDEO+'" poster="'+OFFICIAL_VIDEO+'?x-oss-process=video/snapshot,t_0"></video>')
        self.assertEqual([IMAGE], [v['url'] for v in parsed['images']])
        self.assertEqual(OFFICIAL_VIDEO, parsed['videos'][0]['selected']['url'])
        self.assertEqual(1, len(parsed['videos']))

    def test_miyoushe_highest_variant_is_not_dependent_on_api_order(self):
        variants = [{'url':MI_VIDEO, 'height':1080, 'width':2560, 'bitrate':3131519, 'size':'64071273'},
                    {'url':'https://vod-static.miyoushe.com/fixture/low.mp4','height':480,'size':'36422290'}]
        parsed = parse.article('', wrapper={'vod_list':[{'id':'v1','resolutions':variants}]})
        self.assertEqual(64071273, parsed['videos'][0]['selected']['size_bytes'])
        self.assertEqual(MI_VIDEO, parsed['videos'][0]['selected']['url'])

    def test_title_normalization_preserves_work_type_character_and_version(self):
        self.assertEqual(parse.signature('《绝区零》首曝PV | 世界全剧终'),
                         parse.signature('【绝区零首曝PV】世界全剧终'))
        self.assertNotEqual(parse.signature('简角色PV'), parse.signature('照角色PV'))
        self.assertNotEqual(parse.signature('3.1版本PV'), parse.signature('3.2版本PV'))

    def test_wiki_parent_sections_and_template_picture_order_are_preserved(self):
        page = deepcopy(FIXTURE['wallpaper']['data']['page'])
        media, _ = parse.wiki_media(page)
        self.assertEqual(75, len(media))
        self.assertTrue(all('x-oss-process' not in r['url'] for r in media))
        self.assertTrue(any('「箱庭之内」壁纸' in r['section_path'] for r in media))
        # URL sorting must not dictate the original slide order.
        page = {'id':'1', 'modules':[{'id':'m1','components':[{'component_id':'map_desc','data':{'list':[
            {'image':'https://act-upload.mihoyo.com/z.jpg','tab_name':'1'},
            {'image':'https://act-upload.mihoyo.com/a.jpg','tab_name':'2'}]}}]}]}
        media, _ = parse.wiki_media(page)
        self.assertTrue(media[0]['url'].endswith('/z.jpg'))
        self.assertEqual(['001.jpg','002.jpg'], [m['filename'] for m in media])

    def test_windows_names_and_colliding_module_filenames(self):
        self.assertEqual('a_b', parse.safe_name('a|b'))
        self.assertEqual('_CON', parse.safe_name('CON'))
        entry={'id':'1','title':'合集','category_ids':[98], 'resources':[
            {'id':'a','filename':'001.jpg','section_path':['一组']},
            {'id':'b','filename':'001.jpg','section_path':['一组']}]}
        tree=parse.directory_tree({'1':entry}, {'98':{'name':'壁纸'}})
        self.assertNotEqual(entry['resources'][0]['filename'],entry['resources'][1]['filename'])
        self.assertEqual(2,len(tree[0]['children'][0]['children'][0]['resources']))


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.entry={'id':'907','title':'首曝PV | 世界全剧终','category_ids':[72],
                    'miyoushe_url':'https://www.miyoushe.com/zzz/article/2','wiki_resources':[], 'detail_links':[]}
        self.mi=post('2','【绝区零首曝PV】世界全剧终','miyoushe')
        self.mi['videos']=[{'id':'v1','selected':{'url':MI_VIDEO, 'height':1080}}]
        self.official=post()
        self.official['videos']=[{'id':'v1','selected':{'url':OFFICIAL_VIDEO}}]

    def resolve(self, official):
        return parse.resolve_parsed_wiki(self.entry, {'2':self.mi}, official, parse.official_index(official))

    def test_linked_miyoushe_title_matches_official_and_selects_official_file(self):
        result=self.resolve({'1':self.official})
        self.assertEqual('official',result['selection_status'])
        self.assertEqual(OFFICIAL_VIDEO,result['resources'][0]['url'])
        self.assertEqual(self.official['url'],result['resources'][0]['source_post'])

    def test_missing_official_media_uses_known_miyoushe_highest(self):
        self.official['videos']=[]
        result=self.resolve({'1':self.official})
        self.assertEqual('miyoushe_fallback',result['selection_status'])
        self.assertEqual(MI_VIDEO,result['resources'][0]['url'])

    def test_similar_titles_are_review_candidates_never_automatic_matches(self):
        self.official['title']='《绝区零》首曝PV | 世界全剧终特别剪辑'
        result=self.resolve({'1':self.official})
        self.assertEqual('unmatched',result['official_match']['status'])
        self.assertEqual('miyoushe_fallback',result['selection_status'])

    def test_duplicate_official_titles_do_not_silently_pick_first(self):
        other={**self.official,'id':'3','url':'https://zzz.mihoyo.com/news/3'}
        result=self.resolve({'1':self.official,'3':other})
        self.assertEqual('ambiguous',result['official_match']['status'])
        self.assertEqual(MI_VIDEO,result['resources'][0]['url'])

    def test_official_failure_is_visible_and_does_not_claim_absence(self):
        self.official['status']='error'
        result=self.resolve({'1':self.official})
        self.assertTrue(result['official_unavailable'])
        self.assertEqual('matched',result['official_match']['status'])

    def test_same_title_news_stub_and_video_channel_choose_the_unique_playable_source(self):
        stub={**self.official,'id':'3','url':'https://zzz.mihoyo.com/news/3','videos':[]}
        result=self.resolve({'1':self.official,'3':stub})
        self.assertEqual('official',result['selection_status'])
        self.assertEqual('normalized_title_and_unique_video',result['official_match']['method'])

    def metadata(self, size=1000, etag='"'+'a'*32+'"'):
        return {'status':200,'etag':etag,'size_bytes':size,'content_type':'video/mp4','checked_at':now()}

    def test_identical_http_validators_resolve_duplicate_uploads_to_news(self):
        self.official['source_kind']='news'
        other=deepcopy(self.official);other.update(id='3',source_kind='video',url='https://zzz.mihoyo.com/video')
        other['videos'][0]['selected']['url']='https://webstatic.mihoyo.com/another/path.mp4'
        for p in [self.official,other]:p['videos'][0]['selected']['http_metadata']=self.metadata()
        result=self.resolve({'1':self.official,'3':other})
        self.assertEqual('same_title_and_http_media_identity',result['official_match']['method'])
        self.assertEqual('1',result['official_match']['post_id'])
        self.assertEqual('official',result['selection_status'])

    def test_weak_etag_or_different_size_cannot_prove_duplicate_media(self):
        for metadata in [self.metadata(etag='W/"'+'a'*32+'"'),self.metadata(size=999)]:
            other=deepcopy(self.official);other.update(id='3',url='https://zzz.mihoyo.com/news/3')
            self.official['videos'][0]['selected']['http_metadata']=self.metadata()
            other['videos'][0]['selected']['http_metadata']=metadata
            self.assertEqual('ambiguous',self.resolve({'1':self.official,'3':other})['official_match']['status'])

    def test_distinct_encodes_require_matching_title_duration_and_verified_file_profiles(self):
        self.mi['videos'][0]['duration_ms']=149077
        other=deepcopy(self.official);other.update(id='3',url='https://zzz.mihoyo.com/video')
        for p,width,size,etag in [(self.official,2560,636802607,'a'),(other,1920,469006447,'b')]:
            value=p['videos'][0]['selected'];value['http_metadata']=self.metadata(size=size,etag='"'+etag*32+'"')
            value['video_metadata']={'source_etag':'"'+etag*32+'"','source_size_bytes':size,
                'duration_ms':149077,'width':width,'height':1080,'codec':'avc1','average_bitrate':size*8//149}
        result=self.resolve({'1':self.official,'3':other})
        self.assertEqual('same_title_duration_and_video_profile',result['official_match']['method'])
        self.assertEqual('1',result['official_match']['post_id'])
        other['videos'][0]['selected']['video_metadata']['duration_ms']=200000
        self.assertEqual('ambiguous',self.resolve({'1':self.official,'3':other})['official_match']['status'])

    def test_profile_is_invalidated_when_its_file_validator_changes(self):
        value=self.official['videos'][0]['selected'];value['http_metadata']=self.metadata()
        value['video_metadata']={'source_etag':'"'+'b'*32+'"','source_size_bytes':1000,
            'duration_ms':1000,'width':1920,'height':1080,'codec':'avc1','average_bitrate':8000}
        self.assertIsNone(parse.verified_video_profile(self.official))


class IncrementalTests(unittest.TestCase):
    def test_verified_duplicate_media_does_not_repeat_head_or_range_requests(self):
        from src.scrape.probe import review_duplicates
        one=post();one['videos']=[{'id':'v','selected':{'url':OFFICIAL_VIDEO,'http_metadata':{
            'status':200,'etag':'"'+'a'*32+'"','size_bytes':1000,'content_type':'video/mp4','checked_at':now()}}}]
        two=deepcopy(one);two['id']='2'
        head,profile=Mock(),Mock()
        result=review_duplicates({'1':one,'2':two},probe=head,profile_probe=profile)
        head.assert_not_called();profile.assert_not_called();self.assertEqual(0,result['requested'])

    def test_mp4_metadata_handles_movie_track_and_codec_boxes(self):
        import struct
        from src.scrape.probe import parse_mp4_metadata,parse_headers
        movie=bytearray(108);struct.pack_into('>I4s',movie,0,108,b'mvhd');struct.pack_into('>II',movie,20,1000,149077)
        track=bytearray(92);struct.pack_into('>I4s',track,0,92,b'tkhd');struct.pack_into('>II',track,84,2560*65536,1080*65536)
        codec=bytearray(86);struct.pack_into('>I4s',codec,0,86,b'avc1');struct.pack_into('>HH',codec,32,2560,1080)
        result=parse_mp4_metadata(bytes(movie+track+codec))
        self.assertEqual({'duration_ms':149077,'width':2560,'height':1080,'codec':'avc1'},result)
        headers=parse_headers(b'HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 206\r\nContent-Length: 10\r\nETag: "abc"\r\n\r\n')
        self.assertEqual(206,headers['status']);self.assertEqual(10,headers['size_bytes'])
    def test_expiring_signed_media_refreshes_even_when_post_is_unchanged(self):
        previous=post()
        previous['videos']=[{'selected':{'expires_at':(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()}}]
        self.assertTrue(collect.needs_refresh(previous,previous,7))
    def test_unchanged_record_reused_changed_expired_and_failed_retried(self):
        previous=post()
        self.assertFalse(collect.needs_refresh(previous,previous,7))
        self.assertTrue(collect.needs_refresh(previous,{**previous,'index_signature':'new'},7))
        self.assertTrue(collect.needs_refresh({**previous,'status':'error'},previous,7))
        stale={**previous,'checked_at':(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()}
        self.assertTrue(collect.needs_refresh(stale,previous,7))

    def test_successful_progress_survives_failure_and_resume_fetches_only_failed_item(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'progress.json'
            one,two=post(),post('2')
            fetch=Mock(side_effect=lambda entry: entry if entry['id']=='1' else (_ for _ in ()).throw(FetchError('api_1034')))
            records,failures,_=collect.update_records('test',{'1':one,'2':two},{},path,fetch,2,7)
            self.assertEqual(1,failures)
            self.assertEqual('error',records['2']['status'])
            retry=Mock(side_effect=lambda entry: {**entry,'status':'ok','checked_at':now(),'parser_version':collect.PARSER_VERSION})
            result,failures,_=collect.update_records('test',{'1':one,'2':two},{},path,retry,2,7)
            retry.assert_called_once()
            self.assertEqual(0,failures)
            self.assertEqual('ok',result['2']['status'])

    def test_incomplete_index_fails_instead_of_publishing_a_truncated_source(self):
        client=Mock()
        client.get.return_value={'iTotal':5,'list':[]}
        with self.assertRaises(FetchError):
            collect.discover_official(client)
        client.get.return_value={'list':[]}
        with self.assertRaises(FetchError):
            collect.discover_wiki(client)

    def test_response_cache_is_reused_on_resume_but_not_after_new_run(self):
        with tempfile.TemporaryDirectory() as temp:
            client=Client(temp,0,transport='requests',interval=0)
            path=Path(temp)/(collect.digest('https://example.com/api')+'.json')
            atomic_json(path,{'fixture':1})
            self.assertEqual({'fixture':1},client.get('https://example.com/api'))
            self.assertEqual(1,client.cache_hits)


class BundleTests(unittest.TestCase):
    def bundle(self):
        base={'snapshot_id':'one'}
        return {'official':{**base,'posts':{'1':post()}},
                'miyoushe':{**base,'posts':{'2':post('2',provider='miyoushe')}},
                'wiki':{**base,'categories':{'98':{'name':'壁纸'}},'entries':{
                    '1':{'id':'1','title':'一组','category_ids':[98],'resources':[
                        {'id':'r1','kind':'image','url':IMAGE,'filename':'001.jpg','directories':[['壁纸','一组']]}]}}}}

    def test_bundle_rejects_snapshot_mismatch_and_invalid_download_paths(self):
        bundle=self.bundle()
        collect.validate_bundle(bundle)
        bundle['wiki']['snapshot_id']='different'
        with self.assertRaises(ValueError):collect.validate_bundle(bundle)
        bundle=self.bundle()
        resource=bundle['wiki']['entries']['1']['resources'][0]
        bundle['wiki']['entries']['1']['resources'].append({**resource,'id':'r2'})
        with self.assertRaises(ValueError):collect.validate_bundle(bundle)

    def test_publication_replaces_legacy_metafiles_only_after_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'metafiles';output.mkdir()
            (output/'old.json').write_text('{}')
            bad=self.bundle();bad['wiki']['snapshot_id']='different'
            with self.assertRaises(ValueError):collect.publish(output,bad)
            self.assertTrue((output/'old.json').exists())
            collect.publish(output,self.bundle())
            self.assertEqual({'official.json','miyoushe.json','wiki.json'},{p.name for p in output.iterdir()})


class FullPipelineTests(unittest.TestCase):
    class API:
        instances=[]
        def __init__(self,*args):
            self.calls=[];self.requests=0;self.cache_hits=0
            self.instances.append(self)
        def get(self,url,params=None,**kwargs):
            self.calls.append((url,params));self.requests+=1
            if url.endswith('getContentList'):
                return {'iTotal':1,'list':[{'iInfoId':1,'sTitle':'《绝区零》首曝PV | 世界全剧终',
                    'dtStartTime':'2026-10-25 12:00:00'}]}
            if url.endswith('getContent'):
                return {'sContent':'<video src="'+OFFICIAL_VIDEO+'"></video>'}
            if url.endswith('getForumPostList'):
                return {'is_last':True,'list':[{'post':{'post_id':'2','subject':'【绝区零首曝PV】世界全剧终',
                    'created_at':1792900800,'content':'preview','updated_at':1}}]}
            if url.endswith('getPostFull'):
                return {'post':{'post':{'post_id':'2','subject':'【绝区零首曝PV】世界全剧终',
                    'content':'<p><a href="https://minas.mihoyo.com/d/abc/">下载</a> 提取码 abcd</p>'},
                    'vod_list':[{'id':'v1','resolutions':[{'url':MI_VIDEO,'height':1080}]}]}}
            if url.endswith('/home/content/list'):
                return {'list':[{'id':cid,'name':'分类'+str(cid),'list':[
                    {'content_id':cid,'title':'首曝PV | 世界全剧终' if cid==72 else '图片'+str(cid),
                     'bbs_url':'https://www.miyoushe.com/zzz/article/2' if cid==72 else ''}]}
                    for cid in [72,73,98,99]]}
            if url.endswith('entry_page'):
                return {'page':{'id':params['entry_page_id'],'modules':[{'id':'m1','components':[
                    {'component_id':'map_desc','data':{'list':[{'image':IMAGE,'tab_name':'1'}]}}]}]}}
            raise AssertionError('Unexpected request '+url)

    def test_first_run_then_incremental_skips_every_unchanged_detail(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(collect,'Client',self.API):
            args=SimpleNamespace(cache=str(Path(temp)/'cache'),output=str(Path(temp)/'metafiles'),
                restart=False,fresh=True,interval=0,transport='auto',workers=2,refresh_days=7,allow_partial=False)
            self.assertEqual(0,collect.run(args))
            wiki=json.loads((Path(args.output)/'wiki.json').read_text(encoding='utf8'))
            self.assertEqual(OFFICIAL_VIDEO,wiki['entries']['72']['resources'][-1]['url'])
            mi=json.loads((Path(args.output)/'miyoushe.json').read_text(encoding='utf8'))
            self.assertEqual('abcd',mi['posts']['2']['minas_links'][0]['password'])
            args.fresh=False
            self.assertEqual(0,collect.run(args))
            calls=self.API.instances[-1].calls
            self.assertEqual(4,len(calls))
            self.assertFalse(any(url.endswith(('getContent','getPostFull','entry_page')) for url,_ in calls))


if __name__ == '__main__':
    unittest.main()
