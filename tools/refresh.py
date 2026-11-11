#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Scheduled source polling; no-change runs publish nothing."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from floe_electronics import pipeline, sources

REPOSITORY = "JiangNanGenius/floe-electronics-library"


def download_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "floe-electronics-library-ci", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise sources.SourceError("upstream publication metadata is oversized")
    return json.loads(data)


def previous_release() -> tuple[dict, dict] | None:
    try:
        release = download_json("https://api.github.com/repos/" + REPOSITORY + "/releases/latest")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    urls = {x["name"]: x["browser_download_url"] for x in release["assets"]}
    if not {"catalog.json", "upstreams.lock.json"}.issubset(urls):
        raise sources.SourceError("existing release is missing its catalog or lock")
    return download_json(urls["catalog.json"]), download_json(urls["upstreams.lock.json"])


def main() -> None:
    if os.environ.get("GITHUB_REPOSITORY", REPOSITORY) != REPOSITORY:
        raise sources.SourceError("publication workflow is bound to its own repository")
    revision = os.environ.get("GITHUB_SHA", "")
    if not sources.SHA.fullmatch(revision):
        raise sources.SourceError("refresh requires an immutable producer commit")
    policy = sources.load_policy(ROOT / "upstreams.json")
    lock = sources.resolve(policy)
    pipeline.write_json(ROOT / "candidate.lock.json", lock)
    previous = previous_release()
    fingerprint = pipeline.tooling_digest(ROOT)
    if previous and previous[1] == lock and previous[0].get("toolingSHA256") == fingerprint and previous[0].get("producerRevision") == revision:
        print("All approved upstreams and producer bytes are unchanged; no publication.")
        if output := os.environ.get("GITHUB_OUTPUT"):
            with open(output, "a") as stream:
                stream.write("publish=false\n")
        return
    current = pipeline.build(policy, lock, ROOT, ROOT / ".cache/upstreams", ROOT / "dist", revision)
    if previous:
        pipeline.compare_baseline(previous[0], current)
    result = pipeline.bundle(ROOT / "dist", ROOT / "release")
    print(json.dumps(result))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as stream:
            stream.write("publish=true\ntag=" + result["tag"] + "\n")


if __name__ == "__main__":
    main()
