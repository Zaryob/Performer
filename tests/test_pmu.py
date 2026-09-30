"""PMU count quality and bundle contract without requiring host PMU access."""

from __future__ import annotations

import os
import struct
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
