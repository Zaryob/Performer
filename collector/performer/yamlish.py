"""A small YAML reader.

The profiles are YAML because the specification says so and because they are
meant to be edited by hand on the target machine.  PyYAML is not available
there -- the collector may not depend on anything outside the standard library
-- so this parses the subset the profiles actually use:

    mappings, lists, scalars (string, int, float, bool, null), comments,
    quoted strings, nested block structure by indentation

Deliberately absent: anchors, aliases, multi-document streams, flow
collections, block scalars, tags, multi-line strings.  Anything unsupported
raises :class:`YamlError` pointing at the line, rather than being skipped --
a profile that half-parsed would run a different measurement than the one
written down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

__all__ = ["YamlError", "loads", "load_file"]


class YamlError(Exception):
    """The document is malformed or uses an unsupported construct."""


_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}
_NULL = {"", "null", "~"}

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")

_UNSUPPORTED = {
    "&": "anchors",
    "*": "aliases",
    "!": "tags",
    "|": "block scalars",
    ">": "folded scalars",
}


@dataclass
class _Line:
    number: int
    indent: int
    text: str


def _strip_comment(raw: str) -> str:
    """Remove a trailing comment, respecting quotes.

    ``notes: "a # b"`` must keep its hash; ``notes: a  # b`` must lose it.
    """
    out: List[str] = []
    quote: Optional[str] = None
    for index, char in enumerate(raw):
        if quote is not None:
            out.append(char)
            if char == quote and (index == 0 or raw[index - 1] != "\\"):
                quote = None
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#" and (not out or out[-1] in " \t"):
            break
        out.append(char)
    if quote is not None:
        raise YamlError("unterminated quoted string")
    return "".join(out).rstrip()


def _tokenize(text: str) -> List[_Line]:
    lines: List[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError(f"line {number}: tabs cannot be used for indentation")
        try:
            stripped = _strip_comment(raw)
        except YamlError as exc:
            raise YamlError(f"line {number}: {exc}") from None
        if not stripped.strip():
            continue
        if stripped.strip() == "---":
            continue  # a single document separator is harmless
        indent = len(stripped) - len(stripped.lstrip())
        lines.append(_Line(number, indent, stripped.strip()))
    return lines


def _scalar(token: str, line: int) -> Any:
    token = token.strip()
    # A bare "|" or ">" is as much a block scalar indicator as "|-" is, so the
    # first character decides. Quote the value to use one of these literally.
    if token[:1] in _UNSUPPORTED:
        raise YamlError(f"line {line}: {_UNSUPPORTED[token[0]]} are not supported")
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    lowered = token.lower()
    if lowered in _NULL:
        return None
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    # An empty flow collection is unambiguous and idiomatic, so it is allowed;
    # a populated one is not, because parsing it properly means implementing
    # most of YAML.
    if token == "[]":
        return []
    if token == "{}":
        return {}
    if token.startswith("[") or token.startswith("{"):
        raise YamlError(
            f"line {line}: flow collections are not supported; use block style"
        )
    return token


def _split_key(token: str, line: int) -> Tuple[str, str]:
    """Split ``key: value`` while respecting quotes in the key."""
    quote: Optional[str] = None
    for index, char in enumerate(token):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char == ":" and (index + 1 == len(token) or token[index + 1] in " \t"):
            key = token[:index].strip()
            value = token[index + 1 :].strip()
            if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                key = key[1:-1]
            if not key:
                raise YamlError(f"line {line}: empty key")
            if not _KEY_RE.match(key):
                raise YamlError(
                    f"line {line}: unsupported key {key!r}; use letters, digits, "
                    "dot, dash or underscore"
                )
            return key, value
    raise YamlError(f"line {line}: expected 'key: value', got {token!r}")


class _Parser:
    def __init__(self, lines: List[_Line]) -> None:
        self.lines = lines
        self.pos = 0

    def _peek(self) -> Optional[_Line]:
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def parse_document(self) -> Any:
        if not self.lines:
            return None
        value = self.parse_block(self.lines[0].indent)
        remaining = self._peek()
        if remaining is not None:
            raise YamlError(
                f"line {remaining.number}: unexpected indentation; "
                "the document must be a single block"
            )
        return value

    def parse_block(self, indent: int) -> Union[Dict[str, Any], List[Any]]:
        line = self._peek()
        if line is None:
            return {}
        if line.text.startswith("- "):
            return self.parse_list(indent)
        if line.text == "-":
            return self.parse_list(indent)
        return self.parse_mapping(indent)

    def parse_mapping(self, indent: int) -> Dict[str, Any]:
        mapping: Dict[str, Any] = {}
        while True:
            line = self._peek()
            if line is None or line.indent < indent:
                return mapping
            if line.indent > indent:
                raise YamlError(
                    f"line {line.number}: unexpected indentation inside a mapping"
                )
            if line.text.startswith("- "):
                raise YamlError(
                    f"line {line.number}: list item where a 'key: value' was expected"
                )
            key, raw_value = _split_key(line.text, line.number)
            if key in mapping:
                raise YamlError(f"line {line.number}: duplicate key {key!r}")
            self.pos += 1
            if raw_value:
                mapping[key] = _scalar(raw_value, line.number)
                continue
            child = self._peek()
            if child is None or child.indent <= indent:
                mapping[key] = None
                continue
            mapping[key] = self.parse_block(child.indent)
        return mapping

    def parse_list(self, indent: int) -> List[Any]:
        items: List[Any] = []
        while True:
            line = self._peek()
            if line is None or line.indent < indent:
                return items
            if line.indent > indent:
                raise YamlError(
                    f"line {line.number}: unexpected indentation inside a list"
                )
            if not (line.text == "-" or line.text.startswith("- ")):
                return items
            body = line.text[1:].strip()
            self.pos += 1
            if not body:
                child = self._peek()
                if child is None or child.indent <= indent:
                    items.append(None)
                    continue
                items.append(self.parse_block(child.indent))
                continue
            # "- key: value" starts an inline mapping whose remaining keys are
            # indented to the position of the first key.
            try:
                key, raw_value = _split_key(body, line.number)
            except YamlError:
                items.append(_scalar(body, line.number))
                continue
            entry: Dict[str, Any] = {}
            inner_indent = line.indent + (len(line.text) - len(body))
            if raw_value:
                entry[key] = _scalar(raw_value, line.number)
            else:
                child = self._peek()
                if child is not None and child.indent > inner_indent:
                    entry[key] = self.parse_block(child.indent)
                else:
                    entry[key] = None
            child = self._peek()
            if child is not None and child.indent == inner_indent:
                rest = self.parse_mapping(inner_indent)
                for extra_key, extra_value in rest.items():
                    if extra_key in entry:
                        raise YamlError(f"duplicate key {extra_key!r} in list item")
                    entry[extra_key] = extra_value
            items.append(entry)


def loads(text: str) -> Any:
    """Parse a YAML document from a string."""
    return _Parser(_tokenize(text)).parse_document()


def load_file(path) -> Any:
    from pathlib import Path

    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise YamlError(f"cannot read {target}: {exc}") from None
    try:
        return loads(text)
    except YamlError as exc:
        raise YamlError(f"{target}: {exc}") from None
