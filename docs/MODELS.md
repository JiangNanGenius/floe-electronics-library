# Individually downloadable 3D model inventory

`catalog.modelIndex` identifies the standalone `models.jsonl.gz` Release asset
by path, byte count and SHA-256. `catalog.modelCount` counts its JSONL rows.
The same inventory is included in the full 2D ZIP as small metadata; model
binaries are never included. `release.json` independently binds the public asset.

Each row has this shape:

```json
{"schemaVersion":1,"id":"model:espressif:3dmodels/espressif.3dshapes/ESP32-C3-MINI-1.STEP","kind":"model3d","sourceID":"espressif","repository":"https://github.com/espressif/kicad-libraries","revision":"1dfc3110895c9cd62daf332f49c49ee0ee200831","path":"3dmodels/espressif.3dshapes/ESP32-C3-MINI-1.STEP","gitBlobSHA1":"6aa9d3af806809f8b40636bfcdaf1c49c2162709","format":"step","licenseSourceID":"espressif","modelBinaryBundled":false,"verification":"git-source-object; SHA-256 computed after download"}
```

Byte count and model-file SHA-256 are not fabricated when CI has not downloaded
the binary. Clients compute a local SHA-256 after verifying the actual Git object:

```
SHA1("blob " + decimalByteCount + NUL + originalFileBytes) == gitBlobSHA1
```

This is Git source-object identity, not an Ed25519 signature or a claim that
CI already validated the model's geometry, units, appearance or suitability.
Source identity and the index's SHA-256 are obtained from the trusted publication
before selecting a byte mirror. Files are promoted only after exact-object and
local integrity verification; failed downloads cannot replace a usable cache.

GitHub source files resolve to `raw.githubusercontent.com/<owner>/<repo>/<revision>/<path>`.
GitLab source files resolve to `<repository>/-/raw/<revision>/<path>`.
Encode each path segment; do not expand arbitrary environment variables or accept
a model-authored endpoint. Callers choose the exact returned asset ID. A filename
match alone does not establish physical compatibility or permission to attach it
to a different footprint.

The official KiCad model tree follows the same stable tag as the 2D KiCad
sources. CI reads bounded GitLab tree metadata and rechecks the model repository
license. Other enabled upstream model objects come from `git ls-tree` on their
already pinned partial clones. The command deliberately omits `-l`, which would
force Git to retrieve blob content just to ask its size.

Floe's App adapter reuses the existing GitHub transport mirror roles and policy
store. Mirrors can supply bytes; they cannot change publication identity, file
hashes, approved source origins, grants, or cancellation ownership. Existing
GitHub-only mirrors must not be represented as supporting GitLab without evidence.

For the exact official KiCad GitLab repository, a client may use the compiled
transport alias `https://raw.githubusercontent.com/KiCad/kicad-packages3D/`
with the **same immutable revision and path**, then the existing `rawFiles`
mirror policy. The GitLab source-object identity remains authoritative. The
GitHub repository is archived, so availability of later commits must not be
assumed. The alias cannot select a different version, substitute a filename, or
bypass Git-blob verification; authentication, certificate and integrity failures
are not reasons to try an untrusted source.
