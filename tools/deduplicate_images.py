"""Recoverable current-tree image deduplication; never fetch or prune LFS/history."""
from __future__ import annotations
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".tif", ".tiff", ".svg", ".ico"}
POINTER = re.compile(rb"\Aversion https://git-lfs.github.com/spec/v1\r?\n(?:ext-[^\r\n]+\r?\n)*oid sha256:([0-9a-f]{64})\r?\nsize (0|[1-9][0-9]*)\r?\n?\Z")


def alias_file():
    return Path(os.getenv("MINAS_IMAGE_ALIASES_FILE", "metafiles/image-aliases.json"))


def load_aliases():
    path = alias_file()
    if not path.exists():
        return {"version": 1, "originals": {}, "previews": {}, "identities": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("Unsupported image aliases")
    return data


def resolve(path, mapping):
    seen = set()
    while path in mapping:
        if path in seen:
            raise ValueError("Cyclic image alias")
        seen.add(path)
        path = mapping[path]
    return path


def identity(data):
    match = POINTER.fullmatch(data)
    if match:
        return match[1].decode(), int(match[2]), True
    if data.startswith(b"version https://git-lfs.github.com/spec/"):
        raise ValueError("Malformed image LFS pointer")
    return hashlib.sha256(data).hexdigest(), len(data), False


def catalog():
    index = subprocess.check_output(["git", "ls-files", "--stage", "-z"])
    candidates = {}
    indexed = []
    for record in index.split(b"\0"):
        if not record:
            continue
        fields, raw_path = record.split(b"\t", 1)
        mode, oid, stage = fields.split()
        path = raw_path.decode("utf-8")
        if path.startswith(("downloads/", "docs/thumbs/")) and Path(path).suffix.lower() in IMAGE_EXTS:
            if stage != b"0":
                raise ValueError("Unmerged image in index")
            indexed.append((path, oid))
    checked = subprocess.run(["git", "cat-file", "--batch-check"],
                             input=b"".join(oid + b"\n" for _, oid in indexed),
                             capture_output=True, check=True).stdout.splitlines()
    for (path, oid), line in zip(indexed, checked):
        returned_oid, kind, size = line.split()
        if returned_oid != oid or kind != b"blob":
            raise ValueError("Unexpected indexed object")
        candidates[path] = (oid, int(size))
    for root in (Path("downloads"), Path("docs/thumbs")):
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
                candidates.setdefault(path.as_posix(), None)
    blob_cache = {}
    result = {}
    with subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        for name, entry in sorted(candidates.items()):
            path = Path(name)
            if path.is_symlink():
                raise ValueError("Symlink image")
            if path.is_file():
                with path.open("rb") as stream:
                    head = stream.read(1024)
                    if head.startswith(b"version https://git-lfs.github.com/spec/"):
                        sha, size, pointer = identity(head)
                    else:
                        stream.seek(0)
                        sha, size, pointer = hashlib.file_digest(stream, "sha256").hexdigest(), path.stat().st_size, False
            else:
                if entry is None:
                    continue
                oid, expected = entry
                if oid not in blob_cache:
                    process.stdin.write(oid + b"\n"); process.stdin.flush()
                    header = process.stdout.readline().split()
                    if len(header) != 3 or int(header[2]) != expected:
                        raise ValueError("Unexpected Git image response")
                    # Ordinary preview bytes are read from Git; LFS originals stay tiny pointers.
                    blob = process.stdout.read(expected); process.stdout.read(1)
                    blob_cache[oid] = identity(blob)
                sha, size, pointer = blob_cache[oid]
            result[name] = {"sha256": sha, "size": size, "pointer": pointer}
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("Unable to read image catalog")
    return result


def plan(entries, aliases=None):
    aliases = aliases or {"version": 1, "originals": {}, "previews": {}, "identities": {}}
    output = {"version": 1, "originals": dict(aliases["originals"]),
              "previews": dict(aliases["previews"]), "identities": {}}
    deletions = []
    stats = {}
    for kind, prefix in (("originals", "downloads/"), ("previews", "docs/thumbs/")):
        groups = defaultdict(list)
        subset = {path: item for path, item in entries.items() if path.startswith(prefix) and Path(path).suffix.lower() in IMAGE_EXTS}
        for path, item in subset.items():
            groups[item["sha256"]].append(path)
        for paths in groups.values():
            if len({entries[path]["size"] for path in paths}) != 1:
                raise ValueError("Inconsistent size for identical content identity")
            # Prefer verified materialized bytes when available, then lexical path order.
            canonical = min(paths, key=lambda path: (entries[path]["pointer"], path))
            for path in paths:
                output[kind].pop(path, None)
                if path != canonical:
                    output[kind][path] = canonical
                    deletions.append(path)
            if kind == "originals":
                output["identities"][canonical] = entries[canonical]["sha256"]
        for alias in output[kind]:
            output[kind][alias] = resolve(output[kind][alias], output[kind])
        retained = set(subset) - set(deletions)
        if {entries[path]["sha256"] for path in retained} != set(groups):
            raise ValueError("Deduplication would lose unique content")
        stats[kind] = {"before_count": len(subset), "after_count": len(retained),
                       "before_bytes": sum(item["size"] for item in subset.values()),
                       "after_bytes": sum(entries[path]["size"] for path in retained),
                       "unique_bytes": sum(subset[paths[0]]["size"] for paths in groups.values())}
    return output, sorted(deletions), stats


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def apply_plan(aliases, deletions, stage_sparse=False):
    root = Path.cwd().resolve()
    for name in deletions:
        path = Path(name)
        if (not name.startswith(("downloads/", "docs/thumbs/")) or path.suffix.lower() not in IMAGE_EXTS or
                not path.resolve().is_relative_to(root) or path.is_symlink()):
            raise ValueError("Unsafe duplicate deletion target")
        target = resolve(name, aliases["originals"] if name.startswith("downloads/") else aliases["previews"])
        if target == name or target in deletions:
            raise ValueError("Invalid canonical survivor")
    # Durable remapping first. Deletions affect only byte-identical verified images.
    write_json(alias_file(), aliases)
    state_path = Path(os.getenv("MINAS_FILE_STATE_FILE", "metafiles/minas-files.json"))
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for asset in state["assets"].values():
            old_paths = asset["paths"]
            history = asset.setdefault("historical_paths", [])
            for path in old_paths:
                if path not in history:
                    history.append(path)
            asset["paths"] = sorted({resolve(path, aliases["originals"]) for path in old_paths})
        write_json(state_path, state)
    for name in deletions:
        path = Path(name)
        if path.is_file():
            path.unlink()
    if stage_sparse and deletions:
        subprocess.run(["git", "update-index", "--force-remove", "-z", "--stdin"],
                       input="\0".join(deletions).encode("utf-8") + b"\0", check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply recoverable current-tree image deletions")
    parser.add_argument("--stage-sparse", action="store_true", help="Stage only these verified deletions for sparse Windows checkouts")
    args = parser.parse_args()
    entries = catalog()
    aliases, deletions, stats = plan(entries, load_aliases())
    print(json.dumps({"stats": stats, "deletions": len(deletions)}, indent=2))
    if args.apply:
        apply_plan(aliases, deletions, args.stage_sparse)


if __name__ == "__main__":
    main()
