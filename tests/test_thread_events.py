"""Lifecycle records survive short lifetimes, TID reuse and capture boundaries."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from performer import emit, layout
from performer.bundle import BundleBuilder
from performer.jsonschema import load_schema
from performer.parse.events import thread_events


class ThreadEventTests(unittest.TestCase):
    def test_short_lived_threads_and_reused_tids_keep_separate_generations(self):
        text = '''@_performer_start: 10000000000000000
@thread_event[10000000000000200, 2, 7, 3, renamed, 10000000000000100]: 1
@thread_event[10000000000000100, 1, 7, 3, worker, 10000000000000100]: 1
@thread_event[10000000000000300, 1, 7, 4, worker, with comma, 10000000000000300]: 1
@thread_event[10000000000000400, 2, 7, 4, worker, with comma, 10000000000000300]: 1
@thread_event[10000000000000500, 2, 9, 0, existing, 0]: 1
'''
        document, warnings, partial = thread_events(text)
        self.assertFalse(partial)
        self.assertEqual(warnings, [])
        rows = document["rows"]
        self.assertEqual([row[2] for row in rows], ["created", "exited", "created", "exited", "exited"])
        self.assertEqual(rows[0][0], "10000000000000100")
        self.assertEqual(rows[1][6], "10000000000000100")
        self.assertEqual(rows[3][6], "10000000000000300")
        self.assertEqual(rows[2][5], "worker, with comma")
        self.assertAlmostEqual(rows[1][7], 0.0001)
        self.assertEqual(rows[-1][4], None)
        self.assertEqual(rows[-1][6:], [None, None])
        self.assertEqual(list(load_schema(layout.schema_path("table.schema.json")).validate(document)), [])

    def test_budget_loss_and_bad_records_are_visible(self):
        document, warnings, partial = thread_events("@thread_event[broken]: 1\n@thread_events_dropped: 99\n")
        self.assertTrue(partial)
        self.assertTrue(document["truncated"])
        self.assertEqual(document["rows"], [])
        self.assertTrue(any("99 thread lifecycle events dropped" in warning for warning in warnings))
        self.assertTrue(any("malformed" in warning for warning in warnings))

    def test_map_loss_cannot_look_like_an_empty_complete_list(self):
        document, warnings, partial = thread_events("@_event_count[0]: 2\n")
        self.assertTrue(partial)
        self.assertTrue(document["truncated"])
        self.assertTrue(any("some records are missing" in warning for warning in warnings))

    def test_emitter_keeps_aggregate_counts_when_event_budget_is_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = BundleBuilder(Path(tmp), label="threads")
            result = emit.emit("threadlife", emit.EmitContext(builder, 2),
                               "@_performer_start: 100\n@fork_cnt[parent]: 20000\n@exit_cnt[worker]: 20000\n@thread_events_dropped: 38000\n")
            self.assertTrue(result.partial)
            counts = json.loads(builder.path_for(layout.HIST_THREADLIFE).read_text())
            self.assertEqual(sum(row[1] for row in counts["rows"]), 20000)
            self.assertIn(layout.HIST_THREAD_EVENTS, result.outputs)

    def test_new_idle_capture_and_older_aggregate_only_output_are_distinct(self):
        for text, has_events in (("@_performer_start: 100\n", True), ("@fork_cnt[parent]: 1\n", False)):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                builder = BundleBuilder(Path(tmp), label="idle")
                result = emit.emit("threadlife", emit.EmitContext(builder, 2), text)
                self.assertEqual(layout.HIST_THREAD_EVENTS in result.outputs, has_events)


if __name__ == "__main__":
    unittest.main()
