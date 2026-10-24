import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import deduplicate_images as dedup
from tools import build_gallery as gallery


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.old=Path.cwd();self.temp=tempfile.TemporaryDirectory();os.chdir(self.temp.name)
        self.env=patch.dict(os.environ,{"MINAS_IMAGE_ALIASES_FILE":"metafiles/image-aliases.json"});self.env.start()
    def tearDown(self):
        self.env.stop();os.chdir(self.old);self.temp.cleanup()
    def entry(self,body,pointer=False):
        return {"sha256":hashlib.sha256(body).hexdigest(),"size":len(body),"pointer":pointer}

    def test_only_exact_images_removed_resolutions_and_video_preserved(self):
        same=self.entry(b"same",True)
        entries={"downloads/a.jpg":same,"downloads/b.jpg":same,
                 "downloads/high-resolution.jpg":self.entry(b"different",True),
                 "downloads/video.mp4":same,"docs/thumbs/a.jpg":self.entry(b"preview"),
                 "docs/thumbs/b.jpg":self.entry(b"preview")}
        aliases,deletions,stats=dedup.plan(entries)
        self.assertEqual(["docs/thumbs/b.jpg","downloads/b.jpg"],deletions)
        self.assertEqual(2,stats["originals"]["after_count"])
        self.assertEqual("downloads/a.jpg",aliases["originals"]["downloads/b.jpg"])
        self.assertNotIn("downloads/video.mp4",deletions)
        remaining={path:item for path,item in entries.items() if path not in deletions}
        second,new_deletions,unused=dedup.plan(remaining,aliases)
        self.assertEqual([],new_deletions)
        self.assertEqual(aliases,second)

    def test_pointer_validation_and_inconsistent_declared_sizes(self):
        sha=hashlib.sha256(b"same").hexdigest()
        pointer=f"version https://git-lfs.github.com/spec/v1\noid sha256:{sha}\nsize 4\n".encode()
        self.assertEqual((sha,4,True),dedup.identity(pointer))
        with self.assertRaises(ValueError):dedup.identity(pointer.replace(b"size 4",b"size nope"))
        entries={"downloads/a.jpg":self.entry(b"same",True),"downloads/b.jpg":{**self.entry(b"same",True),"size":99}}
        with self.assertRaises(ValueError):dedup.plan(entries)

    def test_apply_remaps_state_preserves_history_and_never_deletes_unique(self):
        for name,body in [("downloads/a.jpg",b"same"),("downloads/b.jpg",b"same"),
                          ("downloads/movie.mp4",b"video"),("docs/thumbs/a.jpg",b"preview"),
                          ("docs/thumbs/b.jpg",b"preview")]:
            path=Path(name);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
        entries={"downloads/a.jpg":self.entry(b"same"),"downloads/b.jpg":self.entry(b"same"),
                 "docs/thumbs/a.jpg":self.entry(b"preview"),"docs/thumbs/b.jpg":self.entry(b"preview")}
        aliases,deletions,unused=dedup.plan(entries)
        Path("metafiles").mkdir()
        state={"assets":{self.entry(b"same")["sha256"]:{"paths":["downloads/a.jpg","downloads/b.jpg"],"sources":[{"share":"fixture","path":"/b.jpg"}]}}}
        Path("metafiles/minas-files.json").write_text(json.dumps(state))
        dedup.apply_plan(aliases,deletions)
        self.assertEqual(b"same",Path("downloads/a.jpg").read_bytes())
        self.assertFalse(Path("downloads/b.jpg").exists())
        self.assertEqual(b"video",Path("downloads/movie.mp4").read_bytes())
        asset=next(iter(json.loads(Path("metafiles/minas-files.json").read_text(encoding="utf8"))["assets"].values()))
        self.assertEqual(["downloads/a.jpg"],asset["paths"])
        self.assertEqual(2,len(asset["historical_paths"]))
        self.assertEqual(state["assets"][self.entry(b"same")["sha256"]]["sources"],asset["sources"])

    def test_gallery_alias_remap_and_legacy_indices(self):
        sha=hashlib.sha256(b"image").hexdigest()
        aliases={"version":1,"originals":{"downloads/b/b.jpg":"downloads/a/a.jpg"},
                 "previews":{"thumbs/b/b.jpg":"thumbs/a/a.jpg"},"identities":{"downloads/a/a.jpg":sha}}
        # Preview alias keys use docs-relative paths in the gallery, full paths in deletion plans.
        aliases["previews"]={"docs/thumbs/b/b.jpg":"docs/thumbs/a/a.jpg"}
        dedup.write_json(dedup.alias_file(),aliases)
        Path("docs").mkdir()
        old=[{"thumb":"thumbs/a/a.jpg","full":"thumbs/a/a.jpg","name":"a","folder":"a","mtime":0},
             {"thumb":"thumbs/b/b.jpg","full":"thumbs/b/b.jpg","name":"b","folder":"b","mtime":0}]
        Path("docs/meta.json").write_text(json.dumps({"items":old}))
        Path("docs/cached.json").write_text(json.dumps({"downloads/a/a.jpg":{"thumb":"thumbs/a/a.jpg","mtime":0},
             "downloads/b/b.jpg":{"thumb":"thumbs/b/b.jpg","mtime":0}}))
        gallery.main()
        data=json.loads(Path("docs/meta.json").read_text(encoding="utf8"))
        self.assertEqual(1,len(data["items"]))
        self.assertEqual(2,len(data["items"][0]["associations"]))
        self.assertEqual({"0":"sha256:"+sha,"1":"sha256:"+sha},data["legacy_indices"])
        self.assertEqual("thumbs/a/a.jpg",data["items"][0]["thumb"])
        self.assertTrue(all(alias["path"]=="downloads/a/a.jpg" for alias in data["items"][0]["associations"]))
        self.assertEqual(["downloads/a/a.jpg"],list(gallery.load_cache()))
        before=Path("docs/meta.json").read_bytes();gallery.main()
        self.assertEqual(before,Path("docs/meta.json").read_bytes())


if __name__ == "__main__":unittest.main()
