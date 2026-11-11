# SPDX-License-Identifier: MPL-2.0
"""Bounded s-expression parser shared by the KiCad and LibrePCB normalizers.

Both upstream formats are Lisp-style s-expressions.  This parser is a
*reader* only: it never evaluates anything.  It enforces byte, token,
node-count, depth, and string budgets so malformed or hostile input fails
deterministically instead of exhausting memory.

The design mirrors the FloeElectronicsCore ``EDASExpression`` Swift reader:
UTF-8 input, ``;`` line comments, double-quoted strings with ``\\\"``,
``\\\\``, ``\\n``, ``\\r``, ``\\t`` escapes, bare atoms terminated by
whitespace, parentheses, semicolons, or end of input.
"""

from __future__ import annotations


class SExprParseError(ValueError):
    """Raised when source bytes are not a well-formed bounded s-expression."""


MAX_BYTES = 32 * 1024 * 1024
MAX_DEPTH = 96
MAX_NODES = 1_000_000
MAX_TOKEN_BYTES = 1 * 1024 * 1024
MAX_STRING_BYTES = 1 * 1024 * 1024

_ATOM = "atom"
_LIST = "list"


class Node:
    """One s-expression node: either an atom (str value) or a list (items)."""

    __slots__ = ("kind", "value")

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value

    # -- constructors -----------------------------------------------------
    @classmethod
    def atom(cls, text):
        return cls(_ATOM, text)

    @classmethod
    def lst(cls, items):
        return cls(_LIST, items)

    # -- predicates -------------------------------------------------------
    @property
    def is_atom(self):
        return self.kind == _ATOM

    @property
    def is_list(self):
        return self.kind == _LIST

    # -- accessors --------------------------------------------------------
    @property
    def atom_text(self):
        """The atom string, or None when this node is a list."""
        return self.value if self.is_atom else None

    @property
    def items(self):
        """The child list items, or an empty list when this node is an atom."""
        return self.value if self.is_list else []

    @property
    def tag(self):
        """The head atom of a list node, else None."""
        if self.is_list and self.value and self.value[0].is_atom:
            return self.value[0].value
        return None

    def child(self, name):
        """First direct child list whose head atom equals *name*."""
        for item in self.items[1:]:
            if item.is_list and item.tag == name:
                return item
        return None

    def children(self, name):
        """All direct child lists whose head atom equals *name*."""
        return [item for item in self.items[1:] if item.is_list and item.tag == name]

    def value_at(self, index=1):
        """The atom text at *index* in this list, or None."""
        items = self.items
        if 0 <= index < len(items) and items[index].is_atom:
            return items[index].value
        return None


def parse(data, max_bytes=MAX_BYTES, max_depth=MAX_DEPTH, max_nodes=MAX_NODES,
          max_token_bytes=MAX_TOKEN_BYTES, max_string_bytes=MAX_STRING_BYTES):
    """Parse bounded UTF-8 s-expression bytes into a root :class:`Node`.

    Raises :class:`SExprParseError` for empty input, non-UTF-8 input,
    truncated expressions, budget exhaustion, or trailing garbage.
    """
    if not data:
        raise SExprParseError("empty input")
    if len(data) > max_bytes:
        raise SExprParseError(
            "input exceeds %d byte budget (got %d)" % (max_bytes, len(data)))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SExprParseError("input is not valid UTF-8: %s" % exc)

    state = {
        "pos": 0,
        "count": 0,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "max_token_bytes": max_token_bytes,
        "max_string_bytes": max_string_bytes,
    }

    def skip_ws():
        pos = state["pos"]
        n = len(text)
        while pos < n:
            ch = text[pos]
            if ch.isspace() or ch == ";":
                if ch == ";":
                    while pos < n and text[pos] != "\n":
                        pos += 1
                else:
                    pos += 1
            else:
                break
        state["pos"] = pos

    def node(depth):
        state["count"] += 1
        if state["count"] > state["max_nodes"]:
            raise SExprParseError("node budget exhausted")
        if depth > state["max_depth"]:
            raise SExprParseError(
                "nesting depth exceeds %d" % state["max_depth"])
        skip_ws()
        pos = state["pos"]
        n = len(text)
        if pos >= n:
            raise SExprParseError("unfinished expression")
        ch = text[pos]
        if ch == "(":
            state["pos"] = pos + 1
            items = []
            while True:
                skip_ws()
                pos = state["pos"]
                if pos >= n:
                    raise SExprParseError("unclosed list")
                if text[pos] == ")":
                    state["pos"] = pos + 1
                    return Node.lst(items)
                items.append(node(depth + 1))
        if ch == ")":
            raise SExprParseError("unexpected close parenthesis")
        if ch == '"':
            state["pos"] = pos + 1
            out = []
            while True:
                pos = state["pos"]
                if pos >= n:
                    raise SExprParseError("unclosed string")
                c = text[pos]
                state["pos"] = pos + 1
                if c == '"':
                    return Node.atom("".join(out))
                if c == "\\":
                    pos = state["pos"]
                    if pos >= n:
                        raise SExprParseError("unfinished escape")
                    esc = text[pos]
                    state["pos"] = pos + 1
                    if esc == '"' or esc == "\\":
                        out.append(esc)
                    elif esc == "n":
                        out.append("\n")
                    elif esc == "r":
                        out.append("\r")
                    elif esc == "t":
                        out.append("\t")
                    else:
                        raise SExprParseError(
                            "unsupported string escape: \\%s" % esc)
                else:
                    out.append(c)
                if sum(len(p) for p in out) > state["max_string_bytes"]:
                    raise SExprParseError("string budget exhausted")
        start = pos
        while pos < n:
            c = text[pos]
            if c.isspace() or c in "();":
                break
            pos += 1
        if pos == start:
            raise SExprParseError("empty token")
        if pos - start > state["max_token_bytes"]:
            raise SExprParseError("token budget exhausted")
        state["pos"] = pos
        return Node.atom(text[start:pos])

    root = node(0)
    skip_ws()
    if state["pos"] != len(text):
        raise SExprParseError("trailing data after root expression")
    return root


def to_json_value(node):
    """Convert a :class:`Node` tree into JSON-serializable plain data.

    Atoms become strings; lists become ``{"tag": ..., "items": [...]}``
    dicts, where ``tag`` is the head atom string or None.
    """
    if node.is_atom:
        return node.value
    return {
        "tag": node.tag,
        "items": [to_json_value(item) for item in node.items],
    }
