# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Execute the real DMA ownership table with fake IOKit; no hardware claims."""
import ctypes as c
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIB = c.CDLL(str(ROOT/'build/host/lnl_host.dll'))
LIB.lb_dma_quarantine_test_run.argtypes = [c.c_uint32, c.POINTER(c.c_uint32)]
LIB.lb_dma_quarantine_test_run.restype = c.c_int

class QuarantineTests(unittest.TestCase):
    def check_scenario(self, number):
        report = (c.c_uint32*6)()
        self.assertEqual(LIB.lb_dma_quarantine_test_run(number, report), 1)
        self.assertEqual(list(report), [1, 1, 0, 0, 1, 1])

    def test_owner_zero_and_duplicate_reservations(self): self.check_scenario(0)
    def test_eight_slots_bound_all_admissions(self): self.check_scenario(1)
    def test_release_requires_exact_owner_and_is_once_only(self): self.check_scenario(2)
    def test_empty_or_null_context_cannot_enter_quarantine(self): self.check_scenario(3)
    def test_unready_but_retained_context_survives_failed_reset(self): self.check_scenario(4)
    def test_retry_is_exclusive_and_cannot_commit_before_actual_cleanup(self): self.check_scenario(5)
    def test_persistent_failure_keeps_slot_until_later_success(self): self.check_scenario(6)
    def test_bad_transfer_is_atomic_and_preserves_reservation(self): self.check_scenario(7)
    def test_same_context_or_buffer_cannot_be_owned_by_two_slots(self): self.check_scenario(8)
    def test_readonly_clients_release_only_their_reserved_slots(self): self.check_scenario(9)

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(QuarantineTests))
    (ROOT/'tests/dma-quarantine-test-report.json').write_text(json.dumps({
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'successful': result.wasSuccessful(),
        'actual_cpp_ownership_table_executed': True, 'iokit_calls_simulated': True,
        'native_hardware_tested': False, 'parallel_kernel_lifetime_tested': False,
    }, indent=2)+'\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
