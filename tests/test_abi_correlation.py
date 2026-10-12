# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""The table and the dispatch override are two readings of one interface.

One compiled object holds both: a real three-entry dispatch table and the
`externalMethod` override that dispatches through it with a selector base. The
scanner reads the bytes, the call-site reader reads the code, and correlating
them decides what neither can decide alone — the wire selectors, the
authoritative entry count, and which structure interpretation of the same bytes
is the right one.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TARGET = 'x86_64-apple-macos12'

SOURCE_LINES = [
    'typedef unsigned int u32;',
    'struct Args;',
    'struct D { int (*f)(void *, void *, void *); u32 a, b, c, d;',
    '           unsigned char e; const char *g; };',
    'static int h1(void *, void *, void *) { return 0; }',
    'static int h2(void *, void *, void *) { return 1; }',
    'static int h3(void *, void *, void *) { return 2; }',
    'extern int dispatchExternalMethod(void *, u32, struct Args *, const struct D *,',
    '                                  unsigned long, void *, void *);',
    'extern const struct D tbl[3];',
    'const struct D tbl[3] = {',
    '    { h1, 2, 0, 0, 48, 0, 0 },',
    '    { h2, 0, 480, 0, 80, 1, 0 },',
    '    { h3, 0, 0xffffffffu, 1, 0xffffffffu, 0, 0 },',
    '};',
    'struct Client { int externalMethod(u32 selector, struct Args *args); };',
    'int Client::externalMethod(u32 selector, struct Args *args) {',
    '    if (selector < BASEu) return -1;',
    '    u32 index = selector - BASEu;',
    '    if (index >= 3u) return -2;',
    '    return dispatchExternalMethod(this, index, args, tbl, 3, this, 0);',
    '}',
]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DISPATCH = load('abi_dispatch', 'tools/abi_dispatch.py')
CALLSITE = load('abi_callsite', 'tools/abi_callsite.py')
POLICY = load('abi_policy', 'tools/abi_policy.py')


def build(directory: Path, base: int) -> Path | None:
    if not shutil.which('clang++') or not shutil.which('llvm-objdump'):
        return None
    source = directory / 'combined.cpp'
    source.write_text('\n'.join(SOURCE_LINES).replace('BASE', str(base)) + '\n',
                      encoding='utf-8')
    target = directory / 'combined.o'
    result = subprocess.run(
        ['clang++', '-target', TARGET, '-std=c++17', '-O1', '-fno-exceptions', '-fno-rtti',
         '-ffreestanding', '-nostdinc', '-nostdinc++', '-c', str(source), '-o', str(target)],
        capture_output=True, text=True, timeout=300, check=False)
    return target if result.returncode == 0 else None


class CorrelatesTableAndCallSite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.path = build(Path(cls.directory.name), 0x1000)
        if cls.path is None:
            raise unittest.SkipTest('no toolchain for ' + TARGET)
        cls.record = DISPATCH.recover(cls.path, None)
        cls.callsite = CALLSITE.read(cls.path, None, None)
        DISPATCH.correlate(cls.record, cls.callsite)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def modern(self):
        tables = [table for table in self.record['tables'] if table['style'] == '2022']
        self.assertTrue(tables, 'the 2022 table was not recovered')
        return tables[0]

    def test_both_halves_were_read_from_one_object(self):
        self.assertTrue(self.record['tables'])
        self.assertTrue(self.callsite['functions'])
        self.assertTrue(self.record['call_site_correlated'])

    def test_the_call_site_supplies_the_wire_selectors(self):
        table = self.modern()
        self.assertEqual(table['selector_base'], 0x1000)
        self.assertEqual([entry['array_index'] for entry in table['entries']], [0, 1, 2])
        self.assertEqual([entry['wire_selector'] for entry in table['entries']],
                         [0x1000, 0x1001, 0x1002])

    def test_the_match_is_made_through_the_array_symbol(self):
        site = self.modern()['call_site']
        self.assertTrue(site['matched'])
        self.assertEqual(site['matched_by'], 'array symbol')
        self.assertEqual(site['entry_count'], 3)
        self.assertTrue(site['entry_count_agrees'])
        self.assertFalse(site['scan_over_read'])

    def test_the_shapes_the_scanner_read_are_the_ones_the_compiler_emitted(self):
        table = self.modern()
        shapes = [(entry['checks']['scalar_input_count']['value'],
                   entry['checks']['structure_input_size']['value'],
                   entry['checks']['scalar_output_count']['value'],
                   entry['checks']['structure_output_size']['value'])
                  for entry in table['entries']]
        self.assertEqual(shapes, [(2, 0, 0, 48), (0, 480, 0, 80),
                                  (0, 0xFFFFFFFF, 1, 0xFFFFFFFF)])
        self.assertEqual([entry['allow_async'] for entry in table['entries']],
                         [False, True, False])

    def test_a_disagreeing_reading_of_the_same_bytes_is_exposed(self):
        legacy = [table for table in self.record['tables'] if table['style'] == 'legacy']
        self.assertTrue(legacy, 'the same bytes should also parse as a legacy run')
        site = legacy[0].get('call_site') or {}
        if site.get('matched'):
            self.assertFalse(site['entry_count_agrees'],
                             'a legacy misreading must not agree with the call site')

    def test_an_unmatched_table_says_so_instead_of_borrowing_a_base(self):
        record = {'tables': [{'symbol_at_table': None, 'entry_count': 99,
                              'entries': [{'array_index': 0, 'wire_selector': None}],
                              'selector_base': 'unknown'}]}
        DISPATCH.correlate(record, self.callsite)
        table = record['tables'][0]
        self.assertFalse(table['call_site']['matched'])
        self.assertEqual(table['selector_base'], 'unknown')
        self.assertIsNone(table['entries'][0]['wire_selector'])

    def test_an_over_read_run_is_flagged_as_such(self):
        record = {'tables': [{'symbol_at_table': '_tbl', 'entry_count': 9,
                              'entries': [{'array_index': index, 'wire_selector': None}
                                          for index in range(9)],
                              'selector_base': 'unknown'}]}
        DISPATCH.correlate(record, self.callsite)
        site = record['tables'][0]['call_site']
        self.assertTrue(site['matched'])
        self.assertFalse(site['entry_count_agrees'])
        self.assertTrue(site['scan_over_read'])

    def test_the_correlated_record_passes_the_policy_gate(self):
        self.assertEqual(POLICY.check_record(self.record), [])
        self.assertEqual(POLICY.check_record(self.callsite), [])


class RecoversADifferentBase(unittest.TestCase):
    def test_a_second_base_is_read_as_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            path = build(Path(directory), 0x2A)
            if path is None:
                self.skipTest('no toolchain for ' + TARGET)
            record = DISPATCH.recover(path, None)
            callsite = CALLSITE.read(path, None, None)
            DISPATCH.correlate(record, callsite)
        table = [entry for entry in record['tables'] if entry['style'] == '2022'][0]
        self.assertEqual(table['selector_base'], 0x2A)
        self.assertEqual(table['entries'][0]['wire_selector'], 0x2A)


if __name__ == '__main__':
    unittest.main(verbosity=2)
