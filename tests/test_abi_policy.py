# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""The policy gate must pass interface facts and refuse anything that carries
content out of the analysed binary."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


POLICY = load('abi_policy', 'tools/abi_policy.py')

# What a recovered dispatch table legitimately looks like.
CLEAN = {
    'tool': 'tools/abi_dispatch.py',
    'bundle': {'identifier': 'com.apple.driver.AppleIntelICLGraphics',
               'version': '16.0.6',
               'binary_sha256': 'a' * 64,
               'uuid': 'b' * 32},
    'selectors': [
        {'selector': 0, 'handler': '__ZN21AppleIntelICLGraphics14clientMemoryForTypeEjPjPP18IOMemoryDescriptor',
         'check_scalar_input_count': 2, 'check_structure_input_size': 0xFFFFFFFF,
         'check_scalar_output_count': 0, 'check_structure_output_size': 48,
         'allow_async': False, 'entitlement': None, 'table_offset': 0x1A4C0},
        {'selector': 1, 'handler': None, 'check_scalar_input_count': 0,
         'check_structure_input_size': 480, 'check_scalar_output_count': 0,
         'check_structure_output_size': 80, 'allow_async': True,
         'entitlement': 'com.apple.private.gpu.submit', 'table_offset': 0x1A4F0},
    ],
    'citations': ['tests/.references/macos-tahoe/xnu-12377.121.6/iokit/IOKit/IOUserClient.h:1207'],
    'measured_on_hardware': False,
}


class AllowsFacts(unittest.TestCase):
    def test_a_recovered_table_passes(self):
        self.assertEqual(POLICY.check_record(CLEAN), [])

    def test_addresses_and_masks_pass(self):
        record = {'offset': 0x4800, 'mask_hex': '0xffffffff', 'address': '0x1008a2c40',
                  'bits': [3, 4, 7], 'size': 4096}
        self.assertEqual(POLICY.check_record(record), [])

    def test_digests_pass_only_at_digest_keys(self):
        self.assertEqual(POLICY.check_record({'binary_sha256': 'c' * 64}), [])
        self.assertEqual(POLICY.check_record({'uuid': 'd' * 32}), [])
        violations = POLICY.check_record({'note': 'e' * 64})
        self.assertEqual([entry['rule'] for entry in violations], ['hex_blob'])

    def test_symbol_names_and_sections_pass(self):
        record = {'symbols': ['__ZTV21AppleIntelICLGraphics', '_gMetaClass'],
                  'sections': ['__DATA_CONST,__const', '__TEXT,__text'],
                  'classes': {'AppleIntelICLGraphics': ['start', 'newUserClient']}}
        self.assertEqual(POLICY.check_record(record), [])


class DoesNotCryWolf(unittest.TestCase):
    """False positives are as damaging as misses: they block honest records."""

    def test_an_xnu_citation_with_a_cxx_path_passes(self):
        record = {'citation': 'macos-tahoe/xnu-12377.121.6/libkern/libkern/c++/OSMetaClass.h:1052'}
        self.assertEqual(POLICY.check_record(record), [])

    def test_prose_containing_instruction_words_passes(self):
        record = {'check': 'every classSize is a multiple of 8 and between 64 bytes and 64 KiB',
                  'note': 'the test and the report cannot disagree',
                  'rule': 'push or pop the list, then compare and add the result'}
        self.assertEqual(POLICY.check_record(record), [])

    def test_real_at_and_t_disassembly_is_still_refused(self):
        for listing in ('movq -0x18(%rbp), %rax', 'callq *0x2a(%rip)', 'addq $0x20, %rsp',
                        'and $0xfffffff0, %eax'):
            with self.subTest(listing=listing):
                violations = POLICY.check_record({'body': listing})
                self.assertIn('disassembly', [entry['rule'] for entry in violations], listing)

    def test_long_file_paths_pass(self):
        record = {'path': '/System/Library/Extensions/AppleIntelICLGraphics.kext/Contents/MacOS'}
        self.assertEqual(POLICY.check_record(record), [])


class RefusesContent(unittest.TestCase):
    def test_raw_bytes_anywhere_are_refused(self):
        violations = POLICY.check_record({'table': {'entry': b'\x48\x89\xe5'}})
        self.assertEqual([entry['rule'] for entry in violations], ['raw_bytes'])
        self.assertEqual(violations[0]['path'], '$.table.entry')

    def test_a_byte_dump_is_refused(self):
        violations = POLICY.check_record({'prologue': '55 48 89 e5 41 57'})
        self.assertIn('byte_run', [entry['rule'] for entry in violations])

    def test_a_disassembly_listing_is_refused(self):
        violations = POLICY.check_record(
            {'handler_body': 'pushq %rbp\nmovq %rsp, %rbp\ncallq *-0x18(%rax)'})
        self.assertIn('disassembly', [entry['rule'] for entry in violations])

    def test_refused_keys_are_refused_even_when_empty(self):
        for key in ('disassembly', 'hexdump', 'machine_code', 'firmware', 'blob'):
            with self.subTest(key=key):
                violations = POLICY.check_record({key: ''})
                self.assertEqual([entry['rule'] for entry in violations], ['refused_key'])

    def test_an_embedded_blob_is_refused(self):
        violations = POLICY.check_record({'note': 'QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE='})
        self.assertIn('base64_blob', [entry['rule'] for entry in violations])

    def test_a_long_hex_run_is_refused_outside_digest_keys(self):
        violations = POLICY.check_record({'constant': '0x48894c24e8415741564155'})
        self.assertIn('hex_blob', [entry['rule'] for entry in violations])

    def test_an_oversized_string_is_refused(self):
        violations = POLICY.check_record({'comment': 'x' * 5000})
        self.assertIn('string_too_long', [entry['rule'] for entry in violations])

    def test_violations_inside_lists_report_their_index(self):
        violations = POLICY.check_record({'entries': ['fine', '48 89 e5 90']})
        self.assertEqual(violations[0]['path'], '$.entries[1]')

    def test_an_unsupported_type_is_refused_rather_than_ignored(self):
        violations = POLICY.check_record({'handle': object()})
        self.assertEqual([entry['rule'] for entry in violations], ['unsupported_type'])


class RefusesInvocations(unittest.TestCase):
    """A byte-emitting flag is refused before it runs, not filtered afterwards."""

    def test_disassembly_invocations_are_allowed(self):
        for command in (['llvm-objdump', '-d', '--no-show-raw-insn', 'binary'],
                        ['/usr/bin/otool', '-tV', 'binary'],
                        ['nm', '-g', '-U', 'binary'],
                        ['dyld_info', '-objc', '-imports', 'binary']):
            with self.subTest(command=command):
                POLICY.assert_tool_invocation(command)

    def test_byte_dumping_flags_are_refused(self):
        for command in (['llvm-objdump', '--full-contents', 'binary'],
                        ['llvm-objdump', '-d', '--show-raw-insn', 'binary'],
                        ['dyld_info', '-section_bytes', '__TEXT,__text', 'binary'],
                        ['dyld_info', '-disassemble', 'binary'],
                        ['codesign', '-dvvv', '--extract-certificates', 'bundle'],
                        ['dyld_shared_cache_util', '-extract', 'out', 'cache'],
                        ['kmutil', 'splitkc', 'collection']):
            with self.subTest(command=command):
                with self.assertRaises(POLICY.PolicyError):
                    POLICY.assert_tool_invocation(command)

    def test_otool_t_without_v_is_refused_but_tv_is_not(self):
        with self.assertRaises(POLICY.PolicyError):
            POLICY.assert_tool_invocation(['otool', '-t', 'binary'])
        POLICY.assert_tool_invocation(['otool', '-tV', 'binary'])

    def test_tools_that_only_emit_bytes_are_refused_outright(self):
        for name in ('dd', 'xxd', 'hexdump'):
            with self.subTest(tool=name):
                with self.assertRaises(POLICY.PolicyError):
                    POLICY.assert_tool_invocation([name, 'binary'])

    def test_an_unknown_tool_is_not_blocked(self):
        POLICY.assert_tool_invocation(['ioreg', '-l', '-w', '0'])

    def test_an_empty_invocation_is_refused(self):
        with self.assertRaises(POLICY.PolicyError):
            POLICY.assert_tool_invocation([])


class WritesOrRefuses(unittest.TestCase):
    def test_a_clean_record_is_written_with_its_scope_note(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'nested' / 'dispatch.json'
            POLICY.write_record(target, CLEAN, 'dispatch table')
            written = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(written['selectors'], CLEAN['selectors'])
        self.assertIn('No instruction or data bytes', written['scope'])

    def test_a_dirty_record_is_not_written_at_all(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'dispatch.json'
            with self.assertRaises(POLICY.PolicyError) as caught:
                POLICY.write_record(target, {'prologue': '55 48 89 e5'}, 'dispatch table')
            self.assertFalse(target.exists())
        self.assertIn('byte_run', str(caught.exception))
        self.assertIn('dispatch table', str(caught.exception))

    def test_assert_clean_names_every_violation_it_reports(self):
        record = {'a': b'\x90', 'b': '48 89 e5 90', 'disassembly': 'x'}
        with self.assertRaises(POLICY.PolicyError) as caught:
            POLICY.assert_clean(record)
        message = str(caught.exception)
        for rule in ('raw_bytes', 'byte_run', 'refused_key'):
            self.assertIn(rule, message)

    def test_an_existing_scope_note_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'record.json'
            POLICY.write_record(target, {'scope': 'own note', 'value': 1})
            self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['scope'], 'own note')


if __name__ == '__main__':
    unittest.main(verbosity=2)
