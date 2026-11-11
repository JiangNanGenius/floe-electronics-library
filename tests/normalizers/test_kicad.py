# SPDX-License-Identifier: MPL-2.0
"""KiCad symbol and footprint normalizer tests (synthetic fixtures)."""

from __future__ import annotations

import json
import os
import unittest

import support

from floe_electronics.normalizers import normalize

KICAD_PIN_MM = 1.0  # .kicad_sym is mm


class KiCadFixtureCoverage(support.RecordAssertions):
    def setUp(self):
        self.records = support.records_for(
            support.fixture_path("kicad"), "kicad-fixture", "kicad")
        self.by_name = {}
        for record in self.records:
            self.by_name.setdefault(record["name"], []).append(record)

    def one(self, name):
        matches = self.by_name.get(name, [])
        self.assertEqual(len(matches), 1, "expected exactly one %r" % name)
        return matches[0]

    def test_all_records_valid_and_covered(self):
        self.assert_all_valid(self.records)
        self.assert_coverage(self.records, support.fixture_path("kicad"),
                             (".kicad_sym", ".kicad_mod"))

    def test_libraries_and_kinds(self):
        self.assertEqual(self.one("R")["library"], "Device")
        self.assertEqual(self.one("R")["kind"], "symbol")
        self.assertEqual(self.one("R_0805_2012Metric")["library"],
                         "Footprints")
        self.assertEqual(self.one("R_0805_2012Metric")["kind"], "footprint")
        self.assertEqual(self.one("Conn_01x02")["library"], "Unpacked")

    def test_units_are_mm_with_explicit_transform(self):
        r = self.one("R")
        self.assertEqual(r["units"], "mm")
        pin = r["pins"][0]
        self.assertAlmostEqual(pin["positionMm"][1], 3.81 * KICAD_PIN_MM, 9)
        rect = r["graphics"][0]
        self.assertAlmostEqual(rect["startMm"][0], -1.016 * KICAD_PIN_MM, 9)
        self.assertAlmostEqual(rect["endMm"][1], 2.54 * KICAD_PIN_MM, 9)

    def test_symbol_units_and_pin_metadata(self):
        r = self.one("R")
        self.assertEqual(r["properties"]["unitCount"], 1)
        self.assertEqual(r["properties"]["pin_numbers"], "hide")
        self.assertEqual(r["properties"]["Reference"], "R")
        pin_numbers = sorted(p["number"] for p in r["pins"])
        self.assertEqual(pin_numbers, ["1", "2"])
        for pin in r["pins"]:
            self.assertEqual(pin["electricalType"], "passive")
            self.assertEqual(pin["unit"], 1)
            self.assertEqual(pin["style"], 1)
            self.assertAlmostEqual(pin["lengthMm"], 1.27 * KICAD_PIN_MM, 9)
        self.assertAlmostEqual(r["pins"][0]["rotationDeg"], 270.0, 9)

    def test_inheritance_flattens_without_losing_source(self):
        derived = self.one("Q_NMOS_Derived")
        self.assertEqual(derived["status"], "converted")
        self.assertEqual(sorted(p["number"] for p in derived["pins"]),
                         ["1", "2", "3"])
        for pin in derived["pins"]:
            self.assertEqual(pin["inheritedFrom"], "Q_NMOS_GSD")
        self.assertEqual(len(derived["graphics"]), 2)
        for graphic in derived["graphics"]:
            self.assertEqual(graphic["inheritedFrom"], "Q_NMOS_GSD")
        self.assertEqual(derived["properties"]["MPN"], "DMG2302UK")
        self.assertEqual(derived["properties"]["Footprint"], "")
        self.assertEqual(derived["dependencies"]["extends"][0]["id"],
                         "Q_NMOS_GSD")

    def test_cyclic_inheritance_is_unsupported(self):
        for name in ("LoopA", "LoopB"):
            record = self.one(name)
            self.assertEqual(record["status"], "unsupported")
            self.assertIn("cyclic-inheritance", record["issues"])
            self.assertIn("raw", record)

    def test_unresolved_inheritance_is_unsupported(self):
        record = self.one("Orphan")
        self.assertEqual(record["status"], "unsupported")
        self.assertTrue(any(issue.startswith("unresolved-inheritance")
                            for issue in record["issues"]))
        self.assertEqual(record["dependencies"]["extends"][0]["id"],
                         "NotInThisLibrary")

    def test_duplicate_source_names_stay_visible(self):
        duplicates = self.by_name["Dup"]
        self.assertEqual(len(duplicates), 2)
        ids = sorted(record["id"] for record in duplicates)
        self.assertEqual(len(set(ids)), 2)
        statuses = [record["status"] for record in duplicates]
        self.assertIn("reviewRequired", statuses)
        flagged = [r for r in duplicates if "duplicate-id" in r["issues"]]
        self.assertEqual(len(flagged), 1)
        self.assertIn("duplicate-source-name", flagged[0]["issues"])

    def test_unicode_and_negative_coordinates(self):
        record = self.one("Ünïcode-Résistance 10kΩ")
        self.assertEqual(record["status"], "converted")
        self.assertEqual(record["properties"]["Value"], "10kΩ")
        self.assertAlmostEqual(record["pins"][0]["positionMm"][0],
                               -3.81 * KICAD_PIN_MM, 9)
        arc = [g for g in record["graphics"] if g["type"] == "arc"][0]
        self.assertAlmostEqual(arc["midMm"][1], 1.016 * KICAD_PIN_MM, 9)
        text = [g for g in record["graphics"] if g["type"] == "text"][0]
        self.assertEqual(text["text"], "Ω")


class KiCadFootprint(support.RecordAssertions):
    def setUp(self):
        self.records = support.records_for(
            support.fixture_path("kicad", "Footprints.pretty"),
            "kicad-footprints", "kicad")
        self.record = self.records[0]

    def test_pads_preserved(self):
        pads = {pad["number"]: pad for pad in self.record["pads"]}
        self.assertEqual(sorted(pads), ["", "1", "2", "3", "4"])
        self.assertAlmostEqual(pads["1"]["positionMm"][0], -0.9125, 9)
        self.assertEqual(pads["1"]["sizeMm"], [1.025, 1.4])
        self.assertEqual(pads["2"]["rotationDeg"], 90.0)
        self.assertEqual(pads["2"]["net"], {"number": "1", "name": "GND"})
        self.assertEqual(pads[""]["type"], "np_thru_hole")
        self.assertEqual(pads[""]["drill"]["diameterMm"], 0.6)
        self.assertEqual(pads["3"]["drill"]["shape"], "oval")
        self.assertEqual(pads["3"]["drill"]["sizeMm"], [1.0, 0.6])
        self.assertEqual(pads["3"]["drill"]["offsetMm"], [0.1, -0.1])
        self.assertEqual(pads["1"]["roundrectRratio"], 0.243902)

    def test_custom_pad_geometry_is_conservative(self):
        pads = {pad["number"]: pad for pad in self.record["pads"]}
        self.assertTrue(pads["4"]["customGeometry"])
        self.assertEqual(pads["4"]["primitives"]["tag"], "primitives")
        self.assertEqual(self.record["status"], "reviewRequired")
        self.assertIn("custom-pad-geometry", self.record["issues"])

    def test_graphics_and_text(self):
        types = [graphic["type"] for graphic in self.record["graphics"]]
        self.assertEqual(types.count("fp_text"), 2)
        for expected in ("fp_line", "fp_rect", "fp_circle", "fp_arc",
                         "fp_poly"):
            self.assertIn(expected, types)
        ref = [g for g in self.record["graphics"]
               if g.get("kind") == "reference"][0]
        self.assertEqual(ref["text"], "REF**")
        self.assertAlmostEqual(ref["positionMm"][1], -1.65, 9)
        circle = [g for g in self.record["graphics"]
                  if g["type"] == "fp_circle"][0]
        self.assertAlmostEqual(circle["endMm"][0], 0.635, 9)

    def test_model_reference_metadata_only(self):
        models = self.record["modelReferences"]
        self.assertEqual(len(models), 1)
        model = models[0]
        self.assertIn("R_0805_2012Metric.wrl", model["file"])
        self.assertEqual(model["offset"], [0.0, 0.0, 0.0])
        self.assertEqual(model["scale"], [1.0, 1.0, 1.0])
        self.assertEqual(model["rotationDeg"], [0.0, 0.0, 0.0])
        self.assertNotIn("bytes", model)

    def test_attributes_and_text_fields(self):
        self.assertEqual(self.record["properties"]["attributes"], ["smd"])
        self.assertEqual(self.record["properties"]["layer"], "F.Cu")
        self.assertIn("0805", self.record["properties"]["descr"])


class KiCadLegacyAndUnpacked(support.RecordAssertions):
    def test_legacy_module_root(self):
        records = support.records_for(
            support.fixture_path("kicad", "Legacy.pretty"), "legacy", "kicad")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assert_valid_record(record)
        self.assertEqual(record["name"], "Legacy_Test")
        self.assertEqual(record["library"], "Legacy")
        self.assertEqual(len(record["pads"]), 2)
        self.assertEqual(record["pads"][1]["rotationDeg"], 180.0)
        self.assertEqual(record["modelReferences"][0]["file"], "legacy.wrl")

    def test_unpacked_symbol_directory(self):
        root = support.fixture_path("kicad", "Unpacked.kicad_symdir")
        records = support.records_for(root, "kicad-unpacked", "kicad")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assert_valid_record(record)
        self.assertEqual(record["kind"], "symbol")
        self.assertEqual(record["library"], "Unpacked")
        self.assertEqual(record["source"]["path"], "Conn_01x02.kicad_sym")
        self.assertEqual(sorted(p["number"] for p in record["pins"]),
                         ["1", "2"])


class KiCadMalformed(support.RecordAssertions):
    def test_truncated_and_invalid_files_are_accounted(self):
        tree = support.TempTree({
            "bad.kicad_sym": "(kicad_symbol_lib (version 1) (symbol \"X\"",
            "wrong_root.kicad_mod": "(not_a_footprint 1)",
            "ok.kicad_sym": "(kicad_symbol_lib (version 1) "
                            "(symbol \"Y\" (property \"Reference\" \"U\""
                            " (at 0 0 0) (effects (font (size 1 1))))))",
            "empty.kicad_sym": "(kicad_symbol_lib (version 1))",
        })
        try:
            records = support.records_for(tree.root, "malformed", "kicad")
        finally:
            tree.cleanup()
        self.assert_all_valid(records)
        statuses = {record["source"]["path"]: record["status"]
                    for record in records}
        self.assertEqual(statuses["bad.kicad_sym"], "unsupported")
        self.assertEqual(statuses["wrong_root.kicad_mod"], "unsupported")
        self.assertEqual(statuses["empty.kicad_sym"], "unsupported")
        self.assertEqual(statuses["ok.kicad_sym"], "converted")
        issues = {record["source"]["path"]: record["issues"]
                  for record in records}
        self.assertTrue(any(issue.startswith("parse-error")
                            for issue in issues["bad.kicad_sym"]))
        self.assertTrue(any(issue.startswith("invalid-root")
                            for issue in issues["wrong_root.kicad_mod"]))
        self.assertIn("no-symbols", issues["empty.kicad_sym"])


if __name__ == "__main__":
    unittest.main()


class KiCadMillimetreRegression(support.RecordAssertions):
    """Independent regression pins for the KiCad mm (not mil) correction.

    The earlier revision wrongly treated modern .kicad_sym coordinates as
    mils and multiplied by 0.0254; these assertions pin real upstream metric
    values and the identity transform.
    """

    LOCK = os.path.join(support.REPO_ROOT, "upstreams.lock.json")
    CACHE = os.path.join(support.REPO_ROOT, ".cache", "upstreams")

    @classmethod
    def _checkout(cls, source_id):
        try:
            with open(cls.LOCK, "r", encoding="utf-8") as handle:
                lock = json.load(handle)
        except (OSError, ValueError):
            return None
        for source in lock.get("sources", []):
            if source.get("id") == source_id:
                return os.path.join(cls.CACHE, source_id, source["commit"])
        return None

    def setUp(self):
        root = self._checkout("kicad-symbols")
        self.symbol_file = None
        if root:
            candidate = os.path.join(root, "Device.kicad_symdir",
                                     "R.kicad_sym")
            if os.path.isfile(candidate):
                self.symbol_file = candidate

    def test_source_units_constant_is_identity(self):
        from floe_electronics.normalizers import common
        self.assertEqual(common.KICAD_SYMBOL_MM, 1.0)
        self.assertEqual(common.KICAD_FOOTPRINT_MM, 1.0)

    def test_modern_symbol_values_are_not_scaled(self):
        tree = support.TempTree({
            "Device.kicad_symdir/R.kicad_sym": (
                '(kicad_symbol_lib (version 20251024)\n'
                ' (symbol "R"\n'
                '  (property "Reference" "R" (at 2.032 0 90))\n'
                '  (symbol "R_0_1"\n'
                '   (rectangle (start -1.016 -2.54) (end 1.016 2.54)))\n'
                '  (symbol "R_1_1"\n'
                '   (pin passive line (at 0 3.81 270) (length 1.27)'
                ' (name "~") (number "1"))))\n'
                ')\n'),
        })
        try:
            record = support.records_for(tree.root, "units", "kicad")[0]
        finally:
            tree.cleanup()
        pin = record["pins"][0]
        self.assertEqual(pin["positionMm"], [0.0, 3.81])
        self.assertEqual(pin["lengthMm"], 1.27)
        self.assertEqual(record["graphics"][0]["startMm"], [-1.016, -2.54])
        self.assertEqual(record["graphics"][0]["endMm"], [1.016, 2.54])
        # Guard against the failed 0.0254 mil assumption resurfacing.
        self.assertNotAlmostEqual(pin["positionMm"][1], 3.81 * 0.0254, 3)
        self.assertEqual(record["properties"]["Reference"], "R")

    def test_real_upstream_resistor_metric_values(self):
        if not self.symbol_file:
            self.skipTest("pinned kicad-symbols checkout not available")
        records = support.records_for(
            os.path.dirname(self.symbol_file), "kicad-symbols", "kicad")
        record = [r for r in records if r["name"] == "R"][0]
        self.assertEqual(record["library"], "Device")
        self.assertEqual(record["units"], "mm")
        pins = {pin["number"]: pin for pin in record["pins"]}
        self.assertEqual(pins["1"]["positionMm"], [0.0, 3.81])
        self.assertEqual(pins["2"]["positionMm"], [0.0, -3.81])
        self.assertEqual(pins["1"]["lengthMm"], 1.27)
        rect = record["graphics"][0]
        self.assertEqual(rect["startMm"], [-1.016, -2.54])
        self.assertEqual(rect["endMm"], [1.016, 2.54])
        import hashlib
        self.assertEqual(record["source"]["sha256"],
                         hashlib.sha256(
                             support.read_bytes(self.symbol_file)).hexdigest())

    def test_real_upstream_footprint_metric_values(self):
        root = self._checkout("kicad-footprints")
        if not root:
            self.skipTest("pinned kicad-footprints checkout not available")
        candidate = os.path.join(
            root, "TerminalBlock_WAGO.pretty",
            "TerminalBlock_WAGO_236-116_1x16_P5.00mm_45Degree.kicad_mod")
        if not os.path.isfile(candidate):
            self.skipTest("WAGO footprint not present")
        records = support.records_for(
            os.path.dirname(candidate), "kicad-footprints", "kicad")
        record = [r for r in records
                  if r["name"] == "TerminalBlock_WAGO_236-116_"
                            "1x16_P5.00mm_45Degree"][0]
        pads = {pad["number"]: pad for pad in record["pads"]}
        self.assertEqual(pads["1"]["positionMm"], [0.0, 0.0])
        self.assertEqual(pads["1"]["sizeMm"], [1.5, 3.0])
        self.assertEqual(pads["1"]["drill"]["diameterMm"], 1.15)
        line = record["graphics"][0]
        self.assertEqual(line["startMm"], [-3.62, -5.12])
        self.assertEqual(line["endMm"], [78.92, -5.12])
        self.assertTrue(record["modelReferences"][0]["file"].endswith(".step"))
