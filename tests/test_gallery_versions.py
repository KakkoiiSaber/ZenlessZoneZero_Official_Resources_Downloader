import unittest
from tools import build_gallery as gallery


class VersionTests(unittest.TestCase):
    def test_explicit_versions_numeric_order_and_dates(self):
        self.assertEqual(["3.0", "2.10", "2.9"], gallery.parse_versions("2.9版本 / 2.10版本 / v3.0"))
        self.assertEqual([], gallery.parse_versions("2025.11.12 2025年12月月历壁纸"))
        self.assertEqual(["2.3"], gallery.parse_versions("《绝区零》2.3版本-可曾记得梦"))

    def test_cumulative_subfolder_beats_enclosing_post_version(self):
        entry={"associations":[{"path":"downloads/source/2025.01.01 v9.9 影像档案记录分享/《绝区零》2.3版本-可曾记得梦/1.jpg"}],
               "posts":[{"title":"v9.9 影像档案记录分享","remote_path":"/《绝区零》2.3版本-可曾记得梦/1.jpg"}]}
        self.assertEqual(["2.3"],gallery.image_versions(entry))

    def test_multiple_versions_preserved_without_date_inference(self):
        entry={"associations":[{"path":"downloads/canonical/asset.jpg"}],
               "posts":[{"title":"影像档案记录分享","remote_path":"/2.9版本/a.jpg"},
                        {"title":"影像档案记录分享","remote_path":"/2.10版本/a.jpg"}]}
        self.assertEqual(["2.10","2.9"],gallery.image_versions(entry))
        self.assertEqual(["未分类"],gallery.image_versions({"associations":[{"folder":"source/2025.11.12 未知","name":"1"}]}))

    def test_remapped_original_paths_retain_multi_version_membership(self):
        entry={"associations":[{"path":"downloads/content/a.jpg","original_path":"downloads/post/2.9版本/a.jpg"},
                               {"path":"downloads/content/a.jpg","original_path":"downloads/post/2.10版本/a.jpg"}]}
        self.assertEqual(["2.10","2.9"],gallery.image_versions(entry))

    def test_post_metadata_fallback_and_cumulative_unknown(self):
        self.assertEqual(["2.4"],gallery.image_versions({"posts":[{"title":"《绝区零》2.4版本主题壁纸"}]}))
        self.assertEqual(["未分类"],gallery.image_versions({"posts":[{"title":"v9.9 影像档案记录分享","remote_path":"/1.jpg"}]}))


if __name__ == "__main__":unittest.main()
