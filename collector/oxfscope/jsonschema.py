"""A small JSON Schema validator.

The collector is not allowed to depend on anything outside the standard
library, and the target machine is assumed to have no package manager access,
so ``pip install jsonschema`` is not an option.  This module implements the
subset of draft 2020-12 that ``schema/*.json`` actually uses:

    $ref (local pointers only), type, const, enum, required, properties,
    patternProperties, additionalProperties, items, minItems, maxItems,
    uniqueItems, minimum, maximum, exclusiveMinimum, exclusiveMaximum,
    minLength, maxLength, pattern, format (date-time only), allOf, anyOf, oneOf

Anything else in a schema is a programming error and raises ``SchemaError``
rather than being ignored -- silently skipping a keyword would mean the
contract is not actually enforced.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

__all__ = [
    "SchemaError",
    "ValidationError",
    "Validator",
    "load_schema",
    "validate",
]

_KNOWN_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "type",
        "const",
        "enum",
        "required",
        "properties",
        "patternProperties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "allOf",
        "anyOf",
        "oneOf",
    }
)

_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)

_MAX_DEPTH = 64


class SchemaError(Exception):
    """The schema itself is malformed or uses an unsupported keyword."""


@dataclass(frozen=True)
class ValidationError:
    """One failure, located by a JSON Pointer into the instance."""

    path: str
    message: str

    def __str__(self) -> str:
        where = self.path or "<root>"
        return f"{where}: {self.message}"


def _pointer(parts: Sequence[str]) -> str:
    if not parts:
        return ""
    escaped = [p.replace("~", "~0").replace("/", "~1") for p in parts]
    return "/" + "/".join(escaped)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "integer":
        # bool is an int subclass in Python; JSON says otherwise.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "null":
        return value is None
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise SchemaError(f"unknown type name: {expected!r}")


class Validator:
    """Validates instances against one schema document."""

    def __init__(self, schema: Dict[str, Any], *, name: str = "<schema>") -> None:
        if not isinstance(schema, dict):
            raise SchemaError("schema root must be an object")
        self.schema = schema
        self.name = name
        self._pattern_cache: Dict[str, "re.Pattern[str]"] = {}
        self._check_keywords(schema, "#")

    # -- schema sanity ---------------------------------------------------

    def _check_keywords(self, node: Any, where: str) -> None:
        """Walk the schema once so unsupported keywords fail loudly, early."""
        if isinstance(node, list):
            for i, item in enumerate(node):
                self._check_keywords(item, f"{where}/{i}")
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key not in _KNOWN_KEYWORDS:
                # Inside properties/patternProperties/$defs the keys are names,
                # not keywords, so only complain about direct keyword slots.
                raise SchemaError(f"unsupported keyword {key!r} at {where}")
            if key in ("properties", "patternProperties", "$defs"):
                if not isinstance(value, dict):
                    raise SchemaError(f"{key} must be an object at {where}")
                for sub_name, sub in value.items():
                    self._check_keywords(sub, f"{where}/{key}/{sub_name}")
            elif key in ("items", "additionalProperties"):
                if isinstance(value, dict):
                    self._check_keywords(value, f"{where}/{key}")
            elif key in ("allOf", "anyOf", "oneOf"):
                if not isinstance(value, list):
                    raise SchemaError(f"{key} must be an array at {where}")
                self._check_keywords(value, f"{where}/{key}")
            elif key == "pattern":
                self._compile(value)

    def _compile(self, pattern: str) -> "re.Pattern[str]":
        compiled = self._pattern_cache.get(pattern)
        if compiled is None:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:  # pragma: no cover - schema bug
                raise SchemaError(f"bad regex {pattern!r}: {exc}") from exc
            self._pattern_cache[pattern] = compiled
        return compiled

    # -- $ref ------------------------------------------------------------

    def _resolve(self, ref: str) -> Dict[str, Any]:
        if not ref.startswith("#"):
            raise SchemaError(f"only local $ref is supported, got {ref!r}")
        node: Any = self.schema
        for raw in ref[1:].split("/"):
            if raw == "":
                continue
            token = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(node, list):
                try:
                    node = node[int(token)]
                except (ValueError, IndexError) as exc:
                    raise SchemaError(f"unresolvable $ref {ref!r}") from exc
            elif isinstance(node, dict) and token in node:
                node = node[token]
            else:
                raise SchemaError(f"unresolvable $ref {ref!r}")
        if not isinstance(node, dict):
            raise SchemaError(f"$ref {ref!r} does not point at a schema object")
        return node

    # -- validation ------------------------------------------------------

    def iter_errors(self, instance: Any) -> Iterator[ValidationError]:
        yield from self._validate(instance, self.schema, [], 0)

    def validate(self, instance: Any) -> List[ValidationError]:
        return list(self.iter_errors(instance))

    def is_valid(self, instance: Any) -> bool:
        for _ in self.iter_errors(instance):
            return False
        return True

    def _validate(
        self,
        value: Any,
        schema: Dict[str, Any],
        path: List[str],
        depth: int,
    ) -> Iterator[ValidationError]:
        if depth > _MAX_DEPTH:
            raise SchemaError("schema nesting too deep (cycle?)")

        ref = schema.get("$ref")
        if ref is not None:
            yield from self._validate(value, self._resolve(ref), path, depth + 1)

        if "type" in schema:
            expected = schema["type"]
            names = [expected] if isinstance(expected, str) else list(expected)
            if not any(_matches_type(value, n) for n in names):
                yield ValidationError(
                    _pointer(path),
                    f"expected type {'/'.join(names)}, got {_type_name(value)}",
                )
                # Further keywords would only produce noise.
                return

        if "const" in schema and value != schema["const"]:
            yield ValidationError(
                _pointer(path), f"expected constant {schema['const']!r}, got {value!r}"
            )

        if "enum" in schema and value not in schema["enum"]:
            allowed = ", ".join(repr(v) for v in schema["enum"])
            yield ValidationError(
                _pointer(path), f"{value!r} is not one of [{allowed}]"
            )

        for keyword in ("allOf", "anyOf", "oneOf"):
            subschemas = schema.get(keyword)
            if subschemas is None:
                continue
            if keyword == "allOf":
                for sub in subschemas:
                    yield from self._validate(value, sub, path, depth + 1)
                continue
            passed = 0
            first_failure: Optional[ValidationError] = None
            for sub in subschemas:
                errors = list(self._validate(value, sub, path, depth + 1))
                if errors:
                    if first_failure is None:
                        first_failure = errors[0]
                else:
                    passed += 1
            if keyword == "anyOf" and passed == 0:
                detail = f" ({first_failure.message})" if first_failure else ""
                yield ValidationError(
                    _pointer(path), f"does not match any allowed variant{detail}"
                )
            elif keyword == "oneOf" and passed != 1:
                yield ValidationError(
                    _pointer(path),
                    f"must match exactly one allowed variant, matched {passed}",
                )

        if isinstance(value, dict):
            yield from self._validate_object(value, schema, path, depth)
        elif isinstance(value, list):
            yield from self._validate_array(value, schema, path, depth)
        elif isinstance(value, str):
            yield from self._validate_string(value, schema, path)

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            yield from self._validate_number(value, schema, path)

    def _validate_object(
        self,
        value: Dict[str, Any],
        schema: Dict[str, Any],
        path: List[str],
        depth: int,
    ) -> Iterator[ValidationError]:
        for key in schema.get("required", []):
            if key not in value:
                yield ValidationError(_pointer(path), f"missing required key {key!r}")

        properties = schema.get("properties", {})
        pattern_properties = schema.get("patternProperties", {})
        additional = schema.get("additionalProperties", True)

        for key, sub_value in value.items():
            handled = False
            if key in properties:
                handled = True
                yield from self._validate(
                    sub_value, properties[key], path + [key], depth + 1
                )
            for pattern, sub_schema in pattern_properties.items():
                if self._compile(pattern).search(key):
                    handled = True
                    yield from self._validate(
                        sub_value, sub_schema, path + [key], depth + 1
                    )
            if handled:
                continue
            if additional is False:
                yield ValidationError(
                    _pointer(path + [key]), "unexpected key (additionalProperties)"
                )
            elif isinstance(additional, dict):
                yield from self._validate(
                    sub_value, additional, path + [key], depth + 1
                )

    def _validate_array(
        self,
        value: List[Any],
        schema: Dict[str, Any],
        path: List[str],
        depth: int,
    ) -> Iterator[ValidationError]:
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                yield from self._validate(
                    item, item_schema, path + [str(i)], depth + 1
                )
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            yield ValidationError(
                _pointer(path), f"expected at least {min_items} items, got {len(value)}"
            )
        max_items = schema.get("maxItems")
        if max_items is not None and len(value) > max_items:
            yield ValidationError(
                _pointer(path), f"expected at most {max_items} items, got {len(value)}"
            )
        if schema.get("uniqueItems"):
            seen: List[str] = []
            for item in value:
                key = json.dumps(item, sort_keys=True)
                if key in seen:
                    yield ValidationError(_pointer(path), "items must be unique")
                    break
                seen.append(key)

    def _validate_string(
        self, value: str, schema: Dict[str, Any], path: List[str]
    ) -> Iterator[ValidationError]:
        min_length = schema.get("minLength")
        if min_length is not None and len(value) < min_length:
            yield ValidationError(
                _pointer(path), f"shorter than minLength {min_length}"
            )
        max_length = schema.get("maxLength")
        if max_length is not None and len(value) > max_length:
            yield ValidationError(_pointer(path), f"longer than maxLength {max_length}")
        pattern = schema.get("pattern")
        if pattern is not None and not self._compile(pattern).search(value):
            yield ValidationError(
                _pointer(path), f"{value!r} does not match pattern {pattern}"
            )
        if schema.get("format") == "date-time" and not _DATETIME_RE.match(value):
            yield ValidationError(
                _pointer(path), f"{value!r} is not an RFC 3339 date-time"
            )

    def _validate_number(
        self, value: float, schema: Dict[str, Any], path: List[str]
    ) -> Iterator[ValidationError]:
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            yield ValidationError(_pointer(path), f"{value} is below minimum {minimum}")
        maximum = schema.get("maximum")
        if maximum is not None and value > maximum:
            yield ValidationError(_pointer(path), f"{value} is above maximum {maximum}")
        exclusive_min = schema.get("exclusiveMinimum")
        if exclusive_min is not None and value <= exclusive_min:
            yield ValidationError(
                _pointer(path), f"{value} must be greater than {exclusive_min}"
            )
        exclusive_max = schema.get("exclusiveMaximum")
        if exclusive_max is not None and value >= exclusive_max:
            yield ValidationError(
                _pointer(path), f"{value} must be less than {exclusive_max}"
            )


_SCHEMA_CACHE: Dict[Path, Validator] = {}


def load_schema(path: Path) -> Validator:
    """Load and cache a schema file from ``schema/``."""
    path = Path(path).resolve()
    cached = _SCHEMA_CACHE.get(path)
    if cached is None:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        cached = Validator(document, name=path.name)
        _SCHEMA_CACHE[path] = cached
    return cached


def validate(instance: Any, schema: Dict[str, Any]) -> List[ValidationError]:
    return Validator(schema).validate(instance)
