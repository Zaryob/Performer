"""Read loss records from an uncompressed perf.data event section.

The perf file header and PERF_RECORD_LOST / LOST_SAMPLES layouts are the
Linux perf ABI. Unsupported framing remains unknown rather than zero loss.
"""
import os
import struct


def recorded_loss(path):
    try:
        with path.open('rb') as stream:
            header = stream.read(72)
            if len(header) != 72 or header[:8] not in (b'PERFILE2', b'2ELIFREP'):
                return None
            endian = '<' if header[:8] == b'PERFILE2' else '>'
            offset, length = struct.unpack_from(endian + 'QQ', header, 40)
            if offset < 72 or offset + length > os.fstat(stream.fileno()).st_size:
                return None
            stream.seek(offset)
            remaining, lost = length, 0
            while remaining:
                if remaining < 8:
                    return None
                kind, _, size = struct.unpack(endian + 'IHH', stream.read(8))
                if size < 8 or size > remaining or kind in (66, 71, 81):
                    return None  # tracing/AUX/compressed payloads have other framing
                if kind in (2, 13):
                    minimum = 24 if kind == 2 else 16
                    if size < minimum:
                        return None
                    fields = stream.read(minimum - 8)
                    lost += struct.unpack_from(endian + 'Q', fields, 8 if kind == 2 else 0)[0]
                    stream.seek(size - minimum, 1)
                else:
                    stream.seek(size - 8, 1)
                remaining -= size
            return lost
    except (OSError, OverflowError, ValueError, struct.error):
        return None
