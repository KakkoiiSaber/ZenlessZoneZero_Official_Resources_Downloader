"""Bounded, staged asset IO. No destination is changed until ZIP validation succeeds."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import time
import zipfile


def limit(name, default):
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def check_space(path, required=0):
    reserve = limit("MINAS_MIN_FREE_BYTES", 1024 ** 3)
    if shutil.disk_usage(path).free < required + reserve:
        raise ValueError("Insufficient free disk space (including reserve)")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def fingerprint(row):
    # Include source metadata/revision, but never persist plaintext passwords.
    normalized = {k: str(v or "").strip() for k, v in row.items()}
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def download_zip(session, url, dest):
    return download_bounded(session, url, dest, limit("MINAS_MAX_ARCHIVE_BYTES", 512 * 1024 ** 2))


def download_bounded(session, url, dest, maximum, expected_size=None, consume=None, deadline=None):
    check_space(dest.parent)
    created = False
    deadline = min(deadline if deadline is not None else float('inf'),
                   time.monotonic() + limit("MINAS_DOWNLOAD_MAX_SECONDS", 300))
    try:
        with session.get(url, stream=True, timeout=(30, 30),
                         headers={"Accept-Encoding": "identity"}) as response:
            response.raise_for_status()
            length_header = response.headers.get("Content-Length")
            length = int(length_header) if length_header is not None else None
            if length is not None and (length < 0 or length > maximum):
                raise ValueError("Download exceeds size limit")
            if expected_size is not None and length is not None and length != expected_size:
                raise ValueError("Remote size changed during scan")
            if expected_size is not None and "text/html" in response.headers.get("Content-Type", "").lower():
                raise ValueError("Unexpected HTML instead of asset")
            check_space(dest.parent, length or expected_size or 0)
            total = 0
            digest = hashlib.sha256()
            with dest.open("xb") as stream:
                created = True
                for chunk in response.iter_content(64 * 1024):
                    if time.monotonic() > deadline:
                        raise ValueError("Download duration limit exceeded")
                    total += len(chunk)
                    if consume is not None:
                        consume(len(chunk))
                    if total > maximum or (expected_size is not None and total > expected_size):
                        raise ValueError("Streamed download exceeds size limit")
                    check_space(dest.parent, len(chunk))
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if expected_size is not None and total != expected_size:
                raise ValueError("Incomplete asset download")
            if length is not None and not response.headers.get("Content-Encoding") and total != length:
                raise ValueError("Incomplete download")
            return {"sha256": digest.hexdigest(), "bytes": total, "etag": response.headers.get("ETag")}
    except BaseException:
        if created:
            dest.unlink(missing_ok=True)
        raise


def extract_zip(archive, stage):
    maximum = limit("MINAS_MAX_EXTRACTED_BYTES", 2 * 1024 ** 3)
    per_file = limit("MINAS_MAX_FILE_BYTES", 256 * 1024 ** 2)
    count = limit("MINAS_MAX_FILES", 5000)
    if archive.stat().st_size > limit("MINAS_MAX_ARCHIVE_BYTES", 512 * 1024 ** 2):
        raise ValueError("Archive exceeds size limit")
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > count:
            raise ValueError("Too many archive entries")
        files = []
        seen = set()
        for info in entries:
            name = info.filename
            parts = PurePosixPath(name).parts
            if (not parts or "\\" in name or name.startswith("/") or
                    any(p in ("..", ".") or ":" in p for p in parts) or
                    stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError("Unsafe ZIP path or symlink")
            if info.is_dir():
                continue
            if info.file_size > per_file:
                raise ValueError("Extracted file exceeds size limit")
            key = name.casefold()
            if key in seen:
                raise ValueError("Duplicate archive path")
            seen.add(key)
            files.append((info, parts))
        if not files:
            raise ValueError("Empty archive")
        if sum(info.file_size for info, _ in files) > maximum:
            raise ValueError("Extracted archive exceeds size limit")
        check_space(stage, sum(info.file_size for info, _ in files))
        roots = {parts[0] for _, parts in files}
        flatten = len(roots) == 1 and all(len(parts) > 1 for _, parts in files)
        total = 0
        for info, parts in files:
            target = stage.joinpath(*(parts[1:] if flatten else parts))
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with zipped.open(info) as source, target.open("xb") as dest:
                while chunk := source.read(64 * 1024):
                    written += len(chunk)
                    total += len(chunk)
                    if written > per_file or total > maximum:
                        raise ValueError("Actual extracted size exceeds limit")
                    check_space(stage, len(chunk))
                    dest.write(chunk)
            if written != info.file_size:
                raise ValueError("Incomplete extraction")


def install_tree(stage, target):
    """Merge new/changed files, retaining unrelated assets; roll back IO failures."""
    if any(parent.is_symlink() for parent in (target, *target.parents)):
        raise ValueError("Symlink destination")
    target.mkdir(parents=True, exist_ok=True)
    installed = []
    backups = []
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=target.parent) as backup_dir:
        try:
            for source in sorted(stage.rglob("*")):
                if not source.is_file():
                    continue
                rel = source.relative_to(stage)
                dest = target / rel
                if any(parent.is_symlink() for parent in (dest, *dest.parents)):
                    raise ValueError("Symlink destination")
                if dest.exists() and dest.is_file():
                    with source.open("rb") as a, dest.open("rb") as b:
                        if hashlib.file_digest(a, "sha256").digest() == hashlib.file_digest(b, "sha256").digest():
                            continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    if not dest.is_file() or dest.is_symlink():
                        raise ValueError("Destination is not a regular file")
                    backup = Path(backup_dir) / rel
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(dest, backup)
                    backups.append((backup, dest))
                os.replace(source, dest)
                installed.append(dest)
        except BaseException:
            for dest in reversed(installed):
                dest.unlink(missing_ok=True)
            for backup, dest in reversed(backups):
                os.replace(backup, dest)
            raise
