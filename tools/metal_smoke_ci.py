# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Build/run a bounded x86_64 macOS public-Metal test; never install a driver."""
import sys


def unsupported_host():
    # A directly invoked Windows script exits before even importing file helpers.
    print('{"schema":1,"state":"unsupported-host","code":"INTEL_MACOS_REQUIRED",'
          '"build_verified":false,"gpu_execution_verified":false,"report_file_written":false,'
          '"target_hardware_identity_verified":false,"arc130v_backend_verified":false}')
    return 2


if __name__ == '__main__' and sys.platform != 'darwin':
    raise SystemExit(unsupported_host())

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import struct
import subprocess
import tempfile

VALUE_COUNT = 65536
MAX_OUTPUT = 131072
MAX_BINARY = 8 * 1024 * 1024
FLAGS = ('gpu_provider_observed', 'shader_compiled', 'buffers_created', 'command_submitted',
         'completion_handler_observed', 'command_completed', 'gpu_execution_verified')
FALSE_PROOFS = ('cpu_fallback_used', 'target_hardware_identity_verified', 'arc130v_backend_verified',
                'display_acceleration_verified', 'native_driver_support_verified')
CODES = {'COMPUTE_VERIFIED', 'NO_METAL_PROVIDER', 'SHADER_LIBRARY_FAILED', 'SHADER_ENTRY_MISSING',
         'PIPELINE_CREATION_FAILED', 'BUFFER_ALLOCATION_FAILED', 'BUFFER_CONTENTS_UNAVAILABLE',
         'COMMAND_QUEUE_FAILED', 'COMMAND_BUFFER_FAILED', 'ENCODER_CREATION_FAILED',
         'DISPATCH_WIDTH_INVALID', 'COMMAND_TIMEOUT', 'COMMAND_FAILED', 'RESULT_MISMATCH',
         'INVALID_ARGUMENTS'}
FIELDS = {'schema', 'context', 'state', 'code', 'architecture', 'value_count',
          'results_checked', 'mismatches', *FLAGS, *FALSE_PROOFS}
SOURCE_NAMES = ('gpu-prototype/Metal-Minimaltest.mm', 'tools/metal_smoke_ci.py',
                '.github/workflows/metal-smoke.yml', 'docs/METAL_TEST.md')


class ProbeError(ValueError):
    pass


def parse_probe(raw, returncode, context):
    """Only an internally consistent, bounded allowlisted protocol can pass."""
    if not isinstance(raw, bytes):
        raise ProbeError('PROBE_PROTOCOL_INVALID')
    if len(raw) > MAX_OUTPUT:
        raise ProbeError('PROBE_OUTPUT_LIMIT')
    def unique_pairs(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ProbeError('PROBE_DUPLICATE_FIELD')
            result[name] = value
        return result
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_pairs)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ProbeError('PROBE_PROTOCOL_INVALID') from error
    if not isinstance(value, dict) or set(value) != FIELDS or type(returncode) is not int or \
       any(type(value[key]) is not str for key in ('context', 'architecture', 'code', 'state')) or \
       value['context'] != context or \
       value['architecture'] != 'x86_64' or type(value['schema']) is not int or value['schema'] != 1 or \
       type(value['value_count']) is not int or value['value_count'] != VALUE_COUNT or \
       value['code'] not in CODES or value['state'] not in ('passed', 'failed', 'unverified'):
        raise ProbeError('PROBE_PROTOCOL_INVALID')
    if any(type(value[key]) is not bool for key in (*FLAGS, *FALSE_PROOFS)) or \
       any(value[key] for key in FALSE_PROOFS):
        raise ProbeError('PROBE_FORBIDDEN_PROOF')
    checked, mismatches = value['results_checked'], value['mismatches']
    if type(checked) is not int or type(mismatches) is not int or not 0 <= mismatches <= checked <= VALUE_COUNT:
        raise ProbeError('PROBE_RESULT_COUNTS_INVALID')
    phases = [value[key] for key in FLAGS[:-1]]
    if any(phases[index] and not phases[index - 1] for index in range(1, len(phases))) or \
       (checked and not value['command_completed']):
        raise ProbeError('PROBE_PHASE_ORDER_INVALID')
    passed = value['state'] == 'passed'
    if passed:
        if returncode != 0 or value['code'] != 'COMPUTE_VERIFIED' or not all(phases) or \
           checked != VALUE_COUNT or mismatches != 0 or not value['gpu_execution_verified']:
            raise ProbeError('PROBE_SUCCESS_INCOMPLETE')
    elif value['gpu_execution_verified']:
        raise ProbeError('PROBE_FALSE_SUCCESS')
    elif value['state'] == 'unverified':
        if returncode != 2 or value['code'] != 'NO_METAL_PROVIDER' or any(phases) or checked or mismatches:
            raise ProbeError('PROBE_UNVERIFIED_INCONSISTENT')
    elif returncode != 1 or value['code'] in ('COMPUTE_VERIFIED', 'NO_METAL_PROVIDER') or \
         (value['code'] == 'RESULT_MISMATCH' and (checked != VALUE_COUNT or mismatches == 0)):
        raise ProbeError('PROBE_FAILURE_INCONSISTENT')
    return value


def inspect_executable(data):
    """Confirm a real thin Intel MH_EXECUTE with code and LC_MAIN, not an object."""
    if len(data) < 32 or len(data) > MAX_BINARY:
        raise ProbeError('EXECUTABLE_SIZE_INVALID')
    magic, cpu, subtype, kind, count, size, _, reserved = struct.unpack_from('<8I', data)
    if (magic, cpu, subtype, kind, reserved) != (0xfeedfacf, 0x01000007, 3, 2, 0) or \
       not 0 < count <= 256 or size > len(data) - 32:
        raise ProbeError('EXECUTABLE_HEADER_INVALID')
    end, position, text_regions, entry = 32 + size, 32, [], None
    for _ in range(count):
        if position + 8 > end:
            raise ProbeError('EXECUTABLE_COMMAND_INVALID')
        command, length = struct.unpack_from('<2I', data, position)
        if length < 8 or length % 8 or length > end - position:
            raise ProbeError('EXECUTABLE_COMMAND_INVALID')
        if command == 0x19:
            if length < 72:
                raise ProbeError('EXECUTABLE_SEGMENT_INVALID')
            sections = struct.unpack_from('<I', data, position + 64)[0]
            if sections > 128 or length != 72 + sections * 80:
                raise ProbeError('EXECUTABLE_SEGMENT_INVALID')
            for index in range(sections):
                item = position + 72 + index * 80
                name, segment = data[item:item + 16].split(b'\0', 1)[0], data[item + 16:item + 32].split(b'\0', 1)[0]
                if (name, segment) == (b'__text', b'__TEXT'):
                    length_bytes, offset = struct.unpack_from('<QI', data, item + 40)
                    if not length_bytes or offset > len(data) or length_bytes > len(data) - offset:
                        raise ProbeError('EXECUTABLE_TEXT_INVALID')
                    text_regions.append((offset, offset + length_bytes))
        elif command == 0x80000028:
            if length != 24 or entry is not None:
                raise ProbeError('EXECUTABLE_ENTRY_INVALID')
            entry = struct.unpack_from('<Q', data, position + 8)[0]
        position += length
    if position != end or entry is None or not any(begin <= entry < limit for begin, limit in text_regions):
        raise ProbeError('EXECUTABLE_CODE_OR_ENTRY_MISSING')
    return {'architecture': 'x86_64', 'filetype': 'MH_EXECUTE', 'text_and_entry_checked': True}


def private_path(path):
    candidate = Path(path).absolute()
    for current in (candidate, *candidate.parents):
        if current.is_symlink():
            raise ProbeError('PRIVATE_PATH_SYMLINK_REFUSED')
    resolved = candidate.resolve()
    if any(resolved.is_relative_to(Path(root)) for root in ('/System', '/Library', '/usr', '/bin', '/sbin', '/private/var')) or \
       any(part.upper() == 'EFI' or part.lower().endswith(('.app', '.kext', '.framework')) for part in resolved.parts):
        raise ProbeError('PRIVATE_PATH_PROTECTED')
    return resolved


def select_private_paths(root, build_root, output):
    parent = private_path(build_root or root / 'build/metal-smoke-private')
    destination = private_path(output or root / 'build/metal-smoke-report.json')
    protected_sources = [root / name for name in
        ('gpu-prototype', 'native', 'tools', 'docs', '.github', 'tests', 'installer')]
    if parent == root or destination.suffix != '.json' or \
       any(parent.is_relative_to(tree) or destination.is_relative_to(tree) for tree in protected_sources) or \
       not (destination.is_relative_to(parent) or destination.is_relative_to(root / 'build')):
        raise ProbeError('PRIVATE_BUILD_OR_REPORT_SELECTION_INVALID')
    return parent, destination


def run_step(command, directory, timeout):
    if not 1 <= timeout <= 60:
        raise ProbeError('STEP_TIMEOUT_INVALID')
    # Raw compiler/framework messages may contain paths: only a temporary spool,
    # never a published log. Kill the process group on timeout, including clang children.
    with tempfile.TemporaryFile(dir=directory) as spool:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=spool,
                                   stderr=spool, start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as termination:
                raise ProbeError('STEP_TERMINATION_UNCONFIRMED') from termination
            raise ProbeError('STEP_TIMEOUT') from error
        spool.seek(0)
        raw = spool.read(MAX_OUTPUT + 1)
        if len(raw) > MAX_OUTPUT:
            raise ProbeError('STEP_OUTPUT_LIMIT')
        return process.returncode, raw


def safe_version(raw):
    value = raw.decode('ascii', errors='ignore').strip()
    return value if re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,3}', value) else None


def write_report(path, report):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.metal-report-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(report, indent=2) + '\n').encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    # This guard precedes argument parsing, path resolution, mkdir and report writes.
    if sys.platform != 'darwin' or platform.machine() != 'x86_64':
        return unsupported_host()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', choices=('ci-host', 'target-probe'), required=True)
    parser.add_argument('--build-root', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    report = {'schema': 1, 'context': args.context, 'state': 'failed', 'code': 'BUILD_NOT_STARTED',
              'build_verified': False, 'gpu_execution_verified': False, 'cpu_fallback_used': False,
              'target_hardware_identity_verified': False, 'arc130v_backend_verified': False,
              'display_acceleration_verified': False, 'native_driver_support_verified': False,
              'macos_version': safe_version(platform.mac_ver()[0].encode()),
              'source_files_sha256': {}, 'source_commit': None, 'compiler_version': None,
              'sdk_version': None, 'binary_sha256': None, 'executable': None, 'probe': None,
              'frameworks': ['Foundation', 'Metal'], 'timeout_seconds': {'tool_query': 10, 'compile': 55, 'probe': 45}}
    try:
        # A rejected selection is never assigned as the later report write target.
        parent, destination = select_private_paths(root, args.build_root, args.out)
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = Path(tempfile.mkdtemp(prefix='run-', dir=parent))
        os.chmod(directory, 0o700)
        for name in SOURCE_NAMES:
            path = root / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_OUTPUT:
                raise ProbeError('SOURCE_SELECTION_INVALID')
            data = path.read_bytes()
            report['source_files_sha256'][name] = hashlib.sha256(data).hexdigest()
            if name.endswith('.mm'):
                source = directory / 'selected-source.mm'
                source.write_bytes(data)
        status, raw = run_step(['/usr/bin/git', '-C', str(root), 'rev-parse', 'HEAD'], directory, 10)
        if status == 0 and re.fullmatch(rb'[a-f0-9]{40}\s*', raw):
            report['source_commit'] = raw.decode().strip()
        status, raw = run_step(['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-version'], directory, 10)
        if status != 0 or not safe_version(raw):
            raise ProbeError('MACOS_SDK_UNAVAILABLE')
        report['sdk_version'] = safe_version(raw)
        status, raw = run_step(['/usr/bin/xcrun', '--sdk', 'macosx', 'clang++', '--version'], directory, 10)
        version = re.search(rb'Apple clang version ([0-9][0-9A-Za-z.+-]{0,64})', raw)
        if status != 0 or not version:
            raise ProbeError('APPLE_COMPILER_UNAVAILABLE')
        report['compiler_version'] = version.group(1).decode('ascii')
        binary = directory / 'metal-minimaltest'
        command = ['/usr/bin/xcrun', '--sdk', 'macosx', 'clang++', '-arch', 'x86_64', '-std=c++17',
                   '-O2', '-fobjc-arc', '-Wall', '-Wextra', '-Werror', '-mmacosx-version-min=12.0',
                   str(source), '-framework', 'Foundation', '-framework', 'Metal', '-o', str(binary)]
        status, _ = run_step(command, directory, 55)
        if status != 0:
            raise ProbeError('NATIVE_COMPILE_FAILED')
        if binary.is_symlink() or not binary.is_file() or binary.stat().st_size > MAX_BINARY:
            raise ProbeError('NATIVE_BINARY_INVALID')
        data = binary.read_bytes()
        report['executable'] = inspect_executable(data)
        report['binary_sha256'] = hashlib.sha256(data).hexdigest()
        report['build_verified'] = True
        status, raw = run_step([str(binary), '--context', args.context], directory, 45)
        probe = parse_probe(raw, status, args.context)
        report['probe'] = probe
        report['state'], report['code'] = probe['state'], probe['code']
        report['gpu_execution_verified'] = probe['gpu_execution_verified']
    except ProbeError as error:
        report['code'] = str(error)
    except (OSError, ValueError):
        report['code'] = 'NATIVE_STEP_UNAVAILABLE'
    try:
        if 'destination' not in locals():
            print('Metal test output selection refused.', file=sys.stderr)
            return 3
        write_report(destination, report)
    except (OSError, ValueError):
        print('Metal test report could not be saved.', file=sys.stderr)
        return 3
    print('Metal report saved: ' + report['state'] + '; ' + report['code'])
    return 0 if report['state'] == 'passed' else 2 if report['state'] == 'unverified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
