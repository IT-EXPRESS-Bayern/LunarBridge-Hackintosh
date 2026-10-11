# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Offline parser/privacy/cleanup boundaries; these fixtures execute no GPU code."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/metal-shader-tests-private'
spec = importlib.util.spec_from_file_location('metal_shader', ROOT / 'tools/metal_shader_ci.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def temporary_directory():
    WORK.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=WORK)


def air_fixture():
    return b'BC\xc0\xde' + bytes(12), (
        b'; ModuleID = \'/Users/private/input.air\'\n'
        b'target triple = "air64-apple-macosx26.0.0"\n'
        b'define void @lunarbridge_xe2_probe(ptr %a) { ret void }\n'
        b'!air.kernel = !{!0}\n'
        b'!1 = !{!"air.buffer", !"air.buffer", !"air.buffer"}\n')


def instruction(opcode, *operands):
    return ((len(operands) + 1) << 16 | opcode, *operands)


def spirv_fixture(model=5, memory=(0, 1), bound=32):
    # This is only a bounded-parser fixture, not a spirv-val/GPU success claim.
    name = struct.unpack('<2I', b'main\0\0\0\0')
    words = [0x07230203, 0x10300, 0, bound, 0]
    words += instruction(17, 1)
    words += instruction(14, *memory)
    words += instruction(15, model, 1, *name)
    words += instruction(16, 1, 17, 64, 1, 1)
    for index, identity in enumerate((2, 3, 4)):
        words += instruction(71, identity, 34, 0)
        words += instruction(71, identity, 33, index)
        words += instruction(59, 10, identity, 12)
    return struct.pack('<' + str(len(words)) + 'I', *words)


def reflection_fixture():
    return {'reflection_version': 55, 'stage': 'Kernel', 'entry_point': checker.ENTRY,
            'local_size': [64, 1, 1], 'kernel_dispatch': 'Workgroups', 'bindings': [
                {'kind': 'Buffer', 'metal_index': index, 'descriptor': {'set': 0, 'binding': index, 'count': 1},
                 'address_space': 2 if index == 2 else 1, 'access': 'ReadWrite' if index == 1 else 'ReadOnly',
                 'footprint': {'static_ranges': [], 'strided_accesses': [], 'has_unbounded_access': True},
                 'extent': {'Object': {'bytes': 4}} if index == 2 else 'Unbounded'}
                for index in range(3)]}


def fake_process(output=b'ok', error=b'', waits=None):
    process = mock.Mock(pid=4321, returncode=0)
    process.stdout, process.stderr = io.BytesIO(output), io.BytesIO(error)
    if waits is not None:
        process.wait.side_effect = waits
    return process


class ShaderBoundaries(unittest.TestCase):
    def parse_reflection(self, value):
        return checker.inspect_reflection(json.dumps(value).encode())

    def test_air_container_and_required_metadata_are_checked(self):
        data, llvm = air_fixture()
        self.assertTrue(checker.inspect_air(data, llvm)['kernel_metadata_checked'])
        wrapped = struct.pack('<5I', 0x0b17c0de, 0, 20, len(data), 0xffffffff) + data
        self.assertEqual(checker.inspect_air(wrapped, llvm)['payload_bytes'], len(data))

    def test_air_wrapper_offset_size_and_bitcode_magic_are_bounded(self):
        data, llvm = air_fixture()
        for candidate in (b'bad', data[:-12], struct.pack('<5I', 0x0b17c0de, 0, 4096, 16, 0) + data,
                          struct.pack('<5I', 0x0b17c0de, 0, 20, 1000000, 0) + data,
                          bytes(checker.MAX_FILE + 1)):
            with self.assertRaises(checker.ShaderError):
                checker.inspect_air(candidate, llvm)

    def test_air_requires_macos_kernel_entry_and_three_buffers(self):
        data, llvm = air_fixture()
        for text in (llvm.replace(b'air64-apple-macosx', b'spirv64-unknown-vulkan'),
                     llvm.replace(b'!air.kernel', b'!air.vertex'),
                     llvm.replace(b'@lunarbridge_xe2_probe', b'@other_entry'),
                     llvm.replace(b'!"air.buffer"', b'!"unused.buffer"'), b'\xff'):
            with self.assertRaises(checker.ShaderError):
                checker.inspect_air(data, text)

    def test_air_private_metadata_is_refused_but_moduleid_is_not_published(self):
        data, llvm = air_fixture()
        self.assertNotIn('Users', json.dumps(checker.inspect_air(data, llvm)))
        with self.assertRaisesRegex(checker.ShaderError, '^AIR_PRIVATE_PATH_METADATA$'):
            checker.inspect_air(data, llvm + b'!2 = !{!"/Users/secret/shader.metal"}\n')

    def test_shader_spirv_entry_memory_local_size_and_bindings_are_checked(self):
        value = checker.inspect_spirv(spirv_fixture())
        self.assertEqual(value['descriptor_bindings'], [0, 1, 2])
        self.assertEqual(value['local_size'], [64, 1, 1])
        self.assertNotIn('gpu_execution_verified', value)

    def test_spirv_bad_magic_bound_version_and_truncation_are_refused(self):
        for word, new in ((0, 0), (1, 0x10600), (3, 65537), (4, 1)):
            data = bytearray(spirv_fixture())
            struct.pack_into('<I', data, word * 4, new)
            with self.assertRaises(checker.ShaderError):
                checker.inspect_spirv(bytes(data))
        for data in (spirv_fixture()[:-1], spirv_fixture()[:-4], bytes(checker.MAX_SPIRV + 4)):
            with self.assertRaises(checker.ShaderError):
                checker.inspect_spirv(data)

    def test_spirv_instruction_zero_length_and_past_end_are_refused(self):
        for header in (17, (65535 << 16) | 17):
            data = bytearray(spirv_fixture())
            struct.pack_into('<I', data, 20, header)
            with self.assertRaises(checker.ShaderError):
                checker.inspect_spirv(bytes(data))

    def test_opencl_kernel_and_physical_memory_models_are_not_shader_success(self):
        for data in (spirv_fixture(model=6), spirv_fixture(memory=(2, 2))):
            with self.assertRaises(checker.ShaderError):
                checker.inspect_spirv(data)
        data = bytearray(spirv_fixture())
        struct.pack_into('<I', data, 24, 6)
        with self.assertRaises(checker.ShaderError):
            checker.inspect_spirv(bytes(data))

    def test_spirv_wrong_buffer_layout_and_workgroup_size_are_refused(self):
        data = bytearray(spirv_fixture())
        words = list(struct.unpack('<' + str(len(data) // 4) + 'I', data))
        offset = words.index((6 << 16) | 16)
        words[offset + 3] = 32
        with self.assertRaises(checker.ShaderError):
            checker.inspect_spirv(struct.pack('<' + str(len(words)) + 'I', *words))
        words = list(struct.unpack('<' + str(len(data) // 4) + 'I', data))
        offset = next(index for index in range(len(words) - 3) if words[index:index + 3] == [(4 << 16) | 71, 4, 33])
        words[offset + 3] = 0
        with self.assertRaises(checker.ShaderError):
            checker.inspect_spirv(struct.pack('<' + str(len(words)) + 'I', *words))

    def test_exact_reflection_buffers_and_four_byte_count_are_accepted(self):
        value = self.parse_reflection(reflection_fixture())
        self.assertEqual(value['count_bytes'], 4)
        self.assertEqual([item['metal_index'] for item in value['buffers']], [0, 1, 2])

    def test_reflection_wrong_schema_stage_local_size_and_launch_are_refused(self):
        for change in ({'reflection_version': 48}, {'stage': 'Fragment'}, {'entry_point': 'other'},
                       {'local_size': [64, True, 1]}, {'local_size': [32, 1, 1]}, {'kernel_dispatch': 'ThreadsDynamic'}):
            value = reflection_fixture()
            value.update(change)
            with self.assertRaises(checker.ShaderError):
                self.parse_reflection(value)

    def test_reflection_descriptor_access_extent_and_footprint_are_enforced(self):
        for index, change in ((0, {'access': 'ReadWrite'}), (1, {'access': 'ReadOnly'}),
                              (2, {'extent': {'Object': {'bytes': 8}}}), (0, {'footprint': None}),
                              (2, {'address_space': 1}), (0, {'descriptor': {'set': False, 'binding': 0, 'count': 1}})):
            value = reflection_fixture()
            value['bindings'][index].update(change)
            with self.assertRaises(checker.ShaderError):
                self.parse_reflection(value)

    def test_pinned_rust_schema_55_rejects_stale_handbook_version_49(self):
        value = reflection_fixture()
        self.assertEqual(self.parse_reflection(value)['reflection_version'], 55)
        value['reflection_version'] = 49
        with self.assertRaises(checker.ShaderError):
            self.parse_reflection(value)

    def test_footprint_requires_complete_fields_real_boolean_and_bounded_vectors(self):
        for footprint in ({}, None, {'static_ranges': [], 'strided_accesses': []},
                          {'static_ranges': [], 'strided_accesses': [], 'has_unbounded_access': 1},
                          {'static_ranges': [], 'strided_accesses': [], 'has_unbounded_access': False},
                          {'static_ranges': [None] * 4097, 'strided_accesses': [], 'has_unbounded_access': True}):
            value = reflection_fixture()
            value['bindings'][0]['footprint'] = footprint
            with self.assertRaises(checker.ShaderError):
                self.parse_reflection(value)

    def test_count_extent_requires_rust_u32_and_exact_integer_four(self):
        for count in (4.0, True, -1, 1 << 32, '4', None):
            value = reflection_fixture()
            value['bindings'][2]['extent']['Object']['bytes'] = count
            with self.assertRaises(checker.ShaderError):
                self.parse_reflection(value)

    def test_footprint_ranges_reject_wrong_types_and_overflow(self):
        for interval in ({'offset': True, 'size': 4}, {'offset': 0, 'size': 4.0},
                         {'offset': -1, 'size': 4}, {'offset': (1 << 64) - 1, 'size': 4},
                         {'offset': 0, 'size': 0}, {'offset': 0, 'size': 1 << 64},
                         {'offset': 0}, {'offset': 0, 'size': 4, 'unchecked': 9}):
            value = reflection_fixture()
            value['bindings'][2]['footprint']['static_ranges'] = [interval]
            with self.assertRaises(checker.ShaderError):
                self.parse_reflection(value)

    def test_strided_footprint_accesses_require_u64_and_known_ordered_terms(self):
        access = {'base_offset': 0, 'access_size': 4,
                  'terms': [{'source': 'GlobalInvocationIdX', 'stride': 4}]}
        mutations = ({'base_offset': 0.0}, {'access_size': False}, {'access_size': 1 << 64},
                     {'base_offset': (1 << 64) - 1}, {'terms': None},
                     {'terms': [{'source': 'UnknownInvocation', 'stride': 4}]},
                     {'terms': [{'source': 'GlobalInvocationIdX', 'stride': True}]},
                     {'terms': [{'source': 'GlobalInvocationIdX', 'stride': 1 << 64}]},
                     {'terms': [{'source': 'GlobalInvocationIdX', 'stride': 4}] * 2},
                     {'terms': [{'source': 'WorkgroupIdX', 'stride': 4},
                                {'source': 'GlobalInvocationIdX', 'stride': 4}]})
        for change in mutations:
            value = reflection_fixture()
            value['bindings'][0]['footprint']['strided_accesses'] = [dict(access, **change)]
            with self.assertRaises(checker.ShaderError):
                self.parse_reflection(value)

    def test_actual_schema_static_and_affine_footprints_are_accepted(self):
        value = reflection_fixture()
        for index in (0, 1):
            value['bindings'][index]['footprint'] = {
                'static_ranges': [], 'has_unbounded_access': False,
                'strided_accesses': [{'base_offset': 0, 'access_size': 4,
                                      'terms': [{'source': 'GlobalInvocationIdX', 'stride': 4}]}]}
        value['bindings'][2]['footprint'] = {
            'static_ranges': [{'offset': 0, 'size': 4}], 'strided_accesses': [], 'has_unbounded_access': False}
        self.assertTrue(self.parse_reflection(value)['final_module_footprints_present'])

    def test_converter_qualifies_actual_rust_schema_source_not_documentation(self):
        document = b'pub const REFLECTION_VERSION: u32 = 49;'
        with mock.patch.object(checker, 'run_step', side_effect=[(0, checker.CONVERTER_PIN.encode()), (0, b'')]), \
             mock.patch.object(checker, 'read_regular', side_effect=[b'license', b'package', document]):
            with self.assertRaisesRegex(checker.ShaderError, '^CONVERTER_REFLECTION_SCHEMA_MISMATCH$'):
                checker.converter_provenance(ROOT / 'build/external-source', ROOT / 'build/private')

    def test_json_duplicate_keys_nonfinite_utf8_and_size_are_refused(self):
        for raw in (b'{"key":1,"key":2}', b'{"key":NaN}', b'\xff', b'[]',
                    b'{' * 10000, bytes(checker.MAX_FILE + 1)):
            with self.assertRaises(checker.ShaderError):
                checker.safe_json(raw)

    def test_reflection_does_not_export_private_or_escaped_user_paths(self):
        value = reflection_fixture()
        value['unexpected_private_field'] = '/Users/sensitive/example'
        with self.assertRaises(checker.ShaderError):
            self.parse_reflection(value)
        raw = json.dumps(value).encode().replace(b'/Users/', b'\\u002fUsers\\u002f')
        with self.assertRaises(checker.ShaderError):
            checker.inspect_reflection(raw)

    def test_nonregular_and_oversized_files_are_refused_before_open(self):
        with temporary_directory() as name:
            directory = Path(name)
            with mock.patch.object(checker.os, 'open', side_effect=AssertionError('must not open directory')):
                with self.assertRaises(checker.ShaderError):
                    checker.read_regular(directory)
            candidate = directory / 'large'
            candidate.write_bytes(b'12345678')
            with mock.patch.object(checker.os, 'open', side_effect=AssertionError('must not open oversized file')):
                with self.assertRaises(checker.ShaderError):
                    checker.read_regular(candidate, 4)
            if os.name == 'posix':
                fifo = directory / 'fifo'
                os.mkfifo(fifo)
                with self.assertRaises(checker.ShaderError):
                    checker.read_regular(fifo)

    def test_paths_must_be_ignored_build_outputs_and_never_source_or_efi(self):
        for name in (ROOT / 'tools/report.json', ROOT / 'build', ROOT / 'build/EFI/report',
                     ROOT / 'build/driver.kext/report', ROOT.parent / 'other/report'):
            with self.assertRaises(checker.ShaderError):
                checker.private_path(name, ROOT)

    def test_imported_windows_and_arm_hostguards_precede_parser_and_writes(self):
        for system, architecture in (('win32', 'AMD64'), ('darwin', 'arm64')):
            output = io.StringIO()
            with mock.patch.object(checker.sys, 'platform', system), \
                 mock.patch.object(checker.platform, 'machine', return_value=architecture), \
                 mock.patch.object(checker.argparse, 'ArgumentParser', side_effect=AssertionError('no parser')), \
                 mock.patch.object(checker.Path, 'mkdir', side_effect=AssertionError('no writes')), \
                 contextlib.redirect_stdout(output):
                status = checker.main(['--invalid-argument'])
            self.assertEqual(status, 2)
            self.assertFalse(json.loads(output.getvalue())['report_file_written'])

    def test_actual_windows_guard_creates_no_outputs_or_bytecode(self):
        if sys.platform == 'win32':
            with temporary_directory() as name:
                directory = Path(name)
                before = list(directory.iterdir())
                process = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/metal_shader_ci.py'),
                                          '--artifact-root', str(directory / 'must-not-exist')],
                                         cwd=directory, capture_output=True, timeout=15)
                self.assertEqual(process.returncode, 2)
                value = json.loads(process.stdout)
                self.assertEqual(value['state'], 'unsupported-host')
                self.assertFalse(value['report_file_written'])
                self.assertFalse(value['gpu_execution_verified'])
                self.assertEqual(list(directory.iterdir()), before)
        else:
            # Mac tests never relabel the Windows guard as native compilation.
            with mock.patch.object(checker.sys, 'platform', 'win32'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checker.main(), 2)

    def test_step_deadlines_are_bounded_and_windows_process_launch_is_refused(self):
        with mock.patch.object(checker.subprocess, 'Popen', side_effect=AssertionError('no process')):
            for value in (0, -1, 61, 301, True):
                with self.assertRaises(checker.ShaderError):
                    checker.run_step(['tool'], ROOT, value)
            with mock.patch.object(checker.os, 'name', 'nt'):
                with self.assertRaisesRegex(checker.ShaderError, '^PROCESS_GROUPS_REQUIRED$'):
                    checker.run_step(['tool'], ROOT, 1)

    def test_timeout_kills_whole_session_and_reaps_parent(self):
        process = fake_process(waits=[subprocess.TimeoutExpired(['tool'], 1), 0])
        with mock.patch.object(checker.os, 'name', 'posix'), \
             mock.patch.object(checker.signal, 'SIGKILL', 9, create=True), \
             mock.patch.object(checker.subprocess, 'Popen', return_value=process) as launch, \
             mock.patch.object(checker.os, 'killpg', create=True) as kill:
            with self.assertRaisesRegex(checker.ShaderError, '^STEP_TIMEOUT$'):
                checker.run_step(['tool'], ROOT, 1)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        kill.assert_called_with(process.pid, 9)
        self.assertEqual([call.kwargs['timeout'] for call in process.wait.call_args_list], [1, 5])
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_success_still_cleans_descendants_and_json_stdout_is_separate(self):
        process = fake_process(output=b'{"valid":1}', error=b'warning with private path')
        with mock.patch.object(checker.os, 'name', 'posix'), \
             mock.patch.object(checker.signal, 'SIGKILL', 9, create=True), \
             mock.patch.object(checker.subprocess, 'Popen', return_value=process), \
             mock.patch.object(checker.os, 'killpg', create=True) as kill:
            status, output = checker.run_step(['tool'], ROOT, 1, stdout_only=True)
        self.assertEqual((status, output), (0, b'{"valid":1}'))
        kill.assert_called_with(process.pid, 9)
        self.assertEqual(process.wait.call_count, 2)

    def test_process_output_and_failed_group_cleanup_cannot_pass(self):
        for process, kill_error, expected in ((fake_process(output=bytes(checker.MAX_CAPTURE + 1)), None, 'STEP_OUTPUT_LIMIT'),
                                               (fake_process(), OSError('sensitive'), 'PROCESS_GROUP_CLEANUP_FAILED'),
                                               (fake_process(waits=[0, subprocess.TimeoutExpired(['tool'], 5)]), None,
                                                'STEP_TERMINATION_UNCONFIRMED')):
            with mock.patch.object(checker.os, 'name', 'posix'), \
                 mock.patch.object(checker.signal, 'SIGKILL', 9, create=True), \
                 mock.patch.object(checker.subprocess, 'Popen', return_value=process), \
                 mock.patch.object(checker.os, 'killpg', create=True, side_effect=kill_error):
                with self.assertRaisesRegex(checker.ShaderError, '^' + expected + '$'):
                    checker.run_step(['tool'], ROOT, 1)

    def test_active_pipe_reader_is_never_synchronously_closed(self):
        process = fake_process()
        reader = mock.Mock()
        reader.is_alive.return_value = True
        process.stdout.close = mock.Mock(side_effect=AssertionError('active pipe must remain owned by reader'))
        process.stderr.close = mock.Mock(side_effect=AssertionError('active pipe must remain owned by reader'))
        with mock.patch.object(checker.os, 'name', 'posix'), \
             mock.patch.object(checker.signal, 'SIGKILL', 9, create=True), \
             mock.patch.object(checker.subprocess, 'Popen', return_value=process), \
             mock.patch.object(checker.os, 'killpg', side_effect=OSError('synthetic cleanup failure'), create=True), \
             mock.patch.object(checker.threading, 'Thread', return_value=reader):
            with self.assertRaisesRegex(checker.ShaderError, '^STEP_OUTPUT_DRAIN_UNCONFIRMED$'):
                checker.run_step(['synthetic'], ROOT, 1)
        process.stdout.close.assert_not_called()
        process.stderr.close.assert_not_called()
        self.assertEqual([call.kwargs['timeout'] for call in reader.join.call_args_list], [5, 5])

    def test_real_held_buffered_pipes_return_cleanup_error_within_external_deadline(self):
        # Real buffered readers model a surviving child holding pipe writers.
        # The only actual process is this CPU-only supervised Python worker.
        worker = r'''
import importlib.util
import json
import os
import sys
from unittest import mock
spec = importlib.util.spec_from_file_location('pipe_boundary', sys.argv[1])
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
read_out, write_out = os.pipe()
read_err, write_err = os.pipe()
process = mock.Mock(pid=4321, returncode=0)
process.stdout = os.fdopen(read_out, 'rb')
process.stderr = os.fdopen(read_err, 'rb')
try:
    with mock.patch.object(checker.os, 'name', 'posix'), \
         mock.patch.object(checker.signal, 'SIGKILL', 9, create=True), \
         mock.patch.object(checker.subprocess, 'Popen', return_value=process), \
         mock.patch.object(checker.os, 'killpg', side_effect=OSError('synthetic'), create=True):
        try:
            checker.run_step(['synthetic-no-native-tool'], '.', 1)
        except checker.ShaderError as error:
            print(json.dumps({'code': str(error), 'native_tool_executed': False}))
        else:
            raise AssertionError('cleanup must not succeed')
finally:
    os.close(write_out)
    os.close(write_err)
'''
        with temporary_directory() as name:
            result = subprocess.run([sys.executable, '-B', '-c', worker, str(ROOT / 'tools/metal_shader_ci.py')],
                                    cwd=name, stdin=subprocess.DEVNULL, capture_output=True, timeout=13)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {'code': 'STEP_OUTPUT_DRAIN_UNCONFIRMED',
                                                    'native_tool_executed': False})

    def test_atomic_report_failure_preserves_existing_bytes_and_cleans_temporary(self):
        with temporary_directory() as name:
            directory = Path(name)
            target = directory / 'report.json'
            target.write_bytes(b'old report')
            with mock.patch.object(checker.os, 'replace', side_effect=OSError('/Users/private')):
                with self.assertRaises(OSError):
                    checker.write_report(target, {'schema': 1})
            self.assertEqual(target.read_bytes(), b'old report')
            self.assertEqual(list(directory.iterdir()), [target])

    def test_cli_report_failure_prints_only_fixed_error_without_private_path(self):
        with temporary_directory() as name:
            directory = Path(name)
            output = io.StringIO()
            with mock.patch.object(checker.sys, 'platform', 'darwin'), \
                 mock.patch.object(checker.platform, 'machine', return_value='x86_64'), \
                 mock.patch.object(checker, 'converter_provenance', side_effect=checker.ShaderError('CONVERTER_SOURCE_PIN_MISMATCH')), \
                 mock.patch.object(checker, 'write_report', side_effect=OSError('/Users/PRIVATE-ID/report.json')), \
                 contextlib.redirect_stdout(output):
                status = checker.main(['--context', 'ci-host', '--converter-source', str(directory / 'source'),
                                       '--artifact-root', str(directory / 'artifacts')])
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(output.getvalue())['code'], 'REPORT_WRITE_FAILED')
            self.assertNotIn('PRIVATE-ID', output.getvalue())
            self.assertNotIn(name, output.getvalue())

    def test_dependency_graph_checksums_license_and_private_paths_are_qualified(self):
        origin = 'registry+https://github.com/rust-lang/crates.io-index'
        lock = ('version = 4\n[[package]]\nname = "metal2vulkan"\nversion = "0.1.0"\n'
                '[[package]]\nname = "serde"\nversion = "1.0.0"\nsource = "' + origin + '"\n'
                'checksum = "' + 'a' * 64 + '"\n').encode()
        metadata = {'packages': [{'id': 'root', 'name': 'metal2vulkan', 'version': '0.1.0',
                                 'license': 'LGPL-3.0-or-later', 'source': None},
                                {'id': 'dep', 'name': 'serde', 'version': '1.0.0',
                                 'license': 'MIT OR Apache-2.0', 'source': origin}],
                    'resolve': {'root': 'root', 'nodes': [{'id': 'root', 'dependencies': ['dep']},
                                                        {'id': 'dep', 'dependencies': []}]}}
        value = checker.dependency_provenance(lock, json.dumps(metadata).encode())
        self.assertEqual(len(value['resolved_packages']), 2)
        self.assertNotIn('root', json.dumps(value))
        for mutation in ('checksum', 'license', 'source'):
            bad = copy.deepcopy(metadata)
            candidate = lock
            if mutation == 'checksum':
                candidate = lock.replace(b'a' * 64, b'not-a-checksum')
            elif mutation == 'license':
                bad['packages'][1]['license'] = None
            else:
                bad['packages'][1]['source'] = 'git+https://user:token@example.invalid/repo'
            with self.assertRaises(checker.ShaderError):
                checker.dependency_provenance(candidate, json.dumps(bad).encode())

    def test_msl_and_opencl_probe_preserve_the_same_owned_integer_operation(self):
        msl = (ROOT / 'gpu-toolchain/xe2_compute_probe.metal').read_text()
        opencl = (ROOT / 'gpu-toolchain/xe2_integer_probe.cl').read_text()
        operation = lambda source: re.sub(r'\s+', '', re.search(r'output\[index\]\s*=\s*(.*?);', source, re.S).group(1))
        self.assertEqual(operation(msl), operation(opencl).replace('(uint)index', 'index'))
        self.assertIn('index < count', msl)
        self.assertIn('constant uint &count [[buffer(2)]]', msl)


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append({'name': test.id().rsplit('.', 1)[-1], 'state': 'passed'})


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ShaderBoundaries)
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    commit = os.environ.get('GITHUB_SHA', '')
    report = {'schema': 1, 'host_platform': sys.platform, 'source_commit': commit if re.fullmatch(r'[0-9a-f]{40}', commit) else None,
              'source_files_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in checker.SOURCE_NAMES},
              'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': len(result.skipped), 'successful': result.wasSuccessful(), 'test_cases': result.cases,
              'actual_windows_hostguard_checked': sys.platform == 'win32',
              'process_group_checks': 'mocked-offline-boundaries', 'native_compilation_tests': 0,
              'shader_translation_verified': False, 'gpu_execution_verified': False,
              'metal_provider_registered': False, 'arc130v_backend_verified': False}
    checker.write_report(ROOT / 'tests/metal-shader-test-report.json', report)
    raise SystemExit(0 if result.wasSuccessful() else 1)
