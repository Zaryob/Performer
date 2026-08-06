"""The bundled YAML subset reader.

Profiles are hand edited on the target machine, so the reader's job is as much
to reject a mistake clearly as to parse a correct file: a silently ignored
threshold would run a different measurement than the one written down.
"""

from __future__ import annotations

import unittest

from performer.yamlish import YamlError, loads


class ScalarTests(unittest.TestCase):
    def test_types(self):
        doc = loads(
            "s: text\n"
            "i: 42\n"
            "neg: -7\n"
            "f: 1.5\n"
            "sci: 1e3\n"
            "t: true\n"
            "f2: no\n"
            "n: null\n"
            "empty:\n"
        )
        self.assertEqual(doc["s"], "text")
        self.assertEqual(doc["i"], 42)
        self.assertEqual(doc["neg"], -7)
        self.assertEqual(doc["f"], 1.5)
        self.assertEqual(doc["sci"], 1000.0)
        self.assertIs(doc["t"], True)
        self.assertIs(doc["f2"], False)
        self.assertIsNone(doc["n"])
        self.assertIsNone(doc["empty"])

    def test_quoted_strings_keep_their_content(self):
        doc = loads('a: "5"\nb: \'true\'\nc: "a: b"\n')
        self.assertEqual(doc["a"], "5")
        self.assertEqual(doc["b"], "true")
        self.assertEqual(doc["c"], "a: b")

    def test_comments_are_stripped_but_not_inside_quotes(self):
        doc = loads('# leading\na: 1  # trailing\nb: "text # not a comment"\n')
        self.assertEqual(doc["a"], 1)
        self.assertEqual(doc["b"], "text # not a comment")

    def test_hash_without_leading_space_is_literal(self):
        self.assertEqual(loads("a: red#5\n")["a"], "red#5")


class StructureTests(unittest.TestCase):
    def test_nested_mapping(self):
        doc = loads("outer:\n  inner:\n    leaf: 1\n")
        self.assertEqual(doc, {"outer": {"inner": {"leaf": 1}}})

    def test_list_of_scalars(self):
        self.assertEqual(loads("xs:\n  - a\n  - b\n")["xs"], ["a", "b"])

    def test_list_of_mappings(self):
        doc = loads(
            "probes:\n"
            "  - name: oncpu\n"
            "    program: oncpu.bt\n"
            "  - name: futex\n"
            "    program: futex.bt\n"
            "    thresholds:\n"
            "      min_us: 50\n"
        )
        self.assertEqual(len(doc["probes"]), 2)
        self.assertEqual(doc["probes"][0], {"name": "oncpu", "program": "oncpu.bt"})
        self.assertEqual(doc["probes"][1]["thresholds"], {"min_us": 50})

    def test_empty_flow_collections_are_allowed(self):
        doc = loads("a: []\nb: {}\n")
        self.assertEqual(doc["a"], [])
        self.assertEqual(doc["b"], {})

    def test_document_separator_is_ignored(self):
        self.assertEqual(loads("---\na: 1\n"), {"a": 1})

    def test_empty_document(self):
        self.assertIsNone(loads(""))
        self.assertIsNone(loads("# only a comment\n"))


class RejectionTests(unittest.TestCase):
    """Every unsupported construct must fail loudly, with a line number."""

    def _reject(self, text: str, fragment: str):
        with self.assertRaises(YamlError) as ctx:
            loads(text)
        self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    def test_tabs(self):
        self._reject("a:\n\tb: 1\n", "tabs")

    def test_anchors_and_aliases(self):
        self._reject("a: &anchor 1\n", "anchors")
        self._reject("a: *alias\n", "aliases")

    def test_block_scalars(self):
        self._reject("a: |\n  text\n", "block scalars")
        self._reject("a: >-\n  text\n", "folded scalars")

    def test_populated_flow_collections(self):
        self._reject("a: [1, 2]\n", "flow collections")

    def test_duplicate_keys(self):
        self._reject("a: 1\na: 2\n", "duplicate key")

    def test_missing_colon(self):
        self._reject("just text\n", "expected 'key: value'")

    def test_unterminated_quote(self):
        self._reject('a: "open\n', "unterminated")

    def test_bad_indentation(self):
        self._reject("a: 1\n  b: 2\n", "indentation")

    def test_errors_name_the_line(self):
        message = self._reject("a: 1\nb: |\n  x\n", "block scalars")
        self.assertIn("line 2", message)


class RealProfileTests(unittest.TestCase):
    def test_the_shipped_profiles_parse(self):
        from performer import profiles

        for name, path in profiles.available().items():
            with self.subTest(profile=name):
                document = __import__(
                    "performer.yamlish", fromlist=["load_file"]
                ).load_file(path)
                self.assertEqual(document["name"], name)
                self.assertTrue(document["probes"])


if __name__ == "__main__":
    unittest.main()
