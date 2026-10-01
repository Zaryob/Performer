"""PMU count quality and bundle contract without requiring host PMU access."""

from __future__ import annotations

import os
import struct
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from performer import layout, pmu
from performer.jsonschema import load_schema


class PmuDocumentTests(unittest.TestCase):
    def setUp(self):
        mocked = patch.object(pmu.proc, "system_info", return_value={"cpu_model": "test CPU"})
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_multiplexed_counts_keep_raw_values_and_running_time(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(lambda: os.close(read_fd))
        self.addCleanup(lambda: os.close(write_fd))
        os.write(write_fd, struct.pack("=QQQQQ", 2, 200, 100, 40, 20))
        session = pmu.Session(123)
        session.groups = [pmu.EVENT_GROUPS[0]]
        session._started_at = 1.0
        session._ended_at = 3.0
        session._initial_count = 1
        record = pmu.ThreadCounters(123, 100, "worker", enabled_at=1.0)
        record.groups[pmu.EVENT_GROUPS[0]] = [read_fd, write_fd]
        session.threads[(123, 100)] = record

        doc = session.document()
        self.assertEqual(doc["totals"]["cycles"]["raw"], 40)
        self.assertEqual(doc["totals"]["cycles"]["scaled"], 80)
        self.assertEqual(doc["totals"]["instructions"]["scaled"], 40)
        self.assertEqual(doc["status"], "partial")
        self.assertTrue(any("less than 90%" in note for note in doc["warnings"]))
        errors = list(load_schema(layout.schema_path("pmu.schema.json")).validate(doc))
        self.assertEqual(errors, [])

    def test_zero_running_time_has_no_scaled_total(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(lambda: os.close(read_fd))
        self.addCleanup(lambda: os.close(write_fd))
        os.write(write_fd, struct.pack("=QQQQQ", 2, 200, 0, 40, 20))
        session = pmu.Session(123)
        session.groups = [pmu.EVENT_GROUPS[0]]
        session._started_at = 1.0
        session._ended_at = 3.0
        record = pmu.ThreadCounters(123, 100, "worker", enabled_at=1.0)
        record.groups[pmu.EVENT_GROUPS[0]] = [read_fd, write_fd]
        session.threads[(123, 100)] = record
        doc = session.document()
        self.assertIsNone(doc["totals"]["cycles"]["scaled"])
        self.assertEqual(doc["status"], "partial")
        errors = list(load_schema(layout.schema_path("pmu.schema.json")).validate(doc))
        self.assertEqual(errors, [])

    def test_idle_thread_with_no_scheduled_time_keeps_active_totals_valid(self):
        session = pmu.Session(123)
        session.groups = list(pmu.EVENT_GROUPS)
        session._started_at = 1.0
        session._ended_at = 3.0
        session._initial_count = 2
        active = pmu.ThreadCounters(123, 100, "active", enabled_at=1.0)
        idle = pmu.ThreadCounters(124, 101, "idle", enabled_at=1.0)
        for group in pmu.EVENT_GROUPS:
            active.samples[group] = (100, 100, 40, 20)
            idle.samples[group] = (0, 0, 0, 0)
        session.threads[(123, 100)] = active
        session.threads[(124, 101)] = idle

        doc = session.document()
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["warnings"], [])
        self.assertEqual(doc["threads_measured"], 2)
        for event in doc["events"]:
            self.assertEqual(doc["totals"][event]["scaled"], 40 if event in (
                "cycles", "branches", "cache_references"
            ) else 20)
            self.assertEqual(doc["threads"][1]["events"][event]["scaled"], 0.0)
        errors = list(load_schema(layout.schema_path("pmu.schema.json")).validate(doc))
        self.assertEqual(errors, [])

    def test_enabled_but_never_running_still_marks_multiplex_failure(self):
        session = pmu.Session(123)
        session.groups = [pmu.EVENT_GROUPS[0]]
        session._started_at = 1.0
        session._ended_at = 3.0
        record = pmu.ThreadCounters(123, 100, "worker", enabled_at=1.0)
        record.samples[pmu.EVENT_GROUPS[0]] = (100, 0, 0, 0)
        session.threads[(123, 100)] = record

        doc = session.document()
        self.assertEqual(doc["status"], "partial")
        self.assertIsNone(doc["totals"]["cycles"]["scaled"])
        self.assertTrue(any("less than 90%" in note for note in doc["warnings"]))

    def test_zero_time_on_one_group_of_active_thread_is_not_idle(self):
        session = pmu.Session(123)
        session.groups = list(pmu.EVENT_GROUPS)
        session._started_at = 1.0
        session._ended_at = 3.0
        record = pmu.ThreadCounters(123, 100, "worker", enabled_at=1.0)
        record.samples[pmu.EVENT_GROUPS[0]] = (100, 100, 40, 20)
        record.samples[pmu.EVENT_GROUPS[1]] = (0, 0, 0, 0)
        record.samples[pmu.EVENT_GROUPS[2]] = (100, 100, 40, 20)
        session.threads[(123, 100)] = record

        doc = session.document()
        self.assertEqual(doc["status"], "partial")
        self.assertEqual(doc["totals"]["cycles"]["scaled"], 40)
        self.assertIsNone(doc["totals"]["branches"]["scaled"])
        self.assertTrue(any("less than 90%" in note for note in doc["warnings"]))

    def test_nonzero_raw_count_with_zero_time_is_not_valid_zero(self):
        session = pmu.Session(123)
        session.groups = [pmu.EVENT_GROUPS[0]]
        session._started_at = 1.0
        session._ended_at = 3.0
        record = pmu.ThreadCounters(123, 100, "worker", enabled_at=1.0)
        record.samples[pmu.EVENT_GROUPS[0]] = (0, 0, 1, 0)
        session.threads[(123, 100)] = record

        doc = session.document()
        self.assertEqual(doc["status"], "partial")
        self.assertIsNone(doc["totals"]["cycles"]["scaled"])

    def test_record_never_enabled_is_not_mistaken_for_idle(self):
        session = pmu.Session(123)
        session.groups = [pmu.EVENT_GROUPS[0]]
        session._started_at = 1.0
        session._ended_at = 3.0
        record = pmu.ThreadCounters(123, 100, "worker")
        record.samples[pmu.EVENT_GROUPS[0]] = (0, 0, 0, 0)
        session.threads[(123, 100)] = record

        doc = session.document()
        self.assertEqual(doc["status"], "partial")
        self.assertIsNone(doc["totals"]["cycles"]["scaled"])

    def test_failed_thread_attachment_is_not_retried_every_scan(self):
        session = pmu.Session(123)
        session.groups = [pmu.EVENT_GROUPS[0]]
        with patch.object(pmu.proc, "read_stat", return_value=SimpleNamespace(start_time_ticks=42, comm="worker")), \
             patch.object(pmu.proc, "read_comm", return_value="worker"), \
             patch.object(session, "_open_group", side_effect=OSError(24, "too many open files")) as opener:
            session._attach(456)
            session._attach(456)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(session._attach_failures, 1)

    def test_thread_churn_releases_fds_and_preserves_counts(self):
        session = pmu.Session(123)
        session.groups = [pmu.EVENT_GROUPS[0]]
        session._started_at = time.monotonic()
        opened = []

        def open_group(_tid, _group):
            read_fd, write_fd = os.pipe()
            os.write(write_fd, struct.pack("=QQQQQ", 2, 100, 100, 40, 20))
            opened.extend((read_fd, write_fd))
            return [read_fd, write_fd]

        tids = list(range(500, 525))
        scans = [[tid] for tid in tids] + [[]]
        stats = {tid: SimpleNamespace(start_time_ticks=tid * 10, comm="worker") for tid in tids}
        with patch.object(session._stop, "wait", side_effect=[False] * len(scans) + [True]), \
             patch.object(pmu.proc, "thread_ids", side_effect=scans), \
             patch.object(pmu.proc, "read_stat", side_effect=lambda _pid, tid: stats[tid]) as read_stat, \
             patch.object(pmu.proc, "read_comm", return_value="worker"), \
             patch.object(session, "_open_group", side_effect=open_group), \
             patch.object(session, "_enable", side_effect=lambda record: setattr(record, "enabled_at", time.monotonic())):
            session._watch_threads()

        self.assertEqual(read_stat.call_count, len(tids))
        self.assertEqual(len(session.threads), len(tids))
        self.assertEqual(session._active, set())
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)
        session._ended_at = time.monotonic()
        document = session.document()
        self.assertEqual(document["threads_measured"], len(tids))
        self.assertEqual(document["totals"]["cycles"]["raw"], 40 * len(tids))
