# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Compile our own MSL to AIR and validated Shader SPIR-V on an Intel macOS host."""
import sys


def unsupported_host():
    print('{"schema":1,"state":"unsupported-host","code":"INTEL_MACOS_REQUIRED",'
          '"shader_translation_verified":false,"gpu_execution_verified":false,'
          '"metal_provider_registered":false,"report_file_written":false}')
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
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import threading
import time
import tomllib

CONVERTER_PIN = '43c46ac8a24adf1a6e872b8a52c706ec9614fad0'
CONVERTER_LICENSE_SHA = 'e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118'
ENTRY = 'lunarbridge_xe2_probe'
LOCAL_SIZE = [64, 1, 1]
REFLECTION_VERSION = 55
REFLECTION_SOURCE_SHA = 'a9ccc05d22e417594181b6f39222476fb4fd66d7dc887e7d8af32a5ec451d576'
MAX_FILE = 4 * 1024 * 1024
MAX_SPIRV = 1024 * 1024
MAX_CAPTURE = 4 * 1024 * 1024
SOURCE_NAMES = ('gpu-toolchain/xe2_compute_probe.metal', 'tools/metal_shader_ci.py',
                'tests/test_metal_shader.py', '.github/workflows/metal-shader.yml',
                'docs/METAL_SHADER_BRIDGE.md', 'gpu-toolchain/xe2_integer_probe.cl')


class ShaderError(ValueError):
    """A fixed allowlisted error code; never attach a path or raw tool output."""


def need(condition, code):
    if not condition:
        raise ShaderError(code)


def read_regular(path, limit=MAX_FILE, allow_empty=False):
    path = Path(path)
    before = path.lstat()
    need(stat.S_ISREG(before.st_mode) and (0 <= before.st_size if allow_empty else 0 < before.st_size) and
         before.st_size <= limit, 'FILE_TYPE_OR_SIZE_INVALID')
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        current = os.fstat(stream.fileno())
        need(stat.S_ISREG(current.st_mode) and (before.st_dev, before.st_ino) ==
             (current.st_dev, current.st_ino) and (0 <= current.st_size if allow_empty else 0 < current.st_size) and
             current.st_size <= limit, 'FILE_CHANGED_OR_INVALID')
        data = stream.read(limit + 1)
    need(len(data) == current.st_size and len(data) <= limit, 'FILE_CHANGED_OR_INVALID')
    return data


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_json(raw, limit=MAX_FILE):
    need(isinstance(raw, bytes) and len(raw) <= limit, 'JSON_SIZE_INVALID')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            need(key not in value, 'JSON_DUPLICATE_KEY')
            value[key] = item
        return value
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ShaderError('JSON_NONFINITE')))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ShaderError('JSON_INVALID') from error
    need(isinstance(value, dict), 'JSON_OBJECT_REQUIRED')
    return value


def inspect_air(data, llvm_text):
    need(isinstance(data, bytes) and 8 <= len(data) <= MAX_FILE, 'AIR_SIZE_INVALID')
    if data.startswith(b'BC\xc0\xde'):
        container, payload = 'llvm-bitcode', len(data)
    else:
        need(len(data) >= 24, 'AIR_CONTAINER_INVALID')
        magic, version, offset, length, _ = struct.unpack_from('<5I', data)
        need(magic == 0x0b17c0de and version == 0 and offset >= 20 and offset % 4 == 0 and
             length >= 4 and offset <= len(data) and length <= len(data) - offset and
             data[offset:offset + 4] == b'BC\xc0\xde', 'AIR_CONTAINER_INVALID')
        container, payload = 'llvm-bitcode-wrapper', length
    need(isinstance(llvm_text, bytes) and 0 < len(llvm_text) <= MAX_FILE, 'AIR_LLVM_SIZE_INVALID')
    try:
        text = llvm_text.decode('utf-8')
    except UnicodeError as error:
        raise ShaderError('AIR_LLVM_INVALID') from error
    # llvm-dis's ModuleID comment names its input file; it is not shader metadata.
    body = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('; ModuleID ='))
    need(not any(marker in body for marker in ('/Users/', 'C:\\Users\\', '/home/', '\\Users\\')),
         'AIR_PRIVATE_PATH_METADATA')
    triple = re.search(r'^target triple = "(air64-apple-macosx[0-9.]+)"$', body, re.M)
    need(triple is not None and re.search(r'^!air\.kernel\s*=\s*!\{!\d+', body, re.M) is not None and
         re.search(r'^define\s+[^\n]*@' + ENTRY + r'\(', body, re.M) is not None and
         body.count('!"air.buffer"') >= 3, 'AIR_KERNEL_METADATA_MISSING')
    return {'container': container, 'payload_bytes': payload, 'target_triple': triple.group(1),
            'entry_point': ENTRY, 'kernel_metadata_checked': True, 'private_path_metadata_checked': True}


def inspect_spirv(data):
    need(isinstance(data, bytes) and 20 <= len(data) <= MAX_SPIRV and len(data) % 4 == 0,
         'SPIRV_SIZE_INVALID')
    words = struct.unpack('<' + str(len(data) // 4) + 'I', data)
    magic, version, _, bound, schema = words[:5]
    need(magic == 0x07230203 and version in range(0x10000, 0x10600, 0x100) and
         1 < bound <= 65536 and schema == 0, 'SPIRV_HEADER_INVALID')
    position, instructions = 5, 0
    capabilities, memories, entries, sizes, decorations, variables = set(), [], [], [], {}, {}
    while position < len(words):
        count, opcode = words[position] >> 16, words[position] & 0xffff
        need(0 < count <= len(words) - position, 'SPIRV_INSTRUCTION_INVALID')
        operands = words[position + 1:position + count]
        if opcode == 17:
            need(count == 2, 'SPIRV_CAPABILITY_INVALID')
            capabilities.add(operands[0])
        elif opcode == 14:
            need(count == 3, 'SPIRV_MEMORY_MODEL_INVALID')
            memories.append(operands)
        elif opcode == 15:
            need(count >= 4 and 0 < operands[1] < bound, 'SPIRV_ENTRY_INVALID')
            name_bytes = struct.pack('<' + str(len(operands) - 2) + 'I', *operands[2:])
            terminator = name_bytes.find(b'\0')
            need(0 <= terminator <= 64, 'SPIRV_ENTRY_NAME_INVALID')
            entries.append((operands[0], operands[1], name_bytes[:terminator]))
        elif opcode == 16 and len(operands) >= 2 and operands[1] == 17:
            need(count == 6, 'SPIRV_LOCAL_SIZE_INVALID')
            sizes.append(operands)
        elif opcode == 71 and len(operands) >= 2 and operands[1] in (33, 34):
            need(count == 4 and 0 < operands[0] < bound, 'SPIRV_DESCRIPTOR_INVALID')
            key = (operands[0], operands[1])
            need(key not in decorations, 'SPIRV_DESCRIPTOR_DUPLICATE')
            decorations[key] = operands[2]
        elif opcode == 59:
            need(count in (4, 5) and 0 < operands[1] < bound, 'SPIRV_VARIABLE_INVALID')
            need(operands[1] not in variables, 'SPIRV_VARIABLE_DUPLICATE')
            variables[operands[1]] = operands[2]
        instructions += 1
        need(instructions <= 65536, 'SPIRV_INSTRUCTION_LIMIT')
        position += count
    need(1 in capabilities and 6 not in capabilities and memories == [(0, 1)], 'SPIRV_SHADER_CONTRACT_INVALID')
    need(len(entries) == 1 and entries[0][0] == 5 and entries[0][2] == b'main', 'SPIRV_COMPUTE_ENTRY_MISSING')
    need(sizes == [(entries[0][1], 17, *LOCAL_SIZE)], 'SPIRV_LOCAL_SIZE_INVALID')
    resources = []
    for (identity, decoration), value in decorations.items():
        if decoration == 33:
            need((identity, 34) in decorations and variables.get(identity) in (2, 12), 'SPIRV_DESCRIPTOR_INVALID')
            resources.append((decorations[(identity, 34)], value))
    need(sorted(resources) == [(0, 0), (0, 1), (0, 2)] and len(decorations) == 6,
         'SPIRV_BUFFER_BINDINGS_INVALID')
    return {'environment': 'vulkan1.2', 'execution_model': 'GLCompute', 'addressing_model': 'Logical',
            'memory_model': 'GLSL450', 'entry_point': 'main', 'local_size': LOCAL_SIZE,
            'descriptor_bindings': [0, 1, 2], 'descriptor_set': 0, 'instructions': instructions,
            'header_and_instruction_bounds_checked': True}


def unsigned(value, bits):
    return type(value) is int and 0 <= value < 1 << bits


def inspect_footprint(value):
    # The pinned Rust schema uses u64 byte ranges/strides and a real bool.
    # These vector caps are harness resource limits, not converter ABI limits.
    need(isinstance(value, dict) and set(value) == {'static_ranges', 'strided_accesses', 'has_unbounded_access'} and
         isinstance(value.get('static_ranges'), list) and len(value['static_ranges']) <= 4096 and
         isinstance(value.get('strided_accesses'), list) and len(value['strided_accesses']) <= 4096 and
         type(value.get('has_unbounded_access')) is bool, 'REFLECTION_EXECUTABLE_FOOTPRINT_INVALID')
    for interval in value['static_ranges']:
        need(isinstance(interval, dict) and set(interval) == {'offset', 'size'} and
             unsigned(interval.get('offset'), 64) and unsigned(interval.get('size'), 64) and
             interval['size'] > 0 and interval['offset'] + interval['size'] < 1 << 64,
             'REFLECTION_BYTE_RANGE_INVALID')
    sources = ('VertexIndex', 'InstanceIndex', 'GlobalInvocationIdX', 'GlobalInvocationIdY',
               'GlobalInvocationIdZ', 'LocalInvocationIdX', 'LocalInvocationIdY', 'LocalInvocationIdZ',
               'WorkgroupIdX', 'WorkgroupIdY', 'WorkgroupIdZ', 'LocalInvocationIndex')
    for access in value['strided_accesses']:
        need(isinstance(access, dict) and set(access) == {'base_offset', 'access_size', 'terms'} and
             unsigned(access.get('base_offset'), 64) and unsigned(access.get('access_size'), 64) and
             access['access_size'] > 0 and access['base_offset'] + access['access_size'] < 1 << 64 and
             isinstance(access.get('terms'), list) and len(access['terms']) <= len(sources),
             'REFLECTION_STRIDED_ACCESS_INVALID')
        previous = -1
        for term in access['terms']:
            need(isinstance(term, dict) and set(term) == {'source', 'stride'} and
                 term.get('source') in sources and unsigned(term.get('stride'), 64),
                 'REFLECTION_STRIDE_TERM_INVALID')
            position = sources.index(term['source'])
            need(position > previous, 'REFLECTION_STRIDE_SOURCE_ORDER_INVALID')
            previous = position
    # All three bindings in our owned fixture execute a memory access.
    need(value['static_ranges'] or value['strided_accesses'] or value['has_unbounded_access'],
         'REFLECTION_EXECUTABLE_FOOTPRINT_EMPTY')


def inspect_reflection(raw):
    value = safe_json(raw)
    canonical = json.dumps(value, ensure_ascii=False)
    need(not any(marker in canonical for marker in ('/Users/', 'C:\\\\Users\\\\', '/home/')),
         'REFLECTION_PRIVATE_PATH_METADATA')
    need(type(value.get('reflection_version')) is int and value['reflection_version'] == REFLECTION_VERSION and
         value.get('stage') == 'Kernel' and value.get('entry_point') == ENTRY and
         value.get('local_size') == LOCAL_SIZE and value.get('kernel_dispatch') == 'Workgroups',
         'REFLECTION_KERNEL_CONTRACT_INVALID')
    need(all(type(item) is int for item in value['local_size']), 'REFLECTION_KERNEL_CONTRACT_INVALID')
    bindings = value.get('bindings')
    need(isinstance(bindings, list) and len(bindings) == 3, 'REFLECTION_BINDINGS_INVALID')
    summaries = []
    for index, binding in enumerate(bindings):
        need(isinstance(binding, dict) and binding.get('kind') == 'Buffer' and
             type(binding.get('metal_index')) is int and binding['metal_index'] == index and
             binding.get('descriptor') == {'set': 0, 'binding': index, 'count': 1} and
             type(binding['descriptor']['set']) is int and type(binding['descriptor']['binding']) is int and
             type(binding['descriptor']['count']) is int, 'REFLECTION_BINDINGS_INVALID')
        need(binding.get('access') in (('ReadOnly',) if index in (0, 2) else ('WriteOnly', 'ReadWrite')) and
             binding.get('address_space') in ((1, 2) if index == 0 else ((1,) if index == 1 else (2,))) and
             type(binding.get('address_space')) is int, 'REFLECTION_BUFFER_ACCESS_INVALID')
        inspect_footprint(binding.get('footprint'))
        extent = binding.get('extent')
        need(extent in ('Unbounded', 'Unknown') or
             (isinstance(extent, dict) and set(extent) == {'Object'} and
              isinstance(extent['Object'], dict) and set(extent['Object']) == {'bytes'} and
              unsigned(extent['Object']['bytes'], 32)), 'REFLECTION_BUFFER_EXTENT_INVALID')
        summaries.append({'metal_index': index, 'descriptor': binding['descriptor'],
                          'access': binding['access'], 'address_space': binding['address_space']})
    need(bindings[2].get('extent') == {'Object': {'bytes': 4}} and
         unsigned(bindings[2]['extent']['Object']['bytes'], 32), 'REFLECTION_COUNT_SIZE_INVALID')
    return {'reflection_version': REFLECTION_VERSION, 'stage': 'Kernel', 'entry_point': ENTRY,
            'local_size': LOCAL_SIZE, 'kernel_dispatch': 'Workgroups', 'buffers': summaries,
            'count_bytes': 4, 'final_module_footprints_present': True}


def private_path(path, root):
    candidate = Path(path).absolute()
    need(not any(item.is_symlink() for item in (candidate, *candidate.parents)), 'PRIVATE_PATH_SYMLINK_REFUSED')
    resolved = candidate.resolve()
    need(resolved.is_relative_to(root / 'build') and resolved != root / 'build' and
         not any(part.upper() == 'EFI' or part.lower().endswith(('.kext', '.app', '.framework'))
                 for part in resolved.parts), 'PRIVATE_PATH_OUTSIDE_BUILD')
    return resolved


def run_step(command, directory, timeout=60, env=None, slow_step=None, stdout_only=False):
    maximum = 300 if slow_step in ('cargo-build', 'toolchain-download') else 60
    need(type(timeout) in (int, float) and 0 < timeout <= maximum, 'STEP_TIMEOUT_INVALID')
    need(os.name == 'posix', 'PROCESS_GROUPS_REQUIRED')
    process = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, start_new_session=True)
    captured, errors, overflow, reader_failed = bytearray(), bytearray(), threading.Event(), threading.Event()
    def drain(stream, destination):
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                if len(destination) + len(chunk) > MAX_CAPTURE:
                    overflow.set()
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                destination.extend(chunk)
        except OSError:
            reader_failed.set()
        finally:
            # The owning reader closes its stream after read() returned. The
            # caller must never acquire a buffered-stream lock held by read().
            stream.close()
    readers = [threading.Thread(target=drain, args=(process.stdout, captured), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, errors), daemon=True)]
    for reader in readers:
        reader.start()
    error = None
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        error = 'STEP_TIMEOUT'
    finally:
        # Kill the entire new session even if its leader exited: a build tool
        # must not leave descendants holding pipes or running after the step.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            error = 'PROCESS_GROUP_CLEANUP_FAILED'
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            error = 'STEP_TERMINATION_UNCONFIRMED'
        for reader, stream in zip(readers, (process.stdout, process.stderr)):
            reader.join(timeout=5)
            if reader.is_alive():
                error = 'STEP_OUTPUT_DRAIN_UNCONFIRMED'
            else:
                stream.close()
    need(error is None, error or 'STEP_FAILURE')
    need(not overflow.is_set(), 'STEP_OUTPUT_LIMIT')
    need(not reader_failed.is_set(), 'STEP_OUTPUT_READ_FAILED')
    return process.returncode, bytes(captured) if stdout_only else bytes(captured + errors)


def version(raw):
    match = re.search(rb'(?<![0-9])([0-9]+(?:\.[0-9]+){1,3})(?![0-9])', raw)
    return match.group(1).decode('ascii') if match else None


def qualify_tool(name, command, directory, path=None):
    code, raw = run_step(command, directory, timeout=15)
    need(code == 0 and version(raw) is not None, 'TOOL_VERSION_UNVERIFIED')
    selected = Path(path or shutil.which(command[0]) or '').resolve()
    return {'name': name, 'version': version(raw), 'executable_sha256': sha(read_regular(selected, 128 * 1024 * 1024))}


def converter_provenance(source, directory):
    status, identity = run_step(['git', '-C', str(source), 'rev-parse', '--verify', 'HEAD'], directory, 15)
    need(status == 0 and identity.strip().decode('ascii', errors='ignore') == CONVERTER_PIN,
         'CONVERTER_SOURCE_PIN_MISMATCH')
    status, dirty = run_step(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=all'], directory, 15)
    need(status == 0 and not dirty.strip(), 'CONVERTER_SOURCE_NOT_CLEAN')
    original = read_regular(source / 'LICENSE')
    manifest = read_regular(source / 'Cargo.toml')
    reflection_schema = read_regular(source / 'src/reflect/mod.rs')
    need(sha(reflection_schema) == REFLECTION_SOURCE_SHA and
         re.search(rb'pub const REFLECTION_VERSION: u32 = 55;', reflection_schema) is not None,
         'CONVERTER_REFLECTION_SCHEMA_MISMATCH')
    need(sha(original) == CONVERTER_LICENSE_SHA, 'CONVERTER_LICENSE_MISMATCH')
    package = tomllib.loads(manifest.decode('utf-8')).get('package', {})
    need(package.get('name') == 'metal2vulkan' and package.get('license') == 'LGPL-3.0-or-later',
         'CONVERTER_PACKAGE_MISMATCH')
    status, raw = run_step(['git', '-C', str(source), 'ls-files', '-z'], directory, 15)
    need(status == 0, 'CONVERTER_SOURCE_LIST_FAILED')
    names = raw.split(b'\0')
    need(names[-1] == b'' and 0 < len(names) <= 4097, 'CONVERTER_SOURCE_LIST_INVALID')
    digest, total = hashlib.sha256(), 0
    for encoded in sorted(names[:-1]):
        name = encoded.decode('utf-8')
        item = Path(name)
        need(not item.is_absolute() and '..' not in item.parts and '\\' not in name and
             not any(ord(character) < 32 for character in name), 'CONVERTER_SOURCE_PATH_INVALID')
        data = read_regular(source / item, allow_empty=True)
        total += len(data)
        need(total <= 64 * 1024 * 1024, 'CONVERTER_SOURCE_TOTAL_LIMIT')
        digest.update(encoded + b'\0' + hashlib.sha256(data).digest())
    return {'repository': 'steelbrain/metal2vulkan', 'commit': CONVERTER_PIN, 'tracked_tree_clean': True,
            'tracked_files': len(names) - 1, 'tracked_tree_sha256': digest.hexdigest(),
            'license': 'LGPL-3.0-or-later', 'license_sha256': sha(original),
            'cargo_manifest_sha256': sha(manifest),
            'reflection_schema_source_sha256': sha(reflection_schema),
            'reflection_schema_version': REFLECTION_VERSION,
            'required_rust_version': package.get('rust-version')}, original


def dependency_provenance(lock_data, metadata_raw):
    try:
        lock = tomllib.loads(lock_data.decode('utf-8'))
    except (UnicodeError, ValueError) as error:
        raise ShaderError('CARGO_LOCK_INVALID') from error
    need(not any(marker in lock_data for marker in (b'/Users/', b'C:\\\\Users\\\\', b'/home/')),
         'CARGO_LOCK_PRIVATE_PATH')
    need(isinstance(lock.get('package'), list) and len(lock['package']) <= 4096 and
         all(isinstance(item, dict) and item.get('source') in
             (None, 'registry+https://github.com/rust-lang/crates.io-index') for item in lock['package']),
         'CARGO_LOCK_SOURCE_UNVERIFIED')
    metadata = safe_json(metadata_raw)
    packages = metadata.get('packages')
    resolve = metadata.get('resolve')
    need(isinstance(packages, list) and isinstance(resolve, dict) and isinstance(resolve.get('nodes'), list),
         'CARGO_METADATA_INVALID')
    by_id = {package['id']: package for package in packages if isinstance(package, dict) and 'id' in package}
    nodes = {node['id']: node for node in resolve['nodes'] if isinstance(node, dict) and 'id' in node}
    root = resolve.get('root')
    need(root in by_id and by_id[root].get('name') == 'metal2vulkan' and root in nodes,
         'CARGO_ROOT_INVALID')
    checksums = {(item.get('name'), item.get('version'), item.get('source')): item.get('checksum')
                 for item in lock.get('package', []) if isinstance(item, dict)}
    todo, seen, result = [root], set(), []
    while todo:
        identity = todo.pop()
        if identity in seen:
            continue
        need(identity in by_id and identity in nodes and len(seen) < 128, 'CARGO_DEPENDENCY_GRAPH_INVALID')
        seen.add(identity)
        package = by_id[identity]
        name, release, license_text, origin = (package.get(key) for key in ('name', 'version', 'license', 'source'))
        need(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name) and
             isinstance(release, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?', release),
             'CARGO_PACKAGE_INVALID')
        need(isinstance(license_text, str) and len(license_text) <= 160 and
             re.fullmatch(r'[A-Za-z0-9 .()+/-]+', license_text), 'CARGO_LICENSE_UNVERIFIED')
        checksum = checksums.get((name, release, origin))
        if identity == root:
            need(origin is None, 'CARGO_ROOT_INVALID')
        else:
            need(origin == 'registry+https://github.com/rust-lang/crates.io-index' and
                 isinstance(checksum, str) and re.fullmatch(r'[0-9a-f]{64}', checksum),
                 'CARGO_DEPENDENCY_SOURCE_UNVERIFIED')
        result.append({'name': name, 'version': release, 'license': license_text,
                       'source': 'selected-converter-checkout' if identity == root else 'crates.io',
                       'checksum_sha256': checksum})
        dependencies = nodes[identity].get('dependencies')
        need(isinstance(dependencies, list) and all(isinstance(item, str) for item in dependencies),
             'CARGO_DEPENDENCY_GRAPH_INVALID')
        todo.extend(dependencies)
    return {'cargo_lock_sha256': sha(lock_data), 'resolved_packages': sorted(result, key=lambda item: item['name'])}


def write_report(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.shader-report-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(value, indent=2, allow_nan=False) + '\n').encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    if sys.platform != 'darwin' or platform.machine() != 'x86_64':
        return unsupported_host()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', choices=('ci-host', 'target-probe'), required=True)
    parser.add_argument('--converter-source', type=Path, required=True)
    parser.add_argument('--artifact-root', type=Path)
    parser.add_argument('--allow-ci-metal-download', action='store_true')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    report = {'schema': 1, 'context': args.context, 'state': 'unverified', 'phase': 'preflight',
              'code': 'NOT_STARTED', 'source_commit': None, 'source_files_sha256': {}, 'tools': [],
              'converter': None, 'dependencies': None, 'outputs_sha256': {}, 'air': None, 'spirv': None,
              'reflection': None, 'converter_build_verified': False, 'air_compilation_verified': False,
              'shader_translation_verified': False, 'gpu_execution_verified': False,
              'metal_provider_registered': False, 'arc130v_backend_verified': False,
              'target_hardware_identity_verified': False, 'display_acceleration_verified': False,
              'cpu_fallback_used': False, 'metal_toolchain_download': {'attempted': False},
              'architecture': 'x86_64', 'timeouts_seconds': {'probe': 60, 'cargo_build': 300, 'ci_metal_download': 300}}
    artifacts = None
    try:
        source = private_path(args.converter_source, root)
        artifacts = private_path(args.artifact_root or root / 'build/metal-shader-artifacts', root)
        need(not artifacts.exists() and not artifacts.is_relative_to(source) and not source.is_relative_to(artifacts),
             'ARTIFACT_DIRECTORY_MUST_BE_FRESH')
        build_parent = root / 'build/metal-shader-private'
        private_path(build_parent, root)
        build_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifacts.mkdir(parents=True, mode=0o700)
        commit = os.environ.get('GITHUB_SHA', '')
        report['source_commit'] = commit if re.fullmatch(r'[0-9a-f]{40}', commit) else None
        report['source_files_sha256'] = {name: sha(read_regular(root / name)) for name in SOURCE_NAMES}
        need(not args.allow_ci_metal_download or (args.context == 'ci-host' and
             os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('CI', '').lower() == 'true' and
             report['source_commit'] is not None), 'METAL_DOWNLOAD_REQUIRES_EPHEMERAL_CI')
        with tempfile.TemporaryDirectory(prefix='run-', dir=build_parent) as temporary:
            directory = Path(temporary)
            environment = os.environ.copy()
            for name in ('RUSTC_WRAPPER', 'RUSTC_WORKSPACE_WRAPPER', 'RUSTFLAGS',
                         'CARGO_ENCODED_RUSTFLAGS', 'RUSTDOCFLAGS', 'CARGO_TARGET_DIR'):
                environment.pop(name, None)
            environment['TMPDIR'] = str(directory)
            environment['METAL2VULKAN_REPRO_DIR'] = str(directory / 'repros')
            report['phase'] = 'converter-source'
            report['converter'], original_license = converter_provenance(source, directory)
            (artifacts / 'CONVERTER-LICENSE.txt').write_bytes(original_license)
            report['phase'] = 'toolchain'
            required = {name: shutil.which(name) for name in ('cargo', 'rustc', 'llvm-dis', 'spirv-val', 'xcrun')}
            need(all(required.values()), 'REQUIRED_TOOL_MISSING')
            for name in ('cargo', 'rustc', 'llvm-dis', 'spirv-val'):
                report['tools'].append(qualify_tool(name, [required[name], '--version'], directory))
            rust_version = next(item['version'] for item in report['tools'] if item['name'] == 'rustc')
            need(tuple(int(item) for item in rust_version.split('.')[:2]) >= (1, 87), 'RUST_VERSION_TOO_OLD')
            status, metal_path = run_step([required['xcrun'], '-sdk', 'macosx', '-find', 'metal'], directory,
                                          15, stdout_only=True)
            metal_status, metal_version = run_step([required['xcrun'], '-sdk', 'macosx', 'metal', '--version'],
                                                   directory, 15) if status == 0 else (status, b'')
            # Recent Xcode may resolve a launcher even when the actual optional
            # Metal toolchain is absent. Finding that path alone is not readiness.
            if (status or metal_status or version(metal_version) is None) and args.allow_ci_metal_download:
                report['phase'] = 'ci-metal-toolchain-download'
                started = time.monotonic()
                report['metal_toolchain_download'] = {'attempted': True, 'ephemeral_ci_only': True,
                                                       'completed': False, 'returncode': None}
                status, _ = run_step(['xcodebuild', '-downloadComponent', 'MetalToolchain'], directory,
                                     300, env=environment, slow_step='toolchain-download')
                report['metal_toolchain_download'].update(returncode=status, completed=status == 0,
                                                         elapsed_seconds=round(time.monotonic() - started, 3))
                report['phase'] = 'toolchain'
                status, metal_path = run_step([required['xcrun'], '-sdk', 'macosx', '-find', 'metal'], directory,
                                              15, stdout_only=True)
                metal_status, metal_version = run_step([required['xcrun'], '-sdk', 'macosx', 'metal', '--version'],
                                                       directory, 15) if status == 0 else (status, b'')
            need(status == 0 and metal_status == 0 and version(metal_version) is not None,
                 'XCODE_METAL_TOOLCHAIN_MISSING')
            selected_metal = Path(metal_path.decode('utf-8').strip()).resolve()
            report['tools'].append(qualify_tool('metal', [required['xcrun'], '-sdk', 'macosx', 'metal', '--version'],
                                              directory, selected_metal))
            status, sdk = run_step([required['xcrun'], '-sdk', 'macosx', '--show-sdk-version'], directory, 15)
            need(status == 0 and version(sdk) is not None, 'MACOS_SDK_UNVERIFIED')
            report['sdk_version'] = version(sdk)
            environment['METAL2VULKAN_LLVM_DIS'] = str(Path(required['llvm-dis']).resolve())
            environment['METAL2VULKAN_SPIRV_VAL'] = str(Path(required['spirv-val']).resolve())
            report['phase'] = 'converter-build'
            status, _ = run_step([required['cargo'], 'build', '--manifest-path', str(source / 'Cargo.toml'),
                                  '-p', 'metal2vulkan', '--bin', 'metal2vulkan', '--features', 'serde', '--release',
                                  '--target-dir', str(directory / 'cargo-target')], directory, 300,
                                 env=environment, slow_step='cargo-build')
            need(status == 0, 'CONVERTER_BUILD_FAILED')
            executable = directory / 'cargo-target/release/metal2vulkan'
            report['converter']['executable_sha256'] = sha(read_regular(executable, 128 * 1024 * 1024))
            report['converter_build_verified'] = True
            report['phase'] = 'dependency-provenance'
            lock = read_regular(source / 'Cargo.lock')
            status, metadata = run_step([required['cargo'], 'metadata', '--manifest-path', str(source / 'Cargo.toml'),
                                        '--format-version', '1', '--locked', '--features', 'serde'], directory,
                                       60, env=environment, stdout_only=True)
            need(status == 0, 'CARGO_METADATA_FAILED')
            report['dependencies'] = dependency_provenance(lock, metadata)
            (artifacts / 'Cargo.lock').write_bytes(lock)
            write_report(artifacts / 'dependencies.json', report['dependencies'])
            report['phase'] = 'msl-to-air'
            msl = read_regular(root / 'gpu-toolchain/xe2_compute_probe.metal')
            (directory / 'xe2_compute_probe.metal').write_bytes(msl)
            status, _ = run_step([required['xcrun'], '-sdk', 'macosx', 'metal', '-c',
                                  'xe2_compute_probe.metal', '-o', 'xe2_compute_probe.air'], directory, 60, env=environment)
            need(status == 0, 'MSL_AIR_COMPILATION_FAILED')
            report['phase'] = 'air-llvm-contract'
            status, _ = run_step([required['llvm-dis'], 'xe2_compute_probe.air', '-o', 'own-air.ll'],
                                 directory, 60, env=environment)
            need(status == 0, 'AIR_LLVM_DIS_FAILED')
            air = read_regular(directory / 'xe2_compute_probe.air')
            report['air'] = inspect_air(air, read_regular(directory / 'own-air.ll'))
            report['air_compilation_verified'] = True
            report['phase'] = 'air-to-spirv'
            status, _ = run_step([str(executable), 'xe2_compute_probe.air', 'own-shader.spv', '--stage', 'kernel',
                                  '--local', '64,1,1', '--whole-workgroups', '--emit-meta', 'own-reflection.json'],
                                 directory, 60, env=environment)
            need(status == 0, 'AIR_SPIRV_TRANSLATION_FAILED')
            report['phase'] = 'spirv-contract'
            spirv = read_regular(directory / 'own-shader.spv', MAX_SPIRV)
            report['spirv'] = inspect_spirv(spirv)
            status, _ = run_step([required['spirv-val'], '--target-env', 'vulkan1.2', 'own-shader.spv'],
                                 directory, 60, env=environment)
            need(status == 0, 'INDEPENDENT_SPIRV_VALIDATION_FAILED')
            report['spirv']['independent_validator_passed'] = True
            report['phase'] = 'reflection-contract'
            reflection = read_regular(directory / 'own-reflection.json')
            report['reflection'] = inspect_reflection(reflection)
            # Only our own generated shader outputs and licenses/provenance leave
            # scratch space. No vendor sources, tool logs, user IDs or native GPU claim.
            for name, data in (('xe2_compute_probe.metal', msl), ('xe2_compute_probe.air', air),
                               ('own-shader.spv', spirv), ('reflection.json', reflection)):
                (artifacts / name).write_bytes(data)
                report['outputs_sha256'][name] = sha(data)
            report.update(state='passed', phase='completed', code='SHADER_TRANSLATION_VERIFIED',
                          shader_translation_verified=True)
    except ShaderError as error:
        report['code'] = str(error)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError):
        report['code'] = 'CONTROLLED_TOOL_OR_IO_FAILURE'
    if artifacts is not None and artifacts.exists() and report['source_files_sha256']:
        try:
            write_report(artifacts / 'report.json', report)
        except OSError:
            print('{"schema":1,"state":"unverified","code":"REPORT_WRITE_FAILED",'
                  '"shader_translation_verified":false,"gpu_execution_verified":false}')
            return 2
    print(json.dumps({'schema': 1, 'state': report['state'], 'phase': report['phase'], 'code': report['code'],
                      'shader_translation_verified': report['shader_translation_verified'],
                      'gpu_execution_verified': False, 'metal_provider_registered': False}))
    return 0 if report['shader_translation_verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
