"""Explicit perf-script callchains, with raw addresses and modules preserved."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import List, Optional

_HEADER = re.compile(
    r"^\s*(?P<comm>.+?)\s+(?P<pid>[0-9]+)/(?P<tid>[0-9]+)\s+"
    r"(?:\[[0-9]+\]\s+)?(?P<time>[0-9]+(?:\.[0-9]+)?):\s+"
    r"(?:[0-9]+\s+)?(?P<event>\S+):(?:\s+(?P<inline>.*))?$"
)
_FRAME = re.compile(r"^(?P<ip>(?:0x)?[0-9a-fA-F]+)\s+(?P<sym>.*?)\s+\((?P<dso>.*)\)$")
_ADDRESS = re.compile(r"^(?:0x[0-9a-fA-F]+|[0-9a-fA-F]{8,})$")


def is_unknown(symbol: str) -> bool:
    """Keep unresolved addresses as evidence while counting them as unknown."""
    text = (symbol[:-4] if symbol.endswith("_[k]") else symbol).strip()
    return not text or text == "[unknown]" or bool(_ADDRESS.fullmatch(text))


@dataclass(frozen=True)
class PerfFrame:
    address: str
    symbol: str
    module: str

    @property
    def unknown(self) -> bool:
        return is_unknown(self.symbol)


@dataclass
class PerfSample:
    comm: str
    pid: int
    tid: int
    timestamp_s: float
    event: str
    frames: List[PerfFrame] = field(default_factory=list)
    timestamp_ns: int = 0


def timestamp_ns(value: str) -> int:
    seconds, _, fraction = value.partition(".")
    return int(seconds) * 1_000_000_000 + int((fraction + "000000000")[:9])


def parse_perf(text: str):
    """Read the explicit pid/tid export above, preserving symbol offsets/DSOs."""
    samples: List[PerfSample] = []
    warnings: List[str] = []
    current: Optional[PerfSample] = None
    inline: Optional[PerfFrame] = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        header = _HEADER.match(line)
        if header:
            current = PerfSample(
                header["comm"].strip(), int(header["pid"]), int(header["tid"]),
                float(header["time"]), header["event"],
                timestamp_ns=timestamp_ns(header["time"]),
            )
            samples.append(current)
            raw_inline = _FRAME.match((header["inline"] or "").strip())
            inline = PerfFrame(*raw_inline.groups()) if raw_inline else None
            if inline:
                current.frames.append(inline)
            continue
        frame = _FRAME.match(line.strip())
        if current is not None and frame is not None and line[:1].isspace():
            parsed = PerfFrame(*frame.groups())
            # Some exports print the sampled IP both in the header and as the
            # first callchain frame. Count that occurrence once, not twice.
            if inline is not None:
                if int(parsed.address, 16) == int(inline.address, 16):
                    current.frames.pop()
                inline = None
            current.frames.append(parsed)
        else:
            warnings.append(f"line {number}: unrecognised perf record; skipped")
            # An unsupported new event must not donate its frames to the
            # preceding valid sample.
            if not line[:1].isspace():
                current = None
                inline = None
    return samples, warnings

