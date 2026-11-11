#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Verify release assets, stage privately, then publish and read public bytes back."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from floe_electronics import pipeline, sources

REPOSITORY = "JiangNanGenius/floe-electronics-library"


def gh(*args: str, check: bool = True):
    result = subprocess.run(["gh", *args], text=True, capture_output=True, timeout=600)
    if check and result.returncode:
        raise sources.SourceError("GitHub operation failed: " + result.stderr.strip()[-2000:])
    return result


def verify_assets(directory: Path) -> dict:
    receipt = json.loads((directory / "release.json").read_bytes())
    if receipt.get("schemaVersion") != 1 or not re.fullmatch(r"data-v1-[0-9a-f]{20}", receipt.get("tag", "")):
        raise sources.SourceError("unknown release receipt")
    if not sources.SHA.fullmatch(receipt.get("producerRevision", "")):
        raise sources.SourceError("release producer must be an immutable source commit")
    expected = {}
    for item in receipt["assets"]:
        name = sources.safe_path(item["name"])
        if "/" in name or name in expected:
            raise sources.SourceError("duplicate or non-flat release asset name")
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item["bytes"] or pipeline.file_digest(path) != item["sha256"]:
            raise sources.SourceError("release asset changed: " + name)
        expected[name] = item["sha256"]
    expected["release.json"] = pipeline.file_digest(directory / "release.json")
    found = {}
    for line in (directory / "release-SHA256SUMS").read_text().splitlines():
        sha, separator, name = line.partition("  ")
        if not separator or name in found or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise sources.SourceError("malformed release checksums")
        found[sources.safe_path(name)] = sha
    if found != expected:
        raise sources.SourceError("release checksums differ")
    actual = {p.name for p in directory.iterdir() if p.is_file()}
    if actual != set(expected) | {"release-SHA256SUMS"}:
        raise sources.SourceError("unexpected staged release files")
    return receipt


def read_public(receipt: dict) -> None:
    metadata = json.loads(gh("api", "repos/" + REPOSITORY + "/releases/tags/" + receipt["tag"]).stdout)
    latest = json.loads(gh("api", "repos/" + REPOSITORY + "/releases/latest").stdout)
    if metadata["draft"] or metadata["prerelease"] or latest["tag_name"] != receipt["tag"]:
        raise sources.SourceError("public release/latest pointer differs")
    verify_tag(receipt)
    urls = {x["name"]: x["browser_download_url"] for x in metadata["assets"]}
    expected = {x["name"]: (x["bytes"], x["sha256"]) for x in receipt["assets"]}
    receipt_bytes = sources.canonical(receipt) + b"\n"
    expected["release.json"] = (len(receipt_bytes), sources.digest(receipt_bytes))
    checksum_bytes = "".join(f"{value[1]}  {name}\n" for name, value in sorted(expected.items())).encode()
    expected["release-SHA256SUMS"] = (len(checksum_bytes), sources.digest(checksum_bytes))
    if set(urls) != set(expected):
        raise sources.SourceError("remote release asset set differs")
    # Public GETs intentionally carry no account token. SHA/size prove actual assets.
    for name, (size, sha) in sorted(expected.items()):
        request = urllib.request.Request(urls[name], headers={"User-Agent": "floe-electronics-public-readback"})
        calculated = hashlib.sha256()
        observed = 0
        with urllib.request.urlopen(request, timeout=90) as response:
            while block := response.read(1024 * 1024):
                observed += len(block)
                if size is not None and observed > size:
                    raise sources.SourceError("remote asset exceeds expected size")
                calculated.update(block)
        if (size is not None and size != observed) or calculated.hexdigest() != sha:
            raise sources.SourceError("public asset digest differs: " + name)
    print(json.dumps({"publicReadBack": receipt["tag"], "verifiedAssets": len(expected)}))


def verify_tag(receipt: dict) -> None:
    target = json.loads(gh("api", "repos/" + REPOSITORY + "/git/ref/tags/" + receipt["tag"]).stdout)["object"]
    for _ in range(4):
        if target["type"] == "commit":
            break
        if target["type"] != "tag":
            raise sources.SourceError("release tag does not resolve to a commit")
        target = json.loads(gh("api", "repos/" + REPOSITORY + "/git/tags/" + target["sha"]).stdout)["object"]
    if target["type"] != "commit" or target["sha"] != receipt["producerRevision"]:
        raise sources.SourceError("release tag/source binding differs")


def ensure_candidate_tag(receipt: dict) -> None:
    """Pin a new candidate before creating a draft; never move an existing ref."""
    lookup = gh("api", "repos/" + REPOSITORY + "/git/ref/tags/" + receipt["tag"], check=False)
    if lookup.returncode == 0:
        verify_tag(receipt)
        return
    try:
        failure = json.loads(lookup.stdout)
    except (ValueError, TypeError):
        raise sources.SourceError("cannot establish candidate tag state") from None
    if str(failure.get("status")) != "404":
        raise sources.SourceError("cannot establish candidate tag state")
    gh("api", "--method", "POST", "repos/" + REPOSITORY + "/git/refs",
       "-f", "ref=refs/tags/" + receipt["tag"], "-f", "sha=" + receipt["producerRevision"])
    verify_tag(receipt)


def publish(directory: Path) -> None:
    receipt = verify_assets(directory)
    revision = os.environ.get("GITHUB_SHA", "")
    if receipt["producerRevision"] != revision or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise sources.SourceError("release is not bound to the producing workflow source")
    tag = receipt["tag"]
    listing = json.loads(gh("api", "repos/" + REPOSITORY + "/releases?per_page=100").stdout)
    existing = next((x for x in listing if x["tag_name"] == tag), None)
    if existing and not existing["draft"]:
        read_public(receipt)
        return
    if existing and existing.get("target_commitish") != revision:
        raise sources.SourceError("existing draft targets a different producer")
    ensure_candidate_tag(receipt)
    notes = directory.parent / "release-notes.md"
    catalog = json.loads((directory / "catalog.json").read_bytes())
    counts = sum(s["recordCount"] for s in catalog["sources"])
    notes.write_text(f"Versioned 2D component-library exchange data: {len(catalog['sources'])} upstream libraries, {counts} records.\n\n"
                     "Includes the complete 2D download and individual upstream packages, source originals, licenses, immutable upstream pins and SHA-256 manifests. "
                     "Compatibility status is explicit per record. 3D references are retained; model binaries are excluded. "
                     "Exchange files are separate from native Floe runtime packages; conversion status does not assert native placement or 3D rendering.\n\n"
                     f"Producer source: `{revision}`. Data: `{receipt['datasetSHA256']}`.\n")
    present = set()
    if existing:
        verify_tag(receipt)
        present = {item["name"] for item in existing["assets"]}
        local = {p.name for p in directory.iterdir() if p.is_file()}
        if not present.issubset(local):
            raise sources.SourceError("existing draft contains unexpected assets")
        if present:
            with tempfile.TemporaryDirectory(prefix="floe-library-resume-") as temp:
                gh("release", "download", tag, "--repo", REPOSITORY, "--dir", temp)
                for name in present:
                    if pipeline.file_digest(Path(temp) / name) != pipeline.file_digest(directory / name):
                        raise sources.SourceError("existing draft bytes differ; preserve and investigate")
    else:
        gh("release", "create", tag, "--repo", REPOSITORY, "--target", revision,
           "--title", "Electronics 2D library " + receipt["dataVersion"], "--notes-file", str(notes), "--draft")
    # Upload once; no --clobber or mutation of an existing dataset is permitted.
    names = sorted(str(p) for p in directory.iterdir() if p.is_file() and p.name not in present)
    for offset in range(0, len(names), 16):
        gh("release", "upload", tag, *names[offset:offset + 16], "--repo", REPOSITORY)
    with tempfile.TemporaryDirectory(prefix="floe-library-readback-") as temp:
        gh("release", "download", tag, "--repo", REPOSITORY, "--dir", temp)
        remote = verify_assets(Path(temp))
        if remote != receipt:
            raise sources.SourceError("uploaded candidate differs")
    gh("release", "edit", tag, "--repo", REPOSITORY, "--draft=false", "--latest")
    read_public(receipt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify", "publish", "read-back"])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "publish":
        publish(args.directory)
    elif args.command == "read-back":
        read_public(verify_assets(args.directory))
    else:
        print(json.dumps({"verified": verify_assets(args.directory)["tag"]}))
