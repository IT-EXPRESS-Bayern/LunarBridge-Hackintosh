# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Check the interface comparison against the hash-pinned upstream sources.

Run tools/fetch_references.py first; these tests read the pinned Linux Xe uAPI
and the published Apple xnu / IOGraphics / IOPCIFamily headers from
tests/.references and verify the compiled Arc interface table against them.
No system, Apple binary or GPU is inspected.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = None


def load_tool():
    spec = importlib.util.spec_from_file_location('compare_interfaces',
                                                  ROOT / 'tools/compare_interfaces.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load_tool()


class LinuxExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.linux = TOOL.extract_linux()

    def test_all_twelve_pinned_ioctls_are_served_by_the_driver(self):
        self.assertEqual(self.linux['ioctl_count'], 12)
        self.assertEqual(self.linux['served_by_driver_table'], 12)
        self.assertEqual(self.linux['drm_command_base'], 0x40)
        # One permission model for every entry: a render node anyone with
        # access to /dev/dri/renderD* may call.
        self.assertEqual(self.linux['permission_model'], ['DRM_RENDER_ALLOW'])

    def test_ioctl_numbers_and_directions_match_the_header(self):
        ioctls = self.linux['ioctls']
        self.assertEqual(ioctls['DEVICE_QUERY']['ioctl_nr'], 0x40)
        self.assertEqual(ioctls['WAIT_USER_FENCE']['ioctl_nr'], 0x4A)
        self.assertEqual(ioctls['OBSERVATION']['ioctl_nr'], 0x4B)
        self.assertEqual(ioctls['VM_BIND']['direction_macro'], 'DRM_IOW')
        self.assertEqual(ioctls['VM_CREATE']['direction_macro'], 'DRM_IOWR')

    def test_constructs_are_derived_from_real_fields(self):
        ioctls = self.linux['ioctls']
        self.assertEqual(ioctls['VM_BIND']['construct_names'],
                         ['extension_chain', 'handle_id', 'sync_array', 'user_pointer',
                          'vector_of_ops'])
        self.assertIn('vector_of_binds', ioctls['VM_BIND']['fields'])
        self.assertEqual(ioctls['GEM_MMAP_OFFSET']['construct_names'],
                         ['extension_chain', 'handle_id', 'mmap_offset'])
        self.assertEqual(ioctls['WAIT_USER_FENCE']['construct_names'],
                         ['extension_chain', 'handle_id', 'infinite_wait',
                          'user_fence_address'])
        self.assertEqual(ioctls['VM_DESTROY']['construct_names'], ['handle_id'])

    def test_sync_and_bind_vocabularies_are_complete(self):
        self.assertEqual(self.linux['sync_types'],
                         {'SYNCOBJ': '0x0', 'TIMELINE_SYNCOBJ': '0x1', 'USER_FENCE': '0x2'})
        self.assertEqual(self.linux['bind_operations'],
                         {'MAP': '0x0', 'UNMAP': '0x1', 'MAP_USERPTR': '0x2',
                          'UNMAP_ALL': '0x3', 'PREFETCH': '0x4'})
        self.assertTrue(self.linux['bind_op_pat_index'])

    def test_missing_entities_are_errors_not_defaults(self):
        with self.assertRaises(TOOL.ExtractionError):
            TOOL.struct_body('struct drm_xe_vm_bind {\n};\n', 'drm_xe_absent')
        with self.assertRaises(TOOL.ExtractionError):
            TOOL.read_reference('linux-v6.17/include/uapi/drm/not_pinned.h')


class MacOSExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.macos = TOOL.extract_macos()

    def test_external_method_contract_is_fixed_and_checked(self):
        self.assertEqual(self.macos['dispatch_check_fields'],
                         ['checkScalarInputCount', 'checkStructureInputSize',
                          'checkScalarOutputCount', 'checkStructureOutputSize'])
        self.assertEqual(self.macos['scalar_input_count_max'], 16)
        self.assertEqual(self.macos['scalar_output_count_max'], 16)
        self.assertEqual(self.macos['variable_structure_size'], 0xFFFFFFFF)
        self.assertIn('structureInput', self.macos['external_method_arguments_fields'])
        self.assertTrue(self.macos['has_structure_input_descriptor'])
        self.assertTrue(self.macos['has_async_reference'])

    def test_tahoe_dispatch_adds_entitlement_and_async_gates(self):
        self.assertEqual(self.macos['dispatch_2022_extra_fields'],
                         ['allowAsync', 'checkEntitlement'])

    def test_public_accelerator_interface_is_a_surface_api(self):
        self.assertEqual(self.macos['accelerator_client_types'],
                         ['kIOAccelSurfaceClientType', 'kIOAccelNumClientTypes'])
        # 18 methods at indices 0..17; the 19th enum member is the terminator
        # kIOAccelNumSurfaceMethods, which is the count rather than a method.
        self.assertEqual(self.macos['accelerator_surface_method_count'], 18)
        self.assertEqual(self.macos['accelerator_surface_method_terminator'],
                         'kIOAccelNumSurfaceMethods')
        self.assertEqual(self.macos['highest_public_surface_selector'], 17)
        self.assertNotIn('kIOAccelNumSurfaceMethods',
                         self.macos['accelerator_surface_methods'])
        self.assertEqual(self.macos['accelerator_surface_methods'][0],
                         'kIOAccelSurfaceReadLockOptions')
        self.assertEqual(self.macos['accelerator_surface_methods'][-1],
                         'kIOAccelSurfaceSetShapeBackingAndLength')
        self.assertTrue(self.macos['dma_command_segment_api'])
        self.assertTrue(self.macos['pci_memory_mapping_api'])


class AppleDriverInventory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.apple = TOOL.extract_apple_driver_surface()

    def test_newest_apple_intel_driver_is_ice_lake(self):
        self.assertEqual(self.apple['kext_count'], 20)
        self.assertEqual(self.apple['newest_generation'], 'ICL (Gen11)')
        self.assertIn('com.apple.driver.AppleIntelICLGraphics', self.apple['bundle_identifiers'])
        self.assertIn('com.apple.driver.AppleIntelICLLPGraphicsFramebuffer',
                      self.apple['newest_framebuffer_bundles'])

    def test_no_apple_driver_exists_for_gen12_xe_or_xe2(self):
        self.assertFalse(self.apple['xe_or_xe2_driver_present'])


class DriverTableVerification(unittest.TestCase):
    def test_compiled_table_matches_the_pinned_sources(self):
        verification = TOOL.verify_driver_table(LIBRARY, TOOL.extract_linux(),
                                                TOOL.extract_macos())
        self.assertEqual(verification['mismatches'], [])
        self.assertEqual(verification['operation_count'], 11)
        self.assertEqual(verification['linux_uapi_version'], '6.17')
        self.assertEqual(verification['darwin_kernel_major'], 26)
        self.assertEqual(verification['unmapped_linux_ioctls'], ['OBSERVATION'])
        self.assertFalse(verification['gpu_execution_implemented'])
        self.assertFalse(verification['metal_provider_implemented'])

    def test_record_is_written_with_every_section(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'diff.json'
            argv = sys.argv
            sys.argv = ['compare_interfaces.py', '--library', str(LIBRARY), '--out', str(target)]
            try:
                self.assertEqual(TOOL.main(), 0)
            finally:
                sys.argv = argv
            record = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(sorted(record), ['apple_intel_driver_inventory', 'driver_table_verification',
                                          'generated_by', 'linux_arc_130v_driver',
                                          'macos_tahoe_driver_contract', 'operation_mapping',
                                          'scope'])
        self.assertEqual(len(record['driver_table_verification']['operations']), 11)

    def test_source_only_mapping_is_reproducible_without_a_library(self):
        # The committed record is generated without a library so it contains no
        # local path or build hash; the driver table is checked separately.
        mapping = TOOL.mapping_from_sources(TOOL.extract_linux())
        self.assertEqual(len(mapping['mapped']), 11)
        self.assertEqual(mapping['unmapped'], ['OBSERVATION'])
        committed = json.loads((ROOT / 'docs/ARC_INTERFACE_DIFF.json').read_text(encoding='utf-8'))
        self.assertNotIn('driver_table_verification', committed)
        self.assertEqual(committed['operation_mapping'], mapping)


def main():
    global LIBRARY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('library', type=Path,
                        help='Compiled library exporting lb_arc_interface_describe')
    arguments = parser.parse_args()
    LIBRARY = arguments.library.resolve(strict=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__('__main__'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
