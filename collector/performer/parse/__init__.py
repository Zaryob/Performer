"""Parsers turning bpftrace output into the normalised bundle formats.

bpftrace's text output is not a stable interface -- it changes between
releases -- so every parser here is driven by fixtures captured from real
output of more than one version (``tests/fixtures/bpftrace/``).  When a new
version breaks something, the fix is a new fixture and a failing test, not a
guess.

    stacks.py    stack maps  -> FlameGraph folded format
    hist.py      hist()/lhist()/stats() and plain value maps
    syscalls.py  syscall number -> name
"""

from __future__ import annotations


def count_map_entries(text: str) -> int:
    """How many map entries a probe printed, across every output shape.

    Used by the preflight smoke test, which only needs to know whether a
    probe produced anything at all.  bpftrace always prints "Attaching N
    probes...", so testing for non-empty stdout would pass a probe that
    attached and then collected nothing -- and a histogram-only probe such as
    runqlat prints no stack maps, so testing for those alone would fail a
    probe that worked perfectly.

    The count may double count an entry that both parsers recognise. That is
    fine: the caller compares it against zero.
    """
    from . import hist, stacks

    stack_dump = stacks.parse_maps(text)
    hist_dump = hist.parse_maps(text)
    return (
        sum(len(entries) for entries in stack_dump.maps.values())
        + sum(len(series) for series in hist_dump.histograms.values())
        + sum(len(rows) for rows in hist_dump.values.values())
        + sum(len(rows) for rows in hist_dump.stats.values())
    )
