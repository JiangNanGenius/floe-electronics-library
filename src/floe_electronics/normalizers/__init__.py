# SPDX-License-Identifier: MPL-2.0
"""Public API for the Floe electronics library normalizers.

Usage::

    from pathlib import Path
    from floe_electronics.normalizers import normalize

    for record in normalize(Path(root), "kicad-symbols", "kicad"):
        ...

``root`` must be a read-only source checkout.  Records are yielded as
JSON-serializable dicts (see ``docs/FORMAT.md`` and
``schemas/normalized-record.schema.json``).
"""

from __future__ import annotations

from . import common
from .kicad import normalize_kicad
from .librepcb import normalize_librepcb

_FORMATS = {
    "kicad": normalize_kicad,
    "librepcb": normalize_librepcb,
}

NormalizeError = common.NormalizeError

__all__ = ["normalize", "NormalizeError", "FORMAT_NAMES"]

FORMAT_NAMES = tuple(sorted(_FORMATS))


def normalize(root, source_id, format_name):
    """Yield normalized records for *root* (a read-only source checkout).

    :param root: source checkout directory (``pathlib.Path`` or path-like).
    :param source_id: stable upstream identifier (e.g. ``"kicad-symbols"``).
    :param format_name: ``"kicad"`` or ``"librepcb"``.
    :returns: iterator of JSON-serializable record dicts.
    :raises NormalizeError: for unknown formats or unsafe/invalid roots.
    """
    normalizer = _FORMATS.get(format_name)
    if normalizer is None:
        raise common.NormalizeError(
            "unknown format %r (expected one of %s)"
            % (format_name, ", ".join(FORMAT_NAMES)))
    checked = common.check_root(root)
    return normalizer(checked, source_id)
