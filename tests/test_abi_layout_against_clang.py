# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Let the compiler judge the layout, and the scanner read what it emitted.

tools/abi_contract.py computes the x86_64 layout of the dispatch structures from
the pinned xnu declarations. That computation is an assumption about the ABI
until something independent confirms it — so here the structures are generated
back out of the pinned declarations, handed to clang with static assertions on
every computed size and offset, and compiled for Mach-O. If clang disagrees with
a single offset, the compile fails and so does this test.

The compiled object then carries a real, compiler-emitted dispatch table, and
the scanner has to recover it exactly. That is a stronger end-to-end check than
any hand-built bytes, and it needs no Apple binary.
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


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTRACT = load('abi_contract', 'tools/abi_contract.py')
COMPARE = load('compare_interfaces', 'tools/compare_interfaces.py')
MACHO = load('abi_macho', 'tools/abi_macho.py')
DISPATCH = load('abi_dispatch', 'tools/abi_dispatch.py')

# Field types as they must be spelled in a freestanding translation unit.
TYPE_SOURCE = {
    'IOExternalMethodAction': 'int (*%s)(void *, void *, void *)',
    'uint32_t': 'unsigned int %s',
    'uint8_t': 'unsigned char %s',
    'const char*': 'const char *%s',
    'const char *': 'const char *%s',
}


def declare(fields: list[tuple[str, str]]) -> str:
    lines = []
    for type_name, name in fields:
        pattern = TYPE_SOURCE.get(type_name)
        if pattern is None:
            raise AssertionError('no C spelling for ' + type_name)
        lines.append('    ' + (pattern % name) + ';')
    return '\n'.join(lines)


def fixture_source(contract: dict) -> str:
    """A translation unit that asserts every computed offset and plants a table."""
    parts = ['// Generated from the pinned xnu declarations by the test.',
             'typedef unsigned long size_t;']
    for name in ('IOExternalMethodDispatch', 'IOExternalMethodDispatch2022'):
        structure = contract['dispatch_structures'][name]
        fields = [(entry['type'], entry['name']) for entry in structure['declared_fields']]
        parts.append('struct ' + name + ' {\n' + declare(fields) + '\n};')
        parts.append('static_assert(sizeof(struct ' + name + ') == '
                     + str(structure['layout']['size'])
                     + ', "computed size disagrees with the compiler");')
        for field in structure['layout']['fields']:
            if field['type'] is None:
                continue
            parts.append('static_assert(__builtin_offsetof(struct ' + name + ', '
                         + field['name'] + ') == ' + str(field['offset'])
                         + ', "computed offset disagrees with the compiler");')
    sentinel = contract['sentinels']['kIOUCVariableStructureSize']['value']
    parts.append('''
static int lb_submit(void *, void *, void *) { return 0; }
static int lb_configure(void *, void *, void *) { return 1; }
static int lb_query(void *, void *, void *) { return 2; }

static const char kSubmitEntitlement[] = "com.apple.private.gpu.submit";

extern const struct IOExternalMethodDispatch2022 lbModernTable[4];
const struct IOExternalMethodDispatch2022 lbModernTable[4] = {
    { lb_submit,     2, 0,   0, 48, 0, 0 },
    { lb_configure,  0, 480, 0, 80, 1, 0 },
    { lb_query,      0, %(sentinel)uu, 1, %(sentinel)uu, 0, kSubmitEntitlement },
    { 0,             1, 0,   0, 0,  0, 0 },
};

extern const struct IOExternalMethodDispatch lbLegacyTable[3];
const struct IOExternalMethodDispatch lbLegacyTable[3] = {
    { lb_submit,    2, 0,   0, 48 },
    { lb_configure, 0, 480, 0, 80 },
    { lb_query,     0, %(sentinel)uu, 1, %(sentinel)uu },
};
''' % {'sentinel': sentinel})
    return '\n'.join(parts) + '\n'


def compile_fixture(source: str, directory: Path) -> Path | None:
    """Compile for Mach-O, or return None when this host cannot."""
    compiler = shutil.which('clang++')
    if not compiler:
        return None
    path = directory / 'dispatch_fixture.cpp'
    path.write_text(source, encoding='utf-8')
    target = directory / 'dispatch_fixture.o'
    result = subprocess.run(
        [compiler, '-target', TARGET, '-std=c++17', '-O2', '-fno-exceptions', '-fno-rtti',
         '-ffreestanding', '-nostdinc', '-nostdinc++', '-Wall', '-Wextra', '-Werror',
         '-c', str(path), '-o', str(target)],
        capture_output=True, text=True, timeout=300, check=False)
    if result.returncode:
        # A static assertion failing here is a real finding about our layout
        # computation, so it is reported rather than skipped.
        if 'static_assert' in result.stderr or 'static assertion' in result.stderr:
            raise AssertionError('the compiler rejected a computed layout:\n'
                                 + result.stderr[:2000])
        return None
    return target


class CompilerConfirmsTheLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = CONTRACT.contract(COMPARE.read_reference)
        cls.directory = tempfile.TemporaryDirectory()
        cls.object_path = compile_fixture(fixture_source(cls.contract),
                                          Path(cls.directory.name))
        if cls.object_path is None:
            raise unittest.SkipTest('no clang that can target ' + TARGET)
        cls.image = MACHO.open_image(cls.object_path.read_bytes(), 'fixture')

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_the_object_is_a_real_mach_o(self):
        described = self.image.describe()
        self.assertEqual(described['architecture'], 'x86_64')
        self.assertEqual(described['filetype_name'], 'MH_OBJECT')
        self.assertTrue(any(name.endswith('__const') for name in described['sections']),
                        described['sections'])

    def test_the_compiler_emitted_the_entry_size_we_computed(self):
        # Every static_assert in the fixture passed, so sizes and offsets agree.
        # This re-states the headline number for the record.
        self.assertEqual(
            self.contract['dispatch_structures']['IOExternalMethodDispatch2022'][
                'layout']['size'], 40)
        self.assertEqual(
            self.contract['dispatch_structures']['IOExternalMethodDispatch'][
                'layout']['size'], 24)

    def test_the_symbol_table_names_both_tables(self):
        names = set(self.image.symbols)
        self.assertIn('_lbModernTable', names)
        self.assertIn('_lbLegacyTable', names)


class ScannerReadsCompilerOutput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = CONTRACT.contract(COMPARE.read_reference)
        cls.directory = tempfile.TemporaryDirectory()
        cls.object_path = compile_fixture(fixture_source(cls.contract),
                                          Path(cls.directory.name))
        if cls.object_path is None:
            raise unittest.SkipTest('no clang that can target ' + TARGET)
        cls.image = MACHO.open_image(cls.object_path.read_bytes(), 'fixture')
        cls.tables = DISPATCH.Scanner(cls.image, cls.contract).run()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def modern(self):
        tables = [table for table in self.tables if table['style'] == '2022']
        self.assertTrue(tables, 'the compiler-emitted 2022 table was not recovered')
        return max(tables, key=lambda table: table['entry_count'])

    def test_the_compiler_emitted_table_is_recovered(self):
        table = self.modern()
        self.assertGreaterEqual(table['entry_count'], 4)
        self.assertEqual(table['entry_bytes'], 40)

    def test_every_shape_survives_the_round_trip(self):
        table = self.modern()
        shapes = [(entry['checks']['scalar_input_count']['value'],
                   entry['checks']['structure_input_size']['value'],
                   entry['checks']['scalar_output_count']['value'],
                   entry['checks']['structure_output_size']['value'])
                  for entry in table['entries'][:4]]
        sentinel = self.contract['sentinels']['kIOUCVariableStructureSize']['value']
        self.assertEqual(shapes, [(2, 0, 0, 48), (0, 480, 0, 80),
                                  (0, sentinel, 1, sentinel), (1, 0, 0, 0)])

    def test_the_asynchronous_flag_and_entitlement_survive(self):
        table = self.modern()
        self.assertEqual([entry['allow_async'] for entry in table['entries'][:4]],
                         [False, True, False, False])
        # In a relocatable object the string's address is not filled in yet, so
        # the entitlement is known by its symbol and not yet by its text. The
        # linked case, where the text resolves, is covered in test_abi_dispatch.
        entry = table['entries'][2]
        self.assertIsNone(entry['entitlement'])
        self.assertIn('kSubmitEntitlement', entry['entitlement_symbol'])
        self.assertEqual(table['entitlement_gated'], 1)

    def test_pointer_fields_are_read_from_relocations_in_an_object(self):
        table = self.modern()
        kinds = {entry['handler']['kind'] for entry in table['entries'][:3]}
        self.assertEqual(kinds, {'relocation'})

    def test_handlers_resolve_to_the_names_the_compiler_gave_them(self):
        table = self.modern()
        symbols = [entry['handler']['symbol'] for entry in table['entries'][:3]]
        self.assertTrue(any(name and 'lb_submit' in name for name in symbols), symbols)
        self.assertEqual(table['entries'][3]['handler']['address'], 0)

    def test_the_array_symbol_is_attributed_to_the_table(self):
        table = self.modern()
        self.assertEqual(table['symbol_at_table'], '_lbModernTable')
        self.assertEqual(DISPATCH.confidence(table)['level'], 'high')

    def test_the_legacy_table_is_recovered_from_the_same_object(self):
        legacy = [table for table in self.tables if table['style'] == 'legacy'
                  and table['entry_count'] >= 3]
        self.assertTrue(legacy, 'the compiler-emitted legacy table was not recovered')


if __name__ == '__main__':
    unittest.main(verbosity=2)
