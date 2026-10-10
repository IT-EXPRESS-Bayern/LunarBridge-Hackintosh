# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Structural validation of our x86_64 Mach-O objects and kernel bundle."""
import struct

def inspect_macho(data, require_kext=False):
    def region(offset, size):
        if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
            raise ValueError('Mach-O range outside file')
        return data[offset:offset + size]
    if len(data) < 32: raise ValueError('Mach-O header missing')
    magic, cpu, subtype, kind, count, size, flags, reserved = struct.unpack('<8I', data[:32])
    if magic != 0xfeedfacf or cpu != 0x01000007: raise ValueError('Expected x86_64 Mach-O')
    if kind not in (1, 11) or (require_kext and kind != 11):
        raise ValueError('Expected a real MH_KEXT_BUNDLE, not a renamed object file')
    if count > 4096 or reserved: raise ValueError('Invalid Mach-O header')
    region(32, size)
    sections, symbols, commands = [], {}, []
    position = 32
    symtab = None
    for _ in range(count):
        command, length = struct.unpack('<2I', region(position, 8))
        if length < 8 or length % 8 or position + length > 32 + size:
            raise ValueError('Invalid Mach-O load command')
        block = region(position, length)
        commands.append(command)
        if command == 0x19:
            if length < 72: raise ValueError('Segment header missing')
            nsects = struct.unpack_from('<I', block, 64)[0]
            if length != 72 + 80 * nsects: raise ValueError('Invalid segment section count')
            for index in range(nsects):
                item = block[72 + index * 80:152 + index * 80]
                name = item[:16].split(b'\0', 1)[0].decode('ascii')
                segment = item[16:32].split(b'\0', 1)[0].decode('ascii')
                address, length_bytes, offset = struct.unpack_from('<QQI', item, 32)
                section_flags = struct.unpack_from('<I', item, 64)[0]
                if section_flags & 0xff not in (1, 12): region(offset, length_bytes)
                sections.append({'name': name, 'segment': segment, 'address': address,
                                 'bytes': length_bytes, 'offset': offset, 'flags': section_flags})
        elif command == 2:
            if length != 24: raise ValueError('Invalid symbol table command')
            symtab = struct.unpack_from('<4I', block, 8)
        position += length
    if position != 32 + size: raise ValueError('Load command size mismatch')
    if symtab:
        offset, number, strings_offset, strings_bytes = symtab
        if number > 100000: raise ValueError('Symbol table too large')
        table, strings = region(offset, number * 16), region(strings_offset, strings_bytes)
        for index in range(number):
            string_index, symbol_type, section, description, value = struct.unpack_from('<IBBHQ', table, index * 16)
            if string_index >= len(strings): raise ValueError('Symbol string outside table')
            end = strings.find(b'\0', string_index)
            if end < 0: raise ValueError('Unterminated symbol name')
            name = strings[string_index:end].decode('ascii')
            if symbol_type & 0xe0: continue
            symbols[name] = {'type': symbol_type & 0x0e, 'section': section, 'value': value}
    result = {'filetype': kind, 'architecture': 'x86_64', 'section_names': [s['name'] for s in sections],
              'defined_symbols': sorted(n for n, s in symbols.items() if s['type'] in (2, 14)),
              'undefined_symbols': sorted(n for n, s in symbols.items() if s['type'] == 0),
              'has_code_signature': 0x1d in commands, 'native_load_verified': False}
    if require_kext:
        if any(c in commands for c in (0xc, 0x18, 0x80000018, 0x8000001f, 0x80000034)):
            raise ValueError('Userspace dylib dependency or chained fixup in kernel bundle')
        if not any(s['name'] == '__text' and s['bytes'] for s in sections) or \
           not any(s['name'] == '__mod_init_func' and s['bytes'] for s in sections):
            raise ValueError('Kernel code or C++ initializer missing')
        for name in ('_kmod_info', '__start', '__stop'):
            if name not in symbols or symbols[name]['type'] != 14:
                raise ValueError('Kernel bundle symbol missing: ' + name)
        symbol = symbols['_kmod_info']
        if not 1 <= symbol['section'] <= len(sections): raise ValueError('Invalid kmod section')
        section = sections[symbol['section'] - 1]
        displacement = symbol['value'] - section['address']
        if displacement < 0 or displacement + 144 > section['bytes']:
            raise ValueError('Kernel module metadata outside section')
        info = region(section['offset'] + displacement, 144)
        result['identifier'] = info[16:80].split(b'\0', 1)[0].decode('ascii')
        result['version'] = info[80:144].split(b'\0', 1)[0].decode('ascii')
    return result
