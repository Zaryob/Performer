"""Exercise the installed GDB Python command API with a real debugger.

Only eBPF is replaced; GDB breakpoints, target thread stops, collector process,
Unix IPC, measurement bounds and final bundle validation are real.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest

from performer import layout
from performer.bundle import Bundle
from . import REPO_ROOT
from .support import fake_bpftrace


@unittest.skipUnless(os.path.isdir('/proc/self') and shutil.which('gdb') and shutil.which('g++'), 'requires Linux, gdb with Python, and g++')
class GdbCaptureTests(unittest.TestCase):
    def test_two_breakpoints_start_and_stop_capture_and_leave_target_at_end(self):
        with tempfile.TemporaryDirectory() as tmp, fake_bpftrace():
            root = Path(tmp)
            target = root / 'target'
            subprocess.run(['g++', '-O0', '-g', '-pthread', str(REPO_ROOT/'tests/target/breakpoints.cc'), '-o', str(target)], check=True, capture_output=True)
            socket_path = root / 'c.sock'
            log = root / 'collector.log'
            commands = root / 'capture.gdb'
            collector = REPO_ROOT/'collector/bin/performer'
            helper = REPO_ROOT/'debugger/performer-gdb.py'
            # GDB Python uses /usr/bin/python3 to start the collector, not
            # sys.executable (which can point to gdb inside embedded Python).
            commands.write_text(f'''set pagination off
set confirm off
start
python
import gdb, pathlib, subprocess, time
socket_path = pathlib.Path({str(socket_path)!r})
log = open({str(log)!r}, 'w')
collector = subprocess.Popen(['/usr/bin/python3', {str(collector)!r}, 'collect', '--pid', str(gdb.selected_inferior().pid), '--label', 'gdb-region', '--out', {str(root/'runs')!r}, '--profile', 'light', '--duration', '2', '--control-timeout', '5', '--control-socket', str(socket_path)], stdout=log, stderr=subprocess.STDOUT)
deadline = time.monotonic() + 10
while not socket_path.exists() and collector.poll() is None and time.monotonic() < deadline:
    time.sleep(0.01)
assert socket_path.exists(), 'collector did not become ready'
end
source {helper}
performer-connect {socket_path}
performer-record-between region_start region_end
continue
python
assert gdb.selected_frame().name() == 'region_end', gdb.selected_frame().name()
assert collector.wait(timeout=10) == 0, 'collector failed'
log.close()
end
performer-disconnect
''')
            process = subprocess.Popen(['gdb', '-q', '-nx', '-batch', '-x', str(commands), str(target)],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            try:
                output = process.communicate(timeout=30)[0]
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    process.kill()
                process.wait()
            detail = output + '\n' + (log.read_text() if log.exists() else '')
            self.assertEqual(process.returncode, 0, detail)
            self.assertIn('Performer recording stopped', output, detail)
            self.assertFalse(socket_path.exists(), detail)
            archives = list((root/'runs').glob('*.tgz'))
            self.assertEqual(len(archives), 1, detail)
            with Bundle.open(archives[0]) as bundle:
                validation = bundle.validate(verify_hashes=True)
                self.assertTrue(validation.ok, validation.flat())
                self.assertEqual(bundle.manifest['status'], 'ok', detail)
                timing = bundle.read_json(layout.META_WINDOW)
                self.assertTrue(timing['certified'], detail)
                self.assertEqual(timing['control']['ended_by'], 'controlled_stop')
                self.assertGreater(int(timing['control']['stop_client_ns']), timing['start_ns'])
                self.assertLess(timing['end_ns'] - timing['start_ns'], 1_000_000_000)
