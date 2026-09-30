"""bpftrace stack map parsing, driven by captured output fixtures.

bpftrace's text output is not a stable interface, so the fixtures cover more
than one release.  When a future version breaks the parser, the fix belongs
here first: a new fixture and a failing test.
"""

from __future__ import annotations

import unittest

from performer.parse.stacks import (
    UNKNOWN,
    MapEntry,
    StackKey,
    clean_frame,
    fold_oncpu,
    format_folded,
    parse_maps,
    parse_oncpu,
)

from . import FIXTURES

BPFTRACE = FIXTURES / "bpftrace"


def load(name: str) -> str:
    return (BPFTRACE / f"{name}.txt").read_text(encoding="utf-8")


class VersionFixtureTests(unittest.TestCase):
    """The same logical profile, as printed by two bpftrace generations."""

    def test_v014_structure(self):
        result = parse_maps(load("oncpu_v0.14"))
        entries = result.entries("cpu")
        self.assertEqual(len(entries), 5)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.preamble, ["Attaching 2 probes..."])

        first = entries[0]
        self.assertEqual(first.value, 1493)
        self.assertEqual(len(first.keys), 3)
        kernel, user, comm = first.keys
        self.assertIsInstance(kernel, StackKey)
        self.assertIsInstance(user, StackKey)
        self.assertEqual(comm, "TimerWheel0")
        self.assertEqual(kernel[0], "__schedule+723")
        self.assertEqual(user[0], "__lll_lock_wait+40")

    def test_v020_structure(self):
        result = parse_maps(load("oncpu_v0.20"))
        entries = result.entries("cpu")
        self.assertEqual(len(entries), 5)
        self.assertEqual(result.warnings, [])
        self.assertEqual(entries[0].value, 2044)

    def test_both_versions_fold_to_the_same_shape(self):
        for version in ("oncpu_v0.14", "oncpu_v0.20"):
            with self.subTest(version=version):
                folded, stats, warnings = parse_oncpu(load(version))
                self.assertEqual(warnings, [])
                self.assertEqual(len(folded), 5)
                self.assertGreater(stats.total_samples, 0)
                # Sorted by descending sample count, so the hot path is first.
                self.assertEqual(folded[0][1], max(v for _s, v in folded))
                self.assertTrue(folded[0][0].startswith("TimerWheel0;start_thread;"))

    def test_hex_offsets_are_stripped_like_decimal_ones(self):
        folded, _stats, _w = parse_oncpu(load("oncpu_v0.20"))
        joined = "\n".join(line for line, _v in folded)
        self.assertNotIn("+0x", joined)
        self.assertNotIn("+765", joined)
        self.assertIn("start_thread", joined)

    def test_unknown_frames_are_counted_not_hidden(self):
        _folded, stats, _w = parse_oncpu(load("oncpu_v0.20"))
        self.assertGreater(stats.unknown_frames, 0)
        self.assertGreater(stats.unknown_ratio, 0.0)
        self.assertLess(stats.unknown_ratio, 0.10)


class StackOrderTests(unittest.TestCase):
    def test_folded_line_is_root_first_kernel_on_top(self):
        folded, _stats, _w = parse_oncpu(load("oncpu_v0.14"))
        hot = folded[0][0].split(";")
        self.assertEqual(hot[0], "TimerWheel0")  # thread name at the base
        self.assertEqual(hot[1], "start_thread")  # user stack root
        self.assertEqual(hot[-1], "__schedule")  # kernel leaf on top
        self.assertIn("pthread_mutex_lock", hot)
        self.assertLess(hot.index("WorkerThread::run()"), hot.index("__lll_lock_wait"))

    def test_kernel_annotation_is_opt_in(self):
        text = load("oncpu_v0.14")
        plain, _s, _w = parse_oncpu(text)
        annotated, _s, _w = parse_oncpu(text, annotate_kernel=True)
        self.assertNotIn("_[k]", plain[0][0])
        self.assertIn("__schedule_[k]", annotated[0][0])
        self.assertNotIn("start_thread_[k]", annotated[0][0])


class EdgeCaseTests(unittest.TestCase):
    def test_cpp_symbols_with_commas_survive(self):
        """The case that defeats splitting a key list on commas."""
        folded, _stats, _w = parse_oncpu(load("oncpu_v0.20"))
        joined = "\n".join(line for line, _v in folded)
        self.assertIn("std::map<int, std::pair<long, long> >::find(int const&)", joined)

    def test_operator_symbols_keep_their_plus_signs(self):
        folded, _stats, _w = parse_oncpu(load("oncpu_edge_cases"))
        joined = "\n".join(line for line, _v in folded)
        self.assertIn("operator+(Vector const&, Vector const&)", joined)
        self.assertIn("operator++(Counter&)", joined)

    def test_module_annotation_is_removed(self):
        folded, _stats, _w = parse_oncpu(load("oncpu_edge_cases"))
        joined = "\n".join(line for line, _v in folded)
        self.assertIn(";main;", joined + ";")
        self.assertNotIn("/srv/app", joined)

    def test_semicolon_in_comm_cannot_forge_a_frame(self):
        folded, _stats, _w = parse_oncpu(load("oncpu_edge_cases"))
        line = next(line for line, _v in folded if line.startswith("weird"))
        self.assertTrue(line.startswith("weird:comm;"))

    def test_entry_with_no_stacks_and_no_comm(self):
        folded, _stats, _w = parse_oncpu(load("oncpu_edge_cases"))
        self.assertIn((UNKNOWN, 3), folded)

    def test_other_maps_are_kept_separately(self):
        result = parse_maps(load("oncpu_edge_cases"))
        self.assertIn("other_map", result.maps)
        self.assertEqual(result.entries("other_map")[0].value, 99)
        self.assertEqual(result.entries("other_map")[0].keys, ())

    def test_truncated_output_warns_instead_of_raising(self):
        result = parse_maps(load("oncpu_truncated"))
        self.assertEqual(result.entries("cpu"), [])
        self.assertTrue(any("truncated" in w for w in result.warnings), result.warnings)

    def test_empty_input(self):
        result = parse_maps("")
        self.assertEqual(result.maps, {})
        self.assertEqual(result.warnings, [])

    def test_attach_message_only(self):
        folded, stats, warnings = parse_oncpu("Attaching 2 probes...\n\n")
        self.assertEqual(folded, [])
        self.assertEqual(stats.total_samples, 0)
        self.assertEqual(warnings, [])

    def test_map_under_another_name_is_used_with_a_warning(self):
        text = "Attaching 1 probe...\n\n@samples[\n    f+1\n, \n    g+2\n, w]: 4\n"
        folded, _stats, warnings = parse_oncpu(text)
        self.assertEqual(folded, [("w;g;f", 4)])
        self.assertTrue(any("found '@samples'" in w for w in warnings), warnings)

    def test_a_plain_value_map_is_never_folded_as_stacks(self):
        """A SIGKILL mid dump leaves only the small maps; they are not stacks."""
        text = "@offcpu_by_state[1]: 6041210\n@offcpu_by_state[2]: 38900\n"
        folded, _stats, warnings = parse_oncpu(text, map_name="offcpu_us")
        self.assertEqual(folded, [])
        self.assertFalse(any("used it instead" in w for w in warnings), warnings)

    def test_stats_values_are_left_to_the_histogram_parser(self):
        """One probe prints both shapes; neither parser may warn about the other."""
        text = "@cpu[\n    f+1\n, \n    g+2\n, w]: count 4, average 2\n"
        result = parse_maps(text)
        self.assertEqual(result.entries("cpu"), [])
        self.assertEqual(result.warnings, [])

    def test_a_genuinely_unparseable_value_is_still_reported(self):
        text = "@cpu[\n    f+1\n, \n    g+2\n, w]: not-a-number\n"
        result = parse_maps(text)
        self.assertEqual(result.entries("cpu"), [])
        self.assertTrue(any("not a plain count" in w for w in result.warnings))


class CleanFrameTests(unittest.TestCase):
    def test_offsets(self):
        self.assertEqual(clean_frame("main+66"), "main")
        self.assertEqual(clean_frame("main+0x42"), "main")
        self.assertEqual(clean_frame("main"), "main")

    def test_unresolved_addresses_become_unknown(self):
        self.assertEqual(clean_frame("0x7f3c8a4419a1"), UNKNOWN)
        self.assertEqual(clean_frame(""), UNKNOWN)
        self.assertEqual(clean_frame(UNKNOWN), UNKNOWN)

    def test_offsets_can_be_kept(self):
        self.assertEqual(clean_frame("main+66", strip_offsets=False), "main+66")

    def test_separator_is_escaped(self):
        self.assertEqual(clean_frame("a;b"), "a:b")


class FoldTests(unittest.TestCase):
    def test_identical_stacks_are_summed(self):
        entries = [
            MapEntry((StackKey(["k"]), StackKey(["u"]), "w"), 3),
            MapEntry((StackKey(["k"]), StackKey(["u"]), "w"), 4),
        ]
        folded, stats = fold_oncpu(entries)
        self.assertEqual(folded, [("w;u;k", 7)])
        self.assertEqual(stats.total_samples, 7)

    def test_offsets_collapse_into_one_stack(self):
        """Two samples in the same function at different offsets are one path."""
        entries = [
            MapEntry((StackKey([]), StackKey(["work+10", "main+2"]), "w"), 5),
            MapEntry((StackKey([]), StackKey(["work+90", "main+2"]), "w"), 5),
        ]
        folded, _stats = fold_oncpu(entries)
        self.assertEqual(folded, [("w;main;work", 10)])

    def test_sample_weighted_unknown_ratio(self):
        entries = [
            MapEntry((StackKey([]), StackKey(["0x1", "0x2"]), "w"), 90),
            MapEntry((StackKey([]), StackKey(["good", "main"]), "w"), 10),
        ]
        _folded, stats = fold_oncpu(entries)
        # 180 unknown frames of 200 total: the rare good stack must not
        # rescue the average.
        self.assertAlmostEqual(stats.unknown_ratio, 0.9, places=6)

    def test_format_folded_round_trip(self):
        text = format_folded([("a;b", 2), ("c", 1)])
        self.assertEqual(text, "a;b 2\nc 1\n")


if __name__ == "__main__":
    unittest.main()
