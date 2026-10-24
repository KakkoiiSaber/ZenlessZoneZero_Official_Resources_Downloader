"""File-level updates for mutable image-archive shares; credentials are never persisted."""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
from urllib.parse import urlencode, urlparse

import requests
try:
    from .asset_io import atomic_json, check_space, download_bounded, limit
except ImportError:
    from asset_io import atomic_json, check_space, download_bounded, limit

CATEGORY = "影像档案记录分享"
DEFAULT_STATE = "metafiles/minas-files.json"


def state_path():
    return Path(os.getenv("MINAS_FILE_STATE_FILE", DEFAULT_STATE))


def normalize_share(value):
    parsed = urlparse(str(value).strip().strip("\"'"))
    if (parsed.scheme != "https" or parsed.netloc != "minas.mihoyo.com" or
            not re.fullmatch(r"/d/[a-zA-Z0-9]+/?", parsed.path)):
        raise ValueError("Unsupported Minas share URL")
    return "https://minas.mihoyo.com" + parsed.path.rstrip("/") + "/"


def remote_path(value, parent, directory=False):
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value or "\x00" in value:
        raise ValueError("Unsafe remote path")
    parts = value.strip("/").split("/")
    if any(not part or part in (".", "..") or ":" in part for part in parts):
        raise ValueError("Unsafe remote path")
    path = "/" + "/".join(parts)
    if PurePosixPath(path).parent.as_posix().rstrip("/") != parent.rstrip("/"):
        raise ValueError("Listing entry is outside requested directory")
    return path + "/" if directory else path


def load_state():
    path = state_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "version": 1, "sources": {}, "assets": {}, "posts": {}}
    if data.get("version") != 1:
        raise ValueError("Unsupported file state version")
    for key in ("sources", "assets", "posts"):
        if not isinstance(data.get(key), dict):
            raise ValueError("Invalid file state")
    return data


class FileUpdater:
    def __init__(self, out_dir, authenticate, session_factory=requests.Session, now=None):
        self.out_dir = Path(out_dir)
        self.authenticate = authenticate
        self.session_factory = session_factory
        self.now = time.time() if now is None else now
        self.state = load_state()
        aliases = Path(os.getenv("MINAS_IMAGE_ALIASES_FILE", "metafiles/image-aliases.json"))
        self.canonical_images = {}
        if aliases.exists():
            for path, sha in json.loads(aliases.read_text(encoding="utf-8"))["identities"].items():
                self.canonical_images[sha] = path
        self.max_bytes = limit("MINAS_FILE_RUN_MAX_BYTES", 512 * 1024 ** 2)
        self.max_downloads = limit("MINAS_FILE_RUN_MAX_DOWNLOADS", 32)
        self.verify_quota = limit("MINAS_FILE_RUN_VERIFY_FILES", 4)
        self.refresh_seconds = limit("MINAS_FILE_VERIFY_DAYS", 7) * 86400
        self.used_bytes = 0
        self.downloads = 0
        self.verifications = 0
        self.pending = 0
        self.metadata_requests = 0

    def save(self):
        atomic_json(state_path(), self.state)

    def request_count(self):
        self.metadata_requests += 1
        if self.metadata_requests > limit("MINAS_FILE_MAX_METADATA_REQUESTS", 10000):
            raise ValueError("Metadata request limit exceeded")

    def listing(self, session, share):
        token = urlparse(share).path.strip("/").split("/")[-1]
        endpoint = "https://minas.mihoyo.com/api/v2.1/share-links/" + token + "/dirents/"
        queue = ["/"]
        seen = set()
        files = {}
        directories = 0
        entries = 0
        while queue:
            parent = queue.pop(0)
            directories += 1
            if directories > limit("MINAS_FILE_MAX_DIRS", 256):
                raise ValueError("Directory limit exceeded")
            self.request_count()
            with session.get(endpoint, params={"path": parent}, stream=True, timeout=(30, 30)) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_content(65536):
                    raw.extend(chunk)
                    if len(raw) > limit("MINAS_FILE_MAX_LISTING_BYTES", 2 * 1024 ** 2):
                        raise ValueError("Directory response too large")
                data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("dirent_list"), list):
                raise ValueError("Invalid directory response")
            # The observed API is unpaginated. Fail closed if pagination is introduced.
            if any(data.get(key) for key in ("has_more", "next", "next_page", "next_page_token")):
                raise ValueError("Unexpected listing pagination")
            for entry in data["dirent_list"]:
                entries += 1
                if entries > limit("MINAS_MAX_FILES", 5000):
                    raise ValueError("Listing entry limit exceeded")
                directory = entry.get("is_dir")
                if not isinstance(directory, bool):
                    raise ValueError("Invalid directory entry type")
                path = remote_path(entry.get("folder_path" if directory else "file_path"), parent, directory)
                key = path.rstrip("/").casefold()
                if key in seen:
                    raise ValueError("Repeated/colliding listing path")
                seen.add(key)
                if len(PurePosixPath(path).parts) > limit("MINAS_FILE_MAX_DEPTH", 32):
                    raise ValueError("Directory depth exceeded")
                if directory:
                    queue.append(path)
                else:
                    size = entry.get("size")
                    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                        raise ValueError("Invalid listing size")
                    if size > limit("MINAS_MAX_FILE_BYTES", 256 * 1024 ** 2):
                        raise ValueError("Remote file exceeds size limit")
                    files[path] = {"size": size, "last_modified": entry.get("last_modified")}
        return files

    def add_post(self, row, share):
        title = str(row.get("post_name", "")).strip().strip("\"'")
        post_id = str(row.get("post_url") or row.get("post_time", "") + " " + title).strip()
        post = self.state["posts"].setdefault(post_id, {"sources": []})
        post.update({"title": title, "post_url": row.get("post_url", ""),
                     "post_time": row.get("post_time", ""), "current_source": share})
        if share not in post["sources"]:
            post["sources"].append(share)
        source = self.state["sources"].setdefault(share, {"posts": [], "files": {}})
        if post_id not in source["posts"]:
            source["posts"].append(post_id)

    def head(self, session, share, path):
        self.request_count()
        with session.head(share + "files/", params={"p": path, "dl": "1"},
                          allow_redirects=True, timeout=(30, 30)) as response:
            response.raise_for_status()
            return response.headers.get("ETag") or None

    def has_local_preview(self, asset):
        cache = Path("docs/cached.json")
        if not cache.exists():
            return False
        entries = json.loads(cache.read_text(encoding="utf-8"))
        return any(entries.get(path, {}).get("thumb") and
                   (Path("docs") / entries[path]["thumb"]).is_file() for path in asset["paths"])

    def receive(self, session, share, path, info):
        if info["size"] > self.max_bytes:
            raise ValueError("Remote file exceeds per-run byte budget; raise configuration explicitly")
        if self.downloads >= self.max_downloads or self.used_bytes + info["size"] > self.max_bytes:
            self.pending += 1
            return None
        self.out_dir.mkdir(parents=True, exist_ok=True)
        check_space(self.out_dir, info["size"])
        self.downloads += 1
        with tempfile.TemporaryDirectory(prefix=".minas-file-", dir=self.out_dir) as work:
            staged = Path(work) / "asset.part"
            def consume(size):
                self.used_bytes += size
                if self.used_bytes > self.max_bytes:
                    raise ValueError("Per-run streamed byte budget exceeded")
            result = download_bounded(session, share + "files/?" + urlencode({"p": path, "dl": "1"}),
                                      staged, limit("MINAS_MAX_FILE_BYTES", 256 * 1024 ** 2),
                                      expected_size=info["size"], consume=consume)
            digest = result["sha256"]
            asset = self.state["assets"].get(digest)
            if asset is None and digest in self.canonical_images:
                asset = {"paths": [self.canonical_images[digest]], "size": result["bytes"], "sources": []}
                self.state["assets"][digest] = asset
            if asset is None or not self.has_local_preview(asset):
                suffix = PurePosixPath(path).suffix.lower()
                suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ".bin"
                destination = self.out_dir / "_minas-content" / (digest + suffix)
                if any(p.is_symlink() for p in (destination, *destination.parents)):
                    raise ValueError("Symlink content destination")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    with destination.open("rb") as existing:
                        if hashlib.file_digest(existing, "sha256").hexdigest() != digest:
                            raise ValueError("Content destination hash mismatch")
                else:
                    os.replace(staged, destination)
                if asset is None:
                    asset = {"paths": [], "size": result["bytes"], "sources": []}
                    self.state["assets"][digest] = asset
                if destination.as_posix() not in asset["paths"]:
                    asset["paths"].append(destination.as_posix())
            association = {"share": share, "path": path}
            if association not in asset["sources"]:
                asset["sources"].append(association)
            return {**info, "sha256": digest, "etag": result.get("etag"),
                    "verified_at": self.now, "checked_at": self.now, "baseline_at": self.now}

    def scan(self, session, share):
        files = self.listing(session, share)
        source = self.state["sources"][share]
        # Resume deferred work ahead of previously checked entries.
        ordered = sorted(files, key=lambda path: (source["files"].get(path, {}).get("checked_at", 0), path))
        for path in ordered:
            info = files[path]
            old = source["files"].get(path)
            etag = self.head(session, share, path)
            changed = old is None or old.get("size") != info["size"]
            if old and old.get("last_modified") is not None:
                changed |= old["last_modified"] != info["last_modified"]
            if old and old.get("etag") and etag:
                changed |= old["etag"] != etag
            if old and old.get("seeded"):
                # Trust only older matching-size committed snapshots during bootstrap.
                try:
                    modified = datetime.fromisoformat(info["last_modified"]).timestamp()
                except (ValueError, TypeError):
                    changed = True
                else:
                    changed |= modified > old.get("snapshot_at", 0)
            due = old is not None and self.now - old.get("verified_at", old.get("baseline_at", self.now)) >= self.refresh_seconds
            if due and not changed and self.verifications >= self.verify_quota:
                self.pending += 1
                continue
            if changed or due:
                if due and not changed:
                    self.verifications += 1
                updated = self.receive(session, share, path, info)
                if updated is None:
                    continue
                updated["etag"] = updated.get("etag") or etag
                source["files"][path] = updated
                source["last_transfer_at"] = self.now
                self.save()  # Durable per-file checkpoint, never a partial file.
            else:
                source["files"][path] = {**old, **info, "etag": etag or old.get("etag"),
                                         "checked_at": self.now,
                                         "baseline_at": old.get("baseline_at", self.now)}
                source["files"][path].pop("seeded", None)
        # Keep missing paths and their asset relationships; never infer deletion.
        source["listed_at"] = self.now
        self.save()

    def run(self, rows):
        groups = {}
        for row in rows:
            share = normalize_share(row.get("minas_link", ""))
            groups.setdefault(share, []).append(row)
            self.add_post(row, share)
        errors = []
        for share, posts in sorted(groups.items(), key=lambda item: self.state["sources"][item[0]].get("last_transfer_at", 0)):
            try:
                with self.session_factory() as session:
                    self.authenticate(session, share, str(posts[0].get("minas_pwd", "")).strip().strip("\"'"))
                    self.scan(session, share)
            except Exception as error:
                errors.append(share)
                reason = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
                print(f"[files] Failed source {share}: {reason}")
        print(f"[files] {len(groups)} distinct shares; {self.downloads} downloads; {self.used_bytes} bytes; {self.pending} deferred")
        if errors:
            raise RuntimeError(f"File-level source checks failed: {errors}")
