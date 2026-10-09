"""Preserve native module identity and original stack text for later resolution."""
from __future__ import annotations

import os
import struct
from pathlib import Path

from . import proc
from .parse import stacks
from .parse.offcpu import parse_pending_offcpu


def elf_build_id(path: Path, *, inode=None, device=None):
    """Read GNU build-id notes from ELF32/64 without requiring binutils."""
    try:
        with path.open("rb") as stream:
            identity = os.fstat(stream.fileno())
            if inode is not None and identity.st_ino != inode:
                return None
            if device is not None and identity.st_dev != device:
                return None
            header = stream.read(64)
            if len(header) < 52 or header[:4] != b"\x7fELF" or header[4] not in (1, 2) or header[5] not in (1, 2):
                return None
            endian = "<" if header[5] == 1 else ">"
            wide = header[4] == 2
            offset = struct.unpack_from(endian + ("Q" if wide else "I"), header, 32 if wide else 28)[0]
            size, count = struct.unpack_from(endian + "HH", header, 54 if wide else 42)
            minimum = 56 if wide else 32
            if size < minimum or count > 4096:
                return None
            for index in range(count):
                stream.seek(offset + index * size)
                program = stream.read(minimum)
                if len(program) != minimum or struct.unpack_from(endian + "I", program)[0] != 4:
                    continue
                note_offset = struct.unpack_from(endian + ("Q" if wide else "I"), program, 8 if wide else 4)[0]
                note_size = struct.unpack_from(endian + ("Q" if wide else "I"), program, 32 if wide else 16)[0]
                if note_size > 16 * 1024 * 1024:
                    continue
                stream.seek(note_offset)
                data = stream.read(note_size)
                position = 0
                while position + 12 <= len(data):
                    names, descs, kind = struct.unpack_from(endian + "III", data, position)
                    name_at = position + 12
                    desc_at = name_at + ((names + 3) & ~3)
                    end = desc_at + descs
                    if end > len(data):
                        break
                    if kind == 3 and descs and data[name_at:name_at + names].rstrip(b"\0") == b"GNU":
                        return data[desc_at:end].hex()
                    position = desc_at + ((descs + 3) & ~3)
    except (OSError, ValueError, OverflowError, struct.error):
        pass
    return None


def modules_snapshot(pid: int):
    text = proc._read_text(proc.proc_path(pid, "maps")) or ""
    mappings, identities = [], {}
    for line in text.splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6 or "x" not in fields[1]:
            continue
        span, permissions, offset, device, inode, name = fields
        try:
            start, end = (int(value, 16) for value in span.split("-"))
            file_offset = int(offset, 16)
            major, minor = (int(value, 16) for value in device.split(":"))
            inode_number = int(inode)
            mapped_device = os.makedev(major, minor)
            if end <= start:
                continue
        except (ValueError, OverflowError):
            continue
        identity = (name, device, inode)
        if identity not in identities:
            if name.endswith(" (deleted)"):
                # map_files names the mapped inode; a replacement at the original
                # path must never be reported as the old module's build ID.
                binary = proc.proc_path(pid, "map_files", span)
            else:
                binary = proc.proc_path(pid, "root", name.lstrip("/")) if name.startswith("/") else None
            identities[identity] = elf_build_id(binary, inode=inode_number, device=mapped_device) if binary else None
        mappings.append({
            "path": name, "start": hex(start), "end": hex(end),
            "file_offset": hex(file_offset), "device": device, "inode": inode,
            "permissions": permissions, "build_id": identities[identity],
        })
    return mappings


def stack_evidence(text: str, probe: str):
    parsed = stacks.parse_maps(text)
    maps = dict(parsed.maps)
    if probe == "offcpu":
        maps["observed_open_offcpu_us"] = parse_pending_offcpu(text).entries
    records = []
    for name, entries in maps.items():
        for entry in entries:
            if not entry.stacks():
                continue
            records.append({
                "map": name, "value": entry.value,
                "keys": [{"frames": list(key.frames)} if isinstance(key, stacks.StackKey) else key for key in entry.keys],
            })
    return {"schema_version": 2, "source": "bpftrace", "probe": probe, "records": records}
