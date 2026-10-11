# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Exercise the actual C++ DMA/PPGTT-to-GGTT CPU planner; no MMIO or GPU.

The existing Core validates individual mappings and DMA segments. These tests
check their missing composition with independent GGTT ownership and BAR bounds.
Build integration supplies lb_gpu_memory.cpp and lb_ggtt_plan.cpp in one library.
"""
from __future__ import annotations

import argparse
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import random
import unittest

ROOT = Path(__file__).resolve().parents[1]
PAGE = 4096
LOW = 8 * 1024 * 1024
TOP = 0xFEE00000
MAX = 256
OWNER = 42


class Segment(c.Structure):
    _fields_ = [('address', c.c_uint64), ('bytes', c.c_uint64)]


class Mapping(c.Structure):
    _fields_ = [(n, c.c_uint64) for n in ('token', 'gpuAddress', 'bytes')] + [
        (n, c.c_uint32) for n in ('owner', 'patIndex', 'writable', 'reserved')]


class Arena(c.Structure):
    _fields_ = [(n, c.c_uint64) for n in ('begin', 'end', 'nextToken', 'reservedBytes')] + [
        ('owner', c.c_uint32), ('count', c.c_uint32), ('reserved', c.c_uint32 * 2),
        ('mappings', Mapping * 32)]


class Window(c.Structure):
    _fields_ = [(n, c.c_uint64) for n in (
        'mappedBarBytes', 'gsmBarOffset', 'gsmBytes', 'reservedLowBytes', 'gpuAddressLimit')] + [
        (n, c.c_uint32) for n in ('abi', 'structBytes', 'addressSpaceKind', 'patIndex', 'patValue')] + [
        ('reserved', c.c_uint32 * 3)]


class Write(c.Structure):
    _fields_ = [('barOffset', c.c_uint64), ('pte', c.c_uint64)]


class Plan(c.Structure):
    _fields_ = [(n, c.c_uint64) for n in (
        'sourceToken', 'ggttToken', 'sourceGPUAddress', 'ggttGPUAddress', 'bytes',
        'firstPteIndex', 'barWriteBegin', 'barWriteEndExclusive')] + [
        (n, c.c_uint32) for n in ('abi', 'structBytes', 'owner', 'writeCount', 'patIndex',
            'expectedPatValue', 'requiredQualifications', 'hardwareBound', 'flushCompleted',
            'globalOwnershipVerified')] + [('reserved', c.c_uint32 * 2), ('writes', Write * MAX)]


class JoinInputs:
    def __init__(self, case, segments=None, *, source_pat=2, target_pat=2,
                 source_rw=1, target_rw=1, target_begin=LOW, target_end=TOP,
                 target_padding=0, source_begin=65536):
        self.case = case
        rows = segments or [(0x400000, 2 * PAGE), (0x180000, PAGE)]
        self.segments = (Segment * len(rows))(*(Segment(a, b) for a, b in rows))
        self.pages = [a + offset for a, b in rows for offset in range(0, b, PAGE)]
        self.source, self.target = Arena(), Arena()
        case.assertEqual(case.dll.lb_vm_init(c.byref(self.source), OWNER, source_begin,
                                           source_begin + 16 * 1024 * 1024), 0)
        case.assertEqual(case.dll.lb_vm_init(c.byref(self.target), OWNER, target_begin, target_end), 0)
        if target_padding:
            case.reserve(self.target, target_padding, target_pat, target_rw)
        total = sum(b for _, b in rows)
        self.source_map = case.reserve(self.source, total, source_pat, source_rw)
        self.target_map = case.reserve(self.target, total, target_pat, target_rw)
        self.staged = (c.c_uint64 * MAX)()
        case.assertEqual(case.dll.lb_vm_stage_ptes(
            c.byref(self.source), OWNER, self.source_map.token, self.segments,
            len(self.segments), self.staged, MAX), 0)
        self.window = Window(2 * LOW, LOW, LOW, LOW, TOP,
                             1, c.sizeof(Window), 1, 2, 0x0f)

    def snapshot(self):
        return tuple(bytes(value) for value in (
            self.source, self.target, self.segments, self.staged, self.window))

    def call(self, output_storage, **changes):
        arguments = dict(sourceArena=c.byref(self.source), sourceToken=self.source_map.token,
            ggttArena=c.byref(self.target), ggttToken=self.target_map.token, owner=OWNER,
            segments=self.segments, segmentCount=len(self.segments), stagedPpgtt=self.staged,
            stagedCount=len(self.pages), window=c.byref(self.window), out=c.byref(output_storage),
            outputBytes=c.sizeof(Plan))
        arguments.update(changes)
        self.case.__class__.planner_calls += 1
        return self.case.dll.lb_ggtt_build_plan(*arguments.values())


class GgttPlanTests(unittest.TestCase):
    library_path = None
    planner_calls = 0

    @classmethod
    def setUpClass(cls):
        if (c.sizeof(Segment), c.sizeof(Mapping), c.sizeof(Arena),
                c.sizeof(Window), c.sizeof(Write), c.sizeof(Plan)) != (16, 40, 1328, 72, 16, 4208):
            raise RuntimeError('Host structure layout differs from the fixed 64-bit C++ ABI.')
        supplied = cls.library_path or os.environ.get('LUNARBRIDGE_GGTT_TEST_LIBRARY')
        library = Path(supplied) if supplied else ROOT / 'build' / 'host' / 'lnl_host.dll'
        if not library.is_file():
            raise RuntimeError('Own host library is missing; build lb_gpu_memory.cpp and lb_ggtt_plan.cpp.')
        cls.library_sha256 = hashlib.sha256(library.read_bytes()).hexdigest()
        cls.dll = c.CDLL(str(library.resolve()))
        signatures = {
            'lb_vm_init': [c.POINTER(Arena), c.c_uint32, c.c_uint64, c.c_uint64],
            'lb_vm_reserve': [c.POINTER(Arena), c.c_uint32, c.c_uint64, c.c_uint32,
                              c.c_uint32, c.POINTER(Mapping)],
            'lb_vm_release': [c.POINTER(Arena), c.c_uint32, c.c_uint64],
            'lb_vm_stage_ptes': [c.POINTER(Arena), c.c_uint32, c.c_uint64,
                                c.POINTER(Segment), c.c_uint32, c.POINTER(c.c_uint64), c.c_uint32],
            'lb_ggtt_build_plan': [c.POINTER(Arena), c.c_uint64, c.POINTER(Arena), c.c_uint64,
                                  c.c_uint32, c.POINTER(Segment), c.c_uint32, c.POINTER(c.c_uint64),
                                  c.c_uint32, c.POINTER(Window), c.POINTER(Plan), c.c_uint32],
        }
        for name, arguments in signatures.items():
            getattr(cls.dll, name).argtypes = arguments
            getattr(cls.dll, name).restype = c.c_uint32

    def reserve(self, arena, size, pat=2, writable=1):
        mapping = Mapping()
        self.assertEqual(self.dll.lb_vm_reserve(c.byref(arena), OWNER, size, pat,
                                              writable, c.byref(mapping)), 0)
        return mapping

    def rejected(self, inputs, expected, **changes):
        out = Plan()
        c.memset(c.byref(out), 0xa5, c.sizeof(out))
        before, output_before = inputs.snapshot(), bytes(out)
        self.assertEqual(inputs.call(out, **changes), expected)
        self.assertEqual(bytes(out), output_before, 'A rejected plan changed its output.')
        self.assertEqual(inputs.snapshot(), before, 'The CPU planner changed its inputs.')

    def accepted(self, inputs):
        out = Plan()
        c.memset(c.byref(out), 0xa5, c.sizeof(out))
        before = inputs.snapshot()
        self.assertEqual(inputs.call(out), 0)
        self.assertEqual(inputs.snapshot(), before)
        self.assertEqual((out.abi, out.structBytes, out.owner, out.writeCount),
                         (1, 4208, OWNER, len(inputs.pages)))
        self.assertEqual((out.sourceToken, out.ggttToken, out.sourceGPUAddress, out.ggttGPUAddress),
                         (inputs.source_map.token, inputs.target_map.token,
                          inputs.source_map.gpuAddress, inputs.target_map.gpuAddress))
        self.assertEqual((out.bytes, out.firstPteIndex),
                         (len(inputs.pages) * PAGE, inputs.target_map.gpuAddress // PAGE))
        self.assertEqual((out.patIndex, out.expectedPatValue, out.requiredQualifications), (2, 15, 127))
        self.assertEqual((out.hardwareBound, out.flushCompleted, out.globalOwnershipVerified), (0, 0, 0))
        start = LOW + (inputs.target_map.gpuAddress // PAGE) * 8
        self.assertEqual((out.barWriteBegin, out.barWriteEndExclusive), (start, start + len(inputs.pages) * 8))
        for i, address in enumerate(inputs.pages):
            self.assertEqual((out.writes[i].barOffset, out.writes[i].pte),
                             (start + i * 8, address | 1 | (1 << 53)))
        self.assertEqual(tuple(out.reserved), (0, 0))
        self.assertTrue(all(row.barOffset == row.pte == 0 for row in out.writes[len(inputs.pages):]))
        return out

    def test_segmented_dma_pages_preserve_original_sequence(self):
        self.accepted(JoinInputs(self))

    def test_ppgtt_bits_are_reencoded_as_system_memory_ggtt(self):
        inputs = JoinInputs(self)
        out = self.accepted(inputs)
        for i, address in enumerate(inputs.pages):
            self.assertEqual(inputs.staged[i], address | 0x13)
            self.assertEqual(out.writes[i].pte & 0x13, 1)
            self.assertEqual(out.writes[i].pte & ((1 << 52) | (1 << 53)), 1 << 53)

    def test_target_token_and_va_come_from_separate_reservation(self):
        inputs = JoinInputs(self, target_padding=PAGE)
        out = self.accepted(inputs)
        self.assertEqual((out.sourceToken, out.ggttToken, out.ggttGPUAddress), (1, 2, LOW + PAGE))

    def test_identical_numeric_va_in_different_address_spaces_is_not_pointer_aliasing(self):
        inputs = JoinInputs(self, source_begin=LOW)
        out = self.accepted(inputs)
        self.assertEqual(out.sourceGPUAddress, out.ggttGPUAddress)
        self.assertNotEqual(c.addressof(inputs.source), c.addressof(inputs.target))

    def test_last_usable_ggtt_page_has_bounded_bar_offset(self):
        inputs = JoinInputs(self, [(0x800000, PAGE)], target_begin=TOP - PAGE)
        out = self.accepted(inputs)
        self.assertLessEqual(out.barWriteEndExclusive, 2 * LOW)
        self.assertEqual(out.ggttGPUAddress + out.bytes, TOP)

    def test_all_256_pages_fit_and_unused_write_slots_are_cleared(self):
        self.accepted(JoinInputs(self, [(0x400000, MAX * PAGE)]))
        self.accepted(JoinInputs(self, [(0x400000, PAGE)]))

    def test_fragmented_and_multpage_inputs_match_literal_reference(self):
        rng = random.Random(0x6420)
        for _ in range(40):
            counts = [rng.randint(1, 8) for _ in range(rng.randint(1, 20))]
            positions = list(range(len(counts)))
            rng.shuffle(positions)
            rows = [(0x100000 + p * 16 * PAGE, count * PAGE) for p, count in zip(positions, counts)]
            with self.subTest(pages=sum(counts), segments=len(rows)):
                self.accepted(JoinInputs(self, rows, target_padding=rng.randint(0, 4) * PAGE))

    def test_source_pte_changed_address_and_flags_are_rejected(self):
        for mask in (PAGE, 1, 2, 1 << 4, 1 << 53):
            inputs = JoinInputs(self)
            inputs.staged[0] ^= mask
            with self.subTest(mask=mask):
                self.rejected(inputs, 8)

    def test_source_pte_order_and_last_page_are_validated_before_commit(self):
        inputs = JoinInputs(self)
        inputs.staged[0], inputs.staged[1] = inputs.staged[1], inputs.staged[0]
        self.rejected(inputs, 8)
        inputs = JoinInputs(self)
        inputs.staged[len(inputs.pages) - 1] ^= PAGE
        self.rejected(inputs, 8)

    def test_staged_count_must_equal_reserved_source_pages(self):
        for count in (2, 4):
            with self.subTest(count=count):
                self.rejected(JoinInputs(self), 8, stagedCount=count)

    def test_owner_is_checked_for_each_arena(self):
        self.rejected(JoinInputs(self), 5, owner=OWNER + 1)
        inputs = JoinInputs(self)
        inputs.target.owner = OWNER + 1
        for i in range(inputs.target.count):
            inputs.target.mappings[i].owner = OWNER + 1
        self.rejected(inputs, 5)

    def test_unknown_source_and_target_tokens_are_rejected(self):
        self.rejected(JoinInputs(self), 6, sourceToken=1234567)
        self.rejected(JoinInputs(self), 6, ggttToken=1234567)

    def test_release_and_reuse_cannot_revive_stale_global_token(self):
        inputs = JoinInputs(self)
        old_token, old_va = inputs.target_map.token, inputs.target_map.gpuAddress
        self.assertEqual(self.dll.lb_vm_release(c.byref(inputs.target), OWNER, old_token), 0)
        new_mapping = self.reserve(inputs.target, inputs.target_map.bytes)
        self.assertEqual(new_mapping.gpuAddress, old_va)
        self.assertNotEqual(new_mapping.token, old_token)
        self.rejected(inputs, 6)
        inputs.target_map = new_mapping
        self.accepted(inputs)

    def test_core_corruption_is_propagated_without_output_change(self):
        inputs = JoinInputs(self)
        inputs.target.reservedBytes += PAGE
        self.rejected(inputs, 7)

    def test_source_and_global_reservation_sizes_must_match(self):
        inputs = JoinInputs(self)
        self.assertEqual(self.dll.lb_vm_release(c.byref(inputs.target), OWNER, inputs.target_map.token), 0)
        inputs.target_map = self.reserve(inputs.target, PAGE)
        self.rejected(inputs, 3)

    def test_pat0_staging_and_other_cache_policies_are_never_promoted(self):
        for location in ('source_pat', 'target_pat'):
            for pat in (0, 1, 3, 4):
                with self.subTest(location=location, pat=pat):
                    self.rejected(JoinInputs(self, **{location: pat}), 10)

    def test_read_only_source_or_target_is_never_widened_to_ggtt(self):
        self.rejected(JoinInputs(self, source_rw=0), 10)
        self.rejected(JoinInputs(self, target_rw=0), 10)

    def test_window_pat_index_and_programming_contract_must_be_exact(self):
        for field, value in (('patIndex', 0), ('patIndex', 3), ('patValue', 0x0c), ('patValue', 0x10)):
            inputs = JoinInputs(self)
            setattr(inputs.window, field, value)
            with self.subTest(field=field, value=value):
                self.rejected(inputs, 10)

    def test_bar_must_include_the_whole_gsm_window(self):
        for length in (0, LOW, 2 * LOW - 1):
            inputs = JoinInputs(self)
            inputs.window.mappedBarBytes = length
            with self.subTest(length=length):
                self.rejected(inputs, 9)

    def test_fixed_gsm_layout_and_guc_limit_cannot_be_caller_relocated(self):
        for field, value in (('gsmBarOffset', LOW + PAGE), ('gsmBytes', LOW - 8),
                ('gsmBarOffset', (1 << 64) - PAGE), ('gsmBytes', (1 << 64) - 1),
                ('gpuAddressLimit', 1 << 48)):
            inputs = JoinInputs(self)
            setattr(inputs.window, field, value)
            with self.subTest(field=field):
                self.rejected(inputs, 9)

    def test_window_abi_address_space_and_reserved_bits_are_checked(self):
        for field, value in (('abi', 0), ('abi', 2), ('structBytes', 71), ('addressSpaceKind', 0)):
            inputs = JoinInputs(self)
            setattr(inputs.window, field, value)
            self.rejected(inputs, 9)
        for i in range(3):
            inputs = JoinInputs(self)
            inputs.window.reserved[i] = 1
            self.rejected(inputs, 9)

    def test_whole_global_arena_must_respect_lower_and_upper_reserved_ranges(self):
        self.rejected(JoinInputs(self, target_begin=LOW - PAGE, target_padding=PAGE), 9)
        self.rejected(JoinInputs(self, target_end=TOP + PAGE), 9)

    def test_stricter_wopcm_reservation_cannot_be_ignored(self):
        inputs = JoinInputs(self)
        inputs.window.reservedLowBytes = LOW + PAGE
        self.rejected(inputs, 9)
        inputs = JoinInputs(self, target_begin=LOW + PAGE)
        inputs.window.reservedLowBytes = LOW + PAGE
        self.accepted(inputs)

    def test_lower_reservation_must_be_bounded_and_page_aligned(self):
        for value in (0, LOW - PAGE, LOW + 1, TOP, (1 << 64) - 1):
            inputs = JoinInputs(self)
            inputs.window.reservedLowBytes = value
            with self.subTest(value=value):
                self.rejected(inputs, 9)

    def test_invalid_last_dma_page_never_leaves_partial_plan(self):
        inputs = JoinInputs(self)
        inputs.segments[len(inputs.segments) - 1].address = 1 << 46
        self.rejected(inputs, 3)

    def test_capacity_counts_owner_and_tokens_are_bounded_before_use(self):
        changes = [('segmentCount', 0), ('segmentCount', 257), ('stagedCount', 0),
                   ('stagedCount', 257), ('outputBytes', 4207), ('outputBytes', 4209),
                   ('owner', 0), ('sourceToken', 0), ('ggttToken', 0)]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                self.rejected(JoinInputs(self), 1, **{key: value})

    def test_null_inputs_and_output_are_rejected(self):
        for key in ('sourceArena', 'ggttArena', 'segments', 'stagedPpgtt', 'window', 'out'):
            with self.subTest(key=key):
                self.rejected(JoinInputs(self), 1, **{key: None})

    def test_output_pointer_must_be_aligned_without_touching_buffer(self):
        inputs = JoinInputs(self)
        storage = (c.c_uint64 * 528)()
        c.memset(c.byref(storage), 0xa5, c.sizeof(storage))
        before = bytes(storage)
        pointer = c.cast(c.addressof(storage) + 1, c.POINTER(Plan))
        self.rejected(inputs, 2, out=pointer)
        self.assertEqual(bytes(storage), before)

    def test_output_cannot_alias_any_input_buffer(self):
        for key, pointer_type, value in (
            ('sourceArena', c.POINTER(Arena), 'source'),
            ('ggttArena', c.POINTER(Arena), 'target'),
            ('segments', c.POINTER(Segment), 'segments'),
            ('stagedPpgtt', c.POINTER(c.c_uint64), 'staged'),
            ('window', c.POINTER(Window), 'window')):
            inputs = JoinInputs(self)
            storage = (c.c_uint64 * (c.sizeof(Plan) // 8))()
            c.memmove(c.byref(storage), c.byref(getattr(inputs, value)), c.sizeof(getattr(inputs, value)))
            before = bytes(storage)
            pointer = c.cast(storage, pointer_type)
            out = c.cast(storage, c.POINTER(Plan))
            with self.subTest(key=key):
                self.rejected(inputs, 11, **{key: pointer, 'out': out})
                self.assertEqual(bytes(storage), before)

    def test_source_and_global_arena_objects_must_not_alias(self):
        inputs = JoinInputs(self)
        self.rejected(inputs, 11, ggttArena=c.byref(inputs.source))
        self.rejected(inputs, 11, ggttArena=c.cast(c.addressof(inputs.source) + 8, c.POINTER(Arena)))

    def test_overflowing_input_and_output_pointer_spans_are_rejected_before_dereference(self):
        maximum = (1 << (c.sizeof(c.c_void_p) * 8)) - 1
        for key, kind in (('sourceArena', Arena), ('ggttArena', Arena), ('segments', Segment),
                          ('stagedPpgtt', c.c_uint64), ('window', Window), ('out', Plan)):
            pointer = c.cast(maximum - 7, c.POINTER(kind))
            with self.subTest(key=key):
                self.rejected(JoinInputs(self), 3, **{key: pointer})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, help='Own host library containing the actual C++ planner.')
    parser.add_argument('--report', type=Path, help='Optional address-free synthetic verification JSON.')
    args = parser.parse_args()
    GgttPlanTests.library_path = args.library
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GgttPlanTests))
    report = {
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'successful': result.wasSuccessful(), 'synthetic': True,
        'actual_production_cpp_planner_executed': GgttPlanTests.planner_calls > 0,
        'planner_calls': GgttPlanTests.planner_calls,
        'native_bar_mapping_or_write_tested': False, 'native_ggtt_binding_tested': False,
        'pat_programming_verified': False, 'device_iommu_mapping_verified': False,
        'cpu_or_gpu_dispatch_tested': False, 'metal_acceleration_tested': False,
        'host_library_sha256': getattr(GgttPlanTests, 'library_sha256', None),
        'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            'gpu-prototype/lb_gpu_memory.cpp', 'gpu-prototype/lb_gpu_memory.h',
            'gpu-prototype/lb_ggtt_plan.cpp', 'gpu-prototype/lb_ggtt_plan.h', 'tests/test_ggtt_plan.py')},
    }
    serialized = json.dumps(report, indent=2) + '\n'
    if args.report:
        args.report.write_text(serialized, encoding='utf-8')
    print(serialized, end='')
    raise SystemExit(0 if result.wasSuccessful() else 1)
