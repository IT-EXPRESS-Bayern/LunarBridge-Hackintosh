# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Check the graphics inventory walker against synthetic bundles.

No Apple bundle is used or required. What is checked is that both halves of the
stack are discovered, that a kernel driver is paired with the userspace driver
that drives it, that the Objective-C and IOKit import surfaces are read out of
symbol names, and that the record passes the policy gate.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import plistlib
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load('extract_macos_graphics', 'tools/extract_macos_graphics.py')
ANALYZER = load('analyze_apple_driver', 'tools/analyze_apple_driver.py')
POLICY = load('abi_policy', 'tools/abi_policy.py')

USERSPACE_DEFINED = [
    '_OBJC_CLASS_$_MTLIGAccelDevice',
    '_OBJC_METACLASS_$_MTLIGAccelDevice',
    '_OBJC_CLASS_$_MTLIGAccelCommandQueue',
    '_MTLCreateSystemDefaultDevice',
]
USERSPACE_UNDEFINED = [
    '_OBJC_CLASS_$_NSObject',
    '_IOServiceOpen',
    '_IOConnectCallStructMethod',
    '_IOConnectCallAsyncStructMethod',
    '_IOConnectMapMemory64',
    '_IOServiceGetMatchingService',
    '_malloc',
]


def write_kext(root: Path) -> Path:
    bundle = root / 'AppleIntelICLGraphics.kext'
    (bundle / 'Contents/MacOS').mkdir(parents=True)
    (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({
        'CFBundleIdentifier': 'com.apple.driver.AppleIntelICLGraphics',
        'CFBundleVersion': '16.0.6',
        'CFBundleExecutable': 'AppleIntelICLGraphics',
        'OSBundleLibraries': {'com.apple.iokit.IOGPUFamily': '1.0',
                              'com.apple.iokit.IOPCIFamily': '2.9',
                              'com.apple.iokit.IOSurface': '1.0'},
        'IOKitPersonalities': {'AppleIntelICLGraphics': {
            'IOClass': 'AppleIntelICLGraphics',
            'IOProviderClass': 'IOPCIDevice',
            'IOMatchCategory': 'IOFramebuffer',
            'IOUserClientClass': 'AppleIntelICLGraphicsClient',
            'IOPCIPrimaryMatch': '0x8a520086'}},
    }))
    (bundle / 'Contents/MacOS/AppleIntelICLGraphics').write_bytes(b'not a real binary')
    return bundle


def write_metal_driver(root: Path) -> Path:
    bundle = root / 'AppleIntelICLGraphicsMTLDriver.bundle'
    (bundle / 'Contents/MacOS').mkdir(parents=True)
    (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({
        'CFBundleIdentifier': 'com.apple.AppleIntelICLGraphicsMTLDriver',
        'CFBundleVersion': '16.0.6',
        'CFBundleExecutable': 'AppleIntelICLGraphicsMTLDriver',
    }))
    (bundle / 'Contents/MacOS/AppleIntelICLGraphicsMTLDriver').write_bytes(b'not a real binary')
    return bundle


class Discovers(unittest.TestCase):
    def test_both_halves_are_found_and_labelled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_kext(root)
            write_metal_driver(root)
            (root / 'AppleUSBAudio.kext').mkdir()
            entries = TOOL.discover([('kernel', str(root)), ('userspace', str(root))])
        names = {entry['path'].name: entry['half'] for entry in entries}
        self.assertEqual(names['AppleIntelICLGraphics.kext'], 'kernel')
        self.assertEqual(names['AppleIntelICLGraphicsMTLDriver.bundle'], 'kernel')
        self.assertNotIn('AppleUSBAudio.kext', names)

    def test_plugins_inside_a_bundle_are_discovered_as_userspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = write_kext(root)
            plugin = bundle / 'Contents/PlugIns/AppleIntelICLGraphicsMTLDriver.bundle'
            plugin.mkdir(parents=True)
            entries = TOOL.discover([('kernel', str(root))])
        plugins = [entry for entry in entries if entry.get('plugin_of')]
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0]['half'], 'userspace')
        self.assertEqual(plugins[0]['plugin_of'], 'AppleIntelICLGraphics.kext')

    def test_a_non_graphics_bundle_is_not_touched(self):
        self.assertFalse(TOOL.is_graphics(Path('/System/Library/Extensions/AppleHV.kext')))
        self.assertTrue(TOOL.is_graphics(Path('/System/Library/Extensions/IOGPUFamily.kext')))


class ReadsSymbolSurfaces(unittest.TestCase):
    def test_objective_c_classes_are_split_by_definition_and_reference(self):
        surface = TOOL.objc_surface(USERSPACE_DEFINED, USERSPACE_UNDEFINED)
        self.assertEqual(surface['classes_defined'],
                         ['MTLIGAccelCommandQueue', 'MTLIGAccelDevice'])
        self.assertEqual(surface['metaclasses_defined'], ['MTLIGAccelDevice'])
        self.assertEqual(surface['classes_referenced'], ['NSObject'])
        self.assertEqual(surface['class_count'], 2)

    def test_iokit_entry_points_are_reported_in_a_fixed_order(self):
        imports = TOOL.iokit_imports(USERSPACE_UNDEFINED)
        self.assertEqual(imports, ['IOServiceOpen', 'IOConnectCallStructMethod',
                                   'IOConnectCallAsyncStructMethod', 'IOConnectMapMemory64',
                                   'IOServiceGetMatchingService'])

    def test_a_bundle_calling_nothing_reports_nothing(self):
        self.assertEqual(TOOL.iokit_imports(['_malloc', '_free']), [])


class PairsHalves(unittest.TestCase):
    def records(self):
        return [
            {'half': 'kernel', 'name': 'AppleIntelICLGraphics.kext',
             'path': '/x/AppleIntelICLGraphics.kext',
             'bundle': {'identifier': 'com.apple.driver.AppleIntelICLGraphics'},
             'library_dependencies': {'com.apple.iokit.IOGPUFamily': '1.0'},
             'personalities': {'AppleIntelICLGraphics': {
                 'IOUserClientClass': 'AppleIntelICLGraphicsClient'}}},
            {'half': 'userspace', 'name': 'AppleIntelICLGraphicsMTLDriver.bundle',
             'path': '/x/AppleIntelICLGraphicsMTLDriver.bundle',
             'symbols': {'iokit_imports': ['IOServiceOpen', 'IOConnectCallStructMethod'],
                         'objc': {'class_count': 2}}},
            {'half': 'kernel', 'name': 'IOGPUFamily.kext', 'path': '/x/IOGPUFamily.kext',
             'bundle': {'identifier': 'com.apple.iokit.IOGPUFamily'},
             'library_dependencies': {}, 'personalities': {}},
        ]

    def test_a_driver_is_paired_with_its_metal_plugin(self):
        pairs = {pair['kernel_bundle']: pair for pair in TOOL.pair_halves(self.records())}
        pair = pairs['com.apple.driver.AppleIntelICLGraphics']
        self.assertTrue(pair['paired'])
        self.assertEqual(pair['user_client_classes'], ['AppleIntelICLGraphicsClient'])
        self.assertEqual(pair['linked_families'], ['com.apple.iokit.IOGPUFamily'])
        self.assertEqual(pair['userspace_partners'][0]['iokit_imports'],
                         ['IOServiceOpen', 'IOConnectCallStructMethod'])

    def test_a_family_without_a_userspace_half_is_reported_unpaired(self):
        pairs = {pair['kernel_bundle']: pair for pair in TOOL.pair_halves(self.records())}
        self.assertFalse(pairs['com.apple.iokit.IOGPUFamily']['paired'])


class ProducesACleanRecord(unittest.TestCase):
    def test_mapping_a_bundle_without_a_symbol_tool_degrades(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = write_kext(Path(directory))
            record = TOOL.map_bundle({'half': 'kernel', 'path': bundle, 'kind': 'kext'},
                                     ANALYZER, None)
        self.assertTrue(record['mapped'])
        self.assertFalse(record['symbols']['available'])
        self.assertIn('nm', record['symbols']['reason'])

    def test_the_record_carries_identity_but_no_content(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = write_kext(Path(directory))
            record = TOOL.map_bundle({'half': 'kernel', 'path': bundle, 'kind': 'kext'},
                                     ANALYZER, None)
        self.assertEqual(record['bundle']['identifier'],
                         'com.apple.driver.AppleIntelICLGraphics')
        self.assertEqual(len(record['binary']['sha256']), 64)
        self.assertEqual(POLICY.check_record(record), [])
        self.assertNotIn('not a real binary', repr(record))

    def test_a_bundle_without_an_info_plist_is_reported_not_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / 'AppleGraphicsNothing.kext'
            empty.mkdir()
            record = TOOL.map_bundle({'half': 'kernel', 'path': empty, 'kind': 'kext'},
                                     ANALYZER, None)
        self.assertFalse(record['mapped'])
        self.assertIn('Info.plist', record['reason'])

    def test_the_summary_counts_what_was_actually_mapped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_kext(root)
            write_metal_driver(root)
            entries = TOOL.discover([('kernel', str(root))])
            bundles = [TOOL.map_bundle(entry, ANALYZER, None) for entry in entries]
        record = {'bundles': bundles}
        record['pairs'] = TOOL.pair_halves(bundles)
        summary = TOOL.summarise(record)
        self.assertEqual(summary['bundles_found'], 2)
        self.assertEqual(summary['bundles_mapped'], 2)
        self.assertEqual(summary['symbols_available'], 0)
        self.assertIn('com.apple.iokit.IOGPUFamily', summary['linked_families'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
