# SPDX-License-Identifier: MPL-2.0
"""LibrePCB ``.lplib`` library normalizer (symbol/package/component/device).

LibrePCB libraries are directories of UUID-keyed entities::

    <library>.lplib/
      sym/<uuid>/symbol.lp
      pkg/<uuid>/package.lp          (+ optional <uuid>.step 3D files)
      cmp/<uuid>/component.lp
      dev/<uuid>/device.lp

Each entity file is an s-expression whose root tag is one of
``librepcb_symbol``, ``librepcb_package``, ``librepcb_component`` or
``librepcb_device``.  Coordinates in ``.lp`` files are millimeters.

Devices are emitted as ``kind: "component"`` records with
``properties.role == "device"`` so the catalog keeps three kinds; the
source UUID and the component/package UUIDs keep the identities distinct.

UUID dependencies are resolved against a lightweight index built by a
first streaming pass over the checkout; every dependency keeps its source
UUID and either a resolved relative path or an ``unresolved-dependency``
issue.
"""

from __future__ import annotations

import os

from . import common
from .sexpr import SExprParseError, parse, to_json_value

FORMAT_NAME = "librepcb"

_LP_SUFFIX = ".lp"
_ROOT_TAGS = {
    "librepcb_symbol": common.KIND_SYMBOL,
    "librepcb_package": common.KIND_FOOTPRINT,
    "librepcb_component": common.KIND_COMPONENT,
    "librepcb_device": common.KIND_COMPONENT,
}
_LIB_MARKERS = (".librepcb-lib",)


def normalize_librepcb(root, source_id):
    """Yield normalized records for one LibrePCB source checkout."""
    files = [(rel, path) for rel, path in
             common.iter_source_files(root, (_LP_SUFFIX,))
             if os.path.basename(rel) in
             ("symbol.lp", "package.lp", "component.lp", "device.lp")]
    uuid_index, duplicate_uuids = _build_uuid_index(root, files)
    library_names = {}
    seen_ids = {}
    for relpath, abspath in files:
        source = common.source_ref(relpath, abspath)
        records = _records_for_file(relpath, abspath, source, source_id,
                                    root, uuid_index, duplicate_uuids,
                                    library_names)
        common.assign_ids(records, seen_ids)
        for record in records:
            yield common.strip_empty(record)


_DEFINITION_TAGS = {
    "variant", "gate", "polygon", "circle", "text", "stroke_text", "hole",
    "footprint",
}


def _is_definition(node):
    """True when *node* declares an entity rather than referencing one.

    Reference nodes such as ``(symbol <uuid>)``, ``(component <uuid>)`` or
    device ``(pad <uuid> (signal ...))`` must not enter the index, otherwise
    a reference in one file could shadow the entity defined in another.
    """
    tag = node.tag
    if tag in _ROOT_TAGS:
        return True
    if tag in _DEFINITION_TAGS:
        return True
    if tag == "signal":
        return node.child("name") is not None
    if tag == "pad":
        return any(node.child(key) is not None
                   for key in ("name", "side", "position", "shape"))
    if tag == "3d_model":
        return node.child("name") is not None
    return False


def _build_uuid_index(root, files):
    """First pass: map definition UUID -> (relpath, node tag).

    Each ``.lp`` file is parsed exactly once and discarded, so memory stays
    bounded by one file at a time.  Duplicate detection happens inside the
    same traversal: a UUID defined twice in one file, or defined in more than
    one file, is recorded in *duplicates* so those records receive
    order-independent path suffixes.  Unreadable or malformed files are
    simply absent; the second pass reports them.
    """
    index = {}
    duplicates = set()
    for relpath, abspath in files:
        try:
            with open(abspath, "rb") as handle:
                data = handle.read()
            node = parse(data)
        except (SExprParseError, OSError, ValueError):
            continue
        local = set()
        _index_uuids(node, relpath, index, duplicates, local)
    return index, duplicates


def _index_uuids(node, relpath, index, duplicates, local):
    """Index definition UUIDs in one linear traversal of a file's AST."""
    if not node.is_list:
        return
    if len(node.items) >= 2 and node.items[1].is_atom and \
            common.is_uuid(node.items[1].value):
        uuid = node.items[1].value.lower()
        if _is_definition(node):
            if uuid in local:
                duplicates.add(uuid)          # duplicated within this file
            local.add(uuid)
            existing = index.get(uuid)
            if existing is None:
                index[uuid] = (relpath, node.tag or "")
            elif existing[0] != relpath:
                duplicates.add(uuid)          # duplicated across files
    for child in node.items:
        if child.is_list:
            _index_uuids(child, relpath, index, duplicates, local)


def _root_uuid(node):
    if node.tag is None or not node.is_list:
        return None
    if len(node.items) >= 2 and node.items[1].is_atom:
        candidate = node.items[1].value
        if common.is_uuid(candidate):
            return candidate.lower()
    child = node.child("uuid")
    if child is not None:
        candidate = child.value_at(1)
        if candidate and common.is_uuid(candidate):
            return candidate.lower()
    return None


def _library_name(root, relpath, cache):
    """Library display name: ``library.lp`` metadata, else ``.lplib`` dir.

    Reading the name from the library's own ``library.lp`` keeps ids stable
    even when the checkout directory is named after a commit hash.
    """
    directories = [root]
    for part in relpath.split("/")[:-1]:
        directories.append(os.path.join(directories[-1], part))
    for directory in reversed(directories):
        if directory in cache:
            name = cache[directory]
        else:
            name = _library_metadata_name(directory)
            cache[directory] = name
        if name:
            return name
    parts = relpath.split("/")[:-1]
    for depth in range(len(parts) - 1, -1, -1):
        name = parts[depth]
        if name.endswith(".lplib"):
            return name[: -len(".lplib")]
    # Never derive a library name from the absolute checkout location (it
    # may be a commit hash or a temp directory): use the first relative
    # segment, treating entity directories as an unnamed library root.
    parts = relpath.split("/")[:-1]
    if parts and parts[0] not in ("sym", "pkg", "cmp", "dev"):
        return parts[0]
    return "library"


def _library_metadata_name(directory):
    path = os.path.join(directory, "library.lp")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            node = parse(handle.read())
    except (SExprParseError, OSError, ValueError):
        return None
    if node.tag != "librepcb_library":
        return None
    return _entity_name(node)


def _records_for_file(relpath, abspath, source, source_id, root,
                      uuid_index, duplicate_uuids=frozenset(),
                      library_names=None):
    if library_names is None:
        library_names = {}
    library = _library_name(root, relpath, library_names)
    try:
        with open(abspath, "rb") as handle:
            data = handle.read()
        node = parse(data)
    except SExprParseError as exc:
        return [_error_record(relpath, source, source_id, library,
                              "parse-error:%s" % exc,
                              common.KIND_COMPONENT)]
    except OSError as exc:
        return [_error_record(relpath, source, source_id, library,
                              "read-error:%s" % exc,
                              common.KIND_COMPONENT)]

    kind = _ROOT_TAGS.get(node.tag)
    if kind is None:
        return [_error_record(relpath, source, source_id, library,
                              "invalid-root:%s" % (node.tag,),
                              common.KIND_COMPONENT,
                              raw=to_json_value(node))]

    uuid = _root_uuid(node)
    name = _entity_name(node)
    if name is None:
        name = os.path.basename(os.path.dirname(relpath)) or \
            os.path.splitext(os.path.basename(relpath))[0]

    if uuid:
        discriminator = uuid
        if uuid in duplicate_uuids:
            # Corrupt duplicated source identity: all copies get a stable
            # per-file suffix so no id depends on file iteration order.
            discriminator = "%s-%s" % (uuid,
                                       common.identity_digest(relpath))
    else:
        discriminator = "x" + common.identity_digest(relpath)
    record = common.base_record(FORMAT_NAME, source_id, kind, library, name,
                                source, discriminator=discriminator)
    record["identifiers"]["uuid"] = uuid
    record["identifiers"]["name"] = name
    if uuid is None:
        record["issues"].append("missing-uuid")
        record["status"] = common.STATUS_REVIEW
    elif uuid in duplicate_uuids:
        record["issues"].append("duplicate-source-identity")
        record["status"] = common.STATUS_REVIEW

    _metadata(node, record)
    if node.tag == "librepcb_symbol":
        _symbol_content(node, record)
    elif node.tag == "librepcb_package":
        _package_content(node, record, uuid_index)
    elif node.tag == "librepcb_component":
        _component_content(node, record, uuid_index)
    else:
        record["properties"]["role"] = "device"
        _device_content(node, record, uuid_index)

    _collect_structural_deps(record)
    _resolve_dependencies(record, uuid_index, relpath)

    if record["status"] != common.STATUS_CONVERTED:
        record["raw"] = to_json_value(node)
    return [record]


def _error_record(relpath, source, source_id, library, issue, kind, raw=None):
    stem = os.path.splitext(os.path.basename(relpath))[0]
    if stem in ("symbol", "package", "component", "device"):
        name = os.path.basename(os.path.dirname(relpath)) or stem
    else:
        name = stem
    record = common.base_record(
        FORMAT_NAME, source_id, kind, library, name, source,
        discriminator="x" + common.identity_digest(relpath))
    record["status"] = common.STATUS_UNSUPPORTED
    record["issues"].append(issue)
    if raw is not None:
        record["raw"] = raw
    return record


def _entity_name(node):
    name_node = node.child("name")
    if name_node is None:
        return None
    value = name_node.value_at(1)
    if value is not None:
        return value
    inner = name_node.child("value")
    if inner is not None:
        return inner.value_at(1)
    return None


def _metadata(node, record):
    scalar_tags = {
        "description", "keywords", "author", "version", "created",
        "generated_by", "deprecated", "grid_interval", "assembly_type",
        "min_copper_clearance", "schematic_only", "default_value",
        "prefix", "nominal_voltage", "elected_series",
    }
    for child in node.items[1:]:
        if not child.is_list or child.tag in _ROOT_TAGS:
            continue
        if child.tag == "name":
            continue
        if child.tag == "category":
            uuid = _atom_uuid(child, 1)
            record["dependencies"].setdefault("categories", []).append(
                {"id": uuid} if uuid else {"id": to_json_value(child)})
        elif child.tag in scalar_tags:
            record["properties"][child.tag] = \
                common.atomic_scalar(child)
        # remaining tags are handled by the per-kind content extractor


def _atom_uuid(node, index=1):
    value = node.value_at(index)
    if value and common.is_uuid(value):
        return value.lower()
    return None


def _dep(record, role, uuid, uuid_index):
    entry = {"id": uuid}
    if uuid in uuid_index:
        entry["resolvedPath"] = uuid_index[uuid][0]
    else:
        issue = "unresolved-dependency:%s:%s" % (role, uuid)
        if issue not in record["issues"]:
            record["issues"].append(issue)
        if record["status"] == common.STATUS_CONVERTED:
            record["status"] = common.STATUS_REVIEW
    record["dependencies"].setdefault(role, []).append(entry)


# ---------------------------------------------------------------------------
# Symbol
# ---------------------------------------------------------------------------

def _symbol_content(node, record):
    record["pins"] = []
    record["graphics"] = []
    unhandled = []
    for child in node.items[1:]:
        if not child.is_list:
            continue
        if child.tag == "pin":
            record["pins"].append(_symbol_pin(child))
        elif child.tag in ("polygon", "circle", "text"):
            record["graphics"].append(_lp_graphic(child))
        elif child.tag == "approved":
            _approved(child, record)
        elif child.tag in ("name", "category", "description", "keywords",
                           "author", "version", "created", "generated_by",
                           "deprecated", "grid_interval"):
            continue
        else:
            unhandled.append(to_json_value(child))
    if unhandled:
        record["unhandled"] = unhandled
        record["issues"].append("unhandled-tag")
        if record["status"] == common.STATUS_CONVERTED:
            record["status"] = common.STATUS_REVIEW


def _approved(node, record):
    """LibrePCB ``(approved <reason> [(<kind> <uuid>...)])`` markers."""
    entry = {"reason": node.value_at(1)}
    references = []
    for child in node.items[2:]:
        if child.is_list:
            references.append({
                "kind": child.tag,
                "id": _atom_uuid(child, 1),
            })
    if references:
        entry["references"] = references
    record["properties"].setdefault("approved", []).append(entry)


def _symbol_pin(node):
    issues = []
    pin = {
        "uuid": _atom_uuid(node, 1),
        "name": (node.child("name") or _NONE).value_at(1),
    }
    for key, target, numeric in (
            ("position", "positionMm", True),
            ("rotation", "rotationDeg", False),
            ("length", "lengthMm", True),
            ("name_position", "namePositionMm", True),
            ("name_rotation", "nameRotationDeg", False),
            ("name_height", "nameHeightMm", True)):
        child = node.child(key)
        if child is None:
            continue
        if numeric:
            pin[target] = [
                common.number_or_raw(child, 1, common.LIBREPCB_MM, issues,
                                     "pin." + key),
                common.number_or_raw(child, 2, common.LIBREPCB_MM, issues,
                                     "pin." + key),
            ] if key in ("position", "name_position") else \
                common.number_or_raw(child, 1, common.LIBREPCB_MM, issues,
                                     "pin." + key)
        else:
            value, error = common.parse_number(child.value_at(1))
            pin[target] = value if error is None else child.value_at(1)
    align = node.child("name_align")
    if align is not None:
        pin["nameAlign"] = " ".join(
            item.value for item in align.items[1:] if item.is_atom)
    if issues:
        pin["issues"] = issues
    return pin


_NONE = type("NoneNode", (), {"value_at": lambda self, index: None})()


def _lp_graphic(node):
    tag = node.tag
    issues = []
    graphic = {"type": tag, "uuid": _atom_uuid(node, 1)}
    layer = node.child("layer")
    if layer is not None:
        graphic["layer"] = layer.value_at(1)
    for key, target, numeric in (
            ("width", "widthMm", True),
            ("height", "heightMm", True),
            ("diameter", "diameterMm", True),
            ("stroke_width", "strokeWidthMm", True),
            ("rotation", "rotationDeg", False),
            ("angle", "angleDeg", False),
            ("radius", "radiusMm", True),
            ("lock", "lock", False),
            ("fill", "fill", False),
            ("grab_area", "grabArea", False),
            ("mirror", "mirror", False),
            ("auto_rotate", "autoRotate", False),
            ("letter_spacing", "letterSpacing", False),
            ("line_spacing", "lineSpacing", False)):
        child = node.child(key)
        if child is None:
            continue
        if numeric:
            graphic[target] = common.number_or_raw(
                child, 1, common.LIBREPCB_MM, issues, tag + "." + key)
        else:
            value = common.atomic_scalar(child)
            graphic[target] = value if value is not None \
                else to_json_value(child)
    position = node.child("position")
    if position is not None:
        graphic["positionMm"] = [
            common.number_or_raw(position, 1, common.LIBREPCB_MM, issues,
                                 tag + ".position"),
            common.number_or_raw(position, 2, common.LIBREPCB_MM, issues,
                                 tag + ".position"),
        ]
    if tag == "polygon":
        vertices = []
        for vertex in node.children("vertex"):
            entry = {
                "positionMm": [
                    common.number_or_raw(vertex, 1, common.LIBREPCB_MM,
                                         issues, "vertex"),
                    common.number_or_raw(vertex, 2, common.LIBREPCB_MM,
                                         issues, "vertex"),
                ]
            }
            angle = vertex.child("angle")
            if angle is not None:
                value, error = common.parse_number(angle.value_at(1))
                entry["angleDeg"] = value if error is None \
                    else angle.value_at(1)
            vertices.append(entry)
        graphic["vertices"] = vertices
    if tag in ("text", "stroke_text"):
        value_node = node.child("value")
        if value_node is not None:
            graphic["value"] = value_node.value_at(1)
        align = node.child("align")
        if align is not None:
            graphic["align"] = " ".join(
                item.value for item in align.items[1:] if item.is_atom)
    if issues:
        graphic["issues"] = issues
    return graphic


# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------

def _package_content(node, record, uuid_index):
    record["pads"] = []           # package-level pad identities
    record["footprints"] = []
    record["modelReferences"] = []
    unhandled = []

    for child in node.items[1:]:
        if not child.is_list:
            continue
        if child.tag == "pad":
            pad = {"uuid": _atom_uuid(child, 1),
                   "name": (child.child("name") or _NONE).value_at(1)}
            record["pads"].append(pad)
        elif child.tag == "3d_model":
            record["modelReferences"].append(_lp_3d_model(child))
        elif child.tag == "footprint":
            record["footprints"].append(_lp_footprint(child))
        elif child.tag == "alternative_name":
            record["properties"].setdefault("alternativeNames", []).append({
                "name": child.value_at(1),
                "reference": (child.child("reference") or _NONE).value_at(1),
            })
        elif child.tag == "approved":
            _approved(child, record)
        elif child.tag in ("name", "category", "description", "keywords",
                           "author", "version", "created", "generated_by",
                           "deprecated", "grid_interval", "assembly_type",
                           "min_copper_clearance"):
            continue
        else:
            unhandled.append(to_json_value(child))

    if unhandled:
        record["unhandled"] = unhandled
        record["issues"].append("unhandled-tag")
        if record["status"] == common.STATUS_CONVERTED:
            record["status"] = common.STATUS_REVIEW


def _lp_3d_model(node):
    model = {"uuid": _atom_uuid(node, 1)}
    name = node.child("name")
    if name is not None:
        model["name"] = name.value_at(1)
    for key, target in (("3d_position", "offsetMm"),
                        ("3d_rotation", "rotationDeg"),
                        ("3d_scale", "scale")):
        child = node.child(key)
        if child is None:
            continue
        values = []
        for index in (1, 2, 3):
            raw = child.value_at(index)
            value, error = common.parse_number(raw)
            values.append(value if error is None else raw)
        model[target] = values
    return model


def _lp_footprint(node):
    issues = []
    footprint = {
        "uuid": _atom_uuid(node, 1),
        "name": (node.child("name") or _NONE).value_at(1),
    }
    description = node.child("description")
    if description is not None:
        footprint["description"] = description.value_at(1)
    tags = [item.value_at(1) for item in node.children("tag")
            if item.value_at(1) is not None]
    if tags:
        footprint["tags"] = tags
    footprint["pads"] = []
    footprint["graphics"] = []
    footprint["modelReferences"] = []
    transform = {}
    for key in ("3d_position", "3d_rotation", "3d_scale"):
        child = node.child(key)
        if child is None:
            continue
        values = []
        for index in (1, 2, 3):
            raw = child.value_at(index)
            value, error = common.parse_number(raw)
            values.append(value if error is None else raw)
        transform[key] = values
    unhandled = []
    for child in node.items[1:]:
        if not child.is_list:
            continue
        if child.tag == "pad":
            footprint["pads"].append(_lp_pad(child, issues))
        elif child.tag in ("polygon", "circle", "stroke_text", "text",
                           "hole", "arc"):
            footprint["graphics"].append(_lp_graphic(child))
        elif child.tag == "3d_model":
            model = _lp_3d_model(child)
            # Footprint-level transforms apply to every referenced model.
            if "3d_position" in transform:
                model["offsetMm"] = transform["3d_position"]
            if "3d_rotation" in transform:
                model["rotationDeg"] = transform["3d_rotation"]
            if "3d_scale" in transform:
                model["scale"] = transform["3d_scale"]
            footprint["modelReferences"].append(model)
        elif child.tag in ("name", "description", "tag", "3d_position",
                           "3d_rotation", "3d_scale"):
            continue
        else:
            unhandled.append(to_json_value(child))
    if unhandled:
        footprint["unhandled"] = unhandled
        issues.append("unhandled-tag")
    if issues:
        footprint["issues"] = issues
    return footprint


def _lp_pad(node, issues):
    pad = {"uuid": _atom_uuid(node, 1)}
    for index, key in ((2, "side"), (3, "shape")):
        value = node.value_at(index)
        if value is not None and not common.is_uuid(value):
            pad[key] = value
    for key, target, numeric in (
            ("position", "positionMm", True),
            ("rotation", "rotationDeg", False),
            ("size", "sizeMm", True),
            ("radius", "radiusMm", True),
            ("drill", "drillMm", True),
            ("clearance", "clearanceMm", True),
            ("stop_mask", "stopMask", False),
            ("solder_paste", "solderPaste", False),
            ("function", "function", False),
            ("package_pad", "packagePadUuid", False),
            ("padstack", "padstackUuid", False)):
        child = node.child(key)
        if child is None:
            continue
        if numeric:
            if key == "position" or key == "size":
                pad[target] = [
                    common.number_or_raw(child, 1, common.LIBREPCB_MM,
                                         issues, "pad." + key),
                    common.number_or_raw(child, 2, common.LIBREPCB_MM,
                                         issues, "pad." + key),
                ]
            else:
                pad[target] = common.number_or_raw(
                    child, 1, common.LIBREPCB_MM, issues, "pad." + key)
        else:
            value = child.value_at(1)
            if key in ("package_pad", "padstack") and value is not None:
                pad[target] = value.lower() \
                    if common.is_uuid(value) else value
            else:
                pad[target] = common.atomic_scalar(child)
    shape = node.child("shape")
    if shape is not None and "shape" not in pad:
        pad["shape"] = shape.value_at(1)
    side = node.child("side")
    if side is not None and "side" not in pad:
        pad["side"] = side.value_at(1)
    return pad


# ---------------------------------------------------------------------------
# Component
# ---------------------------------------------------------------------------

def _component_content(node, record, uuid_index):
    record["pins"] = []           # signals
    record["references"] = {"symbolVariants": []}
    unhandled = []
    for child in node.items[1:]:
        if not child.is_list:
            continue
        if child.tag == "signal":
            signal = {
                "uuid": _atom_uuid(child, 1),
                "name": (child.child("name") or _NONE).value_at(1),
            }
            for key in ("role", "required", "negated", "clock",
                        "forced_net", "is_clock", "is_global"):
                sub = child.child(key)
                if sub is not None:
                    value = common.atomic_scalar(sub)
                    signal[key] = value if value is not None \
                        else to_json_value(sub)
            record["pins"].append(signal)
        elif child.tag == "variant":
            record["references"]["symbolVariants"].append(
                _lp_variant(child))
        elif child.tag == "attribute":
            attribute = {
                "name": child.value_at(1),
                "type": (child.child("type") or _NONE).value_at(1),
                "unit": (child.child("unit") or _NONE).value_at(1),
                "value": (child.child("value") or _NONE).value_at(1),
            }
            record["properties"].setdefault("attributes", []).append(
                attribute)
        elif child.tag == "approved":
            _approved(child, record)
        elif child.tag in ("name", "category", "description", "keywords",
                           "author", "version", "created", "generated_by",
                           "deprecated", "schematic_only", "default_value",
                           "prefix"):
            continue
        else:
            unhandled.append(to_json_value(child))
    if unhandled:
        record["unhandled"] = unhandled
        record["issues"].append("unhandled-tag")
        if record["status"] == common.STATUS_CONVERTED:
            record["status"] = common.STATUS_REVIEW


def _lp_variant(node):
    variant = {
        "uuid": _atom_uuid(node, 1),
        "norm": (node.child("norm") or _NONE).value_at(1),
        "name": (node.child("name") or _NONE).value_at(1),
        "gates": [],
    }
    description = node.child("description")
    if description is not None:
        variant["description"] = description.value_at(1)
    for gate in node.children("gate"):
        variant["gates"].append(_lp_gate(gate))
    return variant


def _lp_gate(node):
    issues = []
    gate = {"uuid": _atom_uuid(node, 1)}
    symbol = node.child("symbol")
    if symbol is not None:
        gate["symbolUuid"] = _atom_uuid(symbol, 1)
    position = node.child("position")
    if position is not None:
        gate["positionMm"] = [
            common.number_or_raw(position, 1, common.LIBREPCB_MM, issues,
                                 "gate.position"),
            common.number_or_raw(position, 2, common.LIBREPCB_MM, issues,
                                 "gate.position"),
        ]
    rotation = node.child("rotation")
    if rotation is not None:
        value, error = common.parse_number(rotation.value_at(1))
        gate["rotationDeg"] = value if error is None else rotation.value_at(1)
    for key in ("required", "suffix"):
        child = node.child(key)
        if child is not None:
            gate[key] = common.atomic_scalar(child)
    pins = []
    for pin in node.children("pin"):
        entry = {"uuid": _atom_uuid(pin, 1)}
        signal = pin.child("signal")
        if signal is not None:
            entry["signalUuid"] = _atom_uuid(signal, 1)
        text = pin.child("text")
        if text is not None:
            entry["text"] = text.value_at(1)
        pins.append(entry)
    if pins:
        gate["pinSignalMap"] = pins
    if issues:
        gate["issues"] = issues
    return gate


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------

def _device_content(node, record, uuid_index):
    record["references"] = {"pinPadMap": []}
    for child in node.items[1:]:
        if not child.is_list:
            continue
        if child.tag == "component":
            uuid = _atom_uuid(child, 1)
            if uuid:
                record["dependencies"].setdefault("components", []).append(
                    {"id": uuid})
        elif child.tag == "package":
            uuid = _atom_uuid(child, 1)
            if uuid:
                record["dependencies"].setdefault("packages", []).append(
                    {"id": uuid})
        elif child.tag == "pad":
            entry = {
                "padUuid": _atom_uuid(child, 1),
                "optional": None,
                "signalUuid": None,
            }
            optional = child.child("optional")
            if optional is not None:
                entry["optional"] = common.atomic_scalar(optional)
            signal = child.child("signal")
            if signal is not None:
                entry["signalUuid"] = _atom_uuid(signal, 1)
            record["references"]["pinPadMap"].append(entry)
        elif child.tag == "resource":
            resource = {
                "name": child.value_at(1),
                "mediatype": (child.child("mediatype") or _NONE).value_at(1),
                "url": (child.child("url") or _NONE).value_at(1),
            }
            record["references"].setdefault("resources", []).append(resource)
        elif child.tag == "approved":
            _approved(child, record)
        # name/category/metadata handled in _metadata


def _collect_structural_deps(record):
    """Record structural UUID references as dependencies (roles preserved)."""
    roles = []
    for variant in record.get("references", {}).get("symbolVariants", []):
        for gate in variant.get("gates", []):
            if gate.get("symbolUuid"):
                roles.append(("symbols", gate["symbolUuid"]))
    for entry in record.get("references", {}).get("pinPadMap", []):
        if entry.get("signalUuid"):
            roles.append(("signals", entry["signalUuid"]))
    for footprint in record.get("footprints", []):
        for pad in footprint.get("pads", []):
            if pad.get("packagePadUuid"):
                roles.append(("packagePads", pad["packagePadUuid"]))
            if pad.get("padstackUuid"):
                roles.append(("padstacks", pad["padstackUuid"]))
    for role, uuid in roles:
        entries = record["dependencies"].setdefault(role, [])
        if not any(entry.get("id") == uuid for entry in entries):
            entries.append({"id": uuid})


def _resolve_dependencies(record, uuid_index, own_relpath):
    """Attach resolved paths to recorded UUID dependencies.

    Structural roles surface an ``unresolved-dependency`` issue when the
    UUID is not present in this checkout.  ``categories`` are classification
    metadata that routinely points at other libraries, so they are recorded
    without an unresolved issue.
    """
    for role, entries in record.get("dependencies", {}).items():
        for entry in entries:
            identity = entry.get("id")
            if not isinstance(identity, str) or not common.is_uuid(identity):
                continue
            identity = identity.lower()
            entry["id"] = identity
            if identity in uuid_index:
                entry["resolvedPath"] = uuid_index[identity][0]
            elif "resolvedPath" not in entry and role != "categories":
                issue = "unresolved-dependency:%s:%s" % (role, identity)
                if issue not in record["issues"]:
                    record["issues"].append(issue)
                if record["status"] == common.STATUS_CONVERTED:
                    record["status"] = common.STATUS_REVIEW
