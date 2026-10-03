"""bpftrace stack maps -> FlameGraph folded format.

bpftrace prints a map with stack keys like this::

    @cpu[
        __schedule+723
        schedule+70
    ,
        __lll_lock_wait+40
        pthread_mutex_lock+274
        main+1080
    , app]: 1493

Each stack frame occupies a whole line and is indented; keys are separated by
``,`` at column zero; the entry ends with ``]: <value>``.  That structure is
what makes the parse safe: C++ symbols are full of commas
(``std::map<int, int>::find``) and would defeat any attempt to split the key
list on commas alone, but they can never start a line at column zero.

The output is the folded format ``frame;frame;frame <value>``, which
``flamegraph.pl`` consumes unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from . import strip_scratch_maps

UNKNOWN = "[unknown]"

#: A trailing ``+123`` or ``+0x7b`` symbol offset.  Anchored at the end so
#: that ``operator+`` and ``operator++`` survive intact.
_OFFSET_RE = re.compile(r"\+(?:0x[0-9a-fA-F]+|[0-9]+)$")

#: A bare address bpftrace could not resolve to a symbol.
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]+$")

#: A trailing module annotation, e.g. ``main+66 (/usr/local/bin/app)``.  The
#: leading space and the absolute path distinguish it from a C++ signature
#: like ``TimerWheel::arm()``.
_MODULE_RE = re.compile(r"\s+\((?:/[^)]*)\)$")

#: ``@name[`` or ``@name:`` at column zero starts a map entry.
_ENTRY_START_RE = re.compile(r"^@([A-Za-z_][A-Za-z0-9_]*)?(\[|:)")

_TERMINATOR_RE = re.compile(r"\]:\s*(.*)$")

#: ``[2, 4)   8 |@@@|`` -- a histogram bucket, which belongs to parse/hist.py.
_HIST_BUCKET_RE = re.compile(r"^\s*[\[(][^\])]*[\])]\s+\d+\s*\|?")


class StackKey(Sequence[str]):
    """A stack key: frames in bpftrace order, leaf first."""

    __slots__ = ("frames",)

    def __init__(self, frames: Iterable[str]) -> None:
        self.frames: Tuple[str, ...] = tuple(frames)

    def __len__(self) -> int:
        return len(self.frames)

    def __getitem__(self, index):  # type: ignore[override]
        return self.frames[index]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StackKey) and other.frames == self.frames

    def __hash__(self) -> int:
        return hash(self.frames)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StackKey({list(self.frames)!r})"


Key = Union[StackKey, str]


@dataclass(frozen=True)
class MapEntry:
    keys: Tuple[Key, ...]
    value: int

    def stacks(self) -> List[StackKey]:
        return [k for k in self.keys if isinstance(k, StackKey)]

    def scalars(self) -> List[str]:
        return [k for k in self.keys if not isinstance(k, StackKey)]


@dataclass
class ParseResult:
    maps: Dict[str, List[MapEntry]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    #: Lines bpftrace printed that are not map output ("Attaching 1 probe...").
    preamble: List[str] = field(default_factory=list)

    def entries(self, name: str) -> List[MapEntry]:
        return self.maps.get(name, [])


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_maps(text: str) -> ParseResult:
    """Parse every map bpftrace printed.

    Unparseable entries are recorded as warnings rather than raising: half a
    profile is worth keeping, and the manifest has somewhere to say so.
    """
    result = ParseResult()
    lines = strip_scratch_maps(text).splitlines()
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        if not line.startswith("@"):
            if line.strip():
                result.preamble.append(line.strip())
            index += 1
            continue

        match = _ENTRY_START_RE.match(line)
        if match is None:
            result.warnings.append(f"unrecognised map line: {line.strip()[:120]}")
            index += 1
            continue

        name = match.group(1) or ""
        if match.group(2) == ":":
            trailer = line.split(":", 1)[1].strip()
            index += 1
            if not trailer:
                # "@name:" with the value on following lines is a histogram.
                # It belongs to parse/hist.py; skipping its bucket lines here
                # keeps them out of the preamble.
                while index < len(lines) and _HIST_BUCKET_RE.match(lines[index]):
                    index += 1
                continue
            # Unkeyed scalar: "@name: 42"
            _add_entry(result, name, (), trailer)
            continue

        block, index = _collect_entry(lines, index)
        if block is None:
            result.warnings.append(
                f"map '@{name}' is truncated; bpftrace output ends mid entry"
            )
            break
        keytext, raw_value = block
        keys, key_warnings = _parse_keys(keytext)
        result.warnings.extend(f"@{name}: {w}" for w in key_warnings)
        _add_entry(result, name, keys, raw_value)

    return result


def _add_entry(
    result: ParseResult, name: str, keys: Tuple[Key, ...], raw_value: str
) -> None:
    if _STATS_VALUE_RE.match(raw_value.strip()):
        return  # a stats() entry; parse/hist.py handles it
    value = _parse_value(raw_value)
    if value is None:
        result.warnings.append(
            f"@{name}: value {raw_value.strip()[:60]!r} is not a plain count; skipped"
        )
        return
    result.maps.setdefault(name, []).append(MapEntry(keys=keys, value=value))


#: ``count 5, average 3, total 15`` -- a stats() map, which belongs to
#: parse/hist.py.  A single probe prints both shapes into one stream, so this
#: parser has to recognise the other one and stay quiet about it.
_STATS_VALUE_RE = re.compile(r"^(count|average|total|min|max|sum)\s+-?\d+")


def _parse_value(raw: str) -> Optional[int]:
    text = raw.strip()
    try:
        return int(text)
    except ValueError:
        return None


def _collect_entry(
    lines: List[str], start: int
) -> Tuple[Optional[Tuple[List[str], str]], int]:
    """Gather the lines of one keyed entry.

    Returns ``((key_lines, raw_value), next_index)``.  The first key line is
    whatever followed ``@name[`` on the opening line, so a single line entry
    and a multi line one take the same path.
    """
    opening = lines[start]
    head, _, remainder = opening.partition("[")
    del head
    collected: List[str] = []
    index = start
    current = remainder

    while True:
        # A terminator only counts on a line that is not a stack frame, since
        # a symbol could contain "]:" in principle.
        if not (current[:1].isspace() and current.strip()):
            match = _TERMINATOR_RE.search(current)
            if match is not None:
                collected.append(current[: match.start()])
                return (collected, match.group(1)), index + 1
        collected.append(current)
        index += 1
        if index >= len(lines):
            return None, index
        current = lines[index]


def _parse_keys(key_lines: List[str]) -> Tuple[Tuple[Key, ...], List[str]]:
    """Turn the raw key lines into ordered keys.

    Indented lines are stack frames and are collected into a placeholder;
    everything else is literal key text.  Only after the frames are safely out
    of the way is the remaining text split on commas.
    """
    warnings: List[str] = []
    stacks: List[StackKey] = []
    pieces: List[str] = []
    frames: List[str] = []

    def flush_frames() -> None:
        if frames:
            stacks.append(StackKey(frames))
            pieces.append(f"\x00{len(stacks) - 1}\x00")
            frames.clear()

    for line in key_lines:
        if line[:1].isspace() and line.strip():
            frames.append(line.strip())
        else:
            flush_frames()
            pieces.append(line)
    flush_frames()

    keys: List[Key] = []
    for piece in "".join(pieces).split(","):
        token = piece.strip()
        if token.startswith("\x00") and token.endswith("\x00"):
            try:
                keys.append(stacks[int(token.strip("\x00"))])
                continue
            except (ValueError, IndexError):  # pragma: no cover - internal bug
                warnings.append("lost a stack placeholder while parsing keys")
                continue
        if "\x00" in token:  # pragma: no cover - malformed output
            warnings.append(f"mixed stack and scalar in one key: {token!r}")
            token = token.replace("\x00", "")
        # An empty piece is an empty stack: bpftrace prints nothing at all
        # when it could not walk one.
        keys.append(token if token else StackKey(()))

    return tuple(keys), warnings


# --------------------------------------------------------------------------
# folding
# --------------------------------------------------------------------------


def clean_frame(frame: str, *, strip_offsets: bool = True) -> str:
    """Normalise one frame for the folded output."""
    text = frame.strip()
    if not text:
        return UNKNOWN
    text = _MODULE_RE.sub("", text)
    if strip_offsets:
        text = _OFFSET_RE.sub("", text)
    if _ADDRESS_RE.match(text):
        # An address bpftrace could not symbolise is exactly the failure the
        # quality block exists to report, so it is named, not hidden.
        return UNKNOWN
    # ';' separates frames in the folded format and must not appear inside one.
    return text.replace(";", ":")


@dataclass(frozen=True)
class OnCpuLayout:
    """Key positions in ``@cpu[kstack, ustack, comm, tid]``.

    The optional final key is absent in historical three-key stack maps.
    """

    kernel: Optional[int] = 0
    user: Optional[int] = 1
    comm: Optional[int] = 2
    tid: Optional[int] = 3


@dataclass
class FoldStats:
    """Symbolisation quality, summed over samples rather than over stacks.

    A broken stack that was sampled a thousand times matters a thousand times
    more than one sampled once, and it is the sample weighted number that
    decides whether the flame graph is usable.
    """

    total_frames: int = 0
    unknown_frames: int = 0
    total_samples: int = 0
    stacks: int = 0

    @property
    def unknown_ratio(self) -> float:
        if self.total_frames <= 0:
            return 0.0
        return self.unknown_frames / self.total_frames


def fold_oncpu(
    entries: Iterable[MapEntry],
    *,
    layout: OnCpuLayout = OnCpuLayout(),
    annotate_kernel: bool = False,
    strip_offsets: bool = True,
) -> Tuple[List[Tuple[str, int]], FoldStats]:
    """Collapse ``@cpu[kstack, ustack, comm, tid]`` into folded lines.

    Frames are emitted root first (bpftrace prints them leaf first), thread
    name at the bottom and kernel frames on top of user frames, which is the
    layout ``flamegraph.pl`` and every FlameGraph reader expects.
    """
    folded: Dict[str, int] = {}
    stats = FoldStats()

    for entry in entries:
        frames: List[str] = []
        comm = _key_at(entry, layout.comm)
        thread_id = _key_at(entry, layout.tid)
        tid = (
            int(thread_id)
            if isinstance(thread_id, str) and thread_id.isdecimal()
            else 0
        )
        has_root = bool(isinstance(comm, str) and comm) or tid > 0
        if has_root:
            root = (
                clean_frame(comm, strip_offsets=False)
                if isinstance(comm, str) and comm
                else "[unnamed]"
            )
            if tid > 0:
                root += f" [tid={tid}]"
            frames.append(root)

        user = _key_at(entry, layout.user)
        if isinstance(user, StackKey):
            frames.extend(
                clean_frame(f, strip_offsets=strip_offsets) for f in reversed(user.frames)
            )
        kernel = _key_at(entry, layout.kernel)
        if isinstance(kernel, StackKey):
            kernel_frames = [
                clean_frame(f, strip_offsets=strip_offsets) for f in reversed(kernel.frames)
            ]
            if annotate_kernel:
                kernel_frames = [f"{f}_[k]" for f in kernel_frames]
            frames.extend(kernel_frames)

        if len(frames) <= (1 if has_root else 0):
            # Neither stack could be walked; keep the sample but say so, or it
            # silently disappears from the totals.
            frames.append(UNKNOWN)

        stack_frames = frames[1:] if has_root else frames
        stats.stacks += 1
        stats.total_samples += entry.value
        stats.total_frames += entry.value * len(stack_frames)
        stats.unknown_frames += entry.value * sum(
            1 for f in stack_frames if f == UNKNOWN
        )

        line = ";".join(frames)
        folded[line] = folded.get(line, 0) + entry.value

    ordered = sorted(folded.items(), key=lambda item: (-item[1], item[0]))
    return ordered, stats


def _key_at(entry: MapEntry, index: Optional[int]) -> Optional[Key]:
    if index is None or index >= len(entry.keys):
        return None
    return entry.keys[index]


def format_folded(entries: Iterable[Tuple[str, int]]) -> str:
    return "".join(f"{stack} {value}\n" for stack, value in entries)


def parse_oncpu(
    text: str,
    *,
    map_name: str = "cpu",
    annotate_kernel: bool = False,
) -> Tuple[List[Tuple[str, int]], FoldStats, List[str]]:
    """bpftrace stdout -> (folded entries, quality stats, warnings)."""
    result = parse_maps(text)
    entries = result.entries(map_name)
    if not entries and map_name not in result.maps:
        # The probe was edited and the map renamed: use another stack map
        # rather than reporting an empty profile.  Only a stack keyed one --
        # a plain value map such as '@offcpu_by_state' folds into nonsense,
        # and its absence usually means bpftrace was killed mid dump.
        stack_maps = [
            (name, candidates)
            for name, candidates in result.maps.items()
            if any(
                isinstance(key, StackKey) for entry in candidates for key in entry.keys
            )
        ]
        if stack_maps:
            name, entries = stack_maps[0]
            result.warnings.append(
                f"expected map '@{map_name}', found '@{name}'; used it instead"
            )
    folded, stats = fold_oncpu(entries, annotate_kernel=annotate_kernel)
    return folded, stats, result.warnings
