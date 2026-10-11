# SPDX-License-Identifier: GPL-3.0-only
"""Bounded, non-executing ELF64 inspector for the one own GPU program."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

WORK = Path(__file__).resolve().parent
MAX_BINARY = 16 * 1024 * 1024
MAX_SECTIONS = 256
MAX_SECTION_NAMES = 65536
EXPECTED_BINARY_SHA256 = '1e0127eb0886d686e0c002c75b233fb44b936d193b82d09e34097146523e18c1'


class FormatError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise FormatError(message)


def span(data, offset, size, label):
    require(isinstance(offset, int) and isinstance(size, int) and offset >= 0 and size >= 0,
            label + '_invalid_range')
    require(offset <= len(data) and size <= len(data) - offset, label + '_out_of_bounds')
    return data[offset:offset + size]


def terminated_string(data, offset, limit=512):
    require(0 <= offset < len(data), 'string_offset_out_of_bounds')
    end = data.find(b'\0', offset, min(len(data), offset + limit + 1))
    require(end >= 0, 'string_unterminated_or_over_budget')
    value = data[offset:end]
    require(all(32 <= byte < 127 for byte in value), 'string_non_ascii_printable')
    return value.decode('ascii')


def parse_elf64(data):
    require(64 <= len(data) <= MAX_BINARY, 'elf_size_budget')
    header = struct.unpack('<16sHHIQQQIHHHHHH', span(data, 0, 64, 'elf_header'))
    ident, elf_type, machine, version, entry, phoff, shoff, flags, ehsize, phentsize, phnum, shentsize, shnum, shstrndx = header
    require(ident[:7] == b'\x7fELF\x02\x01\x01', 'requires_elf64_little_endian_v1')
    require(version == 1 and ehsize == 64, 'elf_header_version_or_size')
    require(elf_type == 1 and machine == 205 or elf_type == 0xff12, 'not_supported_intel_gpu_zebin')
    require(entry == 0 and phoff == 0 and phnum == 0, 'unexpected_program_headers_or_entry')
    require(shentsize == 64 and 1 < shnum <= MAX_SECTIONS and 0 < shstrndx < shnum,
            'section_count_size_or_name_table')
    require(shoff >= ehsize and shoff % 8 == 0, 'section_table_offset_or_alignment')
    span(data, shoff, shentsize * shnum, 'section_table')
    sections = []
    for index in range(shnum):
        values = struct.unpack('<IIQQQQIIQQ', span(data, shoff + index * shentsize, 64, 'section_header'))
        name, kind, section_flags, address, offset, size, link, info, alignment, entry_size = values
        require(alignment == 0 or alignment <= 1024 * 1024 and alignment & (alignment - 1) == 0,
                'section_alignment')
        require(link < shnum, 'section_link_out_of_bounds')
        require(entry_size == 0 or size % entry_size == 0, 'section_entry_size')
        if kind != 8:  # SHT_NOBITS has no file payload.
            span(data, offset, size, 'section_payload')
        sections.append({'index': index, 'name_offset': name, 'type': kind, 'flags': section_flags,
                         'address': address, 'offset': offset, 'bytes': size, 'link': link,
                         'info': info, 'alignment': alignment, 'entry_size': entry_size})
    null_header = struct.unpack('<IIQQQQIIQQ', span(data, shoff, 64, 'null_section'))
    # SHT_NULL's file offset is ignored; Intel's encoder uses the first payload
    # offset even though this entry has no payload. Extended count encodings
    # remain unsupported (size/link/info must be zero).
    require(all(value == 0 for index, value in enumerate(null_header) if index != 4),
            'section_zero_not_null')
    names_section = sections[shstrndx]
    require(names_section['type'] == 3 and 0 < names_section['bytes'] <= MAX_SECTION_NAMES,
            'section_names_type_or_budget')
    names = span(data, names_section['offset'], names_section['bytes'], 'section_names')
    seen = set()
    for section in sections:
        name = terminated_string(names, section.pop('name_offset'))
        require(not name or name not in seen, 'duplicate_section_name')
        seen.add(name)
        section['name'] = name
        payload = b'' if section['type'] == 8 else span(data, section['offset'], section['bytes'], 'payload')
        section['sha256'] = hashlib.sha256(payload).hexdigest()
    occupied = [(0, ehsize, 'elf_header'), (shoff, shoff + shentsize * shnum, 'section_table')]
    for section in sections:
        if section['type'] != 8 and section['bytes']:
            begin, end = section['offset'], section['offset'] + section['bytes']
            require(not any(begin < previous_end and previous_begin < end
                            for previous_begin, previous_end, _ in occupied), 'overlapping_file_sections')
            occupied.append((begin, end, section['name']))
    return {'format': 'ELF64 little-endian Intel GPU', 'elf_type': elf_type, 'machine': machine,
            'machine_name': 'EM_INTELGT' if machine == 205 else 'legacy_zebin_machine',
            'header_flags': flags, 'section_count': shnum, 'sections': sections}


def section_bytes(data, section):
    require(section['type'] != 8, 'cannot_extract_nobits')
    return span(data, section['offset'], section['bytes'], 'selected_section')


def parse_notes(data):
    require(len(data) <= 65536, 'note_section_budget')
    notes, offset = [], 0
    while offset < len(data):
        require(len(notes) < 32, 'note_count_budget')
        namesz, descsz, kind = struct.unpack('<III', span(data, offset, 12, 'note_header'))
        require(0 < namesz <= 256 and descsz <= 16384, 'note_value_budget')
        name_offset = offset + 12
        name = span(data, name_offset, namesz, 'note_name')
        require(name.endswith(b'\0'), 'note_owner_unterminated')
        owner = terminated_string(name, 0, 256)
        desc_offset = name_offset + (namesz + 3) // 4 * 4
        descriptor = span(data, desc_offset, descsz, 'note_descriptor')
        next_offset = desc_offset + (descsz + 3) // 4 * 4
        span(data, offset, next_offset - offset, 'note_alignment_padding')
        item = {'owner': owner, 'type': kind, 'bytes': descsz, 'descriptor_hex': descriptor.hex()}
        if descsz == 4:
            item['uint32_value'] = struct.unpack('<I', descriptor)[0]
        if owner == 'IntelGT':
            item['intelgt_kind'] = {1: 'product_family', 2: 'gfx_core', 3: 'target_metadata',
                                    4: 'zebin_version', 5: 'visa_abi_version', 6: 'product_config',
                                    7: 'indirect_access_detection_version',
                                    8: 'indirect_access_buffer_major_version'}.get(kind, 'unknown')
        notes.append(item)
        offset = next_offset
    return notes


def parse_symbols_and_relocations(data, sections):
    symbols_by_section, public_symbols, relocations = {}, [], []
    for table in sections:
        if table['type'] != 2:
            continue
        require(table['entry_size'] == 24 and table['bytes'] // 24 <= 1024, 'symbol_table_budget')
        name_table = sections[table['link']]
        require(name_table['type'] == 3 and name_table['bytes'] <= MAX_SECTION_NAMES, 'symbol_name_table')
        names = section_bytes(data, name_table)
        table_bytes = section_bytes(data, table)
        entries = []
        for offset in range(0, len(table_bytes), 24):
            name_offset, info, other, shndx, value, size = struct.unpack_from('<IBBHQQ', table_bytes, offset)
            require(shndx < len(sections) or shndx == 0xfff1, 'symbol_section_out_of_bounds')
            item = {'index': offset // 24, 'name': terminated_string(names, name_offset),
                    'binding': info >> 4, 'type': info & 15, 'visibility': other & 3,
                    'section_index': shndx, 'value': value, 'bytes': size,
                    'symbol_table': table['name']}
            if 0 < shndx < len(sections):
                require(value <= sections[shndx]['bytes'] and size <= sections[shndx]['bytes'] - value,
                        'symbol_value_out_of_bounds')
            entries.append(item)
            public_symbols.append(item)
        symbols_by_section[table['index']] = entries
    for table in sections:
        if table['type'] not in (4, 9):
            continue
        entry_size = 24 if table['type'] == 4 else 16
        require(table['entry_size'] == entry_size and table['bytes'] // entry_size <= 1024,
                'relocation_table_budget')
        require(table['link'] in symbols_by_section and 0 < table['info'] < len(sections),
                'relocation_symbol_or_target_section')
        symbols = symbols_by_section[table['link']]
        target = sections[table['info']]
        table_bytes = section_bytes(data, table)
        for position in range(0, len(table_bytes), entry_size):
            values = struct.unpack_from('<QQq' if entry_size == 24 else '<QQ', table_bytes, position)
            offset, info = values[:2]
            symbol_index, kind = info >> 32, info & 0xffffffff
            require(symbol_index < len(symbols), 'relocation_symbol_out_of_bounds')
            patch_bytes = {0: 0, 1: 8, 2: 4, 3: 4, 4: 4}.get(kind)
            require(patch_bytes is not None, 'unsupported_intelgt_relocation')
            require(offset <= target['bytes'] and patch_bytes <= target['bytes'] - offset,
                    'relocation_patch_out_of_bounds')
            item = {'table': table['name'], 'target_section': target['name'], 'offset': offset,
                    'type': kind, 'type_name': {0: 'R_ZE_NONE', 1: 'R_ZE_SYM_ADDR',
                                              2: 'R_ZE_SYM_ADDR_32', 3: 'R_ZE_SYM_ADDR_32_HI',
                                              4: 'R_PER_THREAD_PAYLOAD_OFFSET'}[kind],
                    'symbol_index': symbol_index, 'symbol_name': symbols[symbol_index]['name'],
                    'patch_bytes': patch_bytes}
            if entry_size == 24:
                item['explicit_addend'] = values[2]
            else:
                item['implicit_addend_raw_hex'] = span(section_bytes(data, target), offset,
                                                       patch_bytes, 'relocation_addend').hex()
            relocations.append(item)
    return public_symbols, relocations


def inspect(data):
    report = parse_elf64(data)
    by_name = {section['name']: section for section in report['sections']}
    text = by_name.get('.text.lunarbridge_xe2_probe')
    ze = by_name.get('.ze_info')
    require(text is not None and text['type'] == 1 and text['flags'] & 4 and text['bytes'] > 0,
            'own_kernel_text_missing_or_not_executable')
    require(ze is not None and ze['type'] == 0xff000011 and 0 < ze['bytes'] <= 65536,
            'own_zebin_metadata_missing')
    ze_text = section_bytes(data, ze).decode('utf-8')
    require('lunarbridge_xe2_probe' in ze_text and '\x00' not in ze_text, 'own_kernel_metadata_mismatch')
    report['classification'] = 'zebin_sections_in_intelgt_elf'
    report['raw_isa'] = {'section': text['name'], 'bytes': text['bytes'], 'sha256': text['sha256'],
                         'human_disassembly_available': False}
    report['ze_info_utf8'] = ze_text
    note = by_name.get('.note.intelgt.compat')
    report['intelgt_notes'] = [] if note is None else parse_notes(section_bytes(data, note))
    report['symbols'], report['relocations'] = parse_symbols_and_relocations(data, report['sections'])
    report['relocations_applied_by_inspector'] = False
    report['other_note_sections_are_opaque'] = [section['name'] for section in report['sections']
                                               if section['type'] == 7 and section['name'] != '.note.intelgt.compat']
    report['native_macos_execution_demonstrated'] = False
    report['binary_executed_by_inspector'] = False
    return report


def main():
    parser = argparse.ArgumentParser(description='Read-only bounded own-kernel ELF/Zebin inspection; no GPU APIs or execution.')
    parser.add_argument('--binary', type=Path, default=WORK / 'fixtures' / 'own-xe2-program.bin')
    parser.add_argument('--output-dir', type=Path, help='Optional NEW directory for byte-identical selected section exports.')
    args = parser.parse_args()
    with args.binary.open('rb') as stream:
        binary = stream.read(MAX_BINARY + 1)
    require(64 <= len(binary) <= MAX_BINARY, 'binary_file_size_budget')
    report = inspect(binary)
    by_name = {section['name']: section for section in report['sections']}
    exports = {
        '.text.lunarbridge_xe2_probe': 'own-xe2-kernel-isa.bin',
        '.ze_info': 'own-xe2-ze-info.yaml',
        '.note.intelgt.compat': 'own-xe2-intelgt-note.bin',
        '.spv': 'own-xe2-kernel.spv',
        '.rel.text.lunarbridge_xe2_probe': 'own-xe2-kernel-relocations.bin',
    }
    report['exports'] = []
    if args.output_dir is not None:
        args.output_dir = args.output_dir.resolve()
        args.output_dir.mkdir(parents=True, exist_ok=False)
    for name, filename in exports.items():
        if name in by_name:
            payload = section_bytes(binary, by_name[name])
            if args.output_dir is not None:
                (args.output_dir / filename).write_bytes(payload)
            report['exports'].append({'filename': filename, 'source_section': name, 'bytes': len(payload),
                                      'sha256': hashlib.sha256(payload).hexdigest()})
    report['source_program_bytes'] = len(binary)
    report['source_program_sha256'] = hashlib.sha256(binary).hexdigest()
    report['inspector_source_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['matches_original_verified_fixture'] = hashlib.sha256(binary).hexdigest() == EXPECTED_BINARY_SHA256
    report['section_files_written'] = args.output_dir is not None
    if args.output_dir is not None:
        (args.output_dir / 'OWN_SHADER_BINARY_METADATA.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (FormatError, UnicodeError, OSError) as error:
        print(json.dumps({'result': 'not_verified', 'error': str(error) if isinstance(error, FormatError) else type(error).__name__}))
        raise SystemExit(1)
