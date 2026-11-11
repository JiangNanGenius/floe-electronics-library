# SPDX-License-Identifier: MPL-2.0
"""Deterministic, independently verified 2D-only content-addressed data packages."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import zipfile

from .sources import (DATA_SUFFIXES, MODEL_SUFFIXES, SourceError, canonical, check_license,
                      digest, materialize, safe_path, source_files, verify_lock)

CHUNK_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 250_000
RECORD_KINDS = {"symbol", "footprint", "component"}
RECORD_STATUSES = {"converted", "reviewRequired", "unsupported"}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")


@contextmanager
def gzip_writer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0, compresslevel=9) as zipped:
            yield zipped


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tooling_digest(root: Path) -> str:
    files = [root / "upstreams.json"]
    for directory in ("src", "tools", "schemas", "licenses", ".github/workflows"):
        files.extend(p for p in (root / directory).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    pairs = [{"path": p.relative_to(root).as_posix(), "sha256": file_digest(p)} for p in sorted(files)]
    return digest(canonical(pairs))


def verify_record(record: dict, source_id: str, files: dict[str, dict]) -> bytes:
    if record.get("schemaVersion") != 1 or record.get("kind") not in RECORD_KINDS:
        raise SourceError("unknown normalized record schema/kind")
    if record.get("status") not in RECORD_STATUSES or record.get("units") != "mm":
        raise SourceError("normalized record has no explicit compatibility/units")
    if not isinstance(record.get("id"), str) or not record["id"] or len(record["id"].encode()) > 8192:
        raise SourceError("invalid normalized record identity")
    if not isinstance(record.get("name"), str) or not isinstance(record.get("issues"), list):
        raise SourceError("normalized record is missing its name/issues")
    origin = record.get("source", {})
    path = safe_path(origin.get("path", ""))
    if path not in files or origin.get("sha256") != files[path]["sha256"]:
        raise SourceError("normalized record has an unbound original source: " + source_id + "/" + path)
    data = canonical(record) + b"\n"
    if len(data) > MAX_RECORD_BYTES:
        raise SourceError("normalized record byte budget exceeded")
    return data


def zip_sources(path: Path, root: Path, files: dict[str, dict], license_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        entries = [("originals/" + name, root / name) for name in files]
        entries += [("licenses/" + p.name, p) for p in sorted(license_root.glob("*.txt"))]
        for name, original in sorted(entries):
            item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            item.create_system = 3
            with archive.open(item, "w", force_zip64=True) as destination, original.open("rb") as source:
                shutil.copyfileobj(source, destination, length=1024 * 1024)


def expected_record_file(path: str) -> bool:
    p = Path(path)
    return p.suffix in {".kicad_sym", ".kicad_mod"} or p.name in {"symbol.lp", "package.lp", "component.lp", "device.lp"}


def build(policy: dict, lock: dict, repository_root: Path, cache: Path, output: Path,
          producer_revision: str, *, roots: dict[str, Path] | None = None,
          normalizer=None) -> dict:
    verify_lock(policy, lock)
    if normalizer is None:
        from .normalizers import normalize as normalizer
    if output.exists() and any(output.iterdir()):
        raise SourceError("output must be empty; retained artifacts are never overwritten")
    output.mkdir(parents=True, exist_ok=True)
    fingerprint = tooling_digest(repository_root)
    dataset = digest(canonical({"lock": lock, "toolingSHA256": fingerprint, "producerRevision": producer_revision}))
    catalog = {"schemaVersion": 1, "format": "org.floe.electronics.exchange", "dataVersion": "v1-" + dataset[:20],
               "datasetSHA256": dataset, "dimension": "2D", "nativeAppIntegration": "not-integrated",
               "producerRevision": producer_revision, "toolingSHA256": fingerprint, "sources": []}
    artifacts: list[dict] = []

    def artifact(path: Path, role: str, source_id: str | None = None):
        value = {"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size,
                 "sha256": file_digest(path), "role": role}
        if source_id:
            value["sourceID"] = source_id
        artifacts.append(value)
        return value

    all_ids: set[str] = set()
    source_roots: dict[str, Path] = {}
    for source in lock["sources"]:
        sid = source["id"]
        root = roots[sid] if roots is not None else materialize(source, cache)
        source_roots[sid] = root
        evidence = check_license(source, root)
        files = {name: {"bytes": path.stat().st_size, "sha256": file_digest(path)}
                 for name, path in source_files(root)}
        expected_inputs = {name for name in files if expected_record_file(name)}
        original_path = output / "originals" / (sid + ".zip")
        zip_sources(original_path, root, files, repository_root / "licenses")
        originals = artifact(original_path, "original-2d-sources", sid)
        inventory_path = output / "inventory" / (sid + ".jsonl.gz")
        with gzip_writer(inventory_path) as inventory:
            for name, details in sorted(files.items()):
                inventory.write(canonical({"path": name, **details}) + b"\n")
        artifact(inventory_path, "original-file-inventory", sid)
        chunks = []
        counts: Counter = Counter()
        kinds: Counter = Counter()
        accounted: set[str] = set()
        index_path = output / "indexes" / (sid + ".jsonl.gz")
        chunk = bytearray()
        chunk_line = 0
        chunk_number = 0

        def chunk_path() -> Path:
            return output / "records" / sid / (f"{chunk_number:05d}.jsonl.gz")

        def flush():
            if not chunk:
                return
            path = chunk_path()
            with gzip_writer(path) as stream:
                stream.write(chunk)
            chunks.append(artifact(path, "normalized-2d-records", sid))

        with gzip_writer(index_path) as index:
            for record in normalizer(root, sid, source["format"]):
                data = verify_record(record, sid, files)
                rid = record["id"]
                if rid in all_ids or len(all_ids) >= MAX_RECORDS:
                    raise SourceError("duplicate record identity or record budget exceeded")
                all_ids.add(rid)
                if chunk and len(chunk) + len(data) > CHUNK_BYTES:
                    flush()
                    chunk.clear()
                    chunk_number += 1
                    chunk_line = 0
                chunk_line += 1
                chunk.extend(data)
                counts[record["status"]] += 1
                kinds[record["kind"]] += 1
                accounted.add(record["source"]["path"])
                row = {"id": rid, "kind": record["kind"], "name": record["name"], "library": record.get("library", ""),
                       "status": record["status"], "path": chunk_path().relative_to(output).as_posix(),
                       "line": chunk_line, "sha256": digest(data), "sourcePath": record["source"]["path"]}
                index.write(canonical(row) + b"\n")
        flush()
        missing = expected_inputs - accounted
        if missing:
            raise SourceError(f"normalizer silently omitted {len(missing)} 2D source files; first: {sorted(missing)[0]}")
        if not counts and expected_inputs:
            raise SourceError("normalization produced no records")
        index_artifact = artifact(index_path, "search-index", sid)
        catalog["sources"].append({"id": sid, "repository": source["repository"], "ref": source["ref"],
                                   "commit": source["commit"], "license": evidence, "recordCount": sum(counts.values()),
                                   "modifications": "Floe normalized exchange conversion, millimetre geometry, search indexing and deterministic packaging. Original source files are unchanged. Compatibility status records unrepresented features; no 3D model files are included.",
                                   "sourceStatus": "normalized" if counts else "empty-upstream",
                                   "statuses": dict(sorted(counts.items())), "kinds": dict(sorted(kinds.items())),
                                   "originalFileCount": len(files), "originalBytes": sum(x["bytes"] for x in files.values()),
                                   "index": index_artifact, "chunks": chunks, "originals": originals})
        print(json.dumps({"source": sid, "records": sum(counts.values()), "statuses": dict(counts)}, ensure_ascii=False), flush=True)
    if policy.get("modelSources"):
        from .models import collect
        model_path = output / "models.jsonl.gz"
        model_count = 0
        with gzip_writer(model_path) as stream:
            for model in collect(lock, source_roots, cache.parent):
                stream.write(canonical(model) + b"\n")
                model_count += 1
        catalog["modelIndex"] = artifact(model_path, "model-index")
        catalog["modelCount"] = model_count
        catalog["modelSources"] = lock["modelSources"]
        print(json.dumps({"modelMetadataRecords": model_count, "modelBinariesDownloaded": 0}), flush=True)
    write_json(output / "upstreams.lock.json", lock)
    artifact(output / "upstreams.lock.json", "upstream-lock")
    write_json(output / "catalog.json", catalog)
    artifact(output / "catalog.json", "catalog")
    manifest = {"schemaVersion": 1, "dataVersion": catalog["dataVersion"], "datasetSHA256": dataset,
                "producerRevision": producer_revision, "toolingSHA256": fingerprint,
                "files": sorted(artifacts, key=lambda x: x["path"])}
    write_json(output / "manifest.json", manifest)
    checksums = [(x["path"], x["sha256"]) for x in manifest["files"]]
    checksums.append(("manifest.json", file_digest(output / "manifest.json")))
    (output / "SHA256SUMS").write_text("".join(f"{sha}  {path}\n" for path, sha in sorted(checksums)))
    verify(output)
    return catalog


def verify(output: Path) -> dict:
    if (output / "manifest.json").stat().st_size > 16 * 1024 * 1024:
        raise SourceError("manifest exceeds its byte budget")
    manifest = json.loads((output / "manifest.json").read_bytes())
    if manifest.get("schemaVersion") != 1 or not re.fullmatch(r"v1-[a-f0-9]{20}", manifest.get("dataVersion", "")):
        raise SourceError("unknown artifact manifest version")
    paths = set()
    for item in manifest["files"]:
        name = safe_path(item["path"])
        path = output / name
        if name in paths or path.is_symlink() or not path.is_file():
            raise SourceError("duplicate, missing or linked artifact")
        paths.add(name)
        if path.stat().st_size != item["bytes"] or file_digest(path) != item["sha256"]:
            raise SourceError("artifact changed: " + name)
        if path.suffix == ".zip":
            total = 0
            with zipfile.ZipFile(path) as archive:
                seen = set()
                for entry in archive.infolist():
                    safe_path(entry.filename)
                    mode = entry.external_attr >> 16
                    if entry.filename in seen or entry.filename.lower().endswith(tuple(MODEL_SUFFIXES)) or (mode & 0o170000) != 0o100000:
                        raise SourceError("unsafe/non-2D original archive entry")
                    seen.add(entry.filename)
                    total += entry.file_size
                    if entry.file_size > 64 * 1024 * 1024 or total > 800 * 1024 * 1024:
                        raise SourceError("original archive expansion limit")
                    with archive.open(entry) as stream:
                        while stream.read(1024 * 1024):
                            pass  # Forces CRC/read verification without extracting files.
    expected_paths = paths | {"manifest.json", "SHA256SUMS"}
    actual_paths = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
    if actual_paths != expected_paths:
        raise SourceError("unlisted or missing published output")
    expected = {name: file_digest(output / name) for name in paths | {"manifest.json"}}
    lines = (output / "SHA256SUMS").read_text().splitlines()
    found = {}
    for line in lines:
        sha, separator, name = line.partition("  ")
        if not separator or name in found or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise SourceError("malformed checksum manifest")
        found[safe_path(name)] = sha
    if found != expected:
        raise SourceError("checksum manifest differs")
    catalog = json.loads((output / "catalog.json").read_bytes())
    if any(catalog.get(k) != manifest.get(k) for k in ("dataVersion", "datasetSHA256", "producerRevision", "toolingSHA256")):
        raise SourceError("catalog/manifest binding differs")
    return catalog


def compare_baseline(previous: dict, current: dict) -> None:
    old = {s["id"]: s for s in previous["sources"]}
    new = {s["id"]: s for s in current["sources"]}
    if set(old) - set(new):
        raise SourceError("previously published source disappeared")
    for sid, before in old.items():
        after = new[sid]
        if after["recordCount"] < before["recordCount"] * 0.95:
            raise SourceError("more than 5% of source records disappeared: " + sid)
        usable_before = before["statuses"].get("converted", 0)
        if after["statuses"].get("converted", 0) < usable_before * 0.95:
            raise SourceError("more than 5% of converted records regressed: " + sid)
    if previous.get("modelCount", 0) and current.get("modelCount", 0) < previous["modelCount"] * 0.95:
        raise SourceError("more than 5% of model metadata records disappeared")


def bundle(output: Path, release: Path) -> dict:
    """GitHub assets are flat: provide full offline ZIP and one ZIP per upstream."""
    catalog = verify(output)
    manifest = json.loads((output / "manifest.json").read_bytes())
    if release.exists() and any(release.iterdir()):
        raise SourceError("release staging must be empty")
    release.mkdir(parents=True, exist_ok=True)
    assets = []

    def add(path: Path, role: str):
        item = {"name": path.name, "bytes": path.stat().st_size, "sha256": file_digest(path), "role": role}
        assets.append(item)
        return item

    def make_zip(path: Path, names: list[str], extra: dict[str, bytes] | None = None):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(names):
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_STORED if name.endswith((".gz", ".zip")) else zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                with archive.open(entry, "w", force_zip64=True) as destination, (output / name).open("rb") as source:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
            for name, data in sorted((extra or {}).items()):
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, data)

    all_names = [x["path"] for x in manifest["files"]] + ["manifest.json", "SHA256SUMS"]
    complete = release / "floe-electronics-all-2d.zip"
    make_zip(complete, all_names)
    add(complete, "all-2d-libraries")
    for source in catalog["sources"]:
        sid = source["id"]
        names = [x["path"] for x in manifest["files"] if x.get("sourceID") == sid]
        data = {"schemaVersion": 1, "dataVersion": catalog["dataVersion"], "source": source,
                "files": [x for x in manifest["files"] if x.get("sourceID") == sid]}
        package = release / (sid + "-2d.zip")
        make_zip(package, names, {"source-package.json": canonical(data) + b"\n"})
        add(package, "single-upstream-2d-library")
    extra_names = ["models.jsonl.gz"] if "modelIndex" in catalog else []
    for name in ("catalog.json", "manifest.json", "upstreams.lock.json", *extra_names):
        shutil.copyfile(output / name, release / name)
        add(release / name, "model-index" if name == "models.jsonl.gz" else name.removesuffix(".json"))
    receipt = {"schemaVersion": 1, "tag": "data-" + catalog["dataVersion"],
               "dataVersion": catalog["dataVersion"], "producerRevision": catalog["producerRevision"],
               "datasetSHA256": catalog["datasetSHA256"], "assets": sorted(assets, key=lambda x: x["name"])}
    write_json(release / "release.json", receipt)
    checksums = [(x["name"], x["sha256"]) for x in assets]
    checksums.append(("release.json", file_digest(release / "release.json")))
    (release / "release-SHA256SUMS").write_text("".join(f"{sha}  {name}\n" for name, sha in sorted(checksums)))
    return {"tag": receipt["tag"], "assets": len(assets) + 2,
            "all2DDownloadBytes": complete.stat().st_size}
