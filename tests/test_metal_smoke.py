# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Metal harness boundaries; synthetic fixtures never establish GPU execution."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/metal-smoke-tests-private'
spec = importlib.util.spec_from_file_location('metal_smoke', ROOT / 'tools/metal_smoke_ci.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def temporary_directory():
    # Test setup files are confined to ignored build output; the probed Windows
    # subprocess must leave the explicitly selected empty directory unchanged.
    if not WORK.resolve().is_relative_to((ROOT / 'build').resolve()):
        raise RuntimeError('Test temporary directory selection refused.')
    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    return tempfile.TemporaryDirectory(dir=WORK)


def evidence(state='passed', code='COMPUTE_VERIFIED'):
    complete = state == 'passed'
    value = {'schema': 1, 'context': 'ci-host', 'state': state, 'code': code,
             'architecture': 'x86_64', 'value_count': 65536,
             'results_checked': 65536 if complete else 0, 'mismatches': 0}
    value.update({name: complete for name in checker.FLAGS})
    value.update({name: False for name in checker.FALSE_PROOFS})
    return value


def execute_fixture(kind=2, cpu=0x01000007, entry=None):
    commands_size, code_size = 152 + 24, 32
    text_offset = 32 + commands_size
    segment = struct.pack('<II16sQQQQIIII', 0x19, 152, b'__TEXT', 0x100000000,
                          4096, 0, text_offset + code_size, 7, 5, 1, 0)
    section = struct.pack('<16s16sQQIIIIIIII', b'__text', b'__TEXT',
                          0x100000000 + text_offset, code_size, text_offset,
                          4, 0, 0, 0x80000400, 0, 0, 0)
    main = struct.pack('<IIQQ', 0x80000028, 24, text_offset if entry is None else entry, 0)
    header = struct.pack('<8I', 0xfeedfacf, cpu, 3, kind, 2, commands_size, 0, 0)
    return header + segment + section + main + bytes([0xc3]) * code_size


class MetalBoundaries(unittest.TestCase):
    def parse(self, value, status=0, context='ci-host'):
        return checker.parse_probe(json.dumps(value).encode(), status, context)

    def test_complete_compute_message_is_accepted_without_target_proof(self):
        value = self.parse(evidence())
        self.assertEqual(value['results_checked'], 65536)
        self.assertTrue(value['gpu_execution_verified'])
        self.assertFalse(value['arc130v_backend_verified'])

    def test_no_provider_is_unverified_not_a_compute_success(self):
        value = self.parse(evidence('unverified', 'NO_METAL_PROVIDER'), 2)
        self.assertFalse(value['gpu_execution_verified'])
        self.assertFalse(value['command_submitted'])

    def test_shader_and_command_error_reports_remain_failures(self):
        value = evidence('failed', 'SHADER_LIBRARY_FAILED')
        value['gpu_provider_observed'] = True
        self.assertEqual(self.parse(value, 1)['code'], 'SHADER_LIBRARY_FAILED')
        value = evidence('failed', 'COMMAND_FAILED')
        for name in checker.FLAGS[:5]:
            value[name] = True
        self.assertFalse(self.parse(value, 1)['command_completed'])

    def test_timeout_and_mismatch_require_no_gpu_success(self):
        value = evidence('failed', 'COMMAND_TIMEOUT')
        for name in checker.FLAGS[:4]:
            value[name] = True
        self.assertFalse(self.parse(value, 1)['gpu_execution_verified'])
        value = evidence()
        value.update(state='failed', code='RESULT_MISMATCH', mismatches=1, gpu_execution_verified=False)
        self.assertEqual(self.parse(value, 1)['mismatches'], 1)
        value['mismatches'] = 0
        with self.assertRaises(checker.ProbeError):
            self.parse(value, 1)

    def test_false_gpu_success_and_incomplete_counts_are_rejected(self):
        for change in ({'results_checked': 65535}, {'command_completed': False},
                       {'mismatches': 1}, {'gpu_execution_verified': False}):
            value = evidence()
            value.update(change)
            with self.assertRaises(checker.ProbeError):
                self.parse(value)
        value = evidence('failed', 'COMMAND_TIMEOUT')
        value['gpu_execution_verified'] = True
        with self.assertRaises(checker.ProbeError):
            self.parse(value, 1)

    def test_cpu_fallback_and_target_claims_are_rejected(self):
        for name in checker.FALSE_PROOFS:
            value = evidence()
            value[name] = True
            with self.assertRaises(checker.ProbeError):
                self.parse(value)

    def test_registry_paths_extra_fields_and_wrong_context_are_rejected(self):
        for field in ('registry_id', 'device_name', 'device_path', 'username', 'serial_number'):
            value = evidence()
            value[field] = 'private-data'
            with self.assertRaises(checker.ProbeError):
                self.parse(value)
        with self.assertRaises(checker.ProbeError):
            self.parse(evidence(), context='target-probe')

    def test_malformed_utf8_duplicate_keys_and_oversized_json_are_rejected(self):
        for raw in (b'{invalid', b'\xff', b'{"schema":1,"schema":1}',
                    b' ' * (checker.MAX_OUTPUT + 1), b'[]'):
            with self.assertRaises(checker.ProbeError):
                checker.parse_probe(raw, 0, 'ci-host')

    def test_wrong_json_types_exit_codes_and_phase_order_are_rejected(self):
        for change in ({'code': []}, {'results_checked': True}, {'mismatches': -1},
                       {'schema': True}, {'command_completed': 1}, {'state': {}}):
            value = evidence()
            value.update(change)
            with self.assertRaises(checker.ProbeError):
                self.parse(value)
        with self.assertRaises(checker.ProbeError):
            self.parse(evidence(), 2)
        value = evidence('failed', 'BUFFER_ALLOCATION_FAILED')
        value['buffers_created'] = True
        with self.assertRaises(checker.ProbeError):
            self.parse(value, 1)

    def test_execute_entry_must_be_inside_real_code(self):
        self.assertEqual(checker.inspect_executable(execute_fixture())['filetype'], 'MH_EXECUTE')
        for data in (execute_fixture(kind=1), execute_fixture(cpu=0x0100000c),
                     execute_fixture(entry=0), execute_fixture(entry=999999),
                     execute_fixture()[:-32], b'not-mach-o'):
            with self.assertRaises(checker.ProbeError):
                checker.inspect_executable(data)

    def test_oversized_or_truncated_macho_commands_are_rejected(self):
        value = bytearray(execute_fixture())
        struct.pack_into('<I', value, 36, 4096)
        with self.assertRaises(checker.ProbeError):
            checker.inspect_executable(bytes(value))
        with self.assertRaises(checker.ProbeError):
            checker.inspect_executable(bytes(checker.MAX_BINARY + 1))

    def test_imported_windows_guard_precedes_paths_and_argument_parsing(self):
        output = io.StringIO()
        with mock.patch.object(checker.sys, 'platform', 'win32'), \
             mock.patch.object(checker.argparse, 'ArgumentParser', side_effect=AssertionError('no parser')), \
             mock.patch.object(checker.Path, 'resolve', side_effect=AssertionError('no path')), \
             mock.patch.object(checker.tempfile, 'mkdtemp', side_effect=AssertionError('no writes')), \
             contextlib.redirect_stdout(output):
            status = checker.main(['malformed-args'])
        self.assertEqual(status, 2)
        self.assertFalse(json.loads(output.getvalue())['report_file_written'])

    def test_non_intel_macos_process_is_refused_without_writes(self):
        with mock.patch.object(checker.sys, 'platform', 'darwin'), \
             mock.patch.object(checker.platform, 'machine', return_value='arm64'), \
             mock.patch.object(checker.Path, 'mkdir', side_effect=AssertionError('no mkdir')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checker.main([]), 2)

    def test_atomic_report_failure_preserves_previous_report_and_cleans_temporary(self):
        with temporary_directory() as directory:
            destination = Path(directory) / 'private-report.json'
            destination.write_bytes(b'original-report')
            with mock.patch.object(checker.os, 'replace', side_effect=OSError('private-path')):
                with self.assertRaises(OSError):
                    checker.write_report(destination, {'state': 'unverified'})
            self.assertEqual(destination.read_bytes(), b'original-report')
            self.assertEqual(list(Path(directory).glob('.metal-report-*')), [])

    def test_timeout_limit_is_enforced_before_process_or_spool_creation(self):
        with mock.patch.object(checker.subprocess, 'Popen', side_effect=AssertionError('no process')), \
             mock.patch.object(checker.tempfile, 'TemporaryFile', side_effect=AssertionError('no spool')):
            for timeout in (0, 61):
                with self.assertRaises(checker.ProbeError):
                    checker.run_step(['never-executed'], WORK, timeout)

    def test_report_selection_cannot_overwrite_repository_source_or_foreign_json(self):
        with mock.patch.object(checker.Path, 'mkdir', side_effect=AssertionError('no directory write')):
            for parent, output in ((ROOT / 'native', ROOT / 'build/report.json'),
                (ROOT / 'build/private', ROOT / 'docs/VALIDATION.json'),
                (ROOT / 'build/private', ROOT.parent / 'metal-smoke-foreign/report.json'),
                (ROOT, ROOT / 'build/report.json')):
                with self.assertRaises(checker.ProbeError):
                    checker.select_private_paths(ROOT, parent, output)

    def test_private_paths_do_not_reuse_or_follow_a_selected_symlink(self):
        expected_parent, expected_report = checker.select_private_paths(ROOT, None, None)
        self.assertEqual(expected_report, ROOT / 'build/metal-smoke-report.json')
        self.assertEqual(expected_parent, ROOT / 'build/metal-smoke-private')
        with mock.patch.object(checker.Path, 'is_symlink', return_value=True):
            with self.assertRaises(checker.ProbeError):
                checker.select_private_paths(ROOT, None, None)

    @unittest.skipUnless(sys.platform == 'win32', 'Actual Windows hostguard only')
    def test_actual_windows_cli_has_no_filesystem_writes(self):
        with temporary_directory() as directory:
            parent = Path(directory)
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/metal_smoke_ci.py'),
                '--context', 'target-probe', '--build-root', str(parent / 'never-build'),
                '--out', str(parent / 'never-report.json')], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(list(parent.iterdir()), [])
            self.assertEqual(result.stderr, b'')
            report = json.loads(result.stdout)
            self.assertFalse(report['report_file_written'])
            self.assertFalse(report['gpu_execution_verified'])


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MetalBoundaries)
    case_names = [case.id().rsplit('.', 1)[-1] for case in suite]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failures = {case.id().rsplit('.', 1)[-1] for case, _ in result.failures}
    errors = {case.id().rsplit('.', 1)[-1] for case, _ in result.errors}
    skipped = {case.id().rsplit('.', 1)[-1] for case, _ in result.skipped}
    selected_sources = (*checker.SOURCE_NAMES, 'tests/test_metal_smoke.py')
    source_commit = None
    try:
        commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                capture_output=True, timeout=5, check=False)
        if commit.returncode == 0 and re.fullmatch(rb'[a-f0-9]{40}\s*', commit.stdout):
            source_commit = commit.stdout.decode('ascii').strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    report = {
        'schema': 1,
        'test_type': 'synthetic protocol, failure and real Windows hostguard boundaries',
        'host_platform': sys.platform,
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'successful': result.wasSuccessful(),
        'native_builds': 0, 'native_gpu_execution_tests': 0,
        'gpu_execution_verified': False, 'arc130v_backend_verified': False,
        'target_hardware_identity_verified': False,
        'actual_windows_cli_checked': sys.platform == 'win32' and
            'test_actual_windows_cli_has_no_filesystem_writes' not in failures | errors | skipped,
        'source_commit': source_commit,
        'source_files_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                for name in selected_sources},
        'test_cases': [{'name': name, 'state': 'failed' if name in failures else
                        'error' if name in errors else 'skipped' if name in skipped else 'passed'}
                       for name in case_names],
    }
    if len(case_names) != 18 or len(json.dumps(report).encode('utf-8')) > 16384:
        raise SystemExit('Metal harness report bound exceeded.')
    checker.write_report(ROOT / 'tests/metal-smoke-test-report.json', report)
    raise SystemExit(0 if result.wasSuccessful() else 1)
