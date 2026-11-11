# SPDX-License-Identifier: MPL-2.0
"""Resolve immutable upstream revisions and materialize 2D-only sparse checkouts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

SHA = re.compile(r"[0-9a-f]{40}\Z")
SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,95}\Z")
REPOSITORY = re.compile(r"https://(?:github\.com|gitlab\.com)/[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+\.git\Z")
MODEL_SUFFIXES = {".step", ".stp", ".stpz", ".wrl", ".wrz", ".glb", ".gltf", ".obj", ".stl", ".iges", ".igs"}
DATA_SUFFIXES = {".kicad_sym", ".kicad_mod", ".lp", ".md", ".txt"}
MAX_SOURCE_BYTES = 768 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024


class SourceError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(value: str) -> str:
    if not value or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value) or len(value.encode()) > 4096:
        raise SourceError("unsafe relative path")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ("", ".", "..") for x in value.split("/")):
        raise SourceError("unsafe relative path")
    return value


def git(args: list[str], *, cwd: Path | None = None, stdin: str | None = None) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    # No checkout hooks, global filters, source-provided programs, or submodules.
    command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
               "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
               "-c", "submodule.recurse=false", *args]
    result = subprocess.run(command, cwd=cwd, env=env, input=stdin, text=True,
                            capture_output=True, timeout=600)
    if result.returncode:
        raise SourceError("git operation failed: " + result.stderr.strip()[-2000:])
    return result.stdout


def load_policy(path: Path) -> dict:
    value = json.loads(path.read_bytes())
    if value.get("schemaVersion") != 1 or not isinstance(value.get("sources"), list):
        raise SourceError("unsupported source policy")
    ids: set[str] = set()
    for source in value["sources"]:
        sid = source.get("id", "")
        if not SOURCE_ID.fullmatch(sid) or sid in ids:
            raise SourceError("invalid or duplicate upstream id")
        ids.add(sid)
        if not REPOSITORY.fullmatch(source.get("repository", "")) or ".." in source["repository"]:
            raise SourceError("upstream must be an approved public HTTPS Git origin")
        if not source.get("enabled"):
            continue
        if source.get("format") not in ("kicad", "librepcb"):
            raise SourceError("unknown source format")
        license_policy = source.get("license", {})
        if license_policy.get("id") not in ("CC-BY-4.0", "CC-BY-SA-4.0 WITH LicenseRef-KiCad-design-exception", "CC0-1.0"):
            raise SourceError("unreviewed upstream license")
        safe_path(license_policy["evidencePath"])
        if not re.fullmatch(r"[0-9a-f]{64}", license_policy.get("approvedTextSHA256", "")):
            raise SourceError("license evidence must have an approved text digest")
    enabled_ids = {s["id"] for s in value["sources"] if s.get("enabled")}
    for model in value.get("modelSources", []):
        sid = model.get("id", "")
        if not SOURCE_ID.fullmatch(sid) or sid in ids:
            raise SourceError("invalid or duplicate model source id")
        ids.add(sid)
        if not REPOSITORY.fullmatch(model.get("repository", "")) or ".." in model["repository"]:
            raise SourceError("unapproved model repository")
        if model.get("provider") != "gitlab" or not model["repository"].startswith("https://gitlab.com/"):
            raise SourceError("unsupported metadata-only model provider")
        if model.get("versionFrom") not in enabled_ids or model.get("licenseSourceID") not in enabled_ids:
            raise SourceError("model source needs approved version/license sources")
    return value


def remote_refs(source: dict) -> dict[str, str]:
    rows = git(["ls-remote", source["repository"]]).splitlines()
    return {ref: sha for row in rows if len(parts := row.split("\t")) == 2
            for sha, ref in [parts] if SHA.fullmatch(sha)}


def semver_tags(refs: dict[str, str], major: int | None) -> dict[tuple[int, int, int], str]:
    result = {}
    for ref in refs:
        match = re.fullmatch(r"refs/tags/(v?(\d+)\.(\d+)\.(\d+))", ref)
        if match:
            version = tuple(map(int, match.groups()[1:]))
            if major is None or version[0] == major:
                result[version] = ref
    return result


def resolve(policy: dict) -> dict:
    enabled = [s for s in policy["sources"] if s.get("enabled")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(remote_refs, enabled))
    refs = {source["id"]: found for source, found in zip(enabled, results)}
    groups: dict[str, list[dict]] = {}
    for source in enabled:
        if group := source.get("releaseGroup"):
            groups.setdefault(group, []).append(source)
    selected_groups = {}
    for group, members in groups.items():
        versions = [set(semver_tags(refs[s["id"]], s["tracking"].get("major"))) for s in members]
        common = set.intersection(*versions)
        if not common:
            raise SourceError("no common stable release for " + group)
        selected_groups[group] = max(common)
    locked = []
    for source in enabled:
        found = refs[source["id"]]
        tracking = source["tracking"]
        if tracking["kind"] == "stable-tag":
            versions = semver_tags(found, tracking.get("major"))
            if not versions:
                raise SourceError("no stable tag: " + source["id"])
            version = selected_groups.get(source.get("releaseGroup"), max(versions))
            ref = versions[version]
        elif tracking["kind"] == "branch":
            branch = tracking["name"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", branch):
                raise SourceError("unsafe tracked branch")
            ref = "refs/heads/" + branch
        else:
            raise SourceError("unknown upstream tracking policy")
        commit = found.get(ref + "^{}", found.get(ref))
        if not commit or not SHA.fullmatch(commit):
            raise SourceError("cannot resolve upstream revision")
        locked.append({"id": source["id"], "repository": source["repository"], "format": source["format"],
                       "ref": ref, "commit": commit, "license": source["license"]})
    model_sources = []
    by_id = {s["id"]: s for s in locked}
    for model in policy.get("modelSources", []):
        reference = by_id[model["versionFrom"]]["ref"]
        if not reference.startswith("refs/tags/"):
            raise SourceError("separate model trees must follow an approved stable tag")
        found = remote_refs(model)
        commit = found.get(reference + "^{}", found.get(reference))
        if not commit or not SHA.fullmatch(commit):
            raise SourceError("model tree lacks the selected stable release")
        model_sources.append({"id": model["id"], "repository": model["repository"], "provider": model["provider"],
                              "ref": reference, "commit": commit, "licenseSourceID": model["licenseSourceID"],
                              "license": by_id[model["licenseSourceID"]]["license"]})
    result = {"schemaVersion": 1, "policySHA256": digest(canonical(policy)), "sources": locked}
    if "modelSources" in policy:
        result["modelSources"] = model_sources
    return result


def verify_lock(policy: dict, lock: dict) -> None:
    expected = {s["id"]: s for s in policy["sources"] if s.get("enabled")}
    if lock.get("schemaVersion") != 1 or lock.get("policySHA256") != digest(canonical(policy)):
        raise SourceError("source lock does not match reviewed policy")
    if not isinstance(lock.get("sources"), list) or len(lock["sources"]) != len(expected):
        raise SourceError("source lock omitted or duplicated a source")
    seen = set()
    for source in lock["sources"]:
        sid = source.get("id")
        if sid not in expected or sid in seen:
            raise SourceError("unexpected locked source")
        seen.add(sid)
        original = expected[sid]
        if any(source.get(key) != original.get(key) for key in ("repository", "format", "license")):
            raise SourceError("locked source changed its authority")
        if not SHA.fullmatch(source.get("commit", "")):
            raise SourceError("source lock needs an immutable commit")
    expected_models = {s["id"]: s for s in policy.get("modelSources", [])}
    pinned_models = lock.get("modelSources", [])
    if len(pinned_models) != len(expected_models) or len({s.get("id") for s in pinned_models}) != len(pinned_models):
        raise SourceError("model lock omitted or duplicated a source")
    by_id = {s["id"]: s for s in lock["sources"]}
    for model in pinned_models:
        original = expected_models.get(model.get("id"))
        if not original or any(model.get(k) != original.get(k) for k in ("repository", "provider", "licenseSourceID")):
            raise SourceError("model lock changed its authority")
        if model.get("ref") != by_id[original["versionFrom"]]["ref"] or not SHA.fullmatch(model.get("commit", "")):
            raise SourceError("model source is not pinned to the selected release")
        if model.get("license") != by_id[original["licenseSourceID"]]["license"]:
            raise SourceError("model source changed its reviewed license")


def materialize(source: dict, cache: Path) -> Path:
    root = cache / source["id"] / source["commit"]
    if (root / ".git").is_dir():
        if git(["rev-parse", "HEAD"], cwd=root).strip() != source["commit"]:
            raise SourceError("cached source revision changed")
        if git(["status", "--porcelain", "--untracked-files=all"], cwd=root):
            raise SourceError("cached upstream has local modifications")
        return root
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise SourceError("source staging directory is not empty")
    git(["init", "--quiet", "--initial-branch=source"], cwd=root)
    git(["remote", "add", "origin", source["repository"]], cwd=root)
    git(["config", "remote.origin.promisor", "true"], cwd=root)
    git(["config", "remote.origin.partialclonefilter", "blob:none"], cwd=root)
    git(["fetch", "--quiet", "--filter=blob:none", "--depth=1", "--no-tags", "origin", source["commit"]], cwd=root)
    git(["sparse-checkout", "init", "--no-cone"], cwd=root)
    patterns = "*.kicad_sym\n*.kicad_mod\n*.lp\n*.md\n*.txt\nLICENSE\nCOPYING\nNOTICE\n"
    git(["sparse-checkout", "set", "--no-cone", "--stdin"], cwd=root, stdin=patterns)
    git(["checkout", "--quiet", "--detach", source["commit"]], cwd=root)
    if git(["rev-parse", "HEAD"], cwd=root).strip() != source["commit"]:
        raise SourceError("upstream checkout read-back differs")
    return root


def source_files(root: Path):
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            raise SourceError("upstream symbolic link: " + relative.as_posix())
        if not path.is_file():
            continue
        safe_path(relative.as_posix())
        if path.suffix.lower() in MODEL_SUFFIXES:
            raise SourceError("unexpected 3D binary in 2D sparse checkout")
        if path.suffix.lower() not in DATA_SUFFIXES and path.name not in {"LICENSE", "COPYING", "NOTICE"}:
            raise SourceError("unexpected upstream file type: " + relative.as_posix())
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_SOURCE_BYTES:
            raise SourceError("upstream 2D byte budget exceeded")
        yield relative.as_posix(), path


def license_text(data: bytes, mode: str) -> bytes:
    text = data.decode("utf-8").replace("\r\n", "\n")
    if mode == "sparkfun-license-section":
        marker = "\nLicense\n"
        if marker not in text:
            raise SourceError("SparkFun license section disappeared")
        text = text.split(marker, 1)[1]
    elif mode != "full":
        raise SourceError("unknown license evidence extraction")
    return text.strip().encode()


def check_license(source: dict, root: Path) -> dict:
    policy = source["license"]
    evidence = root / safe_path(policy["evidencePath"])
    if evidence.is_symlink() or not evidence.is_file() or evidence.stat().st_size > 1024 * 1024:
        raise SourceError("missing bounded upstream license evidence")
    data = evidence.read_bytes()
    observed = digest(license_text(data, policy.get("extract", "full")))
    if observed != policy["approvedTextSHA256"]:
        raise SourceError("upstream license changed; reviewed policy update required: " + source["id"])
    return {"id": policy["id"], "attribution": policy["attribution"], "url": policy["url"],
            "evidencePath": policy["evidencePath"], "evidenceSHA256": digest(data), "approvedTextSHA256": observed}
