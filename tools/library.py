#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""The same source resolution, conversion and verification entry point locally and in CI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from floe_electronics import pipeline, sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["resolve", "fetch", "build", "verify", "compare", "bundle"])
    parser.add_argument("--policy", type=Path, default=ROOT / "upstreams.json")
    parser.add_argument("--lock", type=Path, default=ROOT / "upstreams.lock.json")
    parser.add_argument("--cache", type=Path, default=ROOT / ".cache" / "upstreams")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--release-dir", type=Path, default=ROOT / "release")
    parser.add_argument("--producer-revision", default=os.environ.get("GITHUB_SHA"))
    args = parser.parse_args()
    try:
        if args.command == "bundle":
            print(json.dumps(pipeline.bundle(args.output, args.release_dir)))
            return 0
        if args.command == "verify":
            result = pipeline.verify(args.output)
            print(json.dumps({"verified": result["dataVersion"]}))
            return 0
        if args.command == "compare":
            if args.baseline is None:
                raise sources.SourceError("compare requires --baseline")
            pipeline.compare_baseline(json.loads(args.baseline.read_bytes()), pipeline.verify(args.output))
            return 0
        policy = sources.load_policy(args.policy)
        if args.command == "resolve":
            lock = sources.resolve(policy)
            pipeline.write_json(args.lock, lock)
            print(json.dumps({"lock": str(args.lock), "sources": [{"id": s["id"], "ref": s["ref"], "commit": s["commit"]} for s in lock["sources"]]}))
            return 0
        lock = json.loads(args.lock.read_bytes())
        sources.verify_lock(policy, lock)
        if args.command == "fetch":
            def fetch_one(source):
                root = sources.materialize(source, args.cache)
                evidence = sources.check_license(source, root)
                files = list(sources.source_files(root))
                return {"source": source["id"], "root": str(root), "files": len(files), "license": evidence["id"]}
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(fetch_one, source) for source in lock["sources"]]
                for future in as_completed(futures):
                    print(json.dumps(future.result()), flush=True)
            return 0
        revision = args.producer_revision
        if not revision:
            revision = "working-tree-" + pipeline.tooling_digest(ROOT)
        result = pipeline.build(policy, lock, ROOT, args.cache, args.output, revision)
        print(json.dumps({"built": result["dataVersion"], "sourceCount": len(result["sources"])}))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print("library: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
