# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Read-only call-site observations from a local x86_64 dyld cache.

This recovers literals at IOKit calls, not a complete or executable driver ABI.
Slide-v2 chain membership is checked before resolving an indirect pointer.
Capstone is optional until analyze() is called. No Apple code is emitted.
"""
from __future__ import annotations
import argparse
import bisect
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import sys
import recovery_graphics as fmt


def function_starts(cache, image, description):
    count, size = struct.unpack_from('<II', cache.read(image['address'], 32), 16)
    commands = cache.read(image['address'] + 32, size)
    found = None
    cursor = 0
    for _ in range(count):
        code, length = struct.unpack('<II', fmt.region(commands, cursor, 8))
        fmt.require(length >= 8 and length % 8 == 0, 'FUNCTION_COMMAND_INVALID')
        fmt.region(commands, cursor, length)
        if code == 0x26:
            fmt.require(length == 16 and found is None, 'FUNCTION_STARTS_COMMAND_INVALID')
            offset, length_bytes = struct.unpack_from('<II', commands, cursor + 8)
            fmt.require(0 < length_bytes <= 1024 * 1024, 'FUNCTION_STARTS_SIZE_INVALID')
            found = cache.read_image_offset(description, offset, length_bytes)
        cursor += length
    fmt.require(cursor == size and found is not None, 'FUNCTION_STARTS_REQUIRED')
    text = [s for s in description['sections'] if s['name'] == '__text']
    fmt.require(len(text) == 1, 'FUNCTION_TEXT_REQUIRED')
    current, position, result = image['address'], 0, []
    while position < len(found):
        delta, position = fmt.uleb(found, position)
        if delta == 0:
            fmt.require(not any(found[position:]), 'FUNCTION_STARTS_PADDING_INVALID')
            return result
        current += delta
        fmt.require(text[0]['address'] <= current < text[0]['address'] + text[0]['bytes'],
                    'FUNCTION_START_OUTSIDE_TEXT')
        result.append(current)
        fmt.require(len(result) <= 65536, 'FUNCTION_STARTS_LIMIT')
    raise fmt.FormatError('FUNCTION_STARTS_UNTERMINATED')


class SlideV2:
    def __init__(self, cache):
        self.cache = cache
        self.slides = []
        for filename, data in cache.files.items():
            offset, count = struct.unpack('<II', fmt.region(data, 312, 8))
            fmt.require(0 < count <= 64, 'SLIDE_MAPPING_COUNT_INVALID')
            for index in range(count):
                address, size, file_offset, slide_offset, slide_size, flags, maximum, initial = struct.unpack(
                    '<6Q2I', fmt.region(data, offset + 56 * index, 56))
                match = [m for m in cache.mappings if m['file'] == filename and m['address'] == address
                         and m['bytes'] == size and m['offset'] == file_offset]
                fmt.require(len(match) == 1, 'SLIDE_MAPPING_MISMATCH')
                if not slide_size:
                    continue
                raw = fmt.region(data, slide_offset, slide_size, 1024 * 1024)
                fmt.require(len(raw) >= 40, 'SLIDE_HEADER_TRUNCATED')
                version, page, starts_off, starts_count, extras_off, extras_count, mask, add = struct.unpack_from('<6I2Q', raw)
                fmt.require(version == 2, 'SLIDE_VERSION_UNSUPPORTED')
                fmt.require(page in (4096, 16384) and starts_count == (size + page - 1) // page,
                            'SLIDE_PAGE_COUNT_INVALID')
                fmt.require(starts_off >= 40 and (extras_count == 0 or extras_off >= 40), 'SLIDE_TABLE_OFFSET_INVALID')
                starts = fmt.region(raw, starts_off, starts_count * 2)
                extras = fmt.region(raw, extras_off, extras_count * 2)
                fmt.require(mask > 0, 'SLIDE_MASK_INVALID')
                low = (mask & -mask).bit_length() - 1
                contiguous = mask >> low
                fmt.require(low >= 2 and contiguous & (contiguous + 1) == 0, 'SLIDE_MASK_INVALID')
                self.slides.append({'address': address, 'bytes': size, 'page': page, 'starts': starts,
                                    'extras': extras, 'mask': mask, 'add': add, 'shift': low - 2})

    def pointer(self, address):
        matches = [s for s in self.slides if s['address'] <= address and address + 8 <= s['address'] + s['bytes']]
        fmt.require(len(matches) == 1, 'POINTER_SLIDE_MAPPING_REQUIRED')
        slide = matches[0]
        page_index, within = divmod(address - slide['address'], slide['page'])
        entry = struct.unpack_from('<H', slide['starts'], page_index * 2)[0]
        fmt.require(entry != 0x4000, 'POINTER_PAGE_NOT_REBASED')
        chain_starts = []
        if entry & 0x8000:
            index = entry & 0x3fff
            for _ in range(slide['page'] // 4):
                value = struct.unpack('<H', fmt.region(slide['extras'], index * 2, 2))[0]
                chain_starts.append((value & 0x3fff) * 4)
                index += 1
                if value & 0x8000:
                    break
            else:
                raise fmt.FormatError('SLIDE_EXTRAS_UNTERMINATED')
        else:
            fmt.require(entry & 0x4000 == 0, 'SLIDE_START_FLAGS_INVALID')
            chain_starts.append(entry * 4)
        page_address = slide['address'] + page_index * slide['page']
        seen = set()
        answer = None
        for start in chain_starts:
            position = start
            for _ in range(slide['page'] // 4):
                fmt.require(position % 4 == 0 and position + 8 <= slide['page'] and position not in seen,
                            'SLIDE_CHAIN_INVALID')
                seen.add(position)
                raw = struct.unpack('<Q', self.cache.read(page_address + position, 8))[0]
                if position == within:
                    value = raw & (~slide['mask'] & 0xffffffffffffffff)
                    answer = value + slide['add'] if value else 0
                delta = (raw & slide['mask']) >> slide['shift']
                if not delta:
                    break
                position += delta
            else:
                raise fmt.FormatError('SLIDE_CHAIN_LIMIT')
        fmt.require(answer is not None and answer <= 0xffffffffffffffff, 'POINTER_NOT_IN_SLIDE_CHAIN')
        if answer:
            self.cache.read(answer, 1)
        return answer


def canonical(name):
    aliases = {'eax': 'rax', 'ax': 'rax', 'al': 'rax', 'edi': 'rdi', 'di': 'rdi', 'dil': 'rdi',
               'esi': 'rsi', 'si': 'rsi', 'sil': 'rsi', 'edx': 'rdx', 'dx': 'rdx', 'dl': 'rdx',
               'ecx': 'rcx', 'cx': 'rcx', 'cl': 'rcx', 'ah': 'rax', 'ch': 'rcx', 'dh': 'rdx',
               'ebx': 'rbx', 'bx': 'rbx', 'bl': 'rbx', 'bh': 'rbx', 'ebp': 'rbp', 'bp': 'rbp',
               'esp': 'rsp', 'sp': 'rsp', 'spl': 'rsp', 'bpl': 'rbp'}
    if name in aliases:
        return aliases[name]
    if name.startswith('r') and name.endswith(('d', 'w', 'b')):
        return name[:-1]
    return name


def literal_arguments(instructions, call_index):
    """Only literals in the preceding straight-line block, no branch joins.

    Unknown memory reads and partial-register writes erase prior knowledge.
    Stack references only identify a local capacity cell, not its runtime VA.
    """
    from capstone.x86 import X86_OP_REG, X86_OP_IMM, X86_OP_MEM
    start = call_index
    while start > 0:
        previous = instructions[start - 1]
        if previous.mnemonic.startswith(('j', 'ret', 'loop')) or previous.mnemonic in ('call', 'syscall'):
            break
        start -= 1
    values, cells = {}, {}
    for insn in instructions[start:call_index]:
        ops = insn.operands
        _, writes = insn.regs_access()
        old = dict(values)
        if any(o.type == X86_OP_MEM and o.access & 2 for o in ops):
            cells.clear()
        for reg in writes:
            values.pop(canonical(insn.reg_name(reg)), None)
        if len(ops) != 2:
            continue
        left, right = ops
        target = canonical(insn.reg_name(left.reg)) if left.type == X86_OP_REG else None
        full = left.size >= 4
        if insn.mnemonic == 'mov' and target and full:
            if right.type == X86_OP_IMM:
                values[target] = right.imm & ((1 << (left.size * 8)) - 1)
            elif right.type == X86_OP_REG:
                value = old.get(canonical(insn.reg_name(right.reg)))
                if isinstance(value, int):
                    value &= (1 << (left.size * 8)) - 1
                elif left.size != 8:
                    value = None
                if value is not None:
                    values[target] = value
        elif insn.mnemonic == 'xor' and target and full and right.type == X86_OP_REG and left.reg == right.reg:
            values[target] = 0
        elif insn.mnemonic == 'lea' and target and right.type == X86_OP_MEM and not right.mem.index:
            base = insn.reg_name(right.mem.base)
            if base == 'rbp':
                values[target] = ('local-cell', right.mem.disp)
        if insn.mnemonic == 'mov' and left.type == X86_OP_MEM and not left.mem.index:
            base = insn.reg_name(left.mem.base)
            cell = ('local-cell', left.mem.disp) if base == 'rbp' else old.get(canonical(base))
            if isinstance(cell, tuple) and cell[0] == 'local-cell':
                cell = (cell[0], cell[1] + (0 if base == 'rbp' else left.mem.disp))
                cells.pop(cell, None)
                if right.type == X86_OP_IMM and left.size == 8:
                    cells[cell] = right.imm & 0xffffffffffffffff
                elif right.type == X86_OP_REG and left.size == 8:
                    value = old.get(canonical(insn.reg_name(right.reg)))
                    if isinstance(value, int):
                        cells[cell] = value
    result = {r: values.get(r) if isinstance(values.get(r), int) else None
              for r in ('rdi', 'rsi', 'rdx', 'rcx', 'r8', 'r9')}
    result['r9_capacity_cell'] = cells.get(values.get('r9'))
    return result


def analyze(folder):
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64, __version__
    from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_REG_RIP
    engine = Cs(CS_ARCH_X86, CS_MODE_64)
    engine.detail = True
    names = ('dyld_shared_cache_x86_64', 'dyld_shared_cache_x86_64.01')
    input_hashes = {n: fmt.sha256(folder / n) for n in names}
    with ExitStack() as stack:
        cache = fmt.DyldCache({n: stack.enter_context(fmt.mapped(folder / n)) for n in names})
        slides = SlideV2(cache)
        images = [x for x in cache.images if '/IOAccelerator.framework/' in x['path'] or '/IOKit.framework/' in x['path']]
        fmt.require(len(images) == 2, 'IOACCEL_AND_IOKIT_IMAGES_REQUIRED')
        by_address, parsed = {}, []
        for image in images:
            description = fmt.macho(cache.read, image['address'])
            offset, size = description['exports_trie']
            exports = fmt.exported_symbols(cache.read_image_offset(description, offset, size))
            for entry in exports:
                if not entry['reexport'] and entry['flags'] & 3 == 0:
                    by_address.setdefault(image['address'] + entry['address_offset'], []).append(entry['name'])
            parsed.append((image, description, exports))
        calls = []
        function_count = 0
        for image, description, exports in parsed:
            if '/IOAccelerator.framework/' not in image['path']:
                continue
            boundaries = function_starts(cache, image, description)
            text = [s for s in description['sections'] if s['name'] == '__text'][0]
            stubs = [s for s in description['sections'] if s['name'] == '__stubs']
            for entry in exports:
                if entry['reexport'] or entry['flags'] & 3:
                    continue
                address = image['address'] + entry['address_offset']
                if not text['address'] <= address < text['address'] + text['bytes']:
                    continue
                fmt.require(address in boundaries, 'EXPORTED_FUNCTION_START_NOT_QUALIFIED')
                function_count += 1
                index = bisect.bisect_right(boundaries, address)
                end = boundaries[index] if index < len(boundaries) else text['address'] + text['bytes']
                fmt.require(0 < end - address <= 16384, 'FUNCTION_BYTES_LIMIT')
                raw = cache.read(address, end - address)
                instructions = list(engine.disasm(raw, address))
                decoded = sum(x.size for x in instructions)
                padding = raw[decoded:]
                fmt.require(not padding or len(padding) <= 15 and not any(padding), 'FUNCTION_DECODE_INCOMPLETE')
                for index, insn in enumerate(instructions):
                    if insn.mnemonic not in ('call', 'jmp') or len(insn.operands) != 1 or insn.operands[0].type != X86_OP_IMM:
                        continue
                    target = insn.operands[0].imm
                    labels = by_address.get(target, [])
                    pointer_chain_checked = False
                    if not labels and any(s['address'] <= target and target + 6 <= s['address'] + s['bytes'] for s in stubs):
                        stub = next(engine.disasm(cache.read(target, 6), target), None)
                        if stub and stub.mnemonic == 'jmp' and len(stub.operands) == 1:
                            operand = stub.operands[0]
                            if operand.type == X86_OP_MEM and operand.mem.base == X86_REG_RIP and not operand.mem.index:
                                pointer = stub.address + stub.size + operand.mem.disp
                                target = slides.pointer(pointer)
                                pointer_chain_checked = True
                                labels = by_address.get(target, [])
                    api = [n for n in labels if n.startswith(('_IOConnect', '_IOServiceOpen', '_IOServiceClose'))]
                    if api:
                        calls.append({'function': entry['name'], 'function_address': address, 'call_address': insn.address,
                                      'call_offset': insn.address - address, 'api': api,
                                      'api_address': target, 'indirect_chain_membership_checked': pointer_chain_checked,
                                      'function_sha256': hashlib.sha256(raw).hexdigest(),
                                      'undecoded_trailing_zero_padding_bytes': len(padding),
                                      'local_literal_registers': literal_arguments(instructions, index),
                                      'complete_arguments_qualified': False})
        fmt.require(input_hashes == {n: fmt.sha256(folder / n) for n in names}, 'INPUT_CHANGED_DURING_ANALYSIS')
        return {'schema': 1, 'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'capstone': __version__,
                'input_files': {n: {'bytes': (folder / n).stat().st_size, 'sha256': input_hashes[n]} for n in names},
                'frameworks': images, 'ioaccel_exported_functions_decoded': function_count,
                'io_calls_found': len(calls), 'calls': calls, 'native_execution_tested': False,
                'metal_provider_qualified': False, 'apple_instructions_redistributed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dyld-directory', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    folder = args.dyld_directory.resolve(strict=True)
    fmt.require(not args.report.resolve().is_relative_to(folder), 'REPORT_MUST_NOT_OVERWRITE_INPUT')
    report = analyze(folder)
    report['source_files_sha256'] = {p.name: fmt.sha256(p) for p in (Path(__file__), Path(fmt.__file__))}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({k: report[k] for k in ('ioaccel_exported_functions_decoded', 'io_calls_found', 'native_execution_tested')}))


if __name__ == '__main__':
    try:
        main()
    except (fmt.FormatError, OSError, ImportError, StopIteration) as error:
        print(json.dumps({'verified': False, 'error': type(error).__name__, 'code': str(error)}))
        raise SystemExit(2)
