"""Manifest schema and cross field rules."""

from __future__ import annotations

import datetime as _dt
import json
import unittest

from oxfscope.errors import ManifestError
from oxfscope.manifest import (
    ProbeResult,
    Quality,
    TargetInfo,
    build_manifest,
    derive_status,
    make_run_id,
    validate_manifest,
)

from . import FIXTURES

MANIFEST_FIXTURES = FIXTURES / "manifest"


def _load(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.pop("_why_invalid", None)
    return doc


def _sample(**overrides):
    started = _dt.datetime(2026, 8, 6, 14, 25, 30, tzinfo=_dt.timezone.utc)
    kwargs = dict(
        label="baseline",
        profile="standard",
        duration_s=60.0,
        started_at=started,
        target=TargetInfo(
            pid=205852,
            comm="Hisar_Seri_Uret",
            thread_count_start=315,
            thread_count_end=318,
        ),
        probes=[ProbeResult(name="oncpu", status="ok")],
        quality=Quality(),
        tool_versions={"oxfscope": "0.1.0", "bpftrace": "0.20.2", "kernel": "5.15.0"},
    )
    kwargs.update(overrides)
    return build_manifest(**kwargs)


class FixtureTests(unittest.TestCase):
    def test_valid_fixtures_pass(self):
        paths = sorted(MANIFEST_FIXTURES.glob("valid_*.json"))
        self.assertTrue(paths, "no valid fixtures found")
        for path in paths:
            with self.subTest(fixture=path.name):
                self.assertEqual(validate_manifest(_load(path)), [])

    def test_invalid_fixtures_are_rejected(self):
        paths = sorted(MANIFEST_FIXTURES.glob("invalid_*.json"))
        self.assertTrue(paths, "no invalid fixtures found")
        for path in paths:
            with self.subTest(fixture=path.name):
                raw = json.loads(path.read_text(encoding="utf-8"))
                reason = raw.get("_why_invalid", "?")
                self.assertNotEqual(
                    validate_manifest(_load(path)),
                    [],
                    f"{path.name} should have been rejected: {reason}",
                )


class BuildTests(unittest.TestCase):
    def test_built_manifest_validates(self):
        self.assertEqual(validate_manifest(_sample()), [])

    def test_run_id_encodes_time_and_label(self):
        started = _dt.datetime(2026, 8, 6, 14, 25, 30, tzinfo=_dt.timezone.utc)
        self.assertEqual(make_run_id("baseline", started), "20260806T142530Z-baseline")

    def test_run_id_rejects_path_traversal_labels(self):
        started = _dt.datetime(2026, 8, 6, 14, 25, 30, tzinfo=_dt.timezone.utc)
        for label in ("../evil", "a b", "lbl;rm -rf /", "", "x" * 65):
            with self.subTest(label=label):
                with self.assertRaises(ManifestError):
                    make_run_id(label, started)

    def test_naive_datetime_is_treated_as_utc(self):
        naive = _dt.datetime(2026, 8, 6, 14, 25, 30)
        self.assertEqual(make_run_id("baseline", naive), "20260806T142530Z-baseline")

    def test_optional_blocks_are_omitted_when_empty(self):
        doc = _sample()
        for key in ("tags", "ended_at", "target_died_at", "warnings"):
            self.assertNotIn(key, doc)


class StatusTests(unittest.TestCase):
    def test_all_ok_is_ok(self):
        probes = [ProbeResult("oncpu", "ok"), ProbeResult("futex", "ok")]
        self.assertEqual(derive_status(probes, target_died=False), "ok")

    def test_lost_events_downgrade_to_partial(self):
        probes = [ProbeResult("futex", "ok", events_lost=12)]
        self.assertEqual(derive_status(probes, target_died=False), "partial")

    def test_dead_target_downgrades_to_partial(self):
        probes = [ProbeResult("oncpu", "ok")]
        self.assertEqual(derive_status(probes, target_died=True), "partial")

    def test_all_failed_is_failed(self):
        probes = [ProbeResult("oncpu", "failed"), ProbeResult("futex", "failed")]
        self.assertEqual(derive_status(probes, target_died=False), "failed")

    def test_skipped_probes_do_not_count(self):
        probes = [ProbeResult("oncpu", "ok"), ProbeResult("timers", "skipped")]
        self.assertEqual(derive_status(probes, target_died=False), "ok")

    def test_nothing_requested_is_failed(self):
        self.assertEqual(derive_status([], target_died=False), "failed")

    def test_status_is_derived_when_not_given(self):
        doc = _sample(probes=[ProbeResult("futex", "partial", events_lost=99)])
        self.assertEqual(doc["status"], "partial")


class SemanticTests(unittest.TestCase):
    def test_duplicate_probe_names_are_rejected(self):
        doc = _sample(probes=[ProbeResult("oncpu"), ProbeResult("oncpu")])
        problems = validate_manifest(doc)
        self.assertTrue(any("duplicate probe" in p for p in problems), problems)

    def test_lying_status_is_rejected(self):
        doc = _sample(probes=[ProbeResult("oncpu", "failed")], status="ok")
        problems = validate_manifest(doc)
        self.assertTrue(any("status is 'ok'" in p for p in problems), problems)

    def test_frame_ratio_must_match_sample_counts(self):
        doc = _sample(
            quality=Quality(
                unknown_frame_ratio=0.02,
                unknown_frame_samples=500,
                total_frame_samples=1000,
            )
        )
        problems = validate_manifest(doc)
        self.assertTrue(any("unknown_frame_ratio" in p for p in problems), problems)

    def test_consistent_frame_ratio_passes(self):
        doc = _sample(
            quality=Quality(
                unknown_frame_ratio=0.5,
                unknown_frame_samples=500,
                total_frame_samples=1000,
            )
        )
        self.assertEqual(validate_manifest(doc), [])

    def test_ended_before_started_is_rejected(self):
        started = _dt.datetime(2026, 8, 6, 14, 25, 30, tzinfo=_dt.timezone.utc)
        doc = _sample(started_at=started, ended_at=started - _dt.timedelta(seconds=5))
        problems = validate_manifest(doc)
        self.assertTrue(any("ended_at" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
