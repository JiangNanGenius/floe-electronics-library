# SPDX-License-Identifier: MPL-2.0
"""Public API, determinism, safety and schema-consistency tests."""

from __future__ import annotations

import json
import os
import unittest

import support

from floe_electronics.normalizers import NormalizeError, normalize

SCHEMA_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def minimal_schema_check(instance, schema, path="$"):
    """Validate the subset of JSON Schema used by schemas/*.json."""
    if "enum" in schema:
        if instance not in schema["enum"]:
            raise AssertionError("%s: %r not in enum" % (path, instance))
    expected = schema.get("type")
    if expected is not None:
        expected_types = expected if isinstance(expected, list) else [expected]
        allowed = tuple(SCHEMA_TYPES[name] for name in expected_types)
        if any(name in ("integer", "number") for name in expected_types) \
                and isinstance(instance, bool):
            raise AssertionError("%s: bool is not %s" % (path, expected))
        if not isinstance(instance, allowed):
            raise AssertionError("%s: %r is not %s"
                                 % (path, instance, expected))
    if isinstance(instance, dict):
        for key in schema.get("required", ()):
            if key not in instance:
                raise AssertionError("%s: missing required %r"
                                     % (path, key))
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                minimal_schema_check(value, properties[key],
                                     path + "." + key)
            elif schema.get("additionalProperties") is False:
                raise AssertionError("%s: unexpected key %r" % (path, key))
    if isinstance(instance, list):
        item_schema = schema.get("items")
        if item_schema:
            for index, value in enumerate(instance):
                minimal_schema_check(value, item_schema,
                                     "%s[%d]" % (path, index))


class ApiContract(support.RecordAssertions):
    def test_returns_iterator(self):
        iterator = normalize(support.fixture_path("kicad", "Footprints.pretty"),
                             "x", "kicad")
        self.assertTrue(hasattr(iterator, "__next__"))
        self.assertTrue(hasattr(iterator, "__iter__"))

    def test_unknown_format(self):
        with self.assertRaises(NormalizeError):
            normalize(support.fixture_path("kicad"), "x", "eagle")

    def test_root_must_be_directory(self):
        with self.assertRaises(NormalizeError):
            normalize(support.fixture_path("kicad", "Device.kicad_sym"), "x",
                      "kicad")
        with self.assertRaises(NormalizeError):
            normalize(support.fixture_path("does-not-exist"), "x", "kicad")

    def test_unsafe_symlink_rejected(self):
        tree = support.TempTree({
            "real/symbol.lp": "(librepcb_symbol 88888888-8888-4888-8888-"
                              "888888888888 (name \"S\"))",
        })
        outside = os.path.join(tree.root, "..", "outside-%s" % os.getpid())
        os.makedirs(outside, exist_ok=True)
        leak = os.path.join(outside, "component.lp")
        with open(leak, "w", encoding="utf-8") as handle:
            handle.write("(librepcb_component 1)")
        link = os.path.join(tree.root, "escape", "component.lp")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(leak, link)
        try:
            with self.assertRaises(NormalizeError):
                list(normalize(tree.root, "x", "librepcb"))
        finally:
            os.unlink(link)
            os.unlink(leak)
            os.rmdir(outside)
            tree.cleanup()

    def test_json_roundtrip_and_hash_link(self):
        records = support.records_for(
            support.fixture_path("kicad", "Footprints.pretty"), "x", "kicad")
        record = records[0]
        self.assertEqual(json.loads(support.dumps(records)), records)
        raw = support.read_bytes(os.path.join(
            support.fixture_path("kicad", "Footprints.pretty"),
            "R_0805.kicad_mod"))
        import hashlib
        self.assertEqual(record["source"]["sha256"],
                         hashlib.sha256(raw).hexdigest())


class Determinism(unittest.TestCase):
    FILES = {
        "a/Symbols.kicad_sym": (
            '(kicad_symbol_lib (version 1)\n'
            '  (symbol "Alpha" (property "Reference" "U" (at 0 0 0)\n'
            '    (effects (font (size 1 1))))\n'
            '    (symbol "Alpha_1_1" (pin passive line (at 0 0 0)\n'
            '      (length 2.54) (name "~") (number "1"))))\n'
            '  (symbol "Beta" (property "Reference" "U" (at 0 0 0)\n'
            '    (effects (font (size 1 1)))))\n'
            ')\n'),
        "b/Symbols.kicad_sym": (
            '(kicad_symbol_lib (version 1)\n'
            '  (symbol "Alpha" (property "Reference" "U" (at 0 0 0)\n'
            '    (effects (font (size 1 1)))))\n'
            ')\n'),
        "c/Board.kicad_mod": (
            '(kicad_footprint (version 1) (footprint "F"\n'
            '  (pad "1" smd rect (at 0 0) (size 1 1)\n'
            '    (layers "F.Cu")))\n'
            ')\n'),
    }
    ORDER_A = ["a/Symbols.kicad_sym", "b/Symbols.kicad_sym", "c/Board.kicad_mod"]
    ORDER_B = ["c/Board.kicad_mod", "b/Symbols.kicad_sym", "a/Symbols.kicad_sym"]

    def _build(self, order):
        tree = support.TempTree({path: self.FILES[path] for path in order})
        return tree

    def test_creation_order_does_not_change_output(self):
        first, second = self._build(self.ORDER_A), self._build(self.ORDER_B)
        try:
            left = support.dumps(support.records_for(first.root, "det",
                                                     "kicad"))
            right = support.dumps(support.records_for(second.root, "det",
                                                      "kicad"))
        finally:
            first.cleanup()
            second.cleanup()
        self.assertEqual(left, right)

    def test_repeat_run_is_byte_identical(self):
        tree = self._build(self.ORDER_A)
        try:
            first = support.dumps(support.records_for(tree.root, "det",
                                                      "kicad"))
            second = support.dumps(support.records_for(tree.root, "det",
                                                       "kicad"))
        finally:
            tree.cleanup()
        self.assertEqual(first, second)

    def test_duplicate_names_get_distinct_stable_ids(self):
        tree = self._build(self.ORDER_A)
        try:
            records = support.records_for(tree.root, "det", "kicad")
        finally:
            tree.cleanup()
        alphas = [record for record in records
                  if record["name"] == "Alpha"]
        self.assertEqual(len(alphas), 2)
        ids = sorted(record["id"] for record in alphas)
        self.assertEqual(len(set(ids)), 2)
        for record in alphas:
            self.assertIn("duplicate-source-name", record["issues"])
            self.assertEqual(record["status"], "reviewRequired")
        # Distinct source files have distinct identities: no id collision.
        self.assertNotIn("duplicate-id", alphas[0]["issues"])
        self.assertTrue(ids[0].startswith(
            "floe:electronics:det:symbol:Symbols:Alpha~"))

    def test_ids_survive_insertion_of_an_earlier_duplicate(self):
        base = {
            "a/Symbols.kicad_sym": (
                '(kicad_symbol_lib (version 1)\n'
                '  (symbol "Alpha" (property "Reference" "U"'
                ' (at 0 0 0)))\n)\n'),
            "b/Symbols.kicad_sym": (
                '(kicad_symbol_lib (version 1)\n'
                '  (symbol "Alpha" (property "Reference" "U"'
                ' (at 0 0 0)))\n)\n'),
        }
        inserted = dict(base)
        inserted["0/Symbols.kicad_sym"] = (
            '(kicad_symbol_lib (version 1)\n'
            '  (symbol "Alpha" (property "Reference" "U" (at 0 0 0)))\n)\n')
        after = dict(base)
        after["c/Symbols.kicad_sym"] = (
            '(kicad_symbol_lib (version 1)\n'
            '  (symbol "Alpha" (property "Reference" "U" (at 0 0 0)))\n)\n')

        def id_map(files, order):
            tree = support.TempTree({path: files[path] for path in order})
            try:
                records = support.records_for(tree.root, "det", "kicad")
            finally:
                tree.cleanup()
            return {record["source"]["path"]: record["id"]
                    for record in records}

        before_ids = id_map(base, ["b/Symbols.kicad_sym",
                                   "a/Symbols.kicad_sym"])
        inserted_ids = id_map(
            inserted, ["b/Symbols.kicad_sym", "0/Symbols.kicad_sym",
                       "a/Symbols.kicad_sym"])
        appended_ids = id_map(
            after, ["a/Symbols.kicad_sym", "b/Symbols.kicad_sym",
                    "c/Symbols.kicad_sym"])
        # Inserting a duplicate earlier in sort order (or later) must not
        # shift any existing record id.
        for path in ("a/Symbols.kicad_sym", "b/Symbols.kicad_sym"):
            self.assertEqual(inserted_ids[path], before_ids[path])
            self.assertEqual(appended_ids[path], before_ids[path])
        self.assertEqual(len(set(inserted_ids.values())), 3)
        self.assertEqual(len(set(appended_ids.values())), 3)

    def test_sort_order_is_stable_across_runs(self):
        tree = self._build(self.ORDER_A)
        try:
            records = support.records_for(tree.root, "det", "kicad")
        finally:
            tree.cleanup()
        keys = [(record["source"]["path"], record["id"]) for record in records]
        self.assertEqual(keys, sorted(keys))


class SchemaConsistency(support.RecordAssertions):
    def test_schema_file_is_valid_and_covers_records(self):
        with open(support.SCHEMA_PATH, "r", encoding="utf-8") as handle:
            schema = json.load(handle)
        self.assertEqual(schema.get("$schema"),
                         "https://json-schema.org/draft/2020-12/schema")
        for key in self.REQUIRED:
            self.assertIn(key, schema["required"])
        self.assertEqual(schema["properties"]["schemaVersion"]["const"], 1)
        self.assertEqual(schema["properties"]["units"]["const"], "mm")
        for kind in ("symbol", "footprint", "component"):
            self.assertIn(kind, schema["properties"]["kind"]["enum"])
        for status in ("converted", "reviewRequired", "unsupported"):
            self.assertIn(status, schema["properties"]["status"]["enum"])

        records = []
        records.extend(support.records_for(
            support.fixture_path("kicad"), "schema", "kicad"))
        records.extend(support.records_for(
            support.fixture_path("librepcb"), "schema", "librepcb"))
        self.assertTrue(records)
        for record in records:
            minimal_schema_check(record, schema)


class NonSourceFilesIgnored(unittest.TestCase):
    def test_docs_and_three_d_files_do_not_affect_records(self):
        tree = support.TempTree({
            "lib/pkg/x/package.lp": (
                "(librepcb_package 12121212-1212-4212-8212-121212121212\n"
                " (name \"P\")\n"
                " (pad 12121212-0000-4000-8000-000000000001 (name \"1\"))\n"
                " (3d_model 12121212-0000-4000-8000-000000000002"
                " (name \"P\"))\n)\n"),
            "lib/pkg/x/12121212-0000-4000-8000-000000000002.step": b"\x00\x01",
            "lib/pkg/x/README.md": "not a source file",
            "lib/pkg/x/preview.png": b"\x89PNG\r\n\x1a\n",
        })
        try:
            records = support.records_for(tree.root, "x", "librepcb")
        finally:
            tree.cleanup()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "P")
        self.assertEqual(records[0]["status"], "converted")
        self.assertEqual(len(records[0]["modelReferences"]), 1)


if __name__ == "__main__":
    unittest.main()


class KiCadPackedNameIdentity(unittest.TestCase):
    """Packed-file names whose slugs collide must still get stable ids."""

    HEADER = "(kicad_symbol_lib (version 20231120)\n"
    SYMBOL_1 = (' (symbol "A+B" (property "Reference" "U" (at 0 0 0)))\n')
    SYMBOL_2 = (' (symbol "A B" (property "Reference" "U" (at 0 0 0)))\n')
    FOOTER = ")\n"

    def _ids(self, order):
        content = self.HEADER + "".join(order) + self.FOOTER
        tree = support.TempTree({"Lib.kicad_sym": content})
        try:
            records = support.records_for(tree.root, "packed", "kicad")
        finally:
            tree.cleanup()
        return {record["name"]: record for record in records}

    def test_slug_collision_names_get_distinct_order_independent_ids(self):
        forward = self._ids([self.SYMBOL_1, self.SYMBOL_2])
        reverse = self._ids([self.SYMBOL_2, self.SYMBOL_1])
        self.assertEqual(set(forward), {"A+B", "A B"})
        self.assertEqual(forward["A+B"]["id"], reverse["A+B"]["id"])
        self.assertEqual(forward["A B"]["id"], reverse["A B"]["id"])
        self.assertNotEqual(forward["A+B"]["id"], forward["A B"]["id"])
        for record in forward.values():
            self.assertNotIn("duplicate-id", record["issues"])
            self.assertNotIn("duplicate-source-name", record["issues"])
            self.assertEqual(record["status"], "converted")
            # readable slug is shared; the exact-name digest disambiguates
            self.assertIn(":A_B~", record["id"])
