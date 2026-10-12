# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Build real x86_64 Mach-O images in memory, so recovery can be tested honestly.

No Apple binary is needed to prove a recovery tool works: a planted table in a
genuine Mach-O layout is a stronger test than a vendor file, because the
expected answer is known exactly. This builds kext images and kernel-collection
filesets out of sections, symbols and whatever bytes a test wants to plant.
"""
from __future__ import annotations

import struct

MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_X86_64 = 0x01000007
MH_KEXT_BUNDLE = 0xB
MH_FILESET = 0xC
LC_SYMTAB = 0x2
LC_SEGMENT_64 = 0x19
LC_UUID = 0x1B
LC_FILESET_ENTRY = 0x80000035
S_REGULAR = 0x0
S_CSTRING_LITERALS = 0x2
S_ATTR_PURE_INSTRUCTIONS = 0x80000000
S_ATTR_SOME_INSTRUCTIONS = 0x00000400
N_SECT = 0x0E
N_EXT = 0x01
TEXT_FLAGS = S_ATTR_PURE_INSTRUCTIONS | S_ATTR_SOME_INSTRUCTIONS


def pad16(name: str) -> bytes:
    return name.encode('ascii')[:16].ljust(16, b'\0')


class ImageBuilder:
    """One Mach-O image: segments with sections, plus a symbol table."""

    def __init__(self, base_address: int = 0xFFFFFF8000200000,
                 filetype: int = MH_KEXT_BUNDLE, uuid: bytes | None = None):
        self.base = base_address
        self.filetype = filetype
        self.uuid = uuid
        self.segments: list[dict] = []
        self.symbols: list[tuple[str, int]] = []

    def segment(self, name: str) -> dict:
        for entry in self.segments:
            if entry['name'] == name:
                return entry
        entry = {'name': name, 'sections': []}
        self.segments.append(entry)
        return entry

    def section(self, segment: str, name: str, data: bytes, flags: int = S_REGULAR) -> None:
        self.segment(segment)['sections'].append({'name': name, 'data': data, 'flags': flags})

    def symbol(self, name: str, address: int) -> None:
        self.symbols.append((name, address))

    def build(self) -> tuple[bytes, dict]:
        """The image bytes and a map of section name to its virtual address."""
        command_count = len(self.segments) + 1 + (1 if self.uuid else 0)
        commands_size = sum(72 + 80 * len(entry['sections']) for entry in self.segments) \
            + 24 + (24 if self.uuid else 0)
        header_size = 32 + commands_size
        # Lay out section content after the load commands, page aligned.
        file_cursor = (header_size + 0xFFF) & ~0xFFF
        address_cursor = self.base + file_cursor
        addresses: dict[str, int] = {}
        content: list[tuple[int, bytes]] = []
        for segment in self.segments:
            segment['vmaddr'] = address_cursor
            segment['fileoff'] = file_cursor
            start_file = file_cursor
            for section in segment['sections']:
                section['address'] = address_cursor
                section['offset'] = file_cursor
                addresses[segment['name'] + ',' + section['name']] = address_cursor
                content.append((file_cursor, section['data']))
                size = (len(section['data']) + 7) & ~7
                file_cursor += size
                address_cursor += size
            segment['filesize'] = file_cursor - start_file
            segment['vmsize'] = segment['filesize']

        strings = b'\0'
        table = b''
        for name, address in self.symbols:
            index = len(strings)
            strings += name.encode('ascii') + b'\0'
            section_index = 1
            for position, (segment, section) in enumerate(
                    ((segment, section) for segment in self.segments
                     for section in segment['sections']), start=1):
                if section['address'] <= address < section['address'] + max(
                        len(section['data']), 1):
                    section_index = position
                    break
            table += struct.pack('<IBBHQ', index, N_SECT | N_EXT, section_index, 0, address)
        symbol_offset = file_cursor
        strings_offset = symbol_offset + len(table)
        total = strings_offset + len(strings)

        image = bytearray(total)
        struct.pack_into('<8I', image, 0, MH_MAGIC_64, CPU_TYPE_X86_64, 3, self.filetype,
                         command_count, commands_size, 0, 0)
        position = 32
        for segment in self.segments:
            size = 72 + 80 * len(segment['sections'])
            struct.pack_into('<II16s4Q4I', image, position, LC_SEGMENT_64, size,
                             pad16(segment['name']), segment['vmaddr'], segment['vmsize'],
                             segment['fileoff'], segment['filesize'], 7, 5,
                             len(segment['sections']), 0)
            inner = position + 72
            for section in segment['sections']:
                struct.pack_into('<16s16s2Q6I', image, inner, pad16(section['name']),
                                 pad16(segment['name']), section['address'],
                                 len(section['data']), section['offset'], 3, 0, 0,
                                 section['flags'], 0)
                struct.pack_into('<I', image, inner + 76, 0)
                inner += 80
            position += size
        if self.uuid:
            struct.pack_into('<II16s', image, position, LC_UUID, 24, self.uuid)
            position += 24
        struct.pack_into('<6I', image, position, LC_SYMTAB, 24, symbol_offset,
                         len(self.symbols), strings_offset, len(strings))
        position += 24
        assert position == 32 + commands_size, (position, 32 + commands_size)
        for offset, blob in content:
            image[offset:offset + len(blob)] = blob
        image[symbol_offset:symbol_offset + len(table)] = table
        image[strings_offset:strings_offset + len(strings)] = strings
        return bytes(image), addresses


def fileset(entries: list[tuple[str, bytes]], base_address: int = 0xFFFFFF8000000000) -> bytes:
    """A kernel-collection-shaped MH_FILESET carrying the given images."""
    command_size = 32
    commands_size = sum(((command_size + len(identifier) + 1 + 7) & ~7)
                        for identifier, _ in entries)
    header_size = 32 + commands_size
    cursor = (header_size + 0xFFF) & ~0xFFF
    placed = []
    for identifier, blob in entries:
        placed.append({'identifier': identifier, 'blob': blob, 'fileoff': cursor,
                       'vmaddr': base_address + cursor})
        cursor += (len(blob) + 0xFFF) & ~0xFFF
    image = bytearray(cursor)
    struct.pack_into('<8I', image, 0, MH_MAGIC_64, CPU_TYPE_X86_64, 3, MH_FILESET,
                     len(placed), commands_size, 0, 0)
    position = 32
    for entry in placed:
        name = entry['identifier'].encode('ascii') + b'\0'
        size = (command_size + len(name) + 7) & ~7
        struct.pack_into('<IIQQII', image, position, LC_FILESET_ENTRY, size,
                         entry['vmaddr'], entry['fileoff'], command_size, 0)
        image[position + command_size:position + command_size + len(name)] = name
        position += size
    for entry in placed:
        image[entry['fileoff']:entry['fileoff'] + len(entry['blob'])] = entry['blob']
    return bytes(image)


def dispatch_entry_2022(handler: int, scalar_in: int, struct_in: int, scalar_out: int,
                        struct_out: int, allow_async: bool = False,
                        entitlement: int = 0) -> bytes:
    """One IOExternalMethodDispatch2022 as a compiler would emit it."""
    return (struct.pack('<Q', handler)
            + struct.pack('<4I', scalar_in, struct_in, scalar_out, struct_out)
            + struct.pack('<B', 1 if allow_async else 0) + b'\0' * 7
            + struct.pack('<Q', entitlement))


def dispatch_entry_legacy(handler: int, scalar_in: int, struct_in: int, scalar_out: int,
                          struct_out: int) -> bytes:
    """One IOExternalMethodDispatch as a compiler would emit it."""
    return struct.pack('<Q', handler) + struct.pack('<4I', scalar_in, struct_in,
                                                    scalar_out, struct_out)
