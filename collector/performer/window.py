"""Common kernel timestamp bounds for independently loaded bpftrace programs.

Readiness means a program is attached. It does not mean collection has begun.
Generated programs remain closed until a collector-only ``prctl`` tracepoint
receives their common start/end bounds. A future start lets the collector
confirm every acknowledgement before any program can collect an event.

Ubuntu 22.04/24.04 kernels provide ``ktime_get_boot_ns``. bpftrace 0.14's
``nsecs`` uses that helper when available, so bounds use CLOCK_BOOTTIME rather
than Python's CLOCK_MONOTONIC, which excludes time spent suspended.
"""

from __future__ import annotations

import ctypes
import errno
import platform
import re
import time
from typing import Callable, Optional, Sequence, Tuple

from .errors import PerformerError

# An unrecognised prctl option has no kernel side effect and returns EINVAL.
# A token in arg4 separates simultaneous collections in the same daemon PID.
CONTROL_OPTION = 0x5046524D
ARM_LEAD_NS = 250_000_000
ACK_PREFIX = "PERFORMER_WINDOW"
SEALED_PREFIX = "PERFORMER_WINDOW_SEALED"
_UINT64_MAX = (1 << 64) - 1
_ATTACHPOINT_RE = re.compile(
    r"\b(?:tracepoint|rawtracepoint|kprobe|kretprobe|uprobe|uretprobe|profile|"
    r"interval|software|hardware|usdt|iter|kfunc|kretfunc|fentry|fexit):"
)


def clock_now_ns() -> int:
    """Read the clock used by bpftrace's nsecs on supported Linux kernels."""
    boot_clock = getattr(time, "CLOCK_BOOTTIME", None)
    if boot_clock is None:
        # Rendering and unit tests run on non-Linux development machines too.
        return time.monotonic_ns()
    return time.clock_gettime_ns(boot_clock)


def _unsigned(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _UINT64_MAX:
        raise PerformerError(f"{label} must be an unsigned 64-bit integer")
    return value


def _bounds(start_ns: int, end_ns: int) -> None:
    _unsigned(start_ns, "collection start")
    _unsigned(end_ns, "collection end")
    if not start_ns or (end_ns and end_ns <= start_ns):
        raise PerformerError("collection bounds require a positive start and a later end (or zero for no end)")


def _code_mask(source: str) -> str:
    """Keep code offsets while hiding comment/string braces and attachpoints."""
    masked = list(source)
    position = 0
    while position < len(source):
        if source.startswith("//", position):
            stop = source.find("\n", position + 2)
            stop = len(source) if stop == -1 else stop
        elif source.startswith("/*", position):
            end = source.find("*/", position + 2)
            if end == -1:
                raise PerformerError("cannot gate a probe with an unterminated comment")
            stop = end + 2
        elif source[position] in ('"', "'"):
            quote = source[position]
            stop = position + 1
            while stop < len(source):
                if source[stop] == "\\":
                    stop += 2
                    continue
                if source[stop] == quote:
                    stop += 1
                    break
                stop += 1
            else:
                raise PerformerError("cannot gate a probe with an unterminated string")
        else:
            position += 1
            continue
        for index in range(position, min(stop, len(source))):
            if source[index] != "\n":
                masked[index] = " "
        position = stop
    return "".join(masked)


def _offcpu_clock_block(indent: str = "    ") -> str:
    """A witness bounds pending waits if a probe exits before the chosen end."""
    return (
        f"{indent}if (@_performer_start && nsecs >= @_performer_start) {{\n"
        f"{indent}    @_off_window_end = nsecs;\n"
        f"{indent}    if (@_performer_end && @_off_window_end > @_performer_end) {{\n"
        f"{indent}        @_off_window_end = @_performer_end;\n"
        f"{indent}    }}\n"
        f"{indent}    if (@_performer_sealed && @_performer_end) {{\n"
        f"{indent}        @_off_window_end = @_performer_end;\n"
        f"{indent}    }}\n"
        f"{indent}}}\n"
    )


def _observed_clock_block() -> str:
    """Keep a conservative liveness witness without charging every event."""
    return (
        "    $__performer_tick = nsecs;\n"
        "    if (@_performer_start && $__performer_tick >= @_performer_start) {\n"
        "        $__performer_witness = $__performer_tick;\n"
        "        if (@_performer_end && $__performer_witness > @_performer_end) {\n"
        "            $__performer_witness = @_performer_end;\n"
        "        }\n"
        "        if (@_performer_sealed && @_performer_end) {\n"
        "            $__performer_witness = @_performer_end;\n"
        "        }\n"
        "        @_performer_observed_end = max($__performer_witness);\n"
        "    }\n"
    )


def render_program(
    source: str,
    control_pid: int,
    *,
    offcpu: bool = False,
    control_token: int = 0,
) -> str:
    """Gate all measured handlers; keep readiness/watchdog/control ungated.

    Positional parameters and existing event predicates remain intact. The
    transformation refuses ambiguous top-level blocks instead of silently
    shipping a program whose measurements escape the collection window.

    For off-CPU pending waits, the periodic clock remains an observed witness.
    Sending the same bounds again *after* their end seals the exact endpoint
    before SIGINT/map printing; a crashed probe retains its earlier witness.
    """
    if isinstance(control_pid, bool) or not isinstance(control_pid, int) or control_pid <= 0:
        raise PerformerError("window control PID must be a positive integer")
    _unsigned(control_token, "window control token")
    if "@_performer_start" in source or "@_performer_end" in source:
        raise PerformerError("probe program is already gated")
    if offcpu:
        # These are the two known legacy assignments, in switch-out and timer.
        start_block = "    if (!@_off_window_start) {\n        @_off_window_start = nsecs;\n    }\n"
        # The switch-out block is nested one additional indentation level.
        nested_start = "        if (!@_off_window_start) {\n            @_off_window_start = nsecs;\n        }\n"
        clock_block = "    @_off_window_end = nsecs;\n    @_off_min_us = $3;\n"
        if source.count(start_block) != 1 or source.count(nested_start) != 1 or source.count(clock_block) != 1:
            raise PerformerError("offcpu probe has an unrecognised tracing clock; refusing to gate it")
        source = source.replace(start_block, "").replace(nested_start, "")
        source = source.replace(clock_block, _offcpu_clock_block())

    masked = _code_mask(source)
    depth = 0
    previous_end = 0
    opening = None
    spans = []
    observed_interval = False
    for position, character in enumerate(masked):
        if character == "{":
            if depth == 0:
                opening = position
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                raise PerformerError("cannot gate a probe with unbalanced braces")
            if depth == 0:
                header = masked[previous_end:opening]
                attachpoints = _ATTACHPOINT_RE.findall(header)
                if not attachpoints:
                    raise PerformerError("cannot gate a probe with an unrecognised top-level block")
                interval = [point == "interval:" for point in attachpoints]
                if any(interval) and not all(interval):
                    raise PerformerError("cannot gate mixed interval and measured attachpoints in one block")
                if not all(interval):
                    spans.append((opening, position, False))
                elif re.search(r"\binterval:ms:100\b", header):
                    spans.append((opening, position, True))
                    observed_interval = True
                previous_end = position + 1
    if depth or opening is None:
        raise PerformerError("cannot gate a probe with missing or unbalanced handlers")
    if not any(not observed for _opening, _closing, observed in spans):
        raise PerformerError("probe has no measured handlers to gate")
    for opening, closing, observed in reversed(spans):
        body = source[opening + 1:closing]
        if observed:
            source = source[:closing] + "\n" + _observed_clock_block() + source[closing:]
            continue
        # A handler near a boundary must use one event timestamp for both
        # acceptance and duration arithmetic. Otherwise a later nsecs call
        # could extend a duration beyond the end accepted by the gate.
        body_mask = _code_mask(body)
        clocks = list(re.finditer(r"(?<![@$])\bnsecs\b", body_mask))
        for clock in reversed(clocks):
            body = body[:clock.start()] + "$__performer_now" + body[clock.end():]
        source = (
            source[:opening + 1]
            + "\n    $__performer_now = nsecs;\n"
            + "    if (@_performer_start && $__performer_now >= @_performer_start &&\n"
            + "        (!@_performer_end || $__performer_now < @_performer_end)) {"
            + body
            + "    }\n"
            + source[closing:]
        )

    if not observed_interval:
        source += "\ninterval:ms:100\n{\n" + _observed_clock_block() + "}\n"

    offcpu_setup = ""
    if offcpu:
        offcpu_setup = (
            "    @_off_window_start = @_performer_start;\n"
            "    @_off_min_us = $3;\n"
            + _offcpu_clock_block()
        )
    control_program = (
        "\n// Private collector control; this handler must remain ungated.\n"
        "tracepoint:syscalls:sys_enter_prctl\n"
        f"/ pid == {control_pid} && args->option == {CONTROL_OPTION} &&\n"
        f"  (uint64)args->arg4 == {control_token} /\n"
        "{\n"
        "    @_performer_start = (uint64)args->arg2;\n"
        "    @_performer_end = (uint64)args->arg3;\n"
        "    @_performer_sealed = @_performer_end && nsecs >= @_performer_end;\n"
        + offcpu_setup
        + _observed_clock_block()
        + "    if (@_performer_end && nsecs >= @_performer_end) {\n"
        + f'        printf("{SEALED_PREFIX} %llu %llu\\nPERFORMER_CLOCK %llu\\n", @_performer_start, @_performer_end, nsecs);\n'
        + "    } else {\n"
        + f'        printf("{ACK_PREFIX} %llu %llu\\nPERFORMER_CLOCK %llu\\n", @_performer_start, @_performer_end, nsecs);\n'
        + "    }\n"
        + "}\n"
    )
    # Older bpftrace infers map types in source order. Declare the control
    # maps before timer/data handlers read them (notably on 0.14 and 0.20).
    return control_program + "\n// Measured probe handlers\n" + source


def signal_bounds(start_ns: int, end_ns: int, *, control_token: int = 0) -> Tuple[int, int]:
    """Deliver bounds and return the clock range in which handlers executed.

    The range can calibrate a PERFORMER_CLOCK witness: a kernel lacking the
    BOOTTIME helper must not accidentally accept bounds from another clock.
    On non-Linux developer machines the fake probe protocol supplies the
    control event; no kernel syscall is possible or necessary there.
    """
    _bounds(start_ns, end_ns)
    _unsigned(control_token, "window control token")
    if platform.system() != "Linux":
        now = clock_now_ns()
        return now, now
    libc = ctypes.CDLL(None, use_errno=True)
    control = libc.prctl
    control.restype = ctypes.c_int
    ctypes.set_errno(0)
    before_ns = clock_now_ns()
    result = control(
        ctypes.c_int(CONTROL_OPTION), ctypes.c_ulong(start_ns),
        ctypes.c_ulong(end_ns), ctypes.c_ulong(control_token), ctypes.c_ulong(0),
    )
    after_ns = clock_now_ns()
    error = ctypes.get_errno()
    if result != -1 or error != errno.EINVAL:
        raise PerformerError(
            f"could not signal the collection window through prctl (result {result}, errno {error})"
        )
    return before_ns, after_ns


def wait_for_ack(
    probes: Sequence[object],
    start_ns: int,
    end_ns: int,
    deadline_ns: int,
    *,
    sealed: bool = False,
    control_window_ns: Optional[Tuple[int, int]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], int] = clock_now_ns,
) -> None:
    """Require every live probe's exact bounds before the kernel deadline.

    A late acknowledgement is refused even if it finally appears in stdout:
    accepting it would certify a window after some events could be missed.
    """
    _bounds(start_ns, end_ns)
    _unsigned(deadline_ns, "window acknowledgement deadline")
    prefix = SEALED_PREFIX if sealed else ACK_PREFIX
    expected = re.compile(rf"^{prefix} {start_ns} {end_ns}\r?$", re.M)
    calibrated = re.compile(
        rf"^{prefix} {start_ns} {end_ns}\r?\nPERFORMER_CLOCK ([0-9]+)\r?$", re.M,
    )
    waiting = list(probes)
    while waiting:
        remaining_ns = deadline_ns - clock()
        if remaining_ns <= 0:
            names = ", ".join(probe.name for probe in waiting)
            raise PerformerError(f"collection window acknowledgement timed out: {names}")
        pending = []
        for probe in waiting:
            if not probe.alive:
                raise PerformerError(f"probe '{probe.name}' exited before acknowledging the collection window")
            output = probe.read_stdout()
            if not expected.search(output):
                pending.append(probe)
                continue
            if control_window_ns is not None:
                low, high = control_window_ns
                witnesses = [int(match.group(1)) for match in calibrated.finditer(output)]
                if not witnesses:
                    pending.append(probe)
                elif not any(low <= witness <= high for witness in witnesses):
                    raise PerformerError(
                        f"probe '{probe.name}' clock does not match CLOCK_BOOTTIME; "
                        "cannot certify a common collection window"
                    )
        waiting = pending
        # Reading stdout itself can outlive the deadline on a loaded host.
        # Recheck after the final marker instead of accepting a late barrier.
        if clock() >= deadline_ns:
            names = ", ".join(probe.name for probe in (waiting or probes))
            raise PerformerError(f"collection window acknowledgement timed out: {names}")
        if not waiting:
            for probe in probes:
                if not probe.alive:
                    raise PerformerError(f"probe '{probe.name}' exited before the collection window barrier")
        if waiting:
            sleep(min(0.01, remaining_ns / 1_000_000_000))
