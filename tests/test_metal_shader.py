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
import textwrap
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


def lock_fixture():
    return ('version = 4\n[[package]]\nname = "metal2vulkan"\nversion = "0.1.0"\n'
            '[[package]]\nname = "serde"\nversion = "1.0.0"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
            'checksum = "' + 'a' * 64 + '"\n').encode()


class ShaderBoundaries(unittest.TestCase):
    def parse_reflection(self, value):
        return checker.inspect_reflection(json.dumps(value).encode())

    def partial_air_cli(self, *, outcomes=None, write_lock=True, llvm=None,
                        mutate_build_lock=False, mutate_metadata_lock=False, invalid_air=False):
        # CPU-only fixtures exercise preservation/failure ordering. Every
        # scenario remains unverified; these bytes are not real compiler proof.
        with temporary_directory() as name:
            directory = Path(name)
            source = directory / 'source'
            source.mkdir()
            (source / 'Cargo.toml').write_bytes(b'[package]\nname="offline-fixture"\n')
            tools = {tool: directory / tool for tool in ('cargo', 'rustc', 'llvm-dis', 'spirv-val', 'xcrun', 'metal')}
            for path in tools.values():
                path.write_bytes(b'synthetic offline executable fixture')
            air, default_llvm = air_fixture()
            default_llvm = default_llvm.replace(b'/Users/private/input.air', b'xe2_compute_probe.air')
            llvm = default_llvm if llvm is None else llvm
            calls, outcomes = [], outcomes or {}
            versions = {'cargo': b'cargo 1.98.0', 'rustc': b'rustc 1.98.0',
                        'llvm-dis': b'LLVM 22.1.8', 'spirv-val': b'SPIRV-Tools 2026.3'}

            def step(command, cwd, timeout=60, **options):
                executable = Path(command[0]).name
                if command[1:] == ['--version']:
                    stage, value = executable + '-version', (0, versions[executable])
                elif '-find' in command:
                    stage, value = 'metal-discovery', (0, str(tools['metal']).encode())
                elif 'metal' in command and '--version' in command:
                    stage, value = 'metal-version', (0, b'Metal version 32023.883')
                elif '--show-sdk-version' in command:
                    stage, value = 'macos-sdk-version', (0, b'26.4')
                elif executable == 'xcrun' and '-c' in command:
                    stage, value = 'msl-to-air', (0, b'')
                    (cwd / 'xe2_compute_probe.air').write_bytes(b'invalid' if invalid_air else air)
                elif executable == 'llvm-dis':
                    stage, value = 'air-llvm-dis', (0, b'')
                    (cwd / 'own-air.ll').write_bytes(llvm)
                elif command[1] == 'generate-lockfile':
                    stage, value = 'cargo-resolve', (0, b'')
                    if write_lock:
                        (source / 'Cargo.lock').write_bytes(lock_fixture())
                elif command[1] == 'build':
                    stage, value = 'cargo-build', checker.ShaderError('STEP_TIMEOUT')
                    if mutate_build_lock:
                        (source / 'Cargo.lock').write_bytes(lock_fixture() + b'\n')
                    target = cwd / 'cargo-target/release/metal2vulkan'
                    target.parent.mkdir(parents=True)
                    target.write_bytes(b'synthetic offline converter fixture')
                elif command[1] == 'metadata':
                    stage, value = 'cargo-metadata', checker.ShaderError('STEP_TIMEOUT')
                    if mutate_metadata_lock:
                        (source / 'Cargo.lock').write_bytes(lock_fixture() + b'\n')
                else:
                    raise AssertionError('No full shader/GPU success is permitted in this fixture')
                calls.append({'stage': stage, 'timeout': timeout, 'command': command})
                if 'progress' in options:
                    monitor = checker.CargoProgress()
                    monitor.observe(b'Updating crates.io index' if stage == 'cargo-resolve' else b'Compiling PRIVATE-CRATE v1 /Users/PRIVATE-ID')
                    monitor.finish(options['progress'], 0.125)
                selected = outcomes.get(stage, value)
                if isinstance(selected, Exception):
                    raise selected
                return selected

            output = io.StringIO()
            with mock.patch.object(checker.sys, 'platform', 'darwin'), \
                 mock.patch.object(checker.platform, 'machine', return_value='x86_64'), \
                 mock.patch.dict(checker.os.environ, {'GITHUB_ACTIONS': 'true', 'CI': 'true', 'GITHUB_SHA': 'a' * 40}), \
                 mock.patch.object(checker.shutil, 'which', side_effect=lambda tool: str(tools[Path(tool).name])), \
                 mock.patch.object(checker, 'converter_provenance', return_value=(
                     {'commit': checker.CONVERTER_PIN}, b'synthetic offline license fixture')), \
                 mock.patch.object(checker, 'run_step', side_effect=step), contextlib.redirect_stdout(output):
                status = checker.main(['--context', 'ci-host', '--converter-source', str(source),
                                       '--artifact-root', str(directory / 'artifacts')])
            files = {path.name: path.read_bytes() for path in (directory / 'artifacts').iterdir()}
            report = json.loads(files['report.json'])
            self.assertEqual(status, 2)
            self.assertEqual(report['state'], 'unverified')
            for key in ('shader_translation_verified', 'gpu_execution_verified', 'metal_provider_registered',
                        'arc130v_backend_verified', 'target_hardware_identity_verified',
                        'display_acceleration_verified', 'cpu_fallback_used'):
                self.assertIs(report[key], False)
            for marker in (name, 'PRIVATE-CRATE', 'PRIVATE-ID'):
                self.assertNotIn(marker, json.dumps(report))
                self.assertNotIn(marker, output.getvalue())
            return report, files, calls

    def metal_probe_cli(self, context, *, allow=False, outcomes=None, authorized_ci=True):
        # Offline control-flow fixtures. No compiler/component installer runs,
        # and every scenario stops before compilation can claim success.
        with temporary_directory() as name:
            directory = Path(name)
            tool_names = ('cargo', 'rustc', 'llvm-dis', 'spirv-val', 'xcrun', 'metal')
            tools = {tool: directory / tool for tool in tool_names}
            for path in tools.values():
                path.write_bytes(b'synthetic offline tool qualification fixture')
            calls, outcomes = [], outcomes or {}
            versions = {'cargo': b'cargo 1.98.0', 'rustc': b'rustc 1.98.0',
                        'llvm-dis': b'LLVM 22.1.8', 'spirv-val': b'SPIRV-Tools 2026.3'}

            def step(command, cwd, timeout=60, **options):
                after = any(item['stage'] == 'ci-metal-toolchain-download' for item in calls)
                executable = Path(command[0]).name
                if executable in versions:
                    stage, value = executable + '-version', (0, versions[executable])
                elif executable == 'xcodebuild':
                    self.assertEqual(command, ['xcodebuild', '-downloadComponent', 'MetalToolchain'])
                    stage, value = 'ci-metal-toolchain-download', (0, b'')
                    self.assertEqual(options['slow_step'], 'toolchain-download')
                elif '-find' in command:
                    stage = 'metal-discovery' + ('-after-download' if after else '')
                    value = (0, str(tools['metal']).encode())
                    self.assertTrue(options['stdout_only'])
                elif 'metal' in command:
                    stage = 'metal-version' + ('-after-download' if after else '')
                    value = (0, b'Metal version 32023.27')
                elif '--show-sdk-version' in command:
                    stage, value = 'macos-sdk-version', (1, b'')
                else:
                    raise AssertionError('No native compilation is permitted in this fixture')
                calls.append({'stage': stage, 'timeout': timeout})
                value = outcomes.get(stage, value)
                if isinstance(value, Exception):
                    raise value
                return value

            environment = {'GITHUB_ACTIONS': 'true' if authorized_ci else 'false',
                           'CI': 'true', 'GITHUB_SHA': 'a' * 40}
            output = io.StringIO()
            arguments = ['--context', context, '--converter-source', str(directory / 'source'),
                         '--artifact-root', str(directory / 'artifacts')]
            if allow:
                arguments.append('--allow-ci-metal-download')
            with mock.patch.object(checker.sys, 'platform', 'darwin'), \
                 mock.patch.object(checker.platform, 'machine', return_value='x86_64'), \
                 mock.patch.dict(checker.os.environ, environment), \
                 mock.patch.object(checker.shutil, 'which', side_effect=lambda tool: str(tools[Path(tool).name])), \
                 mock.patch.object(checker, 'converter_provenance', return_value=(
                     {'commit': checker.CONVERTER_PIN}, b'synthetic offline license fixture')), \
                 mock.patch.object(checker, 'run_step', side_effect=step), \
                 contextlib.redirect_stdout(output):
                status = checker.main(arguments)
            report = json.loads((directory / 'artifacts/report.json').read_text())
            self.assertEqual(status, 2)
            self.assertEqual(report['state'], 'unverified')
            for key in ('converter_build_verified', 'air_compilation_verified', 'shader_translation_verified',
                        'gpu_execution_verified', 'metal_provider_registered', 'arc130v_backend_verified',
                        'target_hardware_identity_verified', 'display_acceleration_verified', 'cpu_fallback_used'):
                self.assertIs(report[key], False)
            self.assertNotIn(name, json.dumps(report))
            self.assertNotIn(name, output.getvalue())
            return report, calls

    def test_cold_metal_probe_timeout_has_exact_stage_and_sixty_second_budget(self):
        with temporary_directory() as name:
            directory = Path(name)
            executable = directory / 'metal'
            executable.write_bytes(b'synthetic executable fixture')
            for stage, results in (
                ('metal-discovery', [checker.ShaderError('STEP_TIMEOUT')]),
                ('metal-version', [(0, str(executable).encode()), checker.ShaderError('STEP_TIMEOUT')])):
                report = {}
                with mock.patch.object(checker, 'run_step', side_effect=results) as runner:
                    with self.assertRaisesRegex(checker.ShaderError, '^STEP_TIMEOUT$'):
                        checker.probe_metal('synthetic-xcrun', directory, report)
                self.assertEqual(report['phase'], stage)
                self.assertEqual(report['command_steps'][-1], {
                    'stage': stage, 'timeout_seconds': 60, 'timed_out': True,
                    'completed': False, 'returncode': None, 'code': 'STEP_TIMEOUT'})
                self.assertTrue(all(call.kwargs['timeout'] == 60 for call in runner.call_args_list))
                self.assertNotIn(name, json.dumps(report))
            with mock.patch.object(checker, 'run_step', return_value=(0, b'cargo 1.98.0')) as runner:
                checker.qualify_tool('cargo', ['synthetic-cargo', '--version'], directory, executable)
            self.assertEqual(runner.call_args.kwargs['timeout'], 15)
            with mock.patch.object(checker.subprocess, 'Popen', side_effect=AssertionError('no process')):
                for timeout in (0, True, 61):
                    with self.assertRaisesRegex(checker.ShaderError, '^STEP_TIMEOUT_INVALID$'):
                        checker.qualify_tool('metal', ['synthetic', '--version'], directory, timeout=timeout)

    def test_initial_timeout_on_authorized_ci_attempts_one_bounded_component_download(self):
        for stage, outcome, trigger in (
            ('metal-discovery', checker.ShaderError('STEP_TIMEOUT'), 'STEP_TIMEOUT'),
            ('metal-version', checker.ShaderError('STEP_TIMEOUT'), 'STEP_TIMEOUT'),
            ('metal-discovery', (1, b'unavailable'), 'XCODE_METAL_TOOLCHAIN_MISSING'),
            ('metal-version', (0, b'no installed toolchain'), 'TOOL_VERSION_UNVERIFIED')):
            report, calls = self.metal_probe_cli('ci-host', allow=True,
                outcomes={stage: outcome})
            download = report['metal_toolchain_download']
            self.assertTrue(download['attempted'])
            self.assertTrue(download['completed'])
            self.assertEqual((download['trigger_stage'], download['trigger_code']), (stage, trigger))
            self.assertEqual([item for item in calls if item['stage'] == 'ci-metal-toolchain-download'],
                             [{'stage': 'ci-metal-toolchain-download', 'timeout': 300}])
            self.assertTrue(all(item['timeout'] == 60 for item in calls if item['stage'].startswith('metal-')))
            self.assertEqual(report['phase'], 'macos-sdk-version')
            self.assertEqual(report['code'], 'MACOS_SDK_UNVERIFIED')
            self.assertEqual([item['name'] for item in report['tools']], ['cargo', 'rustc', 'llvm-dis', 'spirv-val', 'metal'])

    def test_non_ci_or_unapproved_probe_never_downloads_after_initial_timeout(self):
        for context in ('target-probe', 'ci-host'):
            for stage in ('metal-discovery', 'metal-version'):
                report, calls = self.metal_probe_cli(context,
                    outcomes={stage: checker.ShaderError('STEP_TIMEOUT')})
                self.assertEqual((report['phase'], report['code']), (stage, 'STEP_TIMEOUT'))
                self.assertFalse(report['metal_toolchain_download']['attempted'])
                self.assertFalse(any(item['stage'] == 'ci-metal-toolchain-download' for item in calls))
        for context, authorized in (('target-probe', True), ('ci-host', False)):
            report, calls = self.metal_probe_cli(context, allow=True, authorized_ci=authorized)
            self.assertEqual(report['code'], 'METAL_DOWNLOAD_REQUIRES_EPHEMERAL_CI')
            self.assertEqual(calls, [])

    def test_post_download_timeout_failure_or_unconfirmed_cleanup_cannot_pass_or_retry(self):
        cases = (
            ('metal-discovery-after-download', checker.ShaderError('STEP_TIMEOUT'), 'STEP_TIMEOUT'),
            ('metal-version-after-download', checker.ShaderError('STEP_TIMEOUT'), 'STEP_TIMEOUT'),
            ('metal-version-after-download', (1, b'no tool'), 'TOOL_VERSION_UNVERIFIED'),
            ('ci-metal-toolchain-download', checker.ShaderError('STEP_TIMEOUT'), 'STEP_TIMEOUT'),
            ('ci-metal-toolchain-download', (1, b''), 'METAL_TOOLCHAIN_DOWNLOAD_FAILED'))
        for stage, outcome, code in cases:
            report, calls = self.metal_probe_cli('ci-host', allow=True, outcomes={
                'metal-discovery': checker.ShaderError('STEP_TIMEOUT'), stage: outcome})
            self.assertEqual((report['phase'], report['code']), (stage, code))
            self.assertTrue(report['metal_toolchain_download']['attempted'])
            self.assertEqual(sum(item['stage'] == 'ci-metal-toolchain-download' for item in calls), 1)
            self.assertTrue(all(item['stage'] in checker.COMMAND_STAGES for item in report['command_steps']))
        for failure in ('PROCESS_GROUP_CLEANUP_FAILED', 'STEP_TERMINATION_UNCONFIRMED',
                        'STEP_OUTPUT_DRAIN_UNCONFIRMED', '/Users/private-unexpected-error'):
            report, calls = self.metal_probe_cli('ci-host', allow=True, outcomes={
                'metal-discovery': checker.ShaderError(failure)})
            self.assertEqual(report['phase'], 'metal-discovery')
            self.assertEqual(report['code'], failure if failure in checker.COMMAND_ERRORS else 'CONTROLLED_TOOL_OR_IO_FAILURE')
            self.assertFalse(report['metal_toolchain_download']['attempted'])
            self.assertFalse(any(item['stage'] == 'ci-metal-toolchain-download' for item in calls))
            self.assertNotIn('private-unexpected-error', json.dumps(report))

    def test_partial_air_original_bytes_survive_later_cargo_timeout(self):
        report, files, calls = self.partial_air_cli()
        self.assertEqual((report['phase'], report['code']), ('cargo-build', 'STEP_TIMEOUT'))
        self.assertTrue(report['air_compilation_verified'])
        self.assertTrue(report['air_llvm_text_exported'])
        self.assertFalse(report['converter_build_verified'])
        self.assertEqual(set(report['outputs_sha256']), {'xe2_compute_probe.metal', 'xe2_compute_probe.air', 'own-air.ll'})
        for name, digest in report['outputs_sha256'].items():
            self.assertEqual(hashlib.sha256(files[name]).hexdigest(), digest)
        self.assertEqual(files['xe2_compute_probe.metal'], (ROOT / 'gpu-toolchain/xe2_compute_probe.metal').read_bytes())
        self.assertEqual(files['xe2_compute_probe.air'], air_fixture()[0])
        self.assertEqual(files['own-air.ll'], air_fixture()[1].replace(b'/Users/private/input.air', b'xe2_compute_probe.air'))
        self.assertEqual(files['Cargo.lock'], lock_fixture())
        self.assertEqual(report['cargo_lock']['sha256'], hashlib.sha256(lock_fixture()).hexdigest())
        self.assertTrue(report['cargo_lock']['resolver_completed'])
        self.assertTrue(report['cargo_lock']['unchanged_after_build'])
        self.assertIsNone(report['dependencies'])
        stages = [call['stage'] for call in calls]
        self.assertLess(stages.index('air-llvm-dis'), stages.index('cargo-resolve'))
        self.assertLess(stages.index('cargo-resolve'), stages.index('cargo-build'))
        build = next(call for call in calls if call['stage'] == 'cargo-build')
        self.assertIn('--locked', build['command'])
        self.assertEqual(build['timeout'], 900)
        self.assertEqual(report['command_steps'][-1]['progress']['last_category'], 'compile')

    def test_resolver_timeout_preserves_only_actual_existing_lock_snapshot(self):
        for existing in (False, True):
            report, files, calls = self.partial_air_cli(write_lock=existing,
                outcomes={'cargo-resolve': checker.ShaderError('STEP_TIMEOUT')})
            self.assertEqual((report['phase'], report['code']), ('cargo-resolve', 'STEP_TIMEOUT'))
            self.assertTrue(report['air_compilation_verified'])
            self.assertEqual('Cargo.lock' in files, existing)
            self.assertEqual(report['cargo_lock'] is not None, existing)
            if existing:
                self.assertFalse(report['cargo_lock']['resolver_completed'])
                self.assertIsNone(report['cargo_lock']['unchanged_after_build'])
            self.assertNotIn('cargo-build', [call['stage'] for call in calls])
            self.assertEqual(report['command_steps'][-1]['progress']['last_category'], 'index')

    def test_resolver_nonzero_or_missing_lock_never_starts_locked_build(self):
        for options, expected in (({'outcomes': {'cargo-resolve': (101, b'private error')}}, 'CARGO_RESOLUTION_FAILED'),
                                  ({'write_lock': False}, 'CARGO_LOCK_MISSING')):
            report, _, calls = self.partial_air_cli(**options)
            self.assertEqual(report['phase'], 'cargo-resolve')
            self.assertEqual(report['code'], expected)
            self.assertNotIn('cargo-build', [call['stage'] for call in calls])
            self.assertFalse(report['converter_build_verified'])

    def test_locked_build_and_metadata_refuse_changed_lock_without_replacing_snapshot(self):
        for metadata in (False, True):
            options = {'mutate_build_lock': not metadata, 'mutate_metadata_lock': metadata}
            if metadata:
                options['outcomes'] = {'cargo-build': (0, b''), 'cargo-metadata': (0, b'{}')}
            report, files, calls = self.partial_air_cli(**options)
            self.assertEqual(report['code'], 'CARGO_LOCK_CHANGED')
            self.assertEqual(files['Cargo.lock'], lock_fixture())
            self.assertIsNone(report['dependencies'])
            if metadata:
                self.assertIn('--locked', calls[-1]['command'])
            else:
                self.assertFalse(report['cargo_lock']['unchanged_after_build'])

    def test_failed_air_or_disassembly_cannot_start_cargo_or_export_unqualified_bytes(self):
        for options, expected in (({'invalid_air': True}, 'AIR_SIZE_INVALID'),
                                  ({'llvm': air_fixture()[1] + b'!2 = !{!"/Users/PRIVATE-ID/source"}\n'}, 'AIR_PRIVATE_PATH_METADATA'),
                                  ({'outcomes': {'msl-to-air': (1, b'')}}, 'MSL_AIR_COMPILATION_FAILED'),
                                  ({'outcomes': {'air-llvm-dis': (1, b'')}}, 'AIR_LLVM_DIS_FAILED')):
            report, files, calls = self.partial_air_cli(**options)
            self.assertEqual(report['code'], expected)
            self.assertFalse(report['air_compilation_verified'])
            self.assertEqual(report['outputs_sha256'], {})
            self.assertNotIn('cargo-resolve', [call['stage'] for call in calls])
            self.assertNotIn('own-air.ll', files)

    def test_private_moduleid_prevents_original_llvm_export_without_silent_rewrite(self):
        original = air_fixture()[1]
        report, files, calls = self.partial_air_cli(llvm=original)
        self.assertEqual(report['code'], 'AIR_LLVM_EXPORT_PRIVATE_PATH')
        self.assertTrue(report['air_compilation_verified'])
        self.assertFalse(report['air_llvm_text_exported'])
        self.assertEqual(set(report['outputs_sha256']), {'xe2_compute_probe.metal', 'xe2_compute_probe.air'})
        self.assertNotIn('own-air.ll', files)
        self.assertNotIn('cargo-resolve', [call['stage'] for call in calls])
        self.assertIn(b'/Users/private/input.air', original)

    def test_private_air_classification_exports_only_categories_counts_and_original_hashes(self):
        lines = [b'source_filename = "/Users/PRIVATE-ID/shader.metal"',
                 b'!1 = !DIFile(filename: "shader", directory: "/home/PRIVATE-ID")',
                 b'!2 = !DICompileUnit(producer: "/Users/PRIVATE-ID/tool")',
                 b'!3 = !{!"/Users/PRIVATE-ID/metadata"}',
                 b'; unknown /Users/PRIVATE-ID/comment']
        original = b'\n'.join(lines) + b'\n'
        summary = checker.classify_private_air_metadata(original)
        self.assertEqual(summary['original_llvm_text_sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(summary['scope'], 'kernel-metadata')
        self.assertEqual(summary['observations'], 5)
        expected = dict.fromkeys(checker.PRIVATE_IR_CATEGORIES, 0)
        expected.update(source_filename=1, di_file=1, di_compile_unit=1, other_metadata=1, other_ir=1)
        self.assertEqual(summary['categories'], expected)
        self.assertEqual([row['line_sha256'] for row in summary['line_hashes']],
                         [hashlib.sha256(line).hexdigest() for line in lines])
        self.assertFalse(summary['observation_limit_reached'])
        self.assertFalse(summary['classification_incomplete'])
        self.assertNotIn('PRIVATE-ID', json.dumps(summary))
        self.assertNotIn('/Users/', json.dumps(summary))
        self.assertEqual(original, b'\n'.join(lines) + b'\n')

    def test_private_air_classification_follows_bounded_option_and_identity_metadata(self):
        original = (b'!air.compile_options = !{!10}\n!10 = !{!11}\n'
                    b'!11 = !{!10, !"/Users/PRIVATE-ID/option"}\n'
                    b'!llvm.commandline = !{!12}\n!12 = !{!"/home/PRIVATE-ID/option"}\n'
                    b'!llvm.ident = !{!13, !14}\n!13 = !{!"/Users/PRIVATE-ID/compiler"}\n'
                    b'!air.compile_options_extra = !{!"/Users/PRIVATE-ID/unrelated"}\n'
                    b'!14 = !{!"!20"}\n!20 = !{!"/Users/PRIVATE-ID/unreferenced"}\n')
        summary = checker.classify_private_air_metadata(original)
        self.assertEqual(summary['categories']['compiler_option'], 2)
        self.assertEqual(summary['categories']['compiler_identity'], 1)
        self.assertEqual(summary['categories']['other_metadata'], 2)
        self.assertEqual(summary['observations'], 5)
        self.assertFalse(summary['classification_incomplete'])
        self.assertNotIn('PRIVATE-ID', json.dumps(summary))

    def test_private_air_classification_observation_line_and_reference_limits_are_explicit(self):
        original = b'!1 = !{!"/Users/PRIVATE-ID/value"}\n' * (checker.MAX_PRIVATE_IR_OBSERVATIONS + 3)
        summary = checker.classify_private_air_metadata(original)
        self.assertEqual(summary['observations'], checker.MAX_PRIVATE_IR_OBSERVATIONS)
        self.assertTrue(summary['observation_limit_reached'])
        self.assertEqual(len(summary['line_hashes']), checker.MAX_PRIVATE_IR_OBSERVATIONS)
        original = b'\n' * checker.MAX_PRIVATE_IR_LINES + b'!1 = !{!"/Users/PRIVATE-ID/value"}\n'
        summary = checker.classify_private_air_metadata(original)
        self.assertTrue(summary['classification_incomplete'])
        self.assertEqual(summary['observations'], 0)
        with self.assertRaisesRegex(checker.ShaderError, '^AIR_PRIVATE_PATH_METADATA$'):
            checker.inspect_air(air_fixture()[0], air_fixture()[1] + original)
        graph = b'!air.compile_options = !{' + b', '.join(
            ('!' + str(index)).encode() for index in range(checker.MAX_PRIVATE_IR_REFERENCES + 2)) + b'}\n'
        self.assertTrue(checker.classify_private_air_metadata(graph)['classification_incomplete'])

    def test_private_air_classification_input_types_utf8_and_moduleid_scope_are_bounded(self):
        for raw, options in ((b'\xff', {}), (b'', {}), ('private', {}),
                             (bytes(checker.MAX_FILE + 1), {}), (b'valid', {'include_module_id': 1})):
            with self.assertRaises(checker.ShaderError):
                checker.classify_private_air_metadata(raw, **options)
        original = air_fixture()[1]
        summary = checker.classify_private_air_metadata(original)
        self.assertEqual(summary['observations'], 0)
        self.assertFalse(summary['classification_incomplete'])
        exported = checker.classify_private_air_metadata(original, include_module_id=True)
        self.assertEqual(exported['categories']['module_id'], 1)
        self.assertEqual(exported['scope'], 'exported-llvm-text')
        self.assertNotIn('/Users/', json.dumps(exported))

    def test_private_air_cli_retains_bounded_diagnostic_and_refuses_shader_outputs_and_cargo(self):
        original = air_fixture()[1].replace(b'/Users/private/input.air', b'input.air')
        original += b'source_filename = "/Users/PRIVATE-ID/shader.metal"\n'
        report, files, calls = self.partial_air_cli(llvm=original)
        self.assertEqual((report['phase'], report['code']), ('air-llvm-contract', 'AIR_PRIVATE_PATH_METADATA'))
        self.assertEqual(report['air_private_metadata']['categories']['source_filename'], 1)
        self.assertEqual(report['air_private_metadata']['original_llvm_text_sha256'],
                         hashlib.sha256(original).hexdigest())
        self.assertEqual(report['outputs_sha256'], {})
        self.assertNotIn('cargo-resolve', [call['stage'] for call in calls])
        self.assertNotIn('own-air.ll', files)
        self.assertNotIn('PRIVATE-ID', json.dumps(report))
        for flag in ('air_compilation_verified', 'shader_translation_verified', 'gpu_execution_verified',
                     'metal_provider_registered', 'arc130v_backend_verified'):
            self.assertFalse(report[flag])

    def test_private_air_cli_moduleid_diagnostic_preserves_original_air_and_export_refusal(self):
        original = air_fixture()[1]
        report, files, calls = self.partial_air_cli(llvm=original)
        self.assertEqual(report['code'], 'AIR_LLVM_EXPORT_PRIVATE_PATH')
        self.assertEqual(report['air_private_metadata']['categories']['module_id'], 1)
        self.assertEqual(report['air_private_metadata']['original_llvm_text_sha256'],
                         hashlib.sha256(original).hexdigest())
        self.assertEqual(files['xe2_compute_probe.air'], air_fixture()[0])
        self.assertTrue(report['air_compilation_verified'])
        self.assertFalse(report['air_llvm_text_exported'])
        self.assertFalse(report['shader_translation_verified'])
        self.assertNotIn('own-air.ll', files)
        self.assertNotIn('cargo-resolve', [call['stage'] for call in calls])
        self.assertNotIn('/Users/', json.dumps(report))

    def test_lock_snapshot_schema_checksum_duplicate_comment_and_paths_are_bounded(self):
        valid = lock_fixture()
        summary = checker.lock_summary(valid)
        self.assertEqual(summary['sha256'], hashlib.sha256(valid).hexdigest())
        self.assertEqual(summary['package_count'], 2)
        mutations = (valid.replace(b'version = 4', b'version = true'),
            valid.replace(b'version = 4', b'version = 5'), valid + b'\nextra = "private"\n',
            valid.replace(b'a' * 64, b'bad-checksum'),
            valid.replace(b'registry+https://github.com/rust-lang/crates.io-index', b'git+https://user:token@example.invalid'),
            valid + b'\n# /Users/PRIVATE-ID\n', valid + b'\n# token=private\n',
            valid + b'\n[[package]]\nname="metal2vulkan"\nversion="0.1.0"\n',
            valid + b'\ndependencies=["/home/private"]\n',
            valid.replace(b'name = "serde"', b'name = "../private"'),
            valid.replace(b'checksum = "', b'checksum = true\nold_checksum = "'))
        for raw in mutations:
            with self.assertRaises(checker.ShaderError):
                checker.lock_summary(raw)

    def test_progress_categories_fragment_bounds_and_final_snapshot_exclude_raw_text(self):
        collector = checker.CargoProgress()
        stream = checker.CargoProgressStream(collector)
        stream.feed(b'Updat')
        stream.feed(b'ing crates.io index\nDownloading PRIVATE-CRATE\nCompil')
        stream.feed(b'ing PRIVATE-CRATE /Users/PRIVATE-ID\nChecking secret\nFinished release\n')
        stream.feed(b'x' * (checker.MAX_PROGRESS_LINE + 1) + b'\nprivate trailing', final=True)
        target = {}
        collector.finish(target, 1.234)
        self.assertEqual(target['categories'], {'index': 1, 'download': 1, 'compile': 1,
                                               'check': 1, 'finish': 1, 'unknown': 2})
        before = copy.deepcopy(target)
        collector.observe(b'Compiling after-finalization')
        self.assertEqual(target, before)
        self.assertNotIn('PRIVATE', json.dumps(target))
        limited = checker.CargoProgress()
        for _ in range(checker.MAX_PROGRESS_OBSERVATIONS + 3):
            limited.observe(b'Compiling secret')
        summary = {}
        limited.finish(summary, 0.5)
        self.assertEqual(summary['observations'], checker.MAX_PROGRESS_OBSERVATIONS)
        self.assertTrue(summary['observation_limit_reached'])

    def test_optional_progress_survives_actual_supervised_timeout_and_retains_cleanup(self):
        process = fake_process(output=b'Updating crates.io index\n',
                               error=b'Compiling PRIVATE-CRATE /Users/PRIVATE-ID\n',
                               waits=[subprocess.TimeoutExpired(['synthetic'], 1), 0])
        report = {}
        with mock.patch.object(checker.os, 'name', 'posix'), \
             mock.patch.object(checker.signal, 'SIGKILL', 9, create=True), \
             mock.patch.object(checker.subprocess, 'Popen', return_value=process), \
             mock.patch.object(checker.os, 'killpg', create=True) as kill:
            with self.assertRaisesRegex(checker.ShaderError, '^STEP_TIMEOUT$'):
                checker.reported_step('cargo-build', ['synthetic'], ROOT, report, 1,
                                      slow_step='cargo-build', cargo_progress=True)
        detail = report['command_steps'][0]
        self.assertTrue(detail['timed_out'])
        self.assertFalse(detail['completed'])
        self.assertEqual(detail['progress']['categories']['index'], 1)
        self.assertEqual(detail['progress']['categories']['compile'], 1)
        self.assertGreaterEqual(detail['progress']['elapsed_seconds'], 0)
        self.assertNotIn('PRIVATE', json.dumps(report))
        kill.assert_called_with(process.pid, 9)
        self.assertEqual([call.kwargs['timeout'] for call in process.wait.call_args_list], [1, 5])
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_slow_step_budgets_and_progress_authorization_precede_process_launch(self):
        with mock.patch.object(checker.subprocess, 'Popen', side_effect=AssertionError('no process')):
            for timeout, slow in ((901, 'cargo-build'), (301, 'cargo-resolve'), (301, 'toolchain-download'),
                                  (61, None), (900, 'unknown-step'), (True, 'cargo-build')):
                with self.assertRaisesRegex(checker.ShaderError, '^STEP_TIMEOUT_INVALID$'):
                    checker.run_step(['synthetic'], ROOT, timeout, slow_step=slow)
            for progress, slow in (([], 'cargo-build'), ({'raw': 'private'}, 'cargo-build'), ({}, None)):
                with self.assertRaisesRegex(checker.ShaderError, '^PROGRESS_REPORT_INVALID$'):
                    checker.run_step(['synthetic'], ROOT, 1, slow_step=slow, progress=progress)
        for timeout, slow in ((900, 'cargo-build'), (300, 'cargo-resolve'), (60, None)):
            process = fake_process()
            with mock.patch.object(checker.os, 'name', 'posix'), \
                 mock.patch.object(checker.signal, 'SIGKILL', 9, create=True), \
                 mock.patch.object(checker.subprocess, 'Popen', return_value=process), \
                 mock.patch.object(checker.os, 'killpg', create=True):
                checker.run_step(['synthetic'], ROOT, timeout, slow_step=slow)
            self.assertEqual(process.wait.call_args_list[0].kwargs['timeout'], timeout)

    def test_optional_progress_never_closes_active_readers_or_claims_success(self):
        process = fake_process()
        reader = mock.Mock()
        reader.is_alive.return_value = True
        process.stdout.close = mock.Mock(side_effect=AssertionError('reader owns buffered pipe'))
        process.stderr.close = mock.Mock(side_effect=AssertionError('reader owns buffered pipe'))
        progress = {}
        with mock.patch.object(checker.os, 'name', 'posix'), \
             mock.patch.object(checker.signal, 'SIGKILL', 9, create=True), \
             mock.patch.object(checker.subprocess, 'Popen', return_value=process), \
             mock.patch.object(checker.os, 'killpg', create=True), \
             mock.patch.object(checker.threading, 'Thread', return_value=reader):
            with self.assertRaisesRegex(checker.ShaderError, '^STEP_OUTPUT_DRAIN_UNCONFIRMED$'):
                checker.run_step(['synthetic'], ROOT, 1, slow_step='cargo-build', progress=progress)
        process.stdout.close.assert_not_called()
        process.stderr.close.assert_not_called()
        self.assertEqual([call.kwargs['timeout'] for call in reader.join.call_args_list], [5, 5])
        self.assertEqual(progress['observations'], 0)

    def test_atomic_partial_output_failure_preserves_bytes_and_report_hash(self):
        with temporary_directory() as name:
            directory = Path(name)
            path = directory / 'own-air.ll'
            path.write_bytes(b'old verified bytes')
            report = {'outputs_sha256': {'own-air.ll': hashlib.sha256(path.read_bytes()).hexdigest()}}
            original = copy.deepcopy(report)
            with mock.patch.object(checker.os, 'replace', side_effect=OSError('/Users/private')):
                with self.assertRaises(OSError):
                    checker.retain_output(directory, report, 'own-air.ll', b'new bytes')
            self.assertEqual(path.read_bytes(), b'old verified bytes')
            self.assertEqual(report, original)
            self.assertEqual(list(directory.iterdir()), [path])

    def test_actual_workflow_guard_accepts_partial_evidence_and_rejects_tampering(self):
        report, files, _ = self.partial_air_cli()
        workflow = (ROOT / '.github/workflows/metal-shader.yml').read_text()
        self.assertIn('timeout-minutes: 25', workflow)
        block = textwrap.dedent(workflow.split("python3 -B - <<'PY'\n", 1)[1].split('\n          PY', 1)[0])
        with temporary_directory() as name:
            directory = Path(name)
            for source in checker.SOURCE_NAMES:
                target = directory / source
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / source).read_bytes())
            artifacts = directory / 'build/metal-shader-artifacts'
            artifacts.mkdir(parents=True)
            for filename, data in files.items():
                (artifacts / filename).write_bytes(data)
            environment = os.environ | {'GITHUB_SHA': 'a' * 40}
            success = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', block], cwd=directory,
                                     env=environment, capture_output=True, timeout=15)
            self.assertEqual(success.returncode, 0, success.stderr)
            for mutation in ('bytes', 'missing-llvm', 'gpu-claim', 'translation-claim',
                             'progress-extra', 'progress-observation-bool', 'progress-elapsed-bool', 'progress-nondict'):
                candidate = copy.deepcopy(report)
                (artifacts / 'own-air.ll').write_bytes(files['own-air.ll'])
                if mutation == 'bytes':
                    (artifacts / 'own-air.ll').write_bytes(b'tampered IR')
                elif mutation == 'missing-llvm':
                    (artifacts / 'own-air.ll').unlink()
                elif mutation == 'gpu-claim':
                    candidate['gpu_execution_verified'] = True
                elif mutation == 'translation-claim':
                    candidate['shader_translation_verified'] = True
                elif mutation == 'progress-extra':
                    candidate['command_steps'][-1]['progress']['raw'] = '/Users/PRIVATE-ID'
                elif mutation == 'progress-observation-bool':
                    candidate['command_steps'][-1]['progress']['observations'] = True
                elif mutation == 'progress-elapsed-bool':
                    candidate['command_steps'][-1]['progress']['elapsed_seconds'] = True
                else:
                    candidate['command_steps'][-1]['progress'] = []
                (artifacts / 'report.json').write_text(json.dumps(candidate))
                failure = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', block], cwd=directory,
                                         env=environment, capture_output=True, timeout=15)
                self.assertNotEqual(failure.returncode, 0)

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
progress = {} if sys.argv[2] == 'progress' else None
try:
    with mock.patch.object(checker.os, 'name', 'posix'), \
         mock.patch.object(checker.signal, 'SIGKILL', 9, create=True), \
         mock.patch.object(checker.subprocess, 'Popen', return_value=process), \
         mock.patch.object(checker.os, 'killpg', side_effect=OSError('synthetic'), create=True):
        try:
            checker.run_step(['synthetic-no-native-tool'], '.', 1,
                             slow_step='cargo-build' if progress is not None else None, progress=progress)
        except checker.ShaderError as error:
            print(json.dumps({'code': str(error), 'native_tool_executed': False,
                              'progress_enabled': progress is not None}))
        else:
            raise AssertionError('cleanup must not succeed')
finally:
    os.close(write_out)
    os.close(write_err)
'''
        for mode in ('plain', 'progress'):
            with temporary_directory() as name:
                result = subprocess.run([sys.executable, '-B', '-c', worker, str(ROOT / 'tools/metal_shader_ci.py'), mode],
                                        cwd=name, stdin=subprocess.DEVNULL, capture_output=True, timeout=13)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {'code': 'STEP_OUTPUT_DRAIN_UNCONFIRMED',
                                                        'native_tool_executed': False,
                                                        'progress_enabled': mode == 'progress'})

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
