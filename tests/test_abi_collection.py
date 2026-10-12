# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Check the collection reader against a synthetic kernel collection.

The collection is built by the test from real Mach-O images, and nm is replaced
by a script that prints the layout Apple's nm prints for a fileset. That makes
both halves checkable here: the fileset parsing against known entries, and the
symbol filtering against known output — including the case where nm does not
walk filesets at all, which has to be reported rather than assumed away.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))

import macho_builder as builder  # noqa: E402


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COLLECTION = load('abi_collection', 'tools/abi_collection.py')
MACHO = load('abi_macho', 'tools/abi_macho.py')
POLICY = load('abi_policy', 'tools/abi_policy.py')

IDENTIFIERS = ('com.apple.driver.AppleIntelICLGraphics',
               'com.apple.iokit.IOGPUFamily',
               'com.apple.driver.AppleUSBAudio')

NM_FILESET_OUTPUT = '''
Symbols for com.apple.driver.AppleIntelICLGraphics:
ffffff8000300000 T __ZN21AppleIntelICLGraphics5startEP9IOService
ffffff8000300040 T __ZN21AppleIntelICLGraphics14newUserClientEP4taskPvjPP12IOUserClient
ffffff8000300080 S __ZN21AppleIntelICLGraphics10gMetaClassE

Symbols for com.apple.iokit.IOGPUFamily:
ffffff8000400000 T __ZN11IOGPUDevice5startEP9IOService

Symbols for com.apple.driver.AppleUSBAudio:
ffffff8000500000 T __ZN13AppleUSBAudio5startEP9IOService
'''

NM_UNDEFINED_OUTPUT = '''
Symbols for com.apple.driver.AppleIntelICLGraphics:
                 U __ZN11IOGPUDevice10gMetaClassE
                 U __ZN16IOUserClient202222dispatchExternalMethodEjP33IOExternalMethodArgumentsOpaque
                 U _memcpy

Symbols for com.apple.iokit.IOGPUFamily:
                 U __ZN9IOService15registerServiceEj
'''

NM_WITHOUT_FILESET_SUPPORT = '''
ffffff8000300000 T __ZN21AppleIntelICLGraphics5startEP9IOService
'''


def fake_nm(directory: Path, defined: str, undefined: str) -> str:
    """A stand-in for nm that prints the given output for the matching flags."""
    script = directory / 'fake-nm'
    script.write_text(
        '#!/bin/sh\n'
        'case "$*" in\n'
        '  *-u*) cat <<\'UNDEFINED\'\n' + undefined + 'UNDEFINED\n'
        '  ;;\n'
        '  *) cat <<\'DEFINED\'\n' + defined + 'DEFINED\n'
        '  ;;\n'
        'esac\n', encoding='utf-8')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def build_collection(directory: Path) -> Path:
    images = []
    for identifier in IDENTIFIERS:
        image = builder.ImageBuilder()
        image.section('__TEXT', '__text', b'\x90' * 32, builder.TEXT_FLAGS)
        image.section('__DATA_CONST', '__const', bytes(64))
        data, _ = image.build()
        images.append((identifier, data))
    path = directory / 'BootKernelExtensions.kc'
    path.write_bytes(builder.fileset(images))
    return path


class ReadsTheFileset(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = build_collection(Path(self.directory.name))

    def tearDown(self):
        self.directory.cleanup()

    def test_every_bundle_is_listed_with_its_address(self):
        entries = COLLECTION.bundles(self.path)
        self.assertEqual([entry['identifier'] for entry in entries], sorted(IDENTIFIERS))
        for entry in entries:
            self.assertGreater(entry['vmaddr'], 0)
            self.assertGreater(entry['fileoff'], 0)
            self.assertTrue(entry['vmaddr_hex'].startswith('0x'))

    def test_graphics_bundles_are_separated_from_the_rest(self):
        record = COLLECTION.describe(self.path, None, nm=None)
        names = [entry['identifier'] for entry in record['graphics_bundles']]
        self.assertIn('com.apple.driver.AppleIntelICLGraphics', names)
        self.assertIn('com.apple.iokit.IOGPUFamily', names)
        self.assertNotIn('com.apple.driver.AppleUSBAudio', names)

    def test_a_plain_binary_is_refused_as_a_collection(self):
        image = builder.ImageBuilder()
        image.section('__TEXT', '__text', b'\x90' * 16, builder.TEXT_FLAGS)
        data, _ = image.build()
        single = Path(self.directory.name) / 'single'
        single.write_bytes(data)
        with self.assertRaises(COLLECTION.CollectionError):
            COLLECTION.bundles(single)

    def test_the_collection_is_hashed_without_being_read_whole(self):
        import hashlib
        expected = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(MACHO.digest(self.path), expected)

    def test_the_file_is_mapped_rather_than_buffered(self):
        data, handle = MACHO.map_file(self.path)
        try:
            self.assertEqual(len(data), self.path.stat().st_size)
            image = MACHO.open_image(data, 'mapped')
            self.assertEqual(image.describe()['filetype_name'], 'MH_FILESET')
        finally:
            if handle is not None:
                data.close()
                handle.close()


class ReadsSymbolsThroughNm(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = build_collection(self.root)
        self.nm = fake_nm(self.root, NM_FILESET_OUTPUT, NM_UNDEFINED_OUTPUT)

    def tearDown(self):
        self.directory.cleanup()

    def test_fileset_support_is_probed_not_assumed(self):
        support = COLLECTION.nm_understands_filesets(self.path, self.nm)
        self.assertTrue(support['supported'])
        self.assertGreaterEqual(support['headers_seen'], 2)

    def test_a_tool_without_fileset_support_is_reported(self):
        blind = fake_nm(self.root, NM_WITHOUT_FILESET_SUPPORT, '')
        support = COLLECTION.nm_understands_filesets(self.path, blind)
        self.assertFalse(support['supported'])
        self.assertIn('fileset', support['reason'])

    def test_only_the_requested_bundle_is_collected(self):
        found = COLLECTION.symbols(self.path, 'com.apple.driver.AppleIntelICLGraphics',
                                   self.nm)
        self.assertEqual(len(found['defined']), 3)
        self.assertTrue(all('AppleIntelICLGraphics' in name for name in found['defined']))
        self.assertNotIn('__ZN11IOGPUDevice5startEP9IOService', found['defined'])
        self.assertNotIn('__ZN13AppleUSBAudio5startEP9IOService', found['defined'])

    def test_imported_symbols_are_read_separately(self):
        found = COLLECTION.symbols(self.path, 'com.apple.driver.AppleIntelICLGraphics',
                                   self.nm)
        self.assertEqual(len(found['undefined']), 3)
        self.assertTrue(any('dispatchExternalMethod' in name
                            for name in found['undefined']),
                        'the decisive dispatch-path import must survive')

    def test_a_second_bundle_is_filtered_independently(self):
        found = COLLECTION.symbols(self.path, 'com.apple.iokit.IOGPUFamily', self.nm)
        self.assertEqual(found['defined'], ['__ZN11IOGPUDevice5startEP9IOService'])
        self.assertEqual(found['undefined'], ['__ZN9IOService15registerServiceEj'])

    def test_an_unknown_identifier_yields_nothing_and_says_why(self):
        found = COLLECTION.symbols(self.path, 'com.apple.driver.Absent', self.nm)
        self.assertEqual(found['defined'], [])
        self.assertEqual(found['undefined'], [])
        self.assertIn('identifier', found['reason'])

    def test_the_whole_record_passes_the_policy_gate(self):
        record = COLLECTION.describe(self.path,
                                     'com.apple.driver.AppleIntelICLGraphics', self.nm)
        self.assertEqual(POLICY.check_record(record), [])
        self.assertEqual(len(record['collection']['sha256']), 64)
        self.assertEqual(record['symbols']['identifier'],
                         'com.apple.driver.AppleIntelICLGraphics')

    def test_symbols_are_skipped_when_the_tool_cannot_walk_filesets(self):
        blind = fake_nm(self.root, NM_WITHOUT_FILESET_SUPPORT, '')
        record = COLLECTION.describe(self.path,
                                     'com.apple.driver.AppleIntelICLGraphics', blind)
        self.assertFalse(record['symbols']['performed'])
        self.assertIn('abi_dispatch', record['symbols']['reason'])

    def test_a_byte_dumping_nm_invocation_would_be_refused(self):
        with self.assertRaises(POLICY.PolicyError):
            POLICY.assert_tool_invocation(['hexdump', str(self.path)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
