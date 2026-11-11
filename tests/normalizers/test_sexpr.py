# SPDX-License-Identifier: MPL-2.0
"""Parser budget, escaping, comment and malformed-input tests."""

from __future__ import annotations

import unittest

import support

from floe_electronics.normalizers import sexpr


class ParseBasics(unittest.TestCase):
    def test_atoms_and_lists(self):
        root = sexpr.parse(b'(a "b" (c 1 -2.5e-3))')
        self.assertEqual(root.tag, "a")
        self.assertEqual(root.value_at(1), "b")
        inner = root.items[2]
        self.assertEqual(inner.tag, "c")
        self.assertEqual(inner.value_at(1), "1")
        self.assertEqual(inner.value_at(2), "-2.5e-3")

    def test_comments_are_skipped(self):
        root = sexpr.parse(
            b"; leading comment\n(a ; trailing\n (b 1) ; after\n)")
        self.assertEqual(root.tag, "a")
        self.assertEqual(root.child("b").value_at(1), "1")

    def test_string_escapes(self):
        root = sexpr.parse(rb'(s "quote:\" backslash:\\ nl:\n tab:\t cr:\r")')
        self.assertEqual(root.value_at(1),
                         'quote:" backslash:\\ nl:\n tab:\t cr:\r')

    def test_unicode_names_and_negative_coordinates(self):
        root = sexpr.parse(
            '(symbol "Ünïcode-Résistance 10kΩ" (at -1.016 -2.54 90))'.encode(
                "utf-8"))
        self.assertEqual(root.value_at(1), "Ünïcode-Résistance 10kΩ")
        at = root.child("at")
        self.assertEqual(at.value_at(1), "-1.016")
        self.assertEqual(at.value_at(2), "-2.54")

    def test_json_conversion_shape(self):
        value = sexpr.to_json_value(sexpr.parse(b'(a "x" (b 1))'))
        self.assertEqual(value["tag"], "a")
        self.assertEqual(value["items"][0], "a")
        self.assertEqual(value["items"][1], "x")
        self.assertEqual(value["items"][2]["tag"], "b")


class ParseRejections(unittest.TestCase):
    def test_empty(self):
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(b"")

    def test_blank_only(self):
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(b"   \n; nothing\n")

    def test_non_utf8(self):
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(b'(a "\xff\xfe")')

    def test_unclosed_list(self):
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(b"(a (b 1)")

    def test_unclosed_string(self):
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(b'(a "never closed')

    def test_unexpected_close(self):
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(b"a)")

    def test_trailing_garbage(self):
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(b"(a 1) (b 2)")

    def test_unsupported_escape(self):
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(rb'(a "\q")')

    def test_byte_budget(self):
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(b"(a " + b"x" * 64 + b")", max_bytes=32)

    def test_depth_budget(self):
        deep = b"(" * 40 + b"a" + b")" * 40
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(deep, max_depth=16)

    def test_node_budget(self):
        body = b" ".join(b"x" for _ in range(64))
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(b"(a " + body + b")", max_nodes=10)

    def test_token_budget(self):
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(b"(a " + b"x" * 100 + b")", max_token_bytes=32)

    def test_string_budget(self):
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(b'(a "' + b"x" * 100 + b'")', max_string_bytes=32)

    def test_truncated_kicad_library(self):
        data = support.read_bytes(support.fixture_path(
            "librepcb", "Broken.lplib", "sym",
            "55555555-5555-4555-8555-555555555555", "symbol.lp"))
        with self.assertRaises(sexpr.SExprParseError):
            sexpr.parse(data)


if __name__ == "__main__":
    unittest.main()
