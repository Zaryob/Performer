"""Probe output -> bundle files, one emitter at a time.

These run against the same canned outputs the fake bpftrace produces, and
check both the documents written and, through the bundle validator, that they
satisfy the schemas.
"""

from __future__ import annotations

import datetime as _dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from performer import emit, layout
from performer.bundle import BundleBuilder
from performer.jsonschema import load_schema
from performer.parse import syscalls

from .fake_bpftrace import OUTPUT_BY_PROBE

STARTED = _dt.datetime(2026, 8, 6, 14, 25, 30, tzinfo=_dt.timezone.utc)


def probe_output(probe: str, comm: str = "app") -> str:
    return "Attaching 3 probes...\n\n" + OUTPUT_BY_PROBE[probe].format(comm=comm)


class EmitterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.builder = BundleBuilder(self.tmp, label="unit", started_at=STARTED)
        self.context = emit.EmitContext(builder=self.builder, duration_s=20.0)

    def run_emitter(self, probe: str, text=None) -> emit.EmitResult:
        return emit.emit(probe, self.context, text or probe_output(f"{probe}.bt"))

    def read(self, relpath: str):
        import json

        return json.loads((self.builder.root / relpath).read_text())

    def assert_matches_schema(self, relpath: str, schema_name: str) -> None:
        validator = load_schema(layout.schema_path(schema_name))
        errors = [str(e) for e in validator.validate(self.read(relpath))]
        self.assertEqual(errors, [], f"{relpath} does not satisfy {schema_name}")


class OnCpuTests(EmitterTestCase):
    def test_writes_folded_stacks(self):
        result = self.run_emitter("oncpu")
        self.assertEqual(result.outputs, [layout.STACK_ONCPU])
        self.assertIsNotNone(result.fold_stats)
        text = (self.builder.root / layout.STACK_ONCPU).read_text()
        self.assertIn("pthread_mutex_lock", text)

    def test_empty_output_writes_nothing(self):
        result = self.run_emitter("oncpu", "Attaching 1 probe...\n\n")
        self.assertEqual(result.outputs, [])
        self.assertTrue(any("no on-CPU stacks" in w for w in result.warnings))


class OffCpuTests(EmitterTestCase):
    def test_writes_stacks_histogram_and_state_split(self):
        result = self.run_emitter("offcpu")
        self.assertIn(layout.STACK_OFFCPU, result.outputs)
        self.assertIn(layout.HIST_OFFCPU_DURATION, result.outputs)
        self.assertIn(layout.HIST_OFFCPU_BY_STATE, result.outputs)
        self.assert_matches_schema(layout.HIST_OFFCPU_DURATION, "hist.schema.json")
        self.assert_matches_schema(layout.HIST_OFFCPU_BY_STATE, "table.schema.json")

    def test_task_state_is_named_not_filtered(self):
        """Futex waits are interruptible, so that bucket must survive."""
        self.run_emitter("offcpu")
        rows = self.read(layout.HIST_OFFCPU_BY_STATE)["rows"]
        states = {row[0] for row in rows}
        self.assertIn("interruptible", states)
        self.assertIn("uninterruptible", states)
        # Sorted by blocked time, and the interruptible (futex) side leads.
        self.assertEqual(rows[0][0], "interruptible")

    def test_values_are_microseconds_not_samples(self):
        result = self.run_emitter("offcpu")
        self.assertTrue(any("us blocked" in note for note in result.notes), result.notes)


class RunqlatTests(EmitterTestCase):
    def test_histogram_and_per_comm_stats_share_one_document(self):
        result = self.run_emitter("runqlat")
        self.assertEqual(result.outputs, [layout.HIST_RUNQLAT])
        self.assert_matches_schema(layout.HIST_RUNQLAT, "hist.schema.json")
        document = self.read(layout.HIST_RUNQLAT)
        keys = [s["key"] for s in document["series"]]
        self.assertIn("", keys)  # the global histogram
        self.assertIn("worker", keys)  # aggregate for all TIDs named worker
        stats = next(s for s in document["series"] if s["key"] == "worker")["stats"]
        self.assertEqual(stats["count"], 1347)

    def test_no_samples_is_reported(self):
        result = self.run_emitter("runqlat", "Attaching 2 probes...\n\n")
        self.assertEqual(result.outputs, [])
        self.assertTrue(any("no run queue" in w for w in result.warnings))

    def test_legacy_comm_map_name_is_still_readable(self):
        result = self.run_emitter(
            "runqlat",
            "@runq_us:\n[1]  1 |@|\n"
            "@runq_by_thread[worker]: count 1, average 1, total 1\n",
        )
        self.assertEqual(result.outputs, [layout.HIST_RUNQLAT])
        keys = [s["key"] for s in self.read(layout.HIST_RUNQLAT)["series"]]
        self.assertIn("worker", keys)

    def test_impossible_wait_discards_polluted_stats(self):
        # A 20-second probe cannot observe a 2G-microsecond wait. A historical
        # system-wide runqlat probe could produce this bin from idle TID 0 on
        # multiple CPUs; its per-comm stats were contaminated too.
        output = (
            "@runq_us:\n"
            "[1, 2)  5 |@|\n"
            "[2G, 4G)  1 |@|\n"
            "@runq_by_comm[swapper/0]: count 1, average 2147483648, total 2147483648\n"
        )
        result = self.run_emitter("runqlat", output)
        self.assertEqual(result.outputs, [])
        self.assertTrue(any("discarded" in w for w in result.warnings))
        self.assertFalse((self.builder.root / layout.HIST_RUNQLAT).exists())


class FutexTests(EmitterTestCase):
    def test_addresses_are_hex_and_ranked(self):
        result = self.run_emitter("futex")
        self.assertIn(layout.HIST_FUTEX_BY_ADDR, result.outputs)
        self.assert_matches_schema(layout.HIST_FUTEX_BY_ADDR, "table.schema.json")
        rows = self.read(layout.HIST_FUTEX_BY_ADDR)["rows"]
        self.assertTrue(rows[0][0].startswith("0x"))
        # Ranked by total wait: identifying the hot lock is the whole point.
        self.assertEqual([row[1] for row in rows], sorted((r[1] for r in rows), reverse=True))

    def test_average_is_derived_from_the_call_count(self):
        self.run_emitter("futex")
        row = self.read(layout.HIST_FUTEX_BY_ADDR)["rows"][0]
        _addr, total_us, calls, avg_us = row
        self.assertAlmostEqual(avg_us, round(total_us / calls, 2), places=2)

    def test_a_dominant_lock_is_called_out(self):
        result = self.run_emitter("futex")
        note = next(n for n in result.notes if "lock addresses" in n)
        self.assertIn("98%", note)

    def test_stacks_and_duration_histogram_are_written(self):
        result = self.run_emitter("futex")
        self.assertIn(layout.STACK_FUTEX, result.outputs)
        self.assertIn(layout.HIST_FUTEX_DURATION, result.outputs)

    def test_each_lock_is_joined_to_the_code_that_takes_it(self):
        # An address is an identity, not an answer.  The probe keys on
        # [uaddr, ustack] together precisely because the two separate
        # aggregations cannot be joined afterwards.
        result = self.run_emitter("futex")
        self.assertIn(layout.HIST_FUTEX_SITES, result.outputs)
        self.assert_matches_schema(layout.HIST_FUTEX_SITES, "table.schema.json")
        doc = self.read(layout.HIST_FUTEX_SITES)
        self.assertEqual(
            [column["id"] for column in doc["columns"]],
            ["addr", "stack", "total_us", "calls", "avg_us"],
        )
        top = doc["rows"][0]
        self.assertTrue(top[0].startswith("0x"))
        # Root first, so it reads as a call path rather than a stack dump, and
        # naming the code that took the lock rather than where every contended
        # mutex ends up.
        self.assertTrue(top[1].startswith("WorkerThread::run()"))
        self.assertIn("TimerWheel::arm", top[1])
        self.assertTrue(top[1].endswith("__lll_lock_wait"))

    def test_one_address_can_be_taken_from_several_places(self):
        self.run_emitter("futex")
        rows = self.read(layout.HIST_FUTEX_SITES)["rows"]
        hottest = rows[0][0]
        sites = [row for row in rows if row[0] == hottest]
        self.assertGreater(len(sites), 1)
        # And ranked, so the first one is the answer.
        self.assertEqual(
            [row[2] for row in rows], sorted((row[2] for row in rows), reverse=True)
        )

    def test_site_totals_do_not_exceed_the_address_total(self):
        # The two maps are filled from the same events, so a site total larger
        # than its address total would mean one of the parsers is wrong.
        self.run_emitter("futex")
        by_addr = {
            row[0]: row[1] for row in self.read(layout.HIST_FUTEX_BY_ADDR)["rows"]
        }
        summed: dict = {}
        for addr, _stack, total_us, _calls, _avg in self.read(
            layout.HIST_FUTEX_SITES
        )["rows"]:
            summed[addr] = summed.get(addr, 0) + total_us
        for addr, total in summed.items():
            self.assertLessEqual(total, by_addr[addr], f"{addr} sites exceed its total")

    def test_symbolised_duplicate_stacks_are_one_site_with_summed_counts(self):
        # Distinct bpftrace stack IDs can print the same symbols with different
        # offsets. The displayed site and its call count must be aggregated.
        stack = (
            "    __lll_lock_wait+{offset}\n"
            "    ___pthread_mutex_lock+20\n"
            "    hold_shared_lock(int)+30\n"
            "    worker_loop()+40\n"
        )
        text = (
            "@futex_site[123,\n" + stack.format(offset=1) + "]: 200\n"
            "@futex_site[123,\n" + stack.format(offset=2) + "]: 300\n"
            "@futex_site_cnt[123,\n" + stack.format(offset=1) + "]: 2\n"
            "@futex_site_cnt[123,\n" + stack.format(offset=2) + "]: 3\n"
        )
        self.run_emitter("futex", text)
        rows = self.read(layout.HIST_FUTEX_SITES)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2:], [500, 5, 100.0])

    def test_the_hist_parser_stays_quiet_about_the_joined_map(self):
        # A single probe prints stack keyed and scalar keyed maps into one
        # stream.  @futex_site[<addr>, <stack>] opens with a scalar, which is
        # what the stack map detector used to miss -- and eight spurious
        # warnings per run would end up in the manifest.
        result = self.run_emitter("futex")
        self.assertEqual(
            [w for w in result.warnings if "futex_site" in w or "unrecognised" in w], []
        )


class WakeupTests(EmitterTestCase):
    def test_edges_are_parsed_and_ranked(self):
        result = self.run_emitter("wakeup")
        self.assertEqual(result.outputs, [layout.GRAPH_WAKEUP_EDGES])
        self.assert_matches_schema(
            layout.GRAPH_WAKEUP_EDGES, "wakeup_edges.schema.json"
        )
        document = self.read(layout.GRAPH_WAKEUP_EDGES)
        self.assertEqual(document["total_edges"], 5)
        self.assertEqual(document["edges"][0]["from_tid"], 4101)
        self.assertGreaterEqual(
            document["edges"][0]["count"], document["edges"][1]["count"]
        )

    def test_a_hub_is_called_out(self):
        result = self.run_emitter("wakeup")
        self.assertTrue(any("busiest waker" in n for n in result.notes))

    def test_malformed_edges_are_counted_not_silently_dropped(self):
        result = self.run_emitter(
            "wakeup", "@wake_cnt[4101]: 5\n@wake_cnt[4101, 4102]: 7\n"
        )
        self.assertTrue(any("could not be parsed" in w for w in result.warnings))
        self.assertEqual(self.read(layout.GRAPH_WAKEUP_EDGES)["total_edges"], 1)


class SyscallLatTests(EmitterTestCase):
    def setUp(self):
        super().setUp()
        # The captured bpftrace fixture uses x86_64 syscall numbers.
        mapping = syscalls.SyscallTable(dict(syscalls._X86_64), "fixture x86_64 table")
        mock = patch.object(emit.syscall_parse, "load", return_value=mapping)
        mock.start()
        self.addCleanup(mock.stop)

    def test_numbers_become_names(self):
        result = self.run_emitter("syscall_lat")
        self.assertEqual(result.outputs, [layout.HIST_SYSCALL_LATENCY])
        self.assert_matches_schema(layout.HIST_SYSCALL_LATENCY, "table.schema.json")
        rows = self.read(layout.HIST_SYSCALL_LATENCY)["rows"]
        names = {row[0] for row in rows}
        self.assertIn("futex", names)
        self.assertIn("epoll_wait", names)

    def test_unmappable_numbers_are_flagged_not_guessed(self):
        result = self.run_emitter("syscall_lat")
        self.assertTrue(any("not in the mapping" in w for w in result.warnings))
        names = {row[0] for row in self.read(layout.HIST_SYSCALL_LATENCY)["rows"]}
        self.assertIn("syscall_9999", names)

    def test_the_mapping_source_is_recorded(self):
        self.run_emitter("syscall_lat")
        self.assertIn("syscall_lat.bt", self.read(layout.HIST_SYSCALL_LATENCY)["source"])


class ThreadlifeTests(EmitterTestCase):
    def test_rates_use_the_measured_duration(self):
        self.context.duration_s = 20.0
        result = self.run_emitter("threadlife")
        self.assertIn(layout.HIST_THREADLIFE, result.outputs)
        self.assert_matches_schema(layout.HIST_THREADLIFE, "table.schema.json")
        row = next(r for r in self.read(layout.HIST_THREADLIFE)["rows"] if r[0] == "app")
        self.assertEqual(row[1], 42)
        self.assertAlmostEqual(row[3], 42 / 20.0, places=3)

    def test_a_short_run_still_reports_the_right_rate(self):
        """A run cut short must not report a rate as if it lasted the full time."""
        self.context.duration_s = 5.0
        self.run_emitter("threadlife")
        row = next(r for r in self.read(layout.HIST_THREADLIFE)["rows"] if r[0] == "app")
        self.assertAlmostEqual(row[3], 42 / 5.0, places=3)

    def test_lifetime_histogram_is_written(self):
        result = self.run_emitter("threadlife")
        self.assertIn(layout.HIST_THREAD_LIFETIME, result.outputs)
        self.assert_matches_schema(layout.HIST_THREAD_LIFETIME, "hist.schema.json")

    def test_no_churn_is_reported_as_such(self):
        result = self.run_emitter("threadlife", "Attaching 2 probes...\n\n")
        self.assertEqual(result.outputs, [layout.HIST_THREADLIFE])
        self.assertTrue(any("no threads were created" in w for w in result.warnings))
        self.assert_matches_schema(layout.HIST_THREADLIFE, "table.schema.json")
        self.assertEqual(self.read(layout.HIST_THREADLIFE)["rows"], [])
        self.assertEqual(self.read(layout.HIST_THREADLIFE)["total_rows"], 0)


class TimersTests(EmitterTestCase):
    def test_probe_names_become_call_names_with_rates(self):
        self.context.duration_s = 20.0
        result = self.run_emitter("timers")
        self.assertEqual(result.outputs, [layout.HIST_TIMERS])
        self.assert_matches_schema(layout.HIST_TIMERS, "table.schema.json")
        rows = self.read(layout.HIST_TIMERS)["rows"]
        names = {row[0] for row in rows}
        self.assertIn("timerfd_settime", names)
        self.assertIn("clock_nanosleep", names)
        row = next(r for r in rows if r[0] == "timerfd_settime")
        self.assertAlmostEqual(row[2], 121400 / 20.0, places=2)

    def test_the_busiest_call_is_called_out(self):
        result = self.run_emitter("timers")
        self.assertTrue(any("timerfd_settime" in n for n in result.notes))


class RegistryTests(EmitterTestCase):
    def test_an_unregistered_probe_says_so_rather_than_failing(self):
        result = emit.emit("nosuchprobe", self.context, "@x: 1\n")
        self.assertEqual(result.outputs, [])
        self.assertTrue(any("no parser" in w for w in result.warnings))

    def test_no_emitter_raises_on_empty_input(self):
        """A probe that produced nothing must degrade, never crash the run."""
        for probe in emit.EMITTERS:
            with self.subTest(probe=probe):
                result = emit.emit(probe, self.context, "")
                self.assertEqual(result.outputs, [])


if __name__ == "__main__":
    unittest.main()
