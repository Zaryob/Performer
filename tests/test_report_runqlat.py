"""Inspect must mark implausible legacy run queue data as unusable."""

import unittest

from performer import layout
from performer.errors import BundleError
from performer.report import build_summary


class _Bundle:
    manifest = {
        "schema_version": 2,
        "duration_s": 45,
        "status": "ok",
        "quality": {
            "frame_pointers_ok": True,
            "unknown_frame_ratio": 0,
            "estimated_overhead_pct": 0,
        },
        "probes": [],
    }

    def __init__(self, bucket_lo):
        self.histogram = {
            "series": [{"key": "", "buckets": [{"lo": bucket_lo, "count": 1}]}]
        }

    def paths(self):
        return [layout.HIST_RUNQLAT]

    def read_json(self, path):
        assert path == layout.HIST_RUNQLAT
        return self.histogram


class LegacyRunqlatTests(unittest.TestCase):
    def test_impossible_wait_is_flagged(self):
        summary = build_summary(_Bundle(2_147_483_648), validate=False)
        self.assertTrue(any(
            flag.code == "runqlat_unreliable" and flag.level == "error"
            for flag in summary.flags
        ))

    def test_plausible_wait_is_kept(self):
        summary = build_summary(_Bundle(32), validate=False)
        self.assertFalse(any(
            flag.code == "runqlat_unreliable" for flag in summary.flags
        ))

    def test_legacy_scope_is_flagged_even_with_plausible_waits(self):
        bundle = _Bundle(32)
        bundle.manifest = dict(bundle.manifest, schema_version=1)
        summary = build_summary(bundle, validate=False)
        self.assertTrue(any("system-wide" in flag.message for flag in summary.flags))

    def test_malformed_artifact_does_not_hide_validation_results(self):
        for histogram in (None, [], 5, {"series": "invalid"}):
            with self.subTest(histogram=histogram):
                bundle = _Bundle(32)
                bundle.histogram = histogram
                summary = build_summary(bundle, validate=False)
                self.assertTrue(any(flag.code == "runqlat_unreadable" for flag in summary.flags))

    def test_invalid_json_is_flagged(self):
        class UnreadableBundle(_Bundle):
            def read_json(self, path):
                raise BundleError("invalid JSON")

        summary = build_summary(UnreadableBundle(32), validate=False)
        self.assertTrue(any(flag.code == "runqlat_unreadable" for flag in summary.flags))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
