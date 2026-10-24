"""Conservative debut wallpaper rules and verified official version names.

Rule provenance and accepted/rejected examples are documented in README.md.
These rules describe the content's debut association, never its upload date.
"""
from __future__ import annotations
import re
from urllib.parse import urlparse

VERSION_NAMES = {
    "1.1": ("卧底蓝调", "https://zenless.hoyoverse.com/zh-cn/news/125244"),
    "1.2": ("火狱骑行", "https://zenless.hoyoverse.com/zh-cn/news/126054"),
    "1.3": ("虚拟杀机", "https://zenless.hoyoverse.com/zh-cn/news/126739"),
    "1.4": ("星流霆击", "https://zenless.hoyoverse.com/zh-cn/news/127445"),
    "1.5": ("闪耀的此刻", "https://zenless.hoyoverse.com/zh-cn/news/129572"),
    "1.6": ("在被遗忘的废墟之上", "https://zenless.hoyoverse.com/zh-cn/news/154662"),
    "1.7": ("将眼泪与过往一同埋葬", "https://zenless.hoyoverse.com/zh-cn/news/155458"),
    "2.0": ("云霞同归处", "https://zenless.hoyoverse.com/zh-cn/news/156753"),
    "2.1": ("迟吟的浪涌", "https://zenless.hoyoverse.com/zh-cn/news/157696"),
    "2.2": ("不要温和地走入那良夜", "https://zenless.hoyoverse.com/zh-cn/news/159176"),
    "2.3": ("可曾记得梦", "https://zzz.mihoyo.com/news/160252"),
}
DEBUT_TITLE_RULE = "official-wallpaper-debut-v1"
ROLE_TYPES = {"角色PV", "角色展示"}
OTHER_TYPES = {"EP", "MV"}


def version_label(version, *, inferred=False):
    name = VERSION_NAMES.get(version, ("", ""))[0]
    label = ("关联版本 " if inferred else "版本 ") + version
    if name:
        label += " · " + name
    return label + (" · 推断" if inferred else "")


def normalized(text):
    return re.sub(r'[\s「」“”\"‘’]', '', text)


def debut_title_evidence(entry, contexts, aliases, debut):
    """Require a structured official-source post plus matching per-image paths.

    A role-only bundle is sufficient; mixed EP/MV bundles require an unambiguous
    role subfolder. Bare PV, rerun/review titles and arbitrary character mentions
    are deliberately outside this rule. Each association is evaluated separately.
    """
    by_alias = {normalized(alias): name for name, values in aliases.items() for alias in values}
    alias_pattern = "(?:" + "|".join(re.escape(x) for x in sorted(by_alias, key=len, reverse=True)) + ")"
    name_pattern = alias_pattern + "(?:&" + alias_pattern + ")*"
    type_pattern = r"(?:角色PV|角色展示|EP|MV)(?:&(?:角色PV|角色展示|EP|MV))*"
    title_pattern = re.compile(r"壁纸分享[|丨](?P<names>" + name_pattern + r")(?P<types>" + type_pattern + r"|主题)壁纸", re.I)
    own_pattern = re.compile(r"(?:(?P<names>" + name_pattern + r"))?(?P<types>角色PV|角色展示)(?:&(?:角色PV|角色展示))*", re.I)
    results = []
    for post in entry.get("posts", []):
        title, url = post.get("title", ""), post.get("post_url", "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"www.miyoushe.com", "miyoushe.com"} or not re.fullmatch(r"/zzz/article/\d+/?", parsed.path):
            continue
        matched = title_pattern.fullmatch(normalized(title))
        if not matched:
            continue
        names = [by_alias[name] for name in matched["names"].split("&")]
        declared_types = set(matched["types"].upper().split("&"))
        role_bundle = bool(declared_types & ROLE_TYPES) and not (declared_types & OTHER_TYPES)
        for context in contexts:
            asset_bound = context["scope"] == "asset_path" and normalized(title) in normalized(context["value"])
            source_bound = (bool(post.get("share")) and context["scope"] == "source_path" and context.get("source_url") == post.get("share")
                            and context["value"] == post.get("remote_path"))
            if not asset_bound and not source_bound:
                continue
            own = context.get("own_path", "")
            # An EP/MV folder cannot inherit a role type from the enclosing post.
            if re.search(r"(?<![a-zA-Z])(?:EP|MV)(?![a-zA-Z])", own, re.I):
                continue
            own_roles = []
            for part in own.replace("\\", "/").split("/")[:-1]:
                part = re.sub(r"[ _-]*\d{3,5}[x×*-]\d{3,5}$", "", part, flags=re.I)
                match = own_pattern.fullmatch(normalized(part))
                if match:
                    own_names = [by_alias[name] for name in match["names"].split("&")] if match["names"] else names
                    own_roles.extend(name for name in own_names if name in names)
            # Source labels in an unrecognised folder are insufficient for a
            # whole-bundle promotion: a stricter directory/title rule must match.
            has_other_context = any(word in own for word in ("复刻", "返场", "回顾", "混剪", "二创", "同人"))
            if has_other_context:
                continue
            if not own_roles and asset_bound and role_bundle and not re.search(r"角色PV|角色展示|(?<![a-zA-Z])PV(?![a-zA-Z])", own, re.I):
                own_roles = names
            for name in sorted(set(own_roles)):
                mapping = debut.get(name)
                if not mapping:
                    continue
                evidence = {"scope": "debut_title_rule", "rule_id": DEBUT_TITLE_RULE,
                            "character": name, "value": title, "asset_path" if asset_bound else "source_path": context["value"],
                            "source_url": url, "mapping_source_url": mapping["source_url"]}
                pair = (mapping["version"], evidence)
                if pair not in results:
                    results.append(pair)
    return results
