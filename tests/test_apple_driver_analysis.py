# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Check the Apple driver interface mapper against synthetic bundles.

No Apple binary is used, downloaded or required: the fixtures here are built in
the test. What is checked is that the mapper reports only names, counts and
hashes, never bundle content, and that it degrades to metadata when the symbol
tool is absent — which is the case on every machine that is not the one holding
the driver.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    spec = importlib.util.spec_from_file_location('analyze_apple_driver',
                                                  ROOT / 'tools/analyze_apple_driver.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load_tool()

# Mangled names in the shape a graphics kext exports. Chosen to cover a nested
# class, a plain C symbol and the metaclass instances the IOKit runtime needs.
DEFINED = [
    '__ZN21AppleIntelICLGraphics5startEP9IOService',
    '__ZN21AppleIntelICLGraphics4stopEP9IOService',
    '__ZN21AppleIntelICLGraphics14newUserClientEP4taskPvjPP12IOUserClient',
    '__ZN21AppleIntelICLGraphics10gMetaClassE',
    '__ZN27AppleIntelICLGraphicsClient14externalMethodEjP29IOExternalMethodArgumentsP',
    '__ZN27AppleIntelICLGraphicsClient19clientMemoryForTypeEjPjPP18IOMemoryDescriptor',
    '__ZN27AppleIntelICLGraphicsClient10gMetaClassE',
    '_kmod_info',
]
UNDEFINED = [
    '__ZN11IOGPUDevice10gMetaClassE',
    '__ZN11IOGPUDevice5startEP9IOService',
    '__ZN14IOAccelerator213createAccelIDEjPi',
    '__ZN9IOService15registerServiceEj',
    '__ZN18IOMemoryDescriptor7prepareEj',
    '__ZN12IOPCIDevice118configRead32Ej',
    '_memcpy',
]


def write_bundle(directory: Path, *, with_binary: bool = True) -> Path:
    bundle = directory / 'AppleIntelICLGraphics.kext'
    (bundle / 'Contents/MacOS').mkdir(parents=True)
    info = {
        'CFBundleIdentifier': 'com.apple.driver.AppleIntelICLGraphics',
        'CFBundleExecutable': 'AppleIntelICLGraphics',
        'CFBundleVersion': '99.1',
        'CFBundleShortVersionString': '99.1',
        'OSBundleLibraries': {'com.apple.iokit.IOGPUFamily': '1.0',
                              'com.apple.iokit.IOPCIFamily': '2.0',
                              'com.apple.kpi.iokit': '8.0.0'},
        'IOKitPersonalities': {
            'AppleIntelICLGraphics': {
                'IOClass': 'AppleIntelICLGraphics',
                'IOProviderClass': 'IOPCIDevice',
                'IOMatchCategory': 'IOAccelerator',
                'IOUserClientClass': 'AppleIntelICLGraphicsClient',
                'IOPCIMatch': '0x8a520086',
                'ComplexData': {'nested': True},
            },
        },
    }
    (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    if with_binary:
        (bundle / 'Contents/MacOS/AppleIntelICLGraphics').write_bytes(b'not a real mach-o')
    return bundle


class NameParsing(unittest.TestCase):
    def test_nested_names_split_into_class_and_member(self):
        self.assertEqual(TOOL.symbol_class_and_member(DEFINED[0]),
                         ('AppleIntelICLGraphics', 'start'))
        self.assertEqual(TOOL.symbol_class_and_member(UNDEFINED[2]),
                         ('IOAccelerator2', 'createAccelID'))
        self.assertEqual(TOOL.symbol_class_and_member('_kmod_info'), ('', '_kmod_info'))

    def test_classes_and_metaclasses_are_separated(self):
        grouped = TOOL.group_symbols(DEFINED)
        self.assertEqual(grouped['class_count'], 2)
        self.assertIn('start', grouped['classes']['AppleIntelICLGraphics'])
        self.assertIn('_kmod_info', grouped['plain_symbols'])
        self.assertEqual(TOOL.metaclass_owners(DEFINED),
                         ['AppleIntelICLGraphics', 'AppleIntelICLGraphicsClient'])
        self.assertEqual(TOOL.metaclass_owners(UNDEFINED), ['IOGPUDevice'])

    def test_foreign_references_are_grouped_by_family(self):
        references = TOOL.family_references(UNDEFINED)
        families = references['by_family']
        self.assertEqual(families['IOGPU'], ['IOGPUDevice'])
        self.assertEqual(families['IOAccelerator'], ['IOAccelerator2'])
        self.assertIn('IOService', families)
        self.assertIn('IOMemoryDescriptor', families)
        self.assertEqual(references['other_classes'], [])


class BundleMapping(unittest.TestCase):
    def test_map_reports_metadata_without_a_symbol_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = write_bundle(Path(directory))
            record = TOOL.analyze(bundle, None)
        self.assertEqual(record['bundle']['identifier'],
                         'com.apple.driver.AppleIntelICLGraphics')
        self.assertEqual(record['library_dependencies']['com.apple.iokit.IOGPUFamily'], '1.0')
        personality = record['personalities']['AppleIntelICLGraphics']
        self.assertEqual(personality['IOProviderClass'], 'IOPCIDevice')
        self.assertEqual(personality['IOMatchCategory'], 'IOAccelerator')
        # A nested plist value is reduced to its type name, never carried over.
        self.assertEqual(personality['ComplexData'], 'dict')
        self.assertFalse(record['symbols']['available'])
        self.assertTrue(record['binary']['present'])
        self.assertEqual(len(record['binary']['sha256']), 64)

    def test_map_contains_no_bundle_content(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = write_bundle(Path(directory))
            record = TOOL.analyze(bundle, None)
            serialised = json.dumps(record)
        self.assertNotIn('not a real mach-o', serialised)
        self.assertIn('no binary content', record['scope'])

    def test_missing_binary_is_reported_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = write_bundle(Path(directory), with_binary=False)
            record = TOOL.analyze(bundle, None)
        self.assertFalse(record['binary']['present'])
        self.assertNotIn('symbols', record)

    def test_a_bundle_without_info_plist_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / 'Empty.kext'
            empty.mkdir()
            with self.assertRaises(SystemExit):
                TOOL.analyze(empty, None)

    def test_summary_lists_the_interface_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = write_bundle(Path(directory))
            record = TOOL.analyze(bundle, None)
            record['symbols'] = {
                'available': True, 'nm': '/usr/bin/nm',
                'defined': TOOL.group_symbols(DEFINED),
                'referenced': TOOL.family_references(UNDEFINED),
                'classes_defined_by_this_bundle': TOOL.metaclass_owners(DEFINED),
                'foreign_metaclasses_referenced': TOOL.metaclass_owners(UNDEFINED),
                'iokit_overrides': {'AppleIntelICLGraphics': ['newUserClient', 'start', 'stop']},
            }
            summary = TOOL.summarise(record)
        self.assertIn('com.apple.iokit.IOGPUFamily', summary)
        self.assertIn('IOClass=AppleIntelICLGraphics', summary)
        self.assertIn('newUserClient', summary)
        self.assertIn('IOGPU: IOGPUDevice', summary)
        self.assertIn('No instruction or data bytes', summary)


if __name__ == '__main__':
    unittest.main(verbosity=2)
