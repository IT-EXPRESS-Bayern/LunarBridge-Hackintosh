# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Prove the call-site reader against dispatch overrides the compiler produced.

The two facts a recovered table cannot state — how many entries the driver
believes it has, and whether a client's selector is the array index or that
index plus a base — are decided in the subclass's own externalMethod override.
Each fixture here is compiled for Mach-O from a known override, so the expected
answer is known exactly: the count, the base, the table symbol and the callee.
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

PREAMBLE = '''
typedef unsigned int u32;
struct Args;
struct D { int (*f)(void *, void *, void *); u32 a, b, c, d; };
extern const struct D tbl[%(count)d];
extern int dispatchExternalMethod(void *, u32, struct Args *, const struct D *,
                                  unsigned long, void *, void *);
struct Client { int externalMethod(u32 selector, struct Args *args); };
'''

WITH_BASE = PREAMBLE + '''
int Client::externalMethod(u32 selector, struct Args *args) {
    if (selector < %(base)du) return -1;
    u32 index = selector - %(base)du;
    if (index >= %(count)du) return -2;
    return dispatchExternalMethod(this, index, args, tbl, %(count)d, this, 0);
}
'''

WITHOUT_BASE = PREAMBLE + '''
int Client::externalMethod(u32 selector, struct Args *args) {
    if (selector >= %(count)du) return -2;
    return dispatchExternalMethod(this, selector, args, tbl, %(count)d, this, 0);
}
'''

LEGACY_STYLE = PREAMBLE + '''
extern int legacyDispatch(void *, u32, struct Args *, const struct D *);
int Client::externalMethod(u32 selector, struct Args *args) {
    if (selector >= %(count)du) return -2;
    return legacyDispatch(this, selector, args, &tbl[selector]);
}
'''


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CALLSITE = load('abi_callsite', 'tools/abi_callsite.py')
MACHO = load('abi_macho', 'tools/abi_macho.py')
POLICY = load('abi_policy', 'tools/abi_policy.py')


def build(template: str, directory: Path, *, count: int = 6, base: int = 0x100,
          name: str = 'client') -> Path | None:
    compiler = shutil.which('clang++')
    if not compiler or not shutil.which('llvm-objdump'):
        return None
    source = directory / (name + '.cpp')
    source.write_text(template % {'count': count, 'base': base}, encoding='utf-8')
    target = directory / (name + '.o')
    result = subprocess.run(
        [compiler, '-target', TARGET, '-std=c++17', '-O1', '-fno-exceptions', '-fno-rtti',
         '-ffreestanding', '-nostdinc', '-nostdinc++', '-c', str(source), '-o', str(target)],
        capture_output=True, text=True, timeout=300, check=False)
    return target if result.returncode == 0 else None


class ReadsTheOverride(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.path = build(WITH_BASE, Path(cls.directory.name))
        if cls.path is None:
            raise unittest.SkipTest('no clang targeting ' + TARGET + ' with llvm-objdump')
        cls.record = CALLSITE.read(cls.path, None, None)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def conclusions(self):
        self.assertTrue(self.record['functions'], 'no function was read')
        return self.record['functions'][0]['conclusions']

    def test_the_override_is_found_by_name(self):
        self.assertTrue(any('externalMethod' in name
                            for name in self.record['candidate_symbols']))

    def test_the_entry_count_is_confirmed_by_two_independent_readings(self):
        count = self.conclusions()['entry_count']
        self.assertEqual(count['value'], 6)
        self.assertTrue(count['sources_agree'])
        self.assertIn(6, count['from_bounds_check'])
        self.assertEqual(count['from_count_argument'], [6])

    def test_the_selector_base_is_recovered_and_cross_checked(self):
        base = self.conclusions()['selector_base']
        self.assertEqual(base['value'], 0x100)
        self.assertEqual(base['from_adjustment'], [0x100])
        self.assertIn(0x100, base['from_lower_bound'])
        self.assertTrue(base['sources_agree'])

    def test_frame_arithmetic_is_not_mistaken_for_a_selector_adjustment(self):
        base = self.conclusions()['selector_base']
        self.assertNotIn(16, base['from_adjustment'])
        self.assertNotIn(32, base['from_adjustment'])

    def test_the_table_and_the_callee_are_named(self):
        conclusions = self.conclusions()
        self.assertEqual(conclusions['table_symbols'], ['_tbl'])
        self.assertTrue(any('dispatchExternalMethod' in name
                            for name in conclusions['callees']))
        self.assertEqual(conclusions['dispatch_style'], '2022')

    def test_the_record_carries_no_instruction_text(self):
        self.assertEqual(POLICY.check_record(self.record), [])
        flat = repr(self.record)
        for fragment in ('movl', 'cmpl', 'callq', '%rsp', 'retq'):
            self.assertNotIn(fragment, flat)

    def test_offsets_are_recorded_so_a_finding_can_be_located(self):
        function = self.record['functions'][0]
        self.assertTrue(all(isinstance(entry['at_offset'], int)
                            for entry in function['bounds_checks']))
        self.assertGreater(function['instruction_count'], 5)


class HandlesTheOtherShapes(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        if not shutil.which('clang++') or not shutil.which('llvm-objdump'):
            self.skipTest('no toolchain for ' + TARGET)

    def tearDown(self):
        self.directory.cleanup()

    def read(self, template: str, **arguments):
        path = build(template, Path(self.directory.name), **arguments)
        if path is None:
            self.skipTest('fixture did not compile')
        record = CALLSITE.read(path, None, None)
        self.assertTrue(record['functions'], 'no function was read')
        return record['functions'][0]['conclusions']

    def test_without_a_base_the_wire_selector_is_the_array_index(self):
        conclusions = self.read(WITHOUT_BASE, count=11, name='nobase')
        self.assertEqual(conclusions['selector_base']['value'], 0)
        self.assertIn('array index', conclusions['selector_base']['note'])
        self.assertEqual(conclusions['entry_count']['value'], 11)

    def test_a_different_count_is_read_from_the_same_two_sources(self):
        conclusions = self.read(WITH_BASE, count=24, base=0x200, name='twentyfour')
        self.assertEqual(conclusions['entry_count']['value'], 24)
        self.assertEqual(conclusions['selector_base']['value'], 0x200)

    def test_a_self_dispatched_client_is_not_claimed_to_use_the_2022_path(self):
        conclusions = self.read(LEGACY_STYLE, count=7, name='legacy')
        self.assertNotEqual(conclusions['dispatch_style'], '2022')
        # The comparison gives two possible limits and no count argument exists
        # on this path, so the reader reports both and decides neither.
        self.assertEqual(conclusions['entry_count']['from_bounds_check'], [6, 7])
        self.assertEqual(conclusions['entry_count']['from_count_argument'], [])
        self.assertFalse(conclusions['entry_count']['sources_agree'])
        self.assertIsNone(conclusions['entry_count']['value'])


class FailsHonestly(unittest.TestCase):
    def test_no_disassembler_is_reported_not_worked_around(self):
        original = CALLSITE.shutil.which
        CALLSITE.shutil.which = lambda name: None
        try:
            with self.assertRaises(CALLSITE.DisassemblyUnavailable):
                CALLSITE.disassembler()
        finally:
            CALLSITE.shutil.which = original

    def test_a_binary_without_an_override_yields_no_function(self):
        with tempfile.TemporaryDirectory() as directory:
            compiler = shutil.which('clang++')
            if not compiler:
                self.skipTest('no clang')
            source = Path(directory) / 'empty.cpp'
            source.write_text('int unrelated(int value) { return value + 1; }\n',
                              encoding='utf-8')
            target = Path(directory) / 'empty.o'
            result = subprocess.run(
                [compiler, '-target', TARGET, '-std=c++17', '-O1', '-ffreestanding',
                 '-nostdinc', '-nostdinc++', '-c', str(source), '-o', str(target)],
                capture_output=True, text=True, timeout=300, check=False)
            if result.returncode:
                self.skipTest('fixture did not compile')
            record = CALLSITE.read(target, None, None)
        self.assertEqual(record['candidate_symbols'], [])
        self.assertEqual(record['functions'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
