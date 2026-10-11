# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Read-only x86_64 recovery graphics metadata, dyld cache and fileset analysis.

Inputs are locally extracted files from the user's own recovery image. This
tool never mounts an image, executes an Apple binary or installs a driver.
Binary format definitions: apple-oss-distributions/{dyld,xnu} on GitHub.
Symbol/selector observations are candidates, not a qualified driver ABI.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import mmap
from pathlib import Path
import re
import struct
import plistlib

MAX_READ = 16 * 1024 * 1024
INTEREST = re.compile(r'IOAccel|IOSurface|Framebuffer|MetalPlugin|MTLIOAccel|IOConnect|IOService')
TARGET_FRAMEWORKS = ('Metal.framework', 'IOSurface.framework', 'IOAccelerator.framework',
                     'IOSurfaceAccelerator.framework', 'MetalTools.framework', 'CoreDisplay.framework')
TARGET_BUNDLES = re.compile(r'AppleIntel.*(?:Graphics|Framebuffer)|IOAccelerator|IOSurface|IOGraphicsFamily|AppleVesa')


class FormatError(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise FormatError(code)


def region(data, offset, size, limit=MAX_READ):
    require(type(offset) is int and type(size) is int and offset >= 0 and 0 <= size <= limit and
            offset <= len(data) and size <= len(data) - offset, 'BINARY_RANGE_INVALID')
    return data[offset:offset + size]


def cstring(data, offset=0, end=None, limit=4096):
    end = len(data) if end is None else end
    require(0 <= offset < end <= len(data), 'STRING_RANGE_INVALID')
    stop = data.find(b'\0', offset, min(end, offset + limit + 1))
    require(stop >= 0, 'STRING_NOT_TERMINATED')
    try:
        return data[offset:stop].decode('utf-8')
    except UnicodeError:
        raise FormatError('STRING_ENCODING_INVALID') from None


def fixed_name(data):
    require(len(data) == 16, 'FIXED_NAME_SIZE_INVALID')
    try:
        return data.split(b'\0', 1)[0].decode('ascii')
    except UnicodeError:
        raise FormatError('FIXED_NAME_ENCODING_INVALID') from None


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


@contextlib.contextmanager
def mapped(path):
    require(path.is_file() and not path.is_symlink(), 'INPUT_NOT_REGULAR_FILE')
    with path.open('rb') as stream:
        require(0 < path.stat().st_size <= 4 * 1024 * 1024 * 1024, 'INPUT_SIZE_INVALID')
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            yield data


def macho(read, offset=0):
    header = read(offset, 32)
    magic, cpu, subtype, kind, count, size, flags, reserved = struct.unpack('<8I', header)
    require(magic == 0xFEEDFACF and cpu == 0x01000007, 'X86_64_MACHO_REQUIRED')
    require(0 < count <= 4096 and 8 * count <= size <= 2 * 1024 * 1024 and reserved == 0,
            'MACHO_COMMAND_LIMIT')
    commands = read(offset + 32, size)
    result = {'architecture': 'x86_64', 'filetype': kind, 'cpu_subtype': subtype,
              'segments': [], 'sections': [], 'dependencies': [], 'fileset_entries': [],
              'symbol_table': None, 'exports_trie': None, 'code_signature_present': False}
    cursor = 0
    for _ in range(count):
        command, length = struct.unpack('<2I', region(commands, cursor, 8))
        require(length >= 8 and length % 8 == 0, 'MACHO_COMMAND_SIZE_INVALID')
        item = region(commands, cursor, length)
        if command == 0x19:
            require(length >= 72, 'MACHO_SEGMENT_TRUNCATED')
            name = fixed_name(item[8:24])
            address, virtual_size, file_offset, file_size = struct.unpack_from('<4Q', item, 24)
            sections = struct.unpack_from('<I', item, 64)[0]
            require(sections <= 4096 and length == 72 + 80 * sections, 'MACHO_SECTION_COUNT_INVALID')
            result['segments'].append({'name': name, 'address': address, 'virtual_bytes': virtual_size,
                                       'file_offset': file_offset, 'file_bytes': file_size})
            for index in range(sections):
                record = item[72 + index * 80:152 + index * 80]
                section_address, section_size, section_offset = struct.unpack_from('<QQI', record, 32)
                section_flags = struct.unpack_from('<I', record, 64)[0]
                result['sections'].append({'name': fixed_name(record[:16]),
                    'segment': fixed_name(record[16:32]), 'address': section_address,
                    'bytes': section_size, 'file_offset': section_offset, 'flags': section_flags})
        elif command in (0xC, 0x80000018, 0x8000001F, 0x80000023, 0xD):
            require(length >= 24, 'MACHO_DYLIB_COMMAND_INVALID')
            name_offset = struct.unpack_from('<I', item, 8)[0]
            require(24 <= name_offset < length, 'MACHO_DYLIB_NAME_INVALID')
            path = cstring(item, name_offset)
            if command == 0xD:
                result['install_name'] = path
            else:
                result['dependencies'].append({'name': path, 'load_command': command})
        elif command == 0x80000035:
            require(length >= 40, 'FILESET_COMMAND_INVALID')
            address, file_offset, name_offset, padding = struct.unpack_from('<QQII', item, 8)
            require(32 <= name_offset < length and padding == 0, 'FILESET_IDENTIFIER_INVALID')
            result['fileset_entries'].append({'identifier': cstring(item, name_offset),
                                              'address': address, 'file_offset': file_offset})
        elif command == 2:
            require(length == 24 and result['symbol_table'] is None, 'SYMTAB_COMMAND_INVALID')
            result['symbol_table'] = list(struct.unpack_from('<4I', item, 8))
        elif command == 0x80000033:
            require(length == 16 and result['exports_trie'] is None, 'EXPORTS_COMMAND_INVALID')
            result['exports_trie'] = list(struct.unpack_from('<2I', item, 8))
        elif command in (0x22, 0x80000022):
            require(length == 48, 'DYLD_INFO_COMMAND_INVALID')
            offset_value, size_value = struct.unpack_from('<2I', item, 40)
            if size_value:
                require(result['exports_trie'] is None, 'EXPORTS_COMMAND_DUPLICATE')
                result['exports_trie'] = [offset_value, size_value]
        elif command == 0x1D:
            result['code_signature_present'] = True
        cursor += length
    require(cursor == size, 'MACHO_COMMAND_TOTAL_INVALID')
    identifiers = [x['identifier'] for x in result['fileset_entries']]
    require(len(identifiers) == len(set(identifiers)), 'FILESET_IDENTIFIER_DUPLICATE')
    return result


def uleb(data, cursor):
    value = 0
    for index in range(10):
        require(cursor < len(data), 'ULEB_TRUNCATED')
        byte = data[cursor]
        cursor += 1
        require(index != 9 or byte <= 1, 'ULEB_OVERFLOW')
        value |= (byte & 127) << (7 * index)
        if byte < 128:
            return value, cursor
    raise FormatError('ULEB_OVERFLOW')


def exported_symbols(data):
    require(0 < len(data) <= 4 * 1024 * 1024, 'EXPORT_TRIE_SIZE_INVALID')
    pending = [(0, '', ())]
    symbols, steps = [], 0
    while pending:
        start, name, ancestry = pending.pop()
        require(start not in ancestry and len(ancestry) <= 512, 'EXPORT_TRIE_CYCLE_OR_DEPTH')
        require(len(name) <= 4096 and steps < 65536, 'EXPORT_TRIE_LIMIT')
        steps += 1
        terminal, cursor = uleb(data, start)
        require(terminal <= len(data) - cursor, 'EXPORT_TERMINAL_OUTSIDE_TRIE')
        end = cursor + terminal
        if terminal:
            flags, entry = uleb(data, cursor)
            require(entry <= end, 'EXPORT_TERMINAL_TRUNCATED')
            value, entry = uleb(data, entry)
            require(entry <= end, 'EXPORT_TERMINAL_TRUNCATED')
            if flags & 8:
                imported = cstring(data, entry, end)
                record = {'name': name, 'flags': flags, 'reexport': True,
                          'library_ordinal': value, 'import_name': imported}
            else:
                record = {'name': name, 'flags': flags, 'reexport': False, 'address_offset': value}
            symbols.append(record)
            require(len(symbols) <= 32768, 'EXPORT_SYMBOL_LIMIT')
        require(end < len(data), 'EXPORT_CHILD_COUNT_MISSING')
        count = data[end]
        cursor = end + 1
        for _ in range(count):
            edge = cstring(data, cursor)
            require(bool(edge), 'EXPORT_EMPTY_EDGE')
            cursor += len(edge.encode('utf-8')) + 1
            child, cursor = uleb(data, cursor)
            require(child < len(data), 'EXPORT_CHILD_OUTSIDE_TRIE')
            pending.append((child, name + edge, ancestry + (start,)))
    return sorted(symbols, key=lambda x: x['name'])


def symbol_table(read, description):
    table = description['symbol_table']
    if table is None:
        return {'available': False, 'selected': []}
    offset, count, strings_offset, strings_size = table
    require(count <= 262144 and 0 < strings_size <= MAX_READ, 'SYMBOL_TABLE_LIMIT')
    raw = read(offset, count * 16)
    strings = read(strings_offset, strings_size)
    names = []
    for index in range(count):
        name_index, kind, section, flags, value = struct.unpack_from('<IBBHQ', raw, index * 16)
        require(name_index < len(strings), 'SYMBOL_NAME_OUTSIDE_TABLE')
        if not kind & 0xE0:
            name = cstring(strings, name_index)
            if INTEREST.search(name):
                names.append({'name': name, 'defined': bool(kind & 0xE), 'value': value})
    require(len(names) <= 8192, 'SELECTED_SYMBOL_LIMIT')
    return {'available': True, 'total_symbols': count, 'selected': names,
            'observation_scope': 'Referenced nlist table; symbol ownership and calling ABI not qualified.',
            'ownership_qualified': False}


class DyldCache:
    def __init__(self, data_files):
        self.files = data_files
        self.mappings = []
        for name, data in data_files.items():
            require(region(data, 0, 16).startswith(b'dyld_v1  x86_64'), 'DYLD_CACHE_ARCH_UNSUPPORTED')
            offset, count = struct.unpack('<2I', region(data, 16, 8))
            require(456 <= offset <= 1024 * 1024 and 0 < count <= 64, 'DYLD_MAPPING_TABLE_INVALID')
            for i in range(count):
                address, size, file_offset, maximum, initial = struct.unpack('<3Q2I', region(data, offset + i * 32, 32))
                require(size > 0 and file_offset <= len(data) and size <= len(data) - file_offset and
                        address + size <= (1 << 64) and initial & ~maximum == 0, 'DYLD_MAPPING_INVALID')
                self.mappings.append({'file': name, 'address': address, 'bytes': size, 'offset': file_offset})
        self.mappings.sort(key=lambda x: x['address'])
        for left, right in zip(self.mappings, self.mappings[1:]):
            require(left['address'] + left['bytes'] <= right['address'], 'DYLD_MAPPING_OVERLAP')
        main = data_files['dyld_shared_cache_x86_64']
        sub_offset, sub_count = struct.unpack('<2I', region(main, 392, 8))
        require(sub_count <= 16 and sub_count + 1 == len(data_files), 'DYLD_SUBCACHE_COUNT_MISMATCH')
        suffixes = set()
        for index in range(sub_count):
            entry = region(main, sub_offset + index * 56, 56)
            suffix = cstring(entry, 24, 56, 31)
            require(re.fullmatch(r'\.[A-Za-z0-9.]{1,30}', suffix) is not None and suffix not in suffixes,
                    'DYLD_SUBCACHE_SUFFIX_INVALID')
            suffixes.add(suffix)
            sub_name = 'dyld_shared_cache_x86_64' + suffix
            require(sub_name in data_files and region(data_files[sub_name], 88, 16) == entry[:16],
                    'DYLD_SUBCACHE_UUID_MISMATCH')
        image_offset, count = struct.unpack('<2I', region(main, 448, 8))
        require(0 < count <= 65536, 'DYLD_IMAGE_COUNT_INVALID')
        table = region(main, image_offset, count * 32)
        self.images = []
        for index in range(count):
            address, modified, inode, path_offset, padding = struct.unpack_from('<3Q2I', table, index * 32)
            require(padding == 0, 'DYLD_IMAGE_PADDING_INVALID')
            path = cstring(main, path_offset)
            require(path.startswith('/') and '..' not in path.split('/'), 'DYLD_IMAGE_PATH_INVALID')
            self.images.append({'path': path, 'address': address})

    def read(self, address, size):
        require(0 <= size <= MAX_READ, 'DYLD_VM_READ_LIMIT')
        candidates = [m for m in self.mappings if m['address'] <= address and
                      address - m['address'] <= m['bytes'] and size <= m['bytes'] - (address - m['address'])]
        require(len(candidates) == 1, 'DYLD_VM_RANGE_UNMAPPED')
        mapping = candidates[0]
        return region(self.files[mapping['file']], mapping['offset'] + address - mapping['address'], size)

    def read_image_offset(self, description, offset, size):
        candidates = [s for s in description['segments'] if s['file_offset'] <= offset and
                      offset - s['file_offset'] <= s['file_bytes'] and
                      size <= s['file_bytes'] - (offset - s['file_offset'])]
        require(len(candidates) == 1, 'DYLD_IMAGE_FILE_RANGE_UNMAPPED')
        segment = candidates[0]
        return self.read(segment['address'] + offset - segment['file_offset'], size)


def framework_summary(cache, image):
    description = macho(cache.read, image['address'])
    exports = []
    if description['exports_trie'] is not None:
        offset, size = description['exports_trie']
        exports = exported_symbols(cache.read_image_offset(description, offset, size))
    strings = {}
    for section in description['sections']:
        if section['name'] in ('__objc_methname', '__objc_classname', '__cstring') and section['bytes']:
            raw = cache.read(section['address'], section['bytes'])
            candidates = []
            for item in raw.split(b'\0'):
                if item and len(item) <= 4096:
                    try:
                        text = item.decode('utf-8')
                    except UnicodeError:
                        continue
                    if INTEREST.search(text) or re.search(r'new(?:CommandQueue|Buffer|ComputePipeline|Heap)|supportsFamily|submitCommand', text):
                        candidates.append(text)
            require(len(candidates) <= 8192, 'FRAMEWORK_STRING_LIMIT')
            strings[section['name']] = sorted(set(candidates))
    ivars = []
    for symbol in exports:
        if (symbol['name'].startswith('_OBJC_IVAR_$_MTLIOAccel') and
                not symbol['reexport'] and symbol['flags'] & 3 == 0):
            address = image['address'] + symbol['address_offset']
            raw_offset = struct.unpack('<I', cache.read(address, 4))[0]
            require(raw_offset <= 16 * 1024 * 1024, 'OBJC_DECLARED_IVAR_OFFSET_INVALID')
            ivars.append({'name': symbol['name'], 'raw_declared_byte_offset': raw_offset,
                          'runtime_adjustment_qualified': False, 'field_type_qualified': False})
    return {'path': image['path'], 'macho_header_address': image['address'],
            'architecture': description['architecture'], 'dependencies': description['dependencies'],
            'export_count': len(exports), 'exports': exports, 'selected_section_strings': strings,
            'declared_ivar_offsets': ivars,
            'method_ownership_qualified': False, 'method_argument_types_qualified': False,
            'native_provider_abi_qualified': False}


def bundle_summary(value):
    keys = ('CFBundleIdentifier', 'CFBundleVersion', 'CFBundleExecutable', 'OSBundleCompatibleVersion',
            'OSBundleRequired', 'OSBundleLibraries', 'IOKitPersonalities', 'Personality',
            'CFPlugInFactories', 'CFPlugInTypes')
    return {key: value[key] for key in keys if key in value}


def kernel_summary(data):
    description = macho(lambda offset, size: region(data, offset, size))
    require(description['filetype'] == 12, 'KERNEL_FILESET_REQUIRED')
    selected, prelink = [], []
    for entry in description['fileset_entries']:
        require(entry['file_offset'] + 32 <= len(data), 'FILESET_IMAGE_OFFSET_INVALID')
        if TARGET_BUNDLES.search(entry['identifier']):
            embedded = macho(lambda offset, size: region(data, offset, size), entry['file_offset'])
            symbols = symbol_table(lambda offset, size: region(data, offset, size), embedded)
            selected.append(entry | {'filetype': embedded['filetype'], 'symbols': symbols})
    for section in description['sections']:
        if section['segment'] == '__PRELINK_INFO' and section['name'] == '__info' and section['bytes']:
            raw = region(data, section['file_offset'], section['bytes']).rstrip(b'\0')
            metadata = plistlib.loads(raw)
            values = metadata.get('_PrelinkInfoDictionary', [])
            require(isinstance(values, list) and len(values) <= 4096, 'KERNEL_PRELINK_COUNT_INVALID')
            for value in values:
                if TARGET_BUNDLES.search(value.get('CFBundleIdentifier', '')):
                    prelink.append(bundle_summary(value))
    return {'fileset_entry_count': len(description['fileset_entries']),
            'all_fileset_identifiers': [x['identifier'] for x in description['fileset_entries']],
            'selected_images': selected, 'selected_prelink_metadata': prelink,
            'native_execution_verified': False}


def analyze(extracted):
    root = extracted.resolve()
    if (root / 'macOS Base System').is_dir():
        root = root / 'macOS Base System'
    library = root / 'System/Library'
    require(library.is_dir(), 'RECOVERY_LIBRARY_REQUIRED')
    report = {'schema': 1, 'inspected_at_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'Selected extracted x86_64 recovery graphics components',
              'source_files': [], 'plists': [], 'kernel_collections': [], 'dyld': None,
              'metal_libraries': [], 'image_mounted': False, 'apple_binary_executed': False,
              'driver_installed': False, 'native_metal_provider_qualified': False,
              'arc130v_acceleration_qualified': False, 'errors': []}
    files = [p for p in library.rglob('*') if p.is_file()]
    require(0 < len(files) <= 100, 'RECOVERY_SELECTED_FILE_COUNT_INVALID')
    for file in files:
        require(not file.is_symlink() and file.resolve().is_relative_to(root), 'RECOVERY_SOURCE_SYMLINK_REFUSED')
        relative = file.relative_to(root).as_posix()
        report['source_files'].append({'path': relative, 'bytes': file.stat().st_size, 'sha256': sha256(file)})
        try:
            if file.name == 'Info.plist':
                require(file.stat().st_size <= 1024 * 1024, 'PLIST_SIZE_LIMIT')
                value = plistlib.loads(file.read_bytes())
                report['plists'].append({'path': relative, 'metadata': bundle_summary(value)})
            elif file.suffix == '.kc':
                with mapped(file) as data:
                    report['kernel_collections'].append({'path': relative} | kernel_summary(data))
            elif file.name == 'IOAccelerator2D':
                with mapped(file) as data:
                    read = lambda offset, size: region(data, offset, size)
                    description = macho(read)
                    report['ioaccelerator_2d_plugin'] = {'path': relative,
                        'dependencies': description['dependencies'], 'symbols': symbol_table(read, description),
                        'metal_provider_qualified': False}
            elif file.suffix in ('.metallib', '.rtlib'):
                with mapped(file) as data:
                    magic = region(data, 0, 4)
                    positions, cursor = [], 0
                    for _ in range(128):
                        found = data.find(b'BC\xc0\xde', cursor)
                        if found < 0:
                            break
                        positions.append(found)
                        cursor = found + 4
                    report['metal_libraries'].append({'path': relative, 'magic_hex': magic.hex(),
                        'mtlb_header_present': magic == b'MTLB', 'llvm_bitcode_marker_offsets': positions,
                        'bitcode_structure_qualified': False, 'xe2_executable_code_qualified': False})
        except (FormatError, struct.error, plistlib.InvalidFileException) as error:
            report['errors'].append({'path': relative, 'code': str(error)})
    cache_main = library / 'dyld/dyld_shared_cache_x86_64'
    if cache_main.is_file():
        cache_paths = [cache_main] + sorted(p for p in cache_main.parent.glob(cache_main.name + '.*')
                                           if re.fullmatch(r'dyld_shared_cache_x86_64\.[0-9a-z.]+', p.name) and
                                           p.suffix not in ('.map', '.atlas', '.symbols'))
        try:
            with contextlib.ExitStack() as stack:
                data = {file.name: stack.enter_context(mapped(file)) for file in cache_paths}
                cache = DyldCache(data)
                targets = [image for image in cache.images if any('/' + name + '/' in image['path'] for name in TARGET_FRAMEWORKS)]
                report['dyld'] = {'cache_files': len(data), 'subcache_uuids_verified': True,
                                  'image_count': len(cache.images), 'mapping_count': len(cache.mappings),
                                  'frameworks': [framework_summary(cache, image) for image in targets],
                                  'intel_driver_cache_paths': [x['path'] for x in cache.images if re.search(r'AppleIntel.*Graphics', x['path'])]}
        except (FormatError, struct.error) as error:
            report['errors'].append({'path': 'System/Library/dyld', 'code': str(error)})
    report['metadata_read_complete_for_supported_inputs'] = not report['errors']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extracted', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    require(not args.report.resolve().is_relative_to(args.extracted.resolve()), 'REPORT_INSIDE_READ_ONLY_INPUT')
    report = analyze(args.extracted)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'state': 'metadata-read' if not report['errors'] else 'partial-metadata',
                      'files_hashed': len(report['source_files']), 'kernel_collections': len(report['kernel_collections']),
                      'dyld_frameworks': len(report['dyld']['frameworks']) if report['dyld'] else 0,
                      'errors': report['errors'], 'native_gpu_qualified': False}))
    return 0 if not report['errors'] else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (FormatError, OSError) as error:
        print(json.dumps({'state': 'unverified', 'code': str(error)}))
        raise SystemExit(2)
