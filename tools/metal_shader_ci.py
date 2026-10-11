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
METAL_PROBE_TIMEOUT = 60
METAL_DOWNLOAD_TIMEOUT = 300
CARGO_RESOLVE_TIMEOUT = 300
CARGO_BUILD_TIMEOUT = 900
PROGRESS_CATEGORIES = ('index', 'download', 'compile', 'check', 'finish', 'unknown')
MAX_PROGRESS_OBSERVATIONS = 4096
MAX_PROGRESS_LINE = 1024
PRIVATE_IR_CATEGORIES = ('source_filename', 'di_file', 'di_compile_unit', 'compiler_option',
                         'compiler_identity', 'module_id', 'other_metadata', 'other_ir')
MAX_PRIVATE_IR_OBSERVATIONS = 64
MAX_PRIVATE_IR_LINES = 65536
MAX_PRIVATE_IR_REFERENCES = 4096
COMMAND_STAGES = frozenset(('cargo-version', 'rustc-version', 'llvm-dis-version', 'spirv-val-version',
                           'metal-discovery', 'metal-version', 'ci-metal-toolchain-download',
                           'metal-discovery-after-download', 'metal-version-after-download', 'macos-sdk-version',
                           'msl-to-air', 'air-llvm-dis', 'cargo-resolve', 'cargo-build', 'cargo-metadata'))
COMMAND_ERRORS = frozenset(('STEP_TIMEOUT', 'STEP_TIMEOUT_INVALID', 'PROCESS_GROUPS_REQUIRED',
                           'PROCESS_GROUP_CLEANUP_FAILED', 'STEP_TERMINATION_UNCONFIRMED',
                           'STEP_OUTPUT_DRAIN_UNCONFIRMED', 'STEP_OUTPUT_LIMIT', 'STEP_OUTPUT_READ_FAILED'))
SOURCE_NAMES = ('gpu-toolchain/xe2_compute_probe.metal', 'tools/metal_shader_ci.py',
                'tests/test_metal_shader.py', '.github/workflows/metal-shader.yml',
                'docs/METAL_SHADER_BRIDGE.md', 'gpu-toolchain/xe2_integer_probe.cl')
OWN_OUTPUT_NAMES = ('xe2_compute_probe.metal', 'xe2_compute_probe.air', 'own-air.ll',
                    'own-shader.spv', 'reflection.json')


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


def classify_private_air_metadata(raw, *, include_module_id=False):
    """Classify rejected original LLVM text without exporting any field values."""
    need(isinstance(raw, bytes) and 0 < len(raw) <= MAX_FILE and type(include_module_id) is bool,
         'AIR_LLVM_SIZE_INVALID')
    try:
        raw.decode('utf-8')
    except UnicodeError as error:
        raise ShaderError('AIR_LLVM_INVALID') from error
    lines = raw.split(b'\n', MAX_PRIVATE_IR_LINES)
    incomplete = len(lines) > MAX_PRIVATE_IR_LINES
    lines = lines[:MAX_PRIVATE_IR_LINES]
    nodes, roots, references = {}, {'compiler_option': set(), 'compiler_identity': set()}, 0
    for raw_line in lines:
        line = raw_line.decode('utf-8').strip()
        node = re.match(r'^!(\d{1,10})\s*=', line)
        root = re.match(r'^!(air\.compile_options|llvm\.commandline|llvm\.ident)\s*=', line)
        if not node and not root:
            continue
        selected = set()
        # Numeric text inside a quoted value is not a metadata reference.
        references_text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line.split('=', 1)[1])
        for match in re.finditer(r'!(\d{1,10})(?!\d)', references_text):
            if references >= MAX_PRIVATE_IR_REFERENCES:
                incomplete = True
                break
            references += 1
            selected.add(match.group(1))
        if node:
            if len(nodes) >= MAX_PRIVATE_IR_REFERENCES or node.group(1) in nodes:
                incomplete = True
            else:
                nodes[node.group(1)] = selected
        if root:
            category = 'compiler_identity' if root.group(1) == 'llvm.ident' else 'compiler_option'
            roots[category].update(selected)
    for category, seeds in roots.items():
        todo, reached = list(seeds), set()
        while todo and len(reached) < MAX_PRIVATE_IR_REFERENCES:
            identity = todo.pop()
            if identity not in reached:
                reached.add(identity)
                todo.extend(nodes.get(identity, set()) - reached)
        incomplete |= bool(todo)
        roots[category] = reached
    counts = {name: 0 for name in PRIVATE_IR_CATEGORIES}
    observations, limit_reached = [], False
    markers = (b'/Users/', b'C:\\Users\\', b'C:\\\\Users\\\\', b'/home/', b'\\Users\\')
    for raw_line in lines:
        line = raw_line.decode('utf-8').strip()
        module_id = line.startswith('; ModuleID =')
        if (module_id and not include_module_id) or not any(marker in raw_line for marker in markers):
            continue
        if len(observations) == MAX_PRIVATE_IR_OBSERVATIONS:
            limit_reached = True
            break
        node = re.match(r'^!(\d{1,10})\s*=', line)
        if module_id:
            category = 'module_id'
        elif re.match(r'^source_filename\s*=', line):
            category = 'source_filename'
        elif re.search(r'!DIFile\s*\(', line):
            category = 'di_file'
        elif re.search(r'!DICompileUnit\s*\(', line):
            category = 'di_compile_unit'
        elif re.match(r'^!(air\.compile_options|llvm\.commandline)\s*=', line) or (
                node and node.group(1) in roots['compiler_option']):
            category = 'compiler_option'
        elif re.match(r'^!llvm\.ident\s*=', line) or (node and node.group(1) in roots['compiler_identity']):
            category = 'compiler_identity'
        else:
            category = 'other_metadata' if line.startswith('!') else 'other_ir'
        counts[category] += 1
        observations.append({'category': category, 'line_sha256': sha(raw_line)})
    return {'schema': 1, 'scope': 'exported-llvm-text' if include_module_id else 'kernel-metadata',
            'original_llvm_text_sha256': sha(raw), 'categories': counts,
            'observations': len(observations), 'line_hashes': observations,
            'observation_limit_reached': limit_reached, 'classification_incomplete': bool(incomplete)}


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


class CargoProgress:
    """Bounded categories only; stream text never enters the exported summary."""

    def __init__(self):
        self.lock = threading.Lock()
        self.counts = dict.fromkeys(PROGRESS_CATEGORIES, 0)
        self.observations = 0
        self.last = None
        self.limited = False
        self.closed = False

    def observe(self, line, truncated=False):
        line = line.strip()
        if not line and not truncated:
            return
        category = 'unknown'
        if not truncated:
            for prefix, selected in ((b'Updating ', 'index'), (b'Downloading ', 'download'),
                                     (b'Downloaded ', 'download'), (b'Compiling ', 'compile'),
                                     (b'Checking ', 'check'), (b'Finished ', 'finish')):
                if line.startswith(prefix):
                    category = selected
                    break
        with self.lock:
            if self.closed:
                return
            if self.observations == MAX_PROGRESS_OBSERVATIONS:
                self.limited = True
                return
            self.observations += 1
            self.counts[category] += 1
            self.last = category

    def finish(self, target, elapsed):
        with self.lock:
            self.closed = True
            target.update(categories=dict(self.counts), observations=self.observations,
                          last_category=self.last, observation_limit_reached=self.limited,
                          elapsed_seconds=round(elapsed, 3))


class CargoProgressStream:
    """Each pipe owns a short unfinished line; a shared collector owns counts."""

    def __init__(self, collector):
        self.collector = collector
        self.pending = bytearray()
        self.truncated = False

    def feed(self, chunk, final=False):
        pieces = chunk.split(b'\n')
        for index, piece in enumerate(pieces):
            if len(self.pending) + len(piece) > MAX_PROGRESS_LINE:
                self.pending.clear()
                self.truncated = True
            elif not self.truncated:
                self.pending.extend(piece)
            if index < len(pieces) - 1:
                self.collector.observe(bytes(self.pending), self.truncated)
                self.pending.clear()
                self.truncated = False
        if final and (self.pending or self.truncated):
            self.collector.observe(bytes(self.pending), self.truncated)
            self.pending.clear()
            self.truncated = False


def run_step(command, directory, timeout=60, env=None, slow_step=None, stdout_only=False, progress=None):
    maximum = CARGO_BUILD_TIMEOUT if slow_step == 'cargo-build' else (
        CARGO_RESOLVE_TIMEOUT if slow_step == 'cargo-resolve' else (
            METAL_DOWNLOAD_TIMEOUT if slow_step == 'toolchain-download' else 60))
    need(type(timeout) in (int, float) and 0 < timeout <= maximum, 'STEP_TIMEOUT_INVALID')
    need(progress is None or (type(progress) is dict and not progress and
         slow_step in ('cargo-resolve', 'cargo-build')), 'PROGRESS_REPORT_INVALID')
    need(os.name == 'posix', 'PROCESS_GROUPS_REQUIRED')
    collector = CargoProgress() if progress is not None else None
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, start_new_session=True)
    captured, errors, overflow, reader_failed = bytearray(), bytearray(), threading.Event(), threading.Event()
    def drain(stream, destination):
        observer = CargoProgressStream(collector) if collector is not None else None
        try:
            while True:
                read = getattr(stream, 'read1', stream.read) if observer is not None else stream.read
                chunk = read(65536)
                if not chunk:
                    break
                if observer is not None:
                    observer.feed(chunk)
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
            if observer is not None:
                observer.feed(b'', final=True)
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
        if collector is not None:
            collector.finish(progress, time.monotonic() - started)
    need(error is None, error or 'STEP_FAILURE')
    need(not overflow.is_set(), 'STEP_OUTPUT_LIMIT')
    need(not reader_failed.is_set(), 'STEP_OUTPUT_READ_FAILED')
    return process.returncode, bytes(captured) if stdout_only else bytes(captured + errors)


def version(raw):
    match = re.search(rb'(?<![0-9])([0-9]+(?:\.[0-9]+){1,3})(?![0-9])', raw)
    return match.group(1).decode('ascii') if match else None


def reported_step(stage, command, directory, report, timeout, cargo_progress=False, **options):
    need(stage in COMMAND_STAGES, 'COMMAND_STAGE_INVALID')
    report['phase'] = stage
    detail = {'stage': stage, 'timeout_seconds': timeout, 'timed_out': False,
              'completed': False, 'returncode': None, 'code': 'NOT_STARTED'}
    report.setdefault('command_steps', []).append(detail)
    if cargo_progress:
        need(stage in ('cargo-resolve', 'cargo-build'), 'PROGRESS_REPORT_INVALID')
        detail['progress'] = {}
        options['progress'] = detail['progress']
    try:
        code, raw = run_step(command, directory, timeout=timeout, **options)
    except ShaderError as error:
        fixed = str(error) if str(error) in COMMAND_ERRORS else 'CONTROLLED_TOOL_OR_IO_FAILURE'
        detail.update(timed_out=fixed == 'STEP_TIMEOUT', code=fixed)
        raise ShaderError(fixed) from None
    except OSError:
        detail['code'] = 'CONTROLLED_TOOL_OR_IO_FAILURE'
        raise
    detail.update(completed=True, returncode=code,
                  code='COMMAND_COMPLETED' if code == 0 else 'COMMAND_RETURNED_NONZERO')
    return code, raw


def qualify_tool(name, command, directory, path=None, timeout=15, report=None, stage=None):
    code, raw = (run_step(command, directory, timeout=timeout) if report is None else
                 reported_step(stage, command, directory, report, timeout))
    need(code == 0 and version(raw) is not None, 'TOOL_VERSION_UNVERIFIED')
    selected = Path(path or shutil.which(command[0]) or '').resolve()
    return {'name': name, 'version': version(raw), 'executable_sha256': sha(read_regular(selected, 128 * 1024 * 1024))}


def probe_metal(xcrun, directory, report, after_download=False):
    suffix = '-after-download' if after_download else ''
    code, raw = reported_step('metal-discovery' + suffix,
                             [xcrun, '-sdk', 'macosx', '-find', 'metal'], directory, report,
                             METAL_PROBE_TIMEOUT, stdout_only=True)
    need(code == 0, 'XCODE_METAL_TOOLCHAIN_MISSING')
    name = raw.decode('utf-8').strip()
    need(bool(name) and '\n' not in name and '\r' not in name, 'METAL_DISCOVERY_OUTPUT_INVALID')
    selected = Path(name).resolve()
    # Qualify the selected executable once. A resolved Xcode launcher alone
    # does not prove that its optional Metal toolchain is installed.
    return qualify_tool('metal', [xcrun, '-sdk', 'macosx', 'metal', '--version'],
                        directory, selected, timeout=METAL_PROBE_TIMEOUT,
                        report=report, stage='metal-version' + suffix)


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


def lock_summary(raw):
    try:
        value = tomllib.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError) as error:
        raise ShaderError('CARGO_LOCK_INVALID') from error
    need(not any(marker in raw for marker in (b'/Users/', b'C:\\Users\\', b'C:\\\\Users\\\\', b'/home/')),
         'CARGO_LOCK_PRIVATE_PATH')
    comments = (b'# This file is automatically @generated by Cargo.',
                b'# It is not intended for manual editing.')
    need(all(line.strip() in comments for line in raw.splitlines() if line.lstrip().startswith(b'#')),
         'CARGO_LOCK_COMMENT_UNVERIFIED')
    need(set(value) == {'version', 'package'} and type(value['version']) is int and value['version'] in (3, 4) and
         isinstance(value['package'], list) and 0 < len(value['package']) <= 4096, 'CARGO_LOCK_INVALID')
    identities = set()
    for item in value['package']:
        need(isinstance(item, dict) and set(item) <= {'name', 'version', 'source', 'checksum', 'dependencies'} and
             isinstance(item.get('name'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', item['name']) and
             isinstance(item.get('version'), str) and
             re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?', item['version']),
             'CARGO_LOCK_PACKAGE_INVALID')
        origin = item.get('source')
        need(origin in (None, 'registry+https://github.com/rust-lang/crates.io-index'),
             'CARGO_LOCK_SOURCE_UNVERIFIED')
        need(('checksum' not in item if origin is None else
              isinstance(item.get('checksum'), str) and re.fullmatch(r'[0-9a-f]{64}', item['checksum'])),
             'CARGO_LOCK_CHECKSUM_UNVERIFIED')
        identity = (item['name'], item['version'], origin)
        need(identity not in identities, 'CARGO_LOCK_PACKAGE_DUPLICATE')
        identities.add(identity)
        dependencies = item.get('dependencies', [])
        need(isinstance(dependencies, list) and len(dependencies) <= 4096 and all(
            isinstance(dep, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}(?: [0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)?'
                r'(?: \(registry\+https://github.com/rust-lang/crates.io-index\))?', dep)
            for dep in dependencies), 'CARGO_LOCK_DEPENDENCY_INVALID')
    return {'sha256': sha(raw), 'format_version': value['version'], 'package_count': len(identities),
            'registry_sources_and_checksums_checked': True}


def write_bytes_atomic(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.shader-report-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_report(path, value):
    write_bytes_atomic(path, (json.dumps(value, indent=2, allow_nan=False) + '\n').encode('utf-8'))


def retain_output(artifacts, report, name, data):
    need(name in OWN_OUTPUT_NAMES and isinstance(data, bytes), 'OUTPUT_NAME_INVALID')
    write_bytes_atomic(artifacts / name, data)
    report['outputs_sha256'][name] = sha(data)


def inspect_llvm_export(raw):
    # inspect_air ignores llvm-dis's ModuleID comment for kernel semantics.
    # An exported original file must additionally pass privacy in that comment.
    need(not any(marker in raw for marker in (b'/Users/', b'C:\\Users\\', b'C:\\\\Users\\\\', b'/home/', b'\\Users\\')),
         'AIR_LLVM_EXPORT_PRIVATE_PATH')


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
              'air_llvm_text_exported': False, 'air_private_metadata': None, 'cargo_lock': None,
              'shader_translation_verified': False, 'gpu_execution_verified': False,
              'metal_provider_registered': False, 'arc130v_backend_verified': False,
              'target_hardware_identity_verified': False, 'display_acceleration_verified': False,
              'cpu_fallback_used': False, 'metal_toolchain_download': {'attempted': False},
              'command_steps': [], 'architecture': 'x86_64',
              'timeouts_seconds': {'probe': METAL_PROBE_TIMEOUT, 'cargo_build': CARGO_BUILD_TIMEOUT,
                                   'cargo_resolve': CARGO_RESOLVE_TIMEOUT,
                                   'ci_metal_download': METAL_DOWNLOAD_TIMEOUT}}
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
            environment['CARGO_TERM_COLOR'] = 'never'
            report['phase'] = 'converter-source'
            report['converter'], original_license = converter_provenance(source, directory)
            write_bytes_atomic(artifacts / 'CONVERTER-LICENSE.txt', original_license)
            report['phase'] = 'toolchain'
            required = {name: shutil.which(name) for name in ('cargo', 'rustc', 'llvm-dis', 'spirv-val', 'xcrun')}
            need(all(required.values()), 'REQUIRED_TOOL_MISSING')
            for name in ('cargo', 'rustc', 'llvm-dis', 'spirv-val'):
                report['tools'].append(qualify_tool(name, [required[name], '--version'], directory,
                                                   report=report, stage=name + '-version'))
            rust_version = next(item['version'] for item in report['tools'] if item['name'] == 'rustc')
            need(tuple(int(item) for item in rust_version.split('.')[:2]) >= (1, 87), 'RUST_VERSION_TOO_OLD')
            try:
                metal = probe_metal(required['xcrun'], directory, report)
            except ShaderError as error:
                # Only a cleanly terminated initial timeout or unavailable
                # tool may trigger this explicitly authorized ephemeral CI step.
                # Cleanup failures and all post-download probes remain terminal.
                need(args.allow_ci_metal_download and str(error) in
                     ('STEP_TIMEOUT', 'XCODE_METAL_TOOLCHAIN_MISSING', 'TOOL_VERSION_UNVERIFIED'), str(error))
                trigger_stage, trigger_code = report['phase'], str(error)
                started = time.monotonic()
                report['metal_toolchain_download'] = {'attempted': True, 'ephemeral_ci_only': True,
                                                       'completed': False, 'returncode': None,
                                                       'trigger_stage': trigger_stage, 'trigger_code': trigger_code}
                try:
                    status, _ = reported_step('ci-metal-toolchain-download',
                        ['xcodebuild', '-downloadComponent', 'MetalToolchain'], directory, report,
                        METAL_DOWNLOAD_TIMEOUT, env=environment, slow_step='toolchain-download')
                    report['metal_toolchain_download'].update(returncode=status, completed=status == 0)
                finally:
                    report['metal_toolchain_download']['elapsed_seconds'] = round(time.monotonic() - started, 3)
                need(status == 0, 'METAL_TOOLCHAIN_DOWNLOAD_FAILED')
                metal = probe_metal(required['xcrun'], directory, report, after_download=True)
            report['tools'].append(metal)
            status, sdk = reported_step('macos-sdk-version',
                [required['xcrun'], '-sdk', 'macosx', '--show-sdk-version'], directory, report, 15)
            need(status == 0 and version(sdk) is not None, 'MACOS_SDK_UNVERIFIED')
            report['sdk_version'] = version(sdk)
            environment['METAL2VULKAN_LLVM_DIS'] = str(Path(required['llvm-dis']).resolve())
            environment['METAL2VULKAN_SPIRV_VAL'] = str(Path(required['spirv-val']).resolve())
            # Real owned AIR is useful evidence even if the later cold Rust
            # build times out. Preserve original bytes only after qualification.
            msl = read_regular(root / 'gpu-toolchain/xe2_compute_probe.metal')
            (directory / 'xe2_compute_probe.metal').write_bytes(msl)
            status, _ = reported_step('msl-to-air',
                [required['xcrun'], '-sdk', 'macosx', 'metal', '-c',
                 'xe2_compute_probe.metal', '-o', 'xe2_compute_probe.air'], directory, report, 60, env=environment)
            need(status == 0, 'MSL_AIR_COMPILATION_FAILED')
            status, _ = reported_step('air-llvm-dis',
                [required['llvm-dis'], 'xe2_compute_probe.air', '-o', 'own-air.ll'],
                directory, report, 60, env=environment)
            need(status == 0, 'AIR_LLVM_DIS_FAILED')
            report['phase'] = 'air-llvm-contract'
            air = read_regular(directory / 'xe2_compute_probe.air')
            llvm_text = read_regular(directory / 'own-air.ll')
            try:
                report['air'] = inspect_air(air, llvm_text)
            except ShaderError as error:
                if str(error) == 'AIR_PRIVATE_PATH_METADATA':
                    report['air_private_metadata'] = classify_private_air_metadata(llvm_text)
                raise
            retain_output(artifacts, report, 'xe2_compute_probe.metal', msl)
            retain_output(artifacts, report, 'xe2_compute_probe.air', air)
            report['air_compilation_verified'] = True
            try:
                inspect_llvm_export(llvm_text)
            except ShaderError as error:
                if str(error) == 'AIR_LLVM_EXPORT_PRIVATE_PATH':
                    report['air_private_metadata'] = classify_private_air_metadata(llvm_text, include_module_id=True)
                raise
            retain_output(artifacts, report, 'own-air.ll', llvm_text)
            report['air_llvm_text_exported'] = True
            report['code'] = 'AIR_PARTIAL_EVIDENCE_READY'
            write_report(artifacts / 'report.json', report)

            # Source remains the exact selected checkout. Cargo itself creates
            # the lockfile; no hand-written lock or unqualified cache is used.
            lock_path, lock = source / 'Cargo.lock', None
            resolve_status = None
            try:
                resolve_status, _ = reported_step('cargo-resolve',
                    [required['cargo'], 'generate-lockfile', '--manifest-path', str(source / 'Cargo.toml')],
                    directory, report, CARGO_RESOLVE_TIMEOUT, env=environment,
                    slow_step='cargo-resolve', cargo_progress=True)
            finally:
                if lock_path.exists() or lock_path.is_symlink():
                    lock = read_regular(lock_path)
                    summary = lock_summary(lock)
                    write_bytes_atomic(artifacts / 'Cargo.lock', lock)
                    report['cargo_lock'] = summary | {'resolver_completed': resolve_status == 0,
                                                     'unchanged_after_build': None}
                    write_report(artifacts / 'report.json', report)
            need(resolve_status == 0, 'CARGO_RESOLUTION_FAILED')
            need(lock is not None, 'CARGO_LOCK_MISSING')
            report['phase'] = 'converter-build'
            try:
                status, _ = reported_step('cargo-build',
                    [required['cargo'], 'build', '--manifest-path', str(source / 'Cargo.toml'),
                     '-p', 'metal2vulkan', '--bin', 'metal2vulkan', '--features', 'serde', '--release', '--locked',
                     '--target-dir', str(directory / 'cargo-target')], directory, report, CARGO_BUILD_TIMEOUT,
                    env=environment, slow_step='cargo-build', cargo_progress=True)
            finally:
                report['cargo_lock']['unchanged_after_build'] = read_regular(lock_path) == lock
                need(report['cargo_lock']['unchanged_after_build'], 'CARGO_LOCK_CHANGED')
            need(status == 0, 'CONVERTER_BUILD_FAILED')
            executable = directory / 'cargo-target/release/metal2vulkan'
            report['converter']['executable_sha256'] = sha(read_regular(executable, 128 * 1024 * 1024))
            report['converter_build_verified'] = True
            report['phase'] = 'dependency-provenance'
            status, metadata = reported_step('cargo-metadata',
                [required['cargo'], 'metadata', '--manifest-path', str(source / 'Cargo.toml'),
                 '--format-version', '1', '--locked', '--features', 'serde'], directory, report,
                60, env=environment, stdout_only=True)
            need(status == 0, 'CARGO_METADATA_FAILED')
            need(read_regular(lock_path) == lock, 'CARGO_LOCK_CHANGED')
            report['phase'] = 'dependency-provenance'
            report['dependencies'] = dependency_provenance(lock, metadata)
            write_report(artifacts / 'dependencies.json', report['dependencies'])
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
            for name, data in (('own-shader.spv', spirv), ('reflection.json', reflection)):
                retain_output(artifacts, report, name, data)
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
