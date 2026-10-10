# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Execute the actual preflight and trace C++ functions in the host DLL."""
import ctypes as c
import json
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parent
DLL = c.CDLL(str(Path(sys.argv.pop(1)).resolve()))

class Input(c.Structure):
    _fields_ = [(n, c.c_uint32) for n in ('vendor', 'device', 'subvendor', 'subdevice',
                'classCode', 'command', 'pmcsr', 'providerPowerState', 'optIn', 'pmCapabilityPresent')]

class Record(c.Structure):
    _fields_ = [(n, c.c_uint64) for n in ('sequence', 'ticks', 'arg0', 'arg1')] + \
               [(n, c.c_uint32) for n in ('event', 'status', 'context', 'reserved')]

class Batch(c.Structure):
    _fields_ = [('abi', c.c_uint32), ('count', c.c_uint32), ('oldestSequence', c.c_uint64),
               ('nextSequence', c.c_uint64), ('overwritten', c.c_uint64), ('records', Record * 16)]

class Ring(c.Structure):
    _fields_ = [('nextSequence', c.c_uint64), ('overwritten', c.c_uint64), ('count', c.c_uint32),
               ('reserved', c.c_uint32), ('records', Record * 128)]

DLL.lb_mmio_preflight.argtypes = [c.POINTER(Input), c.c_uint64, c.POINTER(c.c_uint32), c.POINTER(c.c_uint32)]
DLL.lb_mmio_preflight.restype = c.c_uint32
DLL.lb_buffer_request.argtypes = [c.c_uint64, c.c_uint64, c.POINTER(c.c_uint64)]
DLL.lb_buffer_request.restype = c.c_int
DLL.lb_trace_init.argtypes = [c.POINTER(Ring)]
DLL.lb_trace_push.argtypes = [c.POINTER(Ring), c.c_uint64, c.c_uint32, c.c_uint32,
                             c.c_uint32, c.c_uint64, c.c_uint64]
DLL.lb_trace_push.restype = c.c_uint64
DLL.lb_trace_read.argtypes = [c.POINTER(Ring), c.c_uint64, c.POINTER(Batch)]

def valid_input():
    return Input(0x8086, 0x64a0, 0x144d, 0xc1dd, 0x030000, 2, 0, 2, 1, 1)

class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.ring = Ring()
        self.assertEqual(DLL.lb_trace_init(c.byref(self.ring)), 1)

    def push(self, count):
        for i in range(count):
            self.assertEqual(DLL.lb_trace_push(c.byref(self.ring), i * 100, 5, 0, 3, i, i + 1), i + 1)

    def test_wire_abi_and_zeroed_unused_records(self):
        self.assertEqual(c.sizeof(Record), 48)
        self.assertEqual(c.sizeof(Batch), 800)
        batch = Batch()
        c.memset(c.byref(batch), 0xa5, c.sizeof(batch))
        self.assertEqual(DLL.lb_trace_read(c.byref(self.ring), 0, c.byref(batch)), 1)
        self.assertEqual((batch.abi, batch.count, batch.nextSequence), (1, 0, 1))
        self.assertEqual(bytes(batch.records), bytes(48 * 16))

    def test_mmio_requires_explicit_opt_in(self):
        value = valid_input()
        for disabled in (0, 2, 0xffffffff):
            value.optIn = disabled
            a, b = c.c_uint32(111), c.c_uint32(222)
            self.assertEqual(DLL.lb_mmio_preflight(c.byref(value), 0x400000, c.byref(a), c.byref(b)), 1)
            self.assertEqual((a.value, b.value), (111, 222))

    def test_mmio_rejects_each_missing_precondition(self):
        cases = [('vendor', 0x1234, 2), ('device', 0x9a49, 2), ('subvendor', 0x1234, 3),
                 ('subdevice', 0, 3), ('classCode', 0x020000, 4), ('command', 0, 5),
                 ('pmCapabilityPresent', 0, 6), ('pmcsr', 0xffff, 6), ('pmcsr', 3, 7),
                 ('providerPowerState', 0, 8), ('providerPowerState', 3, 8)]
        cases += [('command', 0xffff, 11), ('command', 0x10000, 11), ('pmcsr', 0x10000, 11)]
        for name, bad, expected in cases:
            with self.subTest(name=name, bad=bad):
                value = valid_input()
                setattr(value, name, bad)
                a, b = c.c_uint32(111), c.c_uint32(222)
                self.assertEqual(DLL.lb_mmio_preflight(c.byref(value), 0x400000, c.byref(a), c.byref(b)), expected)
                self.assertEqual((a.value, b.value), (111, 222))

    def test_mmio_mapping_boundary_and_supported_classes(self):
        value = valid_input()
        a, b = c.c_uint32(), c.c_uint32()
        limit = 0x380d8c + 4
        self.assertEqual(DLL.lb_mmio_preflight(c.byref(value), limit - 1, c.byref(a), c.byref(b)), 9)
        for kind in (0x030000, 0x038000):
            value.classCode = kind
            for length in (limit, 0xffffffffffffffff):
                self.assertEqual(DLL.lb_mmio_preflight(c.byref(value), length, c.byref(a), c.byref(b)), 0)
                self.assertEqual((a.value, b.value), (0xd8c, 0x380d8c))

    def test_mmio_null_parameters(self):
        value = valid_input()
        a, b = c.c_uint32(), c.c_uint32()
        for args in ((None, c.byref(a), c.byref(b)), (c.byref(value), None, c.byref(b)),
                     (c.byref(value), c.byref(a), None)):
            self.assertEqual(DLL.lb_mmio_preflight(args[0], 0x400000, args[1], args[2]), 11)

    def test_buffer_overflow_budget_and_boundaries(self):
        for requested, reserved, allowed, rounded in [(1, 0, 1, 4096), (4096, 0, 1, 4096),
                (4097, 0, 1, 8192), (1048576, 0, 1, 1048576), (0, 0, 0, None),
                (1048577, 0, 0, None), (2**64 - 1, 0, 0, None), (1, 8388608, 0, None),
                (4096, 8384512, 1, 4096), (4097, 8384512, 0, None), (1, 2**64 - 1, 0, None)]:
            out = c.c_uint64(0xa5)
            self.assertEqual(DLL.lb_buffer_request(requested, reserved, c.byref(out)), allowed)
            self.assertEqual(out.value, rounded if allowed else 0xa5)
        self.assertEqual(DLL.lb_buffer_request(4096, 0, None), 0)

    def test_buffer_random_sizes_against_independent_arithmetic(self):
        rng = random.Random(130)
        for _ in range(5000):
            requested = rng.randrange(0, 2000000)
            reserved = rng.randrange(0, 10000000)
            rounded = ((requested + 4095) // 4096) * 4096
            expected = 0 < requested <= 1048576 and reserved <= 8388608 and rounded + reserved <= 8388608
            out = c.c_uint64()
            self.assertEqual(bool(DLL.lb_buffer_request(requested, reserved, c.byref(out))), expected)
            if expected: self.assertEqual(out.value, rounded)

    def test_trace_batches_preserve_order_and_payload(self):
        self.push(25)
        cursor, sequences = 0, []
        for _ in range(3):
            batch = Batch()
            self.assertEqual(DLL.lb_trace_read(c.byref(self.ring), cursor, c.byref(batch)), 1)
            for row in batch.records[:batch.count]:
                self.assertEqual((row.ticks, row.arg0, row.arg1, row.event, row.context, row.reserved),
                                 ((row.sequence - 1) * 100, row.sequence - 1, row.sequence, 5, 3, 0))
                sequences.append(row.sequence)
            cursor = batch.nextSequence - 1
        self.assertEqual(sequences, list(range(1, 26)))

    def test_trace_wraparound_and_overwritten_count(self):
        self.push(1000)
        batch = Batch()
        self.assertEqual(DLL.lb_trace_read(c.byref(self.ring), 1, c.byref(batch)), 1)
        self.assertEqual((batch.oldestSequence, batch.overwritten), (873, 872))
        cursor, sequences = 0, []
        while True:
            self.assertEqual(DLL.lb_trace_read(c.byref(self.ring), cursor, c.byref(batch)), 1)
            sequences += [row.sequence for row in batch.records[:batch.count]]
            if not batch.count: break
            cursor = batch.nextSequence - 1
        self.assertEqual(sequences, list(range(873, 1001)))

    def test_invalid_trace_cursors_preserve_output(self):
        self.push(4)
        for cursor in (5, 2**64 - 1):
            batch = Batch()
            c.memset(c.byref(batch), 0xa5, c.sizeof(batch))
            before = bytes(batch)
            self.assertEqual(DLL.lb_trace_read(c.byref(self.ring), cursor, c.byref(batch)), 0)
            self.assertEqual(bytes(batch), before)

    def test_trace_corruption_and_sequence_overflow_are_rejected(self):
        self.ring.count = 129
        before = bytes(self.ring)
        self.assertEqual(DLL.lb_trace_push(c.byref(self.ring), 0, 1, 0, 0, 0, 0), 0)
        self.assertEqual(bytes(self.ring), before)
        self.ring.count = 0
        self.ring.nextSequence = 2**64 - 1
        before = bytes(self.ring)
        self.assertEqual(DLL.lb_trace_push(c.byref(self.ring), 0, 1, 0, 0, 0, 0), 0)
        self.assertEqual(bytes(self.ring), before)

    def test_null_trace_pointers_are_rejected(self):
        self.assertEqual(DLL.lb_trace_init(None), 0)
        self.assertEqual(DLL.lb_trace_push(None, 0, 1, 0, 0, 0, 0), 0)
        self.assertEqual(DLL.lb_trace_read(None, 0, c.byref(Batch())), 0)
        self.assertEqual(DLL.lb_trace_read(c.byref(self.ring), 0, None), 0)

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DiagnosticsTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    (ROOT / 'diagnostics-test-report.json').write_text(json.dumps({
        'test_type': 'Executed host C++ preflight, bounded buffer policy and trace ABI; no native IOKit execution',
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'successful': result.wasSuccessful(), 'macos_hardware_tests': 0,
    }, indent=2) + '\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
