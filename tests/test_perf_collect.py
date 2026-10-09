"""Native callchain evidence, precise filtering and controlled perf startup."""
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from performer import symbols
from performer.errors import PerformerError, PreflightError
from performer.perf_collect import collect_perf, control_command, fold_samples, record_command
from performer.parse.perf import parse_perf


class NativeEvidenceTests(unittest.TestCase):
    def test_gnu_build_id_in_elf32_and_elf64_both_endian(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'elf'
            for wide in (False, True):
                for endian in ('<', '>'):
                    header = bytearray(64)
                    header[:6] = b'\x7fELF' + bytes([2 if wide else 1, 1 if endian == '<' else 2])
                    struct.pack_into(endian + ('Q' if wide else 'I'), header, 32 if wide else 28, 64)
                    struct.pack_into(endian + 'HH', header, 54 if wide else 42, 56 if wide else 32, 1)
                    program = bytearray(56 if wide else 32)
                    struct.pack_into(endian + 'I', program, 0, 4)
                    struct.pack_into(endian + ('Q' if wide else 'I'), program, 8 if wide else 4, 128)
                    note = struct.pack(endian + 'III', 4, 4, 3) + b'GNU\0' + bytes.fromhex('abcd1234')
                    struct.pack_into(endian + ('Q' if wide else 'I'), program, 32 if wide else 16, len(note))
                    file.write_bytes(header + program + bytes(128 - 64 - len(program)) + note)
                    self.assertEqual(symbols.elf_build_id(file), 'abcd1234')
                    file.write_bytes(file.read_bytes()[:-2])
                    self.assertIsNone(symbols.elf_build_id(file))
            file.write_bytes(b'not ELF')
            self.assertIsNone(symbols.elf_build_id(file))
            self.assertIsNone(symbols.elf_build_id(file.with_name('missing')))

    def test_executable_mappings_keep_namespace_path_offsets_and_deleted_identity(self):
        maps = '1000-2000 r-xp 00001000 08:01 7 /opt/my app (deleted)\n2000-3000 rw-p 00002000 08:01 7 /opt/my app (deleted)\n3000-4000 r-xp 00003000 08:01 7 /opt/my app (deleted)\n'
        with patch('performer.symbols.proc._read_text', return_value=maps), patch('performer.symbols.elf_build_id', return_value='abc') as identity:
            rows = symbols.modules_snapshot(42)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['file_offset'], '0x1000')
        self.assertEqual(rows[0]['path'], '/opt/my app (deleted)')
        self.assertEqual(rows[0]['build_id'], 'abc')
        identity.assert_called_once_with(Path('/proc/42/root/opt/my app'))

    def test_raw_stack_symbols_offsets_and_addresses_survive_folding(self):
        text = '@cpu[\n    work+0x15 (/opt/app)\n    0xdeadbeef\n, worker, 42]: 3\n'
        evidence = symbols.stack_evidence(text, 'oncpu')
        self.assertEqual(evidence['records'][0]['value'], 3)
        self.assertEqual(evidence['records'][0]['keys'][0]['frames'], ['work+0x15 (/opt/app)', '0xdeadbeef'])
        self.assertEqual(evidence['records'][0]['keys'][-1], '42')


class PerfCaptureTests(unittest.TestCase):
    def test_command_starts_disabled_and_uses_bounded_dwarf_boot_clock(self):
        command = record_command('/usr/bin/perf', 42, 99, 'dwarf', 8192, Path('data'), 3, 4)
        self.assertIn('--delay=-1', command)
        self.assertIn('--control=fd:3,4', command)
        self.assertIn('CLOCK_BOOTTIME', command)
        self.assertIn('dwarf,8192', command)
        self.assertIn('256M', command)
        self.assertNotIn('dwarf,8192', record_command('perf', 42, 99, 'fp', 8192, 'data', 3, 4))

    def test_invalid_options_fail_before_tool_check_or_target_launch(self):
        for option in ({'oncpu_hz': True}, {'oncpu_hz': 4001}, {'duration_s': float('nan')}, {'duration_s': 0}, {'call_graph': 'magic'}, {'dwarf_stack_size': 1025}):
            with self.subTest(option=option), patch('performer.perf_collect.shutil.which') as executable:
                with self.assertRaises(PerformerError):
                    collect_perf(pid=42, label='unit', out_dir=Path('runs'), **option)
                executable.assert_not_called()

    def test_missing_perf_does_not_create_bundle_or_read_target(self):
        with patch('performer.perf_collect.shutil.which', return_value=None), patch('performer.perf_collect.proc.is_running') as target, patch('performer.perf_collect.BundleBuilder') as builder:
            with self.assertRaises(PreflightError):
                collect_perf(pid=42, label='unit', out_dir=Path('runs'))
            target.assert_not_called()
            builder.assert_not_called()

    def test_half_open_window_and_target_event_filter_preserve_tids(self):
        text = ''.join(f'worker 42/{tid} {stamp}: {event}:\n        400123 work+0x1 (/opt/app)\n        deadbeef [unknown] (/opt/app)\n' for tid, stamp, event in [(43, '1.000000000', 'cpu-clock'), (44, '1.999999999', 'cpu-clock'), (45, '2.000000000', 'cpu-clock'), (46, '1.500000000', 'cycles')])
        samples, warnings = parse_perf(text + 'other 99/99 1.5: cpu-clock:\n        400123 work (/opt/other)\n')
        folded, stats, selected = fold_samples(samples, 42, 1_000_000_000, 2_000_000_000)
        self.assertEqual(warnings, [])
        self.assertEqual([sample.tid for sample in selected], [43, 44])
        self.assertEqual(stats.unknown_ratio, .5)
        self.assertEqual(sum(value for _, value in folded), 2)
        self.assertTrue(any('[tid=44]' in stack for stack, _ in folded))
        self.assertEqual(selected[0].frames[1].address, 'deadbeef')

    def test_control_waits_for_ack_and_rejects_dead_recorder(self):
        ctl_read, ctl_write = os.pipe()
        ack_read, ack_write = os.pipe()
        try:
            os.write(ack_write, b'ack\n')
            control_command(Mock(alive=True), ctl_write, ack_read, 'enable', .1)
            self.assertEqual(os.read(ctl_read, 100), b'enable\n')
            with self.assertRaisesRegex(PerformerError, 'exited before disable'):
                control_command(Mock(alive=False, read_stderr=lambda: 'denied'), ctl_write, ack_read, 'disable', .1)
            with self.assertRaisesRegex(PerformerError, 'did not acknowledge'):
                control_command(Mock(alive=True), ctl_write, ack_read, 'disable', .01)
        finally:
            for fd in (ctl_read, ctl_write, ack_read, ack_write):
                os.close(fd)
