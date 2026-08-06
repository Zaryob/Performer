"""Parsers turning bpftrace output into the normalised bundle formats.

bpftrace's text output is not a stable interface -- it changes between
releases -- so every parser here is driven by fixtures captured from real
output of more than one version (``tests/fixtures/bpftrace/``).  When a new
version breaks something, the fix is a new fixture and a failing test, not a
guess.

    stacks.py    stack maps  -> FlameGraph folded format   [M1]
    hist.py      hist()/lhist() -> bucket lists            [M2]
    syscalls.py  syscall id -> name                        [M2]
"""

from __future__ import annotations
