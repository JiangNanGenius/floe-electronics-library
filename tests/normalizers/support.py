# SPDX-License-Identifier: MPL-2.0
"""Shared test support: source path bootstrap, fixtures, structural checks.

Stdlib only.  Tests run with ``python3 -m unittest discover -s tests/normalizers``
from the repository root, so ``src`` is added to ``sys.path`` here.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SRC = os.path.join(REPO_ROOT, "src")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SCHEMA_PATH = os.path.join(REPO_ROOT, "schemas", "normalized-record.schema.json")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from floe_electronics.normalizers import (  # noqa: E402
    NormalizeError, normalize)
from floe_electronics.normalizers import common  # noqa: E402


def fixture_path(*parts):
    return os.path.join(FIXTURES, *parts)


def records_for(root, source_id, format_name):
    return list(normalize(root, source_id, format_name))


def dumps(records):
    return json.dumps(records, sort_keys=True, ensure_ascii=False)


class TempTree(object):
    """Create a synthetic source checkout from {relpath: bytes-or-str}."""

    def __init__(self, files, base=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name if base is None else base
        os.makedirs(self.root, exist_ok=True)
        for relpath, content in files.items():
            target = os.path.join(self.root, *relpath.split("/"))
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            data = content.encode("utf-8") if isinstance(content, str) \
                else content
            with open(target, "wb") as handle:
                handle.write(data)

    def cleanup(self):
        self._tmp.cleanup()


def normalized_paths(records):
    return [record["source"]["path"] for record in records]


class RecordAssertions(unittest.TestCase):
    """Structural checks mirroring schemas/normalized-record.schema.json."""

    REQUIRED = ("schemaVersion", "id", "kind", "name", "library", "format",
                "units", "source", "identifiers", "status", "issues",
                "properties")

    def assert_valid_record(self, record):
        for key in self.REQUIRED:
            self.assertIn(key, record)
        self.assertEqual(record["schemaVersion"], 1)
        self.assertEqual(record["units"], "mm")
        self.assertIn(record["kind"], common.KINDS)
        self.assertIn(record["status"], common.STATUSES)
        self.assertIsInstance(record["issues"], list)
        self.assertIsInstance(record["properties"], dict)
        self.assertIsInstance(record["identifiers"], dict)
        self.assertIsInstance(record["name"], str)
        self.assertIsInstance(record["library"], str)
        self.assertTrue(record["id"].startswith("floe:electronics:"))
        source = record["source"]
        self.assertIsInstance(source, dict)
        self.assertEqual(sorted(source), ["path", "sha256"])
        self.assertFalse(os.path.isabs(source["path"]))
        self.assertNotIn("..", source["path"].split("/"))
        self.assertEqual(len(source["sha256"]), 64)
        int(source["sha256"], 16)
        json.dumps(record)  # must be JSON-serializable

    def assert_all_valid(self, records):
        for record in records:
            self.assert_valid_record(record)

    def assert_coverage(self, records, root, suffixes):
        covered = set(normalized_paths(records))
        expected = set()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in filenames:
                if name.endswith(tuple(suffixes)):
                    rel = os.path.relpath(os.path.join(dirpath, name), root)
                    expected.add(rel.replace(os.sep, "/"))
        self.assertEqual(expected - covered, set(),
                         "uncovered source files")


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def write_tree(base, files):
    for relpath, content in files.items():
        target = os.path.join(base, *relpath.split("/"))
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        with open(target, "wb") as handle:
            handle.write(data)
