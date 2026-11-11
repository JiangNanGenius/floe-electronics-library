# SPDX-License-Identifier: MPL-2.0
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from floe_electronics import models, sources


class ModelInventoryTests(unittest.TestCase):
    def test_git_tree_index_never_requests_blob_sizes_or_model_bytes(self):
        source = {"id": "fixture", "repository": "https://github.com/example/models.git", "commit": "a" * 40}
        listing = "100644 blob " + "b" * 40 + "\t3dmodels/part.STEP\x00" + "100644 blob " + "c" * 40 + "\tDevice.kicad_sym\x00"
        with patch.object(models, "git", return_value=listing) as git:
            rows = list(models.git_models(source, Path("unused")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gitBlobSHA1"], "b" * 40)
        self.assertFalse(rows[0]["modelBinaryBundled"])
        self.assertNotIn("-l", git.call_args.args[0])
        self.assertNotIn("sha256", rows[0])

    def test_unsafe_model_path_and_noncanonical_identity_are_rejected(self):
        for path, oid in [("../part.step", "b" * 40), ("part.step", "bad"), ("part\n.step", "b" * 40)]:
            with self.subTest(path=path), self.assertRaises(sources.SourceError):
                models.record("fixture", "https://github.com/example/models", "a" * 40, path, oid, "fixture")

    def test_git_symbolic_links_cannot_become_downloadable_models(self):
        source = {"id": "fixture", "repository": "https://github.com/example/models.git", "commit": "a" * 40}
        with patch.object(models, "git", return_value="120000 blob " + "b" * 40 + "\tpart.step\x00"):
            with self.assertRaisesRegex(sources.SourceError, "regular"):
                list(models.git_models(source, Path("unused")))

    def test_git_object_proof_is_distinct_from_file_sha256(self):
        data = b"ISO-10303-21;\nfixture\nEND-ISO-10303-21;\n"
        oid = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\x00" + data).hexdigest()
        row = models.record("fixture", "https://github.com/example/models", "a" * 40, "part.step", oid, "fixture")
        self.assertEqual(row["gitBlobSHA1"], oid)
        self.assertNotEqual(oid, hashlib.sha256(data).hexdigest())

    def gitlab_source(self):
        return {"id": "fixture-models", "repository": "https://gitlab.com/example/models.git", "commit": "a" * 40,
                "licenseSourceID": "fixture", "license": {"evidencePath": "LICENSE.md", "approvedTextSHA256": sources.digest(b"fixture license")}}

    def test_paginated_model_inventory_is_complete_without_fetching_models(self):
        entries = [{"path": f"parts/p{i:03d}.step", "type": "blob", "mode": "100644", "id": f"{i + 1:040x}"} for i in range(101)]
        calls = []
        def request(url, limit):
            calls.append(url)
            if url.endswith("LICENSE.md"):
                return b"fixture license", {}
            number = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"][0])
            values = entries[:100] if number == 1 else entries[100:]
            return json.dumps(values).encode(), {"X-Total-Pages": "2", "X-Total": "101", "X-Page": str(number)}
        with tempfile.TemporaryDirectory() as temp, patch.object(models, "http_bytes", side_effect=request):
            rows = list(models.gitlab_models(self.gitlab_source(), Path(temp)))
        self.assertEqual(len(rows), 101)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("/tree?" in url or url.endswith("LICENSE.md") for url in calls))

    def test_missing_tree_page_cannot_silently_publish_a_partial_model_catalog(self):
        entries = [{"path": f"parts/p{i:03d}.step", "type": "blob", "mode": "100644", "id": f"{i + 1:040x}"} for i in range(100)]
        def request(url, limit):
            if url.endswith("LICENSE.md"):
                return b"fixture license", {}
            number = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"][0])
            return json.dumps(entries if number == 1 else []).encode(), {"X-Total-Pages": "2", "X-Total": "101", "X-Page": str(number)}
        with tempfile.TemporaryDirectory() as temp, patch.object(models, "http_bytes", side_effect=request):
            with self.assertRaisesRegex(sources.SourceError, "count differs"):
                list(models.gitlab_models(self.gitlab_source(), Path(temp)))


if __name__ == "__main__":
    unittest.main()
