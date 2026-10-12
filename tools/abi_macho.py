# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""A Mach-O reader for interface recovery: segments, symbols, filesets, addresses.

tools/macho.py validates our own build output and deliberately refuses anything
unusual. Recovery needs the opposite disposition: read a foreign image as it is,
including the two shapes that matter on a current macOS install.

    MH_KEXT_BUNDLE   a kext binary on disk, as our own builds also produce
    MH_FILESET       a kernel collection, which carries many kexts in one file
                     and needs no extraction: each LC_FILESET_ENTRY names a
                     bundle identifier and where its image starts

What this gives the scanners above it: sections by segment and name, the symbol
table, a virtual-address to file-offset map, bounded reads by address, the
executable address range, and C strings by address. It reads; it never writes,
and it hands out facts and small reads, never the image.
"""
from __future__ import annotations

import struct

MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_X86_64 = 0x01000007
CPU_TYPE_ARM64 = 0x0100000C
MH_OBJECT, MH_FILESET, MH_KEXT_BUNDLE = 0x1, 0xC, 0xB
LC_SYMTAB = 0x2
LC_SEGMENT_64 = 0x19
LC_UUID = 0x1B
LC_DYLD_CHAINED_FIXUPS = 0x80000034
LC_FILESET_ENTRY = 0x80000035
S_ATTR_PURE_INSTRUCTIONS = 0x80000000
S_ATTR_SOME_INSTRUCTIONS = 0x00000400
S_CSTRING_LITERALS = 0x2
SECTION_TYPE = 0xFF
N_TYPE, N_SECT, N_UNDF, N_ABS, N_STAB = 0x0E, 0x0E, 0x0, 0x2, 0xE0
MAX_SYMBOLS = 4_000_000


class MachOError(Exception):
    """The image is not shaped the way this reader requires."""


class MachOImage:
    """One Mach-O image inside a buffer, addressed by its own virtual addresses."""

    def __init__(self, data: bytes, offset: int = 0, label: str = ''):
        self.data = data
        self.offset = offset
        self.label = label
        if len(data) < offset + 32:
            raise MachOError('image header outside the buffer')
        magic, cpu, _subtype, filetype, count, size, flags, _reserved = struct.unpack_from(
            '<8I', data, offset)
        if magic != MH_MAGIC_64:
            raise MachOError('not a 64-bit little-endian Mach-O')
        self.cpu_type = cpu
        self.filetype = filetype
        self.flags = flags
        self.architecture = {CPU_TYPE_X86_64: 'x86_64', CPU_TYPE_ARM64: 'arm64'}.get(cpu, hex(cpu))
        self.segments: list[dict] = []
        self.sections: list[dict] = []
        self.fileset_entries: list[dict] = []
        self.uuid: str | None = None
        self.has_chained_fixups = False
        self.commands: list[int] = []
        self._symbols: dict | None = None
        self._symbol_list: list | None = None
        self._symtab: tuple | None = None
        self.offset_base = 0
        self.offset_interpretation = 'absolute'
        position = offset + 32
        for _ in range(count):
            if position + 8 > len(data):
                raise MachOError('load command outside the buffer')
            command, length = struct.unpack_from('<2I', data, position)
            if length < 8 or length % 4 or position + length > offset + 32 + size:
                raise MachOError('invalid load command size')
            self.commands.append(command)
            self._read_command(command, position, length)
            position += length
        if offset:
            self._resolve_offset_base()

    def _read_command(self, command: int, position: int, length: int) -> None:
        data = self.data
        if command == LC_SEGMENT_64:
            name = data[position + 8:position + 24].split(b'\0', 1)[0].decode('ascii', 'replace')
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<4Q', data, position + 24)
            nsects = struct.unpack_from('<I', data, position + 64)[0]
            self.segments.append({'name': name, 'vmaddr': vmaddr, 'vmsize': vmsize,
                                  'fileoff': fileoff, 'filesize': filesize,
                                  'section_count': nsects})
            for index in range(nsects):
                base = position + 72 + index * 80
                if base + 80 > position + length:
                    raise MachOError('section header outside its segment command')
                section = data[base:base + 80]
                self.sections.append({
                    'name': section[:16].split(b'\0', 1)[0].decode('ascii', 'replace'),
                    'segment': section[16:32].split(b'\0', 1)[0].decode('ascii', 'replace'),
                    'address': struct.unpack_from('<Q', section, 32)[0],
                    'bytes': struct.unpack_from('<Q', section, 40)[0],
                    'offset': struct.unpack_from('<I', section, 48)[0],
                    'alignment': struct.unpack_from('<I', section, 52)[0],
                    'relocation_offset': struct.unpack_from('<I', section, 56)[0],
                    'relocation_count': struct.unpack_from('<I', section, 60)[0],
                    'flags': struct.unpack_from('<I', section, 64)[0],
                })
        elif command == LC_SYMTAB:
            self._symtab = struct.unpack_from('<4I', data, position + 8)
        elif command == LC_UUID:
            self.uuid = data[position + 8:position + 24].hex()
        elif command == LC_FILESET_ENTRY:
            vmaddr, fileoff, name_offset = struct.unpack_from('<QQI', data, position + 8)
            start = position + name_offset
            end = data.find(b'\0', start)
            if not 0 <= name_offset < length or end < 0:
                raise MachOError('fileset entry identifier outside its command')
            self.fileset_entries.append({
                'identifier': data[start:end].decode('ascii', 'replace'),
                'vmaddr': vmaddr, 'fileoff': fileoff})
        elif command == LC_DYLD_CHAINED_FIXUPS:
            self.has_chained_fixups = True

    def _offsets_valid(self, base: int) -> bool:
        """Do this image's recorded file offsets land inside the buffer at `base`?"""
        for entry in self.sections:
            if not entry['bytes'] or entry['flags'] & SECTION_TYPE == 0x1:
                continue
            start = base + entry['offset']
            if start < 0 or start + entry['bytes'] > len(self.data):
                return False
        if self._symtab:
            table_offset, count, strings_offset, strings_size = self._symtab
            if base + table_offset + count * 16 > len(self.data):
                return False
            if base + strings_offset + strings_size > len(self.data):
                return False
            # A Mach-O string table always begins with a NUL byte, which tells
            # the two interpretations apart when both are in range.
            if strings_size and self.data[base + strings_offset] != 0:
                return False
        return True

    def _resolve_offset_base(self) -> None:
        """An image inside a fileset records offsets absolutely or relative to itself.

        Apple's collections use absolute offsets; an image embedded verbatim
        keeps its own. Rather than assume one, both are checked against the
        buffer and the interpretation that holds is recorded.
        """
        for base, name in ((0, 'absolute'), (self.offset, 'image_relative')):
            if self._offsets_valid(base):
                self.offset_base, self.offset_interpretation = base, name
                return
        raise MachOError('file offsets fit neither interpretation inside this fileset')

    @property
    def symbols(self) -> dict:
        """Name to {value, section, type}, read once and cached."""
        if self._symbols is None:
            self._read_symbols()
        return self._symbols

    @property
    def symbol_list(self) -> list:
        """Symbols in symbol-table order, which is what relocations index."""
        if self._symbol_list is None:
            self._read_symbols()
        return self._symbol_list

    def _read_symbols(self) -> None:
        self._symbols = {}
        self._symbol_list = []
        if not self._symtab:
            return
        table_offset, count, strings_offset, strings_size = self._symtab
        table_offset += self.offset_base
        strings_offset += self.offset_base
        if count > MAX_SYMBOLS:
            raise MachOError('implausible symbol count: ' + str(count))
        end = table_offset + count * 16
        if end > len(self.data) or strings_offset + strings_size > len(self.data):
            raise MachOError('symbol table outside the buffer')
        strings = self.data[strings_offset:strings_offset + strings_size]
        for index in range(count):
            string_index, symbol_type, section, _description, value = struct.unpack_from(
                '<IBBHQ', self.data, table_offset + index * 16)
            name = ''
            if not symbol_type & N_STAB and string_index < len(strings):
                stop = strings.find(b'\0', string_index)
                if stop >= 0:
                    name = strings[string_index:stop].decode('utf-8', 'replace')
            entry = {'name': name, 'value': value, 'section': section,
                     'type': symbol_type & N_TYPE, 'stab': bool(symbol_type & N_STAB)}
            # The list keeps every slot, including unnamed ones, because a
            # relocation addresses symbols by their index in this table.
            self._symbol_list.append(entry)
            if name and not entry['stab']:
                self._symbols[name] = {'value': value, 'section': section,
                                       'type': symbol_type & N_TYPE}

    def relocations(self, section: dict) -> list[dict]:
        """A section's relocation entries, resolved to symbol names where external.

        In a relocatable object a pointer field holds zero and a relocation
        names the symbol the linker will put there. Reading those is the only
        way to recover a table from an object file rather than a linked image.
        """
        count = section.get('relocation_count') or 0
        start = self.offset_base + (section.get('relocation_offset') or 0)
        if not count or start + count * 8 > len(self.data):
            return []
        symbols = self.symbol_list
        out = []
        for index in range(count):
            address, info = struct.unpack_from('<iI', self.data, start + index * 8)
            if address < 0:  # a scattered relocation, which these tables do not use
                continue
            symbol_number = info & 0xFFFFFF
            entry = {
                'offset': address,
                'pc_relative': bool((info >> 24) & 1),
                'length': 1 << ((info >> 25) & 3),
                'external': bool((info >> 27) & 1),
                'type': (info >> 28) & 0xF,
                'symbol': None,
            }
            if entry['external'] and symbol_number < len(symbols):
                entry['symbol'] = symbols[symbol_number]['name'] or None
            elif not entry['external']:
                entry['section_number'] = symbol_number
            out.append(entry)
        return out

    def section(self, segment: str, name: str) -> dict | None:
        for entry in self.sections:
            if entry['segment'] == segment and entry['name'] == name:
                return entry
        return None

    def section_of_address(self, address: int) -> dict | None:
        for entry in self.sections:
            if entry['bytes'] and entry['address'] <= address < entry['address'] + entry['bytes']:
                return entry
        return None

    def executable_ranges(self) -> list[tuple[int, int]]:
        ranges = []
        for entry in self.sections:
            if entry['flags'] & (S_ATTR_PURE_INSTRUCTIONS | S_ATTR_SOME_INSTRUCTIONS):
                ranges.append((entry['address'], entry['address'] + entry['bytes']))
        return ranges

    def is_executable_address(self, address: int) -> bool:
        return any(start <= address < stop for start, stop in self.executable_ranges())

    def is_cstring_address(self, address: int) -> bool:
        entry = self.section_of_address(address)
        return bool(entry and entry['flags'] & SECTION_TYPE == S_CSTRING_LITERALS)

    def read_section(self, entry: dict) -> bytes:
        start, size = self.offset_base + entry['offset'], entry['bytes']
        if entry['flags'] & SECTION_TYPE == 0x1:  # S_ZEROFILL has no file content
            return b''
        if start + size > len(self.data):
            raise MachOError('section content outside the buffer')
        return self.data[start:start + size]

    def read_address(self, address: int, size: int) -> bytes | None:
        entry = self.section_of_address(address)
        if not entry:
            return None
        displacement = address - entry['address']
        if displacement + size > entry['bytes']:
            return None
        start = self.offset_base + entry['offset'] + displacement
        if start + size > len(self.data):
            return None
        return self.data[start:start + size]

    def cstring_at(self, address: int, limit: int = 256) -> str | None:
        """A short C string by address — used for entitlement names only."""
        entry = self.section_of_address(address)
        if not entry:
            return None
        displacement = address - entry['address']
        start = self.offset_base + entry['offset'] + displacement
        stop = self.data.find(b'\0', start, min(start + limit, len(self.data)))
        if stop < 0:
            return None
        text = self.data[start:stop]
        if not text or any(byte < 0x20 or byte > 0x7E for byte in text):
            return None
        return text.decode('ascii')

    def symbol_at(self, address: int) -> str | None:
        for name, symbol in self.symbols.items():
            if symbol['value'] == address and symbol['type'] in (N_SECT, N_ABS):
                return name
        return None

    def address_index(self) -> dict:
        """Address to symbol name, built once for repeated lookups."""
        index: dict = {}
        for name, symbol in self.symbols.items():
            if symbol['type'] in (N_SECT, N_ABS):
                index.setdefault(symbol['value'], name)
        return index

    def describe(self) -> dict:
        return {
            'label': self.label,
            'architecture': self.architecture,
            'filetype': self.filetype,
            'filetype_name': {MH_OBJECT: 'MH_OBJECT', MH_FILESET: 'MH_FILESET',
                              MH_KEXT_BUNDLE: 'MH_KEXT_BUNDLE'}.get(self.filetype,
                                                                    hex(self.filetype)),
            'uuid': self.uuid,
            'has_chained_fixups': self.has_chained_fixups,
            'segments': [{key: entry[key] for key in ('name', 'vmaddr', 'vmsize')}
                         for entry in self.segments],
            'sections': [entry['segment'] + ',' + entry['name'] for entry in self.sections],
            'symbol_count': len(self.symbols),
            'offset_interpretation': self.offset_interpretation,
            'fileset_entry_count': len(self.fileset_entries),
        }


def open_image(data: bytes, label: str = '') -> MachOImage:
    return MachOImage(data, 0, label)


def fileset_images(image: MachOImage) -> dict:
    """Every kext inside a kernel collection, by bundle identifier.

    A fileset needs no extraction: each entry's image starts at its recorded
    file offset inside the same buffer, so it is opened in place.
    """
    if image.filetype != MH_FILESET:
        raise MachOError('not a fileset: ' + image.describe()['filetype_name'])
    images = {}
    for entry in image.fileset_entries:
        try:
            images[entry['identifier']] = MachOImage(image.data, entry['fileoff'],
                                                     entry['identifier'])
        except MachOError as error:
            images[entry['identifier']] = {'error': str(error)}
    return images


def load_image(path, bundle_identifier: str | None = None) -> MachOImage:
    """Open a binary, descending into a collection when one is given."""
    from pathlib import Path

    data = Path(path).read_bytes()
    image = open_image(data, str(path))
    if image.filetype != MH_FILESET:
        if bundle_identifier:
            raise MachOError('a bundle identifier was given but this is not a collection')
        return image
    if not bundle_identifier:
        return image
    images = fileset_images(image)
    found = images.get(bundle_identifier)
    if found is None:
        raise MachOError('no such bundle in this collection: ' + bundle_identifier)
    if isinstance(found, dict):
        raise MachOError('unreadable image for ' + bundle_identifier + ': ' + found['error'])
    return found
