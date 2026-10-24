"""Commit verified update output; preserve a recovery branch if main advanced."""
from __future__ import annotations
import argparse
import csv
import io
import json
import os
from pathlib import Path
import re
import subprocess

SCOPES = ("downloads", "metafiles", "docs/meta.json", "docs/cached.json", "docs/thumbs")


def git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=repo, text=True, encoding="utf-8",
                          capture_output=True, check=check,
                          env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"})


def checkpoint(repo, allow_main, run_key):
    repo = Path(repo)
    if not re.fullmatch(r"\d+-\d+", run_key):
        raise ValueError("Invalid recovery run identity")
    if git(repo, "diff", "--cached", "--name-only").stdout.strip():
        raise RuntimeError("Refusing to include pre-existing staged changes")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    scopes = [p for p in SCOPES if (repo / p).exists() or git(repo, "ls-files", "--", p).stdout]
    if scopes:
        git(repo, "add", "-A", "--", *scopes)
    if git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return {"mode": "unchanged", "commit": base, "branch": "main"}
    # Invalid metadata can be retained for diagnosis, but never published to main.
    for path in (repo / "metafiles").glob("*.json"):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            allow_main = False
    for path in (repo / "metafiles").glob("*.csv"):
        relative = path.relative_to(repo).as_posix()
        old = git(repo, "show", base + ":" + relative, check=False)
        if old.returncode == 0:
            prior = list(csv.DictReader(io.StringIO(old.stdout)))
            current = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
            old_urls = {row.get("post_url") for row in prior if row.get("post_url")}
            new_urls = {row.get("post_url") for row in current if row.get("post_url")}
            if not old_urls <= new_urls:
                allow_main = False
    git(repo, "-c", "user.name=github-actions[bot]", "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", "chore: checkpoint incremental asset update")
    commit = git(repo, "rev-parse", "HEAD").stdout.strip()
    remote = git(repo, "ls-remote", "origin", "refs/heads/main").stdout.split()
    if allow_main and remote and remote[0] == base:
        pushed = git(repo, "push", "origin", "HEAD:refs/heads/main", check=False)
        # Check uncertain network errors before attempting recovery.
        remote = git(repo, "ls-remote", "origin", "refs/heads/main").stdout.split()
        if remote and remote[0] == commit:
            return {"mode": "main", "commit": commit, "branch": "main"}
        if pushed.returncode == 0:
            raise RuntimeError("Push succeeded but remote commit could not be verified")
    branch = "asset-recovery/" + run_key
    git(repo, "push", "origin", "HEAD:refs/heads/" + branch)
    remote = git(repo, "ls-remote", "origin", "refs/heads/" + branch).stdout.split()
    if not remote or remote[0] != commit:
        raise RuntimeError("Recovery commit could not be verified")
    return {"mode": "recovery", "commit": commit, "branch": branch}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-main", action="store_true")
    args = parser.parse_args()
    result = checkpoint(Path.cwd(), args.allow_main,
                        os.environ["GITHUB_RUN_ID"] + "-" + os.getenv("GITHUB_RUN_ATTEMPT", "1"))
    print(json.dumps(result))
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            for key, value in result.items():
                stream.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
