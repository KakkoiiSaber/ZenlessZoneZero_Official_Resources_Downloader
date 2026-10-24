"""Keep bounded, neutral evidence from posts already fetched by the link scraper."""
import json
import os
from pathlib import Path
import re
from bs4 import BeautifulSoup
from src.downloader.asset_io import atomic_json


def capture_post_evidence(url, title, html_content, structured_content, topics=None, protected_values=()):
    def safe(value):
        value = str(value or "").strip()
        if any(term in value.lower() for term in ("http://", "https://", "密码", "提取码", "访问码", "password", "passcode", "minas")):
            return ""
        if any(secret and secret in value for secret in protected_values):
            return ""
        return value
    lines = BeautifulSoup((html_content or "")[:200000], "html.parser").get_text("\n", strip=True).splitlines()
    try:
        operations = json.loads((structured_content or "[]")[:200000])
        lines += [line for op in operations if isinstance(op, dict) and isinstance(op.get("insert"), str)
                  for line in op["insert"].splitlines()]
    except (ValueError, TypeError):
        pass
    mentions = []
    for raw_line in lines:
        line = safe(raw_line)
        if not line:
            continue
        for match in re.finditer(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\s*版本|(?:version|v)\s*(\d{1,2})\.(\d{1,2})(?![\d.])", line, re.I):
            major, minor = (match[1], match[2]) if match[1] else (match[3], match[4])
            start = max(0, match.start() - 70)
            evidence = {"version": f"{int(major)}.{int(minor)}", "excerpt": line[start:start+180],
                        "scope": "post_body", "confidence": "unreviewed"}
            if evidence not in mentions:
                mentions.append(evidence)
            if len(mentions) >= 5:
                break
        if len(mentions) >= 5:
            break
    names = []
    for topic in topics[:100] if isinstance(topics, list) else []:
        name = safe(topic.get("name") or topic.get("title")) if isinstance(topic, dict) else safe(topic)
        if name and len(name) <= 80 and name not in names:
            names.append(name)
    path = Path(os.getenv("MINAS_POST_EVIDENCE_FILE", "metafiles/post-evidence.json"))
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "posts": {}}
    if data.get("version") != 1:
        raise ValueError("Unsupported post evidence schema")
    data["posts"][url] = {"title": safe(title)[:200], "source_url": url,
                           "version_mentions": mentions, "topics": sorted(names)[:20]}
    atomic_json(path, data)
