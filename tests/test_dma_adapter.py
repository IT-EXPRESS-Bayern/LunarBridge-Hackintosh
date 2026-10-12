# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Execute the actual DMA adapter with explicit IOKit doubles; no hardware."""
import ctypes as c
import json
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
class Report(c.Structure):
    _fields_ = [(key, c.c_uint32) for key in (
        'synthetic', 'prepareStatus', 'ready', 'segments', 'ptes', 'reentryStatus',
        'firstReset', 'secondReset', 'finalReady', 'badSequence', 'mapperCopies',
        'mapperReleases', 'memoryRetains', 'memoryReleases', 'wireCalls', 'unwireCalls',
        'commandCreated', 'commandReleased', 'attachCalls', 'detachCalls',
        'prepareCalls', 'completeCalls', 'segmentCalls', 'heldAfterFirstReset', 'finalHeld', 'reserved')]

class DMAAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Same override as tests/test_forcewake.py, so a host library built
        # elsewhere (tools/recon.py) can be tested in place.
        cls.dll = c.CDLL(os.environ.get('LB_HOST_LIBRARY',
                                        str(ROOT/'build/host/lnl_host.dll')))
        cls.dll.lb_dma_test_run.argtypes = [c.c_uint32, c.c_uint64, c.c_uint32, c.c_uint64, c.POINTER(Report)]
        cls.dll.lb_dma_test_run.restype = c.c_int

    def run_adapter(self, fault=0, size=8192, owner=7, descriptor_size=None):
        result = Report()
        self.assertEqual(self.dll.lb_dma_test_run(fault, size, owner,
            size if descriptor_size is None else descriptor_size, c.byref(result)), 1)
        self.assertEqual((result.synthetic, result.badSequence, result.finalReady, result.finalHeld, result.reserved),
                         (1, 0, 0, 0, 0))
        self.assertEqual(result.memoryRetains, result.memoryReleases)
        self.assertEqual(result.commandCreated, result.commandReleased)
        self.assertEqual(result.secondReset, 0)
        return result

    def rejected(self, fault, **kwargs):
        r = self.run_adapter(fault, **kwargs)
        self.assertNotEqual(r.prepareStatus, 0)
        self.assertEqual((r.ready, r.segments, r.ptes), (0, 0, 0))
        return r

    def test_one_page_prepared_and_released_in_order(self):
        r = self.run_adapter(size=4096)
        self.assertEqual((r.prepareStatus, r.ready, r.segments, r.ptes), (0, 1, 1, 1))
        self.assertEqual((r.mapperCopies, r.mapperReleases, r.wireCalls, r.unwireCalls,
                          r.attachCalls, r.detachCalls, r.prepareCalls, r.completeCalls), (1, 1, 1, 1, 1, 1, 1, 1))

    def test_maximum_buffer_has_256_validated_pages(self):
        r = self.run_adapter(size=1048576)
        self.assertEqual((r.ready, r.segments, r.ptes, r.segmentCalls), (1, 256, 256, 256))

    def test_invalid_sizes_and_owner_never_acquire_a_mapper(self):
        for size, owner in ((0, 1), (1, 1), (4095, 1), (4097, 1), (1048577, 1),
                            (2**64-1, 1), (4096, 0)):
            with self.subTest(size=size, owner=owner):
                r = self.rejected(0, size=size, owner=owner)
                self.assertEqual((r.mapperCopies, r.wireCalls), (0, 0))

    def test_descriptor_length_must_match_request(self):
        r = self.rejected(0, descriptor_size=4096)
        self.assertEqual(r.mapperCopies, 0)

    def test_null_provider_rejected_before_mapping(self):
        self.assertEqual(self.rejected(19).mapperCopies, 0)

    def test_null_memory_rejected_before_mapping(self):
        self.assertEqual(self.rejected(20).mapperCopies, 0)

    def test_no_device_mapper_has_no_system_or_physical_fallback(self):
        r = self.rejected(1)
        self.assertEqual((r.prepareStatus, r.mapperCopies, r.mapperReleases, r.wireCalls), (0xe00002c7, 1, 0, 0))

    def test_memory_prepare_failure_does_not_unwire_unprepared_memory(self):
        r = self.rejected(2)
        self.assertEqual((r.wireCalls, r.unwireCalls, r.commandCreated, r.mapperReleases), (1, 0, 0, 1))

    def test_command_allocation_failure_unwires_memory(self):
        r = self.rejected(3)
        self.assertEqual((r.wireCalls, r.unwireCalls, r.commandCreated), (1, 1, 0))

    def test_descriptor_attachment_failure_does_not_complete_a_command(self):
        r = self.rejected(4)
        self.assertEqual((r.attachCalls, r.detachCalls, r.prepareCalls, r.completeCalls), (1, 0, 0, 0))

    def test_command_prepare_failure_detaches_and_unwires(self):
        r = self.rejected(5)
        self.assertEqual((r.prepareCalls, r.completeCalls, r.detachCalls, r.unwireCalls), (1, 0, 1, 1))

    def test_segment_error_completes_the_prepared_command(self):
        r = self.rejected(6)
        self.assertEqual((r.prepareCalls, r.completeCalls, r.detachCalls, r.unwireCalls), (1, 1, 1, 1))

    def test_short_segment_rejected(self): self.rejected(7)
    def test_dma_address_outside_46_bit_range_rejected(self): self.rejected(8)
    def test_overlapping_dma_segments_rejected(self): self.rejected(9)
    def test_unaligned_dma_address_rejected(self): self.rejected(10)
    def test_zero_segment_count_rejected(self): self.rejected(11)
    def test_nonadvancing_dma_cursor_rejected(self): self.rejected(12)
    def test_cursor_beyond_descriptor_rejected(self): self.rejected(13)
    def test_zero_segment_length_rejected(self): self.rejected(14)

    def test_late_segment_failure_does_not_publish_partial_ready_state(self):
        r = self.rejected(15)
        self.assertEqual((r.segmentCalls, r.completeCalls), (2, 1))

    def test_completion_failure_retains_mapping_until_retry(self):
        r = self.run_adapter(16)
        self.assertNotEqual(r.firstReset, 0)
        self.assertEqual((r.ready, r.heldAfterFirstReset, r.completeCalls, r.detachCalls), (1, 1, 2, 1))

    def test_detach_failure_retains_wired_memory_until_retry(self):
        r = self.run_adapter(17)
        self.assertNotEqual(r.firstReset, 0)
        self.assertEqual((r.heldAfterFirstReset, r.detachCalls, r.completeCalls), (1, 2, 1))

    def test_unwire_failure_retains_descriptor_until_retry(self):
        r = self.run_adapter(18)
        self.assertNotEqual(r.firstReset, 0)
        self.assertEqual((r.heldAfterFirstReset, r.unwireCalls, r.memoryReleases), (1, 2, 1))

    def test_active_context_cannot_be_reinitialised(self):
        r = self.run_adapter()
        self.assertEqual((r.reentryStatus, r.mapperCopies, r.commandCreated), (0xe00002d5, 1, 1))

    def test_repeated_reset_is_idempotent(self):
        r = self.run_adapter()
        self.assertEqual((r.firstReset, r.secondReset, r.commandReleased, r.mapperReleases), (0, 0, 1, 1))

    def test_null_output_and_unknown_fault_rejected(self):
        self.assertEqual(self.dll.lb_dma_test_run(0, 4096, 1, 4096, None), 0)
        self.assertEqual(self.dll.lb_dma_test_run(24, 4096, 1, 4096, c.byref(Report())), 0)

    def persistent_cleanup_failure(self, fault):
        result = Report()
        self.assertEqual(self.dll.lb_dma_test_run(fault, 4096, 1, 4096, c.byref(result)), 1)
        self.assertEqual((result.synthetic, result.prepareStatus, result.ready, result.finalReady,
                          result.finalHeld, result.badSequence), (1, 0, 1, 0, 1, 0))
        self.assertNotEqual(result.firstReset, 0)
        self.assertNotEqual(result.secondReset, 0)
        self.assertEqual((result.mapperReleases, result.memoryReleases, result.commandReleased), (0, 0, 0))

    def test_persistent_dma_completion_failure_keeps_all_references(self): self.persistent_cleanup_failure(21)
    def test_persistent_detachment_failure_keeps_all_references(self): self.persistent_cleanup_failure(22)
    def test_persistent_unwire_failure_keeps_all_references(self): self.persistent_cleanup_failure(23)

if __name__ == '__main__':
    outcome = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DMAAdapterTests))
    (ROOT/'tests/dma-adapter-test-report.json').write_text(json.dumps({
        'tests_run': outcome.testsRun, 'failures': len(outcome.failures), 'errors': len(outcome.errors),
        'skipped': len(outcome.skipped), 'successful': outcome.wasSuccessful(),
        'io_kit_calls_simulated': True, 'actual_adapter_cpp_executed': True,
        'native_dma_hardware_tested': False, 'gpu_binding_or_execution_tested': False,
    }, indent=2)+'\n', encoding='utf-8')
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
