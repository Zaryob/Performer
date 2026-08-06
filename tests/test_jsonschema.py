"""Tests for the bundled JSON Schema subset validator."""

from __future__ import annotations

import unittest

from oxfscope.jsonschema import SchemaError, Validator


def errors(schema, instance):
    return [str(e) for e in Validator(schema).validate(instance)]


class TypeTests(unittest.TestCase):
    def test_integer_rejects_bool(self):
        self.assertTrue(errors({"type": "integer"}, True))
        self.assertFalse(errors({"type": "integer"}, 3))

    def test_number_accepts_int_and_float(self):
        self.assertFalse(errors({"type": "number"}, 3))
        self.assertFalse(errors({"type": "number"}, 3.5))
        self.assertTrue(errors({"type": "number"}, "3"))

    def test_union_types(self):
        schema = {"type": ["string", "null"]}
        self.assertFalse(errors(schema, "x"))
        self.assertFalse(errors(schema, None))
        self.assertTrue(errors(schema, 1))

    def test_type_mismatch_suppresses_downstream_noise(self):
        schema = {"type": "object", "required": ["a", "b"]}
        self.assertEqual(len(errors(schema, "not an object")), 1)


class ObjectTests(unittest.TestCase):
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "integer"}},
        "additionalProperties": False,
    }

    def test_missing_required(self):
        self.assertIn("missing required key 'a'", errors(self.schema, {})[0])

    def test_additional_properties_false(self):
        found = errors(self.schema, {"a": 1, "b": 2})
        self.assertEqual(len(found), 1)
        self.assertIn("/b", found[0])

    def test_additional_properties_schema(self):
        schema = {"type": "object", "additionalProperties": {"type": "string"}}
        self.assertFalse(errors(schema, {"x": "ok"}))
        self.assertTrue(errors(schema, {"x": 1}))

    def test_pattern_properties(self):
        schema = {
            "type": "object",
            "patternProperties": {"^[0-9]+$": {"type": "string"}},
            "additionalProperties": False,
        }
        self.assertFalse(errors(schema, {"12": "ok"}))
        self.assertTrue(errors(schema, {"12": 5}))
        self.assertTrue(errors(schema, {"tid": "nope"}))

    def test_error_path_is_a_json_pointer(self):
        schema = {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {"inner": {"type": "integer"}},
                }
            },
        }
        found = errors(schema, {"outer": {"inner": "x"}})
        self.assertEqual(found[0].split(":")[0], "/outer/inner")


class ArrayAndScalarTests(unittest.TestCase):
    def test_items_and_bounds(self):
        schema = {"type": "array", "items": {"type": "integer"}, "minItems": 2}
        self.assertFalse(errors(schema, [1, 2]))
        self.assertTrue(errors(schema, [1]))
        self.assertTrue(errors(schema, [1, "x"]))

    def test_unique_items(self):
        schema = {"type": "array", "uniqueItems": True}
        self.assertTrue(errors(schema, [{"a": 1}, {"a": 1}]))
        self.assertFalse(errors(schema, [{"a": 1}, {"a": 2}]))

    def test_string_constraints(self):
        schema = {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^a"}
        self.assertFalse(errors(schema, "abc"))
        self.assertTrue(errors(schema, "a"))
        self.assertTrue(errors(schema, "abcde"))
        self.assertTrue(errors(schema, "bbb"))

    def test_numeric_bounds(self):
        schema = {"type": "number", "minimum": 0, "maximum": 1}
        self.assertFalse(errors(schema, 0.5))
        self.assertTrue(errors(schema, -0.1))
        self.assertTrue(errors(schema, 1.1))

    def test_exclusive_bounds(self):
        schema = {"type": "number", "exclusiveMinimum": 0}
        self.assertTrue(errors(schema, 0))
        self.assertFalse(errors(schema, 0.1))

    def test_enum_and_const(self):
        self.assertFalse(errors({"enum": ["a", "b"]}, "a"))
        self.assertTrue(errors({"enum": ["a", "b"]}, "c"))
        self.assertFalse(errors({"const": 1}, 1))
        self.assertTrue(errors({"const": 1}, 2))

    def test_date_time_format(self):
        schema = {"type": "string", "format": "date-time"}
        self.assertFalse(errors(schema, "2026-08-06T14:25:30Z"))
        self.assertFalse(errors(schema, "2026-08-06T14:25:30.5+03:00"))
        self.assertTrue(errors(schema, "2026-08-06 14:25:30"))


class CompositionTests(unittest.TestCase):
    def test_any_of(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertFalse(errors(schema, "x"))
        self.assertFalse(errors(schema, 1))
        self.assertTrue(errors(schema, 1.5))

    def test_one_of_requires_exactly_one(self):
        schema = {"oneOf": [{"type": "number"}, {"type": "integer"}]}
        self.assertTrue(errors(schema, 1))  # matches both
        self.assertFalse(errors(schema, 1.5))  # matches only "number"

    def test_all_of(self):
        schema = {"allOf": [{"type": "string"}, {"minLength": 3}]}
        self.assertFalse(errors(schema, "abc"))
        self.assertTrue(errors(schema, "ab"))


class RefTests(unittest.TestCase):
    schema = {
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/item"}},
        "$defs": {"item": {"type": "integer", "minimum": 0}},
    }

    def test_ref_is_followed(self):
        self.assertFalse(errors(self.schema, {"item": 1}))
        self.assertTrue(errors(self.schema, {"item": -1}))

    def test_unresolvable_ref_raises(self):
        with self.assertRaises(SchemaError):
            Validator({"$ref": "#/$defs/missing"}).validate(1)

    def test_remote_ref_is_rejected(self):
        with self.assertRaises(SchemaError):
            Validator({"$ref": "https://example.com/x.json"}).validate(1)


class SchemaSanityTests(unittest.TestCase):
    def test_unsupported_keyword_is_loud(self):
        # Silently ignoring a keyword would mean the contract is not enforced.
        with self.assertRaises(SchemaError):
            Validator({"type": "object", "dependentRequired": {"a": ["b"]}})

    def test_bad_regex_is_rejected_at_load_time(self):
        with self.assertRaises(SchemaError):
            Validator({"type": "string", "pattern": "["})

    def test_unknown_type_name_raises(self):
        with self.assertRaises(SchemaError):
            Validator({"type": "int"}).validate(1)


if __name__ == "__main__":
    unittest.main()
