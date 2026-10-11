# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Synthetic slide-chain, instruction and call-boundary checks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import recovery_graphics as fmt
import ioaccel_calls as tool
from capstone import Cs, CS_ARCH_X86, CS_MODE_64, __version__

BASE = 0x100000
MASK = 0x00ffff0000000000


class Cache:
    def __init__(self):
        self.data = bytearray(12288)
        self.files = {'fixture': self.data}
        self.mappings = [{'file': 'fixture', 'address': BASE, 'bytes': 4096, 'offset': 0}]
        struct.pack_into('<II', self.data, 312, 512, 1)
        struct.pack_into('<6Q2I', self.data, 512, BASE, 4096, 0, 8192, 46, 0, 3, 3)
        struct.pack_into('<6I2Q', self.data, 8192, 2, 4096, 40, 1, 42, 2, MASK, BASE)
        struct.pack_into('<H', self.data, 8232, 4)
        struct.pack_into('<Q', self.data, 16, (8 << 38) | 0x300)
        struct.pack_into('<Q', self.data, 24, 0x380)

    def read(self, address, size):
        return fmt.region(self.data, address - BASE, size)


def arguments(code):
    engine = Cs(CS_ARCH_X86, CS_MODE_64)
    engine.detail = True
    # Original synthetic machine code followed by a direct call.
    insns = list(engine.disasm(code + bytes.fromhex('e800000000'), BASE))
    return tool.literal_arguments(insns, len(insns) - 1)


class IoAccelCallTests(unittest.TestCase):
    def test_slide_membership_reconstructs_both_chained_targets_without_mutation(self):
        cache = Cache()
        before = bytes(cache.data)
        slide = tool.SlideV2(cache)
        self.assertEqual(slide.pointer(BASE + 16), BASE + 0x300)
        self.assertEqual(slide.pointer(BASE + 24), BASE + 0x380)
        self.assertEqual(bytes(cache.data), before)

    def test_plausible_pointer_outside_chain_is_rejected(self):
        cache = Cache()
        struct.pack_into('<Q', cache.data, 32, 0x300)
        with self.assertRaisesRegex(fmt.FormatError, 'POINTER_NOT_IN_SLIDE_CHAIN'):
            tool.SlideV2(cache).pointer(BASE + 32)

    def test_slide_page_without_rebases_is_rejected(self):
        cache = Cache()
        struct.pack_into('<H', cache.data, 8232, 0x4000)
        with self.assertRaisesRegex(fmt.FormatError, 'POINTER_PAGE_NOT_REBASED'):
            tool.SlideV2(cache).pointer(BASE + 16)

    def test_slide_extra_chains_are_followed_with_end_marker(self):
        cache = Cache()
        struct.pack_into('<H', cache.data, 8232, 0x8000)
        struct.pack_into('<2H', cache.data, 8234, 4, 0x8008)
        struct.pack_into('<Q', cache.data, 32, 0x390)
        self.assertEqual(tool.SlideV2(cache).pointer(BASE + 32), BASE + 0x390)

    def test_truncated_extra_index_is_rejected(self):
        cache = Cache()
        struct.pack_into('<H', cache.data, 8232, 0x8002)
        with self.assertRaises(fmt.FormatError):
            tool.SlideV2(cache).pointer(BASE + 16)

    def test_chain_cannot_leave_its_page(self):
        cache = Cache()
        struct.pack_into('<Q', cache.data, 16, (4092 << 38) | 0x300)
        with self.assertRaisesRegex(fmt.FormatError, 'SLIDE_CHAIN_INVALID'):
            tool.SlideV2(cache).pointer(BASE + 16)

    def test_overlapping_extra_chains_are_rejected(self):
        cache = Cache()
        struct.pack_into('<H', cache.data, 8232, 0x8000)
        struct.pack_into('<2H', cache.data, 8234, 4, 0x8004)
        with self.assertRaisesRegex(fmt.FormatError, 'SLIDE_CHAIN_INVALID'):
            tool.SlideV2(cache).pointer(BASE + 16)

    def test_unsupported_slide_version_does_not_guess_encoding(self):
        cache = Cache()
        struct.pack_into('<I', cache.data, 8192, 3)
        with self.assertRaisesRegex(fmt.FormatError, 'SLIDE_VERSION_UNSUPPORTED'):
            tool.SlideV2(cache)

    def test_slide_mapping_must_match_verified_cache_mapping(self):
        cache = Cache()
        struct.pack_into('<Q', cache.data, 512, BASE + 16)
        with self.assertRaisesRegex(fmt.FormatError, 'SLIDE_MAPPING_MISMATCH'):
            tool.SlideV2(cache)

    def test_non_contiguous_delta_mask_is_rejected(self):
        cache = Cache()
        struct.pack_into('<Q', cache.data, 8192 + 24, MASK ^ (1 << 44))
        with self.assertRaisesRegex(fmt.FormatError, 'SLIDE_MASK_INVALID'):
            tool.SlideV2(cache)

    def test_decoded_target_must_be_inside_cache(self):
        cache = Cache()
        struct.pack_into('<Q', cache.data, 24, 0xfffff)
        cache.read = lambda a, n: fmt.region(cache.data[:4096], a - BASE, n)
        with self.assertRaises(fmt.FormatError):
            tool.SlideV2(cache).pointer(BASE + 24)

    def test_zero_chain_target_remains_null(self):
        cache = Cache()
        struct.pack_into('<Q', cache.data, 24, 0)
        self.assertEqual(tool.SlideV2(cache).pointer(BASE + 24), 0)

    def test_literals_are_recovered_from_original_synthetic_x86_code(self):
        value = arguments(bytes.fromhex('be0100000031d231c941b904000000'))
        self.assertEqual((value['rsi'], value['rdx'], value['rcx'], value['r9']), (1, 0, 0, 4))

    def test_partial_register_write_discards_prior_selector(self):
        self.assertIsNone(arguments(bytes.fromhex('be0100000040b607'))['rsi'])

    def test_memory_load_does_not_preserve_stale_literal(self):
        self.assertIsNone(arguments(bytes.fromhex('be010000008b37'))['rsi'])

    def test_jump_boundary_does_not_merge_register_states(self):
        self.assertIsNone(arguments(bytes.fromhex('be01000000eb0031d2'))['rsi'])

    def test_call_boundary_does_not_preserve_caller_clobbered_selector(self):
        self.assertIsNone(arguments(bytes.fromhex('be01000000e80000000031d2'))['rsi'])

    def test_local_output_capacity_cell_is_observed_without_runtime_address(self):
        value = arguments(bytes.fromhex('4c8d4de84dc70108000000be0c000000'))
        self.assertEqual(value['r9_capacity_cell'], 8)
        self.assertIsNone(value['r9'])

    def test_modified_capacity_cell_does_not_keep_stale_size(self):
        value = arguments(bytes.fromhex('4c8d4de84dc7010800000049830101'))
        self.assertIsNone(value['r9_capacity_cell'])

    def test_pointer_truncation_cannot_fake_output_capacity_cell(self):
        value = arguments(bytes.fromhex('4c8d4de84589c94dc70108000000'))
        self.assertIsNone(value['r9_capacity_cell'])

    def test_unknown_alias_write_cannot_preserve_capacity(self):
        value = arguments(bytes.fromhex('4c8d4de84dc7010800000048c70700000000'))
        self.assertIsNone(value['r9_capacity_cell'])

    def test_overlapping_narrow_write_cannot_preserve_capacity(self):
        value = arguments(bytes.fromhex('4c8d4de84dc70108000000c645e900'))
        self.assertIsNone(value['r9_capacity_cell'])

    def test_indirect_input_count_remains_unknown(self):
        self.assertIsNone(arguments(bytes.fromhex('4c8b4dc8be00000000'))['r9'])

    def test_function_starts_are_bounded_by_text_and_terminated(self):
        cache = Cache()
        commands = struct.pack('<4I', 0x26, 16, 700, 3)
        struct.pack_into('<II', cache.data, 16, 1, 16)
        cache.data[32:48] = commands
        cache.data[700:703] = b'\x40\x10\0'
        cache.read_image_offset = lambda desc, off, size: fmt.region(cache.data, off, size)
        description = {'sections': [{'name': '__text', 'address': BASE + 64, 'bytes': 64}]}
        self.assertEqual(tool.function_starts(cache, {'address': BASE}, description), [BASE + 64, BASE + 80])
        cache.data[700] = 32
        with self.assertRaisesRegex(fmt.FormatError, 'FUNCTION_START_OUTSIDE_TEXT'):
            tool.function_starts(cache, {'address': BASE}, description)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(IoAccelCallTests))
    if args.report:
        names = ('tools/ioaccel_calls.py', 'tools/recovery_graphics.py', 'tests/test_ioaccel_calls.py',
                 'tools/ioaccel-requirements.txt', '.github/workflows/ioaccel-calls.yml')
        proof = {'schema': 1, 'host_platform': sys.platform, 'source_commit': os.environ.get('GITHUB_SHA'),
                 'capstone': __version__, 'tests_run': result.testsRun, 'failures': len(result.failures),
                 'errors': len(result.errors), 'skipped': len(result.skipped), 'successful': result.wasSuccessful(),
                 'source_files_sha256': {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in names},
                 'apple_binaries_used': False, 'gpu_execution_tested': False}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8', newline='\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
