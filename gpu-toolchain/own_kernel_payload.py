# SPDX-License-Identifier: GPL-3.0-only
"""CPU-only payload/link plan for the authenticated own OpenCL Xe2 kernel.

This module never loads a runtime, binds GPU addresses, encodes a WALKER or
executes a kernel. It is deliberately limited to one immutable own fixture.
Intel ABI constants/semantics: compute-runtime
7323fcafa8979708e73f1865c61cb9790b367948; see KERNEL_PAYLOAD.md and
KERNEL_PAYLOAD_SOURCES.json. Those upstream definitions carry MIT notices.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import sys

import inspect_own_zebin as zebin


HERE = Path(__file__).resolve().parent
RUNTIME_PIN = '7323fcafa8979708e73f1865c61cb9790b367948'
KERNEL_NAME = 'lunarbridge_xe2_probe'
PROGRAM_BYTES = 5728
PROGRAM_SHA256 = '1e0127eb0886d686e0c002c75b233fb44b936d193b82d09e34097146523e18c1'
ISA_BYTES = 896
ISA_SHA256 = '52168f1f1ee471ccc41d02256f225a55ca866bf08d599b2b2344fca7d43f1930'
ZEINFO_SHA256 = '0e1523f4ef161272599f09ae13e9c9f41e57ce9b6a0c99a49005665a58a002b7'
RELOCATIONS_SHA256 = '1e0901df04f721feb450423d70836de8b24b445c58268c542d69a1607dc91a03'
TEXT_SECTION = '.text.' + KERNEL_NAME
RELOCATION_TABLE = '.rel.text.' + KERNEL_NAME
IMPLICIT_ARGS_SYMBOL = '__INTEL_PATCH_CROSS_THREAD_OFFSET_OFF_R0'
R_ZE_SYM_ADDR_32 = 2
PATCH_OFFSETS = (76, 156)
SIMD = 32
GRF_COUNT = 128
INLINE_BYTES = 32
# max(payload.offset + payload.size) = 44; Intel decoder alignUp(..., 32).
PAYLOAD_USED_BYTES = 44
CROSS_THREAD_BYTES = (PAYLOAD_USED_BYTES + 31) // 32 * 32
INDIRECT_BYTES = CROSS_THREAD_BYTES - INLINE_BYTES
LOCAL_ID_BYTES_PER_THREAD = 64
# This is our small offline preparation budget, not a claimed hardware limit.
MAX_ELEMENTS = 4096
MAX_PLANNED_LOCAL_SIZE = 256


class PayloadError(ValueError):
    """Stable path-free reason code for a rejected preparation."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise PayloadError(reason)


def _uint32(value, name: str) -> int:
    # bool is an int in Python but is not a valid kernel numeric argument here.
    _require(type(value) is int and 0 <= value <= 0xffffffff, name + '_not_uint32')
    return value


def _disabled_bool(value, name: str) -> None:
    _require(type(value) is bool, name + '_not_bool')
    _require(value is False, name + '_unsupported')


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class OpenBinding:
    role: str
    argument_index: int
    cross_thread_offset: int
    segment: str
    segment_offset: int
    access: str
    minimum_bytes: int

    def report(self) -> dict:
        return {'role': self.role, 'argument_index': self.argument_index,
                'cross_thread_offset': self.cross_thread_offset,
                'segment': self.segment, 'segment_offset': self.segment_offset,
                'pointer_bytes': 8, 'access': self.access,
                'minimum_buffer_bytes': self.minimum_bytes, 'resolved': False,
                'placeholder_uint64': 0,
                'requires': 'native validated GPU-VA and DMA mapping'}


@dataclass(frozen=True)
class RelocationDecision:
    offset: int
    original: int
    value: int

    def report(self) -> dict:
        return {'target_section': TEXT_SECTION, 'offset': self.offset,
                'type': R_ZE_SYM_ADDR_32, 'type_name': 'R_ZE_SYM_ADDR_32',
                'symbol': IMPLICIT_ARGS_SYMBOL, 'patch_bytes': 4,
                'original_uint32': self.original, 'resolved_uint32': self.value,
                'action': 'preserve_original_zero_without_implicit_args'}


@dataclass(frozen=True)
class PreparedOwnKernel:
    cross_thread: bytes
    inline: bytes
    indirect: bytes
    isa: bytes
    count: int
    global_size_x: int
    local_size_x: int
    bindings: tuple[OpenBinding, ...]
    relocations: tuple[RelocationDecision, ...]

    def report(self) -> dict:
        return {
            'schema': 'lunarbridge-own-opencl-xe2-payload-v1',
            'status': 'cpu_template_prepared_with_unresolved_gpu_bindings',
            'kernel_name': KERNEL_NAME,
            'intel_runtime_abi_reference_commit': RUNTIME_PIN,
            'own_program_sha256': PROGRAM_SHA256,
            'historical_windows_fixture_authentication': True,
            'new_gpu_execution_performed': False,
            'shader_compilation_performed': False,
            'air_msl_translation_implemented': False,
            'native_gpu_bindings_implemented': False,
            'native_gpu_submission_implemented': False,
            'native_macos_metal_execution_demonstrated': False,
            'dispatch_ready': False,
            'layout': {
                'abi': 'Intel OpenCL Zebin; not Vulkan Shader SPIR-V',
                'simd_size': SIMD, 'grf_count': GRF_COUNT,
                'argument_data_end': PAYLOAD_USED_BYTES,
                'cross_thread_bytes': len(self.cross_thread),
                'inline_bytes': len(self.inline),
                'indirect_cross_thread_bytes': len(self.indirect),
                'per_thread_data_offset_after_inline': INDIRECT_BYTES,
                'cross_thread_hex': self.cross_thread.hex(),
                'inline_hex': self.inline.hex(), 'indirect_hex': self.indirect.hex(),
                'cross_thread_sha256': _sha256(self.cross_thread),
                'inline_sha256': _sha256(self.inline),
                'indirect_sha256': _sha256(self.indirect),
                'padding_offset': PAYLOAD_USED_BYTES,
                'padding_bytes': CROSS_THREAD_BYTES - PAYLOAD_USED_BYTES,
                'padding_is_zero': not any(self.cross_thread[PAYLOAD_USED_BYTES:]),
            },
            'planned_dispatch': {
                'work_dimensions': 1, 'global_offset': [0, 0, 0],
                'global_size': [self.global_size_x, 1, 1],
                'enqueued_local_size': [self.local_size_x, 1, 1],
                'group_count': [self.global_size_x // self.local_size_x, 1, 1],
                'count_argument': self.count,
                'planned_local_size_was_hardware_qualified': False,
                'historical_windows_launch_requested_local_size': None,
                'historical_windows_effective_local_size_known': False,
                'minimum_input_buffer_bytes': self.count * 4,
                'minimum_output_buffer_bytes': self.count * 4,
                'local_id_bytes_per_simd_thread_from_metadata': LOCAL_ID_BYTES_PER_THREAD,
                'local_id_payload_generated': False,
                'hardware_local_id_generation_selected': False,
                'entry_offset_skip_per_thread_data_load_from_metadata': 128,
                'entrypoint_selection_implemented': False,
                'walker_command_encoded': False,
                'implicit_args_required': False,
                'debugger_active': False, 'stack_calls_enabled': False,
            },
            'unresolved_bindings': [binding.report() for binding in self.bindings],
            'isa_link': {
                'bytes': len(self.isa), 'original_sha256': ISA_SHA256,
                'prepared_sha256': _sha256(self.isa),
                'bytes_changed': 0,
                'implicit_args_bytes': 0,
                'relocations': [relocation.report() for relocation in self.relocations],
            },
            'remaining_native_requirements': [
                'validated GPU-VA ownership and DMA bindings for both buffers',
                'per-thread local-ID/entrypoint/WALKER handling',
                'ISA/indirect heap allocation and Xe2 command encoding',
                'native queue, GuC scheduling, completion and synchronization',
                'Metal provider and a separately validated AIR/MSL compiler route',
            ],
        }


def _validate_relocations(isa: bytes, relocations: list[dict]) -> tuple[RelocationDecision, ...]:
    """Accept only the two observed 32-bit implicit-size relocations."""
    _require(type(isa) is bytes and len(isa) == ISA_BYTES, 'isa_size_or_type')
    _require(type(relocations) is list and len(relocations) == 2, 'relocation_count_or_type')
    decisions = []
    for expected_offset, relocation in zip(PATCH_OFFSETS, relocations):
        _require(type(relocation) is dict, 'relocation_record_type')
        _require(type(relocation.get('offset')) is int and relocation['offset'] == expected_offset,
                 'relocation_offset_or_order')
        _require(relocation.get('table') == RELOCATION_TABLE and
                 relocation.get('target_section') == TEXT_SECTION, 'relocation_section')
        _require(type(relocation.get('type')) is int and
                 relocation['type'] == R_ZE_SYM_ADDR_32, 'relocation_type')
        _require(type(relocation.get('patch_bytes')) is int and
                 relocation['patch_bytes'] == 4, 'relocation_width')
        _require(relocation.get('symbol_name') == IMPLICIT_ARGS_SYMBOL, 'relocation_symbol')
        _require('explicit_addend' not in relocation, 'rela_not_supported_for_own_fixture')
        original = struct.unpack_from('<I', isa, expected_offset)[0]
        _require(original == 0 and relocation.get('implicit_addend_raw_hex') == '00000000',
                 'relocation_original_not_zero')
        # The pinned Intel linker does not patch this symbol when implicit args
        # are disabled. Retain the authenticated original value; do not patch
        # cross-thread size (64), inline size (32), or a guessed GPU address.
        decisions.append(RelocationDecision(expected_offset, original, 0))
    return tuple(decisions)


def prepare(program: bytes, *, count: int = 64, local_size: int = 32,
            debugger_active: bool = False, stack_calls_enabled: bool = False,
            implicit_args_required: bool = False) -> PreparedOwnKernel:
    """Produce an offline template with zero pointer holes, never a GPU job.

    No pointer/address input is accepted. Callers must not interpret the result
    as bound memory. Only complete 1-D groups and the own bounded count are
    planned; a rounded-up global size is safe because the own kernel checks
    index < count before either buffer access.
    """
    count = _uint32(count, 'count')
    local_size = _uint32(local_size, 'local_size')
    _require(1 <= count <= MAX_ELEMENTS, 'count_outside_preparation_budget')
    _require(SIMD <= local_size <= MAX_PLANNED_LOCAL_SIZE and local_size % SIMD == 0,
             'local_size_requires_bounded_complete_simd32')
    _disabled_bool(debugger_active, 'debugger')
    _disabled_bool(stack_calls_enabled, 'stack_calls')
    _disabled_bool(implicit_args_required, 'implicit_args')
    _require(type(program) is bytes and len(program) == PROGRAM_BYTES, 'program_size_or_type')
    _require(_sha256(program) == PROGRAM_SHA256, 'program_not_authenticated_own_fixture')
    try:
        decoded = zebin.inspect(program)
        sections = {item['name']: item for item in decoded['sections']}
        _require(sections[TEXT_SECTION]['sha256'] == ISA_SHA256, 'own_isa_digest')
        _require(sections['.ze_info']['sha256'] == ZEINFO_SHA256, 'own_metadata_digest')
        _require(sections[RELOCATION_TABLE]['sha256'] == RELOCATIONS_SHA256, 'own_relocations_digest')
        isa = zebin.section_bytes(program, sections[TEXT_SECTION])
        decisions = _validate_relocations(isa, decoded['relocations'])
    except (zebin.FormatError, KeyError, UnicodeError, struct.error) as exc:
        raise PayloadError('own_program_structure_invalid') from exc
    global_size = (count + local_size - 1) // local_size * local_size
    _require(global_size <= MAX_ELEMENTS, 'rounded_global_size_outside_preparation_budget')
    payload = bytearray(CROSS_THREAD_BYTES)
    # Verified ZEInfo offsets: global_id_offset=0, enqueued_local_size=12,
    # input uint64 pointer=24, output uint64 pointer=32, count uint32=40.
    struct.pack_into('<3I', payload, 0, 0, 0, 0)
    struct.pack_into('<3I', payload, 12, local_size, 1, 1)
    struct.pack_into('<I', payload, 40, count)
    cross_thread = bytes(payload)
    bindings = (
        OpenBinding('input', 0, 24, 'inline', 24, 'readonly', count * 4),
        OpenBinding('output', 1, 32, 'indirect', 0, 'readwrite', count * 4),
    )
    return PreparedOwnKernel(cross_thread, cross_thread[:INLINE_BYTES], cross_thread[INLINE_BYTES:],
                             isa, count, global_size, local_size, bindings, decisions)


def read_own_fixture() -> bytes:
    # Bounded even if the local fixture was replaced with an oversized file.
    with (HERE / 'fixtures' / 'own-xe2-program.bin').open('rb') as source:
        data = source.read(PROGRAM_BYTES + 1)
    _require(len(data) == PROGRAM_BYTES, 'fixture_size')
    return data


def write_bundle(prepared: PreparedOwnKernel, destination: Path) -> dict:
    """Write only into a new explicitly selected directory; never overwrite."""
    _require(type(prepared) is PreparedOwnKernel, 'prepared_type')
    _require(isinstance(destination, Path), 'destination_type')
    # A caller-constructed dataclass is not accepted as trusted report input.
    expected = prepare(read_own_fixture(), count=prepared.count, local_size=prepared.local_size_x)
    _require(PreparedOwnKernel.__eq__(prepared, expected), 'prepared_object_not_canonical')
    # Export only freshly authenticated canonical data. A caller object cannot
    # override the report or substitute bytes through polymorphic equality.
    report = expected.report()
    contents = {'cross-thread.bin': expected.cross_thread,
                'inline.bin': expected.inline, 'indirect-cross-thread.bin': expected.indirect,
                'own-isa-no-implicit-args.bin': expected.isa}
    report['bundle_files'] = [{'filename': name, 'bytes': len(data), 'sha256': _sha256(data)}
                              for name, data in contents.items()]
    destination.mkdir(parents=False, exist_ok=False)
    for name, data in contents.items():
        with (destination / name).open('xb') as output:
            output.write(data)
    with (destination / 'PAYLOAD_REPORT.json').open('x', encoding='utf-8', newline='\n') as output:
        json.dump(report, output, indent=2, ensure_ascii=False)
        output.write('\n')
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Prepare own Xe2 payload bytes offline; no GPU execution.')
    parser.add_argument('--count', type=int, default=64)
    parser.add_argument('--local-size', type=int, default=32,
                        help='planned SIMD32-multiple, not a hardware-qualified dispatch size')
    parser.add_argument('--output-dir', type=Path, help='explicit new directory for CPU-only artifacts')
    args = parser.parse_args(argv)
    try:
        prepared = prepare(read_own_fixture(), count=args.count, local_size=args.local_size)
        report = prepared.report() if args.output_dir is None else write_bundle(prepared, args.output_dir)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except PayloadError as exc:
        print(json.dumps({'status': 'rejected', 'reason': str(exc)}))
    except OSError:
        # No local paths, usernames or OS error strings enter exported reports.
        print(json.dumps({'status': 'rejected', 'reason': 'fixture_or_output_io_error'}))
    return 2


if __name__ == '__main__':
    sys.exit(main())
