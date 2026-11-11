# SPDX-License-Identifier: MPL-2.0
"""LibrePCB .lplib normalizer tests (synthetic, real-shape fixtures)."""

from __future__ import annotations

import json
import os
import unittest

import support

from floe_electronics.normalizers import normalize


class LibrePCBRecords(support.RecordAssertions):
    @classmethod
    def setUpClass(cls):
        cls.root = support.fixture_path("librepcb")
        cls.records = support.records_for(
            cls.root, "librepcb-librepcb-base", "librepcb")
        cls.by_name = {}
        for record in cls.records:
            cls.by_name.setdefault(record["name"], []).append(record)

    def one(self, name):
        matches = self.by_name.get(name, [])
        self.assertEqual(len(matches), 1, "expected exactly one %r" % name)
        return matches[0]

    def test_all_records_valid_and_covered(self):
        self.assert_all_valid(self.records)
        self.assert_coverage(self.records, self.root,
                             ("symbol.lp", "package.lp", "component.lp",
                              "device.lp"))

    def test_record_inventory(self):
        expected = {"Test Symbol Ω", "TEST-2P-2.54", "Test Component",
                    "Test Device ⌀2.54", "Orphan Device",
                    "55555555-5555-4555-8555-555555555555"}
        self.assertEqual(set(self.by_name), expected)
        kinds = {r["name"]: r["kind"] for r in self.records}
        self.assertEqual(kinds["Test Symbol Ω"], "symbol")
        self.assertEqual(kinds["TEST-2P-2.54"], "footprint")
        self.assertEqual(kinds["Test Component"], "component")
        self.assertEqual(kinds["Test Device ⌀2.54"], "component")
        self.assertEqual(kinds["Orphan Device"], "component")

    def test_library_name(self):
        for record in self.records:
            if record["name"] in ("Test Symbol Ω", "TEST-2P-2.54",
                                  "Test Component", "Test Device ⌀2.54",
                                  "Orphan Device"):
                self.assertEqual(record["library"], "Test")
            else:
                self.assertEqual(record["library"], "Broken")

    def test_empty_upstream_yields_no_records(self):
        records = support.records_for(
            support.fixture_path("librepcb", "Empty.lplib"), "empty",
            "librepcb")
        self.assertEqual(records, [])

    def test_symbol_geometry_and_pins(self):
        record = self.one("Test Symbol Ω")
        self.assertEqual(record["status"], "converted")
        self.assertEqual(record["identifiers"]["uuid"],
                         "11111111-1111-4111-8111-111111111111")
        self.assertEqual(record["properties"]["grid_interval"], 2.54)
        self.assertEqual(record["properties"]["deprecated"], False)
        self.assertIn("multi-line\ndescription", record["properties"]
                      ["description"])
        self.assertIn('"quotes"', record["properties"]["description"])
        self.assertEqual(len(record["pins"]), 2)
        pin = {p["name"]: p for p in record["pins"]}["A+"]
        self.assertEqual(pin["positionMm"], [-5.08, 2.54])
        self.assertEqual(pin["rotationDeg"], 0.0)
        self.assertEqual(pin["lengthMm"], 2.0)
        self.assertEqual(pin["nameAlign"], "left bottom")
        self.assertEqual(len(record["graphics"]), 3)
        polygon = [g for g in record["graphics"]
                   if g["type"] == "polygon"][0]
        self.assertEqual(len(polygon["vertices"]), 4)
        self.assertEqual(polygon["vertices"][1]["angleDeg"], 90.0)
        self.assertEqual(polygon["layer"], "sym_outlines")
        circle = [g for g in record["graphics"] if g["type"] == "circle"][0]
        self.assertEqual(circle["diameterMm"], 0.254)
        self.assertEqual(circle["positionMm"], [-1.254, 1.254])
        text = [g for g in record["graphics"] if g["type"] == "text"][0]
        self.assertEqual(text["value"], "{{NAME}}")

    def test_package_pads_footprint_and_models(self):
        record = self.one("TEST-2P-2.54")
        self.assertEqual(record["kind"], "footprint")
        self.assertEqual(sorted(pad["name"] for pad in record["pads"]),
                         ["1", "2"])
        self.assertEqual(record["properties"]["assembly_type"], "tht")
        self.assertEqual(record["properties"]["min_copper_clearance"], 0.25)
        self.assertEqual(len(record["footprints"]), 1)
        footprint = record["footprints"][0]
        self.assertEqual(footprint["name"], "through-hole")
        self.assertEqual(footprint["tags"], ["through-hole"])
        self.assertEqual(len(footprint["pads"]), 2)
        pad = footprint["pads"][0]
        self.assertEqual(pad["side"], "through_hole")
        self.assertEqual(pad["shape"], "round")
        self.assertEqual(pad["positionMm"], [-1.27, 0.0])
        self.assertEqual(pad["rotationDeg"], 45.0)
        self.assertEqual(pad["sizeMm"], [1.8, 1.8])
        self.assertEqual(pad["function"], "standard")
        self.assertEqual(pad["packagePadUuid"],
                         "22222222-0000-4000-8000-000000000001")
        self.assertEqual(len(footprint["graphics"]), 4)
        self.assertEqual(footprint["graphics"][3]["type"], "hole")
        models = footprint["modelReferences"]
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["uuid"],
                         "aaaa1111-2222-4333-8444-555566667777")
        self.assertEqual(models[0]["offsetMm"], [0.0, 0.0, -2.5])
        self.assertEqual(models[0]["rotationDeg"], [0.0, 0.0, 90.0])
        self.assertEqual(record["modelReferences"][0]["name"], "TEST-2P")
        # Every footprint pad's package-pad reference resolves inside the
        # same package.lp file.
        resolved = record["dependencies"]["packagePads"]
        self.assertEqual(len(resolved), 2)
        for entry in resolved:
            self.assertEqual(entry["resolvedPath"],
                             "Test.lplib/pkg/22222222-2222-4222-8222-222222222222/"
                             "package.lp")

    def test_component_signals_and_variant_mapping(self):
        record = self.one("Test Component")
        self.assertEqual(record["status"], "converted")
        self.assertEqual(record["properties"]["prefix"], "TP")
        self.assertEqual(record["properties"]["schematic_only"], False)
        signals = {pin["name"]: pin for pin in record["pins"]}
        self.assertEqual(set(signals), {"+", "−"})
        self.assertEqual(signals["−"]["role"], "power")
        self.assertEqual(signals["−"]["forced_net"], "GND")
        variant = record["references"]["symbolVariants"][0]
        self.assertEqual(variant["name"], "default")
        gate = variant["gates"][0]
        self.assertEqual(gate["symbolUuid"],
                         "11111111-1111-4111-8111-111111111111")
        self.assertEqual(len(gate["pinSignalMap"]), 2)
        # The pin->signal mapping is copied from the source, never inferred.
        mapping = {entry["uuid"]: entry["signalUuid"]
                   for entry in gate["pinSignalMap"]}
        self.assertEqual(
            mapping["33333333-0000-4000-8000-000000000012"],
            "33333333-0000-4000-8000-000000000001")
        symbol_dep = record["dependencies"]["symbols"][0]
        self.assertEqual(symbol_dep["resolvedPath"],
                         "Test.lplib/sym/11111111-1111-4111-8111-111111111111/"
                         "symbol.lp")

    def test_device_mapping_and_identity(self):
        record = self.one("Test Device ⌀2.54")
        self.assertEqual(record["kind"], "component")
        self.assertEqual(record["properties"]["role"], "device")
        self.assertEqual(record["properties"]["approved"],
                         [{"reason": "no_parts"}])
        self.assertEqual(record["dependencies"]["components"][0]
                         ["resolvedPath"],
                         "Test.lplib/cmp/33333333-3333-4333-8333-333333333333/"
                         "component.lp")
        self.assertEqual(record["dependencies"]["packages"][0]
                         ["resolvedPath"],
                         "Test.lplib/pkg/22222222-2222-4222-8222-222222222222/"
                         "package.lp")
        mapping = record["references"]["pinPadMap"]
        self.assertEqual(len(mapping), 2)
        self.assertEqual(mapping[0]["padUuid"],
                         "22222222-0000-4000-8000-000000000001")
        self.assertEqual(mapping[0]["optional"], False)
        self.assertEqual(mapping[1]["optional"], True)
        for entry in mapping:
            self.assertEqual(entry["signalUuid"],
                             "33333333-0000-4000-8000-%s" %
                             ("000000000001" if entry is mapping[0]
                              else "000000000002"))

    def test_unresolved_dependencies_are_visible(self):
        record = self.one("Orphan Device")
        self.assertEqual(record["status"], "reviewRequired")
        joined = " ".join(record["issues"])
        self.assertIn("unresolved-dependency:components", joined)
        self.assertIn("unresolved-dependency:packages", joined)
        self.assertIn("unresolved-dependency:signals", joined)

    def test_categories_do_not_force_review(self):
        record = self.one("Test Symbol Ω")
        self.assertEqual(record["status"], "converted")
        self.assertEqual(record["dependencies"]["categories"][0]["id"],
                         "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.assertNotIn("resolvedPath",
                         record["dependencies"]["categories"][0])

    def test_broken_file_accounted_unsupported(self):
        record = self.one("55555555-5555-4555-8555-555555555555")
        self.assertEqual(record["status"], "unsupported")
        self.assertTrue(any(issue.startswith("parse-error")
                            for issue in record["issues"]))
        self.assertEqual(record["source"]["path"],
                         "Broken.lplib/sym/55555555-5555-4555-8555-"
                         "555555555555/symbol.lp")

    def test_no_3d_binary_bytes(self):
        payload = support.dumps(self.records)
        three_d = (".step", ".stp", ".stl", ".wrl")
        for record in self.records:
            self.assertFalse(record["source"]["path"].endswith(three_d))
            for reference in record.get("modelReferences", []):
                self.assertNotIn("bytes", reference)
                self.assertNotIn("data", reference)
        self.assertNotIn("ISO-10303", payload)


class LibrePCBUnsafeInputs(support.RecordAssertions):
    def test_invalid_root_and_missing_name(self):
        tree = support.TempTree({
            "lib/x/symbol.lp": "(librepcb_symbol (name \"NoUuid\"))",
            "lib/y/component.lp": "(librepcb_component 77777777-7777-4777-"
                                  "8777-777777777777 (name \"No Description\"))",
            "lib/z/device.lp": "(librepcb_device (name \"No UUID\"))",
            "lib/w/package.lp": "(other_root 1)",
        })
        try:
            records = support.records_for(tree.root, "unsafe", "librepcb")
        finally:
            tree.cleanup()
        self.assert_all_valid(records)
        by_path = {record["source"]["path"]: record for record in records}
        self.assertIn("missing-uuid", by_path["lib/x/symbol.lp"]["issues"])
        self.assertEqual(by_path["lib/x/symbol.lp"]["name"], "NoUuid")
        self.assertEqual(by_path["lib/y/component.lp"]["name"],
                         "No Description")
        self.assertIn("missing-uuid", by_path["lib/z/device.lp"]["issues"])
        self.assertTrue(any(issue.startswith("invalid-root")
                            for issue in by_path["lib/w/package.lp"]["issues"]))

    def test_unrecognized_lp_files_are_ignored(self):
        tree = support.TempTree({
            "lib/library.lp": "(librepcb_lib 1 (name \"L\") (version \"1\"))",
            "lib/categories.lp": "(librepcb_categories)",
        })
        try:
            records = support.records_for(tree.root, "ignored", "librepcb")
        finally:
            tree.cleanup()
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()


class LibrePCBDuplicateIdentity(support.RecordAssertions):
    UUID = "abcdefab-1234-4234-8234-abcdefabcdef"

    def test_duplicate_uuid_ids_are_path_stable(self):
        files = {
            "lib/sym/%s/symbol.lp" % self.UUID:
                "(librepcb_symbol %s (name \"Alpha\"))" % self.UUID,
            "lib/sym/%s-copy/symbol.lp" % self.UUID:
                "(librepcb_symbol %s (name \"Beta\"))" % self.UUID,
        }
        first = support.TempTree(files)
        reversed_tree = support.TempTree(dict(reversed(list(files.items()))))
        try:
            left = support.records_for(first.root, "dup", "librepcb")
            right = support.records_for(reversed_tree.root, "dup", "librepcb")
        finally:
            first.cleanup()
            reversed_tree.cleanup()
        self.assertEqual(len(left), 2)
        self.assertEqual(support.dumps(left), support.dumps(right))
        ids = [record["id"] for record in left]
        self.assertEqual(len(set(ids)), 2)
        for record in left:
            self.assertIn("duplicate-source-identity", record["issues"])
            self.assertEqual(record["status"], "reviewRequired")
            self.assertIn(self.UUID, record["id"])
            self.assertNotIn("duplicate-id", record["issues"])


class LibrePCBDuplicateDetection(unittest.TestCase):
    """The single-pass UUID index detects local and cross-file duplicates."""

    def test_local_and_cross_file_duplicates_detected(self):
        from floe_electronics.normalizers import librepcb
        uuid = "abcdefab-1234-4234-8234-abcdefabcdef"
        pad = "11111111-2222-4333-8444-555555555555"
        tree = support.TempTree({
            # duplicate pad definition inside one package file
            "lib/pkg/%s/package.lp" % uuid: (
                "(librepcb_package %s (name \"DupPads\")\n"
                " (pad %s (name \"1\"))\n"
                " (pad %s (name \"1\"))\n)" % (uuid, pad, pad)),
            "lib/sym/%s/symbol.lp" % pad: (
                "(librepcb_symbol %s (name \"CrossDup\"))" % pad),
            "lib/sym/other/symbol.lp": (
                "(librepcb_symbol %s (name \"CrossDup2\"))" % pad),
        })
        try:
            resolved = librepcb.common.check_root(tree.root)
            files = list(librepcb.common.iter_source_files(
                resolved, (".lp",)))
            index, duplicates = librepcb._build_uuid_index(resolved, files)
        finally:
            tree.cleanup()
        self.assertIn(pad, duplicates)      # cross-file duplicate
        self.assertIn(uuid, index)
        self.assertIn(pad, index)

    def test_local_duplicate_only(self):
        from floe_electronics.normalizers import librepcb
        uuid = "abcdefab-1234-4234-8234-abcdefabcdef"
        pad = "11111111-2222-4333-8444-555555555555"
        tree = support.TempTree({
            "lib/pkg/%s/package.lp" % uuid: (
                "(librepcb_package %s (name \"DupPads\")\n"
                " (pad %s (name \"1\"))\n"
                " (pad %s (name \"2\"))\n)" % (uuid, pad, pad)),
        })
        try:
            resolved = librepcb.common.check_root(tree.root)
            files = list(librepcb.common.iter_source_files(
                resolved, (".lp",)))
            _, duplicates = librepcb._build_uuid_index(resolved, files)
        finally:
            tree.cleanup()
        self.assertEqual(duplicates, {pad})
