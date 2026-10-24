from __future__ import annotations

from typing import Any, Dict, List, Tuple
from pathlib import Path
import csv
import json
import re

from bs4 import BeautifulSoup
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # for src/

# Robust imports so it works when run as a package or module
try:
    from ..base.BaseScraper import BaseScraper  # src.scraper.MinasScraper path
except Exception:  # pragma: no cover
    try:
        from src.base.BaseScraper import BaseScraper
    except Exception:
        from base.BaseScraper import BaseScraper


from src.utils.post_evidence import capture_post_evidence


class MinasScraper(BaseScraper):
    """
    Scan posts from a CSV under /posts and append Minas link/password.

    Input task example:
    {
        "name": "米游社-官方资讯-minas",
        "source": "米游社-官方资讯.csv",   # the file under /posts/
        "keyword": ["壁纸", "影像档案"],     # optional; can be string or list
        "type": "minas",
        "comment": "扫描所有米游社官方资讯的minas链接"
    }

    Output: /metafiles/<task_name>.csv with columns:
        post_time, post_name, post_url, minas_link, minas_pwd
    """

    # --------------------- init ---------------------
    def __init__(self, task_name: str, **kwargs: Any) -> None:
        super().__init__(task_name, **kwargs)

        t = self.task
        src = (t.get("source") or t.get("source_csv") or "").strip()
        if not src:
            raise ValueError("Task must define 'source' (posts CSV filename under /posts)")

        self.source_csv = Path(src)
        if not self.source_csv.is_absolute():
            self.source_csv = Path("posts") / self.source_csv
        self.out_csv = Path("metafiles") / f"{self.task_name}.csv"

        # keyword filter (string or list)
        raw_kw = t.get("keyword") or t.get("keywords") or []
        if isinstance(raw_kw, str):
            raw_kw = [raw_kw] if raw_kw.strip() else []
        self.keywords: List[str] = [str(k).strip() for k in raw_kw if str(k).strip()]
        self.keywords_lower = [k.lower() for k in self.keywords]

        # compile patterns once
        self._mihoyo_post_id = re.compile(r"/zzz/article/(\d+)")
        self._url_patterns = [
            re.compile(r"https?://minas\.mihoyo\.com/\S+", re.I),
            re.compile(r"https?://pan\.baidu\.com/s/[a-zA-Z0-9\-_=]+", re.I),
            re.compile(r"https?://(www\.)?lanzou[inx]?\.com/[a-zA-Z0-9\-_]+", re.I),
            re.compile(r"https?://cloud\.189\.cn/t/[a-zA-Z0-9]+", re.I),
            re.compile(r"https?://(www\.)?123pan\.com/s/[a-zA-Z0-9\-_]+", re.I),
            re.compile(r"https?://share\.weiyun\.com/[a-zA-Z0-9]+", re.I),
            re.compile(r"https?://[\w\-\.]*mihoyo\.[\w\.]+/\S+", re.I),
            re.compile(r"https?://\S*minas\S+", re.I),
        ]
        self._pwd_patterns = [
            re.compile(r"提取码[:：\s]*([a-zA-Z0-9]{3,20})"),
            re.compile(r"密码[:：\s]*([a-zA-Z0-9]{3,20})"),
            re.compile(r"访问码[:：\s]*([a-zA-Z0-9]{3,20})"),
            re.compile(r"pass(code)?[:：\s]*([a-zA-Z0-9]{3,20})", re.I),
        ]

    # --------------------- public API ---------------------
    def run(self) -> None:
        """Main entry: read posts CSV, filter by keywords, extract minas, write metafile."""
        if not self.source_csv.exists():
            raise FileNotFoundError(f"posts CSV not found: {self.source_csv}")


        # load existing results to merge/skip
        existing_map = self._load_existing_map(self.out_csv)

        # read posts
        rows: List[Dict[str, str]] = []
        with open(self.source_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append({
                    "post_time": (r.get("post_time") or "").strip(),
                    "post_name": (r.get("post_name") or "").strip(),
                    "post_url":  (r.get("post_url")  or "").strip(),
                })

        # stop_on_seen: if True, stop processing after first seen (cached) entry
        stop_on_seen = bool(self.task.get("stop_on_seen", False))
        seen_cached = False

        results: List[Dict[str, str]] = []
        total = len(rows)
        for idx, row in enumerate(rows, start=1):
            url = row.get("post_url")
            if not url:
                continue

            cached = existing_map.get(url, {})

            # keyword pre-filter — do not write non-matching posts to metafile
            if not self._matches_keywords(row.get("post_name", "")):
                self.log.info(f"[{idx}/{total}] skip (keyword): {row.get('post_time','')} | {row.get('post_name','')}")
                continue

            # if we already have both, keep and skip network
            if cached.get("minas_link") and cached.get("minas_pwd"):
                results.append(self._merge_cached(row, cached))
                self.log.info(f"[{idx}/{total}] skip (cached): {row.get('post_time','')} | {row.get('post_name','')}")
                if stop_on_seen:
                    seen_cached = True
                    break
                continue

            self.log.info(f"[{idx}/{total}] check: {row.get('post_time','')} | {row.get('post_name','')} | {url}")

            minas_link = cached.get("minas_link") or ""
            minas_pwd  = cached.get("minas_pwd")  or ""

            # 0) Miyoushe API fast path for /zzz/article/<id>
            m = self._mihoyo_post_id.search(url)
            if m:
                pid = m.group(1)
                api_link, api_pwd = self._extract_via_miyoushe_api(pid)
                if api_link:
                    minas_link = api_link
                    minas_pwd = minas_pwd or api_pwd or ""
                    self.log.info(f"    -> api: link found ({'pwd ok' if api_pwd else 'no pwd'})")

            # 1) Fallback to HTML only if still missing link
            soup = None
            if not minas_link:
                soup = self.get_soup(url)
                ml, mp = self._extract_from_soup(soup)
                if ml:
                    minas_link = ml
                    minas_pwd = minas_pwd or mp or ""

            # 2) Fallback to raw text only if anything still missing
            if (not minas_link) or (not minas_pwd):
                if soup is None:
                    try:
                        soup = self.get_soup(url)
                    except Exception:
                        soup = None
                if soup is not None:
                    text = soup.get_text("\n", strip=False)
                    tl, tp = self._extract_from_text(text)
                    minas_link = minas_link or tl or ""
                    minas_pwd = minas_pwd or tp or ""

            results.append({
                **row,
                "minas_link": minas_link,
                "minas_pwd": minas_pwd,
            })
            if minas_link:
                self.log.info(f"    -> found: {minas_link}  pwd: {minas_pwd or '-'}")
            else:
                self.log.info("    -> no minas link")
            self._sleep()

        self._write_csv(results)

    # --------------------- extraction helpers ---------------------
    def _matches_keywords(self, name: str) -> bool:
        if not self.keywords_lower:
            return True
        s = (name or "").lower()
        return any(k in s for k in self.keywords_lower)

    def _merge_cached(self, row: Dict[str, str], cached: Dict[str, str]) -> Dict[str, str]:
        return {
            "post_time": row.get("post_time", ""),
            "post_name": row.get("post_name", ""),
            "post_url":  row.get("post_url",  ""),
            "minas_link": (cached or {}).get("minas_link", ""),
            "minas_pwd":  (cached or {}).get("minas_pwd",  ""),
        }

    def _extract_from_soup(self, soup: BeautifulSoup) -> Tuple[str | None, str | None]:
        link = None
        pwd = None
        # anchors first
        for a in soup.select('a[href]'):
            href = (a.get('href') or '').strip()
            if not href:
                continue
            for up in self._url_patterns:
                m = up.search(href)
                if m:
                    link = m.group(0)
                    pwd = pwd or self._password_near(a)
                    if link and pwd:
                        return link, pwd
        # then full text
        text = soup.get_text("\n", strip=False)
        return self._extract_from_text(text)

    def _extract_from_text(self, text: str) -> Tuple[str | None, str | None]:
        link = None
        pwd = None
        for up in self._url_patterns:
            m = up.search(text)
            if m:
                link = m.group(0)
                break
        if link:
            for pp in self._pwd_patterns:
                m2 = pp.search(text)
                if m2:
                    # pick first non-empty captured group
                    for g in m2.groups():
                        if g:
                            pwd = g
                            break
                    break
        return link, pwd

    def _password_near(self, a_tag) -> str | None:
        candidates: List[str] = []
        try:
            candidates.append(a_tag.get_text(" ", strip=True) or "")
            if a_tag.parent:
                candidates.append(a_tag.parent.get_text(" ", strip=True) or "")
            prev = a_tag.find_previous(string=True)
            if prev:
                candidates.append(str(prev))
            nxt = a_tag.find_next(string=True)
            if nxt:
                candidates.append(str(nxt))
        except Exception:
            pass
        blob = "\n".join(candidates)
        for pp in self._pwd_patterns:
            m = pp.search(blob)
            if m:
                for g in m.groups():
                    if g:
                        return g
        return None

    def _extract_via_miyoushe_api(self, post_id: str) -> Tuple[str | None, str | None]:
        api = "https://bbs-api.miyoushe.com/post/wapi/getPostFull"
        params = {"gids": 8, "post_id": post_id, "read": 1}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.miyoushe.com",
            "Referer": "https://www.miyoushe.com/",
        }
        # allow config to inject mihoyo-specific headers
        ds = ((self.config or {}).get("mihoyo") or {}).get("ds")
        if ds:
            headers["DS"] = ds
        try:
            resp = self.fetch(api, params=params, headers=headers)
            if getattr(resp, 'status_code', 200) in (403, 404):
                self.log.info(f"API blocked or not found ({resp.status_code}) for post {post_id}")
            data = resp.json()
        except Exception as e:
            self.log.info(f"API getPostFull failed for {post_id}: {e}")
            return None, None

        post = (((data or {}).get("data") or {}).get("post") or {}).get("post") or {}
        if not post:
            return None, None

        blobs: List[str] = []
        html_content = post.get("content") or ""
        if html_content:
            blobs.append(html_content)
        struct_raw = post.get("structured_content") or ""
        if struct_raw:
            try:
                ops = json.loads(struct_raw)
                for op in ops:
                    ins = op.get("insert")
                    if isinstance(ins, str):
                        blobs.append(ins)
                    attrs = op.get("attributes") or {}
                    if isinstance(attrs, dict):
                        lnk = attrs.get("link")
                        if lnk:
                            blobs.append(str(lnk))
            except Exception:
                pass

        big_text = "\n".join(blobs)
        link, pwd = self._extract_from_text(big_text)
        try:
            wrapper = ((data or {}).get("data") or {}).get("post") or {}
            capture_post_evidence(f"https://www.miyoushe.com/zzz/article/{post_id}",
                                  post.get("subject", ""), html_content, struct_raw,
                                  topics=wrapper.get("topics"), protected_values=(pwd,))
        except (OSError, ValueError, TypeError) as error:
            # Additive evidence must not break an otherwise successful link extraction.
            self.log.warning(f"Post evidence not saved: {type(error).__name__}")
        return link, pwd

    # --------------------- IO helpers ---------------------
    def _load_existing_map(self, path: Path) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        if not path.exists():
            return out
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    url = (r.get("post_url") or "").strip()
                    if url:
                        out[url] = {
                            "minas_link": (r.get("minas_link") or "").strip(),
                            "minas_pwd":  (r.get("minas_pwd")  or "").strip(),
                        }
        except Exception:
            pass
        return out

    def _write_csv(self, rows: List[Dict[str, str]]) -> None:
        self.out_csv.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["post_time", "post_name", "post_url", "minas_link", "minas_pwd"]

        # Load existing rows
        existing: List[Dict[str, str]] = []
        if self.out_csv.exists():
            try:
                with open(self.out_csv, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        existing.append({k: r.get(k, "") for k in fieldnames})
            except Exception:
                pass

        # De-dup by URL: new rows first, then existing rows not in new
        new_urls = {(r.get("post_url") or "").strip() for r in rows if (r.get("post_url") or "").strip()}
        merged_rows = rows + [r for r in existing if (r.get("post_url") or "").strip() not in new_urls]

        with open(self.out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in merged_rows:
                w.writerow({k: r.get(k, "") for k in fieldnames})
        self.log.info(f"wrote CSV: {self.out_csv} (rows: {len(merged_rows)})")

if __name__ == "__main__":
    MinasScraper("米游社-官方资讯-minas").run()