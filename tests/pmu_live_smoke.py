"""Exercise actual Linux PMU counters without requiring bpftrace.

Run on the target host as ``sudo python3 tests/pmu_live_smoke.py``. CI may pass
``--optional`` to report a restricted virtual PMU as skipped rather than fail.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))

from performer import layout, pmu  # noqa: E402
from performer.errors import PreflightError  # noqa: E402
from performer.jsonschema import load_schema  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Check real perf_event_open counters")
    parser.add_argument("--optional", action="store_true", help="skip when the PMU is unavailable")
    args = parser.parse_args()

    session = pmu.Session(os.getpid())
    try:
        try:
            session.prepare()
        except PreflightError as error:
            if args.optional and "cycles/instructions unavailable" in str(error):
                print(f"SKIP live PMU: {error}")
                return 0
            raise

        done = threading.Event()

        def work() -> None:
            value = 1
            while not done.is_set():
                for _ in range(1000):
                    value = (value * 1664525 + 1013904223) & 0xFFFFFFFF

        session.start()
        worker = threading.Thread(target=work, name="pmu-smoke-worker")
        worker.start()
        try:
            time.sleep(2)
        finally:
            done.set()
            worker.join()
            session.stop()

        doc = session.document()
        problems = list(load_schema(layout.schema_path("pmu.schema.json")).validate(doc))
        if problems:
            raise AssertionError(f"invalid PMU document: {problems}")
        worker_row = next((row for row in doc["threads"] if row["tid"] == worker.native_id), None)
        if worker_row is None or worker_row["events"].get("cycles", {}).get("raw", 0) <= 0:
            raise AssertionError("worker thread has no measured CPU cycles")
        if doc["totals"].get("instructions", {}).get("raw", 0) <= 0:
            raise AssertionError("no instructions were measured")
        print(
            f"OK live PMU: {doc['status']}, {doc['threads_measured']} threads, "
            f"{doc['totals']['cycles']['raw']} cycles, "
            f"{doc['totals']['instructions']['raw']} instructions"
        )
        for warning in doc["warnings"]:
            print(f"  warning: {warning}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PreflightError, AssertionError) as error:
        print(f"live PMU check failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
