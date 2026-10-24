"""Explicitly build legacy Pages and verify its exact data commit."""
from __future__ import annotations
import argparse
import os
import time
import requests


def publish(session, repository, commit, timeout=600, sleep=time.sleep, clock=time.monotonic):
    base = "https://api.github.com/repos/" + repository
    def get(path):
        response = session.get(base + path, timeout=30)
        response.raise_for_status()
        return response.json()
    if get("/git/ref/heads/main")["object"]["sha"] != commit:
        raise RuntimeError("Main advanced before Pages publication")
    response = session.get(base + "/pages/builds/latest", timeout=30)
    if response.status_code == 404:
        latest = {}
    else:
        response.raise_for_status()
        latest = response.json()
    if latest.get("commit") == commit and latest.get("status") == "built":
        return "already_built"
    if latest.get("commit") != commit or latest.get("status") not in ("queued", "building"):
        response = session.post(base + "/pages/builds", timeout=30)
        response.raise_for_status()
    deadline = clock() + timeout
    while clock() < deadline:
        latest = get("/pages/builds/latest")
        if latest.get("commit") == commit:
            if latest.get("status") == "built":
                return "built"
            if latest.get("status") == "errored":
                raise RuntimeError("Pages build failed")
        sleep(5)
    raise TimeoutError("Pages publication was not confirmed before the deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    with requests.Session() as session:
        session.headers.update({"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                                "Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28"})
        print("Pages:", publish(session, os.environ["GITHUB_REPOSITORY"], args.commit))


if __name__ == "__main__":
    main()
