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
from datetime import datetime
from urllib.parse import quote

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


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".gallery-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


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


def find_images() -> list[dict]:
    cache = load_cache()
    # Union with cache: sparse/missing originals must not erase published assets.
    paths = {p.as_posix(): p for p in DOWNLOAD_ROOT.rglob("*")
             if p.is_file() and p.suffix.lower() in IMG_EXTS}
    for key in cache:
        paths.setdefault(key.replace("\\", "/"), Path(key))
    items = []
    for key, path in sorted(paths.items()):
        cached = cache.get(key, {})
        try:
            rel = path.relative_to(DOWNLOAD_ROOT)
        except ValueError:
            continue
        folder = rel.parent.as_posix()
        folder = "" if folder == "." else folder
        present = path.is_file()
        identity, pointer = source_identity(path) if present else (None, True)
        # Preserve original published timestamps, independent of checkout mtimes.
        mtime = cached.get("mtime", 0)
        existing_thumb = cached.get("thumb")
        reusable = existing_thumb and (DOCS_DIR / existing_thumb).is_file()
        unchanged = identity == cached.get("identity") and cached.get("settings") == [MAX_BYTES, MAX_EDGE, MAX_PIXELS]
        items.append({
            "src": "../" + quote(path.as_posix(), safe="/"),
            "fs_path": str(path), "folder": folder, "name": path.stem,
            "mtime": mtime, "date_sort": _date_from_folder(rel.parts[1] if len(rel.parts)>2 else folder, mtime),
            "cache_key": key,
            "identity": (cached.get("identity") or identity) if pointer else identity,
            "settings": cached.get("settings") if pointer else [MAX_BYTES, MAX_EDGE, MAX_PIXELS],
            "unavailable": pointer, "previous_thumb": existing_thumb if reusable else None,
            "cached_thumb": existing_thumb if reusable and (pointer or unchanged) else None,
        })
    return sorted(items, key=lambda it: (-it["date_sort"], it["cache_key"]))


def build_previews(items: list[dict]) -> None:
    from PIL import Image
    if MAX_BYTES <= 0 or MAX_EDGE <= 0 or MAX_PIXELS <= 0:
        raise ValueError("Gallery limits must be positive")
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    for it in items:
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


def write_meta(items: list[dict]) -> None:
    previous = json.loads(META_FILE.read_text(encoding="utf-8")) if META_FILE.exists() else {"items": []}
    payload = {(it["folder"], it["name"]): it for it in previous.get("items", [])}
    for it in items:
        if it.get("thumb"):
            payload[(it["folder"], it["name"])] = {
                "thumb": it["thumb"], "full": it["thumb"], "name": it["name"],
                "folder": it["folder"], "mtime": it["mtime"],
            }
    ordered = sorted(payload.values(), key=lambda it: -_date_from_folder(
        it["folder"].split("/")[1] if "/" in it["folder"] else it["folder"], it["mtime"]))
    atomic_write(META_FILE, json.dumps({"items": ordered}, ensure_ascii=False).encode())
    print(f"[gallery] Wrote {META_FILE} with {len(payload)} entries")


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    # Corrupt state should stop the build instead of silently discarding it.
    return {key.replace("\\", "/"): value for key, value in json.loads(CACHE_FILE.read_text(encoding="utf-8")).items()}


def save_cache(items: list[dict]) -> None:
    data = load_cache()
    for it in items:
        if it.get("thumb") and not it.get("failed"):
            entry = {"thumb": it["thumb"], "mtime": it["mtime"], "identity": it["identity"],
                     "settings": it["settings"]}
            data[it["cache_key"]] = entry
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
