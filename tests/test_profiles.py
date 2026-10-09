"""Profile loading.

A profile decides what gets traced and therefore what the run costs the
target, so a malformed one must be refused rather than half applied.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from performer import profiles
from performer.errors import PerformerError


class ShippedProfileTests(unittest.TestCase):
    def test_the_three_tiers_exist(self):
        self.assertEqual(sorted(profiles.available()), ["deep", "light", "standard"])

    def test_tiers_are_ordered_by_what_they_trace(self):
        light = set(profiles.load("light").probe_names)
        standard = set(profiles.load("standard").probe_names)
        deep = set(profiles.load("deep").probe_names)
        self.assertTrue(light < standard < deep)

    def test_the_expensive_tier_has_the_shortest_limit(self):
        """The overhead ladder has to run the other way from the time budget."""
        light = profiles.load("light")
        standard = profiles.load("standard")
        deep = profiles.load("deep")
        self.assertGreater(light.max_duration_s, standard.max_duration_s)
        self.assertGreater(standard.max_duration_s, deep.max_duration_s)
        self.assertLessEqual(deep.max_duration_s, 60)

    def test_blocking_probes_carry_thresholds(self):
        standard = profiles.load("standard")
        self.assertEqual(standard.probe("offcpu").thresholds, {"min_us": 100.0})
        self.assertEqual(standard.probe("futex").thresholds, {"min_us": 50.0})

    def test_every_probe_program_exists(self):
        for profile in profiles.load_all():
            for spec in profile.probes:
                with self.subTest(profile=profile.name, probe=spec.name):
                    self.assertTrue(profiles.program_path(spec).is_file())

    def test_every_probe_has_an_emitter(self):
        """A probe whose output nothing can parse would be collected for nothing."""
        from performer.emit import EMITTERS

        for profile in profiles.load_all():
            for spec in profile.probes:
                with self.subTest(profile=profile.name, probe=spec.name):
                    self.assertIn(spec.name, EMITTERS)

    def test_no_probe_uses_begin_or_end(self):
        """A stripped bpftrace (Ubuntu 22.04's 0.14.0) cannot attach them.

        It fails on SIGINT with "Could not resolve symbol:
        /proc/self/exe:END_trigger" and exits without printing any map.
        """
        import re

        for profile in profiles.load_all():
            for spec in profile.probes:
                with self.subTest(profile=profile.name, probe=spec.name):
                    source = profiles.program_path(spec).read_text(encoding="utf-8")
                    self.assertIsNone(
                        re.search(r"^\s*(BEGIN|END)\b", source, re.M),
                        f"{spec.program} has a BEGIN or END block",
                    )

    def test_threadlife_fork_and_exit_use_the_same_process_filter(self):
        """A worker's parent_pid is its TID, so it cannot filter all forks."""
        import re

        spec = profiles.load("light").probe("threadlife")
        source = profiles.program_path(spec).read_text(encoding="utf-8")
        for event in ("fork", "exit"):
            with self.subTest(event=event):
                predicate = re.search(
                    rf"tracepoint:sched:sched_process_{event}\s*/([^/]+)/",
                    source,
                )
                self.assertIsNotNone(predicate)
                self.assertEqual(predicate.group(1).strip(), "pid == $1")

    def test_every_probe_reports_readiness_from_a_running_timer(self):
        """An attach banner printed by userspace cannot prove BPF is active."""
        for profile in profiles.load_all():
            for spec in profile.probes:
                with self.subTest(profile=profile.name, probe=spec.name):
                    source = profiles.program_path(spec).read_text(encoding="utf-8")
                    self.assertIn("interval:ms:100", source)
                    self.assertIn('printf("PERFORMER_READY\\n")', source)
                    self.assertIn("if (!@_performer_ready)", source)

    def test_oncpu_is_required_everywhere(self):
        for profile in profiles.load_all():
            self.assertTrue(profile.probe("oncpu").required, profile.name)

    def test_oncpu_frequency_uses_the_installed_program_at_99_hz(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = profiles.probe_command(
                profiles.ONCPU, 42, 10, bpftrace="bpftrace",
                generated_dir=Path(tmp),
            )
            self.assertEqual(command[0], "bpftrace")
            self.assertEqual(command[1:3], ["-B", "line"])
            self.assertEqual(command[-3:], [
                str(profiles.program_path(profiles.ONCPU)), "42", "10",
            ])
            self.assertFalse((Path(tmp) / "oncpu.bt").exists())

    def test_oncpu_frequency_rewrites_exactly_one_attachpoint(self):
        original = profiles.program_path(profiles.ONCPU).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            command = profiles.probe_command(
                profiles.ONCPU, 42, 10, bpftrace="bpftrace", oncpu_hz=999,
                generated_dir=Path(tmp),
            )
            generated = Path(command[-3])
            self.assertEqual(generated.name, "oncpu.bt")
            self.assertEqual(command[-2:], ["42", "10"])
            changed = generated.read_text(encoding="utf-8")
            self.assertEqual(changed, original.replace("profile:hz:99\n", "profile:hz:999\n", 1))
        self.assertEqual(profiles.program_path(profiles.ONCPU).read_text(encoding="utf-8"), original)

    def test_oncpu_frequency_rejects_bad_values_and_missing_attachpoint(self):
        for value in (0, 4001, 99.5, True):
            with self.subTest(value=value), self.assertRaises(PerformerError):
                profiles.validate_oncpu_hz(value)
        with tempfile.TemporaryDirectory() as tmp:
            program = Path(tmp) / "oncpu.bt"
            program.write_text("profile:hz:100\n", encoding="utf-8")
            with mock.patch.object(profiles, "probes_dir", return_value=Path(tmp)):
                for hz in (99, 999):
                    with self.subTest(hz=hz), self.assertRaisesRegex(PerformerError, "exactly one"):
                        profiles.probe_command(
                            profiles.ONCPU, 42, 10, bpftrace="bpftrace", oncpu_hz=hz,
                            generated_dir=Path(tmp) / "generated",
                        )


class NameValidationTests(unittest.TestCase):
    def test_path_traversal_is_refused_before_any_file_access(self):
        for name in ("../../etc/passwd", "..", "a/b", "Upper", "", "with space"):
            with self.subTest(name=name):
                with self.assertRaises(PerformerError):
                    profiles.load(name)

    def test_unknown_profile_lists_the_available_ones(self):
        with self.assertRaises(PerformerError) as ctx:
            profiles.load("nonsense")
        self.assertIn("standard", str(ctx.exception))

    def test_probe_program_must_be_a_bare_name(self):
        for program in ("../../etc/passwd", "/etc/passwd", "sub/dir.bt"):
            with self.subTest(program=program):
                spec = profiles.ProbeSpec(name="x", program=program)
                with self.assertRaises(PerformerError):
                    profiles.program_path(spec)


class DocumentValidationTests(unittest.TestCase):
    def _parse(self, document):
        return profiles.parse_profile(document, source="<test>")

    def _minimal(self, **overrides):
        document = {
            "name": "unit",
            "probes": [{"name": "oncpu", "program": "oncpu.bt"}],
        }
        document.update(overrides)
        return document

    def test_minimal_document(self):
        profile = self._parse(self._minimal())
        self.assertEqual(profile.probe_names, ("oncpu",))
        self.assertEqual(profile.max_duration_s, 300)

    def test_unknown_top_level_key_is_refused(self):
        with self.assertRaises(PerformerError) as ctx:
            self._parse(self._minimal(probs=[]))
        self.assertIn("unknown key", str(ctx.exception))

    def test_unknown_probe_key_is_refused(self):
        """A misspelled 'thresholds' would silently change the measurement."""
        with self.assertRaises(PerformerError) as ctx:
            self._parse(
                self._minimal(
                    probes=[{"name": "oncpu", "program": "oncpu.bt", "threshold": 5}]
                )
            )
        self.assertIn("unknown key", str(ctx.exception))

    def test_missing_probes_is_refused(self):
        with self.assertRaises(PerformerError):
            self._parse({"name": "unit"})
        with self.assertRaises(PerformerError):
            self._parse({"name": "unit", "probes": []})

    def test_duplicate_probe_is_refused(self):
        with self.assertRaises(PerformerError) as ctx:
            self._parse(
                self._minimal(
                    probes=[
                        {"name": "oncpu", "program": "oncpu.bt"},
                        {"name": "oncpu", "program": "oncpu.bt"},
                    ]
                )
            )
        self.assertIn("twice", str(ctx.exception))

    def test_non_numeric_threshold_is_refused(self):
        with self.assertRaises(PerformerError) as ctx:
            self._parse(
                self._minimal(
                    probes=[
                        {
                            "name": "futex",
                            "program": "futex.bt",
                            "thresholds": {"min_us": "fifty"},
                        }
                    ]
                )
            )
        self.assertIn("must be a number", str(ctx.exception))

    def test_bad_max_duration_is_refused(self):
        for value in (0, -1, "sixty", 1.5):
            with self.subTest(value=value):
                with self.assertRaises(PerformerError):
                    self._parse(self._minimal(max_duration_s=value))

    def test_program_must_be_a_bt_file(self):
        with self.assertRaises(PerformerError):
            self._parse(
                self._minimal(probes=[{"name": "oncpu", "program": "oncpu.py"}])
            )


class FileLoadingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        import os

        self._previous = os.environ.get("PERFORMER_PROFILES_DIR")
        os.environ["PERFORMER_PROFILES_DIR"] = str(self.tmp)

        def restore():
            if self._previous is None:
                os.environ.pop("PERFORMER_PROFILES_DIR", None)
            else:
                os.environ["PERFORMER_PROFILES_DIR"] = self._previous

        self.addCleanup(restore)

    def test_name_inside_the_file_must_match_the_file_name(self):
        """The manifest records the name in the file, so they cannot differ."""
        (self.tmp / "mine.yaml").write_text(
            "name: theirs\nprobes:\n  - name: oncpu\n    program: oncpu.bt\n"
        )
        with self.assertRaises(PerformerError) as ctx:
            profiles.load("mine")
        self.assertIn("calls itself", str(ctx.exception))

    def test_malformed_yaml_is_reported_with_its_line(self):
        (self.tmp / "broken.yaml").write_text("name: broken\nprobes: |\n  oops\n")
        with self.assertRaises(PerformerError) as ctx:
            profiles.load("broken")
        self.assertIn("line 2", str(ctx.exception))

    def test_a_valid_custom_profile_loads(self):
        (self.tmp / "mine.yaml").write_text(
            "name: mine\n"
            "description: custom\n"
            "max_duration_s: 30\n"
            "probes:\n"
            "  - name: futex\n"
            "    program: futex.bt\n"
            "    thresholds:\n"
            "      min_us: 25\n"
        )
        profile = profiles.load("mine")
        self.assertEqual(profile.max_duration_s, 30)
        self.assertEqual(profile.probe("futex").thresholds, {"min_us": 25.0})
        self.assertEqual(profile.probe("futex").probe_args(7, 90), ("7", "90", "25"))


if __name__ == "__main__":
    unittest.main()
