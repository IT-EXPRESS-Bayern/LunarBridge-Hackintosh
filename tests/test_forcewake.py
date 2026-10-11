# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Execute production forcewake code with fault-injected register callbacks.

This suite proves bounded CPU protocol behaviour, not a GPU/IOKit handshake.
"""
import ctypes as c
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
CONTROL, ACK, GET, PUT = 0xa188, 0xdfc, 0x10001, 0x10000
OK, BAD_IO, BAD_STATE, BUSY, IO_FAILED, INVALID_MMIO, TIMEOUT, CLOCK_FAILED, IDLE = range(9)

class State(c.Structure):
    _fields_ = [(key, c.c_uint32) for key in (
        'magic', 'references', 'owned', 'awake', 'unresolved', 'reads', 'writes', 'polls', 'lastAck')]
    _fields_ += [('reserved', c.c_uint32 * 3)]

Read = c.CFUNCTYPE(c.c_bool, c.c_void_p, c.c_uint32, c.POINTER(c.c_uint32))
Write = c.CFUNCTYPE(c.c_bool, c.c_void_p, c.c_uint32, c.c_uint32)
Sleep = c.CFUNCTYPE(c.c_bool, c.c_void_p, c.c_uint32)
Clock = c.CFUNCTYPE(c.c_uint64, c.c_void_p)

class IO(c.Structure):
    _fields_ = [('context', c.c_void_p), ('read32', Read), ('write32', Write),
               ('sleepUs', Sleep), ('nowUs', Clock)]

class Registers:
    def __init__(self):
        self.control, self.ack, self.time, self.phase = 0x80, 0x80, 0, None
        self.get_delay, self.put_delay, self.transition_at = 0, 0, 0
        self.get_stuck = self.put_stuck = self.sleep_fail = self.clock_stalled = False
        self.read_fail_at, self.write_fail_at, self.bad_read_at = 0, 0, 0
        self.reads, self.writes, self.sleeps, self.bad_offsets = [], [], [], []
        self.late_read_us, self.clock_values = 0, []
        self.cookie = c.c_uint32(1)
        # Hold callbacks for the whole C++ call lifetime.
        self.callbacks = (Read(self.read), Write(self.write), Sleep(self.sleep), Clock(self.clock))
        self.io = IO(c.addressof(self.cookie), *self.callbacks)

    def read(self, context, offset, out):
        self.reads.append(offset)
        if offset not in (CONTROL, ACK):
            self.bad_offsets.append(offset)
            return False
        if self.read_fail_at == len(self.reads): return False
        if self.bad_read_at == len(self.reads):
            out[0] = 0xffffffff
            return True
        if offset == ACK and self.phase:
            self.time += self.late_read_us
            if self.time >= self.transition_at:
                if self.phase == 'get' and not self.get_stuck: self.ack |= 1
                if self.phase == 'put' and not self.put_stuck: self.ack &= ~1
        out[0] = self.control if offset == CONTROL else self.ack
        return True

    def write(self, context, offset, value):
        self.writes.append((offset, value))
        if offset != CONTROL or value not in (GET, PUT):
            self.bad_offsets.append(offset)
            return False
        # Model a write reaching hardware even when its callback reports failure.
        self.phase = 'get' if value == GET else 'put'
        self.control = (self.control & ~1) | (value & 1)
        self.transition_at = self.time + (self.get_delay if value == GET else self.put_delay)
        return self.write_fail_at != len(self.writes)

    def sleep(self, context, us):
        self.sleeps.append(us)
        if not self.clock_stalled: self.time += us
        return not self.sleep_fail

    def clock(self, context):
        return self.clock_values.pop(0) if self.clock_values else self.time

class ForceWakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dll = c.CDLL(os.environ.get('LB_FORCEWAKE_LIBRARY', str(ROOT/'build/host/lnl_host.dll')))
        cls.dll.lb_forcewake_init.argtypes = [c.POINTER(State)]
        for method in ('lb_forcewake_get', 'lb_forcewake_put'):
            getattr(cls.dll, method).argtypes = [c.POINTER(State), c.POINTER(IO)]
            getattr(cls.dll, method).restype = c.c_uint32
        cls.dll.lb_forcewake_drained.argtypes = [c.POINTER(State)]

    def setUp(self):
        self.state, self.registers = State(), Registers()
        self.assertEqual(self.dll.lb_forcewake_init(c.byref(self.state)), OK)

    def get(self): return self.dll.lb_forcewake_get(c.byref(self.state), c.byref(self.registers.io))
    def put(self): return self.dll.lb_forcewake_put(c.byref(self.state), c.byref(self.registers.io))
    def drained(self): return bool(self.dll.lb_forcewake_drained(c.byref(self.state)))
    def outstanding(self):
        self.assertFalse(self.drained())
        self.assertEqual((self.state.references, self.state.owned, self.state.awake, self.state.unresolved), (0, 1, 0, 1))

    def test_exact_paired_masked_writes_preserve_other_requestors(self):
        self.assertEqual((self.get(), self.put()), (OK, OK))
        self.assertEqual(self.registers.writes, [(CONTROL, GET), (CONTROL, PUT)])
        self.assertEqual((self.registers.control, self.registers.ack), (0x80, 0x80))
        self.assertEqual(self.registers.bad_offsets, [])
        self.assertTrue(self.drained())

    def test_nested_references_do_not_issue_extra_requests(self):
        self.assertEqual((self.get(), self.get(), self.put()), (OK, OK, OK))
        self.assertEqual(self.state.references, 1)
        self.assertEqual(self.registers.writes, [(CONTROL, GET)])
        self.assertEqual(self.put(), OK)
        self.assertTrue(self.drained())

    def test_idle_put_never_touches_registers(self):
        self.assertEqual(self.put(), IDLE)
        self.assertEqual((self.registers.reads, self.registers.writes), ([], []))

    def test_existing_control_request_is_not_stolen(self):
        self.registers.control |= 1
        self.assertEqual(self.get(), BUSY)
        self.assertEqual(self.registers.writes, [])
        self.assertTrue(self.drained())

    def test_existing_ack_is_not_cleared(self):
        self.registers.ack |= 1
        self.assertEqual(self.get(), BUSY)
        self.assertEqual(self.put(), IDLE)
        self.assertEqual(self.registers.writes, [])

    def test_all_ones_control_is_error_before_request(self):
        self.registers.control = 0xffffffff
        self.assertEqual(self.get(), INVALID_MMIO)
        self.assertTrue(self.drained())
        self.assertEqual(self.registers.writes, [])

    def test_all_ones_initial_ack_is_error_before_request(self):
        self.registers.ack = 0xffffffff
        self.assertEqual(self.get(), INVALID_MMIO)
        self.assertEqual(self.state.lastAck, 0xffffffff)
        self.assertTrue(self.drained())

    def test_failed_initial_read_never_issues_request(self):
        self.registers.read_fail_at = 2
        self.assertEqual(self.get(), IO_FAILED)
        self.assertTrue(self.drained())

    def test_failed_get_write_may_have_reached_device_and_requires_put(self):
        self.registers.write_fail_at = 1
        self.assertEqual(self.get(), IO_FAILED)
        self.outstanding()
        self.assertEqual(self.get(), BUSY)
        self.assertEqual(self.put(), OK)
        self.assertTrue(self.drained())
        self.assertEqual(self.registers.writes, [(CONTROL, GET), (CONTROL, PUT)])

    def test_invalid_ack_after_request_requires_cleanup(self):
        self.registers.bad_read_at = 3
        self.assertEqual(self.get(), INVALID_MMIO)
        self.outstanding()
        self.assertEqual(self.put(), OK)

    def test_failed_ack_after_request_requires_cleanup(self):
        self.registers.read_fail_at = 3
        self.assertEqual(self.get(), IO_FAILED)
        self.outstanding()
        self.assertEqual(self.put(), OK)

    def test_get_timeout_is_bounded_at_50ms(self):
        self.registers.get_stuck = True
        self.assertEqual(self.get(), TIMEOUT)
        self.assertEqual(self.registers.time, 50000)
        self.assertEqual(len(self.registers.sleeps), 50)
        self.outstanding()
        self.assertEqual(self.put(), OK)

    def test_delayed_ack_before_deadline_can_succeed(self):
        self.registers.get_delay, self.registers.put_delay = 49000, 49000
        self.assertEqual((self.get(), self.put()), (OK, OK))
        self.assertEqual(self.registers.time, 98000)

    def test_ack_read_crossing_deadline_is_not_success(self):
        self.registers.late_read_us = 50000
        self.assertEqual(self.get(), TIMEOUT)
        self.outstanding()
        self.registers.late_read_us = 0
        self.assertEqual(self.put(), OK)

    def test_release_ack_read_crossing_deadline_is_retained(self):
        self.assertEqual(self.get(), OK)
        self.registers.late_read_us = 50000
        self.assertEqual(self.put(), TIMEOUT)
        self.outstanding()
        self.registers.late_read_us = 0
        self.assertEqual(self.put(), OK)

    def test_put_timeout_retains_lease_until_successful_retry(self):
        self.assertEqual(self.get(), OK)
        self.registers.put_stuck = True
        self.assertEqual(self.put(), TIMEOUT)
        self.outstanding()
        self.assertEqual(self.get(), BUSY)
        self.registers.put_stuck = False
        self.assertEqual(self.put(), OK)
        self.assertTrue(self.drained())

    def test_failed_put_write_keeps_unresolved_ownership(self):
        self.assertEqual(self.get(), OK)
        self.registers.write_fail_at = 2
        self.assertEqual(self.put(), IO_FAILED)
        self.outstanding()
        self.assertEqual(self.put(), OK)

    def test_invalid_release_ack_is_not_treated_as_clear(self):
        self.assertEqual(self.get(), OK)
        self.registers.bad_read_at = len(self.registers.reads) + 1
        self.assertEqual(self.put(), INVALID_MMIO)
        self.outstanding()
        self.assertEqual(self.put(), OK)

    def test_sleep_failure_retains_request(self):
        self.registers.get_stuck = self.registers.sleep_fail = True
        self.assertEqual(self.get(), IO_FAILED)
        self.outstanding()
        self.registers.sleep_fail = False
        self.assertEqual(self.put(), OK)

    def test_backward_clock_fails_without_unbounded_wait(self):
        self.registers.clock_values = [10000, 9000]
        self.assertEqual(self.get(), CLOCK_FAILED)
        self.outstanding()
        self.assertEqual(self.put(), OK)

    def test_backward_clock_after_ack_read_is_rejected(self):
        self.registers.clock_values = [10000, 10000, 9000]
        self.assertEqual(self.get(), CLOCK_FAILED)
        self.outstanding()

    def test_stalled_clock_still_has_finite_poll_bound(self):
        self.registers.get_stuck = self.registers.clock_stalled = True
        self.assertEqual(self.get(), TIMEOUT)
        self.assertEqual(len(self.registers.sleeps), 64)
        self.assertEqual(self.state.polls, 64)
        self.outstanding()

    def test_reinitialization_cannot_erase_live_or_drained_state(self):
        self.assertEqual(self.get(), OK)
        self.assertEqual(self.dll.lb_forcewake_init(c.byref(self.state)), BAD_STATE)
        self.assertEqual(self.state.references, 1)
        self.assertEqual(self.put(), OK)
        self.assertEqual(self.dll.lb_forcewake_init(c.byref(self.state)), BAD_STATE)

    def test_reference_overflow_is_rejected_without_io(self):
        self.assertEqual(self.get(), OK)
        self.state.references = 65535
        prior = (len(self.registers.reads), len(self.registers.writes))
        self.assertEqual(self.get(), BUSY)
        self.assertEqual((len(self.registers.reads), len(self.registers.writes)), prior)

    def test_corrupted_state_rejects_io_and_is_not_drained(self):
        for field, value in [('magic', 0), ('references', 1), ('owned', 2), ('awake', 1), ('unresolved', 1)]:
            with self.subTest(field=field):
                state = State.from_buffer_copy(bytes(self.state))
                setattr(state, field, value)
                self.assertEqual(self.dll.lb_forcewake_get(c.byref(state), c.byref(self.registers.io)), BAD_STATE)
                self.assertFalse(self.dll.lb_forcewake_drained(c.byref(state)))
        self.assertEqual((self.registers.reads, self.registers.writes), ([], []))

    def test_reserved_state_rejects_reinitialization_and_operations(self):
        self.state.reserved[2] = 1
        self.assertEqual(self.get(), BAD_STATE)
        self.assertEqual(self.put(), BAD_STATE)
        self.assertFalse(self.drained())

    def test_null_io_rejects_before_hardware(self):
        self.assertEqual(self.dll.lb_forcewake_get(c.byref(self.state), None), BAD_IO)
        self.registers.io.context = None
        self.assertEqual(self.get(), BAD_IO)
        self.assertTrue(self.drained())

    def test_read_counter_overflow_fails_without_wrapping(self):
        self.state.reads = 0xffffffff
        self.assertEqual(self.get(), BAD_STATE)
        self.assertEqual(self.registers.reads, [])

    def test_write_counter_overflow_still_preserves_unresolved_request(self):
        self.state.writes = 0xffffffff
        self.assertEqual(self.get(), BAD_STATE)
        self.outstanding()
        self.assertEqual(self.registers.writes, [])

if __name__ == '__main__': unittest.main()
