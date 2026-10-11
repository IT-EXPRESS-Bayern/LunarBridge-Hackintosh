# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Synthetic binary-contract checks; no Apple payload or target GPU required."""
import importlib.util
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import tempfile
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('recovery_graphics', ROOT / 'tools/recovery_graphics.py')
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def header(commands, kind=6, cpu=0x01000007):
    return struct.pack('<8I', 0xFEEDFACF, cpu, 3, kind, len(commands), sum(map(len, commands)), 0, 0) + b''.join(commands)


def segment(name='__TEXT', address=0x100000, length=4096, file_offset=0):
    return struct.pack('<II16s4Q4I', 0x19, 72, name.encode(), address, length, file_offset, length, 5, 5, 0, 0)


def synthetic_cache(sub=False):
    data = bytearray(4096)
    data[:16] = b'dyld_v1  x86_64\0'
    struct.pack_into('<2I', data, 16, 512, 1)
    struct.pack_into('<3Q2I', data, 512, 0x200000 if sub else 0x100000, len(data), 0, 5, 5)
    data[88:104] = (b'\x22' if sub else b'\x11') * 16
    struct.pack_into('<2I', data, 448, 1024, 1)
    struct.pack_into('<3Q2I', data, 1024, 0x100800, 0, 0, 1080, 0)
    path = b'/System/Library/Frameworks/Metal.framework/Versions/A/Metal\0'
    data[1080:1080 + len(path)] = path
    own_header = header([segment()])
    data[2048:2048 + len(own_header)] = own_header
    return data


def synthetic_collection():
    data = bytearray(8192)
    identifier = b'com.apple.iokit.IOAcceleratorFamily2\0'
    entry_size = (32 + len(identifier) + 7) & ~7
    entry = struct.pack('<IIQQII', 0x80000035, entry_size, 0x200000, 512, 32, 0)
    entry += identifier + bytes(entry_size - 32 - len(identifier))
    metadata = plistlib.dumps({'_PrelinkInfoDictionary': [{'CFBundleIdentifier': identifier[:-1].decode(), 'CFBundleVersion': '1.2.3'}]})
    section = struct.pack('<16s16sQQIIIIIIII', b'__info', b'__PRELINK_INFO', 0x101000, len(metadata), 1024, 0, 0, 0, 0, 0, 0, 0)
    seg = struct.pack('<II16s4Q4I', 0x19, 152, b'__PRELINK_INFO', 0x101000, len(metadata), 1024, len(metadata), 1, 1, 1, 0) + section
    root = header([entry, seg], kind=12)
    data[:len(root)] = root
    child = header([struct.pack('<6I', 2, 24, 4096, 0, 4096, 1)], kind=11)
    data[512:512 + len(child)] = child
    data[1024:1024 + len(metadata)] = metadata
    return data


class RecoveryGraphicsTests(unittest.TestCase):
    def test_declared_full_width_macho_name_needs_no_nul(self):
        self.assertEqual(tool.fixed_name(b'__objc_classlist'), '__objc_classlist')

    def test_binary_ranges_reject_negative_overflow_and_large_reads(self):
        for offset, size in ((-1, 2), (9, 2), (2, -1), (0, tool.MAX_READ + 1)):
            with self.subTest(offset=offset, size=size), self.assertRaises(tool.FormatError):
                tool.region(b'0123456789', offset, size)

    def test_unterminated_string_does_not_read_past_its_container(self):
        with self.assertRaises(tool.FormatError):
            tool.cstring(b'abcd\0', 0, 4)

    def test_macho_accepts_x86_64_dylib_load_commands(self):
        data = header([segment()])
        report = tool.macho(lambda offset, size: tool.region(data, offset, size))
        self.assertEqual(report['architecture'], 'x86_64')
        self.assertEqual(report['segments'][0]['address'], 0x100000)

    def test_non_x86_64_macho_is_not_silently_accepted(self):
        data = header([segment()], cpu=0x0100000C)
        with self.assertRaises(tool.FormatError):
            tool.macho(lambda offset, size: tool.region(data, offset, size))

    def test_load_command_cannot_escape_declared_area(self):
        data = bytearray(header([segment()]))
        struct.pack_into('<I', data, 36, 80)
        with self.assertRaises(tool.FormatError):
            tool.macho(lambda offset, size: tool.region(data, offset, size))

    def test_section_count_must_match_command_size(self):
        data = bytearray(header([segment()]))
        struct.pack_into('<I', data, 32 + 64, 1)
        with self.assertRaises(tool.FormatError):
            tool.macho(lambda offset, size: tool.region(data, offset, size))

    def test_load_command_count_is_bounded(self):
        data = bytearray(header([segment()]))
        struct.pack_into('<I', data, 16, 4097)
        with self.assertRaises(tool.FormatError):
            tool.macho(lambda offset, size: tool.region(data, offset, size))

    def test_export_trie_decodes_original_symbol_and_value(self):
        trie = b'\0\1_probe\0\x0a\x02\0\x20\0'
        self.assertEqual(tool.exported_symbols(trie), [{'name': '_probe', 'flags': 0, 'reexport': False, 'address_offset': 32}])

    def test_export_trie_reexport_is_distinguished_from_code(self):
        terminal = b'\x08\x01_real\0'
        trie = b'\0\1_probe\0\x0a' + bytes([len(terminal)]) + terminal + b'\0'
        record = tool.exported_symbols(trie)[0]
        self.assertTrue(record['reexport'])
        self.assertEqual(record['import_name'], '_real')
        self.assertNotIn('address_offset', record)

    def test_cyclic_export_trie_cannot_hang_analysis(self):
        with self.assertRaises(tool.FormatError):
            tool.exported_symbols(b'\0\1x\0\0')

    def test_truncated_export_terminal_is_rejected(self):
        with self.assertRaises(tool.FormatError):
            tool.exported_symbols(b'\x10\0')

    def test_export_child_cannot_reference_outside_data(self):
        with self.assertRaises(tool.FormatError):
            tool.exported_symbols(b'\0\1x\0\x7f')

    def test_export_empty_edge_is_rejected(self):
        with self.assertRaises(tool.FormatError):
            tool.exported_symbols(b'\0\1\0\0')

    def test_uleb_overflow_is_rejected(self):
        with self.assertRaises(tool.FormatError):
            tool.uleb(b'\xff' * 10, 0)

    def test_uleb_truncation_is_rejected(self):
        with self.assertRaises(tool.FormatError):
            tool.uleb(b'\x80', 0)

    def test_cache_image_table_and_vm_mapping_are_read(self):
        data = synthetic_cache()
        cache = tool.DyldCache({'dyld_shared_cache_x86_64': data})
        self.assertEqual(len(cache.images), 1)
        self.assertEqual(cache.read(0x100800, 4), b'\xcf\xfa\xed\xfe')

    def test_vm_read_cannot_cross_mapping_end(self):
        cache = tool.DyldCache({'dyld_shared_cache_x86_64': synthetic_cache()})
        with self.assertRaises(tool.FormatError):
            cache.read(0x100FFF, 2)

    def test_cache_mapping_outside_file_is_rejected(self):
        data = synthetic_cache()
        struct.pack_into('<Q', data, 512 + 8, 4097)
        with self.assertRaises(tool.FormatError):
            tool.DyldCache({'dyld_shared_cache_x86_64': data})

    def test_cache_image_count_is_bounded(self):
        data = synthetic_cache()
        struct.pack_into('<I', data, 452, 65537)
        with self.assertRaises(tool.FormatError):
            tool.DyldCache({'dyld_shared_cache_x86_64': data})

    def test_cache_image_path_is_bounded(self):
        data = synthetic_cache()
        data[1080:] = b'x' * (len(data) - 1080)
        with self.assertRaises(tool.FormatError):
            tool.DyldCache({'dyld_shared_cache_x86_64': data})

    def test_subcache_uuid_pairing_is_verified(self):
        main, sub = synthetic_cache(), synthetic_cache(sub=True)
        struct.pack_into('<2I', main, 392, 1152, 1)
        main[1152:1208] = struct.pack('<16sQ32s', b'\x22' * 16, 0x100000, b'.01')
        cache = tool.DyldCache({'dyld_shared_cache_x86_64': main, 'dyld_shared_cache_x86_64.01': sub})
        self.assertEqual(cache.read(0x200000, 16), sub[:16])

    def test_subcache_uuid_mismatch_is_rejected(self):
        main, sub = synthetic_cache(), synthetic_cache(sub=True)
        struct.pack_into('<2I', main, 392, 1152, 1)
        main[1152:1208] = struct.pack('<16sQ32s', b'\x33' * 16, 0x100000, b'.01')
        with self.assertRaises(tool.FormatError):
            tool.DyldCache({'dyld_shared_cache_x86_64': main, 'dyld_shared_cache_x86_64.01': sub})

    def test_missing_required_subcache_is_rejected(self):
        data = synthetic_cache()
        struct.pack_into('<2I', data, 392, 1152, 1)
        with self.assertRaises(tool.FormatError):
            tool.DyldCache({'dyld_shared_cache_x86_64': data})

    def test_kernel_fileset_and_prelink_metadata_are_read(self):
        result = tool.kernel_summary(synthetic_collection())
        self.assertEqual(result['fileset_entry_count'], 1)
        self.assertEqual(result['selected_prelink_metadata'][0]['CFBundleVersion'], '1.2.3')
        self.assertFalse(result['native_execution_verified'])

    def test_kernel_fileset_offset_cannot_escape_file(self):
        data = synthetic_collection()
        struct.pack_into('<Q', data, 32 + 16, len(data) + 1)
        with self.assertRaises(tool.FormatError):
            tool.kernel_summary(data)

    def test_non_fileset_cannot_be_reported_as_kernel_collection(self):
        data = synthetic_collection()
        struct.pack_into('<I', data, 12, 6)
        with self.assertRaises(tool.FormatError):
            tool.kernel_summary(data)

    def test_symbol_names_must_stay_inside_string_table(self):
        data = struct.pack('<IBBHQ', 100, 1, 0, 0, 0) + b'\0'
        with self.assertRaises(tool.FormatError):
            tool.symbol_table(lambda offset, size: tool.region(data, offset, size), {'symbol_table': [0, 1, 16, 1]})

    def test_missing_symbol_table_is_not_fabricated(self):
        self.assertEqual(tool.symbol_table(None, {'symbol_table': None}), {'available': False, 'selected': []})

    def test_read_only_mapping_does_not_modify_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'own-fixture.bin'
            path.write_bytes(b'own binary test data')
            before = tool.sha256(path)
            with tool.mapped(path) as data:
                self.assertEqual(data[:3], b'own')
                with self.assertRaises(TypeError):
                    data[0] = 42
            self.assertEqual(before, tool.sha256(path))

    def test_declared_ivar_offset_does_not_claim_runtime_layout(self):
        data = synthetic_cache()
        name = b'_OBJC_IVAR_$_MTLIOAccelDevice._deviceRef'
        child = len(name) + 4
        trie = b'\0\1' + name + b'\0' + bytes([child]) + b'\x03\0\x80\x0a\0'
        own_header = header([segment(address=0x100800, length=2048), struct.pack('<4I', 0x80000033, 16, 1024, len(trie))])
        data[2048:2048 + len(own_header)] = own_header
        data[3072:3072 + len(trie)] = trie
        struct.pack_into('<I', data, 3328, 712)
        cache = tool.DyldCache({'dyld_shared_cache_x86_64': data})
        result = tool.framework_summary(cache, cache.images[0])
        self.assertEqual(result['declared_ivar_offsets'][0]['raw_declared_byte_offset'], 712)
        self.assertFalse(result['declared_ivar_offsets'][0]['runtime_adjustment_qualified'])
        self.assertFalse(result['native_provider_abi_qualified'])

    def test_unreasonable_ivar_offset_is_rejected(self):
        data = synthetic_cache()
        name = b'_OBJC_IVAR_$_MTLIOAccelDevice._deviceRef'
        trie = b'\0\1' + name + b'\0' + bytes([len(name) + 4]) + b'\x03\0\x80\x0a\0'
        own_header = header([segment(address=0x100800, length=2048), struct.pack('<4I', 0x80000033, 16, 1024, len(trie))])
        data[2048:2048 + len(own_header)] = own_header
        data[3072:3072 + len(trie)] = trie
        struct.pack_into('<I', data, 3328, 0xFFFFFFFF)
        cache = tool.DyldCache({'dyld_shared_cache_x86_64': data})
        with self.assertRaises(tool.FormatError):
            tool.framework_summary(cache, cache.images[0])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RecoveryGraphicsTests))
    if args.report:
        names = ['tools/recovery_graphics.py', 'tests/test_recovery_graphics.py', '.github/workflows/recovery-analysis.yml']
        sha = os.environ.get('GITHUB_SHA')
        proof = {'schema': 1, 'scope': 'Synthetic binary format and input boundary tests',
                 'host_platform': sys.platform, 'source_commit': sha if sha and len(sha) == 40 else None,
                 'source_files_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names},
                 'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
                 'skipped': len(result.skipped), 'successful': result.wasSuccessful(),
                 'apple_binaries_used': False, 'gpu_execution_tested': False}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
