#!/usr/bin/env python3
"""
Build a lightweight gallery for GitHub Pages:
 - Scans downloads/ for images (newest first)
 - Generates compressed previews (~100 KB max each) in docs/thumbs/
 - Writes docs/meta.json with preview/original info
 - Writes docs/index.html that fetches meta.json client-side and renders cards

Env overrides:
  GALLERY_DOWNLOADS   root folder to scan (default: downloads)
  GALLERY_TITLE       page title (default: Downloads Gallery)
  GALLERY_MAX_BYTES   max preview size in bytes (default: 100000)
"""
from __future__ import annotations

import hashlib
import tempfile
import html
import io
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime
from urllib.parse import quote

try:
    from .deduplicate_images import load_aliases, resolve
except ImportError:
    from deduplicate_images import load_aliases, resolve

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DOWNLOAD_ROOT = Path(os.getenv("GALLERY_DOWNLOADS", "downloads"))
DOCS_DIR = Path("docs")
META_FILE = DOCS_DIR / "meta.json"
CACHE_FILE = DOCS_DIR / "cached.json"
TITLE = os.getenv("GALLERY_TITLE", "Downloads Gallery")
THUMB_DIR = DOCS_DIR / "thumbs"
MAX_BYTES = int(os.getenv("GALLERY_MAX_BYTES", "100000"))
MAX_EDGE = int(os.getenv("GALLERY_MAX_EDGE", "1600"))
MAX_PIXELS = int(os.getenv("GALLERY_MAX_PIXELS", "50000000"))
ORIGINAL_BASE = os.getenv("GALLERY_ORIGINAL_BASE", "https://media.githubusercontent.com/media/KakkoiiSaber/zzz_scraper/main/")


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".gallery-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def preview_alias(value, aliases):
    prefix = DOCS_DIR.as_posix().rstrip("/") + "/"
    if value.startswith("thumbs/"):
        mapped = resolve(prefix + value, aliases["previews"])
        return mapped[len(prefix):] if mapped.startswith(prefix) else mapped
    return value


def source_identity(path):
    with path.open("rb") as stream:
        head = stream.read(1024)
        if head.splitlines()[:1] == [b"version https://git-lfs.github.com/spec/v1"]:
            match = re.search(rb"oid sha256:([0-9a-f]{64})", head)
            return ("sha256:" + match[1].decode() if match else None), True
        stream.seek(0)
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest(), False



def _date_from_folder(folder: str, mtime: float) -> float:
    """
    Try to parse a leading date in folder name like YYYY.MM.DD.
    Return a sortable timestamp (seconds). Fallback to mtime.
    """
    m = re.match(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", folder)
    if not m:
        return mtime
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return dt.timestamp()
    except Exception:
        return mtime


def parse_versions(text):
    matches = re.findall(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\s*版本", str(text))
    matches += re.findall(r"(?<![\w.])[vV](\d{1,2})\.(\d{1,2})(?![\d.])", str(text))
    return sorted({f"{int(major)}.{int(minor)}" for major, minor in matches},
                  key=lambda value: tuple(map(int, value.split("."))), reverse=True)


def image_versions(entry):
    versions = set()
    for alias in entry.get("associations", []):
        path = alias.get("original_path") or alias.get("path") or alias.get("folder", "") + "/" + alias.get("name", "")
        parts = path.replace("\\", "/").split("/")
        # Exclude the enclosing post title for cumulative image-archive posts.
        position = next((i for i, part in enumerate(parts) if "影像档案记录分享" in part), None)
        own_path = "/".join(parts[position + 1:]) if position is not None else path
        versions.update(parse_versions(own_path))
    for association in entry.get("posts", []):
        versions.update(parse_versions(association.get("remote_path", "")))
    if not versions:
        for association in entry.get("posts", []):
            title = association.get("title", "")
            if "影像档案记录分享" not in title:
                versions.update(parse_versions(title))
    return sorted(versions, key=lambda value: tuple(map(int, value.split("."))), reverse=True) or ["未分类"]


def image_categories(entry):
    texts = [alias.get("folder", "") for alias in entry.get("associations", [])]
    texts += [post.get("title", "") for post in entry.get("posts", [])]
    categories = set()
    for text in texts:
        if "影像档案" in text:
            categories.add("影像档案")
        elif "月历" in text:
            categories.add("月历壁纸")
        elif "表情" in text:
            categories.add("表情包")
        elif "壁纸" in text:
            categories.add("壁纸")
    return sorted(categories) or ["其他"]


def git_pointer_identities(wanted):
    """Read tiny committed pointer blobs in sparse checkouts, never run Git LFS."""
    if not Path(".git").exists() or not wanted:
        return {}
    tree = subprocess.run(["git", "ls-tree", "-rlz", "HEAD", "--", DOWNLOAD_ROOT.as_posix()],
                          capture_output=True, check=True).stdout
    objects = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        fields, raw_path = record.split(b"\t", 1)
        mode, kind, oid, size = fields.split()
        path = raw_path.decode("utf-8")
        if kind == b"blob" and path in wanted and int(size) <= 1024:
            objects[path] = oid
    identities = {}
    with subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        for path, oid in objects.items():
            process.stdin.write(oid + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob" or int(header[2]) > 1024:
                raise ValueError("Unexpected Git pointer response")
            data = process.stdout.read(int(header[2]))
            process.stdout.read(1)
            if data.splitlines()[:1] == [b"version https://git-lfs.github.com/spec/v1"]:
                match = re.search(rb"oid sha256:([0-9a-f]{64})", data)
                if match:
                    identities[path] = "sha256:" + match[1].decode()
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("Unable to read committed pointer identities")
    return identities


def content_index():
    path = Path(os.getenv("MINAS_FILE_STATE_FILE", "metafiles/minas-files.json"))
    if not path.exists():
        return {}, {}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1:
        raise ValueError("Unsupported file index")
    paths, posts = {}, {}
    for sha, asset in state["assets"].items():
        identity = "sha256:" + sha
        for name in asset["paths"]:
            paths[name] = identity
        associations = []
        for source in asset["sources"]:
            for post_id in state["sources"].get(source["share"], {}).get("posts", []):
                post = state["posts"][post_id]
                associations.append({"post_id": post_id, "title": post["title"],
                    "post_url": post.get("post_url", ""), "share": source["share"],
                    "remote_path": source["path"], "relation": "included-in-share"})
        posts[identity] = associations
    return paths, posts


def find_images() -> list[dict]:
    cache = load_cache()
    indexed_paths, indexed_posts = content_index()
    aliases = load_aliases()
    indexed_paths.update({path: "sha256:" + sha for path, sha in aliases["identities"].items()})
    for old_path, canonical in aliases["originals"].items():
        if canonical in indexed_paths:
            indexed_paths[old_path] = indexed_paths[canonical]
    published = {}
    if META_FILE.exists():
        for entry in json.loads(META_FILE.read_text(encoding="utf-8")).get("items", []):
            for alias in entry.get("associations") or [entry]:
                published[(alias["folder"], alias["name"])] = entry
    # Union with cache: sparse/missing originals must not erase published assets.
    paths = {p.as_posix(): p for p in DOWNLOAD_ROOT.rglob("*")
             if p.is_file() and p.suffix.lower() in IMG_EXTS}
    git_identities = git_pointer_identities({key for key in cache if not Path(key).is_file()})
    for key in set(cache) | set(indexed_paths):
        if Path(key).suffix.lower() in IMG_EXTS:
            paths.setdefault(key.replace("\\", "/"), Path(resolve(key, aliases["originals"])))
    items = []
    for key, path in sorted(paths.items()):
        path = Path(resolve(path.as_posix(), aliases["originals"]))
        cached = dict(cache.get(resolve(key, aliases["originals"]), {}))
        try:
            rel = Path(key).relative_to(DOWNLOAD_ROOT)
        except ValueError:
            continue
        folder = rel.parent.as_posix()
        folder = "" if folder == "." else folder
        logical_name = Path(key).stem
        prior = published.get((folder, logical_name), {})
        if not cached.get("identity") and prior.get("identity"):
            cached["identity"] = prior["identity"]
        present = path.is_file()
        identity, pointer = source_identity(path) if present else (None, True)
        identity = identity or indexed_paths.get(key) or git_identities.get(key)
        # Preserve original published timestamps, independent of checkout mtimes.
        mtime = prior.get("mtime", cached.get("mtime", 0))
        existing_thumb = cached.get("thumb")
        reusable = existing_thumb and (DOCS_DIR / existing_thumb).is_file()
        unchanged = identity == cached.get("identity") and cached.get("settings") == [MAX_BYTES, MAX_EDGE, MAX_PIXELS]
        items.append({
            "src": "../" + quote(path.as_posix(), safe="/"),
            "fs_path": str(path), "folder": folder, "name": logical_name,
            "mtime": mtime, "date_sort": _date_from_folder(rel.parts[1] if len(rel.parts)>2 else folder, mtime),
            "cache_key": key,
            "identity": (cached.get("identity") or identity) if pointer else identity,
            "settings": cached.get("settings") if pointer else [MAX_BYTES, MAX_EDGE, MAX_PIXELS],
            "posts": indexed_posts.get((cached.get("identity") or identity) if pointer else identity, []),
            "unavailable": pointer, "previous_thumb": existing_thumb if reusable else None,
            "cached_thumb": existing_thumb if reusable and (pointer or unchanged) else None,
        })
    return sorted(items, key=lambda it: (-it["date_sort"], it["cache_key"]))


def build_previews(items: list[dict]) -> None:
    from PIL import Image
    if MAX_BYTES <= 0 or MAX_EDGE <= 0 or MAX_PIXELS <= 0:
        raise ValueError("Gallery limits must be positive")
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    groups = {}
    for it in items:
        groups.setdefault(it.get("identity") or it["cache_key"], []).append(it)
    representatives = []
    representative_by_id = {}
    for members in groups.values():
        representative = min(members, key=lambda it: (not bool(it.get("cached_thumb")), it["unavailable"], it["cache_key"]))
        representatives.append(representative)
        representative_by_id[id(members)] = representative
    for it in representatives:
        if it.get("cached_thumb") or it["unavailable"]:
            it["thumb"] = it.get("cached_thumb")
            continue
        src_path = Path(it["fs_path"])
        preview_path = THUMB_DIR / src_path.relative_to(DOWNLOAD_ROOT).with_suffix(".jpg")
        try:
            with Image.open(src_path) as source:
                if source.width * source.height > MAX_PIXELS:
                    raise ValueError("Image exceeds pixel limit")
                source.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                img = source.convert("RGB")
                while True:
                    for quality in range(85, 24, -10):
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG", quality=quality, optimize=True)
                        data = buf.getvalue()
                        if len(data) <= MAX_BYTES:
                            break
                    if len(data) <= MAX_BYTES:
                        break
                    if img.size == (1, 1):
                        raise ValueError("GALLERY_MAX_BYTES too small for JPEG")
                    img = img.resize((max(1, img.width * 3 // 4), max(1, img.height * 3 // 4)), Image.Resampling.LANCZOS)
                atomic_write(preview_path, data)
                it["thumb"] = preview_path.relative_to(DOCS_DIR).as_posix()
                it["built"] = True
        except Exception as error:
            # Keep published content, but do not mark the failed identity as processed.
            print(f"[gallery] preview failed for {src_path}: {error}")
            it["thumb"] = it.get("previous_thumb")
            it["failed"] = True

    for members in groups.values():
        representative = representative_by_id[id(members)]
        for it in members:
            it["thumb"] = representative.get("thumb")
            if representative.get("built"):
                it["settings"] = representative["settings"]
            if representative.get("failed"):
                it["failed"] = True


def write_meta(items: list[dict]) -> None:
    previous = json.loads(META_FILE.read_text(encoding="utf-8")) if META_FILE.exists() else {"items": []}
    aliases = load_aliases()
    identities = {(it["folder"], it["name"]): it.get("identity") for it in items}
    legacy_indices = previous["legacy_indices"] if "legacy_indices" in previous else {
        str(index): entry.get("identity") or identities.get((entry["folder"], entry["name"]))
        for index, entry in enumerate(previous.get("items", []))}
    records = {}
    for entry in previous.get("items", []):
        contexts = entry.get("associations") or [{"folder": entry["folder"], "name": entry["name"]}]
        entry = {**entry, "thumb": preview_alias(entry["thumb"], aliases),
                 "full": preview_alias(entry.get("full", entry["thumb"]), aliases)}
        for alias in contexts:
            records[(alias["folder"], alias["name"])] = {**entry, "associations": [alias]}
    for it in items:
        key = (it["folder"], it["name"])
        if it.get("thumb") or key in records:
            entry = records.get(key, {})
            thumb = it.get("thumb") or entry["thumb"]
            records[key] = {"thumb": thumb, "full": thumb, "name": it["name"],
                "folder": it["folder"], "mtime": it["mtime"],
                "identity": it.get("identity") or entry.get("identity"),
                "original": resolve(it["cache_key"], aliases["originals"]),
                "download": ORIGINAL_BASE.rstrip("/") + "/" + quote(resolve(it["cache_key"], aliases["originals"]), safe="/"),
                "associations": [{"folder": it["folder"], "name": it["name"],
                    "path": resolve(it["cache_key"], aliases["originals"]),
                    "original_path": it["cache_key"]}],
                "posts": it.get("posts", []) or entry.get("posts", [])}
    groups = {}
    for key, entry in sorted(records.items()):
        group = entry.get("identity") or key
        if group not in groups:
            groups[group] = {**entry, "associations": [], "posts": []}
        merged = groups[group]
        for field in ("associations", "posts"):
            for association in entry.get(field, []):
                if association not in merged[field]:
                    merged[field].append(association)
    ordered = sorted(groups.values(), key=lambda it: (-_date_from_folder(
        it["folder"].split("/")[1] if "/" in it["folder"] else it["folder"], it["mtime"]),
        str(it.get("identity") or (it["folder"], it["name"]))))
    for entry in ordered:
        entry["versions"] = image_versions(entry)
        entry["categories"] = image_categories(entry)
        for field in ("associations", "posts"):
            entry[field].sort(key=lambda value: json.dumps(value, sort_keys=True))
    atomic_write(META_FILE, json.dumps({"items": ordered, "legacy_indices": legacy_indices}, ensure_ascii=False).encode())
    print(f"[gallery] Wrote {META_FILE} with {len(ordered)} content cards")


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    aliases = load_aliases()
    data = {}
    for key, value in json.loads(CACHE_FILE.read_text(encoding="utf-8")).items():
        canonical = resolve(key.replace("\\", "/"), aliases["originals"])
        entry = {**value, "thumb": preview_alias(value["thumb"], aliases)}
        data.setdefault(canonical, entry)
    return data


def save_cache(items: list[dict]) -> None:
    data = load_cache()
    aliases = load_aliases()
    for it in items:
        if it.get("thumb") and not it.get("failed"):
            entry = {"thumb": it["thumb"], "mtime": it["mtime"], "identity": it["identity"],
                     "settings": it["settings"]}
            data[resolve(it["cache_key"], aliases["originals"])] = entry
    atomic_write(CACHE_FILE, json.dumps(data, ensure_ascii=False).encode())


def render_base(count: int) -> str:
    # minimal shell; cards are injected via meta.json
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(TITLE)}</title>
  <style>
    :root {{
      --bg: #0a0d12;
      --card: #111826;
      --border: #1c2737;
      --text: #e7ecf4;
      --muted: #9fb4ce;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "Noto Sans SC", system-ui, -apple-system, sans-serif;
    }}
    header {{
      padding: 16px 20px;
      font-weight: 600;
      font-size: 18px;
    }}
    header .count {{ color: var(--muted); font-weight: 400; margin-left: 8px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      grid-auto-rows: 10px;
      grid-auto-flow: dense;
      gap: 12px;
      padding: 0 16px 32px;
    }}
    @media (max-width: 900px) {{
      .grid {{ grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); }}
    }}
    @media (max-width: 640px) {{
      .grid {{ grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }}
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(0,0,0,.28);
    }}
    .card img {{
      display: block;
      width: 100%;
      height: auto;
    }}
    .empty {{ padding: 18px; color: var(--muted); }}
    .pager {{
      text-align: center;
      padding: 12px 0 20px;
    }}
    .load-more {{
      background: var(--card);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 16px;
      cursor: pointer;
      font-weight: 600;
    }}
    .load-more:disabled {{
      opacity: 0.4;
      cursor: default;
    }}
  </style>
</head>
<body>
  <header>{html.escape(TITLE)}<span class="count">{count} images</span></header>
  <section class="grid" id="grid">
    <p class="empty" id="loading">Loading…</p>
  </section>
  <div class="pager">
    <button class="load-more" id="loadMore" disabled>Load more</button>
  </div>
  <script>
    const PAGE_SIZE = 60;
    let items = [];
    let cursor = 0;
    const gridEl = document.getElementById('grid');

    function setSpan(card, img) {{
      const styles = window.getComputedStyle(gridEl);
      const rowHeight = parseFloat(styles.gridAutoRows) || 10;
      const rowGap = parseFloat(styles.rowGap || styles.gap) || 0;
      const cardWidth = card.getBoundingClientRect().width || 260;
      const ratio = img.naturalHeight && img.naturalWidth ? (img.naturalHeight / img.naturalWidth) : 1;
      const height = cardWidth * ratio;
      const span = Math.max(1, Math.ceil((height + rowGap) / (rowHeight + rowGap)));
      card.style.gridRowEnd = `span ${span}`;
    }}

    function renderMore() {{
      const grid = gridEl;
      if (!items.length) return;
      const end = Math.min(cursor + PAGE_SIZE, items.length);
      const frag = document.createDocumentFragment();
      for (let i = cursor; i < end; i++) {{
        const it = items[i];
        const art = document.createElement('article');
        art.className = 'card';
        const a = document.createElement('a');
        a.href = it.full;
        a.target = '_blank';
        a.rel = 'noopener';
        const img = document.createElement('img');
        img.loading = 'lazy';
        img.src = it.thumb;
        img.alt = '';
        img.addEventListener('load', () => setSpan(art, img));
        a.appendChild(img);
        art.appendChild(a);
        frag.appendChild(art);
      }}
      if (cursor === 0) grid.innerHTML = '';
      grid.appendChild(frag);
      cursor = end;
      const btn = document.getElementById('loadMore');
      if (cursor >= items.length) {{
        btn.disabled = true;
        btn.textContent = 'All loaded';
      }} else {{
        btn.disabled = false;
        btn.textContent = 'Load more (' + cursor + '/' + items.length + ')';
      }}
    }}

    async function load() {{
      const grid = document.getElementById('grid');
      const loading = document.getElementById('loading');
      const btn = document.getElementById('loadMore');
      try {{
        const res = await fetch('meta.json?_=' + Date.now());
        const data = await res.json();
        items = data.items || [];
        const countEl = document.querySelector('.count');
        countEl.textContent = items.length + ' images';
        if (loading) loading.remove();
        btn.disabled = false;
        renderMore();
      }} catch (e) {{
        if (loading) loading.textContent = 'Failed to load gallery.';
        console.error(e);
      }}
    }}
    load();

    document.getElementById('loadMore').addEventListener('click', () => {{
      renderMore();
    }});
  </script>
</body>
</html>"""


def main() -> None:
    items = find_images()
    build_previews(items)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    write_meta(items)
    save_cache(items)
    if any(it.get("failed") for it in items):
        raise RuntimeError("Gallery preview failures; existing previews preserved")
    print(f"[gallery] Previews + meta ready for manual index (items: {len(items)})")


if __name__ == "__main__":
    main()
