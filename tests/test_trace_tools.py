# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Diagnostic wire/privacy/failure tests. Fake IOKit calls are not native proof."""
import ctypes as c
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
from lb_wire import Snapshot, TraceRecord, TraceBatch, BufferInfo, decode_snapshot, decode_batch, parse_kernel_messages
from analyze_trace import findings, markdown
import collect_macos as collector
from macho import inspect_macho

def snapshot_bytes(capabilities=7):
    value = Snapshot()
    value.abi, value.structBytes, value.policy, value.capabilities = 1, c.sizeof(value), 1, capabilities
    value.vendor, value.device, value.subvendor, value.subdevice = 0x8086, 0x64a0, 0x144d, 0xc1dd
    return bytes(value)

class Function:
    def __init__(self, fn): self.fn = fn
    def __call__(self, *args): return self.fn(*args)

class FakeKit:
    def __init__(self):
        self.closed, self.released, self.calls, self.unmapped = [], [], [], []
        self.memory = (c.c_uint8 * 4096)()
        self.map_size, self.map_status, self.unmap_status, self.trace_status = 4096, 0, 0, 0
        self.IOServiceMatching = Function(lambda name: 1)
        self.IOServiceGetMatchingServices = Function(lambda port, match, out: self.put(out, 10))
        self.IOIteratorNext = Function(lambda iterator: 11)
        self.IOServiceOpen = Function(lambda service, task, kind, out: self.put(out, 12))
        self.IOServiceClose = Function(lambda connection: self.closed.append(connection) or 0)
        self.IOObjectRelease = Function(lambda item: self.released.append(item) or 0)
        self.IOConnectCallMethod = Function(self.call)
        self.IOConnectMapMemory64 = Function(self.map)
        self.IOConnectUnmapMemory64 = Function(self.unmap)

    @staticmethod
    def put(pointer, value):
        pointer._obj.value = value
        return 0

    def call(self, connection, selector, inputs, count, input_struct, input_size,
             outputs, output_count, output_struct, output_size):
        self.calls.append(selector)
        if selector == 1:
            outputs[0] = 4096
            self.put(output_count, 1)
            return 0
        if selector == 0: data = snapshot_bytes()
        elif selector == 2: data = bytes(BufferInfo(4096, 1, 4096, 1, 0))
        elif selector == 3:
            if self.trace_status: return self.trace_status
            batch = TraceBatch()
            batch.abi, batch.oldestSequence, batch.nextSequence = 1, 1, 3
            if inputs[0] == 0:
                batch.count = 2
                batch.records[0] = TraceRecord(1, 100, 0, 0, 1, 0, 0, 0)
                batch.records[1] = TraceRecord(2, 200, 0, 0, 7, 0, 0, 0)
            data = bytes(batch)
        else: raise AssertionError('Unexpected synthetic selector')
        c.memmove(output_struct, data, len(data))
        self.put(output_size, len(data))
        return 0

    def map(self, connection, kind, task, address, size, options):
        if self.map_status: return self.map_status
        self.put(address, c.addressof(self.memory))
        self.put(size, self.map_size)
        return 0

    def unmap(self, connection, kind, task, address):
        self.unmapped.append(address)
        return self.unmap_status

    def info_call(self, selector, structure): return bytes(BufferInfo(4096, 1, 4096, 1, 0))

class TraceToolTests(unittest.TestCase):
    def test_wire_sizes_and_snapshot_never_promote_claim_to_proof(self):
        self.assertEqual([c.sizeof(t) for t in (Snapshot, TraceRecord, TraceBatch, BufferInfo)], [104, 48, 800, 24])
        result = decode_snapshot(snapshot_bytes(0x20007))
        self.assertTrue(result['claims_metal'])
        self.assertFalse(result['metal_execution_verified'])
        self.assertEqual(result['policy_name'], 'explicit_opt_in_missing')

    def test_snapshot_rejects_length_version_and_reserved(self):
        for data in (b'', snapshot_bytes()[:-1], snapshot_bytes() + b'\0'):
            with self.assertRaises(ValueError): decode_snapshot(data)
        for name, value in [('abi', 2), ('structBytes', 103), ('reserved', 1)]:
            s = Snapshot.from_buffer_copy(snapshot_bytes())
            setattr(s, name, value)
            with self.assertRaises(ValueError): decode_snapshot(bytes(s))

    def test_trace_checks_cursor_order_unused_bytes_and_reserved(self):
        batch = TraceBatch()
        batch.abi, batch.count, batch.oldestSequence, batch.nextSequence, batch.overwritten = 1, 2, 9, 11, 8
        batch.records[0] = TraceRecord(9, 100, 1, 2, 22, 0, 3, 0)
        batch.records[1] = TraceRecord(10, 200, 1, 2, 23, 0, 3, 0)
        original = bytes(batch)
        self.assertEqual([r['sequence'] for r in decode_batch(original)['records']], [9, 10])
        for change in (lambda b: setattr(b, 'abi', 2), lambda b: setattr(b, 'count', 17),
                       lambda b: setattr(b, 'nextSequence', 10),
                       lambda b: setattr(b.records[1], 'sequence', 12),
                       lambda b: setattr(b.records[0], 'reserved', 1),
                       lambda b: setattr(b.records[2], 'arg0', 1)):
            bad = TraceBatch.from_buffer_copy(original)
            change(bad)
            with self.assertRaises(ValueError): decode_batch(bytes(bad))

    def test_log_decoder_bounds_output_and_excludes_surrounding_private_text(self):
        pattern = '[LunarBridge] seq={} event=24 status=0xe00002bd context=3 a0=4096 a1=0'
        messages = ['private name: ' + pattern.format(i) + ' private serial' for i in range(1, 301)]
        messages += [None, {'eventMessage': 'private'}, pattern.format(2**64), 'unrelated serial/name',
                     '[LunarBridge] seq=1 event=4294967296 status=0x00000000 context=0 a0=0 a1=0']
        rows = parse_kernel_messages(messages)
        self.assertEqual((len(rows), rows[0]['sequence'], rows[-1]['sequence']), (256, 45, 300))
        self.assertNotIn('private', json.dumps(rows))
        self.assertEqual(rows[0]['status'], '0xe00002bd')

    def test_registry_is_exact_allowlist_even_for_lb_prefixed_private_keys(self):
        result = collector.filtered_registry([{'LBDriverABI': 1, 'IOPlatformSerialNumber': 'private',
            'LBPrivateSerial': 'private', 'IORegistryEntryName': 'private', 'LBUnknown': 'private',
            'IORegistryEntryChildren': [{'LBPCIIdentifier': 0x64a08086, 'IOUser': 'private'}]}])
        self.assertEqual(result, [{'LBDriverABI': 1}, {'LBPCIIdentifier': 0x64a08086}])
        self.assertNotIn('private', json.dumps(result))

    def test_triage_separates_expected_blocks_and_actual_errors(self):
        report = {'synthetic': True, 'snapshot': decode_snapshot(snapshot_bytes(0x20007)),
                  'trace_overwritten': 2, 'cpu_memory_test': {'state': 'failed'}, 'trace_error': 'fixture',
                  'trace': [{'sequence': i, 'event': event, 'status': '0x00000000'}
                            for i, event in enumerate((1, 4, 6, 24, 26, 27, 29), 1)]}
        indexed = {item['code']: item for item in findings(report)}
        self.assertEqual(indexed['MMIO_DEFAULT_OFF']['level'], 'info')
        self.assertEqual(indexed['GPU_NOT_IMPLEMENTED']['level'], 'info')
        self.assertEqual(indexed['CPU_ROUNDTRIP_FAILED']['level'], 'error')
        self.assertEqual(indexed['BAR_MAP_FAILED']['level'], 'error')
        self.assertIn('METAL_CLAIM_UNVERIFIED', indexed)
        self.assertIn('START_NOT_COMPLETED', indexed)
        self.assertIn('synthetische Testdaten', markdown(report))
        self.assertIn('TRACE_READ_FAILED', indexed)

    def test_collector_retains_snapshot_and_trace_after_cpu_test_failure(self):
        kit = FakeKit()
        with mock.patch.object(collector.c, 'CDLL', side_effect=[kit, object()]), \
             mock.patch.object(collector.c.c_uint32, 'in_dll', return_value=c.c_uint32(1)), \
             mock.patch.object(collector, 'cpu_memory_roundtrip', side_effect=RuntimeError('CPU allocation: 0xe00002bd')):
            report = collector.collect_iokit(memory_test=True)
        self.assertEqual(report['snapshot']['device'], 0x64a0)
        self.assertEqual([r['event'] for r in report['trace']], [1, 7])
        self.assertEqual(report['cpu_memory_test']['state'], 'failed')
        self.assertEqual(kit.closed, [12])
        self.assertEqual(kit.released, [11, 10])

    def test_collector_retains_snapshot_after_trace_failure_and_closes_handles(self):
        kit = FakeKit()
        kit.trace_status = 0xe00002bd
        with mock.patch.object(collector.c, 'CDLL', side_effect=[kit, object()]), \
             mock.patch.object(collector.c.c_uint32, 'in_dll', return_value=c.c_uint32(1)):
            report = collector.collect_iokit()
        self.assertIn('snapshot', report)
        self.assertIn('0xe00002bd', report['trace_error'])
        self.assertEqual(kit.closed, [12])
        self.assertNotIn(4, kit.calls)

    def test_cpu_self_test_uses_only_own_buffer_and_unmaps(self):
        kit = FakeKit()
        report = collector.cpu_memory_roundtrip(kit, 12, 1, kit.info_call)
        self.assertEqual(report['state'], 'passed')
        self.assertFalse(report['gpu_bound'])
        self.assertFalse(report['gpu_execution_tested'])
        self.assertEqual((kit.memory[0], kit.memory[-1]), (0x4c, 0x42))
        self.assertEqual(len(kit.unmapped), 1)
        self.assertEqual(kit.calls, [1])

    def test_cpu_self_test_unmaps_after_size_or_initial_data_failure(self):
        for corrupt_size in (True, False):
            kit = FakeKit()
            if corrupt_size: kit.map_size = 8192
            else: kit.memory[100] = 0xa5
            with self.assertRaises(ValueError): collector.cpu_memory_roundtrip(kit, 12, 1, kit.info_call)
            self.assertEqual(len(kit.unmapped), 1)

    def test_failed_unmap_or_map_is_not_recorded_as_passing(self):
        kit = FakeKit()
        kit.unmap_status = 0xe00002bd
        with self.assertRaises(RuntimeError): collector.cpu_memory_roundtrip(kit, 12, 1, kit.info_call)
        kit = FakeKit()
        kit.map_status = 0xe00002bd
        with self.assertRaises(RuntimeError): collector.cpu_memory_roundtrip(kit, 12, 1, kit.info_call)
        self.assertEqual(kit.unmapped, [])

    def test_real_cross_compiled_files_are_objects_and_cannot_be_installed(self):
        objects = sorted((ROOT/'build/macos').glob('*.o'))
        self.assertEqual(len(objects), 5)
        for path in objects:
            result = inspect_macho(path.read_bytes())
            self.assertEqual((result['filetype'], result['architecture']), (1, 'x86_64'))
            with self.assertRaises(ValueError): inspect_macho(path.read_bytes(), require_kext=True)

    @unittest.skipIf(sys.platform == 'darwin', 'Windows guard test')
    def test_native_collector_and_linker_refuse_windows_without_fake_success(self):
        for script, args in [('collect_macos.py', ['--out', str(ROOT/'build/not-a-native-report')]),
                             ('build_native.py', ['--sdk', str(ROOT/'build/not-a-sdk')])]:
            proc = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT/'tools'/script), *args],
                                  capture_output=True, timeout=30)
            self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((ROOT/'build/not-a-native-report').exists())

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(TraceToolTests))
    (ROOT/'tests/trace-tools-test-report.json').write_text(json.dumps({
        'test_type': 'Python ABI, privacy, triage and simulated IOKit failure tests; actual Mach-O object format checks',
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'successful': result.wasSuccessful(),
        'iokit_calls_simulated': True, 'native_macos_hardware_tests': 0,
    }, indent=2)+'\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
