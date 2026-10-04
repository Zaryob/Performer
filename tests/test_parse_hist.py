"""Histogram, stats and value map parsing, from captured bpftrace output."""

from __future__ import annotations

import os
import unittest

from performer.parse import hist, syscalls

from . import FIXTURES

BPFTRACE = FIXTURES / "bpftrace"


def load(name: str) -> str:
    return (BPFTRACE / f"{name}.txt").read_text(encoding="utf-8")


class BucketTests(unittest.TestCase):
    def test_power_of_two_buckets(self):
        dump = hist.parse_maps(
            "@runq_us:\n"
            "[0]        3 |@@@   |\n"
            "[1]       12 |@@@@  |\n"
            "[2, 4)     8 |@@    |\n"
            "[4, 8)    21 |@@@@@ |\n"
        )
        buckets = dump.histograms["runq_us"][0].buckets
        self.assertEqual(
            [(b.lo, b.hi, b.count) for b in buckets],
            [(0.0, 1.0, 3), (1.0, 2.0, 12), (2.0, 4.0, 8), (4.0, 8.0, 21)],
        )

    def test_si_suffixes_are_binary(self):
        dump = hist.parse_maps("@h:\n[1K, 2K)   4 |@|\n[1M, 2M)   1 |@|\n")
        buckets = dump.histograms["h"][0].buckets
        self.assertEqual(buckets[0].lo, 1024.0)
        self.assertEqual(buckets[0].hi, 2048.0)
        self.assertEqual(buckets[1].lo, 1024.0 * 1024)

    def test_open_edges_are_null_at_both_ends(self):
        """An lhist underflow bucket has a genuinely open lower edge."""
        dump = hist.parse_maps("@h:\n(..., 0)   2 |@|\n[1M, ...)  1 |@|\n")
        buckets = dump.histograms["h"][0].buckets
        self.assertIsNone(buckets[0].lo)
        self.assertEqual(buckets[0].hi, 0.0)
        self.assertEqual(buckets[1].lo, 1024.0**2)
        self.assertIsNone(buckets[1].hi)

    def test_total_count_sums_the_buckets(self):
        dump = hist.parse_maps("@h:\n[0]   3 |@|\n[1]   4 |@|\n")
        self.assertEqual(dump.histograms["h"][0].total_count, 7)

    def test_keyed_histograms_become_separate_series(self):
        dump = hist.parse_maps(
            "@lat[worker0]:\n[0]  1 |@|\n@lat[worker1]:\n[1]  2 |@|\n"
        )
        series = dump.histograms["lat"]
        self.assertEqual([s.key for s in series], ["worker0", "worker1"])

    def test_empty_histogram_is_not_an_error(self):
        dump = hist.parse_maps("@h:\n")
        self.assertEqual(dump.histograms, {})
        self.assertEqual(dump.warnings, [])


class ValueAndStatsTests(unittest.TestCase):
    def test_value_maps(self):
        dump = hist.parse_maps("@by_addr[1234]: 500\n@by_addr[5678]: 100\n")
        self.assertEqual(dump.values["by_addr"], [("1234", 500), ("5678", 100)])

    def test_unkeyed_value_map(self):
        self.assertEqual(hist.parse_maps("@total: 42\n").values["total"], [("", 42)])

    def test_stats_maps(self):
        dump = hist.parse_maps("@t[w0]: count 5, average 3, total 15\n")
        key, stats = dump.stats["t"][0]
        self.assertEqual(key, "w0")
        # count is a cardinality; the schema types it as an integer.
        self.assertIsInstance(stats["count"], int)
        self.assertEqual(stats["count"], 5)
        self.assertEqual(stats["avg"], 3.0)
        self.assertEqual(stats["sum"], 15.0)

    def test_non_numeric_value_is_reported(self):
        dump = hist.parse_maps("@x[a]: something\n")
        self.assertTrue(any("not a plain number" in w for w in dump.warnings))


class CoexistenceTests(unittest.TestCase):
    """One probe prints several shapes into one stream.

    Each parser must skip what belongs to the other in silence: a warning per
    line of the other parser's output would bury the warnings that matter.
    """

    def test_stack_maps_are_skipped_without_warnings(self):
        dump = hist.parse_maps(
            "@offcpu_us[\n"
            "    schedule+70\n"
            ",\n"
            "    main+12\n"
            ", worker]: 5000\n"
            "@offcpu_by_state[1]: 900\n"
        )
        self.assertEqual(dump.warnings, [])
        self.assertEqual(dump.values["offcpu_by_state"], [("1", 900)])
        self.assertNotIn("offcpu_us", dump.values)

    def test_the_stack_parser_ignores_histograms_and_stats(self):
        from performer.parse import stacks

        text = (
            "@cpu[\n    f+1\n,\n    g+2\n, w]: 5\n"
            "@hist:\n[0]  3 |@|\n[1]  4 |@|\n"
            "@t[w0]: count 5, average 3, total 15\n"
        )
        result = stacks.parse_maps(text)
        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.entries("cpu")), 1)
        # Bucket lines must not be mistaken for the probe's preamble.
        self.assertEqual(result.preamble, [])

    def test_scratch_maps_are_dropped_by_both_parsers(self):
        """Probes cannot clear bookkeeping maps in END, so bpftrace prints them."""
        from performer.parse import count_map_entries, stacks

        text = (
            "Attaching 3 probes...\n\n"
            "@_off_kstack[4243]: \n        __schedule+723\n        schedule+70\n\n"
            "@_off_start[4243]: 91838741177\n"
            "@offcpu_by_state[1]: 900\n"
            "@_queued_at[4242]: 91838740021\n"
        )
        dump = hist.parse_maps(text)
        result = stacks.parse_maps(text)
        self.assertEqual(dump.warnings, [])
        self.assertEqual(result.warnings, [])
        self.assertEqual(dump.values, {"offcpu_by_state": [("1", 900)]})
        self.assertEqual(result.preamble, ["Attaching 3 probes..."])
        self.assertEqual(count_map_entries(text), 2)  # offcpu_by_state, once per parser

    def test_scratch_maps_alone_are_not_data(self):
        from performer.parse import count_map_entries

        self.assertEqual(count_map_entries("@_born_at[12]: 881\n@_born_at[13]: 882\n"), 0)
        self.assertEqual(count_map_entries("PERFORMER_READY\n@_performer_ready: 1\n"), 0)

    def test_every_probe_output_parses_cleanly(self):
        """The fixtures are what the emitters are fed in production."""
        from .fake_bpftrace import OUTPUT_BY_PROBE
        from performer.parse import stacks

        for probe, template in OUTPUT_BY_PROBE.items():
            with self.subTest(probe=probe):
                text = "Attaching 3 probes...\n\n" + template.format(comm="app")
                self.assertEqual(hist.parse_maps(text).warnings, [])
                self.assertEqual(stacks.parse_maps(text).warnings, [])


class TableDocTests(unittest.TestCase):
    def test_sorting_and_truncation_are_recorded(self):
        rows = [["a", 1], ["b", 9], ["c", 5]]
        doc = hist.table_doc(
            "t", "src", [{"id": "k", "type": "string"}, {"id": "v", "type": "int"}],
            rows, limit=2, sort_by=1,
        )
        self.assertEqual(doc["rows"], [["b", 9], ["c", 5]])
        self.assertTrue(doc["truncated"])
        self.assertEqual(doc["total_rows"], 3)

    def test_untruncated_tables_do_not_claim_to_be(self):
        doc = hist.table_doc("t", "src", [{"id": "k", "type": "string"}], [["a"]])
        self.assertNotIn("truncated", doc)
        self.assertEqual(doc["total_rows"], 1)


class SyscallTableTests(unittest.TestCase):
    def setUp(self):
        syscalls.reset_cache()
        self.addCleanup(syscalls.reset_cache)

    def test_names_resolve(self):
        table = syscalls.load()
        machine = os.uname().machine
        if machine == "x86_64":
            self.assertEqual(table.name(0), "read")
            self.assertEqual(table.name(202), "futex")
            self.assertEqual(table.name(230), "clock_nanosleep")
        elif machine == "aarch64" and "no built-in" not in table.source:
            self.assertEqual(table.name(63), "read")
            self.assertEqual(table.name(98), "futex")
            self.assertEqual(table.name(115), "clock_nanosleep")
        else:
            self.skipTest(f"no known syscall table for {machine}")

    def test_foreign_headers_do_not_override_native_syscall_numbers(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        filler = "\n".join(f"#define __NR_extra_{n} {1000 + n}" for n in range(60))
        headers = {
            "/usr/include/x86_64-linux-gnu/asm/unistd_64.h":
                filler + "\n#define __NR_read 0\n#define __NR_futex 202\n",
            "/usr/include/asm-generic/unistd.h":
                filler + "\n#define __NR_read 63\n#define __NR_futex 98\n",
        }

        def read_header(path, **kwargs):
            if str(path) not in headers:
                raise FileNotFoundError(str(path))
            return headers[str(path)]

        for machine, read_nr, futex_nr in (("aarch64", 63, 98), ("x86_64", 0, 202)):
            with self.subTest(machine=machine), patch(
                "performer.parse.syscalls.os.uname", return_value=SimpleNamespace(machine=machine)
            ), patch("performer.parse.syscalls.Path.read_text", read_header):
                syscalls.reset_cache()
                table = syscalls.load()
                self.assertEqual(table.name(read_nr), "read")
                self.assertEqual(table.name(futex_nr), "futex")

    def test_x86_does_not_fall_back_to_foreign_generic_headers(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        def read_header(path, **kwargs):
            if str(path) == "/usr/include/asm-generic/unistd.h":
                return "\n".join(f"#define __NR_extra_{n} {n}" for n in range(60))
            raise FileNotFoundError(str(path))

        with patch("performer.parse.syscalls.os.uname", return_value=SimpleNamespace(machine="x86_64")), patch(
            "performer.parse.syscalls.Path.read_text", read_header
        ):
            table = syscalls.load()
        self.assertEqual(table.source, "built-in x86_64 table")
        self.assertEqual(table.name(0), "read")

    def test_unknown_numbers_are_named_honestly(self):
        table = syscalls.load()
        self.assertFalse(table.known(999_999))
        self.assertEqual(table.name(999_999), "syscall_999999")

    def test_non_x86_fallback_does_not_guess_names(self):
        if os.uname().machine == "x86_64":
            self.skipTest("x86_64 has a built-in table")
        table = syscalls.load(prefer_headers=False)
        self.assertFalse(table.known(202))
        self.assertEqual(table.name(202), "syscall_202")

    def test_builtin_fallback_agrees_with_the_system_headers(self):
        """A wrong table would silently mislabel every row."""
        if os.uname().machine != "x86_64":
            self.skipTest("the built-in table is for x86_64")
        from_headers = syscalls.load()
        if "built-in" in from_headers.source:
            self.skipTest("no system syscall headers on this host to compare against")
        syscalls.reset_cache()
        builtin = syscalls.load(prefer_headers=False)
        mismatches = [
            (number, name, from_headers.name(number))
            for number, name in syscalls._X86_64.items()
            if from_headers.known(number) and from_headers.name(number) != name
        ]
        self.assertEqual(mismatches, [], "built-in table disagrees with the headers")
        self.assertIn("built-in", builtin.source)

    def test_source_is_reported(self):
        self.assertTrue(syscalls.load().source)


if __name__ == "__main__":
    unittest.main()
