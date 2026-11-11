# Floe normalized 2D electronics library format (schema v1)

This document defines the normalized exchange/catalog records produced by
`floe_electronics.normalizers.normalize()`.

It is **not** a native runtime package format. The records are a stable,
source-linked intermediate consumed by a future native adapter; the original
source bytes are packaged separately. Nothing here implies App/runtime
integration.

## Scope

- Upstream inputs: KiCad S-expression symbol libraries (packed `.kicad_sym`
  files and unpacked `<library>.kicad_symdir/*.kicad_sym`), KiCad footprint
  files (`.kicad_mod`, in `<library>.pretty/` or standalone), and LibrePCB
  `.lplib` libraries (`symbol.lp`, `package.lp`, `component.lp`, `device.lp`).
- Only 2D geometry and metadata are normalized. 3D model files are **never
  read, downloaded, or embedded**. Model references (file name, LibrePCB
  UUID, offset, rotation, scale) are retained as metadata only.
- The normalizer is pure Python standard library, performs no network access
  and executes no source content.

```python
from pathlib import Path
from floe_electronics.normalizers import normalize

for record in normalize(Path(root), "kicad-symbols", "kicad"):
    ...  # JSON-serializable dict
```

`format_name` is `"kicad"` or `"librepcb"`. `root` is a read-only checkout
directory. `normalize()` returns an iterator; records stream per source file
(KiCad) or per source library (unpacked KiCad symbol directories and
LibrePCB libraries, whose cross-file references must be resolved together).

## Units and coordinate transforms

All emitted geometry is millimetres (`units: "mm"`). Source transforms are
explicit and auditable:

| Source | Source unit | Factor to mm |
| --- | --- | --- |
| KiCad `.kicad_sym` (S-expression, modern) | millimetre | 1.0 |
| KiCad `.kicad_mod` | millimetre | 1.0 |
| KiCad legacy `.lib` | mil | **not supported** (not ingested) |
| LibrePCB `.lp` | millimetre | 1.0 |

Values are rounded to 9 decimal places.

### Corrected assumption (retained as evidence)

An earlier revision of this normalizer treated modern KiCad S-expression
symbol coordinates as mils and multiplied by `0.0254` (39.37x too small).
That assumption was wrong: per the KiCad file-format documentation
(<https://dev-docs.kicad.org/en/file-formats/sexpr-intro/#_coordinates_and_sizes>)
all coordinates and sizes in the S-expression formats are given in
millimetres. The pinned upstream files confirm it directly, e.g.
`Device.kicad_symdir/R.kicad_sym` stores property positions `2.032`, font
sizes `1.27`, rectangle corners `(-1.016, -2.54)`/`(1.016, 2.54)` and pins at
`(0, ±3.81)` with length `1.27` — all millimetres. Regression tests pin these
real upstream values (`test_kicad.KiCadMillimetreRegression`) and the identity
factor `common.KICAD_SYMBOL_MM == 1.0`; the failed assumption is recorded
here and in the constant's comment. Legacy mil-based `.lib` files are outside
scope and are not silently reinterpreted.

Model offsets/scales/rotations are retained **verbatim in their source-native
3D units** (KiCad historically uses inches for 3D offsets) and are not
converted; records mark this with `unitNote` on KiCad model references.

## Record shape

Required keys on every record (see
`schemas/normalized-record.schema.json`):

| Field | Type | Meaning |
| --- | --- | --- |
| `schemaVersion` | integer | Always `1`. |
| `id` | string | Stable opaque identity (see below). |
| `kind` | `symbol` / `footprint` / `component` | Normalized kind. |
| `name` | string | Source element name (exact, Unicode-preserving). |
| `library` | string | Library name (`library.lp` metadata, `.lplib`/`.pretty`/`.kicad_symdir` directory, or packed file stem). |
| `format` | `kicad` / `librepcb` | Source format. |
| `units` | string | Always `"mm"`. |
| `source` | object | `{"path": relative POSIX path, "sha256": SHA-256 of exact source file bytes}`. |
| `identifiers` | object | Source identities: `name`, plus LibrePCB `uuid` (may be `null`). |
| `status` | enum | `converted`, `reviewRequired`, `unsupported`. |
| `issues` | array of strings | Machine-readable issue codes (may be empty). |
| `properties` | object | Source metadata (properties, flags, attributes, `unitCount`, device `role`, ...). |

Optional keys, present when the source record has the corresponding content:

- `pins` — symbol pins / LibrePCB component signals (position, rotation
  degrees, length, electrical type, unit/style, alternates, inherited-from).
- `pads` — footprint pads (position, rotation, size, layers, drill, net,
  roundrect/chamfer attributes, custom primitives).
- `footprints` — LibrePCB package footprints (name, pads, graphics, model
  references).
- `graphics` — semantic 2D geometry: `rectangle`, `circle`, `arc`, `polyline`,
  `bezier`, `text`, `text_box`, `fp_line`, `fp_rect`, `fp_circle`, `fp_arc`,
  `fp_poly`, `fp_curve`, `fp_text`, `polygon`, `stroke_text`, `hole`, ...
  Coordinates are `*Mm` arrays; arcs keep `start`/`mid`/`end` (no center
  guessing); LibrePCB polygon vertices keep their `angleDeg` for arcs.
- `dependencies` — source dependency identities by role: `extends`,
  `categories`, `symbols`, `components`, `packages`, `signals`,
  `packagePads`, `padstacks`. Each entry has `id` and, when resolvable inside
  the same checkout, `resolvedPath`.
- `references` — structured cross-entity references copied from the source:
  LibrePCB `symbolVariants` (gates, source-provided pin→signal maps),
  `pinPadMap` (device pad→signal map, **never inferred**), `resources`
  (datasheet links).
- `modelReferences` — metadata-only 3D references. No bytes, no paths to
  binary content other than the source-declared file string.
- `unhandled` — verbatim JSON of source nodes this normalizer does not
  semantically decode. Presence forces `reviewRequired` and `unhandled-tag`.
- `raw` — verbatim JSON of the complete source element tree. Retained for
  every non-`converted` record (and every record with `unhandled` nodes) so
  unknowns stay auditable; fully converted records carry decoded geometry and
  source hashes instead (original bytes are packaged separately).

## Stable ids

```
floe:electronics:<source>:<kind>:<library-slug>:<name-slug>~<discriminator>
```

- Readable segments (`source`, `kind`, `library-slug`, `name-slug`) use a
  conservative slug; slugs are **lossy** and are not the identity.
- `discriminator` is the identity:
  - LibrePCB: the intrinsic source UUID (lowercase). A UUID defined in more
    than one file (corrupt source) gets `-<path-digest>` added for every copy
    and is flagged `duplicate-source-identity`.
  - KiCad: a SHA-256-derived digest of the exact source path **and** the
    exact unsanitized element name. This keeps distinct packed-file symbols
    such as `A+B` and `A B` (same slug) distinct, and is independent of file
    iteration and document order.
  - KiCad records without a source element name (file-level placeholders,
    parse errors) use the same path+name digest.
- Consequence: ids do not change when new source files are inserted, when
  document order changes, or when a new duplicate name appears earlier in
  sort order. Identical source bytes at two paths are two identities.
- `assign_ids()` is only a safety net. A residual collision (pathological
  byte-identical duplicates) gets a content digest suffix + numeric suffix,
  `duplicate-id`, and `reviewRequired`. Exact duplicate source identities are
  allowed to remain `reviewRequired`; they are never silently merged.

Consumers must treat ids as opaque strings. Ids do not encode native runtime
identity and do not imply EDALibraryPackage compatibility.

## Status and issues

- `converted` — every source node was decoded or classified as known
  metadata. No `unhandled` nodes; no unresolved structural dependency.
- `reviewRequired` — conversion succeeded but something needs review:
  custom pad primitives, copper zones/groups in footprints, unresolved
  structural dependency, duplicate source name/identity, missing UUID,
  invalid/out-of-range numbers.
- `unsupported` — cannot be used as-is: malformed/truncated input, invalid
  root element, cyclic inheritance, unresolved inheritance target, no
  symbols in a library file.

Issue codes (prefix before `:` where parameterised):

`parse-error`, `read-error`, `invalid-root`, `no-symbols`, `missing-uuid`,
`invalid-number`, `cyclic-inheritance`, `unresolved-inheritance`,
`unresolved-dependency:<role>:<uuid>`, `unhandled-tag`,
`custom-pad-geometry`, `duplicate-source-name`, `duplicate-source-identity`,
`duplicate-id`.

Categories are classification metadata that commonly references other
libraries; unresolved category UUIDs are recorded without forcing review.
Structural references (components, packages, symbols, signals, package pads,
padstacks) do force review when unresolved.

## KiCad mapping notes

- Packed `.kicad_sym` files may contain many symbols; unpacked
  `.kicad_symdir` directories in current upstream sources contain **one
  symbol per `.kicad_sym` file**. Derived symbols (`extends`) are resolved
  across all files of the same library directory (real upstream case, e.g.
  `4xxx.kicad_symdir/14528.kicad_sym` extends `4538`); the dependency records
  the resolved sibling path. Same-file inheritance (packed libraries) works
  identically. Cycles and missing targets make the record `unsupported`.
- Inherited pins/graphics are flattened and tagged
  `inheritedFrom: <base symbol>`; nothing is dropped and the child's own
  geometry is appended after the inherited geometry.
- Unit sub-symbols (`<name>_<unit>_<style>`) are flattened with `unit` and
  `style` indices; `properties.unitCount` records the highest unit index.
- Pin `orientation`/`orientationDeg` follows the source rotation and the
  legacy R/U/L/D letters when present. Pin `alternate` definitions are kept in
  `pins[].alternates`.
- Footprint `.kicad_mod` root may be `(footprint "Name" ...)` (KiCad 6+) or
  legacy `(module Name ...)`; both are handled. `fp_text`/properties,
  pads and board graphics are decoded. Custom pads (`shape custom`) keep their
  `primitives` verbatim and set `custom-pad-geometry` + `reviewRequired`.
- Footprint `zone`/`group` nodes are not semantically decoded; they remain in
  `unhandled` (conservative, visible) with `reviewRequired`. Other modern
  metadata (`embedded_fonts`, `duplicate_pad_numbers_are_jumpers`,
  `clearance`, `solder_paste_*`, `private_layers`, `tedit`, ...) is retained
  in `properties`.
- 3D `(model ...)` entries keep `file`, `offset`, `scale`, `rotationDeg` and
  unhandled extra nodes; the referenced files are never opened.

## LibrePCB mapping notes

- Entity roots: `librepcb_symbol`, `librepcb_package`, `librepcb_component`,
  `librepcb_device`. Kind mapping: symbol→`symbol`, package→`footprint`,
  component→`component`, device→`component` with
  `properties.role == "device"` (the catalog has three kinds; the UUID keeps
  devices distinct from same-named components).
- Library name comes from the library's own `library.lp` `(name ...)`
  metadata, then a `.lplib` directory, then the first relative path segment —
  never from an absolute checkout path or commit-hash directory name.
- Coordinates in `.lp` files are already millimetres. Pins, polygons
  (with per-vertex arc angles), circles, texts, package pads, footprint pads
  (side, shape, position, rotation, size, radius, stop/paste mask,
  clearance, function, package-pad reference), holes and 3D model placements
  are decoded.
- UUID dependencies are resolved with a first streaming pass over all `.lp`
  files. Definitions are indexed, not references, so a reference in one file
  cannot shadow an entity defined in another. Dependencies keep `id` and
  `resolvedPath`; unresolved structural references are flagged.
- Component signals → `pins`; variants/gates → `references.symbolVariants`
  including the source-provided pin→signal map. Device pads →
  `references.pinPadMap` (`padUuid`, `signalUuid`, `optional`). These maps are
  copied from the source and are never guessed.
- Component `attribute`, package `alternative_name` and `approved` markers
  are decoded into `properties`; device `resource` (datasheet URL) is decoded
  into `references.resources`. `resource` nodes found at component level are
  retained verbatim as `unhandled` and keep the record `reviewRequired`.
- A checkout containing only `library.lp`/categories (no symbol/package/
  component/device file) yields zero records; the packaging layer labels it
  `empty-upstream`. No placeholder records are fabricated.

## Bounds, safety, coverage, determinism

- Parser budgets: 32 MiB per file, depth 96, 1,000,000 nodes, 1 MiB per
  token, 1 MiB per string. Empty input, non-UTF-8, truncated expressions,
  trailing data, unsupported escapes and budget exhaustion raise
  `SExprParseError`; the affected file still yields one `unsupported` record
  so every recognised source file is accounted for.
- `normalize()` rejects unsafe roots: non-directory roots raise
  `NormalizeError`; symlinked or root-escaping source paths are rejected
  fail-closed. Inputs are read-only; nothing is written, executed or fetched.
- Every recognised source file appears at least once as `record.source.path`,
  including unsupported/empty parses. `source.sha256` hashes the exact
  original bytes. Records never embed 3D binaries.
- Deterministic output: files are enumerated in sorted relative-path order;
  per-library symbol groups and per-file records are sorted by
  `(source.path, name, status, id)`; ids are intrinsic (see above). Repeated
  runs and different file-creation orders produce byte-identical JSON.
- Memory is bounded per source file or per symbol/`.lplib` library; the whole
  corpus is never held in memory.

## Validation

```sh
# Focused stdlib tests (Python 3.12):
/opt/homebrew/bin/python3.12 -m unittest discover -s tests/normalizers -v

# Real 52-upstream corpus validation (coverage, statuses, 3D absence,
# optional rerun determinism), writes JSONL + report.json:
/opt/homebrew/bin/python3.12 tests/normalizers/real_corpus_check.py \
    --lock upstreams.lock.json \
    --out "$TMPDIR/floe-normalized" --determinism
```

The corpus runner ignores `lock.modelSources` (metadata-only 3D inventory),
uses only `lock.sources`, and fails if a recognised source file has no
record, if any 3D binary is emitted, or if a source normalizes
non-deterministically.

## Known limitations / remaining review surface

- Copper `zone` and `group` nodes in KiCad footprints, and component-level
  LibrePCB `resource` nodes, are preserved verbatim but not decoded
  (`reviewRequired`).
- Custom pad primitives are preserved verbatim but not converted to native
  outline geometry (`reviewRequired`).
- Legacy KiCad `.lib` (mil) libraries, legacy `.mod` and non-S-expression
  formats are out of scope.
- Text glyph metrics/effects are retained as source JSON; no font shaping is
  performed.
- Free-angle arcs in KiCad symbols keep `start`/`mid`/`end`; center/radius
  derivation is left to the consumer.
- LibrePCB checkouts that are sparse and lack referenced component/package
  files surface `unresolved-dependency`; this is truthful, not repaired by
  guessing.
- These records are candidate catalog data; they are not installed-App
  acceptance and do not claim runtime/native package compatibility.
