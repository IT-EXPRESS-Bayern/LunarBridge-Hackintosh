# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Check generated kernel commands and reject Darwin userspace stack probes.

Run the portable command/fixture tests with unittest discovery. After a real
native build, run this file with --binary <kext executable> to inspect its actual
Mach-O symbols. Neither mode loads or executes any macOS kernel code.
"""
import argparse
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import runpy
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
from macho import inspect_macho
from installer_fixtures import kernel_fixture


def check_stack_probe_contract(inspection):
    """Reject imports and replacement definitions of the Darwin probe family."""
    for field in ('undefined_symbols', 'defined_symbols'):
        bad = [name for name in inspection[field]
               if name.lstrip('_').startswith('chkstk_darwin')]
        if bad:
            raise ValueError('Darwin userspace stack probe in ' + field + ': ' + ', '.join(bad))


def verify_binary(path):
    """Inspect one bounded real kernel bundle; this is not a kernel ABI test."""
    with path.open('rb') as handle:
        data = handle.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError('Kernel bundle byte budget exceeded')
    inspection = inspect_macho(data, require_kext=True)
    check_stack_probe_contract(inspection)
    return inspection


def fixture_with_symbol(name, *, defined=False):
    """Extend the existing non-runnable parser fixture with one external symbol."""
    data = bytearray(kernel_fixture())
    _, _, offset, count, strings_offset, strings_size = struct.unpack_from('<6I', data, 344)
    strings = bytes(data[strings_offset:strings_offset + strings_size])
    extra = struct.pack('<IBBHQ', len(strings), 0x0f if defined else 0x01,
                        1 if defined else 0, 0, 0x1000 if defined else 0)
    strings += name.encode('ascii') + b'\0'
    data = data[:strings_offset] + extra + strings
    struct.pack_into('<6I', data, 344, 2, 24, offset, count + 1,
                     strings_offset + len(extra), len(strings))
    return bytes(data)


class _LinkReached(Exception):
    pass


class KernelBuildContractTests(unittest.TestCase):
    def test_native_builder_commands_exclude_userspace_stack_probe(self):
        spec = importlib.util.spec_from_file_location('lb_kernel_builder_contract', ROOT / 'tools/build_native.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        commands = []
        def capture(command, **kwargs):
            commands.append(command)
            if '-kext' in command:
                raise _LinkReached()
            return subprocess.CompletedProcess(command, 0)
        with tempfile.TemporaryDirectory() as directory:
            argv = ['build_native.py', '--sdk', directory, '--out', str(Path(directory) / 'out')]
            with mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(sys, 'platform', 'darwin'), \
                 mock.patch.object(module.subprocess, 'check_output', return_value='selected-tool\n'), \
                 mock.patch.object(module.subprocess, 'run', side_effect=capture):
                with self.assertRaises(_LinkReached):
                    module.main()
        kernels = [command for command in commands if '-c' in command]
        # Thirteen original kernel sources plus lb_arc_interface.cpp.
        self.assertEqual(len(kernels), 14)
        for command in kernels:
            self.assertEqual(command.count('-fno-stack-check'), 1)
            self.assertIn('-mkernel', command)
            self.assertIn('-fapple-kext', command)
            self.assertNotIn('-fstack-check', command)
        self.assertNotIn('-fno-stack-check', commands[-1])

    def test_cross_builder_preserves_windows_and_changes_only_kernel_commands(self):
        commands = []
        def capture(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, '', '')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            argv = ['build_gpu.py', '--compiler', str(base / 'zig'), '--sdk', str(base / 'sdk'),
                    '--host-dir', str(base / 'host'), '--object-dir', str(base / 'objects')]
            # Suppress only the builder's report write. The real command assembly
            # and temporary-directory creation still run; no compiler is invoked.
            with mock.patch.object(sys, 'argv', argv), \
                 mock.patch('subprocess.run', side_effect=capture), \
                 mock.patch('subprocess.check_output', return_value='0.15.2\n'), \
                 mock.patch.object(Path, 'write_text'), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as status:
                    runpy.run_path(str(ROOT / 'gpu-prototype/build_gpu.py'), run_name='__main__')
            self.assertEqual(status.exception.code, 0)
        # One Windows DLL command plus fifteen macOS object commands.
        self.assertEqual(len(commands), 16)
        windows, kernels = commands[0], commands[1:]
        self.assertEqual(windows[windows.index('-target') + 1], 'x86_64-windows-gnu')
        self.assertIn('-shared', windows)
        self.assertIn('-DLB_DMA_HOST_TEST', windows)
        self.assertIn('-DLB_FORCEWAKE_HOST_TEST', windows)
        for flag in ('-fno-stack-check', '-fstack-check', '-mkernel', '-fapple-kext', '-mno-red-zone'):
            self.assertNotIn(flag, windows)
        for command in kernels:
            self.assertEqual(command[command.index('-target') + 1], 'x86_64-macos')
            self.assertEqual(command.count('-fno-stack-check'), 1)
            self.assertIn('-mkernel', command)
            self.assertNotIn('-fstack-check', command)

    def test_rejects_actual_undefined_symbol_family(self):
        for name in ('____chkstk_darwin', '___chkstk_darwin', '__chkstk_darwin', '_chkstk_darwin_probe'):
            with self.subTest(name=name):
                inspection = inspect_macho(fixture_with_symbol(name), require_kext=True)
                with self.assertRaisesRegex(ValueError, 'undefined_symbols'):
                    check_stack_probe_contract(inspection)

    def test_rejects_dummy_definition(self):
        inspection = inspect_macho(fixture_with_symbol('____chkstk_darwin', defined=True), require_kext=True)
        with self.assertRaisesRegex(ValueError, 'defined_symbols'):
            check_stack_probe_contract(inspection)

    def test_allows_public_mach_time_import(self):
        inspection = inspect_macho(fixture_with_symbol('_absolutetime_to_nanoseconds'), require_kext=True)
        check_stack_probe_contract(inspection)
        self.assertIn('_absolutetime_to_nanoseconds', inspection['undefined_symbols'])

    def test_name_in_ordinary_metadata_is_not_a_probe_import(self):
        data = kernel_fixture(identifier='de.test.____chkstk_darwin')
        check_stack_probe_contract(inspect_macho(data, require_kext=True))

    def test_binary_mode_requires_kernel_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture'
            path.write_bytes(kernel_fixture(kind=1))
            with self.assertRaisesRegex(ValueError, 'MH_KEXT_BUNDLE'):
                verify_binary(path)

    def test_binary_mode_checks_real_symbol_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture'
            path.write_bytes(fixture_with_symbol('____chkstk_darwin'))
            with self.assertRaisesRegex(ValueError, 'undefined_symbols'):
                verify_binary(path)
            path.write_bytes(kernel_fixture())
            self.assertEqual(verify_binary(path)['filetype'], 11)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, help='Inspect a built kext executable without loading it')
    args, remaining = parser.parse_known_args()
    if args.binary:
        if remaining:
            parser.error('Unexpected arguments for binary inspection')
        try:
            inspection = verify_binary(args.binary)
        except (OSError, ValueError) as error:
            print('Kernel stack-probe contract failed: ' + str(error), file=sys.stderr)
            raise SystemExit(1)
        print('Real MH_KEXT_BUNDLE: no Darwin userspace stack-probe imports or stubs. '
              'Kernel stack budget and native loading remain unverified.')
    else:
        unittest.main(argv=[sys.argv[0], *remaining])
