"""Load with `source /path/to/performer-gdb.py` inside Python-enabled GDB.

Uses ordinary breakpoint command lists: no inferior function calls, shell
commands or execution-state changes inside Breakpoint.stop callbacks.
"""
from pathlib import Path
import sys

import gdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
from performer.control import request
from performer.errors import PerformerError


class PerformerSession:
    def __init__(self):
        self.path = None
        self.pid = None
        self.active = False
        self.breakpoints = []

    def target(self):
        inferior = gdb.selected_inferior()
        if not inferior.pid:
            raise gdb.GdbError("Attach to or start the target before connecting Performer.")
        if gdb.parameter("non-stop"):
            raise gdb.GdbError("Performer breakpoint capture requires GDB all-stop mode.")
        if any(not thread.is_stopped() for thread in inferior.threads()):
            raise gdb.GdbError("Stop all target threads before controlling Performer.")
        return inferior.pid

    def call(self, command):
        if self.path is None:
            raise gdb.GdbError("Use performer-connect SOCKET first.")
        if self.target() != self.pid:
            raise gdb.GdbError("Selected inferior differs from the connected Performer target.")
        try:
            return request(self.path, command, pid=self.pid)
        except PerformerError as error:
            raise gdb.GdbError(str(error))

    def clear_breakpoints(self):
        for breakpoint in self.breakpoints:
            if breakpoint.is_valid():
                breakpoint.delete()
        self.breakpoints = []


_session = PerformerSession()


class Connect(gdb.Command):
    def __init__(self):
        super().__init__("performer-connect", gdb.COMMAND_USER)

    def invoke(self, argument, from_tty):
        args = gdb.string_to_argv(argument)
        if len(args) != 1:
            raise gdb.GdbError("Usage: performer-connect SOCKET")
        if _session.active:
            raise gdb.GdbError("Stop the active capture before connecting another one.")
        pid = _session.target()
        try:
            response = request(Path(args[0]), "status", pid=pid)
        except PerformerError as error:
            raise gdb.GdbError(str(error))
        if response.get("protocol") != 1 or response.get("state") != "ready":
            raise gdb.GdbError("Collector is not ready for a new controlled capture.")
        _session.clear_breakpoints()
        _session.path, _session.pid = Path(args[0]), pid
        gdb.write(f"Performer prepared for PID {pid}; recording is off.\n")


class Start(gdb.Command):
    def __init__(self):
        super().__init__("performer-start", gdb.COMMAND_USER)

    def invoke(self, argument, from_tty):
        if argument.strip():
            raise gdb.GdbError("Usage: performer-start")
        if _session.active:
            raise gdb.GdbError("Performer is already recording.")
        response = _session.call("start")
        _session.active = True
        for breakpoint in _session.breakpoints[1:]:
            if breakpoint.is_valid():
                breakpoint.enabled = True
        gdb.write(f"Performer recording; gate start {response['start_ns']} ns BOOTTIME.\n")


class Stop(gdb.Command):
    def __init__(self):
        super().__init__("performer-stop", gdb.COMMAND_USER)

    def invoke(self, argument, from_tty):
        if argument.strip():
            raise gdb.GdbError("Usage: performer-stop")
        if not _session.active:
            raise gdb.GdbError("Performer is not recording.")
        response = _session.call("stop")
        _session.active = False
        gdb.write(f"Performer recording stopped; gate end {response['end_ns']} ns BOOTTIME. Collector is writing the bundle.\n")


class RecordBetween(gdb.Command):
    def __init__(self):
        super().__init__("performer-record-between", gdb.COMMAND_USER)

    def invoke(self, argument, from_tty):
        args = gdb.string_to_argv(argument)
        if len(args) != 2:
            raise gdb.GdbError("Usage: performer-record-between START_LOCATION END_LOCATION")
        if _session.active:
            raise gdb.GdbError("Stop the active capture first.")
        response = _session.call("status")
        if response.get("state") != "ready":
            raise gdb.GdbError("Prepare a new controlled capture before selecting its breakpoints.")
        _session.clear_breakpoints()
        created = []
        try:
            start = gdb.Breakpoint(args[0], temporary=True)
            created.append(start)
            end = gdb.Breakpoint(args[1], temporary=True)
            created.append(end)
            end.enabled = False
            start.commands = "silent\nperformer-start\ncontinue\n"
            end.commands = "silent\nperformer-stop\n"
        except Exception:
            for breakpoint in created:
                if breakpoint.is_valid():
                    breakpoint.delete()
            raise
        _session.breakpoints = created
        gdb.write("Performer will start at the first location and stop recording at the second; GDB stays stopped at the second. Use continue to run.\n")


class Disconnect(gdb.Command):
    def __init__(self):
        super().__init__("performer-disconnect", gdb.COMMAND_USER)

    def invoke(self, argument, from_tty):
        args = gdb.string_to_argv(argument)
        if args not in ([], ["--force"]):
            raise gdb.GdbError("Usage: performer-disconnect [--force]")
        if _session.active:
            try:
                _session.call("stop")
            except gdb.GdbError as error:
                if not args:
                    raise gdb.GdbError(f"{error}. Use performer-disconnect --force to reset local state after a failed capture.")
                gdb.write("Collector could not be contacted; its configured safety limit still applies.\n")
        _session.clear_breakpoints()
        _session.path, _session.pid, _session.active = None, None, False
        gdb.write("Performer disconnected.\n")


Connect()
Start()
Stop()
RecordBetween()
Disconnect()
gdb.write("Performer commands loaded: performer-connect, performer-start, performer-stop, performer-record-between, performer-disconnect.\n")
