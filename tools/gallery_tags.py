"""Evidence-backed content categories and searchable tags; dates never imply versions."""
from __future__ import annotations
import csv
import json
from pathlib import Path
import re

try:
    from .gallery_tag_rules import VERSION_NAMES, debut_title_evidence, version_label
except ImportError:
    from gallery_tag_rules import VERSION_NAMES, debut_title_evidence, version_label

CATEGORY_ORDER = ["角色图片", "月历", "活动壁纸", "测试期素材", "表情包", "影像档案", "未分类"]
CHARACTER_ALIASES = {
    "伊德海莉": ["伊德海莉"], "狛野真斗": ["狛野真斗"], "奥菲丝": ["奥菲丝"], "鬼火": ["鬼火"],
    "席德": ["席德"], "爱丽丝": ["爱丽丝"], "浮波柚叶": ["浮波柚叶"], "橘福福": ["橘福福"],
    "仪玄": ["仪玄"], "雨果": ["雨果"], "薇薇安": ["薇薇安"], "扳机": ["扳机"],
    "零号·安比": ["零号·安比", "零号安比"], "伊芙琳": ["伊芙琳"], "耀嘉音": ["耀嘉音"],
    "浅羽悠真": ["浅羽悠真"], "星见雅": ["星见雅"], "莱特": ["莱特"], "月城柳": ["月城柳"],
    "凯撒": ["凯撒"], "简": ["代理人简", "简·杜"], "青衣": ["青衣"], "艾莲": ["艾莲"],
}
# Only the character/version relationships verified against official announcements.
DEBUT = {}
for version, names, url in [
    ("2.3", ["伊德海莉", "狛野真斗"], "https://zenless.hoyoverse.com/en-us/news/159945"),
    ("2.2", ["席德", "奥菲丝", "鬼火"], "https://zenless.hoyoverse.com/en-us/news/158967"),
    ("1.7", ["薇薇安", "雨果"], "https://zzz.mihoyo.com/news/155465"),
    ("1.6", ["零号·安比", "扳机"], "https://zenless.hoyoverse.com/en-us/news/154530"),
    ("1.5", ["耀嘉音", "伊芙琳"], "https://zenless.hoyoverse.com/en-us/news/129594"),
]:
    for name in names:
        DEBUT[name] = {"version": version, "source_url": url}


def characters(text):
    return [name for name, aliases in CHARACTER_ALIASES.items() if any(alias in text for alias in aliases)]


def post_contexts():
    """Join existing CSV titles/URLs only; never copy access codes into gallery data."""
    contexts = {}
    for path in sorted(Path("metafiles").glob("*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                title, url = (row.get("post_name") or "").strip(), (row.get("post_url") or "").strip()
                if not title or not url:
                    continue
                title = title.strip("\"'")
                date = (row.get("post_time") or "")[:10].replace("-", ".").replace("/", ".")
                folder = re.sub(r'[\\/:*?"]', "_", (date + " " + title).strip())
                contexts[folder] = {"post_id": url, "post_url": url, "title": title,
                                    "post_time": row.get("post_time", ""), "relation": "published-with"}
    return contexts


def enrich_posts(entry, contexts):
    posts = list(entry.get("posts", []))
    for alias in entry.get("associations", []):
        path = alias.get("original_path") or alias.get("path") or alias.get("folder", "")
        parts = path.replace("\\", "/").split("/")
        folder = parts[2] if parts and parts[0] == "downloads" and len(parts) > 2 else (parts[1] if len(parts) > 1 else "")
        post = contexts.get(folder)
        if post and "影像档案记录分享" not in post["title"] and post not in posts:
            posts.append(post)
    return sorted(posts, key=lambda value: json.dumps(value, sort_keys=True))


def semantic_contexts(entry):
    contexts = []
    for alias in entry.get("associations", []) or [entry]:
        path = alias.get("original_path") or alias.get("path") or alias.get("folder", "") + "/" + alias.get("name", "")
        parts = path.replace("\\", "/").split("/")
        position = next((i for i, part in enumerate(parts) if "影像档案记录分享" in part), None)
        own = "/".join(parts[position + 1:]) if position is not None else path
        inner = "/".join(parts[3:]) if len(parts) > 3 and parts[0] == "downloads" else own
        contexts.append({"text": own, "own_path": own if position is not None else inner,
                         "scope": "asset_path", "value": path})
    for post in entry.get("posts", []):
        if post.get("remote_path"):
            contexts.append({"text": post["remote_path"], "own_path": post["remote_path"], "scope": "source_path",
                             "value": post["remote_path"], "source_url": post.get("share", "")})
        if "影像档案记录分享" not in post.get("title", ""):
            contexts.append({"text": post.get("title", ""), "scope": "post_title",
                             "value": post.get("title", ""), "source_url": post.get("post_url", "")})
    return contexts


def broad_categories(entry):
    result = set()
    texts = [alias.get("original_path") or alias.get("path") or alias.get("folder", "") for alias in entry.get("associations", [])]
    texts += [post.get("title", "") for post in entry.get("posts", [])]
    if not texts:
        texts = [entry.get("folder", "") + "/" + entry.get("name", "")]
    for text in texts:
        if "影像档案" in text:
            result.add("影像档案")
        elif "月历" in text:
            result.add("月历")
        elif any(term in text for term in ("降噪测试", "喧响测试", "调律测试")):
            result.add("测试期素材")
        elif "表情" in text:
            result.add("表情包")
        elif "活动" in text:
            result.add("活动壁纸")
        elif characters(text):
            result.add("角色图片")
    return [category for category in CATEGORY_ORDER if category in result] or ["未分类"]


def source_types(text):
    values = {value.upper() for value in re.findall(r"(?<![a-zA-Z])(PV|EP|MV)(?![a-zA-Z])", text, re.I)}
    values.update(value for value in ("角色展示", "动画短片") if value in text)
    return values


def build_tags(entry):
    tags = {}
    contexts = semantic_contexts(entry)
    own_source_types = set().union(*(source_types(context.get("own_path", "")) for context in contexts))
    asset_months = set()
    for context in contexts:
        if context["scope"] in ("asset_path", "source_path"):
            match = re.search(r"(?<!\d)(1[0-2]|[1-9])月月历", context["text"].replace("\\", "/").split("/")[-1])
            if match:
                asset_months.add(match[1])
    def add(kind, value, label, confidence, evidence, **extra):
        key = (kind, value)
        tag = tags.setdefault(key, {"kind": kind, "value": value, "label": label,
                                   "confidence": confidence, "evidence": [], **extra})
        if evidence not in tag["evidence"]:
            tag["evidence"].append(evidence)
    for context in contexts:
        text = context["text"]
        evidence = {key: value for key, value in context.items() if key not in ("text", "own_path") and value}
        for name in characters(text):
            add("character", name, name, "explicit", evidence, aliases=CHARACTER_ALIASES[name])
        for source_type in sorted(source_types(text)):
            if own_source_types:
                if source_type not in source_types(context.get("own_path", "")):
                    continue
                add("source_type", source_type, source_type, "explicit", evidence, relation="asset_subfolder")
            else:
                add("source_type", source_type, source_type + " · 来源帖", "contextual", evidence, relation="source_collection")
        themes = re.findall(r"[「『]([^」』]+)[」』]", text)
        themes += re.findall(r"版本[-—]([^/]+)", text)
        for theme in themes:
            theme = theme.strip()
            if theme and len(theme) <= 80 and theme not in CHARACTER_ALIASES:
                add("theme", theme, theme, "explicit", evidence)
        if "月历" in text:
            years = re.findall(r"(20\d{2})年", text)
            filename = text.replace("\\", "/").split("/")[-1]
            months = re.findall(r"(?<!\d)(1[0-2]|[1-9])月.*月历|(?<!\d)(1[0-2]|[1-9])月月历", filename)
            month = next((a or b for a, b in months), None)
            if month is None:
                found = re.search(r"(20\d{2})年\s*(1[0-2]|[1-9])月", text)
                month = found[2] if found else None
            selected_months = asset_months or ({month} if month else set())
            for year in years:
                add("calendar", year + "年", year + "年", "explicit", evidence)
                for value_month in selected_months:
                    value = f"{year}-{int(value_month):02}"
                    add("calendar", value, value, "explicit", evidence)
            for value_month in selected_months:
                add("calendar", str(int(value_month)) + "月", str(int(value_month)) + "月", "explicit", evidence)
    explicit = {value for value in entry.get("versions", []) if value != "未分类"}
    for version in explicit:
        evidence = [{key: value for key, value in context.items() if key not in ("text", "own_path") and value}
                    for context in contexts if re.search(r"(?<![\d.])" + re.escape(version) + r"(?:\s*版本|(?=[^\d.]|$))", context["text"])]
        for source in evidence or [{"scope": "asset_metadata", "value": version}]:
            add("version", version, version_label(version), "explicit", source, relation="asset_version")
    supported = set()
    for version, evidence in debut_title_evidence(entry, contexts, CHARACTER_ALIASES, DEBUT):
        supported.add(version)
        kind = "version" if version in explicit else "associated_version"
        confidence = "explicit" if version in explicit else "rule_supported"
        add(kind, version, version_label(version), confidence, evidence, relation="debut_content")
    names = [value for kind, value in tags if kind == "character"]
    for name in names:
        mapping = DEBUT.get(name)
        if mapping and mapping["version"] not in explicit | supported:
            version = mapping["version"]
            add("associated_version", version, version_label(version, inferred=True), "inferred",
                {"scope": "character_debut", "character": name, "source_url": mapping["source_url"]},
                relation="character_debut")
    # A verified version title belongs on the same chip as its numeric version.
    # Other story/theme tags remain independent, even inside cumulative archives.
    version_names = set()
    for (kind, version), tag in list(tags.items()):
        if kind not in ("version", "associated_version") or version not in VERSION_NAMES:
            continue
        name, url = VERSION_NAMES[version]
        version_names.add(name)
        tag["version_name"] = name
        tag["aliases"] = [name]
        tag["evidence"].append({"scope": "version_name", "value": name, "source_url": url})
    for name in version_names:
        tags.pop(("theme", name), None)
    order = {kind: i for i, kind in enumerate(("character", "version", "associated_version", "theme", "source_type", "calendar"))}
    result = sorted(tags.values(), key=lambda tag: (order[tag["kind"]], tag["value"]))
    for tag in result:
        tag["evidence"].sort(key=lambda value: json.dumps(value, sort_keys=True))
    return result
