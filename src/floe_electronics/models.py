# SPDX-License-Identifier: MPL-2.0
"""Index model Git objects without downloading model binaries.

Consumers verify the exact Git blob (header + bytes) against gitBlobSHA1, then
record SHA-256 for their own cache. This is source-object provenance, not a
claim that CI already computed a model's SHA-256 or validated its rendering.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import urllib.parse
import urllib.request

from .sources import (MODEL_SUFFIXES, SHA, SourceError, canonical, digest, git,
                      license_text, safe_path)

MAX_MODELS = 50_000
MAX_METADATA_BYTES = 16 * 1024 * 1024


def http_bytes(url: str, limit: int) -> tuple[bytes, dict]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "gitlab.com" or parts.username or parts.password:
        raise SourceError("model metadata requires the fixed official GitLab origin")
    request = urllib.request.Request(url, headers={"User-Agent": "FloeElectronicsLibrary/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != "gitlab.com":
            raise SourceError("model metadata redirected to a different authority")
        data = response.read(limit + 1)
        headers = dict(response.headers.items())
    if len(data) > limit:
        raise SourceError("model metadata exceeds byte budget")
    return data, headers


def record(source_id: str, repository: str, revision: str, path: str,
           oid: str, license_source_id: str) -> dict:
    safe_path(path)
    if not SHA.fullmatch(oid) or not SHA.fullmatch(revision):
        raise SourceError("model source object identity is not canonical")
    suffix = Path(path).suffix.lower()
    if suffix not in MODEL_SUFFIXES:
        raise SourceError("unsupported model file type")
    return {"schemaVersion": 1, "id": "model:" + source_id + ":" + path,
            "kind": "model3d", "sourceID": source_id,
            "repository": repository.removesuffix(".git"), "revision": revision,
            "path": path, "gitBlobSHA1": oid, "format": suffix[1:],
            "licenseSourceID": license_source_id,
            "verification": "git-source-object; SHA-256 computed after download",
            "modelBinaryBundled": False}


def git_models(source: dict, root: Path):
    # No '-l': querying blob sizes in a partial clone would fetch the blobs.
    tree = git(["ls-tree", "-rz", "--full-tree", source["commit"]], cwd=root)
    for entry in tree.split("\x00"):
        if not entry:
            continue
        header, separator, path = entry.partition("\t")
        if not separator:
            raise SourceError("malformed Git model tree")
        mode, kind, oid = header.split(" ")
        if Path(path).suffix.lower() not in MODEL_SUFFIXES:
            continue
        if mode not in ("100644", "100755") or kind != "blob":
            raise SourceError("model is not a regular Git blob")
        yield record(source["id"], source["repository"], source["commit"], path, oid, source["id"])


def gitlab_models(source: dict, cache: Path):
    repository = source["repository"].removesuffix(".git")
    parts = urllib.parse.urlsplit(repository)
    if parts.scheme != "https" or parts.hostname != "gitlab.com" or not SHA.fullmatch(source["commit"]):
        raise SourceError("invalid official GitLab model source")
    namespace = parts.path.strip("/")
    project = urllib.parse.quote(namespace, safe="")
    prefix = "https://gitlab.com/api/v4/projects/" + project + "/repository/tree?"
    license_url = repository + "/-/raw/" + source["commit"] + "/" + source["license"]["evidencePath"]
    data, _ = http_bytes(license_url, 1024 * 1024)
    license_hash = digest(license_text(data, source["license"].get("extract", "full")))
    if license_hash != source["license"]["approvedTextSHA256"]:
        raise SourceError("3D model source license changed")
    cache_path = cache / "model-trees" / (source["id"] + "-" + source["commit"] + ".json")
    if cache_path.is_file():
        if cache_path.stat().st_size > MAX_METADATA_BYTES:
            raise SourceError("cached model metadata exceeds budget")
        cached = json.loads(cache_path.read_bytes())
        rows = cached["rows"]
        if cached.get("commit") != source["commit"] or cached.get("rowsSHA256") != digest(canonical(rows)):
            raise SourceError("cached model tree identity changed")
    else:
        def page(number: int):
            url = prefix + urllib.parse.urlencode({"ref": source["commit"], "recursive": "true", "per_page": 100, "page": number})
            payload, headers = http_bytes(url, 2 * 1024 * 1024)
            values = json.loads(payload)
            if not isinstance(values, list) or len(values) > 100:
                raise SourceError("malformed GitLab model tree page")
            return values, {k.lower(): v for k, v in headers.items()}
        first, headers = page(1)
        total = int(headers.get("x-total-pages", "0"))
        if not 1 <= total <= 500:
            raise SourceError("GitLab model tree pagination is unavailable or excessive")
        with ThreadPoolExecutor(max_workers=4) as pool:
            remaining = list(pool.map(page, range(2, total + 1)))
        pages = [(first, headers), *remaining]
        for number, (values, page_headers) in enumerate(pages, 1):
            if number < total and len(values) != 100:
                raise SourceError("GitLab tree pagination omitted entries")
            if page_headers.get("x-page", str(number)) != str(number):
                raise SourceError("GitLab tree returned a different page")
        rows = first + [value for values, _ in remaining for value in values]
        if "x-total" in headers and len(rows) != int(headers["x-total"]):
            raise SourceError("GitLab tree count differs from its complete inventory")
        seen = set()
        for item in rows:
            path = safe_path(item["path"])
            if path in seen or not SHA.fullmatch(item["id"]):
                raise SourceError("duplicated or malformed GitLab tree object")
            seen.add(path)
        if len(rows) > MAX_MODELS + 2000:
            raise SourceError("GitLab model tree entry budget exceeded")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        pending = cache_path.with_suffix(".pending")
        pending.write_bytes(canonical({"commit": source["commit"], "rowsSHA256": digest(canonical(rows)), "rows": rows}))
        pending.replace(cache_path)
    for item in sorted(rows, key=lambda value: value["path"]):
        if item["type"] != "blob" or Path(item["path"]).suffix.lower() not in MODEL_SUFFIXES:
            continue
        if item["mode"] not in ("100644", "100755"):
            raise SourceError("model is not a regular GitLab blob")
        yield record(source["id"], source["repository"], source["commit"], item["path"], item["id"], source["licenseSourceID"])


def collect(lock: dict, roots: dict[str, Path], cache: Path):
    rows = []
    for source in lock["sources"]:
        rows.extend(git_models(source, roots[source["id"]]))
        if len(rows) > MAX_MODELS:
            raise SourceError("model inventory exceeds record budget")
    for source in lock.get("modelSources", []):
        rows.extend(gitlab_models(source, cache))
        if len(rows) > MAX_MODELS:
            raise SourceError("model inventory exceeds record budget")
    seen = set()
    for row in sorted(rows, key=lambda value: value["id"]):
        if row["id"] in seen:
            raise SourceError("duplicate 3D model asset identity")
        seen.add(row["id"])
        yield row
