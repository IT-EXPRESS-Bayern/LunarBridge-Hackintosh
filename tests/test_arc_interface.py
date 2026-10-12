# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Run the actual C++ Arc interface adapter; no MMIO, DMA, firmware or GPU.

These tests exercise the compiled production module, not a Python model of it.
An accepted request is a validated macOS call shape; it is never a submission.
"""
from __future__ import annotations

import argparse
import ctypes as c
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import lb_wire  # noqa: E402  user-space decoder of the same wire ABI
DLL = None
LIBRARY_SHA = None
LIBRARY_PATH = None

PAGE = 4096
OWNER = 23
ABI = 1
OPS = 11
MAX_SEGMENTS = 16
MAX_SYNCS = 4
REQUEST_BYTES = 480
DECISION_BYTES = 80
DESCRIPTOR_BYTES = 48
REPORT_BYTES = 48 + OPS * DESCRIPTOR_BYTES
VARIABLE_SIZE = 0xFFFFFFFF
SELECTOR_DESCRIBE = 7
SELECTOR_CHECK = 8
WB_PAT = 2
WB_PAT_VALUE = 0x0F
GSM_OFFSET = 8 * 1024 * 1024
GSM_BYTES = 8 * 1024 * 1024
GUC_TOP = 0xFEE00000
RESERVED_LOW = 8 * 1024 * 1024
MAX_TIMEOUT_NS = 2000000000

# Operations, in table order, with their pinned Linux ioctl offset.
(DEVICE_QUERY, RESOURCE_CREATE, RESOURCE_MAP, VM_CREATE, VM_DESTROY, VM_BIND,
 QUEUE_CREATE, QUEUE_DESTROY, QUEUE_PROPERTY, EXEC, FENCE_WAIT) = range(1, 12)
LINUX_NR = {DEVICE_QUERY: 0x40, RESOURCE_CREATE: 0x41, RESOURCE_MAP: 0x42,
            VM_CREATE: 0x43, VM_DESTROY: 0x44, VM_BIND: 0x45, QUEUE_CREATE: 0x46,
            QUEUE_DESTROY: 0x47, QUEUE_PROPERTY: 0x48, EXEC: 0x49, FENCE_WAIT: 0x4A}
LINUX_DIRECTION = {DEVICE_QUERY: 3, RESOURCE_CREATE: 3, RESOURCE_MAP: 3, VM_CREATE: 3,
                   VM_DESTROY: 1, VM_BIND: 1, QUEUE_CREATE: 3, QUEUE_DESTROY: 1,
                   QUEUE_PROPERTY: 3, EXEC: 1, FENCE_WAIT: 3}

# Results
OK, INPUT, ABI_MISMATCH, OWNER_RESULT, UNKNOWN_OP, CALL_SHAPE, LINUXISM, \
    UNSUPPORTED, RANGE, ALIGNMENT, CAPACITY, POLICY, ALIASING = range(13)
# Reasons
(R_NONE, R_RESERVED, R_USER_POINTER, R_SYNC_OBJECT, R_MMAP_OFFSET, R_HANDLE,
 R_VARIABLE_STRUCT, R_SCALAR_COUNT, R_ASYNC, R_PAT, R_ENGINE, R_SEGMENT_SUM,
 R_PAGE_ALIGNMENT, R_FENCE_ADDRESS, R_TIMEOUT, R_UNMAPPED, R_TOKEN, R_WINDOW,
 R_BATCH, R_CAPACITY, R_FLAGS, R_NO_COMPLETION) = range(22)

# Differences
D_FIXED_STRUCT, D_NO_EXTENSION, D_NO_USER_POINTER, D_FLATTENED, D_NO_SYNCOBJ, \
    D_NO_MMAP, D_OWNER_TOKEN, D_EXPLICIT_PAT, D_ASYNC_REF, D_BOUNDED_TIMEOUT, \
    D_NO_RENDER_NODE, D_ENTITLEMENT = (1 << bit for bit in range(12))
BASE_DIFFERENCES = (D_FIXED_STRUCT | D_NO_EXTENSION | D_OWNER_TOKEN |
                    D_NO_RENDER_NODE | D_ENTITLEMENT)
# Linux constructs
L_EXTENSION, L_USER_POINTER, L_VECTOR, L_SYNC_ARRAY, L_HANDLE, L_MMAP_OFFSET, \
    L_FENCE_ADDR, L_INFINITE_WAIT = (1 << bit for bit in range(8))
# Flags / modes
F_WRITABLE, F_SIGNAL, F_ABSOLUTE = 1, 2, 4
BIND_MAP, BIND_UNMAP, BIND_USERPTR, BIND_UNMAP_ALL, BIND_PREFETCH = range(5)
SYNC_OBJECT, SYNC_TIMELINE, SYNC_USER_FENCE = range(3)


class Segment(c.Structure):
    _fields_ = [('address', c.c_uint64), ('bytes', c.c_uint64)]


class SyncPoint(c.Structure):
    _fields_ = [('type', c.c_uint32), ('flags', c.c_uint32),
                ('address', c.c_uint64), ('value', c.c_uint64)]


class Descriptor(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in (
        'op', 'selector', 'scalarInputCount', 'scalarOutputCount', 'structInputBytes',
        'structOutputBytes', 'differences', 'requiredQualifications', 'linuxIoctlNr',
        'linuxConstructs', 'linuxDirection', 'reserved')]


class Report(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in (
        'abi', 'structBytes', 'opCount', 'differences', 'linuxUapiVersion',
        'darwinKernelMajor', 'scalarLimit', 'maxSegments', 'maxSyncs',
        'unmappedLinuxIoctls', 'gpuExecutionImplemented', 'metalProviderImplemented')] + [
        ('ops', Descriptor * OPS)]


class Request(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in (
        'abi', 'structBytes', 'op', 'owner', 'flags', 'patIndex', 'segmentCount',
        'syncCount', 'engineClass', 'engineSubmitMask', 'bindMode', 'queryKind',
        'memoryType')] + [('reservedWords', c.c_uint32 * 3)] + [
        (name, c.c_uint64) for name in (
            'vmToken', 'resourceToken', 'queueToken', 'gpuAddress', 'bytes',
            'lrcGpuAddress', 'batchAddress', 'timeoutNs')] + [
        ('segments', Segment * MAX_SEGMENTS), ('syncs', SyncPoint * MAX_SYNCS)]


class Decision(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in (
        'abi', 'structBytes', 'op', 'result', 'reason', 'differences',
        'requiredQualifications', 'acceptedSegments')] + [
        (name, c.c_uint64) for name in ('translatedBytes', 'translatedGpuAddress')] + [
        (name, c.c_uint32) for name in (
            'pageCount', 'ggttWriteCount', 'expectedPatValue', 'fenceSlots',
            'hardwareBound', 'gpuExecuted', 'metalVerified', 'reserved')]


class Window(c.Structure):
    _fields_ = [(name, c.c_uint64) for name in (
        'mappedBarBytes', 'gsmBarOffset', 'gsmBytes', 'reservedLowBytes', 'gpuAddressLimit')] + [
        (name, c.c_uint32) for name in (
            'abi', 'structBytes', 'addressSpaceKind', 'patIndex', 'patValue')] + [
        ('reserved', c.c_uint32 * 3)]


def bind_library(path):
    global DLL, LIBRARY_SHA, LIBRARY_PATH
    source = Path(path).resolve(strict=True)
    if not source.is_file():
        raise ValueError('Regular compiled library required')
    LIBRARY_PATH = source
    LIBRARY_SHA = hashlib.sha256(source.read_bytes()).hexdigest()
    DLL = c.CDLL(str(source))
    DLL.lb_arc_interface_describe.argtypes = [c.POINTER(Report), c.c_uint32]
    DLL.lb_arc_interface_describe.restype = c.c_uint32
    DLL.lb_arc_interface_descriptor.argtypes = [c.c_uint32, c.POINTER(Descriptor)]
    DLL.lb_arc_interface_descriptor.restype = c.c_uint32
    DLL.lb_arc_interface_check_call.argtypes = [c.c_uint32] * 6 + [c.POINTER(c.c_uint32)]
    DLL.lb_arc_interface_check_call.restype = c.c_uint32
    DLL.lb_arc_interface_evaluate.argtypes = [c.POINTER(Request), c.c_uint32,
                                              c.POINTER(Window), c.POINTER(Decision), c.c_uint32]
    DLL.lb_arc_interface_evaluate.restype = c.c_uint32
    DLL.lb_arc_interface_queue_registration.argtypes = [c.POINTER(Request), c.c_uint32,
                                                        c.c_void_p, c.c_uint32]
    DLL.lb_arc_interface_queue_registration.restype = c.c_uint32


def window(reserved_low=RESERVED_LOW, bar=64 * 1024 * 1024):
    return Window(bar, GSM_OFFSET, GSM_BYTES, reserved_low, GUC_TOP,
                  1, c.sizeof(Window), 1, WB_PAT, WB_PAT_VALUE, (c.c_uint32 * 3)())


def request(op, **fields):
    value = Request()
    value.abi = ABI
    value.structBytes = c.sizeof(Request)
    value.op = op
    value.owner = OWNER
    segments = fields.pop('segments', None)
    syncs = fields.pop('syncs', None)
    for name, field in fields.items():
        setattr(value, name, field)
    if segments is not None:
        value.segmentCount = len(segments)
        for index, (address, size) in enumerate(segments):
            value.segments[index] = Segment(address, size)
    if syncs is not None:
        value.syncCount = len(syncs)
        for index, (kind, flags, address, fence) in enumerate(syncs):
            value.syncs[index] = SyncPoint(kind, flags, address, fence)
    return value


def accepted_request(op):
    """A minimal request each operation accepts; used as the rejection base."""
    if op == DEVICE_QUERY:
        return request(op, queryKind=0)
    if op == RESOURCE_CREATE:
        return request(op, flags=F_WRITABLE, patIndex=WB_PAT, bytes=2 * PAGE)
    if op == RESOURCE_MAP:
        return request(op, resourceToken=7, memoryType=0)
    if op == VM_CREATE:
        return request(op, gpuAddress=1 << 20, bytes=4 * PAGE)
    if op == VM_DESTROY:
        return request(op, vmToken=5)
    if op == VM_BIND:
        return request(op, flags=F_WRITABLE, patIndex=WB_PAT, bindMode=BIND_MAP,
                       vmToken=5, resourceToken=7, gpuAddress=RESERVED_LOW + PAGE,
                       bytes=2 * PAGE, segments=[(0x400000, 2 * PAGE)],
                       syncs=[(SYNC_USER_FENCE, F_SIGNAL, 0x500000, 1)])
    if op == QUEUE_CREATE:
        return request(op, engineClass=0, engineSubmitMask=1, vmToken=5,
                       lrcGpuAddress=RESERVED_LOW + 2 * PAGE)
    if op == QUEUE_DESTROY:
        return request(op, queueToken=11)
    if op == QUEUE_PROPERTY:
        return request(op, queueToken=11, queryKind=0)
    if op == EXEC:
        return request(op, queueToken=11, batchAddress=0x200040,
                       syncs=[(SYNC_USER_FENCE, F_SIGNAL, 0x500000, 2)])
    if op == FENCE_WAIT:
        return request(op, queueToken=11, timeoutNs=1000000,
                       syncs=[(SYNC_USER_FENCE, 0, 0x500000, 2)])
    raise ValueError('Unknown operation')


class ArcInterfaceCase(unittest.TestCase):
    def evaluate(self, value, *, owner=OWNER, win=None, output_bytes=DECISION_BYTES):
        decision = Decision()
        status = DLL.lb_arc_interface_evaluate(c.byref(value), owner,
                                               c.byref(win) if win else None,
                                               c.byref(decision), output_bytes)
        return status, decision

    def expect(self, value, result, reason, **kwargs):
        status, decision = self.evaluate(value, **kwargs)
        self.assertEqual((status, decision.result, decision.reason), (result, result, reason))
        # A refusal never reports translated values or hardware progress.
        self.assertEqual((decision.translatedBytes, decision.translatedGpuAddress,
                          decision.pageCount, decision.ggttWriteCount, decision.fenceSlots,
                          decision.acceptedSegments, decision.hardwareBound,
                          decision.gpuExecuted, decision.metalVerified),
                         (0, 0, 0, 0, 0, 0, 0, 0, 0))
        return decision

    def accept(self, value, **kwargs):
        status, decision = self.evaluate(value, **kwargs)
        self.assertEqual((status, decision.result, decision.reason), (OK, OK, R_NONE))
        self.assertEqual((decision.abi, decision.structBytes, decision.op),
                         (ABI, DECISION_BYTES, value.op))
        # No accepted shape claims hardware binding, execution or Metal.
        self.assertEqual((decision.hardwareBound, decision.gpuExecuted,
                          decision.metalVerified, decision.reserved), (0, 0, 0, 0))
        return decision


class InterfaceTable(ArcInterfaceCase):
    def setUp(self):
        self.report = Report()
        self.assertEqual(DLL.lb_arc_interface_describe(c.byref(self.report), REPORT_BYTES), OK)

    def test_wire_sizes_match_the_compiled_module(self):
        self.assertEqual((c.sizeof(Request), c.sizeof(Decision), c.sizeof(Descriptor),
                          c.sizeof(Report)),
                         (REQUEST_BYTES, DECISION_BYTES, DESCRIPTOR_BYTES, REPORT_BYTES))
        self.assertEqual(self.report.structBytes, REPORT_BYTES)

    def test_report_states_pins_and_missing_capabilities(self):
        self.assertEqual((self.report.abi, self.report.opCount), (ABI, OPS))
        self.assertEqual(self.report.linuxUapiVersion, (6 << 16) | 17)
        self.assertEqual(self.report.darwinKernelMajor, 26)
        self.assertEqual((self.report.scalarLimit, self.report.maxSegments,
                          self.report.maxSyncs), (16, MAX_SEGMENTS, MAX_SYNCS))
        self.assertEqual(self.report.unmappedLinuxIoctls, 1)
        self.assertEqual((self.report.gpuExecutionImplemented,
                          self.report.metalProviderImplemented), (0, 0))

    def test_every_operation_maps_one_pinned_ioctl(self):
        self.assertEqual([row.op for row in self.report.ops], list(range(1, OPS + 1)))
        for row in self.report.ops:
            self.assertEqual(row.linuxIoctlNr, LINUX_NR[row.op])
            self.assertEqual(row.linuxDirection, LINUX_DIRECTION[row.op])
            self.assertEqual(row.selector, SELECTOR_CHECK)
            self.assertEqual((row.structInputBytes, row.structOutputBytes),
                             (REQUEST_BYTES, DECISION_BYTES))
            self.assertEqual((row.scalarInputCount, row.scalarOutputCount, row.reserved),
                             (0, 0, 0))
            # Every operation carries the differences the macOS call shape
            # itself forces, and every one but the device query addresses a
            # per-file id namespace that becomes an owner-bound token here.
            self.assertEqual(row.differences & BASE_DIFFERENCES, BASE_DIFFERENCES)
            self.assertEqual(bool(row.linuxConstructs & L_HANDLE), row.op != DEVICE_QUERY)

    def test_extension_chains_are_claimed_only_where_they_exist(self):
        rows = {row.op: row for row in self.report.ops}
        # The pinned drm_xe_vm_destroy and drm_xe_exec_queue_destroy carry no
        # extension chain; claiming one for them would overstate the diff.
        for op in (VM_DESTROY, QUEUE_DESTROY):
            self.assertFalse(rows[op].linuxConstructs & L_EXTENSION)
        for op in set(range(1, OPS + 1)) - {VM_DESTROY, QUEUE_DESTROY}:
            self.assertTrue(rows[op].linuxConstructs & L_EXTENSION)

    def test_construct_driven_differences_hold_in_both_directions(self):
        # A difference answering one construct must appear exactly where that
        # construct does. tools/compare_interfaces.py checks the same pairs
        # against the pinned header; this keeps the table honest offline.
        pairs = ((D_NO_USER_POINTER, L_USER_POINTER), (D_FLATTENED, L_VECTOR),
                 (D_NO_SYNCOBJ, L_SYNC_ARRAY), (D_NO_MMAP, L_MMAP_OFFSET),
                 (D_BOUNDED_TIMEOUT, L_INFINITE_WAIT))
        for row in self.report.ops:
            for difference, construct in pairs:
                self.assertEqual(bool(row.differences & difference),
                                 bool(row.linuxConstructs & construct),
                                 msg=f'operation {row.op}, difference {difference:#x}')

    def test_specific_construct_and_difference_pairs(self):
        rows = {row.op: row for row in self.report.ops}
        bind = rows[VM_BIND]
        self.assertEqual(bind.linuxConstructs,
                         L_EXTENSION | L_USER_POINTER | L_VECTOR | L_SYNC_ARRAY | L_HANDLE)
        self.assertEqual(bind.differences & (D_NO_USER_POINTER | D_FLATTENED |
                                             D_NO_SYNCOBJ | D_EXPLICIT_PAT),
                         D_NO_USER_POINTER | D_FLATTENED | D_NO_SYNCOBJ | D_EXPLICIT_PAT)
        self.assertTrue(rows[RESOURCE_MAP].linuxConstructs & L_MMAP_OFFSET)
        self.assertTrue(rows[RESOURCE_MAP].differences & D_NO_MMAP)
        self.assertTrue(rows[FENCE_WAIT].linuxConstructs & L_INFINITE_WAIT)
        self.assertTrue(rows[FENCE_WAIT].differences & D_BOUNDED_TIMEOUT)
        self.assertTrue(rows[EXEC].differences & D_ASYNC_REF)
        self.assertEqual(self.report.differences & D_FLATTENED, D_FLATTENED)

    def test_describe_rejects_wrong_output_contract(self):
        report = Report()
        self.assertEqual(DLL.lb_arc_interface_describe(c.byref(report), REPORT_BYTES - 4), INPUT)
        self.assertEqual(DLL.lb_arc_interface_describe(None, REPORT_BYTES), INPUT)
        self.assertEqual(report.abi, 0)
        storage = (c.c_uint64 * 128)()
        misaligned = c.cast(c.byref(storage, 4), c.POINTER(Report))
        self.assertEqual(DLL.lb_arc_interface_describe(misaligned, REPORT_BYTES), ALIGNMENT)

    def test_descriptor_lookup_refuses_unknown_operations(self):
        descriptor = Descriptor()
        for op in (0, OPS + 1, 0xFFFFFFFF):
            self.assertEqual(DLL.lb_arc_interface_descriptor(op, c.byref(descriptor)), UNKNOWN_OP)
            self.assertEqual(descriptor.op, 0)
        self.assertEqual(DLL.lb_arc_interface_descriptor(VM_BIND, c.byref(descriptor)), OK)
        self.assertEqual(descriptor.linuxIoctlNr, LINUX_NR[VM_BIND])


class CallShape(ArcInterfaceCase):
    def check(self, selector, scalar_in, scalar_out, struct_in, struct_out, async_count=0):
        reason = c.c_uint32(0xDEAD)
        status = DLL.lb_arc_interface_check_call(selector, scalar_in, scalar_out,
                                                 struct_in, struct_out, async_count,
                                                 c.byref(reason))
        return status, reason.value

    def test_both_interface_selectors_have_one_fixed_shape(self):
        self.assertEqual(self.check(SELECTOR_DESCRIBE, 0, 0, 0, REPORT_BYTES), (OK, R_NONE))
        self.assertEqual(self.check(SELECTOR_CHECK, 0, 0, REQUEST_BYTES, DECISION_BYTES),
                         (OK, R_NONE))

    def test_variable_and_wrong_structure_sizes_are_refused(self):
        self.assertEqual(self.check(SELECTOR_CHECK, 0, 0, VARIABLE_SIZE, DECISION_BYTES),
                         (CALL_SHAPE, R_VARIABLE_STRUCT))
        self.assertEqual(self.check(SELECTOR_CHECK, 0, 0, REQUEST_BYTES, VARIABLE_SIZE),
                         (CALL_SHAPE, R_VARIABLE_STRUCT))
        self.assertEqual(self.check(SELECTOR_CHECK, 0, 0, REQUEST_BYTES - 8, DECISION_BYTES),
                         (CALL_SHAPE, R_VARIABLE_STRUCT))
        self.assertEqual(self.check(SELECTOR_DESCRIBE, 0, 0, REQUEST_BYTES, REPORT_BYTES),
                         (CALL_SHAPE, R_VARIABLE_STRUCT))

    def test_scalars_and_async_references_are_refused(self):
        self.assertEqual(self.check(SELECTOR_CHECK, 1, 0, REQUEST_BYTES, DECISION_BYTES),
                         (CALL_SHAPE, R_SCALAR_COUNT))
        self.assertEqual(self.check(SELECTOR_CHECK, 0, 17, REQUEST_BYTES, DECISION_BYTES),
                         (CALL_SHAPE, R_SCALAR_COUNT))
        self.assertEqual(self.check(SELECTOR_CHECK, 0, 0, REQUEST_BYTES, DECISION_BYTES, 3),
                         (UNSUPPORTED, R_ASYNC))

    def test_other_selectors_belong_to_the_older_abi(self):
        for selector in (0, 4, 6, 9, 0xFFFF):
            self.assertEqual(self.check(selector, 0, 0, 0, 0), (UNKNOWN_OP, R_UNMAPPED))

    def test_reason_pointer_is_optional(self):
        status = DLL.lb_arc_interface_check_call(SELECTOR_DESCRIBE, 0, 0, 0, REPORT_BYTES, 0, None)
        self.assertEqual(status, OK)


class RequestEnvelope(ArcInterfaceCase):
    def test_every_operation_has_an_accepted_shape(self):
        for op in range(1, OPS + 1):
            with self.subTest(op=op):
                self.accept(accepted_request(op))

    def test_abi_and_struct_bytes_are_checked(self):
        value = accepted_request(VM_DESTROY)
        value.abi = 2
        self.expect(value, ABI_MISMATCH, R_VARIABLE_STRUCT)
        value = accepted_request(VM_DESTROY)
        value.structBytes = REQUEST_BYTES - 8
        self.expect(value, ABI_MISMATCH, R_VARIABLE_STRUCT)

    def test_requests_are_bound_to_the_calling_owner(self):
        value = accepted_request(VM_DESTROY)
        value.owner = OWNER + 1
        self.expect(value, OWNER_RESULT, R_HANDLE)
        self.expect(accepted_request(VM_DESTROY), OWNER_RESULT, R_HANDLE, owner=0)

    def test_unknown_operations_are_never_defaulted(self):
        for op in (0, OPS + 1, 0x4000):
            value = accepted_request(VM_DESTROY)
            value.op = op
            self.expect(value, UNKNOWN_OP, R_UNMAPPED)

    def test_fields_outside_the_operation_must_be_zero(self):
        cases = {'vmToken': 1, 'batchAddress': 64, 'timeoutNs': 10, 'engineSubmitMask': 1,
                 'memoryType': 1, 'lrcGpuAddress': PAGE, 'queueToken': 3}
        for name, field in cases.items():
            with self.subTest(field=name):
                value = accepted_request(DEVICE_QUERY)
                setattr(value, name, field)
                self.expect(value, INPUT, R_RESERVED)
        value = accepted_request(DEVICE_QUERY)
        value.reservedWords[1] = 1
        self.expect(value, INPUT, R_RESERVED)

    def test_array_entries_beyond_the_declared_count_must_be_zero(self):
        value = accepted_request(VM_BIND)
        value.segments[value.segmentCount] = Segment(0x800000, PAGE)
        self.expect(value, INPUT, R_RESERVED)
        value = accepted_request(EXEC)
        value.syncs[value.syncCount] = SyncPoint(SYNC_USER_FENCE, F_SIGNAL, 0x600000, 1)
        self.expect(value, INPUT, R_RESERVED)

    def test_counts_and_flags_stay_inside_the_declared_bounds(self):
        value = accepted_request(VM_BIND)
        value.segmentCount = MAX_SEGMENTS + 1
        self.expect(value, CAPACITY, R_CAPACITY)
        value = accepted_request(EXEC)
        value.syncCount = MAX_SYNCS + 1
        self.expect(value, CAPACITY, R_CAPACITY)
        value = accepted_request(VM_BIND)
        value.flags |= 1 << 20
        self.expect(value, INPUT, R_FLAGS)

    def test_output_buffer_contract(self):
        value = accepted_request(VM_DESTROY)
        decision = Decision()
        self.assertEqual(DLL.lb_arc_interface_evaluate(c.byref(value), OWNER, None,
                                                       c.byref(decision), DECISION_BYTES - 4), INPUT)
        self.assertEqual(decision.abi, 0)
        self.assertEqual(DLL.lb_arc_interface_evaluate(c.byref(value), OWNER, None,
                                                       None, DECISION_BYTES), INPUT)
        self.assertEqual(DLL.lb_arc_interface_evaluate(None, OWNER, None,
                                                       c.byref(decision), DECISION_BYTES), INPUT)
        storage = (c.c_uint64 * 256)()
        misaligned = c.cast(c.byref(storage, 4), c.POINTER(Decision))
        self.assertEqual(DLL.lb_arc_interface_evaluate(c.byref(value), OWNER, None,
                                                       misaligned, DECISION_BYTES), ALIGNMENT)

    def test_overlapping_input_and_output_are_refused(self):
        storage = (c.c_uint64 * 256)()
        overlapping_request = c.cast(c.byref(storage, 0), c.POINTER(Request))
        overlapping_request[0] = accepted_request(VM_DESTROY)
        decision = c.cast(c.byref(storage, 8), c.POINTER(Decision))
        self.assertEqual(DLL.lb_arc_interface_evaluate(overlapping_request, OWNER, None,
                                                       decision, DECISION_BYTES), ALIASING)


class LinuxConstructsRefused(ArcInterfaceCase):
    def test_user_pointer_binds_have_no_mac_os_counterpart(self):
        value = accepted_request(VM_BIND)
        value.bindMode = BIND_USERPTR
        self.expect(value, LINUXISM, R_USER_POINTER)

    def test_unmap_all_and_prefetch_stay_unmapped(self):
        for mode in (BIND_UNMAP_ALL, BIND_PREFETCH):
            value = accepted_request(VM_BIND)
            value.bindMode = mode
            self.expect(value, UNSUPPORTED, R_UNMAPPED)
        value = accepted_request(VM_BIND)
        value.bindMode = 7
        self.expect(value, INPUT, R_FLAGS)

    def test_sync_objects_are_refused_everywhere(self):
        for op in (VM_BIND, EXEC, FENCE_WAIT):
            for kind in (SYNC_OBJECT, SYNC_TIMELINE):
                with self.subTest(op=op, kind=kind):
                    value = accepted_request(op)
                    value.syncs[0].type = kind
                    self.expect(value, LINUXISM, R_SYNC_OBJECT)
        value = accepted_request(EXEC)
        value.syncs[0].type = 9
        self.expect(value, INPUT, R_UNMAPPED)

    def test_fake_mmap_offsets_become_one_memory_type(self):
        value = accepted_request(RESOURCE_MAP)
        value.memoryType = 1
        self.expect(value, UNSUPPORTED, R_MMAP_OFFSET)
        value = accepted_request(RESOURCE_MAP)
        value.resourceToken = 0
        self.expect(value, INPUT, R_TOKEN)

    def test_unbounded_waits_are_refused(self):
        for timeout in (0, MAX_TIMEOUT_NS + 1, 0xFFFFFFFFFFFFFFFF):
            value = accepted_request(FENCE_WAIT)
            value.timeoutNs = timeout
            self.expect(value, POLICY, R_TIMEOUT)
        value = accepted_request(FENCE_WAIT)
        value.timeoutNs = MAX_TIMEOUT_NS
        self.accept(value)


class MemoryOperations(ArcInterfaceCase):
    def test_resource_create_enforces_page_size_budget_and_pat(self):
        self.assertEqual(self.accept(accepted_request(RESOURCE_CREATE)).pageCount, 2)
        self.assertEqual(self.accept(accepted_request(RESOURCE_CREATE)).expectedPatValue,
                         WB_PAT_VALUE)
        for size, result, reason in ((0, ALIGNMENT, R_PAGE_ALIGNMENT),
                                     (PAGE + 1, ALIGNMENT, R_PAGE_ALIGNMENT),
                                     (2 * 1024 * 1024, CAPACITY, R_CAPACITY)):
            value = accepted_request(RESOURCE_CREATE)
            value.bytes = size
            self.expect(value, result, reason)
        for pat in (0, 1, 3):
            value = accepted_request(RESOURCE_CREATE)
            value.patIndex = pat
            self.expect(value, POLICY, R_PAT)
        value = accepted_request(RESOURCE_CREATE)
        value.flags = F_SIGNAL
        self.expect(value, POLICY, R_FLAGS)

    def test_vm_create_reports_its_translated_span(self):
        decision = self.accept(accepted_request(VM_CREATE))
        self.assertEqual((decision.translatedGpuAddress, decision.translatedBytes,
                          decision.pageCount), (1 << 20, 4 * PAGE, 4))
        value = accepted_request(VM_CREATE)
        value.gpuAddress = (1 << 48) - PAGE
        self.expect(value, RANGE, R_PAGE_ALIGNMENT)
        value = accepted_request(VM_CREATE)
        value.gpuAddress = (1 << 20) + 1
        self.expect(value, ALIGNMENT, R_PAGE_ALIGNMENT)

    def test_bind_requires_segments_that_sum_to_the_mapping(self):
        decision = self.accept(accepted_request(VM_BIND))
        self.assertEqual((decision.acceptedSegments, decision.pageCount,
                          decision.fenceSlots, decision.ggttWriteCount), (1, 2, 1, 0))
        value = accepted_request(VM_BIND)
        value.segments[0] = Segment(0x400000, PAGE)
        self.expect(value, INPUT, R_SEGMENT_SUM)
        value = accepted_request(VM_BIND)
        value.segments[0] = Segment(0x400000 + 8, 2 * PAGE)
        self.expect(value, ALIGNMENT, R_PAGE_ALIGNMENT)
        value = accepted_request(VM_BIND)
        value.segments[0] = Segment((1 << 46) - PAGE, 2 * PAGE)
        self.expect(value, RANGE, R_SEGMENT_SUM)
        value = accepted_request(VM_BIND)
        value.segmentCount = 0
        value.segments[0] = Segment(0, 0)
        self.expect(value, INPUT, R_SEGMENT_SUM)

    def test_unmap_carries_no_addresses_and_no_write_policy(self):
        value = request(VM_BIND, bindMode=BIND_UNMAP, patIndex=WB_PAT, vmToken=5,
                        resourceToken=7, gpuAddress=RESERVED_LOW + PAGE, bytes=2 * PAGE)
        self.accept(value)
        value.flags = F_WRITABLE
        self.expect(value, POLICY, R_FLAGS)
        value = request(VM_BIND, bindMode=BIND_UNMAP, patIndex=WB_PAT, vmToken=5,
                        resourceToken=7, gpuAddress=RESERVED_LOW + PAGE, bytes=2 * PAGE,
                        segments=[(0x400000, 2 * PAGE)])
        self.expect(value, INPUT, R_RESERVED)

    def test_read_only_binds_cannot_be_honoured(self):
        value = accepted_request(VM_BIND)
        value.flags = F_SIGNAL
        self.expect(value, POLICY, R_FLAGS)

    def test_global_window_pre_check_counts_the_entry_writes(self):
        decision = self.accept(accepted_request(VM_BIND), win=window())
        self.assertEqual((decision.ggttWriteCount, decision.expectedPatValue),
                         (2, WB_PAT_VALUE))
        value = accepted_request(VM_BIND)
        value.gpuAddress = PAGE
        self.expect(value, RANGE, R_WINDOW, win=window())
        broken = window()
        broken.patValue = 0
        self.expect(accepted_request(VM_BIND), POLICY, R_PAT, win=broken)
        broken = window()
        broken.gsmBytes = GSM_BYTES // 2
        self.expect(accepted_request(VM_BIND), POLICY, R_WINDOW, win=broken)
        broken = window()
        broken.addressSpaceKind = 0
        self.expect(accepted_request(VM_BIND), POLICY, R_WINDOW, win=broken)

    def test_bind_outside_the_window_is_still_checked_without_one(self):
        # Without a window the bind is validated, but no entry writes exist.
        decision = self.accept(accepted_request(VM_BIND))
        self.assertEqual(decision.ggttWriteCount, 0)


class QueueAndExecution(ArcInterfaceCase):
    def test_queue_create_accepts_only_guc_render_and_compute(self):
        for engine in (0, 4):
            value = accepted_request(QUEUE_CREATE)
            value.engineClass = engine
            self.accept(value)
        for engine in (1, 2, 3, 5):
            value = accepted_request(QUEUE_CREATE)
            value.engineClass = engine
            self.expect(value, UNSUPPORTED, R_ENGINE)

    def test_queue_create_checks_mask_vm_and_lrc(self):
        for mask in (0, 0x100):
            value = accepted_request(QUEUE_CREATE)
            value.engineSubmitMask = mask
            self.expect(value, RANGE, R_ENGINE)
        value = accepted_request(QUEUE_CREATE)
        value.vmToken = 0
        self.expect(value, INPUT, R_TOKEN)
        for lrc in (0, PAGE + 8, GUC_TOP):
            value = accepted_request(QUEUE_CREATE)
            value.lrcGpuAddress = lrc
            self.expect(value, RANGE, R_PAGE_ALIGNMENT)
        # A context page below the reserved low window is refused by policy.
        value = accepted_request(QUEUE_CREATE)
        value.lrcGpuAddress = PAGE * 2
        self.expect(value, POLICY, R_WINDOW)

    def test_exec_needs_an_aligned_batch_and_a_signalling_fence(self):
        decision = self.accept(accepted_request(EXEC))
        self.assertEqual((decision.translatedGpuAddress, decision.fenceSlots), (0x200040, 1))
        value = accepted_request(EXEC)
        value.batchAddress = 0x200048
        self.expect(value, ALIGNMENT, R_BATCH)
        value = accepted_request(EXEC)
        value.batchAddress = 0
        self.expect(value, RANGE, R_BATCH)
        value = accepted_request(EXEC)
        value.syncCount = 0
        value.syncs[0] = SyncPoint(0, 0, 0, 0)
        self.expect(value, INPUT, R_NO_COMPLETION)
        value = accepted_request(EXEC)
        value.syncs[0].flags = 0
        self.expect(value, POLICY, R_NO_COMPLETION)

    def test_fence_wait_watches_exactly_one_user_fence(self):
        decision = self.accept(accepted_request(FENCE_WAIT))
        self.assertEqual((decision.translatedGpuAddress, decision.fenceSlots), (0x500000, 1))
        value = accepted_request(FENCE_WAIT)
        value.syncs[0].flags = F_SIGNAL
        self.expect(value, POLICY, R_FLAGS)
        value = accepted_request(FENCE_WAIT)
        value.syncs[1] = SyncPoint(SYNC_USER_FENCE, 0, 0x600000, 1)
        value.syncCount = 2
        self.expect(value, INPUT, R_NO_COMPLETION)
        for address in (0, 0x500004):
            value = accepted_request(FENCE_WAIT)
            value.syncs[0].address = address
            self.expect(value, RANGE, R_FENCE_ADDRESS)
        value = accepted_request(FENCE_WAIT)
        value.flags = F_ABSOLUTE
        self.accept(value)
        value = accepted_request(FENCE_WAIT)
        value.flags = F_WRITABLE
        self.expect(value, POLICY, R_FLAGS)

    def test_queue_property_answers_one_bounded_record(self):
        self.assertEqual(self.accept(accepted_request(QUEUE_PROPERTY)).translatedBytes, 1)
        value = accepted_request(QUEUE_PROPERTY)
        value.queryKind = 1
        self.expect(value, UNSUPPORTED, R_UNMAPPED)

    def test_device_query_answers_only_known_kinds(self):
        self.assertEqual(self.accept(accepted_request(DEVICE_QUERY)).translatedBytes, 1)
        for kind in (1, 2):
            value = accepted_request(DEVICE_QUERY)
            value.queryKind = kind
            self.assertEqual(self.accept(value).translatedBytes, 2)
        value = accepted_request(DEVICE_QUERY)
        value.queryKind = 3
        self.expect(value, UNSUPPORTED, R_UNMAPPED)


class GuCRegistrationBridge(ArcInterfaceCase):
    def registration(self, value, owner=OWNER, output_bytes=24):
        storage = (c.c_uint64 * 8)()
        status = DLL.lb_arc_interface_queue_registration(c.byref(value), owner,
                                                         c.cast(storage, c.c_void_p), output_bytes)
        fields = c.cast(storage, c.POINTER(c.c_uint64))[0], \
            c.cast(storage, c.POINTER(c.c_uint32))[2], c.cast(storage, c.POINTER(c.c_uint32))[3]
        return status, fields

    def test_accepted_queue_create_becomes_a_guc_registration(self):
        value = accepted_request(QUEUE_CREATE)
        value.engineClass = 4
        value.engineSubmitMask = 3
        status, (lrc, engine_class, mask) = self.registration(value)
        self.assertEqual(status, OK)
        # The registration carries the composed descriptor, not the raw
        # address: valid | legacy 64-bit addressing mode | privilege.
        self.assertEqual((lrc, engine_class, mask),
                         (RESERVED_LOW + 2 * PAGE + 0x119, 4, 3))

    def test_registration_refuses_other_operations_and_bad_requests(self):
        status, fields = self.registration(accepted_request(EXEC))
        self.assertEqual((status, fields), (UNKNOWN_OP, (0, 0, 0)))
        value = accepted_request(QUEUE_CREATE)
        value.engineSubmitMask = 0
        status, fields = self.registration(value)
        self.assertEqual((status, fields), (RANGE, (0, 0, 0)))
        value = accepted_request(QUEUE_CREATE)
        status, fields = self.registration(value, owner=OWNER + 1)
        self.assertEqual((status, fields), (OWNER_RESULT, (0, 0, 0)))
        status, fields = self.registration(accepted_request(QUEUE_CREATE), output_bytes=20)
        self.assertEqual((status, fields), (INPUT, (0, 0, 0)))


class UserSpaceDecoder(ArcInterfaceCase):
    """The kernel module and tools/lb_wire.py must agree byte for byte."""

    def test_interface_report_decodes_in_user_space(self):
        report = Report()
        self.assertEqual(DLL.lb_arc_interface_describe(c.byref(report), REPORT_BYTES), OK)
        decoded = lb_wire.decode_arc_interface(bytes(report))
        self.assertEqual(decoded['operation_count'], OPS)
        self.assertEqual(decoded['linux_uapi_version'], '6.17')
        self.assertEqual(decoded['darwin_kernel_major'], 26)
        self.assertEqual(decoded['unmapped_linux_ioctls'], 1)
        self.assertFalse(decoded['gpu_execution_implemented'])
        self.assertEqual(decoded['operations'][VM_BIND - 1]['operation'], 'vm_bind')
        self.assertIn('flattened_vector', decoded['operations'][VM_BIND - 1]['differences'])
        self.assertIn('ggtt_ownership',
                      decoded['operations'][VM_BIND - 1]['missing_qualifications'])
        self.assertEqual(decoded['operations'][VM_DESTROY - 1]['linux_direction'], 'write')

    def test_accepted_and_refused_verdicts_decode_in_user_space(self):
        decision = self.accept(accepted_request(VM_BIND), win=window())
        decoded = lb_wire.decode_arc_decision(bytes(decision))
        self.assertEqual((decoded['operation'], decoded['result'], decoded['reason']),
                         ('vm_bind', 'accepted', 'none'))
        self.assertEqual((decoded['page_count'], decoded['ggtt_write_count']), (2, 2))
        self.assertFalse(decoded['gpu_executed'])
        value = accepted_request(VM_BIND)
        value.bindMode = BIND_USERPTR
        refused = lb_wire.decode_arc_decision(bytes(self.expect(value, LINUXISM, R_USER_POINTER)))
        self.assertEqual((refused['result'], refused['reason']),
                         ('linux_only_construct', 'user_pointer'))

    def test_decoder_refuses_a_forged_verdict(self):
        decision = self.accept(accepted_request(VM_DESTROY))
        decision.gpuExecuted = 1
        with self.assertRaises(ValueError):
            lb_wire.decode_arc_decision(bytes(decision))
        decision = self.accept(accepted_request(VM_DESTROY))
        decision.result = LINUXISM
        decision.translatedBytes = 4096
        with self.assertRaises(ValueError):
            lb_wire.decode_arc_decision(bytes(decision))

    def test_trace_events_of_the_new_selectors_have_names(self):
        self.assertEqual(lb_wire.EVENTS[60], 'arc_interface_queried')
        self.assertEqual(lb_wire.EVENTS[61], 'arc_request_accepted')
        self.assertEqual(lb_wire.EVENTS[62], 'arc_request_rejected')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('library', type=Path, help='Compiled library with the Arc interface')
    parser.add_argument('--report', type=Path, help='Write a JSON evidence record')
    parser.add_argument('unittest_args', nargs='*')
    arguments = parser.parse_args()
    bind_library(arguments.library)
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__('__main__'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if arguments.report:
        arguments.report.write_text(json.dumps({
            'library': str(LIBRARY_PATH), 'library_sha256': LIBRARY_SHA,
            'tests_run': result.testsRun, 'failures': len(result.failures),
            'errors': len(result.errors),
            'scope': 'CPU interface contract only; no MMIO, DMA, firmware, GPU or Metal',
            'gpu_execution_implemented': False, 'metal_implemented': False,
        }, indent=2) + '\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
