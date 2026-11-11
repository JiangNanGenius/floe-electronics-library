#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Validate the normalizers against real sparse upstream checkouts.

Reads ``upstreams.lock.json`` (``sources`` only; model inventories are
ignored), runs the normalizer for each source root under
``.cache/upstreams/<id>/<commit>``, writes one JSONL per source plus a
``report.json`` summary, and enforces:

* every recognized source file appears as ``record.source.path``;
* zero records are allowed only for genuinely empty upstreams;
* record shape/status/units invariants;
* no 3D model binary content or file is emitted;
* optional rerun determinism (``--determinism``).

Usage::

    /opt/homebrew/bin/python3.12 tests/normalizers/real_corpus_check.py \
        --lock upstreams.lock.json --out "$TMPDIR/floe-normalized" \
        --determinism

Exit code is non-zero when any enforced check fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from floe_electronics.normalizers import normalize, common  # noqa: E402
from floe_electronics.normalizers.sexpr import parse  # noqa: E402

KICAD_SUFFIXES = (".kicad_sym", ".kicad_mod")
LP_NAMES = ("symbol.lp", "package.lp", "component.lp", "device.lp")
THREE_D_SUFFIXES = (".step", ".stp", ".stl", ".wrl", ".step.gz", ".wrz")


def recognized_files(root, format_name):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if format_name == "kicad" and name.endswith(KICAD_SUFFIXES):
                found.append(os.path.relpath(
                    os.path.join(dirpath, name), root).replace(os.sep, "/"))
            elif format_name == "librepcb" and name in LP_NAMES:
                found.append(os.path.relpath(
                    os.path.join(dirpath, name), root).replace(os.sep, "/"))
    return found


def record_digest(records):
    digest = hashlib.sha256()
    for record in records:
        digest.update(json.dumps(record, sort_keys=True,
                                 ensure_ascii=False).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def validate_record(record, errors, context):
    where = "%s:%s" % (context, record.get("source", {}).get("path"))
    if record.get("schemaVersion") != 1:
        errors.append(where + ": schemaVersion != 1")
    if record.get("units") != "mm":
        errors.append(where + ": units != mm")
    if record.get("kind") not in common.KINDS:
        errors.append(where + ": bad kind %r" % (record.get("kind"),))
    if record.get("status") not in common.STATUSES:
        errors.append(where + ": bad status %r" % (record.get("status"),))
    if not isinstance(record.get("issues"), list):
        errors.append(where + ": issues is not a list")
    if not isinstance(record.get("properties"), dict):
        errors.append(where + ": properties is not a dict")
    source = record.get("source") or {}
    path = source.get("path", "")
    if not path or os.path.isabs(path) or ".." in path.split("/"):
        errors.append(where + ": unsafe source path")
    sha = source.get("sha256", "")
    if len(sha) != 64:
        errors.append(where + ": bad source sha256")
    if path.lower().endswith(THREE_D_SUFFIXES):
        errors.append(where + ": 3D binary path emitted as source")
    for key in ("modelBytes", "modelData", "stepBytes"):
        if key in record:
            errors.append(where + ": 3D binary field %r emitted" % key)
    try:
        json.dumps(record)
    except (TypeError, ValueError) as exc:
        errors.append(where + ": not JSON serializable: %s" % exc)


def run_source(source, root, out_dir, determinism, errors, stats):
    source_id = source["id"]
    format_name = source["format"]
    started = time.monotonic()
    records = []
    for record in normalize(root, source_id, format_name):
        validate_record(record, errors, source_id)
        records.append(record)
    elapsed = time.monotonic() - started

    expected = set(recognized_files(root, format_name))
    covered = {record["source"]["path"] for record in records}
    missing = expected - covered
    if missing:
        errors.append("%s: %d recognized source files have no record, e.g. %s"
                      % (source_id, len(missing), sorted(missing)[:3]))

    if not expected and not records:
        source_status = "empty-upstream"
    elif not expected and records:
        errors.append("%s: records emitted without recognized source files"
                      % source_id)
        source_status = "unexpected-records"
    else:
        source_status = "normalized"

    jsonl_path = os.path.join(out_dir, source_id + ".jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True,
                                    ensure_ascii=False) + "\n")
    digest = record_digest(records)

    if determinism and records:
        again = list(normalize(root, source_id, format_name))
        second = record_digest(again)
        if second != digest:
            errors.append("%s: non-deterministic output" % source_id)

    statuses = {}
    kinds = {}
    issue_codes = {}
    unhandled_tags = {}
    for record in records:
        statuses[record["status"]] = statuses.get(record["status"], 0) + 1
        kinds[record["kind"]] = kinds.get(record["kind"], 0) + 1
        for issue in record["issues"]:
            code = issue.split(":", 1)[0]
            issue_codes[code] = issue_codes.get(code, 0) + 1
        for node in record.get("unhandled", []):
            tag = node.get("tag") if isinstance(node, dict) else None
            unhandled_tags[tag] = unhandled_tags.get(tag, 0) + 1

    size = sum(len(json.dumps(record, ensure_ascii=False))
               for record in records)
    entry = {
        "id": source_id,
        "format": format_name,
        "sourceStatus": source_status,
        "commit": source["commit"],
        "recognizedSourceFiles": len(expected),
        "records": len(records),
        "kinds": dict(sorted(kinds.items())),
        "statuses": dict(sorted(statuses.items())),
        "issues": dict(sorted(issue_codes.items())),
        "unhandledTags": dict(sorted(
            unhandled_tags.items(), key=lambda item: str(item[0]))),
        "jsonl": os.path.basename(jsonl_path),
        "jsonlBytes": os.path.getsize(jsonl_path),
        "recordsSha256": digest,
        "normalizeSeconds": round(elapsed, 3),
        "approxRecordBytes": size,
    }
    stats["sources"].append(entry)
    stats["totalRecords"] += len(records)
    stats["totalRecognizedFiles"] += len(expected)
    for key, value in statuses.items():
        stats["statuses"][key] = stats["statuses"].get(key, 0) + value
    for key, value in issue_codes.items():
        stats["issues"][key] = stats["issues"].get(key, 0) + value
    print("%-42s %-8s files=%-6d records=%-6d %s %s"
          % (source_id, format_name, len(expected), len(records),
             json.dumps(statuses, sort_keys=True),
             "" if source_status == "normalized" else source_status),
          flush=True)
    return entry


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", default=os.path.join(
        REPO_ROOT, "upstreams.lock.json"))
    parser.add_argument("--cache", default=os.path.join(
        REPO_ROOT, ".cache", "upstreams"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", default="")
    parser.add_argument("--determinism", action="store_true")
    args = parser.parse_args(argv)

    with open(args.lock, "r", encoding="utf-8") as handle:
        lock = json.load(handle)
    sources = lock.get("sources", [])
    only = {item.strip() for item in args.only.split(",") if item.strip()}
    if only:
        sources = [source for source in sources if source["id"] in only]

    os.makedirs(args.out, exist_ok=True)
    errors = []
    stats = {"sources": [], "totalRecords": 0, "totalRecognizedFiles": 0,
             "statuses": {}, "issues": {}}
    for source in sources:
        root = os.path.join(args.cache, source["id"], source["commit"])
        if not os.path.isdir(root):
            errors.append("%s: missing checkout at %s"
                          % (source["id"], root))
            print("%-42s MISSING CHECKOUT" % source["id"], flush=True)
            continue
        run_source(source, root, args.out, args.determinism, errors, stats)

    stats["errors"] = errors
    stats["ok"] = not errors
    report_path = os.path.join(args.out, "report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(stats, handle, indent=1, sort_keys=True)
        handle.write("\n")
    print("\nTOTAL sources=%d records=%d files=%d statuses=%s"
          % (len(stats["sources"]), stats["totalRecords"],
             stats["totalRecognizedFiles"], json.dumps(
                 stats["statuses"], sort_keys=True)))
    print("report:", report_path)
    if errors:
        print("ERRORS (%d):" % len(errors))
        for error in errors[:40]:
            print(" -", error)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
