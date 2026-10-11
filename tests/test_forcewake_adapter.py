# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Run production LBForceWakeAdapter.cpp with explicit host doubles; no hardware.

Build integration supplies LB_FORCEWAKE_HOST_TEST and the C-exported fixture.
Use --library for a separately built own host library; missing builds fail.
"""
from __future__ import annotations
import argparse
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIELDS = (
    'synthetic', 'scenario', 'samplePrimary', 'sampleCleanup', 'samplePolicy',
    'sampleCompleted', 'sampleAttempted', 'sampleResourcesRetained',
    'sampleReads', 'sampleFlushWrites', 'sampleRequestWrites', 'sampleLeaseReferences',
    'snapshotFlags', 'snapshotCapabilities', 'gmdRawGraphics', 'gmdRawMedia',
    'graphicsIP', 'mediaIP', 'mapReleasesAfterSample', 'closeCallsAfterSample',
    'pciReleasesAfterSample', 'pciRetainsAfterSample', 'heldAfterSample',
    'firstReset', 'heldAfterFirstReset', 'secondReset', 'finalDrained',
    'finalResourcesRetained', 'finalReads', 'finalFlushWrites', 'finalRequestWrites',
    'finalLeaseReferences', 'gpuExecuted', 'metalVerified', 'badSequence',
    'badRegister', 'rootFlushErrors', 'mapCalls', 'mapReleases',
    'openCalls', 'openSuccesses', 'closeCalls', 'pciRetains', 'pciReleases',
    'requestAttempts', 'releaseAttempts', 'successfulControlWrites',
    'gmdGraphicsReads', 'gmdMediaReads', 'controlReads', 'ackReads', 'sleeps',
    'pmCapabilityCalls', 'lastMapReleaseOrder', 'lastCloseOrder', 'lastPCIReleaseOrder',
    'finalControl', 'finalAck', 'failedRequestSideEffects', 'retainedDuringRetry',
    'flushWriteAttempts', 'flushWriteSuccesses', 'flushWriteFailures', 'finalCompleted')


class Report(c.Structure):
    _fields_ = [(key, c.c_uint32) for key in FIELDS]


class ForceWakeAdapterTests(unittest.TestCase):
    reports = {}
    library_path = None

    @classmethod
    def setUpClass(cls):
        assert c.sizeof(Report) == 256
        supplied = cls.library_path or os.environ.get('LUNARBRIDGE_FORCEWAKE_TEST_LIBRARY')
        library = Path(supplied) if supplied else ROOT / 'build' / 'host' / 'lnl_host.dll'
        if not library.is_file():
            raise RuntimeError('Own host library is missing; build production adapter with LB_FORCEWAKE_HOST_TEST.')
        cls.library_sha256 = hashlib.sha256(library.read_bytes()).hexdigest()
        cls.dll = c.CDLL(str(library.resolve()))
        cls.dll.lb_forcewake_adapter_test_run.argtypes = [c.c_uint32, c.POINTER(Report)]
        cls.dll.lb_forcewake_adapter_test_run.restype = c.c_int

    def run_adapter(self, scenario=0):
        result = Report()
        self.assertEqual(self.dll.lb_forcewake_adapter_test_run(scenario, c.byref(result)), 1)
        self.reports[scenario] = {field: getattr(result, field) for field in FIELDS}
        self.assertEqual((result.synthetic, result.scenario, result.badSequence,
                          result.badRegister, result.rootFlushErrors), (1, scenario, 0, 0, 0))
        self.assertEqual((result.finalDrained, result.finalResourcesRetained,
                          result.finalLeaseReferences, result.gpuExecuted, result.metalVerified), (1, 0, 0, 0, 0))
        self.assertEqual(result.pciRetains, result.pciReleases)
        self.assertEqual(result.openSuccesses, result.closeCalls)
        self.assertLessEqual(result.openCalls, 1)
        self.assertLessEqual(result.mapCalls, 1)
        self.assertEqual(result.finalReads, result.gmdGraphicsReads + result.gmdMediaReads
                         + result.controlReads + result.ackReads)
        self.assertEqual(result.finalFlushWrites, result.flushWriteSuccesses)
        self.assertEqual(result.finalRequestWrites, result.successfulControlWrites)
        if not result.flushWriteFailures:
            self.assertEqual(result.finalFlushWrites, 4 * result.finalReads)
        self.assertEqual(result.snapshotCapabilities & ((1 << 16) | (1 << 17)), 0)
        if result.mapReleases:
            self.assertEqual(result.mapReleases, 1)
            self.assertLess(result.lastMapReleaseOrder, result.lastCloseOrder)
        if result.closeCalls:
            self.assertLess(result.lastCloseOrder, result.lastPCIReleaseOrder)
        self.assertIn(result.secondReset, (0, 8))
        return result

    def blocked_preflight(self, scenario, policy):
        result = self.run_adapter(scenario)
        self.assertEqual(result.samplePolicy, policy)
        self.assertEqual((result.gmdGraphicsReads, result.gmdMediaReads,
                          result.requestAttempts, result.releaseAttempts, result.heldAfterSample), (0, 0, 0, 0, 0))
        return result

    def test_masked_get_put_and_every_read_has_four_root_flush_writes(self):
        r = self.run_adapter()
        self.assertEqual((r.samplePrimary, r.sampleCleanup, r.sampleCompleted, r.sampleAttempted), (0, 0, 1, 1))
        self.assertEqual((r.requestAttempts, r.releaseAttempts, r.successfulControlWrites), (1, 1, 2))
        self.assertEqual((r.gmdGraphicsReads, r.gmdMediaReads, r.controlReads, r.ackReads), (1, 1, 1, 3))
        self.assertEqual((r.sampleReads, r.sampleFlushWrites), (6, 24))
        self.assertEqual((r.finalControl, r.finalAck), (0, 0))

    def test_default_wake_off_reads_gmd_only(self):
        r = self.run_adapter(1)
        self.assertEqual((r.samplePrimary, r.sampleCleanup, r.sampleCompleted, r.sampleAttempted), (8, 8, 0, 0))
        self.assertEqual((r.gmdGraphicsReads, r.gmdMediaReads, r.controlReads, r.ackReads), (1, 1, 0, 0))
        self.assertEqual((r.requestAttempts, r.releaseAttempts, r.sampleReads, r.sampleFlushWrites), (0, 0, 2, 8))
        self.assertEqual((r.graphicsIP, r.mediaIP, r.snapshotFlags & 16, r.snapshotCapabilities & 8), (2004, 2000, 16, 8))

    def test_wrong_vendor_and_wrong_device_rejected(self):
        for scenario in (2, 3):
            with self.subTest(scenario=scenario): self.blocked_preflight(scenario, 2)

    def test_wrong_subsystem_rejected(self):
        for scenario in (4, 5):
            with self.subTest(scenario=scenario): self.blocked_preflight(scenario, 3)

    def test_wrong_class_rejected(self): self.blocked_preflight(6, 4)
    def test_memory_decode_off_rejected(self): self.blocked_preflight(7, 5)
    def test_not_d0_rejected(self): self.blocked_preflight(8, 7)
    def test_provider_not_on_rejected(self): self.blocked_preflight(9, 8)

    def test_missing_or_out_of_range_pm_capability_does_not_open_provider(self):
        for scenario in (10, 11):
            with self.subTest(scenario=scenario):
                r = self.blocked_preflight(scenario, 6)
                self.assertEqual((r.openCalls, r.mapCalls, r.pciRetains), (0, 0, 0))

    def test_busy_open_never_retains_or_maps(self):
        r = self.blocked_preflight(12, 10)
        self.assertEqual((r.openCalls, r.openSuccesses, r.mapCalls, r.pciRetains), (1, 0, 0, 0))

    def test_mapping_failure_closes_and_releases_pci_in_order(self):
        r = self.blocked_preflight(13, 9)
        self.assertEqual((r.mapCalls, r.mapReleases, r.closeCalls, r.pciRetains, r.pciReleases), (1, 0, 1, 1, 1))

    def test_zero_virtual_base_releases_returned_map_without_mmio(self):
        r = self.blocked_preflight(14, 9)
        self.assertEqual((r.mapCalls, r.mapReleases, r.sampleReads, r.sampleFlushWrites), (1, 1, 0, 0))

    def test_media_bar_boundary_requires_all_four_bytes(self):
        r = self.blocked_preflight(15, 9)
        self.assertEqual((r.mapReleases, r.sampleReads, r.sampleFlushWrites), (1, 0, 0))

    def test_invalid_graphics_gmd_cannot_wake(self):
        r = self.run_adapter(16)
        self.assertEqual((r.samplePrimary, r.sampleAttempted, r.requestAttempts, r.snapshotCapabilities & 8), (9, 1, 0, 0))

    def test_all_ones_graphics_and_media_mmio_cannot_publish_gmd_sample(self):
        for scenario in (17, 19):
            with self.subTest(scenario=scenario):
                r = self.run_adapter(scenario)
                self.assertEqual((r.samplePrimary, r.requestAttempts, r.snapshotFlags & 16,
                                  r.snapshotCapabilities & 8), (5, 0, 16, 0))

    def test_invalid_nonzero_media_gmd_cannot_wake(self):
        r = self.run_adapter(18)
        self.assertEqual((r.samplePrimary, r.requestAttempts, r.snapshotCapabilities & 8), (9, 0, 0))
        self.assertEqual(r.gmdMediaReads, 1)

    def test_actually_read_zero_media_is_accepted_and_distinguished_from_no_read(self):
        r = self.run_adapter(20)
        self.assertEqual((r.samplePrimary, r.sampleCompleted, r.gmdMediaReads,
                          r.gmdRawMedia, r.mediaIP, r.snapshotFlags & 16, r.snapshotCapabilities & 8), (0, 1, 1, 0, 0, 16, 8))

    def test_decoded_unsupported_graphics_and_media_ip_cannot_wake(self):
        for scenario in (21, 45):
            with self.subTest(scenario=scenario):
                r = self.run_adapter(scenario)
                self.assertEqual((r.samplePrimary, r.requestAttempts, r.snapshotCapabilities & 8), (9, 0, 8))

    def test_existing_control_or_ack_request_is_not_stolen_or_released(self):
        for scenario in (22, 23):
            with self.subTest(scenario=scenario):
                r = self.run_adapter(scenario)
                self.assertEqual((r.samplePrimary, r.sampleCleanup, r.requestAttempts, r.releaseAttempts), (3, 8, 0, 0))
                self.assertEqual(r.finalAck, 1)
                self.assertEqual(r.finalControl, 1 if scenario == 22 else 0)

    def test_delayed_ack_get_and_put_poll_without_extra_register_writes(self):
        r = self.run_adapter(24)
        self.assertEqual((r.samplePrimary, r.sampleCleanup, r.sampleCompleted, r.sleeps), (0, 0, 1, 5))
        self.assertEqual((r.requestAttempts, r.releaseAttempts), (1, 1))
        self.assertGreater(r.ackReads, 3)

    def test_all_ones_ack_after_request_is_cleaned_up(self):
        r = self.run_adapter(25)
        self.assertEqual((r.samplePrimary, r.sampleCleanup, r.sampleCompleted, r.releaseAttempts), (5, 0, 0, 1))
        self.assertEqual((r.finalControl, r.finalAck, r.heldAfterSample), (0, 0, 0))

    def test_partial_failed_request_still_releases_side_effect_and_ownership(self):
        r = self.run_adapter(26)
        self.assertEqual((r.samplePrimary, r.sampleCleanup, r.sampleCompleted), (4, 0, 0))
        self.assertEqual((r.requestAttempts, r.failedRequestSideEffects, r.releaseAttempts,
                          r.sampleRequestWrites, r.finalControl, r.finalAck), (1, 1, 1, 1, 0, 0))

    def retained_until_retry(self, scenario, status):
        r = self.run_adapter(scenario)
        self.assertEqual((r.sampleCleanup, r.sampleCompleted, r.sampleResourcesRetained,
                          r.heldAfterSample, r.heldAfterFirstReset, r.retainedDuringRetry), (status, 0, 1, 1, 1, 1))
        self.assertEqual((r.mapReleasesAfterSample, r.closeCallsAfterSample, r.pciReleasesAfterSample), (0, 0, 0))
        self.assertEqual((r.pciRetainsAfterSample, r.firstReset, r.secondReset), (1, status, 0))
        self.assertEqual((r.mapReleases, r.closeCalls, r.pciReleases, r.finalControl & 1, r.finalAck & 1), (1, 1, 1, 0, 0))
        return r

    def test_release_write_failure_retains_map_open_and_pci_until_retry(self):
        r = self.retained_until_retry(27, 4)
        self.assertEqual((r.samplePrimary, r.releaseAttempts, r.finalCompleted), (0, 3, 1))

    def test_release_ack_timeout_retains_resources_until_zero_ack_retry(self):
        r = self.retained_until_retry(28, 6)
        self.assertEqual((r.samplePrimary, r.releaseAttempts, r.finalCompleted), (0, 3, 1))
        self.assertGreaterEqual(r.sleeps, 100)

    def test_fresh_d0_preflight_after_mapping_rejects_changed_pmcsr(self):
        self.blocked_preflight(29, 7)

    def test_decode_guard_rechecks_between_gmd_reads(self):
        r = self.run_adapter(30)
        self.assertEqual((r.samplePrimary, r.gmdGraphicsReads, r.gmdMediaReads,
                          r.sampleReads, r.sampleFlushWrites, r.requestAttempts), (4, 1, 0, 1, 4, 0))
        self.assertEqual(r.snapshotFlags & 16, 0)

    def test_d0_guard_failure_after_request_retains_until_restored_retry(self):
        r = self.retained_until_retry(31, 4)
        self.assertEqual((r.samplePrimary, r.requestAttempts, r.releaseAttempts, r.finalCompleted), (4, 1, 1, 0))

    def test_pci_guard_rechecks_between_gmd_reads(self):
        r = self.run_adapter(32)
        self.assertEqual((r.samplePrimary, r.gmdGraphicsReads, r.gmdMediaReads, r.requestAttempts), (4, 1, 0, 0))

    def test_pci_guard_failure_after_request_retains_until_restored_retry(self):
        r = self.retained_until_retry(33, 4)
        self.assertEqual((r.samplePrimary, r.requestAttempts, r.releaseAttempts), (4, 1, 1))

    def test_all_ones_initial_control_or_ack_never_claims_request(self):
        for scenario in (34, 35):
            with self.subTest(scenario=scenario):
                r = self.run_adapter(scenario)
                self.assertEqual((r.samplePrimary, r.requestAttempts, r.releaseAttempts), (5, 0, 0))

    def test_shrinking_bar_guard_rejects_second_gmd_read(self):
        r = self.run_adapter(36)
        self.assertEqual((r.samplePrimary, r.gmdGraphicsReads, r.gmdMediaReads), (4, 1, 0))

    def test_null_provider_owner_or_snapshot_never_opens_or_maps(self):
        for scenario in (37, 38, 39):
            with self.subTest(scenario=scenario):
                r = self.run_adapter(scenario)
                self.assertEqual((r.openCalls, r.mapCalls, r.pciRetains, r.finalReads), (0, 0, 0, 0))

    def test_snapshot_opt_in_missing_never_opens_or_maps(self):
        r = self.blocked_preflight(40, 1)
        self.assertEqual((r.openCalls, r.mapCalls, r.pmCapabilityCalls), (0, 0, 0))

    def test_provider_power_guard_rechecks_between_gmd_reads(self):
        r = self.run_adapter(41)
        self.assertEqual((r.samplePrimary, r.gmdGraphicsReads, r.gmdMediaReads), (4, 1, 0))

    def test_clock_rollback_after_request_still_releases_owned_request(self):
        r = self.run_adapter(42)
        self.assertEqual((r.samplePrimary, r.sampleCleanup, r.releaseAttempts), (7, 0, 1))

    def test_request_ack_timeout_is_bounded_and_cleaned_up(self):
        r = self.run_adapter(43)
        self.assertEqual((r.samplePrimary, r.sampleCleanup, r.sampleCompleted), (6, 0, 0))
        self.assertGreaterEqual(r.sleeps, 50)
        self.assertLessEqual(r.ackReads, 66)

    def test_all_ones_release_ack_retains_resources_until_valid_retry(self):
        r = self.retained_until_retry(44, 5)
        self.assertEqual((r.samplePrimary, r.finalCompleted), (0, 1))

    def test_unknown_pmcsr_and_command_all_ones_rejected(self):
        self.blocked_preflight(46, 6)
        self.blocked_preflight(47, 11)

    def test_alternative_supported_display_class_is_accepted(self):
        r = self.run_adapter(48)
        self.assertEqual((r.samplePrimary, r.sampleCleanup, r.sampleCompleted), (0, 0, 1))

    def test_failed_second_root_flush_is_never_published_as_a_read(self):
        r = self.run_adapter(49)
        self.assertEqual((r.samplePrimary, r.sampleReads, r.sampleFlushWrites,
                          r.flushWriteAttempts, r.flushWriteSuccesses, r.flushWriteFailures), (4, 0, 1, 2, 1, 1))
        self.assertEqual((r.gmdGraphicsReads, r.gmdMediaReads, r.requestAttempts), (0, 0, 0))

    def test_failed_request_without_effect_still_runs_mandatory_cleanup(self):
        r = self.run_adapter(50)
        self.assertEqual((r.samplePrimary, r.sampleCleanup, r.requestAttempts,
                          r.releaseAttempts, r.failedRequestSideEffects), (4, 0, 1, 1, 0))

    def test_masked_request_preserves_other_requestor_bits(self):
        r = self.run_adapter(51)
        self.assertEqual((r.samplePrimary, r.sampleCleanup, r.sampleCompleted), (0, 0, 1))
        self.assertEqual((r.finalControl, r.finalAck), (0x400000, 0x400000))

    def test_null_output_and_unknown_scenario_fail_without_executing_adapter(self):
        self.assertEqual(self.dll.lb_forcewake_adapter_test_run(0, None), 0)
        self.assertEqual(self.dll.lb_forcewake_adapter_test_run(52, c.byref(Report())), 0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, help='Own host-test library with production adapter and doubles.')
    parser.add_argument('--report', type=Path, help='Optional path for address-free synthetic JSON.')
    args = parser.parse_args()
    ForceWakeAdapterTests.library_path = args.library
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ForceWakeAdapterTests))
    report = {'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': len(result.skipped), 'successful': result.wasSuccessful(), 'synthetic': 1,
              'io_kit_calls_simulated': True, 'actual_production_adapter_cpp_executed': result.testsRun > 0,
              'native_forcewake_hardware_tested': False, 'cpu_or_gpu_dispatch_tested': False,
              'metal_acceleration_tested': False,
              'host_library_sha256': getattr(ForceWakeAdapterTests, 'library_sha256', None),
              'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                for name in ('native/LBForceWakeAdapter.cpp', 'native/LBForceWakeAdapter.h',
                                             'gpu-prototype/lb_forcewake.cpp', 'tests/forcewake_adapter_fake.cpp',
                                             'tests/forcewake_adapter_fake.h', 'tests/test_forcewake_adapter.py')},
              'scenario_reports': list(ForceWakeAdapterTests.reports.values())}
    serialized = json.dumps(report, indent=2) + '\n'
    if args.report:
        args.report.write_text(serialized, encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'scenario_reports'}, indent=2))
    raise SystemExit(0 if result.wasSuccessful() else 1)
