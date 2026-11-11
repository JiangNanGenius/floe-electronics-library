# SPDX-License-Identifier: MPL-2.0
"""KiCad symbol (``.kicad_sym``) and footprint (``.kicad_mod``) normalizer.

Handles packed libraries (many symbols per ``.kicad_sym`` file), unpacked
symbol directories (``*.kicad_symdir/**/*.kicad_sym``) and footprint files
either inside ``*.pretty`` directories or standalone.

Both modern KiCad symbol (``.kicad_sym``) and footprint (``.kicad_mod``)
files store coordinates and sizes in millimetres (KiCad s-expression file
format documentation: "coordinates and sizes" are in mm).  Legacy mil-based
``.lib`` libraries are not supported.  The explicit transform is therefore
factor 1.0 for both, and every emitted geometry field is already mm.
"""

from __future__ import annotations

import os

from . import common
from .sexpr import SExprParseError, parse, to_json_value

FORMAT_NAME = "kicad"

_SYMBOL_SUFFIX = ".kicad_sym"
_FOOTPRINT_SUFFIX = ".kicad_mod"

# Source units are millimetres; the factor stays explicit and named so the
# transform is auditable.  Values are rounded to 9 decimals.
_SYMBOL_FACTOR = common.KICAD_SYMBOL_MM
_FOOTPRINT_FACTOR = common.KICAD_FOOTPRINT_MM

_PIN_ORIENTATION_DEG = {"R": 0.0, "U": 90.0, "L": 180.0, "D": 270.0}


def normalize_kicad(root, source_id):
    """Yield normalized records for one KiCad source checkout.

    Symbol files are processed per *library* (a packed ``.kicad_sym`` file
    or one ``.kicad_symdir`` directory): derived symbols in unpacked
    libraries legitimately extend base symbols stored in sibling files, so
    one library's ASTs are held while its records are produced.  Footprint
    files stream individually.  Output order is the sorted source path
    order regardless.
    """
    files = list(common.iter_source_files(
        root, (_SYMBOL_SUFFIX, _FOOTPRINT_SUFFIX)))
    symbol_groups = {}
    for relpath, abspath in files:
        if relpath.endswith(_SYMBOL_SUFFIX):
            library = _library_name(relpath, root)
            symbol_groups.setdefault(library, []).append((relpath, abspath))

    seen_ids = {}
    done_libraries = set()
    for relpath, abspath in files:
        if relpath.endswith(_SYMBOL_SUFFIX):
            library = _library_name(relpath, root)
            if library in done_libraries:
                continue
            done_libraries.add(library)
            records = _symbol_library_group_records(
                root, source_id, library, sorted(symbol_groups[library]))
        else:
            source = common.source_ref(relpath, abspath)
            records = _footprint_records_for_file(
                relpath, abspath, source, source_id, root)
        common.assign_ids(records, seen_ids)
        for record in records:
            yield common.strip_empty(record)


def _library_name(relpath, root):
    stem, ext = os.path.splitext(os.path.basename(relpath))
    parts = relpath.split("/")[:-1]
    if ext == _SYMBOL_SUFFIX:
        # Unpacked libraries keep one symbol per file inside
        # ``<library>.kicad_symdir``; the directory names the library.
        for depth in range(len(parts), 0, -1):
            name = parts[depth - 1]
            if name.endswith(".kicad_symdir"):
                return name[: -len(".kicad_symdir")]
        root_name = os.path.basename(root.rstrip(os.sep))
        if root_name.endswith(".kicad_symdir"):
            return root_name[: -len(".kicad_symdir")]
        return stem
    if ext == _FOOTPRINT_SUFFIX:
        # Footprint files live in ``<library>.pretty`` directories.
        if parts:
            parent = parts[-1]
            if parent.endswith(".pretty"):
                return parent[: -len(".pretty")]
            return parent
        root_name = os.path.basename(root.rstrip(os.sep))
        if root_name.endswith(".pretty"):
            return root_name[: -len(".pretty")]
        return "library"
    return stem


def _footprint_records_for_file(relpath, abspath, source, source_id, root):
    library = _library_name(relpath, root)
    try:
        with open(abspath, "rb") as handle:
            data = handle.read()
        root_node = parse(data)
    except SExprParseError as exc:
        return [_file_error_record(relpath, source, source_id, library,
                                   "parse-error:%s" % exc, kind="footprint")]
    except OSError as exc:
        return [_file_error_record(relpath, source, source_id, library,
                                   "read-error:%s" % exc, kind="footprint")]
    return _footprint_records(root_node, source, source_id, library, relpath)


def _file_error_record(relpath, source, source_id, library, issue,
                       kind=common.KIND_FOOTPRINT):
    kind = _kind_for_path(relpath, kind)
    name = os.path.splitext(os.path.basename(relpath))[0]
    record = common.base_record(
        FORMAT_NAME, source_id, kind, library, name, source,
        discriminator=common.identity_digest(
            "%s\0%s" % (source["path"], name)))
    record["status"] = common.STATUS_UNSUPPORTED
    record["issues"].append(issue)
    return record


def _kind_for_path(relpath, default):
    if relpath.endswith(_SYMBOL_SUFFIX):
        return common.KIND_SYMBOL
    if relpath.endswith(_FOOTPRINT_SUFFIX):
        return common.KIND_FOOTPRINT
    return default


def _new_record(source_id, kind, library, name, source):
    # The readable id segments are slugs, which are lossy (e.g. "A+B" and
    # "A B" both become "A_B").  The discriminator therefore hashes the
    # exact unsanitized element name together with the exact source path, so
    # distinct packed-file symbols never share an id and ids do not depend on
    # document or file iteration order.
    discriminator = common.identity_digest(
        "%s\0%s" % (source["path"], name))
    record = common.base_record(
        FORMAT_NAME, source_id, kind, library, name, source,
        discriminator=discriminator)
    record["identifiers"]["name"] = name
    return record


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

_METADATA_TAGS = {
    "version", "generator", "generator_version", "kicad_symbol_lib",
}


def _symbol_library_group_records(root, source_id, library, group):
    """Records for all files of one KiCad symbol library.

    *group* is a sorted list of (relpath, abspath) for every file in the
    library.  Parsed ASTs are dropped as soon as the library is finished.
    """
    records = []
    by_name = {}
    name_paths = {}
    parsed = []  # (relpath, source, [(symbol_node, name)])
    for relpath, abspath in group:
        source = common.source_ref(relpath, abspath)
        try:
            with open(abspath, "rb") as handle:
                data = handle.read()
            root_node = parse(data)
        except SExprParseError as exc:
            records.append(_file_error_record(
                relpath, source, source_id, library,
                "parse-error:%s" % exc, kind="symbol"))
            continue
        except OSError as exc:
            records.append(_file_error_record(
                relpath, source, source_id, library,
                "read-error:%s" % exc, kind="symbol"))
            continue
        if root_node.tag != "kicad_symbol_lib":
            record = _new_record(source_id, common.KIND_SYMBOL, library,
                                 os.path.basename(relpath), source)
            record["status"] = common.STATUS_UNSUPPORTED
            record["issues"].append("invalid-root:%s" % (root_node.tag,))
            record["raw"] = to_json_value(root_node)
            records.append(record)
            continue
        symbols = [child for child in root_node.items[1:]
                   if child.is_list and child.tag == "symbol"]
        if not symbols:
            record = _new_record(
                source_id, common.KIND_SYMBOL, library,
                os.path.splitext(os.path.basename(relpath))[0], source)
            record["status"] = common.STATUS_UNSUPPORTED
            record["issues"].append("no-symbols")
            record["raw"] = to_json_value(root_node)
            records.append(record)
            continue
        entries = []
        for symbol in symbols:
            name = symbol.value_at(1) or ""
            by_name.setdefault(name, []).append(symbol)
            name_paths.setdefault(name, relpath)
            entries.append((symbol, name))
        parsed.append((relpath, source, entries))

    name_counts = {}
    for _, _, entries in parsed:
        for _, name in entries:
            name_counts[name] = name_counts.get(name, 0) + 1
    for relpath, source, entries in parsed:
        for symbol, name in entries:
            records.append(_symbol_record(
                symbol, by_name, name_paths, source, source_id, library,
                name, is_duplicate=name_counts[name] > 1))
    records.sort(key=lambda r: (r["source"]["path"], r["name"],
                                r["status"], r["id"]))
    return records


def _symbol_record(symbol, by_name, name_paths, source, source_id, library,
                   name, is_duplicate=False):
    record = _new_record(source_id, common.KIND_SYMBOL, library, name, source)

    if is_duplicate:
        record["issues"].append("duplicate-source-name")
        if record["status"] == common.STATUS_CONVERTED:
            record["status"] = common.STATUS_REVIEW

    extends = [child.value_at(1) for child in symbol.children("extends")]
    chain = []
    if extends:
        record["dependencies"].setdefault("extends", [])
        for target in extends:
            entry = {"id": target}
            if target in name_paths:
                entry["resolvedPath"] = name_paths[target]
            record["dependencies"]["extends"].append(entry)
        state, chain = _resolve_extends(symbol, by_name)
        if state == "cycle":
            record["status"] = common.STATUS_UNSUPPORTED
            record["issues"].append("cyclic-inheritance")
        elif state == "unresolved":
            record["status"] = common.STATUS_UNSUPPORTED
            record["issues"].append(
                "unresolved-inheritance:%s" % ",".join(extends))

    inherited = {"properties": {}, "pins": [], "graphics": [],
                 "unit_count": 1}
    for ancestor in chain:
        merged = _own_content(ancestor,
                              inherited_from=ancestor.value_at(1) or name)
        for key, value in merged["properties"].items():
            inherited["properties"].setdefault(key, value)
        inherited["pins"].extend(merged["pins"])
        inherited["graphics"].extend(merged["graphics"])
        inherited["unit_count"] = max(inherited["unit_count"],
                                      merged["unit_count"])

    own = _own_content(symbol, inherited_from=None)
    for key, value in inherited["properties"].items():
        own["properties"].setdefault(key, value)

    record["properties"].update(own["properties"])
    record["properties"]["unitCount"] = max(own["unit_count"],
                                            inherited["unit_count"])
    record["pins"] = inherited["pins"] + own["pins"]
    record["graphics"] = inherited["graphics"] + own["graphics"]

    for flag_key in ("exclude_from_sim", "in_bom", "on_board"):
        node = symbol.child(flag_key)
        if node is not None:
            record["properties"][flag_key] = common.atomic_scalar(node)

    for unhandled in own["unhandled"]:
        record.setdefault("unhandled", []).append(unhandled)
    if own["unhandled"]:
        record["issues"].append("unhandled-tag")
        if record["status"] == common.STATUS_CONVERTED:
            record["status"] = common.STATUS_REVIEW

    if record["status"] != common.STATUS_CONVERTED:
        record["raw"] = to_json_value(symbol)
    return record


def _resolve_extends(symbol, by_name, _depth=0):
    """Follow the inheritance chain.

    Returns ("ok" | "cycle" | "unresolved", [ancestor nodes...]).  KiCad
    derived symbols extend at most one base in practice, but multiple
    ``extends`` nodes are followed depth-first without merging duplicates.
    """
    chain = []
    seen = {symbol.value_at(1) or ""}
    current = symbol
    while True:
        if _depth > 64:
            return "cycle", chain
        extends = [child.value_at(1) for child in current.children("extends")]
        if not extends:
            return "ok", chain
        next_parents = []
        for target in extends:
            candidates = by_name.get(target)
            if not candidates:
                return "unresolved", chain
            if target in seen:
                return "cycle", chain
            seen.add(target)
            parent = candidates[0]
            chain.append(parent)
            next_parents.append(parent)
        if len(next_parents) == 1:
            current = next_parents[0]
            continue
        for parent in next_parents:
            state, sub_chain = _resolve_extends(parent, by_name, _depth + 1)
            if state != "ok":
                return state, chain
            chain.extend(sub_chain)
        return "ok", chain


def _own_content(symbol, inherited_from):
    """Extract pins/graphics/properties declared directly on *symbol*.

    Unit sub-symbols ("Name_1_1") contribute content tagged with their
    unit/style indices; content on the top-level body applies to the whole
    symbol.
    """
    content = {
        "properties": {},
        "pins": [],
        "graphics": [],
        "unhandled": [],
        "unit_count": 0,
    }
    name = symbol.value_at(1) or ""
    nested = [c for c in symbol.items[1:]
              if c.is_list and c.tag == "symbol"]
    unit_indexes = set()

    bodies = []
    if nested:
        for child in nested:
            child_name = child.value_at(1) or ""
            parsed = _parse_unit_name(name, child_name)
            if parsed is None:
                bodies.append((child, None, None, child_name))
            else:
                _, unit, style = parsed
                unit_indexes.add(unit)
                bodies.append((child, unit, style, child_name))
        # Content directly on the top-level body alongside unit children is
        # retained with null unit/style so nothing is dropped.
        bodies.append((symbol, None, None, name))
    else:
        bodies.append((symbol, 0, 0, name))

    for body, unit, style, body_name in bodies:
        body_own = _body_content(body, unit, style)
        content["properties"].update(body_own["properties"])
        content["pins"].extend(body_own["pins"])
        content["graphics"].extend(body_own["graphics"])
        content["unhandled"].extend(body_own["unhandled"])
        if inherited_from is not None:
            for pin in body_own["pins"]:
                pin["inheritedFrom"] = inherited_from
            for graphic in body_own["graphics"]:
                graphic["inheritedFrom"] = inherited_from
    content["unit_count"] = max(unit_indexes) if unit_indexes else 1
    return content


def _parse_unit_name(base, child_name):
    prefix = base + "_"
    if not child_name.startswith(prefix):
        return None
    tail = child_name[len(prefix):]
    parts = tail.split("_")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return base, int(parts[0]), int(parts[1])
    return None


_GRAPHIC_TAGS = {"rectangle", "circle", "arc", "polyline", "text",
                  "bezier", "text_box"}
_BODY_METADATA = {
    "pin_numbers", "pin_names", "exclude_from_sim", "in_bom", "on_board",
    "power", "locked", "unit_name", "demorgan", "hide", "embedded_fonts",
    "in_pos_files", "duplicate_pin_numbers_are_jumpers", "body_styles",
}


def _body_content(body, unit, style):
    content = {"properties": {}, "pins": [], "graphics": [], "unhandled": []}
    for child in body.items[1:]:
        if not child.is_list:
            continue
        tag = child.tag
        if tag == "property":
            key = child.value_at(1)
            value = child.value_at(2)
            if key is not None:
                content["properties"][key] = value
        elif tag == "pin":
            content["pins"].append(_symbol_pin(child, unit, style))
        elif tag in _GRAPHIC_TAGS:
            content["graphics"].append(
                _symbol_graphic(child, unit, style, content["unhandled"]))
        elif tag == "symbol":
            continue  # handled by caller
        elif tag in _BODY_METADATA:
            value = common.atomic_scalar(child)
            if value is None:
                value = to_json_value(child)
            content["properties"][tag] = value
        elif tag == "extends":
            continue  # recorded separately on the record
        else:
            content["unhandled"].append(to_json_value(child))
    return content


def _symbol_pin(node, unit, style):
    issues = []
    pin = {
        "name": (node.child("name") or _NONE).value_at(1),
        "number": (node.child("number") or _NONE).value_at(1),
        "electricalType": node.value_at(1),
        "graphicStyle": node.value_at(2),
        "unit": unit,
        "style": style,
    }
    at = node.child("at")
    if at is not None:
        pin["positionMm"] = [
            common.number_or_raw(at, 1, _SYMBOL_FACTOR, issues, "pin.x"),
            common.number_or_raw(at, 2, _SYMBOL_FACTOR, issues, "pin.y"),
        ]
        rotation = at.value_at(3)
        value, error = common.parse_number(rotation) if rotation is not None \
            else (None, None)
        pin["rotationDeg"] = value if error is None else rotation
    length = node.child("length")
    if length is not None:
        pin["lengthMm"] = common.number_or_raw(
            length, 1, _SYMBOL_FACTOR, issues, "pin.length")
    hide = node.child("hide")
    if hide is not None:
        pin["hide"] = common.atomic_scalar(hide)
    orientation = None
    if at is not None:
        raw = at.value_at(3)
        if raw in _PIN_ORIENTATION_DEG:
            orientation = _PIN_ORIENTATION_DEG[raw]
            pin["orientation"] = raw
    if orientation is None and "rotationDeg" in pin:
        orientation = pin["rotationDeg"]
    if orientation is not None:
        pin["orientationDeg"] = orientation
    for extra in ("name", "number"):
        node_child = node.child(extra)
        if node_child is not None:
            effects = node_child.child("effects")
            if effects is not None:
                pin[extra + "Effects"] = to_json_value(effects)
    alternates = []
    for alternate in node.children("alternate"):
        alternates.append({
            "name": alternate.value_at(1),
            "electricalType": alternate.value_at(2),
            "graphicStyle": alternate.value_at(3),
        })
    if alternates:
        pin["alternates"] = alternates
    if issues:
        pin["issues"] = issues
    return pin


_NONE = type("NoneNode", (), {"value_at": lambda self, index: None})()


def _symbol_graphic(node, unit, style, unhandled):
    tag = node.tag
    issues = []
    graphic = {
        "type": tag,
        "unit": unit,
        "style": style,
    }
    if tag == "rectangle":
        start = node.child("start")
        end = node.child("end")
        if start is not None:
            graphic["startMm"] = _point(start, issues, "rect.start")
        if end is not None:
            graphic["endMm"] = _point(end, issues, "rect.end")
    elif tag == "circle":
        center = node.child("center")
        if center is not None:
            graphic["centerMm"] = _point(center, issues, "circle.center")
        radius = node.child("radius")
        if radius is not None:
            graphic["radiusMm"] = common.number_or_raw(
                radius, 1, _SYMBOL_FACTOR, issues, "circle.radius")
    elif tag == "arc":
        for key, label in (("start", "arc.start"), ("mid", "arc.mid"),
                           ("end", "arc.end")):
            point = node.child(key)
            if point is not None:
                graphic[key + "Mm"] = _point(point, issues, label)
    elif tag in ("polyline", "bezier"):
        pts = node.child("pts")
        if pts is not None:
            graphic["pointsMm"] = [
                _point(xy, issues, tag) for xy in pts.children("xy")]
    elif tag in ("text", "text_box"):
        graphic["text"] = node.value_at(1)
        at = node.child("at")
        if at is not None:
            graphic["positionMm"] = _point(at, issues, tag + ".at")
            rotation = at.value_at(3)
            value, error = common.parse_number(rotation) \
                if rotation is not None else (None, None)
            if error is None and value is not None:
                graphic["rotationDeg"] = value
            elif rotation is not None:
                graphic["rotationDeg"] = rotation
        effects = node.child("effects")
        if effects is not None:
            graphic["effects"] = to_json_value(effects)
        if tag == "text_box":
            size = node.child("size")
            if size is not None:
                graphic["sizeMm"] = [
                    common.number_or_raw(size, 1, _SYMBOL_FACTOR, issues,
                                         "text_box.size"),
                    common.number_or_raw(size, 2, _SYMBOL_FACTOR, issues,
                                         "text_box.size"),
                ]
            margins = node.child("margins")
            if margins is not None:
                graphic["marginsMm"] = [
                    common.number_or_raw(margins, 1, _SYMBOL_FACTOR, issues,
                                         "text_box.margins"),
                    common.number_or_raw(margins, 2, _SYMBOL_FACTOR, issues,
                                         "text_box.margins"),
                ]
    stroke = node.child("stroke")
    if stroke is not None:
        graphic["stroke"] = to_json_value(stroke)
    fill = node.child("fill")
    if fill is not None:
        graphic["fill"] = to_json_value(fill)
    for key in ("layer", "uuid", "locked", "tstamp"):
        value = node.value_at(1) if key == "uuid" else None
        child = node.child(key)
        if child is not None:
            graphic[key] = common.atomic_scalar(child)
        elif value is not None:
            graphic[key] = value
    if issues:
        graphic["issues"] = issues
    return graphic


def _point(node, issues, context):
    return [
        common.number_or_raw(node, 1, _SYMBOL_FACTOR, issues,
                             context + ".x"),
        common.number_or_raw(node, 2, _SYMBOL_FACTOR, issues,
                             context + ".y"),
    ]


# ---------------------------------------------------------------------------
# Footprints
# ---------------------------------------------------------------------------

_FP_GRAPHIC_TAGS = {
    "fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_curve",
    "fp_text", "fp_text_box", "fpBezier", "gr_line", "gr_rect",
    "gr_circle", "gr_arc", "gr_poly", "gr_curve", "gr_text", "gr_text_box",
    "dimension",
}

_FP_METADATA_TAGS = {
    "embedded_fonts", "duplicate_pad_numbers_are_jumpers", "point",
    "solder_mask_margin", "solder_paste_margin", "zone_connect",
    "net_tie_pad_groups", "clearance", "solder_paste_margin_ratio",
    "solder_paste_ratio", "private_layers", "tedit",
}


def _footprint_records(root_node, source, source_id, library, relpath):
    if root_node.tag not in ("kicad_footprint", "footprint", "module"):
        record = _new_record(source_id, common.KIND_FOOTPRINT, library,
                             os.path.basename(relpath), source)
        record["status"] = common.STATUS_UNSUPPORTED
        record["issues"].append("invalid-root:%s" % (root_node.tag,))
        record["raw"] = to_json_value(root_node)
        return [record]

    name_node = root_node.child("footprint")
    name = None
    if name_node is not None:
        name = name_node.value_at(1)
    if name is None:
        if root_node.tag in ("footprint", "module"):
            name = root_node.value_at(1)
        else:
            name = os.path.splitext(os.path.basename(relpath))[0]

    record = _new_record(source_id, common.KIND_FOOTPRINT, library, name,
                         source)
    record["pins"] = []
    record["pads"] = []
    record["graphics"] = []
    unhandled = []

    for child in root_node.items[1:]:
        if not child.is_list:
            continue
        tag = child.tag
        if tag == "footprint":
            continue
        if tag == "layer":
            record["properties"]["layer"] = child.value_at(1)
        elif tag in ("descr", "tags", "category", "value"):
            record["properties"][tag] = child.value_at(1)
        elif tag == "property":
            key = child.value_at(1)
            value = child.value_at(2)
            if key is not None:
                record["properties"][key] = value
        elif tag == "attr":
            record["properties"]["attributes"] = [
                item.value for item in child.items[1:] if item.is_atom]
        elif tag == "fp_text":
            record["graphics"].append(_fp_text(child))
        elif tag == "pad":
            record["pads"].append(_pad(child))
        elif tag == "model":
            record["modelReferences"].append(_model(child))
        elif tag in _FP_GRAPHIC_TAGS:
            record["graphics"].append(_fp_graphic(child))
        elif tag in ("version", "generator", "generator_version",
                     "paper", "title_block", "general", "sheetname"):
            record["properties"][tag] = common.atomic_scalar(child)
        elif tag in _FP_METADATA_TAGS:
            value = common.atomic_scalar(child)
            record["properties"][tag] = value if value is not None \
                else to_json_value(child)
        else:
            # zone / group / unknown features stay verbatim and visible.
            unhandled.append(to_json_value(child))

    if unhandled:
        record["unhandled"] = unhandled
        record["issues"].append("unhandled-tag")
        if record["status"] == common.STATUS_CONVERTED:
            record["status"] = common.STATUS_REVIEW
    if any(pad.get("customGeometry") for pad in record["pads"]):
        record["issues"].append("custom-pad-geometry")
        if record["status"] == common.STATUS_CONVERTED:
            record["status"] = common.STATUS_REVIEW

    if record["status"] != common.STATUS_CONVERTED:
        record["raw"] = to_json_value(root_node)
    return [record]


def _fp_text(node):
    issues = []
    text = {
        "type": "fp_text",
        "kind": node.value_at(1),
        "text": node.value_at(2),
    }
    at = node.child("at")
    if at is not None:
        text["positionMm"] = [
            common.number_or_raw(at, 1, _FOOTPRINT_FACTOR, issues, "fp_text"),
            common.number_or_raw(at, 2, _FOOTPRINT_FACTOR, issues, "fp_text"),
        ]
        rotation = at.value_at(3)
        value, error = common.parse_number(rotation) \
            if rotation is not None else (None, None)
        if error is None and value is not None:
            text["rotationDeg"] = value
        elif rotation is not None:
            text["rotationDeg"] = rotation
        if at.child("unlocked") is not None:
            text["unlocked"] = True
    layer = node.child("layer")
    if layer is not None:
        text["layer"] = layer.value_at(1)
    effects = node.child("effects")
    if effects is not None:
        text["effects"] = to_json_value(effects)
    if issues:
        text["issues"] = issues
    return text


def _fp_graphic(node):
    tag = node.tag
    issues = []
    graphic = {"type": tag}
    pairs = {
        "fp_line": (("start", "startMm"), ("end", "endMm")),
        "fp_rect": (("start", "startMm"), ("end", "endMm")),
        "fp_circle": (("center", "centerMm"), ("end", "endMm")),
        "fp_arc": (("start", "startMm"), ("mid", "midMm"), ("end", "endMm")),
        "gr_line": (("start", "startMm"), ("end", "endMm")),
        "gr_rect": (("start", "startMm"), ("end", "endMm")),
        "gr_circle": (("center", "centerMm"), ("end", "endMm")),
        "gr_arc": (("start", "startMm"), ("mid", "midMm"), ("end", "endMm")),
    }
    for source_key, target_key in pairs.get(tag, ()):
        child = node.child(source_key)
        if child is not None:
            graphic[target_key] = [
                common.number_or_raw(child, 1, _FOOTPRINT_FACTOR, issues,
                                     tag),
                common.number_or_raw(child, 2, _FOOTPRINT_FACTOR, issues,
                                     tag),
            ]
    if tag in ("fp_poly", "gr_poly", "fp_curve", "gr_curve"):
        pts = node.child("pts")
        if pts is not None:
            graphic["pointsMm"] = [
                [
                    common.number_or_raw(xy, 1, _FOOTPRINT_FACTOR, issues,
                                         tag),
                    common.number_or_raw(xy, 2, _FOOTPRINT_FACTOR, issues,
                                         tag),
                ]
                for xy in pts.children("xy")]
    if tag in ("fp_text", "gr_text", "fpBezier", "fp_text_box",
               "gr_text_box"):
        graphic["text"] = node.value_at(1)
        at = node.child("at")
        if at is not None:
            graphic["positionMm"] = [
                common.number_or_raw(at, 1, _FOOTPRINT_FACTOR, issues, tag),
                common.number_or_raw(at, 2, _FOOTPRINT_FACTOR, issues, tag),
            ]
            rotation = at.value_at(3)
            value, error = common.parse_number(rotation) \
                if rotation is not None else (None, None)
            if error is None and value is not None:
                graphic["rotationDeg"] = value
    stroke = node.child("stroke")
    if stroke is not None:
        graphic["stroke"] = to_json_value(stroke)
    fill = node.child("fill")
    if fill is not None:
        graphic["fill"] = to_json_value(fill)
    layer = node.child("layer")
    if layer is not None:
        graphic["layer"] = layer.value_at(1)
    width = node.child("width")
    if width is not None:
        graphic["widthMm"] = common.number_or_raw(
            width, 1, _FOOTPRINT_FACTOR, issues, tag + ".width")
    size = node.child("size")
    if size is not None:
        graphic["sizeMm"] = [
            common.number_or_raw(size, 1, _FOOTPRINT_FACTOR, issues,
                                 tag + ".size"),
            common.number_or_raw(size, 2, _FOOTPRINT_FACTOR, issues,
                                 tag + ".size"),
        ]
    if issues:
        graphic["issues"] = issues
    return graphic


_KNOWN_PAD_TAGS = {
    "at", "size", "drill", "layers", "roundrect_rratio", "chamfer_ratio",
    "chamfer", "net", "die_length", "solder_mask_margin", "solder_paste_margin",
    "solder_paste_margin_ratio", "clearance", "thermal_bridge_width",
    "thermal_bridge_angle", "thermal_gap", "zone", "options", "primitives",
    "uuid", "tstamp", "locked", "remove_unused_layers", "keep_end_layers",
    "tenting", "pinfunction", "pintype", "property",
}


def _pad(node):
    issues = []
    pad = {
        "number": node.value_at(1),
        "type": node.value_at(2),
        "shape": node.value_at(3),
    }
    uuid_child = node.child("uuid")
    if uuid_child is not None:
        pad["uuid"] = uuid_child.value_at(1)
    at = node.child("at")
    if at is not None:
        pad["positionMm"] = [
            common.number_or_raw(at, 1, _FOOTPRINT_FACTOR, issues, "pad.x"),
            common.number_or_raw(at, 2, _FOOTPRINT_FACTOR, issues, "pad.y"),
        ]
        rotation = at.value_at(3)
        value, error = common.parse_number(rotation) \
            if rotation is not None else (0.0, None)
        pad["rotationDeg"] = value if error is None else rotation
    size = node.child("size")
    if size is not None:
        pad["sizeMm"] = [
            common.number_or_raw(size, 1, _FOOTPRINT_FACTOR, issues,
                                 "pad.width"),
            common.number_or_raw(size, 2, _FOOTPRINT_FACTOR, issues,
                                 "pad.height"),
        ]
    drill = node.child("drill")
    if drill is not None:
        drill_entry = {}
        shape = drill.value_at(1)
        if shape in ("oval", "circle"):
            drill_entry["shape"] = shape
            offset_indexes = 2
        else:
            offset_indexes = 1
        numbers = []
        for index in range(offset_indexes, offset_indexes + 2):
            raw = drill.value_at(index)
            value, error = common.parse_number(raw)
            if error is None:
                numbers.append(common.to_mm(value, _FOOTPRINT_FACTOR))
        if numbers:
            key = "sizeMm" if drill_entry.get("shape") == "oval" \
                else "diameterMm"
            drill_entry[key] = numbers[0] if len(numbers) == 1 else numbers
        offset = drill.child("offset")
        if offset is not None:
            drill_entry["offsetMm"] = [
                common.number_or_raw(offset, 1, _FOOTPRINT_FACTOR, issues,
                                     "drill.offset.x"),
                common.number_or_raw(offset, 2, _FOOTPRINT_FACTOR, issues,
                                     "drill.offset.y"),
            ]
        pad["drill"] = drill_entry
    layers = node.child("layers")
    if layers is not None:
        pad["layers"] = [item.value for item in layers.items[1:]
                         if item.is_atom]
    for key, target, numeric in (
            ("roundrect_rratio", "roundrectRratio", False),
            ("chamfer_ratio", "chamferRatio", False),
            ("die_length", "dieLengthMm", True),
            ("solder_mask_margin", "solderMaskMarginMm", True),
            ("solder_paste_margin", "solderPasteMarginMm", True),
            ("solder_paste_margin_ratio", "solderPasteMarginRatio", False),
            ("clearance", "clearanceMm", True),
            ("thermal_bridge_width", "thermalBridgeWidthMm", True),
            ("thermal_bridge_angle", "thermalBridgeAngleDeg", False),
            ("thermal_gap", "thermalGapMm", True),
            ("remove_unused_layers", "removeUnusedLayers", False),
            ("keep_end_layers", "keepEndLayers", False)):
        child = node.child(key)
        if child is None:
            continue
        if numeric:
            pad[target] = common.number_or_raw(
                child, 1, _FOOTPRINT_FACTOR, issues, key)
        else:
            value = common.atomic_scalar(child)
            pad[target] = value if value is not None else to_json_value(child)
    chamfer = node.child("chamfer")
    if chamfer is not None:
        pad["chamfers"] = [item.value for item in chamfer.items[1:]
                           if item.is_atom]
    net = node.child("net")
    if net is not None:
        pad["net"] = {"number": net.value_at(1), "name": net.value_at(2)}
    for key in ("options", "zone", "tenting"):
        child = node.child(key)
        if child is not None:
            pad[key] = to_json_value(child)
    for key in ("pinfunction", "pintype", "property"):
        for child in node.children(key):
            if key == "property":
                prop_key = child.value_at(1)
                if prop_key:
                    pad.setdefault("properties", {})[prop_key] = \
                        child.value_at(2)
            else:
                pad[key] = child.value_at(1)
    primitives = node.child("primitives")
    if primitives is not None:
        pad["customGeometry"] = True
        pad["primitives"] = to_json_value(primitives)
    pad_unhandled = []
    known = _KNOWN_PAD_TAGS | {"at"}
    for child in node.items[4:]:
        if child.is_list and child.tag not in known:
            pad_unhandled.append(to_json_value(child))
    if pad_unhandled:
        pad["unhandled"] = pad_unhandled
    if issues:
        pad["issues"] = issues
    return pad


def _model(node):
    model = {"file": node.value_at(1)}
    for key, target in (("offset", "offset"), ("scale", "scale"),
                        ("rotate", "rotationDeg")):
        child = node.child(key)
        if child is None:
            continue
        xyz = child.child("xyz")
        if xyz is None:
            continue
        values = []
        for index in (1, 2, 3):
            raw = xyz.value_at(index)
            value, error = common.parse_number(raw)
            values.append(value if error is None else raw)
        model[target] = values
    extras = []
    for child in node.items[2:]:
        if child.is_list and child.tag not in ("offset", "scale", "rotate"):
            extras.append(to_json_value(child))
    if extras:
        model["unhandled"] = extras
    # KiCad stores model offsets/rotations in the source's native 3D units
    # (historically inches for offsets); they are retained verbatim as
    # metadata and intentionally NOT converted here.
    model["unitNote"] = "source-native 3D units retained verbatim"
    return model
