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

CATEGORY_ORDER = ["角色图片", "月历", "活动壁纸", "纪念与节日", "测试期素材", "表情包", "影像档案", "未分类"]
from src.metadata.knowledge import load_knowledge, tables

_KNOWLEDGE = load_knowledge()
CHARACTER_ALIASES, DEBUT, _VERSION_TABLE = tables(_KNOWLEDGE)
VERSION_NAMES.clear()
VERSION_NAMES.update(_VERSION_TABLE)


def configure_knowledge(data):
    global _KNOWLEDGE
    _KNOWLEDGE = data
    chars, debuts, versions = tables(data)
    CHARACTER_ALIASES.clear(); CHARACTER_ALIASES.update(chars)
    DEBUT.clear(); DEBUT.update(debuts)
    VERSION_NAMES.clear(); VERSION_NAMES.update(versions)


def characters(text):
    # Keep full forms distinct and prevent short aliases such as 本/照/简 from
    # matching ordinary words like 版本/照片/简单. Longest overlapping alias wins.
    candidates = []
    normalized = re.sub(r"[\s「」『』“”]", "", text)
    for name, aliases in CHARACTER_ALIASES.items():
        for alias in aliases:
            alias = re.sub(r"[\s「」『』“”]", "", alias)
            if not alias:
                continue
            for match in re.finditer(re.escape(alias), normalized):
                if len(alias) == 1:
                    before, after = normalized[:match.start()], normalized[match.end():]
                    left_ok = not before or bool(re.search(r"(?:代理人|角色|[|丨/&:：])$", before))
                    right_ok = not after or bool(re.match(r"(?:角色|主题|EP|MV|PV|好感|生日|[./&])", after))
                    if not (left_ok and right_ok):
                        continue
                candidates.append((match.start(), match.end(), name))
    selected = []
    for start, end, name in sorted(candidates, key=lambda value: (-(value[1]-value[0]), value[0], value[2])):
        if not any(start >= a and end <= b for a, b, _ in selected):
            selected.append((start, end, name))
    return sorted({name for _, _, name in selected})


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
        elif any(term in text for term in ("周年", "生日", "节日")):
            result.add("纪念与节日")
        elif characters(text) or any(group in text for group in _KNOWLEDGE["groups"]):
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
        for group in _KNOWLEDGE['groups']:
            if group and group in text:
                add('group', group, group, 'explicit', evidence, relation='collection_context')
        for term in ('周年', '生日', '动态壁纸', '好感壁纸'):
            if term in text:
                add('theme', term, term, 'explicit', evidence)
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
    order = {kind: i for i, kind in enumerate(("character", "group", "version", "associated_version", "theme", "source_type", "calendar"))}
    result = sorted(tags.values(), key=lambda tag: (order[tag["kind"]], tag["value"]))
    for tag in result:
        tag["evidence"].sort(key=lambda value: json.dumps(value, sort_keys=True))
    return result
