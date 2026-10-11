# SPDX-License-Identifier: GPL-3.0-only
"""Offline regressions. These tests do not initialize OpenCL or dispatch work."""
import hashlib
import json
from pathlib import Path
import struct
import unittest
import sys

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))

import inspect_own_zebin as elf
import opencl_xe2_probe as probe

WORK = MODULE / 'fixtures'


class DeviceQualificationTests(unittest.TestCase):
    def setUp(self):
        self.device = {'name': 'Intel(R) Arc(TM) 130V GPU (8GB)', 'device_type': 4,
                       'vendor_id': 0x8086, 'available': True, 'compiler_available': True}

    def test_actual_arc130v_qualification(self):
        self.assertTrue(probe.qualifies(self.device))

    def test_cpu_named_arc_rejected(self):
        self.device['device_type'] = 2
        self.assertFalse(probe.qualifies(self.device))

    def test_other_vendor_named_arc_rejected(self):
        self.device['vendor_id'] = 0x1002
        self.assertFalse(probe.qualifies(self.device))

    def test_other_intel_gpu_rejected(self):
        self.device['name'] = 'Intel(R) Arc(TM) 140V GPU'
        self.assertFalse(probe.qualifies(self.device))

    def test_partial_device_name_rejected(self):
        self.device['name'] = 'Intel Arc 130VeryDifferent'
        self.assertFalse(probe.qualifies(self.device))

    def test_unavailable_gpu_rejected(self):
        self.device['available'] = False
        self.assertFalse(probe.qualifies(self.device))

    def test_missing_compiler_rejected(self):
        self.device['compiler_available'] = False
        self.assertFalse(probe.qualifies(self.device))


class ActualResultTests(unittest.TestCase):
    def test_real_gpu_results_all_equal_reference_and_recorded_digest(self):
        data = (WORK / 'own-kernel-results.bin').read_bytes()
        actual = struct.unpack('<64I', data)
        verified = probe.verify_output(actual, probe.inputs())
        report = json.loads((WORK / 'WINDOWS_GPU_SHADER_PROOF.json').read_text(encoding='utf-8'))
        self.assertEqual(verified, report['verified_results'])

    def test_reference_unsigned_overflow_known_values(self):
        self.assertEqual(probe.expected_output([0, 0xffffffff]), [0x99CBA905, 0x99F0D709])

    def test_one_wrong_result_fails(self):
        actual = probe.expected_output(probe.inputs())
        actual[63] ^= 1
        with self.assertRaises(probe.ProbeError) as raised:
            probe.verify_output(actual, probe.inputs())
        self.assertEqual(raised.exception.code, 'gpu_result_mismatch')
        self.assertEqual(raised.exception.details, {'mismatch_indices': [63]})

    def test_short_result_fails(self):
        with self.assertRaises(probe.ProbeError):
            probe.verify_output(probe.expected_output(probe.inputs())[:-1], probe.inputs())

    def test_bad_opencl_status_fails(self):
        with self.assertRaises(probe.ProbeError) as raised:
            probe.checked(-11, 'build_own_program')
        self.assertEqual(raised.exception.details, {'status': -11})


class ElfReaderTests(unittest.TestCase):
    def setUp(self):
        self.data = (WORK / 'own-xe2-program.bin').read_bytes()
        self.headers_offset = struct.unpack_from('<Q', self.data, 40)[0]

    def mutate(self, offset, format, value):
        result = bytearray(self.data)
        struct.pack_into(format, result, offset, value)
        return bytes(result)

    def section_offset(self, index, field_offset):
        return self.headers_offset + 64 * index + field_offset

    def test_actual_program_hash_and_zebin_sections(self):
        self.assertEqual(hashlib.sha256(self.data).hexdigest(), elf.EXPECTED_BINARY_SHA256)
        report = elf.inspect(self.data)
        self.assertEqual(report['machine'], 205)
        self.assertEqual(report['elf_type'], 1)
        self.assertEqual(report['section_count'], 10)
        self.assertEqual(report['raw_isa']['bytes'], 896)
        self.assertEqual(len(report['intelgt_notes']), 7)

    def test_null_section_ignored_offset_accepted(self):
        self.assertEqual(elf.parse_elf64(self.data)['sections'][0]['offset'], 64)

    def test_actual_runtime_relocations_are_reported_not_applied(self):
        report = elf.inspect(self.data)
        self.assertEqual([item['offset'] for item in report['relocations']], [76, 156])
        self.assertEqual({item['type_name'] for item in report['relocations']}, {'R_ZE_SYM_ADDR_32'})
        self.assertEqual({item['symbol_name'] for item in report['relocations']},
                         {'__INTEL_PATCH_CROSS_THREAD_OFFSET_OFF_R0'})
        self.assertFalse(report['relocations_applied_by_inspector'])

    def test_exported_sections_byte_equal_own_program(self):
        report = json.loads((WORK / 'OWN_SHADER_BINARY_METADATA.json').read_text(encoding='utf-8'))
        by_name = {item['name']: item for item in elf.parse_elf64(self.data)['sections']}
        for item in report['exports']:
            actual = (WORK / item['filename']).read_bytes()
            self.assertEqual(actual, elf.section_bytes(self.data, by_name[item['source_section']]))
            self.assertEqual(hashlib.sha256(actual).hexdigest(), item['sha256'])

    def test_x86_elf_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(18, '<H', 62))

    def test_wrong_elf_type_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(16, '<H', 2))

    def test_big_endian_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(5, '<B', 2))

    def test_header_short_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.data[:63])

    def test_section_count_over_budget_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(60, '<H', 257))

    def test_extended_section_count_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(60, '<H', 0))

    def test_section_table_truncation_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.data[:-1])

    def test_section_payload_range_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(self.section_offset(1, 24), '<Q', len(self.data) + 1))

    def test_header_overlap_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(self.section_offset(1, 24), '<Q', 0))

    def test_invalid_section_alignment_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(self.section_offset(1, 48), '<Q', 3))

    def test_bad_name_offset_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(self.section_offset(1, 0), '<I', 65535))

    def test_duplicate_names_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(self.section_offset(2, 0), '<I', 1))

    def test_symbol_section_invalid_rejected(self):
        symbol1_shndx = 960 + 24 + 6
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(symbol1_shndx, '<H', 300))

    def test_symbol_value_invalid_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(960 + 24 + 8, '<Q', 897))

    def test_relocation_symbol_invalid_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(2608 + 8, '<Q', (300 << 32) | 2))

    def test_relocation_patch_outside_text_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(2608, '<Q', 894))

    def test_unknown_relocation_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.inspect(self.mutate(2608 + 8, '<Q', (3 << 32) | 0xffff))

    def test_intelgt_note_truncated_rejected(self):
        notes = self.data[4688:4860]
        with self.assertRaises(elf.FormatError):
            elf.parse_notes(notes[:-1])

    def test_intelgt_note_descriptor_over_budget_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.parse_notes(struct.pack('<III', 8, 20000, 1) + b'IntelGT\0')

    def test_note_owner_unterminated_rejected(self):
        with self.assertRaises(elf.FormatError):
            elf.parse_notes(struct.pack('<III', 8, 4, 1) + b'IntelGTX' + b'\0' * 4)

    def test_input_bytes_remain_unchanged(self):
        before = hashlib.sha256(self.data).hexdigest()
        elf.inspect(self.data)
        self.assertEqual(hashlib.sha256(self.data).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
