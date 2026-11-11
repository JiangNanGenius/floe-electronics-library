# SPDX-License-Identifier: MPL-2.0
"""Floe electronics normalized 2D component-library exchange format.

This package converts read-only upstream electronics library checkouts
(KiCad symbol/footprint libraries, LibrePCB ``.lplib`` libraries) into a
single deterministic, JSON-serializable catalog schema.

Only 2D data and metadata are normalized. 3D model files are never read,
downloaded, or packaged; model references (file names, UUIDs, offsets,
rotations, scales) are retained as metadata only.

This is a normalized exchange/catalog schema consumed by a future native
adapter.  It does not claim compatibility with any native runtime package
format.
"""
