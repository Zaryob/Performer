"""An unknown CPU overhead must not be reported as a measured zero."""

import unittest
from pathlib import Path

from performer.collect import CollectOptions, _build_quality
from performer.preflight import PreflightReport
from performer.report import quality_flags, usable_overhead_pct


class OverheadQualityTests(unittest.TestCase):
    def setUp(self):
        self.options = CollectOptions(
            pid=1, label="unit", out_dir=Path("."), overhead_window_s=0.0
        )

    def test_missing_baseline_is_null_even_if_traced_cpu_is_known(self):
        quality = _build_quality(
            PreflightReport(), [], self.options, None, 76.43
        ).to_dict()
        self.assertIsNone(quality["estimated_overhead_pct"])
        self.assertEqual(quality["overhead"]["cpu_pct_during"], 76.43)
        self.assertTrue(any("could not be estimated" in note for note in quality["notes"]))
        flags = quality_flags({"quality": quality, "status": "ok", "probes": []})
        self.assertTrue(any(flag.code == "quality_note" for flag in flags))

    def test_unstable_baseline_is_null(self):
        report = PreflightReport(cpu_before={"cpu_pct": 60.0})
        quality = _build_quality(
            report, [], self.options, 100.0, 120.0, {"cpu_pct": 100.0}
        ).to_dict()
        self.assertIsNone(quality["estimated_overhead_pct"])
        self.assertTrue(any("unreliable" in note for note in quality["notes"]))

    def test_measured_zero_remains_zero(self):
        report = PreflightReport(cpu_before={"cpu_pct": 100.0})
        quality = _build_quality(
            report, [], self.options, 100.0, 90.0, {"cpu_pct": 100.0}
        ).to_dict()
        self.assertEqual(quality["estimated_overhead_pct"], 0.0)
        self.assertEqual(usable_overhead_pct(quality), 0.0)

    def test_legacy_false_zero_is_unavailable(self):
        for note in (
            "overhead could not be estimated: CPU samples were unavailable",
            "the target's untraced CPU differed by 40%; the overhead estimate is unreliable",
        ):
            with self.subTest(note=note):
                self.assertIsNone(usable_overhead_pct({
                    "estimated_overhead_pct": 0.0,
                    "notes": [note],
                }))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
