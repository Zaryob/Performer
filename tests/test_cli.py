"""End to end CLI behaviour, including exit codes."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from performer import layout
from performer.cli import main

from . import REPO_ROOT

ENTRY_POINT = REPO_ROOT / "collector" / "bin" / "performer"


def run_cli(*argv):
    """Run the CLI in-process and capture (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _fake_run(self, *extra):
        code, out, err = run_cli(
            "fake-run",
            "--out",
            str(self.tmp),
            "--label",
            "unit",
            "--duration",
            "5",
            "--threads",
            "10",
            *extra,
        )
        self.assertEqual(code, 0, err)
        archives = sorted(self.tmp.glob("*.tgz"))
        return archives[-1] if archives else None, out

    def test_fake_run_then_inspect(self):
        archive, _ = self._fake_run()
        self.assertIsNotNone(archive)
        code, out, err = run_cli("inspect", str(archive))
        self.assertEqual(code, 0, err)
        self.assertIn("run 2", out)
        self.assertIn("schema: OK", out)
        self.assertIn("none -- this run looks trustworthy", out)

    def test_inspect_json_is_machine_readable(self):
        archive, _ = self._fake_run()
        code, out, _ = run_cli("inspect", "--json", str(archive))
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["status"], "ok")
        self.assertTrue(doc["schema_valid"])
        self.assertEqual(doc["flags"], [])
        self.assertIn("origin", doc)

    def test_inspect_multiple_bundles_emits_a_json_array(self):
        first, _ = self._fake_run()
        code, out, _ = run_cli("fake-run", "--out", str(self.tmp), "--label", "second")
        self.assertEqual(code, 0)
        second = sorted(p for p in self.tmp.glob("*second.tgz"))[0]
        code, out, _ = run_cli("inspect", "--json", str(first), str(second))
        self.assertEqual(code, 0)
        documents = json.loads(out)
        self.assertEqual(len(documents), 2)

    def test_strict_inspect_fails_on_error_flags(self):
        archive, _ = self._fake_run("--degraded", "--bad-frame-pointers")
        code, out, _ = run_cli("inspect", str(archive))
        self.assertEqual(code, 0, "flags alone must not fail without --strict")
        self.assertIn("[X]", out)
        code, _, _ = run_cli("inspect", "--strict", str(archive))
        self.assertEqual(code, 1)

    def test_validate_reports_ok(self):
        archive, _ = self._fake_run()
        code, out, _ = run_cli("validate", str(archive))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("OK"))

    def test_validate_detects_a_hand_edited_manifest(self):
        code, _, err = run_cli(
            "fake-run", "--out", str(self.tmp), "--label", "edited", "--no-pack"
        )
        self.assertEqual(code, 0, err)
        run_dir = sorted(self.tmp.glob("run_*"))[0]
        manifest_path = run_dir / layout.MANIFEST
        doc = json.loads(manifest_path.read_text())
        doc["quality"]["unknown_frame_ratio"] = 42
        manifest_path.write_text(json.dumps(doc))
        code, out, _ = run_cli("validate", str(run_dir))
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("unknown_frame_ratio", out)

    def test_unimplemented_commands_exit_three(self):
        for command in ("daemon",):
            with self.subTest(command=command):
                code, _, err = run_cli(command)
                self.assertEqual(code, 3)
                self.assertIn("not implemented", err)

    def test_schema_command_lists_the_contract(self):
        code, out, _ = run_cli("schema")
        self.assertEqual(code, 0)
        self.assertIn("manifest.schema.json", out)
        code, out, _ = run_cli("schema", "manifest")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["title"], "Performer run bundle manifest")

    def test_unknown_bundle_path_is_a_clean_error(self):
        code, _, err = run_cli("inspect", str(self.tmp / "nope.tgz"))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        self.assertNotIn("Traceback", err)

    def test_no_arguments_prints_help(self):
        code, out, _ = run_cli()
        self.assertEqual(code, 0)
        self.assertIn("<command>", out)


class EntryPointTests(unittest.TestCase):
    """The shipped script must work with no install and no PYTHONPATH."""

    def test_executable_runs_from_a_foreign_cwd(self):
        result = subprocess.run(
            [sys.executable, str(ENTRY_POINT), "--version"],
            cwd="/",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("performer", result.stdout)

    def test_help_names_the_tool_consistently(self):
        result = subprocess.run(
            [sys.executable, str(ENTRY_POINT), "--help"],
            cwd="/",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: performer", result.stdout)

    def test_the_tool_has_exactly_one_name(self):
        """No second binary, no alias: one name in one place."""
        entries = sorted(p.name for p in ENTRY_POINT.parent.iterdir())
        self.assertEqual(entries, ["performer"])


if __name__ == "__main__":
    unittest.main()
