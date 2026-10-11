# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Run the actual portable C++ GuC scheduler codec; no firmware or GPU control."""
import argparse
import ctypes as c
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
DLL = None
LIBRARY_SHA = None
OWNER, EPOCH, CONTEXT = 19, 0x100000009, 11


class Message(c.Structure):
    _fields_ = [('fence', c.c_uint32), ('count', c.c_uint32), ('data', c.c_uint32 * 255)]


class Entry(c.Structure):
    _fields_ = [('fence', c.c_uint32), ('operation', c.c_uint32)]


class Context(c.Structure):
    _fields_ = [('epoch', c.c_uint64), ('revision', c.c_uint64),
                ('owner', c.c_uint32), ('contextId', c.c_uint32),
                ('phase', c.c_uint32), ('ledgerCount', c.c_uint32),
                ('ledger', Entry * 16), ('reserved', c.c_uint32 * 4)]


class Registration(c.Structure):
    _fields_ = [('lrcDescriptor', c.c_uint64), ('engineClass', c.c_uint32),
                ('engineSubmitMask', c.c_uint32), ('reserved', c.c_uint32 * 2)]


class Packet(c.Structure):
    _fields_ = [('epoch', c.c_uint64), ('revision', c.c_uint64),
                ('owner', c.c_uint32), ('contextId', c.c_uint32),
                ('operation', c.c_uint32), ('reserved', c.c_uint32), ('message', Message)]


class Observation(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in
                ('operation', 'action', 'hxgType', 'contextId', 'runnableState', 'error', 'hint', 'reserved')]


def bind_library(path):
    global DLL, LIBRARY_SHA
    source = Path(path).resolve(strict=True)
    if not source.is_file():
        raise ValueError('Regular compiled library required')
    LIBRARY_SHA = hashlib.sha256(source.read_bytes()).hexdigest()
    DLL = c.CDLL(str(source))
    signatures = {
        'lb_guc_scheduler_init': [c.POINTER(Context), c.c_uint32, c.c_uint32, c.c_uint64],
        'lb_guc_scheduler_encode': [c.POINTER(Context), c.c_uint32, c.c_uint64, c.c_uint32,
                                    c.POINTER(Registration), c.c_uint32, c.POINTER(Packet)],
        'lb_guc_scheduler_commit': [c.POINTER(Context), c.c_uint32, c.c_uint64, c.POINTER(Packet)],
        'lb_guc_scheduler_receive': [c.POINTER(Context), c.c_uint32, c.c_uint64,
                                     c.POINTER(Message), c.POINTER(Observation)],
        'lb_ct_encode': [c.c_uint32, c.POINTER(Message), c.POINTER(c.c_uint32), c.c_uint32],
        'lb_ct_decode': [c.c_uint32, c.POINTER(c.c_uint32), c.c_uint32, c.POINTER(Message)]}
    for name, arguments in signatures.items():
        function = getattr(DLL, name)
        function.argtypes, function.restype = arguments, c.c_uint32


def filled(kind):
    value = kind()
    c.memset(c.byref(value), 0xa5, c.sizeof(value))
    return value


def registration(**changes):
    value = Registration(0x12345119, 4, 1)
    for name, content in changes.items():
        setattr(value, name, content)
    return value


def event(action, context=CONTEXT, runnable=None, fence=0):
    value = Message(fence, 2 if runnable is None else 3)
    value.data[0], value.data[1] = 0x90000000 | action, context
    if runnable is not None:
        value.data[2] = runnable
    return value


def reply(kind, fence, payload=0, count=1):
    value = Message(fence, count)
    value.data[0] = 0x80000000 | kind << 28 | payload
    return value


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.context = Context()
        self.assertEqual(DLL.lb_guc_scheduler_init(c.byref(self.context), OWNER, CONTEXT, EPOCH), 0)

    def encode(self, operation, fence=None, owner=OWNER, epoch=EPOCH, supplied=None):
        result = Packet()
        if fence is None:
            fence = 0x8000 + self.context.ledgerCount
        supplied = registration() if operation == 1 and supplied is None else supplied
        status = DLL.lb_guc_scheduler_encode(c.byref(self.context), owner, epoch, operation,
                                             c.byref(supplied) if supplied is not None else None,
                                             fence, c.byref(result))
        return status, result

    def commit(self, operation, **options):
        status, packet = self.encode(operation, **options)
        self.assertEqual(status, 0)
        self.assertEqual(DLL.lb_guc_scheduler_commit(c.byref(self.context), OWNER, EPOCH, c.byref(packet)), 0)
        return packet

    def receive(self, message):
        result = Observation()
        status = DLL.lb_guc_scheduler_receive(c.byref(self.context), OWNER, EPOCH,
                                              c.byref(message), c.byref(result))
        return status, result

    def enable(self):
        self.commit(1)
        self.commit(2)
        self.assertEqual(self.receive(event(0x1002, runnable=1))[0], 0)

    def unchanged_rejection(self, message, owner=OWNER, epoch=EPOCH):
        before = bytes(self.context)
        result = filled(Observation)
        sentinel = bytes(result)
        status = DLL.lb_guc_scheduler_receive(c.byref(self.context), owner, epoch,
                                              c.byref(message), c.byref(result))
        self.assertNotEqual(status, 0)
        self.assertEqual(bytes(self.context), before)
        self.assertEqual(bytes(result), sentinel)
        return status

    def test_initialization_rejects_invalid_identity_without_changing_outputs(self):
        for owner, identity, epoch in ((0, 1, 1), (1, 65535, 1), (1, 0xffffffff, 1), (1, 1, 0)):
            value = filled(Context)
            before = bytes(value)
            self.assertNotEqual(DLL.lb_guc_scheduler_init(c.byref(value), owner, identity, epoch), 0)
            self.assertEqual(bytes(value), before)
        for identity in (0, 65534):
            value = Context()
            self.assertEqual(DLL.lb_guc_scheduler_init(c.byref(value), OWNER, identity, EPOCH), 0)

    def test_live_context_cannot_be_reinitialized_to_forget_fences(self):
        self.commit(1)
        before = bytes(self.context)
        self.assertEqual(DLL.lb_guc_scheduler_init(c.byref(self.context), OWNER, CONTEXT, EPOCH + 1), 4)
        self.assertEqual(bytes(self.context), before)

    def test_register_literal_xe_packet_has_twelve_hxg_dwords_and_zero_serial_wq(self):
        before = bytes(self.context)
        status, packet = self.encode(1, fence=0x8123)
        self.assertEqual(status, 0)
        self.assertEqual(bytes(self.context), before)
        self.assertEqual(packet.message.count, 12)
        self.assertEqual(list(packet.message.data)[:12],
                         [0x20004502, 1, 11, 4, 1, 0, 0, 0, 0, 0, 0x12345119, 0])
        frame = (c.c_uint32 * 13)()
        self.assertEqual(DLL.lb_ct_encode(0, c.byref(packet.message), frame, 13), 0)
        self.assertEqual(frame[0], 0x8123000c)
        decoded = Message()
        self.assertEqual(DLL.lb_ct_decode(0, frame, 13, c.byref(decoded)), 0)
        self.assertEqual(bytes(decoded), bytes(packet.message))

    def test_render_and_compute_classes_do_not_invent_engine_mask(self):
        for engine, mask in ((0, 1), (4, 0x80000000), (4, 3)):
            status, packet = self.encode(1, supplied=registration(engineClass=engine, engineSubmitMask=mask))
            self.assertEqual(status, 0)
            self.assertEqual((packet.message.data[3], packet.message.data[4]), (engine, mask))

    def test_lnl_descriptor_flags_and_engine_inputs_leave_packet_unchanged_on_error(self):
        inputs = [registration(lrcDescriptor=value) for value in
                  (0, 0x19, 0x12345018, 0x12345001, 0x12345039, 0x1000012345019, 0xffffffffffffffff)]
        inputs += [registration(engineClass=engine) for engine in (1, 2, 3, 5, 0xffffffff)]
        inputs += [registration(engineSubmitMask=0)]
        bad = registration()
        bad.reserved[1] = 1
        inputs.append(bad)
        for value in inputs:
            packet = filled(Packet)
            before = bytes(packet)
            self.assertEqual(DLL.lb_guc_scheduler_encode(c.byref(self.context), OWNER, EPOCH, 1,
                             c.byref(value), 0x8001, c.byref(packet)), 6)
            self.assertEqual(bytes(packet), before)
            self.assertEqual(self.context.phase, 0)

    def test_wrong_owner_epoch_and_fence_never_mutate_proposals(self):
        value = registration()
        for owner, epoch, fence, expected in ((OWNER + 1, EPOCH, 0x8001, 2),
                                              (OWNER, EPOCH + 1, 0x8001, 3),
                                              (OWNER, EPOCH, 0x7fff, 7),
                                              (OWNER, EPOCH, 0x10000, 7)):
            packet = filled(Packet)
            before = bytes(packet)
            self.assertEqual(DLL.lb_guc_scheduler_encode(c.byref(self.context), owner, epoch, 1,
                             c.byref(value), fence, c.byref(packet)), expected)
            self.assertEqual(bytes(packet), before)

    def test_only_legal_operations_and_required_register_input_are_encoded(self):
        for operation in (0, 6, 0xffffffff, 2, 3, 4, 5):
            self.assertNotEqual(self.encode(operation)[0], 0)
        packet = filled(Packet)
        self.assertEqual(DLL.lb_guc_scheduler_encode(c.byref(self.context), OWNER, EPOCH, 1,
                         None, 0x8001, c.byref(packet)), 1)
        self.commit(1)
        supplied = registration()
        self.assertEqual(DLL.lb_guc_scheduler_encode(c.byref(self.context), OWNER, EPOCH, 2,
                         c.byref(supplied), 0x8001, c.byref(packet)), 1)

    def test_commits_are_single_use_and_prepared_packets_do_not_change_state(self):
        status, first = self.encode(1)
        self.assertEqual(status, 0)
        self.assertEqual((self.context.phase, self.context.revision), (0, 1))
        self.assertEqual(DLL.lb_guc_scheduler_commit(c.byref(self.context), OWNER, EPOCH, c.byref(first)), 0)
        before = bytes(self.context)
        self.assertEqual(DLL.lb_guc_scheduler_commit(c.byref(self.context), OWNER, EPOCH, c.byref(first)), 9)
        self.assertEqual(bytes(self.context), before)

    def test_malformed_commit_flags_headers_counts_and_unused_payload_are_atomic(self):
        _, base = self.encode(1)
        mutations = [('count', 0), ('count', 11), ('count', 13), ('count', 255),
                     (0, 0x4502), (0, 0x30004502), (1, 2), (2, CONTEXT + 1),
                     (5, 1), (9, 1024), (12, 1), (254, 1)]
        for field, value in mutations:
            packet = Packet.from_buffer_copy(base)
            if field == 'count':
                packet.message.count = value
            else:
                packet.message.data[field] = value
            before = bytes(self.context)
            self.assertNotEqual(DLL.lb_guc_scheduler_commit(c.byref(self.context), OWNER, EPOCH, c.byref(packet)), 0)
            self.assertEqual(bytes(self.context), before)

    def test_proposal_metadata_owner_epoch_context_reserved_and_revision_are_checked(self):
        _, base = self.encode(1)
        for name, value in (('owner', OWNER + 1), ('epoch', EPOCH + 1), ('contextId', CONTEXT + 1),
                            ('reserved', 1), ('revision', base.revision + 1)):
            packet = Packet.from_buffer_copy(base)
            setattr(packet, name, value)
            before = bytes(self.context)
            self.assertNotEqual(DLL.lb_guc_scheduler_commit(c.byref(self.context), OWNER, EPOCH, c.byref(packet)), 0)
            self.assertEqual(bytes(self.context), before)

    def test_enable_submit_disable_and_deregister_literal_actions(self):
        self.commit(1)
        packet = self.commit(2)
        self.assertEqual(list(packet.message.data)[:3], [0x20001001, CONTEXT, 1])
        self.assertEqual(self.encode(3)[0], 4)
        self.assertEqual(self.receive(event(0x1002, runnable=1))[0], 0)
        packet = self.commit(3)
        self.assertEqual(list(packet.message.data)[:2], [0x20001000, CONTEXT])
        packet = self.commit(4)
        self.assertEqual(list(packet.message.data)[:3], [0x20001001, CONTEXT, 0])
        self.assertEqual(self.encode(5)[0], 4)
        self.assertEqual(self.receive(event(0x1002, runnable=0))[0], 0)
        packet = self.commit(5)
        self.assertEqual(list(packet.message.data)[:2], [0x20004503, CONTEXT])
        self.assertEqual(self.receive(event(0x4600))[0], 0)
        self.assertEqual(self.context.phase, 7)
        self.assertGreater(self.context.ledgerCount, 0)

    def test_register_does_not_wait_for_or_accept_a_fabricated_success_reply(self):
        packet = self.commit(1)
        self.assertEqual(self.context.phase, 1)
        self.assertEqual(self.unchanged_rejection(reply(7, packet.message.fence)), 11)
        self.assertEqual(self.unchanged_rejection(event(0x4502)), 11)
        self.assertEqual(self.unchanged_rejection(reply(3, packet.message.fence)), 11)
        self.assertEqual(self.unchanged_rejection(reply(5, packet.message.fence)), 11)

    def test_context_mode_done_checks_dword_count_owner_and_runnable_direction(self):
        self.commit(1)
        self.commit(2)
        for value in (event(0x1002, context=CONTEXT + 1, runnable=1),
                      event(0x1002, runnable=0), event(0x1002, runnable=2), event(0x1002)):
            self.unchanged_rejection(value)
        self.assertEqual(self.receive(event(0x1002, runnable=1, fence=0xabcd))[0], 0)
        self.unchanged_rejection(event(0x1002, runnable=1))

    def test_deregister_done_requires_pending_deregister_and_retains_ledger(self):
        self.commit(1)
        self.unchanged_rejection(event(0x4600))
        self.commit(5)
        before_count = self.context.ledgerCount
        self.unchanged_rejection(event(0x4600, context=CONTEXT + 1))
        self.assertEqual(self.receive(event(0x4600))[0], 0)
        self.assertEqual(self.context.ledgerCount, before_count)
        self.unchanged_rejection(event(0x4600))
        self.assertEqual(self.encode(1)[0], 4)

    def test_past_register_failure_is_correlated_after_later_mode_command(self):
        register = self.commit(1)
        self.commit(2)
        status, observation = self.receive(reply(6, register.message.fence, 0xabc0042))
        self.assertEqual(status, 0)
        self.assertEqual((observation.operation, observation.action, observation.error, observation.hint),
                         (1, 0x4502, 0x42, 0xabc))
        self.assertEqual(self.context.phase, 8)
        self.assertEqual(self.context.ledgerCount, 2)
        self.assertEqual(self.encode(5)[0], 4)
        self.unchanged_rejection(event(0x1002, runnable=1))

    def test_failure_reply_requires_exact_fast_request_fence_and_one_dword(self):
        packet = self.commit(1)
        for value in (reply(6, 0x8fff), reply(6, packet.message.fence & 0x7fff),
                      reply(6, 0x10000), reply(6, packet.message.fence, count=2)):
            self.unchanged_rejection(value)

    def test_busy_retry_success_and_unknown_hxg_types_cannot_promote_fast_requests(self):
        packet = self.commit(1)
        for kind in (0, 2, 3, 4, 5, 7):
            self.assertEqual(self.unchanged_rejection(reply(kind, packet.message.fence)), 11)

    def test_non_guc_origin_unknown_action_data0_and_bad_sizes_preserve_observation(self):
        self.commit(1)
        self.commit(2)
        for header, count in ((0x10001002, 3), (0x900010ff, 3), (0x90011002, 3),
                              (0x90001002, 0), (0x90001002, 4), (0x90001002, 255)):
            message = event(0x1002, runnable=1)
            message.data[0], message.count = header, count
            self.unchanged_rejection(message)

    def test_receive_owner_and_local_epoch_mismatch_never_change_state(self):
        self.commit(1)
        self.commit(2)
        message = event(0x1002, runnable=1)
        self.assertEqual(self.unchanged_rejection(message, owner=OWNER + 1), 2)
        self.assertEqual(self.unchanged_rejection(message, epoch=EPOCH + 1), 3)

    def test_bounded_ledger_preserves_two_cleanup_slots_and_never_evicts_fences(self):
        self.enable()
        while self.context.ledgerCount < 14:
            self.commit(3)
        first_fence = self.context.ledger[0].fence
        before = bytes(self.context)
        self.assertEqual(self.encode(3)[0], 8)
        self.assertEqual(bytes(self.context), before)
        self.commit(4)
        self.assertEqual(self.receive(event(0x1002, runnable=0))[0], 0)
        self.commit(5)
        self.assertEqual(self.receive(event(0x4600))[0], 0)
        self.assertEqual(self.context.ledgerCount, 16)
        self.assertEqual(self.context.ledger[0].fence, first_fence)
        self.assertEqual(self.receive(reply(6, first_fence, 1))[0], 0)
        self.assertEqual(self.context.phase, 8)

    def test_duplicate_fast_fences_are_not_reused_after_mode_done(self):
        self.enable()
        for fence in (self.context.ledger[0].fence, self.context.ledger[1].fence):
            self.assertEqual(self.encode(3, fence=fence)[0], 7)

    def test_stale_submit_proposal_cannot_be_committed_after_disable_transition(self):
        self.enable()
        status, submit = self.encode(3, fence=0x9000)
        self.assertEqual(status, 0)
        self.commit(4)
        before = bytes(self.context)
        self.assertEqual(DLL.lb_guc_scheduler_commit(c.byref(self.context), OWNER, EPOCH, c.byref(submit)), 9)
        self.assertEqual(bytes(self.context), before)

    def test_corrupt_context_members_tail_fences_and_phase_are_refused(self):
        self.commit(1)
        original = bytes(self.context)
        for mutation in ('reserved', 'tail', 'phase', 'count', 'first', 'missing-owner', 'revision'):
            self.context = Context.from_buffer_copy(original)
            if mutation == 'reserved': self.context.reserved[0] = 1
            elif mutation == 'tail': self.context.ledger[15].fence = 0x800f
            elif mutation == 'phase': self.context.phase = 3
            elif mutation == 'count': self.context.ledgerCount = 17
            elif mutation == 'first': self.context.ledger[0].operation = 5
            elif mutation == 'missing-owner': self.context.owner = 0
            else: self.context.revision = 0
            self.assertEqual(self.encode(2)[0], 10)

    def test_revision_exhaustion_and_corrupt_init_reserved_keep_all_bytes(self):
        self.context.revision = 0xffffffffffffffff
        self.assertEqual(self.encode(1)[0], 8)
        for index in (0, 3):
            value = Context()
            value.reserved[index] = 1
            before = bytes(value)
            self.assertEqual(DLL.lb_guc_scheduler_init(c.byref(value), OWNER, CONTEXT, EPOCH), 10)
            self.assertEqual(bytes(value), before)

    def test_null_and_overlapping_objects_fail_before_any_write(self):
        value = registration()
        packet = Packet()
        observation = Observation()
        message = event(0x1002, runnable=1)
        self.assertEqual(DLL.lb_guc_scheduler_init(None, OWNER, CONTEXT, EPOCH), 1)
        self.assertEqual(DLL.lb_guc_scheduler_encode(None, OWNER, EPOCH, 1, c.byref(value), 0x8001, c.byref(packet)), 1)
        self.assertEqual(DLL.lb_guc_scheduler_commit(c.byref(self.context), OWNER, EPOCH, None), 1)
        self.assertEqual(DLL.lb_guc_scheduler_receive(c.byref(self.context), OWNER, EPOCH, None, c.byref(observation)), 1)
        before = bytes(self.context)
        alias_packet = c.cast(c.byref(self.context), c.POINTER(Packet))
        self.assertEqual(DLL.lb_guc_scheduler_encode(c.byref(self.context), OWNER, EPOCH, 1,
                         c.byref(value), 0x8001, alias_packet), 1)
        alias_observation = c.cast(c.byref(self.context), c.POINTER(Observation))
        self.assertEqual(DLL.lb_guc_scheduler_receive(c.byref(self.context), OWNER, EPOCH,
                         c.byref(message), alias_observation), 1)
        self.assertEqual(bytes(self.context), before)


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append({'name': test.id().rsplit('.', 1)[-1], 'state': 'passed'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('library', type=Path, help='Explicit compiled library containing scheduler and CT codecs')
    args = parser.parse_args()
    bind_library(args.library)
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(SchedulerTests))
    names = ('gpu-prototype/lb_guc_scheduler.h', 'gpu-prototype/lb_guc_scheduler.cpp',
             'tests/test_guc_scheduler.py', 'docs/GUC_SCHEDULER.md',
             'gpu-prototype/GUC_SCHEDULER_SOURCES.json',
             'gpu-prototype/lb_guc_transport.h', 'gpu-prototype/lb_guc_transport.cpp')
    report = {'schema': 1, 'kind': 'cpu-guC-wire-contract-tests', 'host_platform': sys.platform,
              'library_sha256': LIBRARY_SHA,
              'source_files_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names},
              'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'skips': len(result.skipped), 'passed': result.wasSuccessful(), 'test_cases': result.cases,
              'target_hardware_tested': False, 'firmware_authenticated': False, 'guC_running': False,
              'queue_registered_on_hardware': False, 'hardware_submission_verified': False,
              'gpu_execution_verified': False, 'metal_provider_registered': False}
    (ROOT / 'tests/guc-scheduler-test-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
