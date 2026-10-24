"""
MinasDownloader.py — ZIP-first downloader for Seafile/Minas shares.

Batch mode compares CSV row fingerprints with metafiles/asset-state.json.
Only new/changed rows are downloaded, validated in staging, and merged.
Existing folders alone do not imply completion. Historical records are seeded
from the repository's tracked assets; LFS originals need not be hydrated.

Quick start:
  pip install playwright requests bs4
  python -m playwright install chromium

Examples:
  # Single share (zip root)
  python src/downloader/MinasDownloader.py \
    --url "https://minas.mihoyo.com/d/97936e3e62a949b2930d/" \
    --password "zzzbz2025" \
    --out ./downloads \
    --zip --path "/"

  # Batch from CSV (skips already-downloaded posts)
  python src/downloader/MinasDownloader.py \
    --out ./downloads \
    --batch \
    --csv "metafiles/米游社-官方资讯-minas.csv"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import shutil
import sys
import zipfile
import json
import tempfile
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse, urljoin
import urllib.parse

import requests
from bs4 import BeautifulSoup

try:
    from .asset_io import atomic_json, fingerprint, download_zip, extract_zip, install_tree, check_space
except ImportError:
    from asset_io import atomic_json, fingerprint, download_zip, extract_zip, install_tree, check_space

try:
    from .minas_files import CATEGORY, FileUpdater
except ImportError:
    from minas_files import CATEGORY, FileUpdater

STATE_FILE = Path(os.getenv("MINAS_STATE_FILE", "metafiles/asset-state.json"))

# --- Defaults (override via CLI or env) ---
DEFAULT_URL = os.getenv("MINAS_URL", "https://minas.mihoyo.com/d/97936e3e62a949b2930d/")
DEFAULT_PASSWORD = os.getenv("MINAS_PASSWORD", "zzzbz2025")
DEFAULT_OUTDIR = os.getenv("MINAS_OUTDIR", "./downloads")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

API_BASE = "https://minas.mihoyo.com"
ROW_TIMEOUT_SEC = int(os.getenv("MINAS_ROW_TIMEOUT", "420"))  # per-row timeout to avoid getting stuck
MAX_ROWS = os.getenv("MINAS_MAX_ROWS")  # optional limit for CI/debug

# --------------------- Utilities ---------------------

def sanitize_filename(name: str) -> str:
    # return re.sub(r"[\\/:*?\"<>|]", "_", name or "").strip() or "untitled"
    return re.sub(r"[\\/:*?\"]", "_", name or "").strip() or "untitled"


def _norm_cell(val: str | None) -> str:
    """Normalize CSV cells: strip whitespace and stray quotes (even unmatched)."""
    v = (str(val) if val is not None else "").strip()
    # Strip matching outer quotes if present
    if (len(v) >= 2) and ((v[0] == v[-1]) and v[0] in ('"', "'")):
        v = v[1:-1].strip()
    # Also defensively strip stray quotes on either end
    v = v.strip('\"\'')
    return v

# Only strip a single pair of matching outer quotes (either " or ')
def _norm_outer_quotes(val: str | None) -> str:
    v = (str(val) if val is not None else "").strip()
    if (len(v) >= 2) and ((v[0] == v[-1]) and v[0] in ('"', "'")):
        v = v[1:-1].strip()
    return v

# --------------------- Static path (requests) ---------------------

def authenticate_with_password(session: requests.Session, url: str, password: str) -> requests.Response:
    headers = {"User-Agent": USER_AGENT, "Referer": url}
    r = session.get(url, headers=headers, allow_redirects=True, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    pwd_input = soup.find("input", {"type": "password"})
    if not pwd_input:
        return r  # already accessible

    form = pwd_input.find_parent("form")
    if not form:
        raise RuntimeError("Password form not found in static HTML (site likely JS-driven).")

    action = urljoin(r.url, form.get("action") or r.url)
    if urlparse(action).scheme != "https" or urlparse(action).netloc != urlparse(url).netloc:
        raise RuntimeError("Password form leaves share origin")
    method = (form.get("method") or "post").lower()

    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        if inp.get("type") == "password":
            payload[name] = password
        else:
            payload[name] = inp.get("value") or ""

    if method == "post":
        auth = session.post(action, data=payload, headers=headers, allow_redirects=True, timeout=30)
    else:
        auth = session.get(action, params=payload, headers=headers, allow_redirects=True, timeout=30)

    auth.raise_for_status()
    if BeautifulSoup(auth.text, "html.parser").find("input", {"type": "password"}):
        raise RuntimeError("Static auth failed — password field still present.")
    return auth

# --------------------- ZIP via browser (Playwright) ---------------------

async def _zip_current_path(
    url: str, password: str, path: str, out_dir: Path,
    force_extract_dir: Path | None = None
) -> Path:
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    check_space(out_dir)
    path = "/" + path.strip("/") + "/" if path.strip("/") else "/"
    target_url = f"{url.rstrip('/')}/?{urllib.parse.urlencode({'p': path, 'mode': 'list'})}"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(user_agent=USER_AGENT, accept_downloads=True)
            page = await context.new_page()
            await page.goto(target_url, wait_until="domcontentloaded")
            pwd = await page.query_selector('input[type="password"]')
            if pwd:
                await pwd.fill(password)
                await pwd.press("Enter")
                for selector in ('button[type="submit"]', 'button.btn-success',
                                 'button.btn-primary', 'input[type="submit"]'):
                    button = await page.query_selector(selector)
                    if button:
                        await button.click()
                        break
            await page.wait_for_selector('.shared-dir-view-main', timeout=30000)
            button = page.locator('button.shared-dir-op-btn').first
            # Cancel the browser transfer immediately; stream it ourselves with hard limits.
            async with page.expect_download(timeout=ROW_TIMEOUT_SEC * 1000) as pending:
                await button.click()
            download = await pending.value
            download_url = download.url
            await download.cancel()
            with requests.Session() as session:
                session.headers.update({"User-Agent": USER_AGENT, "Referer": page.url})
                for cookie in await context.cookies():
                    session.cookies.set(cookie["name"], cookie["value"],
                                        domain=cookie["domain"], path=cookie["path"])
                with tempfile.TemporaryDirectory(prefix=".minas-", dir=out_dir) as work:
                    work = Path(work)
                    archive = work / "archive.zip"
                    stage = work / "stage"
                    stage.mkdir()
                    download_zip(session, download_url, archive)
                    extract_zip(archive, stage)
                    destination = force_extract_dir or out_dir / sanitize_filename(Path(download.suggested_filename).stem)
                    install_tree(stage, destination)
                    return destination
        finally:
            await browser.close()

# --------------------- Batch from CSV ---------------------

DEFAULT_CSV = Path("metafiles") / "米游社-官方资讯-minas.csv"
BATCH_OUT_SUBDIR = "米游社-官方资讯-minas"

async def _process_csv_row(row: dict, out_root: Path) -> None:
    post_name = _norm_outer_quotes(row.get("post_name") or row.get("title") or row.get("name"))
    url = _norm_cell(row.get("minas_link") or row.get("url") or row.get("link"))
    password = _norm_cell(row.get("minas_pwd") or row.get("password") or row.get("passwd"))
    post_time = _norm_cell(row.get("post_time") or "")

    # Normalize post_time to yyyy.mm.dd if possible
    norm_post_time = ""
    if post_time:
        # Acceptable formats: YYYY-MM-DD... or YYYY/MM/DD...
        if len(post_time) >= 10 and (
            (post_time[4] == '-' and post_time[7] == '-') or (post_time[4] == '/' and post_time[7] == '/')
        ):
            # Extract first 10 chars and replace - or / with .
            norm_post_time = post_time[:10].replace("-", ".").replace("/", ".")
        else:
            norm_post_time = ""

    if not url or not password or not post_name:
        print(f"[CSV] Skip (missing field): post_name='{post_name}' url='{url}' pwd={'yes' if bool(password) else 'no'}")
        return

    folder_name = f"{norm_post_time} {post_name}" if norm_post_time else post_name
    target_dir = out_root / sanitize_filename(folder_name)
    if target_dir.name in (".", "..") or target_dir.resolve().parent != out_root.resolve():
        raise ValueError("Unsafe post directory")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    key = target_dir.name
    identity = fingerprint(row)
    refresh = {name.strip() for name in os.getenv("MINAS_REFRESH_POSTS", "").splitlines() if name.strip()}
    if state.get(key) == identity and key not in refresh:
        print(f"[CSV] SKIP (unchanged): {target_dir}")
        return
    await _zip_current_path(url, password, path="/", out_dir=out_root, force_extract_dir=target_dir)
    state[key] = identity
    atomic_json(STATE_FILE, state)


def run_batch_from_csv(csv_path: Path, out_dir: Path) -> None:
    out_root = out_dir / BATCH_OUT_SUBDIR
    out_root.mkdir(parents=True, exist_ok=True)

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print(f"[CSV] No rows found in {csv_path}")
        return

    print(f"[CSV] Loaded {len(rows)} rows from {csv_path}")
    max_rows = None
    try:
        max_rows = int(MAX_ROWS) if MAX_ROWS is not None else None
    except Exception:
        max_rows = None
    row_timeout = ROW_TIMEOUT_SEC if ROW_TIMEOUT_SEC > 0 else None

    failures = []
    selected_rows = rows[:max_rows] if max_rows is not None else rows
    category_rows = [row for row in selected_rows if CATEGORY in row.get("post_name", "")
                     and _norm_cell(row.get("minas_link")) and _norm_cell(row.get("minas_pwd"))]
    if category_rows:
        try:
            FileUpdater(out_dir, authenticate_with_password).run(category_rows)
        except Exception as error:
            failures.append("file-level category")
            print(f"[files] FAILED: {error}")
    for idx, row in enumerate(rows, 1):
        if max_rows is not None and idx > max_rows:
            print(f"[CSV] Reached MINAS_MAX_ROWS={max_rows}; stopping early.")
            break
        if row in category_rows:
            continue
        try:
            if row_timeout:
                asyncio.run(asyncio.wait_for(_process_csv_row(row, out_root), timeout=row_timeout))
            else:
                asyncio.run(_process_csv_row(row, out_root))
            print(f"[CSV] ({idx}/{len(rows)}) done")
        except asyncio.TimeoutError:
            failures.append(idx)
            print(f"[CSV] ({idx}/{len(rows)}) TIMEOUT after {row_timeout}s; skipping.")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            failures.append(idx)
            print(f"[CSV] ({idx}/{len(rows)}) FAILED: {e}")
    if failures:
        raise RuntimeError(f"Asset rows failed: {failures}")

# --------------------- Orchestrator ---------------------

def run(url: str, password: str, out_dir: Path) -> None:
    # Default to single ZIP on '/' if neither zip nor batch is selected explicitly
    zip_args = getattr(run, "__zip_args__", None)
    if not zip_args or (not zip_args.get("zip_one")):
        zip_args = {"zip_one": True, "path": "/"}

    if zip_args.get("zip_one"):
        path = zip_args.get("path") or "/"
        saved = asyncio.run(_zip_current_path(url, password, path, out_dir))
        print(f"ZIP completed: {saved}")

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Minas ZIP downloader (single or batch).")
    parser.add_argument("--url", default=DEFAULT_URL, help="Share URL for single ZIP mode")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Password for single ZIP mode")
    parser.add_argument("--out", default=DEFAULT_OUTDIR, help="Output directory (default: ./downloads)")

    # ZIP (single)
    parser.add_argument("--zip", action="store_true", help="Click ZIP and download one path from a single share")
    parser.add_argument("--path", default="/", help="Path within the share for single ZIP mode (default: /)")

    # Batch
    parser.add_argument("--batch", action="store_true", help="Read CSV and download each post (zip, extract, rename)")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Path to CSV (default: metafiles/米游社-官方资讯-minas.csv)")

    args = parser.parse_args(argv)

    # Batch first
    if args.batch:
        run_batch_from_csv(Path(args.csv), Path(args.out))
        return 0

    try:
        run.__zip_args__ = {"zip_one": bool(args.zip), "path": args.path}
        run(args.url, args.password, Path(args.out))
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
