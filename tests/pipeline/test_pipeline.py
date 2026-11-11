# SPDX-License-Identifier: MPL-2.0
import copy
import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from floe_electronics import pipeline, sources


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.upstream = self.root / "upstream"
        (self.repo / "licenses").mkdir(parents=True)
        self.upstream.mkdir()
        (self.repo / "licenses" / "CC0-1.0.txt").write_text("fixture license")
        self.source_bytes = b'(kicad_symbol_lib (symbol "R"))\n'
        (self.upstream / "Device.kicad_sym").write_bytes(self.source_bytes)
        (self.upstream / "LICENSE.txt").write_text("fixture license")
        self.source = {"id": "fixture", "enabled": True, "repository": "https://github.com/example/fixture.git",
                       "format": "kicad", "tracking": {"kind": "stable-tag"},
                       "license": {"id": "CC0-1.0", "evidencePath": "LICENSE.txt", "extract": "full",
                                   "approvedTextSHA256": sources.digest(b"fixture license"),
                                   "attribution": "fixture", "url": "https://example.com/license"}}
        self.policy = {"schemaVersion": 1, "sources": [self.source]}
        (self.repo / "upstreams.json").write_bytes(sources.canonical(self.policy))
        self.lock = {"schemaVersion": 1, "policySHA256": sources.digest(sources.canonical(self.policy)),
                     "sources": [{k: self.source[k] for k in ("id", "repository", "format", "license")}]}
        self.lock["sources"][0].update(commit="a" * 40, ref="refs/tags/1.0.0")

    def records(self, root, sid, fmt):
        yield {"schemaVersion": 1, "id": "fixture:symbol:R", "kind": "symbol", "name": "R", "library": "Device",
               "source": {"path": "Device.kicad_sym", "sha256": sources.digest(self.source_bytes)},
               "units": "mm", "status": "converted", "issues": [], "properties": {}, "pins": [], "graphics": []}

    def build(self, name, normalizer=None):
        out = self.root / name
        return pipeline.build(self.policy, self.lock, self.repo, self.root / "cache", out, "b" * 40,
                              roots={"fixture": self.upstream}, normalizer=normalizer or self.records)

    def test_full_build_is_reproducible_and_binds_originals(self):
        first, second = self.build("one"), self.build("two")
        self.assertEqual(first, second)
        a = {p.relative_to(self.root / "one").as_posix(): p.read_bytes() for p in (self.root / "one").rglob("*") if p.is_file()}
        b = {p.relative_to(self.root / "two").as_posix(): p.read_bytes() for p in (self.root / "two").rglob("*") if p.is_file()}
        self.assertEqual(a, b)
        self.assertEqual(first["nativeAppIntegration"], "not-integrated")
        self.assertEqual(first["sources"][0]["statuses"], {"converted": 1})
        result = pipeline.bundle(self.root / "one", self.root / "release")
        self.assertTrue((self.root / "release" / "fixture-2d.zip").exists())
        self.assertEqual(result["assets"], 7)

    def test_license_change_refuses_candidate(self):
        (self.upstream / "LICENSE.txt").write_text("different terms")
        with self.assertRaisesRegex(sources.SourceError, "license changed"):
            self.build("bad")

    def test_empty_upstream_is_explicit_without_invented_parts(self):
        (self.upstream / "Device.kicad_sym").unlink()
        (self.upstream / "library.lp").write_text('(librepcb_library "fixture")')
        value = self.build("empty", lambda *_: iter(()))
        source = value["sources"][0]
        self.assertEqual(source["recordCount"], 0)
        self.assertEqual(source["sourceStatus"], "empty-upstream")

    def test_outer_release_asset_hashes_and_set_are_verified(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import publication
        self.build("release-data")
        release = self.root / "release-assets"
        pipeline.bundle(self.root / "release-data", release)
        publication.verify_assets(release)
        (release / "fixture-2d.zip").write_bytes(b"corrupt")
        with self.assertRaisesRegex(sources.SourceError, "asset changed"):
            publication.verify_assets(release)

    def test_public_readback_checks_every_asset_including_checksum_table(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import publication
        self.build("public-data")
        release = self.root / "public-assets"
        pipeline.bundle(self.root / "public-data", release)
        receipt = publication.verify_assets(release)
        metadata = {"tag_name": receipt["tag"], "draft": False, "prerelease": False,
                    "assets": [{"name": p.name, "browser_download_url": "https://example.invalid/" + p.name} for p in release.iterdir()]}
        def github(*args, **kwargs):
            value = {"object": {"type": "commit", "sha": "b" * 40}} if "/git/ref/" in args[1] else metadata
            return SimpleNamespace(stdout=json.dumps(value))
        def download(request, **kwargs):
            return io.BytesIO((release / request.full_url.rsplit("/", 1)[1]).read_bytes())
        with patch.object(publication, "gh", side_effect=github), patch.object(publication.urllib.request, "urlopen", side_effect=download) as fetch:
            publication.read_public(receipt)
            self.assertEqual(fetch.call_count, 7)
            (release / "release-SHA256SUMS").write_bytes(b"tampered")
            with self.assertRaisesRegex(sources.SourceError, "digest differs"):
                publication.read_public(receipt)

    def test_release_tag_must_resolve_to_the_producing_commit(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import publication
        with patch.object(publication, "gh", return_value=SimpleNamespace(stdout=json.dumps({"object": {"type": "commit", "sha": "c" * 40}}))):
            with self.assertRaisesRegex(sources.SourceError, "source binding"):
                publication.verify_tag({"tag": "data-v1-" + "a" * 20, "producerRevision": "b" * 40})

    def test_model_files_are_excluded_at_acquisition_boundary(self):
        (self.upstream / "R.step").write_text("not a real model")
        with self.assertRaisesRegex(sources.SourceError, "3D"):
            list(sources.source_files(self.upstream))

    def test_candidate_tag_creation_never_retargets_an_existing_ref(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import publication
        receipt = {"tag": "data-v1-" + "a" * 20, "producerRevision": "b" * 40}
        missing = SimpleNamespace(returncode=1, stdout=json.dumps({"status": "404"}))
        matched = SimpleNamespace(returncode=0, stdout=json.dumps({"object": {"type": "commit", "sha": "b" * 40}}))
        with patch.object(publication, "gh", side_effect=[missing, SimpleNamespace(returncode=0), matched]) as api:
            publication.ensure_candidate_tag(receipt)
            self.assertEqual(api.call_args_list[1].args[1:3], ("--method", "POST"))
            self.assertIn("sha=" + "b" * 40, api.call_args_list[1].args)
        with patch.object(publication, "gh", return_value=matched) as api:
            publication.ensure_candidate_tag(receipt)
            self.assertFalse(any("POST" in call.args for call in api.call_args_list))
        mismatch = SimpleNamespace(returncode=0, stdout=json.dumps({"object": {"type": "commit", "sha": "c" * 40}}))
        with patch.object(publication, "gh", return_value=mismatch) as api:
            with self.assertRaisesRegex(sources.SourceError, "source binding"):
                publication.ensure_candidate_tag(receipt)
            self.assertFalse(any("POST" in call.args for call in api.call_args_list))

    def test_symlink_and_unsafe_relative_paths_are_rejected(self):
        (self.upstream / "escape.txt").symlink_to(self.repo / "upstreams.json")
        with self.assertRaisesRegex(sources.SourceError, "symbolic"):
            list(sources.source_files(self.upstream))
        for value in ("../x", "/absolute", "a//b", "a/./b", "a\\b"):
            with self.subTest(value=value), self.assertRaises(sources.SourceError):
                sources.safe_path(value)

    def test_omitted_source_file_and_duplicate_records_refuse_publication(self):
        (self.upstream / "Other.kicad_sym").write_text('(kicad_symbol_lib (symbol "Other"))')
        with self.assertRaisesRegex(sources.SourceError, "silently omitted"):
            self.build("missing")
        (self.upstream / "Other.kicad_sym").unlink()
        def duplicate(*args):
            yield from self.records(*args)
            yield from self.records(*args)
        with self.assertRaisesRegex(sources.SourceError, "duplicate record"):
            self.build("duplicate", duplicate)

    def test_corrupt_or_extra_published_files_are_rejected(self):
        self.build("corrupt")
        path = self.root / "corrupt" / "catalog.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaisesRegex(sources.SourceError, "artifact changed"):
            pipeline.verify(path.parent)
        path.write_bytes(original)
        (path.parent / "unlisted.txt").write_text("unlisted")
        with self.assertRaisesRegex(sources.SourceError, "unlisted"):
            pipeline.verify(path.parent)

    def test_lock_cannot_retarget_origin_or_omit_source(self):
        changed = copy.deepcopy(self.lock)
        changed["sources"][0]["repository"] = "https://github.com/attacker/other.git"
        with self.assertRaisesRegex(sources.SourceError, "authority"):
            sources.verify_lock(self.policy, changed)
        changed["sources"] = []
        with self.assertRaisesRegex(sources.SourceError, "omitted"):
            sources.verify_lock(self.policy, changed)

    def test_kicad_sources_resolve_to_common_stable_tag(self):
        second = {**self.source, "id": "fixture-two", "releaseGroup": "pair"}
        first = {**self.source, "releaseGroup": "pair"}
        values = [{"refs/tags/1.0.0": "a" * 40, "refs/tags/1.1.0": "b" * 40, "refs/tags/1.2.0-rc1": "c" * 40},
                  {"refs/tags/1.0.0": "d" * 40}]
        with patch.object(sources, "remote_refs", side_effect=lambda s: values[0 if s["id"] == "fixture" else 1]):
            lock = sources.resolve({"schemaVersion": 1, "sources": [first, second]})
        self.assertTrue(all(s["ref"] == "refs/tags/1.0.0" for s in lock["sources"]))

    def test_baseline_regression_stops_update(self):
        old = {"sources": [{"id": "fixture", "recordCount": 100, "statuses": {"converted": 80}}]}
        new = copy.deepcopy(old)
        new["sources"][0]["recordCount"] = 94
        with self.assertRaisesRegex(sources.SourceError, "disappeared"):
            pipeline.compare_baseline(old, new)
        new = copy.deepcopy(old)
        new["sources"][0]["statuses"]["converted"] = 75
        with self.assertRaisesRegex(sources.SourceError, "regressed"):
            pipeline.compare_baseline(old, new)
        old["modelCount"] = 100
        new = copy.deepcopy(old)
        new["modelCount"] = 94
        with self.assertRaisesRegex(sources.SourceError, "model metadata"):
            pipeline.compare_baseline(old, new)


if __name__ == "__main__":
    unittest.main()
