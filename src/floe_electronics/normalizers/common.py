# SPDX-License-Identifier: MPL-2.0
"""Shared helpers for the Floe electronics normalizers."""

from __future__ import annotations

import hashlib
import os
import re

SCHEMA_VERSION = 1
UNITS = "mm"

KIND_SYMBOL = "symbol"
KIND_FOOTPRINT = "footprint"
KIND_COMPONENT = "component"
KINDS = (KIND_SYMBOL, KIND_FOOTPRINT, KIND_COMPONENT)

STATUS_CONVERTED = "converted"
STATUS_REVIEW = "reviewRequired"
STATUS_UNSUPPORTED = "unsupported"
STATUSES = (STATUS_CONVERTED, STATUS_REVIEW, STATUS_UNSUPPORTED)

# Modern KiCad S-expression symbol libraries (.kicad_sym) already store
# millimetres: per dev-docs.kicad.org/en/file-formats/sexpr-intro, all
# coordinates and sizes are given in mm.  Legacy .lib (mils) is NOT
# supported.  The 0.0254 mil factor that an earlier revision applied here
# was wrong and scaled symbol geometry by 39.37x; regression tests pin the
# real millimetre values from pinned upstream files.
KICAD_SYMBOL_MM = 1.0
# KiCad footprint coordinates are also millimetres.
KICAD_FOOTPRINT_MM = 1.0
# LibrePCB .lp files store millimeters.
LIBREPCB_MM = 1.0

_MM_ROUND_DIGITS = 9
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SLUG_SAFE_RE = re.compile(r"[^\w.\-~]+", re.UNICODE)


class NormalizeError(ValueError):
    """Raised for invalid arguments or unsafe source roots."""


def is_uuid(text):
    return bool(text) and bool(_UUID_RE.match(text))


def slug(text):
    """Reduce an arbitrary source name to a stable, URL-safe id segment."""
    if not text:
        return "_"
    out = _SLUG_SAFE_RE.sub("_", text.strip())
    return out or "_"


def identity_digest(text, length=12):
    """Stable hex digest of an exact source identity string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def record_id(source_id, kind, library, name, discriminator=None):
    """Deterministic record id distinguishing source, kind, library, name.

    *discriminator* is an intrinsic source identity (LibrePCB UUID) or a
    stable digest of the exact source element identity (KiCad file path).
    It makes ids collision-free regardless of file iteration order and keeps
    existing ids stable when new source files are inserted.
    """
    base = "floe:electronics:%s:%s:%s:%s" % (
        slug(source_id), slug(kind), slug(library), slug(name))
    if discriminator:
        return base + "~" + discriminator
    return base


def parse_number(text):
    """Parse a source numeric atom.

    Returns (float_value, error).  Exactly one of the two is None.
    """
    if text is None or not _NUMBER_RE.match(text):
        return None, "invalid-number:%s" % (text,)
    try:
        value = float(text)
    except ValueError:
        return None, "invalid-number:%s" % (text,)
    if value != value or value in (float("inf"), float("-inf")):
        return None, "invalid-number:%s" % (text,)
    return value, None


def to_mm(value, factor):
    """Convert a source-unit coordinate to millimeters, rounded stably."""
    return round(value * factor, _MM_ROUND_DIGITS)


def number_or_raw(node, index, factor, issues, context):
    """Read a numeric atom from *node* at *index*, converting to mm.

    On failure an issue code is appended and the raw atom text is returned.
    """
    text = node.value_at(index)
    value, error = parse_number(text)
    if error:
        issues.append("%s@%s" % (error, context))
        return text
    return to_mm(value, factor)


def atomic_scalar(node):
    """Best-effort scalar for a metadata node: bool, float, or string."""
    if node is None:
        return None
    items = node.items
    if len(items) < 2:
        return None
    value = items[1]
    if value.is_atom:
        text = value.value
    elif value.is_list and value.tag == "value" and len(value.items) >= 2:
        text = value.value_at(1)
    else:
        return None
    if text is None:
        return None
    if text in ("true", "yes"):
        return True
    if text in ("false", "no"):
        return False
    number, error = parse_number(text)
    if error is None:
        return number
    return text


def check_root(root):
    if not isinstance(root, os.PathLike):
        root = os.fspath(root)
    root = os.path.realpath(os.path.abspath(root))
    if not os.path.isdir(root):
        raise NormalizeError("source root is not a directory: %r" % (root,))
    return root


def iter_source_files(root, suffixes):
    """Yield (relpath_posix, abspath) for source files in stable order.

    Symlinks and paths escaping the source root are rejected: traversal is
    fail-closed because a source checkout must be self-contained.
    """
    suffixes = tuple(suffixes)
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(suffixes):
                continue
            abspath = os.path.join(dirpath, name)
            if os.path.islink(abspath):
                raise NormalizeError(
                    "unsafe source path (symlink): %r" % (abspath,))
            real = os.path.realpath(abspath)
            if not real.startswith(root + os.sep):
                raise NormalizeError(
                    "unsafe source path (escapes root): %r" % (abspath,))
            found.append(os.path.relpath(real, root))
    found.sort()
    for relpath in found:
        yield relpath.replace(os.sep, "/"), os.path.join(root, relpath)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def source_ref(relpath, abspath):
    return {"path": relpath, "sha256": sha256_file(abspath)}


def base_record(format_name, source_id, kind, library, name, source,
               discriminator=None):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": record_id(source_id, kind, library, name, discriminator),
        "kind": kind,
        "name": name,
        "library": library,
        "format": format_name,
        "units": UNITS,
        "source": source,
        "identifiers": {},
        "status": STATUS_CONVERTED,
        "issues": [],
        "properties": {},
        "dependencies": {},
        "references": {},
        "modelReferences": [],
    }


def add_dependency(record, role, identity, resolved_path=None):
    deps = record["dependencies"].setdefault(role, [])
    entry = {"id": identity}
    if resolved_path is not None:
        entry["resolvedPath"] = resolved_path
    if entry not in deps:
        deps.append(entry)


def assign_ids(records, seen=None):
    """Guard against duplicate ids without order-dependent renaming.

    Normal ids are intrinsic (LibrePCB UUID or KiCad source-identity
    digest), so this is a safety net for pathological exact-duplicate
    source elements.  A colliding record gets a content digest suffix
    (stable for identical bytes) and, only when records are literally
    indistinguishable, a numeric suffix; it is always flagged
    ``duplicate-id`` and forced to ``reviewRequired``.  Existing ids never
    shift because of encounter order.

    Pass a shared *seen* dict to deduplicate across a whole source run while
    still streaming file by file.  Returns the list.
    """
    if seen is None:
        seen = {}
    for record in records:
        base = record["id"]
        candidate = base
        if candidate in seen:
            digest = identity_digest(
                "%s\0%s" % (base, record["source"]["path"]))
            candidate = "%s~%s" % (base, digest)
            attempt = 1
            while candidate in seen:
                attempt += 1
                candidate = "%s~%s~%d" % (base, digest, attempt)
            record["id"] = candidate
            if "duplicate-id" not in record["issues"]:
                record["issues"].append("duplicate-id")
            if record["status"] == STATUS_CONVERTED:
                record["status"] = STATUS_REVIEW
        seen[candidate] = seen.get(candidate, 0) + 1
    return records


def strip_empty(record):
    """Drop empty containers so records stay compact; order is preserved."""
    for key in ("dependencies", "references"):
        if key in record and not record[key]:
            del record[key]
    for key in ("pins", "pads", "footprints", "graphics", "modelReferences"):
        if key in record and not record[key]:
            del record[key]
    return record
