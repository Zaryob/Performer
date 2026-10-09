"""Saved off-CPU map values, including still-open waits at probe shutdown."""

from __future__ import annotations

import unittest

from performer.parse import count_map_entries
from performer.parse.offcpu import parse_pending_offcpu
from performer.parse.stacks import fold_oncpu


def pending(tid=42, start=2_000_000_000, *, comm="worker", state=1):
    return (
        f"@_off_start[{tid}]: {start}\n"
        f"@_off_state[{tid}]: {state}\n"
        f"@_off_comm[{tid}]: {comm}\n"
        f"@_off_kstack[{tid}]: \n    schedule+2\n    kernel_root+4\n\n"
        f"@_off_ustack[{tid}]: \n"
        "    std::map<int, int>::find()+4\n    wait+2\n\n"
    )


CHECKPOINT = (
    "@_off_window_start: 1000000000\n"
    "@_off_window_end: 6000000000\n"
    "@_off_min_us: 100\n"
)


class PendingOffCpuTests(unittest.TestCase):
    def test_open_wait_is_bounded_by_recorded_checkpoint(self):
        result = parse_pending_offcpu(pending() + CHECKPOINT)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.window_ns, (1_000_000_000, 6_000_000_000))
        self.assertEqual(result.total_us, 4_000_000)
        self.assertEqual(result.by_state, {"1": 4_000_000})
        folded, _ = fold_oncpu(result.entries)
        self.assertEqual(folded, [(
            "worker [tid=42];wait;std::map<int, int>::find();kernel_root;schedule",
            4_000_000,
        )])

    def test_interval_before_window_start_is_clipped(self):
        result = parse_pending_offcpu(pending(start=500_000_000) + CHECKPOINT)
        self.assertEqual(result.total_us, 5_000_000)

    def test_interval_after_last_checkpoint_is_not_extended(self):
        result = parse_pending_offcpu(pending(start=6_001_000_000) + CHECKPOINT)
        self.assertEqual(result.entries, [])
        self.assertEqual(result.warnings, [])

    def test_open_interval_keeps_probe_threshold(self):
        result = parse_pending_offcpu(pending(start=5_999_950_000) + CHECKPOINT)
        self.assertEqual(result.entries, [])

    def test_old_dump_without_checkpoint_is_unchanged(self):
        result = parse_pending_offcpu(pending())
        self.assertEqual(result.entries, [])
        self.assertEqual(result.warnings, [])

    def test_invalid_checkpoint_does_not_invent_duration(self):
        for checkpoint in (
            CHECKPOINT.replace("6000000000", "not-a-time"),
            CHECKPOINT.replace("6000000000", "500000000"),
            CHECKPOINT.replace("@_off_window_end: 6000000000\n", ""),
        ):
            with self.subTest(checkpoint=checkpoint):
                result = parse_pending_offcpu(pending() + checkpoint)
                self.assertEqual(result.entries, [])
                self.assertTrue(result.warnings)

    def test_tid_without_switchout_or_stack_is_not_added(self):
        for source in (
            pending().replace("@_off_start[42]: 2000000000\n", ""),
            pending().replace("@_off_ustack[42]: \n", "@_unrelated[42]: \n"),
            pending().replace("@_off_state[42]: 1\n", ""),
        ):
            with self.subTest(source=source):
                result = parse_pending_offcpu(source + CHECKPOINT)
                self.assertEqual(result.entries, [])

    def test_switchout_without_positive_tid_warns_instead_of_crashing(self):
        for header in ("@_off_start:", "@_off_start[0]:"):
            with self.subTest(header=header):
                source = pending().replace("@_off_start[42]:", header)
                result = parse_pending_offcpu(source + CHECKPOINT)
                self.assertEqual(result.entries, [])
                self.assertTrue(any("positive TID" in warning for warning in result.warnings))

    def test_120_same_named_open_waits_are_all_present(self):
        result = parse_pending_offcpu("".join(pending(tid) for tid in range(1000, 1120)) + CHECKPOINT)
        folded, _ = fold_oncpu(result.entries)
        self.assertEqual(len(folded), 120)
        self.assertEqual(result.total_us, 120 * 4_000_000)

    def test_pending_only_output_is_valid_preflight_data(self):
        self.assertEqual(count_map_entries(pending() + CHECKPOINT), 1)
        self.assertEqual(count_map_entries(CHECKPOINT), 0)


if __name__ == "__main__":
    unittest.main()
