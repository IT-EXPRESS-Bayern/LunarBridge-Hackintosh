# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Prove the dispatch-table recovery against planted tables in real Mach-O images.

Every image here is built by the test, so the expected answer is known exactly:
which selectors exist, which argument shapes they check, which are entitlement
gated and which handler each one points at. A vendor binary could not establish
that — only that the tool produced something plausible.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
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


MACHO = load('abi_macho', 'tools/abi_macho.py')
DISPATCH = load('abi_dispatch', 'tools/abi_dispatch.py')
CONTRACT = load('abi_contract', 'tools/abi_contract.py')
COMPARE = load('compare_interfaces', 'tools/compare_interfaces.py')
POLICY = load('abi_policy', 'tools/abi_policy.py')
SENTINEL = 0xFFFFFFFF


def build_driver(*, style: str = '2022', with_symbols: bool = True) -> tuple[bytes, dict]:
    """A kext image whose user client has a four-selector dispatch table."""
    image = builder.ImageBuilder(uuid=bytes(range(16)))
    # Four handlers' worth of code, 16 bytes apart, plus one entitlement string.
    image.section('__TEXT', '__text', b'\x90' * 64, builder.TEXT_FLAGS)
    image.section('__TEXT', '__cstring',
                  b'com.apple.private.gpu.submit\0com.apple.private.gpu.config\0',
                  builder.S_CSTRING_LITERALS)
    text, _ = image.build()
    # Rebuild with the real addresses now known: a second pass is simpler than
    # predicting the layout, and this is exactly how a linker behaves.
    probe = MACHO.open_image(text, 'probe')
    text_section = probe.section('__TEXT', '__text')
    cstring = probe.section('__TEXT', '__cstring')
    handlers = [text_section['address'] + index * 16 for index in range(4)]
    submit = cstring['address']
    config = cstring['address'] + len('com.apple.private.gpu.submit') + 1

    final = builder.ImageBuilder(uuid=bytes(range(16)))
    final.section('__TEXT', '__text', b'\x90' * 64, builder.TEXT_FLAGS)
    final.section('__TEXT', '__cstring',
                  b'com.apple.private.gpu.submit\0com.apple.private.gpu.config\0',
                  builder.S_CSTRING_LITERALS)
    if style == '2022':
        table = (
            builder.dispatch_entry_2022(handlers[0], 2, 0, 0, 48)
            + builder.dispatch_entry_2022(handlers[1], 0, 480, 0, 80, allow_async=True)
            + builder.dispatch_entry_2022(handlers[2], 0, SENTINEL, 1, SENTINEL,
                                          entitlement=submit)
            + builder.dispatch_entry_2022(0, 1, 0, 0, 0, entitlement=config)
        )
    else:
        table = (
            builder.dispatch_entry_legacy(handlers[0], 2, 0, 0, 48)
            + builder.dispatch_entry_legacy(handlers[1], 0, 480, 0, 80)
            + builder.dispatch_entry_legacy(handlers[2], 0, SENTINEL, 1, SENTINEL)
        )
    # Unrelated read-only data on both sides, so the scanner has to find the run.
    final.section('__DATA_CONST', '__const',
                  struct.pack('<4Q', 0x1122334455667788, 0, 0xDEADBEEF, 7) + table
                  + struct.pack('<2Q', 0x99, 0xAA))
    expected = {'handlers': handlers, 'entitlements': {'submit': submit, 'config': config}}
    if with_symbols:
        final.symbol('__ZN21LunarBridgeUserClient6submitEP29IOExternalMethodArguments',
                     handlers[0])
        final.symbol('__ZN21LunarBridgeUserClient9configureEP29IOExternalMethodArguments',
                     handlers[1])
        final.symbol('__ZN21LunarBridgeUserClient5queryEP29IOExternalMethodArguments',
                     handlers[2])
    data, addresses = final.build()
    expected['table_address'] = addresses['__DATA_CONST,__const'] + 32
    return data, expected


class ReadsImages(unittest.TestCase):
    def test_a_kext_image_describes_itself(self):
        data, _ = build_driver()
        image = MACHO.open_image(data, 'synthetic')
        described = image.describe()
        self.assertEqual(described['architecture'], 'x86_64')
        self.assertEqual(described['filetype_name'], 'MH_KEXT_BUNDLE')
        self.assertEqual(described['uuid'], bytes(range(16)).hex())
        self.assertIn('__DATA_CONST,__const', described['sections'])
        self.assertEqual(described['symbol_count'], 3)

    def test_addresses_resolve_to_their_sections(self):
        data, expected = build_driver()
        image = MACHO.open_image(data, 'synthetic')
        self.assertTrue(image.is_executable_address(expected['handlers'][0]))
        self.assertFalse(image.is_executable_address(expected['table_address']))
        self.assertTrue(image.is_cstring_address(expected['entitlements']['submit']))
        self.assertEqual(image.cstring_at(expected['entitlements']['config']),
                         'com.apple.private.gpu.config')

    def test_a_read_outside_a_section_returns_nothing(self):
        data, _ = build_driver()
        image = MACHO.open_image(data, 'synthetic')
        self.assertIsNone(image.read_address(0x1, 8))
        self.assertIsNone(image.cstring_at(0x1))

    def test_a_truncated_image_is_refused(self):
        data, _ = build_driver()
        with self.assertRaises(MACHO.MachOError):
            MACHO.open_image(data[:16], 'truncated')

    def test_a_foreign_magic_is_refused(self):
        with self.assertRaises(MACHO.MachOError):
            MACHO.open_image(b'\x7fELF' + bytes(60), 'elf')


class ReadsFilesets(unittest.TestCase):
    def test_a_collection_lists_its_bundles(self):
        first, _ = build_driver()
        second, _ = build_driver(style='legacy')
        collection = builder.fileset([('com.apple.driver.AppleIntelICLGraphics', first),
                                      ('com.apple.iokit.IOGPUFamily', second)])
        image = MACHO.open_image(collection, 'collection')
        self.assertEqual(image.describe()['filetype_name'], 'MH_FILESET')
        self.assertEqual([entry['identifier'] for entry in image.fileset_entries],
                         ['com.apple.driver.AppleIntelICLGraphics',
                          'com.apple.iokit.IOGPUFamily'])

    def test_an_image_inside_a_collection_opens_in_place(self):
        first, expected = build_driver()
        collection = builder.fileset([('com.apple.driver.AppleIntelICLGraphics', first)])
        images = MACHO.fileset_images(MACHO.open_image(collection, 'collection'))
        inner = images['com.apple.driver.AppleIntelICLGraphics']
        self.assertEqual(inner.describe()['filetype_name'], 'MH_KEXT_BUNDLE')
        self.assertEqual(inner.describe()['symbol_count'], 3)
        self.assertTrue(inner.is_executable_address(expected['handlers'][0]))

    def test_an_unknown_bundle_is_an_error_not_an_empty_result(self):
        collection = builder.fileset([('com.apple.iokit.IOGPUFamily', build_driver()[0])])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'collection.kc'
            path.write_bytes(collection)
            with self.assertRaises(MACHO.MachOError):
                MACHO.load_image(path, 'com.apple.driver.Absent')


class RecoversTables(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = CONTRACT.contract(COMPARE.read_reference)

    def scan(self, data: bytes):
        image = MACHO.open_image(data, 'synthetic')
        scanner = DISPATCH.Scanner(image, self.contract)
        return scanner.run()

    def test_the_planted_table_is_found_at_its_address(self):
        data, expected = build_driver()
        tables = [table for table in self.scan(data) if table['style'] == '2022']
        self.assertTrue(tables, 'no 2022 table recovered')
        table = tables[0]
        self.assertEqual(table['address'], expected['table_address'])
        self.assertEqual(table['entry_count'], 4)
        self.assertEqual(table['entry_bytes'], 40)
        self.assertEqual(table['section'], '__DATA_CONST,__const')

    def test_every_selector_keeps_its_index_and_shape(self):
        data, _ = build_driver()
        table = [t for t in self.scan(data) if t['style'] == '2022'][0]
        shapes = [(entry['array_index'],
                   entry['checks']['scalar_input_count']['value'],
                   entry['checks']['structure_input_size']['value'],
                   entry['checks']['scalar_output_count']['value'],
                   entry['checks']['structure_output_size']['value'])
                  for entry in table['entries']]
        self.assertEqual(shapes, [(0, 2, 0, 0, 48), (1, 0, 480, 0, 80),
                                  (2, 0, SENTINEL, 1, SENTINEL), (3, 1, 0, 0, 0)])

    def test_a_variable_size_check_is_reported_as_variable(self):
        data, _ = build_driver()
        table = [t for t in self.scan(data) if t['style'] == '2022'][0]
        entry = table['entries'][2]
        self.assertTrue(entry['checks']['structure_input_size']['variable'])
        self.assertFalse(entry['checks']['scalar_input_count']['variable'])

    def test_the_asynchronous_flag_is_read(self):
        data, _ = build_driver()
        table = [t for t in self.scan(data) if t['style'] == '2022'][0]
        self.assertEqual([entry['allow_async'] for entry in table['entries']],
                         [False, True, False, False])

    def test_entitlement_strings_are_resolved(self):
        data, _ = build_driver()
        table = [t for t in self.scan(data) if t['style'] == '2022'][0]
        self.assertEqual([entry['entitlement'] for entry in table['entries']],
                         [None, None, 'com.apple.private.gpu.submit',
                          'com.apple.private.gpu.config'])
        self.assertEqual(table['entitlement_gated'], 2)

    def test_handlers_resolve_to_symbol_names(self):
        data, expected = build_driver()
        table = [t for t in self.scan(data) if t['style'] == '2022'][0]
        self.assertEqual(table['entries'][0]['handler']['address'], expected['handlers'][0])
        self.assertIn('submit', table['entries'][0]['handler']['symbol'])
        self.assertIn('configure', table['entries'][1]['handler']['symbol'])
        self.assertEqual(table['named_handlers'], 3)

    def test_a_null_handler_is_recorded_as_such(self):
        data, _ = build_driver()
        table = [t for t in self.scan(data) if t['style'] == '2022'][0]
        entry = table['entries'][3]
        self.assertEqual(entry['handler']['address'], 0)
        self.assertEqual(entry['handler']['kind'], 'null')
        self.assertIsNone(entry['handler']['symbol'])

    def test_a_stripped_image_still_yields_the_table(self):
        data, _ = build_driver(with_symbols=False)
        table = [t for t in self.scan(data) if t['style'] == '2022'][0]
        self.assertEqual(table['entry_count'], 4)
        self.assertEqual(table['named_handlers'], 0)
        self.assertEqual(table['confidence']['level'] if 'confidence' in table
                         else DISPATCH.confidence(table)['level'], 'medium')

    def test_the_legacy_structure_is_recovered_too(self):
        data, _ = build_driver(style='legacy')
        tables = [table for table in self.scan(data) if table['style'] == 'legacy']
        self.assertTrue(tables)
        table = tables[0]
        self.assertEqual(table['entry_bytes'], 24)
        self.assertGreaterEqual(table['entry_count'], 3)
        self.assertEqual(table['entries'][1]['checks']['structure_input_size']['value'], 480)

    def test_surrounding_data_is_not_mistaken_for_entries(self):
        data, expected = build_driver()
        table = [t for t in self.scan(data) if t['style'] == '2022'][0]
        self.assertEqual(table['address'], expected['table_address'])
        self.assertEqual(table['entry_count'], 4)


class ReportsRatherThanTruncates(unittest.TestCase):
    """A compiler habit must not be able to hide a real table."""

    @classmethod
    def setUpClass(cls):
        cls.contract = CONTRACT.contract(COMPARE.read_reference)

    def build_with_anomaly(self):
        """A four-entry table whose last entry has non-zero padding."""
        image = builder.ImageBuilder()
        image.section('__TEXT', '__text', b'\x90' * 64, builder.TEXT_FLAGS)
        probe_image, _ = image.build()
        probe = MACHO.open_image(probe_image, 'probe')
        handlers = [probe.section('__TEXT', '__text')['address'] + index * 16
                    for index in range(4)]
        dirty = bytearray(builder.dispatch_entry_2022(handlers[3], 1, 0, 0, 8))
        dirty[25] = 0x01  # a byte the ABI leaves undefined
        final = builder.ImageBuilder()
        final.section('__TEXT', '__text', b'\x90' * 64, builder.TEXT_FLAGS)
        final.section('__DATA_CONST', '__const',
                      builder.dispatch_entry_2022(handlers[0], 2, 0, 0, 48)
                      + builder.dispatch_entry_2022(handlers[1], 0, 480, 0, 80)
                      + builder.dispatch_entry_2022(handlers[2], 0, 0, 0, 0)
                      + bytes(dirty))
        data, _ = final.build()
        return data

    def test_a_padding_anomaly_extends_the_table_instead_of_ending_it(self):
        image = MACHO.open_image(self.build_with_anomaly(), 'anomaly')
        tables = [table for table in DISPATCH.Scanner(image, self.contract).run()
                  if table['style'] == '2022']
        self.assertTrue(tables)
        table = max(tables, key=lambda entry: entry['entry_count'])
        self.assertEqual(table['entry_count'], 4)
        self.assertEqual(table['padding_anomalies'], 1)
        self.assertEqual(table['extended_entries'], 1)
        self.assertTrue(table['entries'][3]['padding_nonzero'])
        self.assertFalse(table['entries'][3]['anchored'])

    def test_an_anomaly_lowers_confidence_with_a_stated_reason(self):
        image = MACHO.open_image(self.build_with_anomaly(), 'anomaly')
        table = max((t for t in DISPATCH.Scanner(image, self.contract).run()
                     if t['style'] == '2022'), key=lambda entry: entry['entry_count'])
        verdict = DISPATCH.confidence(table)
        self.assertTrue(any('non-zero padding' in reason for reason in verdict['reasons']))

    def test_the_recovered_number_is_an_array_index_not_a_wire_selector(self):
        data, _ = build_driver()
        image = MACHO.open_image(data, 'synthetic')
        table = [t for t in DISPATCH.Scanner(image, self.contract).run()
                 if t['style'] == '2022'][0]
        self.assertEqual([entry['array_index'] for entry in table['entries']], [0, 1, 2, 3])
        self.assertTrue(all(entry['wire_selector'] is None for entry in table['entries']))
        self.assertIn('unknown', table['selector_base'])

    def test_a_check_states_that_it_is_an_exact_requirement(self):
        data, _ = build_driver()
        image = MACHO.open_image(data, 'synthetic')
        table = [t for t in DISPATCH.Scanner(image, self.contract).run()
                 if t['style'] == '2022'][0]
        exact = table['entries'][0]['checks']['structure_output_size']
        self.assertIn('exactly', exact['meaning'])
        variable = table['entries'][2]['checks']['structure_input_size']
        self.assertIn('any count', variable['meaning'])

    def test_the_pointer_form_is_detected_and_named(self):
        data, _ = build_driver()
        image = MACHO.open_image(data, 'synthetic')
        table = [t for t in DISPATCH.Scanner(image, self.contract).run()
                 if t['style'] == '2022'][0]
        self.assertEqual(table['pointer_form'], 'rebased_addresses')


class BoundsLiteralContent(unittest.TestCase):
    """The entitlement identifier is the one literal string we keep, so its
    shape is enforced rather than trusted."""

    @classmethod
    def setUpClass(cls):
        cls.contract = CONTRACT.contract(COMPARE.read_reference)

    def build_with_string(self, text: bytes):
        image = builder.ImageBuilder()
        image.section('__TEXT', '__text', b'\x90' * 32, builder.TEXT_FLAGS)
        image.section('__TEXT', '__cstring', text + b'\0', builder.S_CSTRING_LITERALS)
        probe_image, _ = image.build()
        probe = MACHO.open_image(probe_image, 'probe')
        handler = probe.section('__TEXT', '__text')['address']
        string_address = probe.section('__TEXT', '__cstring')['address']
        final = builder.ImageBuilder()
        final.section('__TEXT', '__text', b'\x90' * 32, builder.TEXT_FLAGS)
        final.section('__TEXT', '__cstring', text + b'\0', builder.S_CSTRING_LITERALS)
        final.section('__DATA_CONST', '__const',
                      builder.dispatch_entry_2022(handler, 0, 0, 0, 0,
                                                  entitlement=string_address)
                      + builder.dispatch_entry_2022(handler, 1, 0, 0, 0,
                                                    entitlement=string_address))
        data, _ = final.build()
        image = MACHO.open_image(data, 'strings')
        tables = [table for table in DISPATCH.Scanner(image, self.contract).run()
                  if table['style'] == '2022']
        return tables[0]['entries'][0] if tables else None

    def test_an_identifier_shaped_entitlement_is_kept(self):
        entry = self.build_with_string(b'com.apple.private.gpu.submit')
        self.assertEqual(entry['entitlement'], 'com.apple.private.gpu.submit')
        self.assertNotIn('entitlement_unreadable', entry)

    def test_prose_at_that_pointer_is_recorded_by_length_only(self):
        entry = self.build_with_string(b'this is not an entitlement, it is a sentence')
        self.assertIsNone(entry['entitlement'])
        self.assertTrue(entry['entitlement_unreadable'])
        self.assertEqual(entry['entitlement_bytes'],
                         len('this is not an entitlement, it is a sentence'))

    def test_an_overlong_string_is_not_reproduced_but_the_entry_survives(self):
        entry = self.build_with_string(b'com.apple.' + b'a' * 200)
        self.assertIsNotNone(entry, 'the entry was discarded instead of flagged')
        self.assertIsNone(entry['entitlement'])
        self.assertTrue(entry['entitlement_unreadable'])
        self.assertIsNone(entry['entitlement_bytes'])
        self.assertIn('longer than', entry['entitlement_reason'])

    def test_a_pointer_into_non_string_data_still_rejects_the_entry(self):
        # A field pointing at something that is not string data is structural
        # evidence that this window is not a dispatch entry at all.
        data, expected = build_driver()
        image = MACHO.open_image(data, 'synthetic')
        scanner = DISPATCH.Scanner(image, self.contract)
        ok, text, extra = scanner._entitlement(expected['handlers'][0])
        self.assertFalse(ok)
        self.assertIsNone(text)

    def test_the_record_states_the_one_literal_it_keeps(self):
        data, _ = build_driver()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'driver'
            path.write_bytes(data)
            record = DISPATCH.recover(path, None)
        self.assertIn('entitlement identifier', record['literal_content'])
        self.assertEqual(POLICY.check_record(record), [])


class JudgesAndPlans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = CONTRACT.contract(COMPARE.read_reference)
        data, cls.expected = build_driver()
        image = MACHO.open_image(data, 'synthetic')
        cls.table = [table for table in DISPATCH.Scanner(image, cls.contract).run()
                     if table['style'] == '2022'][0]

    def test_named_handlers_and_entitlements_raise_confidence(self):
        verdict = DISPATCH.confidence(self.table)
        self.assertEqual(verdict['level'], 'high')
        self.assertTrue(any('symbol names' in reason for reason in verdict['reasons']))

    def test_the_probe_plan_marks_which_checks_run_a_handler(self):
        plan = DISPATCH.probe_plan(self.table, self.contract)
        self.assertEqual(len(plan), 4)
        entitled = [step for step in plan if step['expect'] == 'kIOReturnNotPrivileged']
        self.assertEqual(len(entitled), 2)
        self.assertTrue(all('array_index' in step for step in plan))
        for step in entitled:
            self.assertFalse(step['handler_runs'])
            self.assertEqual(step['expect_value'], '0xe00002c1')
        self.assertTrue(plan[0]['handler_runs'])

    def test_a_variable_check_is_planned_as_any(self):
        plan = DISPATCH.probe_plan(self.table, self.contract)
        self.assertEqual(plan[2]['shape']['structure_input_size'], 'any')
        self.assertEqual(plan[1]['shape']['structure_input_size'], 480)


class ProducesACleanRecord(unittest.TestCase):
    def test_a_full_recovery_passes_the_policy_gate(self):
        data, expected = build_driver()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'AppleIntelICLGraphics'
            path.write_bytes(data)
            record = DISPATCH.recover(path, None)
            self.assertEqual(POLICY.check_record(record), [])
            POLICY.write_record(Path(directory) / 'dispatch.json', record, 'tables')
        self.assertEqual(record['entry_sizes'], {'2022': 40, 'legacy': 24})
        self.assertGreaterEqual(record['table_count'], 1)
        self.assertEqual(record['image']['uuid'], bytes(range(16)).hex())

    def test_recovery_from_a_collection_names_the_bundle(self):
        first, _ = build_driver()
        collection = builder.fileset([('com.apple.driver.AppleIntelICLGraphics', first)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'BootKernelExtensions.kc'
            path.write_bytes(collection)
            record = DISPATCH.recover(path, 'com.apple.driver.AppleIntelICLGraphics')
        self.assertEqual(record['binary']['bundle'],
                         'com.apple.driver.AppleIntelICLGraphics')
        self.assertGreaterEqual(record['table_count'], 1)
        self.assertEqual(POLICY.check_record(record), [])

    def test_no_byte_of_the_analysed_image_is_reproduced(self):
        data, _ = build_driver()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'driver'
            path.write_bytes(data)
            record = DISPATCH.recover(path, None)
        flat = repr(record)
        self.assertNotIn('\\x90', flat)
        self.assertNotIn('1122334455667788', flat)


if __name__ == '__main__':
    unittest.main(verbosity=2)
