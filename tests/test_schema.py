from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from src.scrape import collect, parse
from src.scrape.http import atomic_json, now
from src.scrape.schema import pack_bundle, expand_bundle
from test_manifests import post, IMAGE, OFFICIAL_VIDEO, MI_VIDEO


def bundle():
    official = post()
    official['images'] = [{'url':IMAGE,'observed_url':IMAGE+'?x-oss-process=image/format,webp','caption':''}]
    official['videos'] = [{'id':'0','duration_ms':1000,'variants':[{'url':OFFICIAL_VIDEO}],
                           'selected':{'url':OFFICIAL_VIDEO}}]
    mi = post('2','【绝区零首曝PV】世界全剧终','miyoushe')
    mi['videos'] = [{'id':'v1','duration_ms':1000,'variants':[{'url':MI_VIDEO}], 'selected':{'url':MI_VIDEO}}]
    mi['minas_links'] = [{'url':'https://minas.mihoyo.com/d/abc/','password':'abcd'},
                         {'url':'https://minas.mihoyo.com/d/def/','password':None}]
    entry={'id':'1','url':'https://baike.mihoyo.com/zzz/wiki/content/1/detail',
           'title':'首曝PV | 世界全剧终','category_ids':[72], 'index_signature':'wiki-signature',
           'checked_at':now(),'status':'ok','parser_version':collect.PARSER_VERSION,
           'miyoushe_url':mi['url'],'detail_links':[mi['url']], 'wiki_resources':[
               {'id':'wiki:1:module:1:1','kind':'image','filename':'001.jpg','url':IMAGE,
                'observed_url':IMAGE+'?x-oss-process=image/format,webp','section_path':['一组'],'selected_source':'wiki'}]}
    entry=parse.resolve_parsed_wiki(entry,{'2':mi},{'1':official},parse.official_index({'1':official}))
    categories={'72':{'id':72,'name':'游戏视频','entry_ids':['1']}}
    tree=parse.directory_tree({'1':entry},categories)
    common={'schema_version':1,'snapshot_id':'same','generated_at':now(),'status':'complete'}
    return {'official':{**common,'posts':{'1':official}}, 'miyoushe':{**common,'posts':{'2':mi}},
            'wiki':{**common,'categories':categories,'entries':{'1':entry},'directory_tree':tree}}


def selected_files(value):
    return [(entry_id,r['id'],r['url'],r['filename'],r['directories'])
            for entry_id,entry in value['wiki']['entries'].items() for r in entry['resources']]


class MinimalSchemaTests(unittest.TestCase):
    def test_entry_cover_survives_round_trip_without_becoming_a_media_resource(self):
        original = bundle()
        original['wiki']['entries']['1']['cover_url'] = IMAGE
        compact = pack_bundle(original)
        self.assertEqual(compact['wiki']['entries']['1']['cover_url'], IMAGE)
        self.assertEqual(compact, pack_bundle(expand_bundle(compact)))
        self.assertEqual(selected_files(original), selected_files(expand_bundle(compact)))

    def test_wiki_tab_names_survive_incremental_round_trip(self):
        original = bundle()
        image = original['wiki']['entries']['1']['resources'][0]
        image['caption'] = '她与她的隐秘往事'
        compact = pack_bundle(original)
        self.assertEqual(compact['wiki']['entries']['1']['resources'][0]['caption'], '她与她的隐秘往事')
        self.assertEqual(compact, pack_bundle(expand_bundle(compact)))
        self.assertEqual(selected_files(original), selected_files(expand_bundle(compact)))
        image['caption'] = '1'
        self.assertNotIn('caption', pack_bundle(original)['wiki']['entries']['1']['resources'][0])

    def test_output_uses_one_image_url_one_video_version_and_one_folder_table(self):
        compact=pack_bundle(bundle())
        self.assertEqual([IMAGE],compact['official']['posts']['1']['images'])
        self.assertEqual(OFFICIAL_VIDEO,compact['official']['posts']['1']['videos'][0]['url'])
        forbidden={'variants','selected','observed_url','wiki_resources','directory_tree','entry_ids',
                   'http_metadata','video_metadata','evidence','candidates','body_source'}
        def check(value):
            if isinstance(value,dict):
                self.assertFalse(forbidden & set(value))
                for child in value.values():check(child)
            elif isinstance(value,list):
                for child in value:check(child)
        check(compact)
        entry=compact['wiki']['entries']['1']
        self.assertEqual([0,1],[r['folder'] for r in entry['resources']])
        self.assertNotIn('directories',entry['resources'][0])
        self.assertNotIn('id',compact['official']['posts']['1'])
        self.assertNotIn('url',compact['official']['posts']['1'])

    def test_repeated_round_trips_preserve_download_paths_and_are_idempotent(self):
        original=bundle();compact=pack_bundle(original)
        for _ in range(3):
            expanded=expand_bundle(compact)
            self.assertEqual(selected_files(original),selected_files(expanded))
            self.assertEqual(compact,pack_bundle(expanded))
            collect.validate_bundle(expanded)

    def test_scan_state_credentials_and_signed_link_expiry_survive_without_local_cache(self):
        original=bundle();chosen=original['miyoushe']['posts']['2']['videos'][0]['selected']
        chosen.update(url='https://prod-vod-sign.miyoushe.com/opaque?auth_key=2000000000-public-signature',
                      expires_at='2033-05-18T03:33:20+00:00',extension='.mp4')
        expanded=expand_bundle(pack_bundle(original))
        for source in ('official','miyoushe'):
            for ident,record in original[source]['posts'].items():
                restored=expanded[source]['posts'][ident]
                self.assertFalse(collect.needs_refresh(restored,record,7))
                for field in ('title','url','published_at','index_signature','checked_at','parser_version'):
                    self.assertEqual(record[field],restored[field])
        mi=expanded['miyoushe']['posts']['2']
        self.assertEqual(original['miyoushe']['posts']['2']['minas_links'],mi['minas_links'])
        self.assertEqual(chosen,mi['videos'][0]['selected'])

    def test_verified_profiles_and_duplicate_identity_survive_but_detailed_headers_do_not(self):
        original=bundle();video=original['official']['posts']['1']['videos'][0]
        facts={'status':200,'etag':'"'+'a'*32+'"','size_bytes':1000,'content_type':'video/mp4',
               'checked_at':now(),'last_modified':'unneeded'}
        profile={'source_etag':facts['etag'],'source_size_bytes':1000,'duration_ms':1000,
                 'width':2560,'height':1080,'codec':'avc1','average_bitrate':8000,'checked_at':now()}
        video['selected'].update(http_metadata=facts,video_metadata=profile)
        compact=pack_bundle(original);flat=compact['official']['posts']['1']['videos'][0]
        self.assertTrue(flat['profile_verified']);self.assertNotIn('last_modified',json.dumps(flat))
        expanded=expand_bundle(compact)['official']['posts']['1']
        self.assertEqual(parse.media_identity(original['official']['posts']['1']),parse.media_identity(expanded))
        self.assertEqual(2560,parse.verified_video_profile(expanded)['width'])

    def test_unselected_wiki_video_is_retained_once_as_a_future_fallback(self):
        original=bundle();entry=original['wiki']['entries']['1']
        raw={'id':'wiki:1:module:1:2','url':'https://act-upload.mihoyo.com/direct.mp4','kind':'video',
             'filename':'002.mp4','section_path':[],'selected_source':'wiki'}
        entry['wiki_resources'].append(raw)
        compact=pack_bundle(original)
        self.assertEqual(1,len(compact['wiki']['entries']['1']['wiki_fallbacks']))
        restored=expand_bundle(compact)
        restored['official']['posts']['1']['videos']=[]
        restored['miyoushe']['posts']['2']['videos']=[]
        resolved=parse.resolve_parsed_wiki(restored['wiki']['entries']['1'],restored['miyoushe']['posts'],
            restored['official']['posts'],parse.official_index(restored['official']['posts']))
        self.assertEqual('wiki_direct',resolved['selection_status'])
        self.assertIn(raw['url'],[r['url'] for r in resolved['resources']])

    def test_shared_directories_are_not_repeated_and_order_is_preserved(self):
        original=bundle();entry=original['wiki']['entries']['1'];one=entry['resources'][0]
        entry['resources'].insert(1,{**deepcopy(one),'id':'wiki:1:module:1:2','filename':'002.jpg',
                                   'url':'https://act-upload.mihoyo.com/second.jpg'})
        entry['wiki_resources'].append(deepcopy(entry['resources'][1]))
        compact=pack_bundle(original)['wiki']['entries']['1']
        self.assertEqual(2,len(compact['folders']))
        self.assertEqual([0,0,1],[r['folder'] for r in compact['resources']])

    def test_invalid_folder_reference_is_rejected(self):
        compact=pack_bundle(bundle());compact['wiki']['entries']['1']['resources'][0]['folder']=999
        with self.assertRaises(ValueError):expand_bundle(compact)

    def test_offline_migration_calls_no_network_and_keeps_records_readable_per_line(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'metafiles';output.mkdir()
            original=bundle()
            for source,document in original.items():atomic_json(output/(source+'.json'),document)
            with patch('src.scrape.http.Client.get',side_effect=AssertionError('network forbidden')):
                self.assertEqual(0,collect.compact_only(SimpleNamespace(output=str(output))))
                first={p.name:p.read_bytes() for p in output.iterdir()}
                self.assertEqual(0,collect.compact_only(SimpleNamespace(output=str(output))))
                self.assertEqual(first,{p.name:p.read_bytes() for p in output.iterdir()})
            self.assertEqual(selected_files(original),selected_files(collect.read_bundle(output)))
            text=(output/'official.json').read_text(encoding='utf8')
            self.assertGreater(text.count('\n'),3)
            self.assertTrue(any(line.startswith('    "1":') for line in text.splitlines()))


if __name__=='__main__':
    unittest.main()
