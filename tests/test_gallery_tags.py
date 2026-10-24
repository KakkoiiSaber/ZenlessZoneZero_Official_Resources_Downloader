import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from PIL import Image
from tools import build_gallery as gallery
from tools.gallery_tags import broad_categories,build_tags
from src.utils.post_evidence import capture_post_evidence
from src.scraper.MinasScraper import MinasScraper


def entry(path,posts=()):
    return {"associations":[{"original_path":path,"path":"downloads/canonical/a.jpg","folder":"fixture","name":"a"}],"posts":list(posts),"versions":["未分类"]}


class TagTests(unittest.TestCase):
    def test_publication_workflow_has_no_recurring_or_push_trigger(self):
        workflow=(Path(__file__).resolve().parents[1]/".github/workflows/update.yml").read_text(encoding="utf8")
        import re
        self.assertNotRegex(workflow,r"(?m)^\s+schedule:")
        self.assertNotRegex(workflow,r"(?m)^\s+cron:")
        self.assertNotRegex(workflow,r"(?m)^  push:")
        self.assertIn("  workflow_dispatch:",workflow)

    def test_multiple_character_source_and_inferred_version_tags(self):
        image=entry("downloads/source/2025.06.05 壁纸分享 | 薇薇安角色PV&角色展示壁纸/a.png")
        self.assertEqual(["角色图片"],broad_categories(image))
        tags=build_tags(image);by_kind={tag["kind"]:tag for tag in tags}
        self.assertEqual("薇薇安",by_kind["character"]["value"])
        self.assertIn("PV",{tag["value"] for tag in tags if tag["kind"]=="source_type"})
        inferred=by_kind["associated_version"]
        self.assertEqual("1.7",inferred["value"]);self.assertEqual("inferred",inferred["confidence"])
        self.assertIn("推断",inferred["label"])
        self.assertEqual("https://zzz.mihoyo.com/news/155465",inferred["evidence"][0]["source_url"])
        self.assertNotIn("version",by_kind)

    def test_unverified_character_mapping_is_not_invented(self):
        image=entry("downloads/source/浮波柚叶主题壁纸/a.jpg")
        tags=build_tags(image)
        self.assertIn("浮波柚叶",[tag["value"] for tag in tags])
        self.assertFalse(any(tag["kind"]=="associated_version" for tag in tags))

    def test_cumulative_parent_does_not_label_each_asset(self):
        image=entry("downloads/source/v9.9 薇薇安 影像档案记录分享/《绝区零》2.3版本-可曾记得梦/a.jpg",
                    [{"title":"v9.9 薇薇安 影像档案记录分享","remote_path":"/《绝区零》2.3版本-可曾记得梦/a.jpg"}])
        image["versions"]=gallery.image_versions(image)
        self.assertEqual(["影像档案"],broad_categories(image))
        tags=build_tags(image)
        self.assertEqual(["2.3"],[tag["value"] for tag in tags if tag["kind"]=="version"])
        self.assertFalse(any(tag["kind"] in ("character","associated_version") for tag in tags))
        self.assertIn("可曾记得梦",[tag["value"] for tag in tags])

    def test_explicit_and_inferred_versions_remain_distinct_no_rerun_mapping(self):
        image=entry("downloads/source/影像档案记录分享/2.3版本/薇薇安.jpg")
        image["versions"]=["2.3"]
        tags=build_tags(image)
        self.assertEqual([("version","2.3","explicit"),("associated_version","1.7","inferred")],
                         [(t["kind"],t["value"],t["confidence"]) for t in tags if "version" in t["kind"]])

    def test_calendar_own_month_not_parent_post_or_date(self):
        image=entry("downloads/source/2024.05.01 《绝区零》2024年5月月历壁纸/6月月历壁纸-PC版.jpg",
                    [{"title":"《绝区零》2024年5月月历壁纸"}])
        self.assertEqual(["月历"],broad_categories(image))
        values=[t["value"] for t in build_tags(image)]
        self.assertIn("2024-06",values);self.assertNotIn("2024-05",values)
        self.assertFalse(any("version" in tag["kind"] for tag in build_tags(image)))

    def test_image_subfolder_source_type_beats_mixed_post_title(self):
        image=entry("downloads/source/薇薇安PV&角色展示壁纸/角色展示/1.jpg",
                    [{"title":"薇薇安PV&角色展示壁纸"}])
        sources=[tag for tag in build_tags(image) if tag["kind"]=="source_type"]
        self.assertEqual(["角色展示"],[tag["value"] for tag in sources])
        self.assertEqual("explicit",sources[0]["confidence"])

    def test_mixed_bundle_source_types_are_labeled_as_post_context(self):
        image=entry("downloads/source/薇薇安PV&角色展示壁纸/3840x2160/1.jpg")
        sources=[tag for tag in build_tags(image) if tag["kind"]=="source_type"]
        self.assertEqual({"PV","角色展示"},{tag["value"] for tag in sources})
        self.assertTrue(all(tag["confidence"]=="contextual" and "来源帖" in tag["label"] for tag in sources))

    def test_non_version_categories_and_unknown_fallback(self):
        for text,category in [("降噪测试/阵营/a.jpg","测试期素材"),("美梦如约而至活动壁纸/a.jpg","活动壁纸"),
                              ("三明治香气的梦想表情包/a.jpg","表情包"),("2025.01.17 unknown/a.jpg","未分类")]:
            with self.subTest(text=text):self.assertEqual([category],broad_categories(entry("downloads/source/"+text)))
        self.assertEqual([],build_tags(entry("downloads/source/2025.01.17 unknown/a.jpg")))


class EvidenceAndAggregationTests(unittest.TestCase):
    def setUp(self):
        self.old=Path.cwd();self.temp=tempfile.TemporaryDirectory();os.chdir(self.temp.name)
    def tearDown(self):os.chdir(self.old);self.temp.cleanup()

    def test_dedup_aggregates_tags_and_post_associations_idempotently(self):
        paths=[Path("downloads/source/2025.01.01 薇薇安PV壁纸/a.png"),Path("downloads/source/2025.01.02 雨果EP壁纸/a.png")]
        for path in paths:path.parent.mkdir(parents=True);Image.new("RGB",(60,40),"red").save(path)
        Path("metafiles").mkdir()
        with Path("metafiles/fixture.csv").open("w",encoding="utf8",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=["post_name","post_time","post_url","minas_pwd"]);writer.writeheader()
            writer.writerows([{"post_name":"薇薇安PV壁纸","post_time":"2025-01-01","post_url":"https://example.com/one","minas_pwd":"fixture-code"},
                             {"post_name":"雨果EP壁纸","post_time":"2025-01-02","post_url":"https://example.com/two","minas_pwd":"fixture-code"}])
        gallery.main();before=Path("docs/meta.json").read_bytes()
        item=json.loads(before)["items"][0]
        self.assertEqual(1,len(json.loads(before)["items"]));self.assertEqual(2,len(item["associations"]));self.assertEqual(2,len(item["posts"]))
        self.assertEqual({"薇薇安","雨果"},{tag["value"] for tag in item["tags"] if tag["kind"]=="character"})
        inferred=next(tag for tag in item["tags"] if tag["kind"]=="associated_version")
        self.assertEqual(2,len(inferred["evidence"]))
        self.assertNotIn(b"fixture-code",before)
        gallery.main();self.assertEqual(before,Path("docs/meta.json").read_bytes())

    def test_future_body_evidence_bounded_redacted_and_not_auto_promoted(self):
        capture_post_evidence("https://example.com/post","帖子",'<p>1.6版本内容</p><p>2.3版本提取码 fixture-code</p>',
                              json.dumps([{"insert":"v1.7 特别说明"}]),topics=[{"name":"壁纸"},{"name":"密码 fixture-code"}],protected_values=("fixture-code",))
        raw=Path("metafiles/post-evidence.json").read_text(encoding="utf8")
        self.assertNotIn("fixture-code",raw);data=json.loads(raw)["posts"]["https://example.com/post"]
        self.assertEqual({"1.6","1.7"},{mention["version"] for mention in data["version_mentions"]})
        self.assertTrue(all(mention["confidence"]=="unreviewed" for mention in data["version_mentions"]))
        image=entry("downloads/source/unknown/a.jpg",[{"title":"帖子",**data}])
        self.assertFalse(any("version" in tag["kind"] for tag in build_tags(image)))

    def test_existing_fetch_captures_evidence_without_extra_requests_or_breakage(self):
        scraper=MinasScraper.__new__(MinasScraper);scraper.config={};scraper.log=Mock()
        scraper.fetch=Mock(return_value=Mock(json=lambda:{"data":{"post":{"post":{"subject":"帖子","content":"<p>1.6版本说明</p>","structured_content":"[]"},"topics":[{"name":"壁纸"}]}}}))
        scraper._extract_from_text=Mock(return_value=("https://minas.mihoyo.com/d/fixture/","fixture-code"))
        result=scraper._extract_via_miyoushe_api("123")
        self.assertEqual("fixture-code",result[1]);scraper.fetch.assert_called_once()
        self.assertTrue(Path("metafiles/post-evidence.json").exists())
        Path("metafiles/post-evidence.json").write_text("broken")
        self.assertEqual(result,scraper._extract_via_miyoushe_api("123"))
        self.assertEqual("broken",Path("metafiles/post-evidence.json").read_text());scraper.log.warning.assert_called_once()


if __name__=="__main__":unittest.main()
