# SPDX-License-Identifier: GPL-3.0-only
"""Meaningful CPU-only regressions; no OpenCL runtime or GPU dispatch."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
import own_kernel_payload as payload

PROGRAM = (MODULE / 'fixtures' / 'own-xe2-program.bin').read_bytes()


class ActualFixturePayloadTests(unittest.TestCase):
    def setUp(self):
        self.prepared = payload.prepare(PROGRAM)

    def test_exact_cross_thread_layout_and_zero_padding(self):
        self.assertEqual(len(self.prepared.cross_thread), 64)
        self.assertEqual(struct.unpack('<3I', self.prepared.cross_thread[:12]), (0, 0, 0))
        self.assertEqual(struct.unpack('<3I', self.prepared.cross_thread[12:24]), (32, 1, 1))
        self.assertEqual(struct.unpack('<Q', self.prepared.cross_thread[24:32])[0], 0)
        self.assertEqual(struct.unpack('<Q', self.prepared.cross_thread[32:40])[0], 0)
        self.assertEqual(struct.unpack('<I', self.prepared.cross_thread[40:44])[0], 64)
        self.assertEqual(self.prepared.cross_thread[44:], b'\0' * 20)

    def test_inline_indirect_split_places_real_argument_holes(self):
        self.assertEqual(len(self.prepared.inline), 32)
        self.assertEqual(len(self.prepared.indirect), 32)
        self.assertEqual(self.prepared.inline + self.prepared.indirect, self.prepared.cross_thread)
        self.assertEqual(self.prepared.inline[24:32], b'\0' * 8)
        self.assertEqual(self.prepared.indirect[:8], b'\0' * 8)
        self.assertEqual(struct.unpack('<I', self.prepared.indirect[8:12])[0], 64)

    def test_both_bindings_remain_unresolved_with_bounds_and_access(self):
        bindings = self.prepared.report()['unresolved_bindings']
        self.assertEqual([(b['role'], b['cross_thread_offset'], b['segment_offset'], b['access'])
                          for b in bindings], [('input', 24, 24, 'readonly'),
                                                ('output', 32, 0, 'readwrite')])
        self.assertTrue(all(b['resolved'] is False and b['placeholder_uint64'] == 0 for b in bindings))
        self.assertEqual([b['minimum_buffer_bytes'] for b in bindings], [256, 256])

    def test_relocations_preserve_zero_and_exact_original_isa(self):
        self.assertEqual(hashlib.sha256(self.prepared.isa).hexdigest(), payload.ISA_SHA256)
        self.assertEqual(self.prepared.isa, (MODULE / 'fixtures' / 'own-xe2-kernel-isa.bin').read_bytes())
        self.assertEqual([(r.offset, r.original, r.value) for r in self.prepared.relocations],
                         [(76, 0, 0), (156, 0, 0)])
        self.assertEqual(self.prepared.report()['isa_link']['bytes_changed'], 0)

    def test_prepared_data_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.prepared.count = 1
        with self.assertRaises(TypeError):
            self.prepared.cross_thread[24] = 1

    def test_no_native_gpu_metal_or_compiler_claim(self):
        report = self.prepared.report()
        for field in ('dispatch_ready', 'native_gpu_bindings_implemented',
                      'native_gpu_submission_implemented', 'native_macos_metal_execution_demonstrated',
                      'air_msl_translation_implemented', 'shader_compilation_performed',
                      'new_gpu_execution_performed'):
            self.assertIs(report[field], False, field)
        self.assertIs(report['planned_dispatch']['walker_command_encoded'], False)
        self.assertIs(report['planned_dispatch']['local_id_payload_generated'], False)
        self.assertIs(report['planned_dispatch']['entrypoint_selection_implemented'], False)

    def test_historical_unspecified_local_size_is_not_rewritten_as_known(self):
        dispatch = self.prepared.report()['planned_dispatch']
        self.assertEqual(dispatch['enqueued_local_size'], [32, 1, 1])
        self.assertIs(dispatch['planned_local_size_was_hardware_qualified'], False)
        self.assertIsNone(dispatch['historical_windows_launch_requested_local_size'])
        self.assertIs(dispatch['historical_windows_effective_local_size_known'], False)

    def test_non_multiple_count_uses_guarded_complete_groups(self):
        prepared = payload.prepare(PROGRAM, count=65)
        self.assertEqual(prepared.global_size_x, 96)
        self.assertEqual(prepared.report()['planned_dispatch']['group_count'], [3, 1, 1])
        self.assertEqual(struct.unpack('<I', prepared.indirect[8:12])[0], 65)
        self.assertEqual([b.minimum_bytes for b in prepared.bindings], [260, 260])

    def test_minimum_count_and_largest_bounded_count(self):
        for count, global_size in ((1, 32), (4096, 4096)):
            with self.subTest(count=count):
                prepared = payload.prepare(PROGRAM, count=count)
                self.assertEqual(prepared.global_size_x, global_size)
                self.assertEqual(struct.unpack_from('<I', prepared.cross_thread, 40)[0], count)

    def test_local_sizes_change_only_local_values_and_dispatch_plan(self):
        for local_size in (32, 64, 96, 128, 160, 192, 224, 256):
            with self.subTest(local_size=local_size):
                prepared = payload.prepare(PROGRAM, count=64, local_size=local_size)
                self.assertEqual(struct.unpack_from('<3I', prepared.cross_thread, 12), (local_size, 1, 1))
                self.assertEqual(prepared.global_size_x % local_size, 0)
                changed_offsets = [index for index, (a, b) in
                                   enumerate(zip(prepared.cross_thread, self.prepared.cross_thread)) if a != b]
                self.assertTrue(set(changed_offsets).issubset({12, 13, 14, 15}))
                self.assertEqual(prepared.isa, self.prepared.isa)


class RejectionTests(unittest.TestCase):
    def rejected(self, reason, **kwargs):
        with self.assertRaisesRegex(payload.PayloadError, reason):
            payload.prepare(PROGRAM, **kwargs)

    def test_non_uint32_count_rejected(self):
        for value in (True, False, None, '64', 64.0, -1, 0x100000000):
            with self.subTest(value=value):
                self.rejected('count_not_uint32', count=value)

    def test_zero_and_over_budget_count_rejected(self):
        for value in (0, 4097, 0xffffffff):
            with self.subTest(value=value):
                self.rejected('count_outside_preparation_budget', count=value)

    def test_non_uint32_local_size_rejected(self):
        for value in (True, None, '32', 32.0, -1, 0x100000000):
            with self.subTest(value=value):
                self.rejected('local_size_not_uint32', local_size=value)

    def test_partial_simd_and_over_budget_local_size_rejected(self):
        for value in (0, 1, 16, 31, 33, 255, 257, 512):
            with self.subTest(value=value):
                self.rejected('local_size_requires_bounded_complete_simd32', local_size=value)

    def test_rounded_global_size_cannot_exceed_preparation_budget(self):
        self.rejected('rounded_global_size_outside_preparation_budget', count=4096, local_size=96)

    def test_debugger_stack_and_implicit_args_policies_rejected(self):
        for name, reason in (('debugger_active', 'debugger_unsupported'),
                            ('stack_calls_enabled', 'stack_calls_unsupported'),
                            ('implicit_args_required', 'implicit_args_unsupported')):
            with self.subTest(name=name):
                self.rejected(reason, **{name: True})

    def test_non_bool_implicit_policy_inputs_rejected(self):
        for name, reason in (('debugger_active', 'debugger_not_bool'),
                            ('stack_calls_enabled', 'stack_calls_not_bool'),
                            ('implicit_args_required', 'implicit_args_not_bool')):
            for value in (0, 1, None, 'false'):
                with self.subTest(name=name, value=value):
                    self.rejected(reason, **{name: value})

    def test_program_type_and_size_are_exact(self):
        for program in (bytearray(PROGRAM), memoryview(PROGRAM), PROGRAM[:-1], PROGRAM + b'\0', b''):
            with self.subTest(size=len(program)):
                with self.assertRaisesRegex(payload.PayloadError, 'program_size_or_type'):
                    payload.prepare(program)

    def test_any_changed_program_byte_is_rejected_before_parsing(self):
        for position in (0, 64, 140, 2608, 3000, len(PROGRAM) - 1):
            changed = bytearray(PROGRAM)
            changed[position] ^= 1
            with self.subTest(position=position), patch.object(payload.zebin, 'inspect') as inspector:
                with self.assertRaisesRegex(payload.PayloadError, 'program_not_authenticated_own_fixture'):
                    payload.prepare(bytes(changed))
                inspector.assert_not_called()

    def test_gpu_pointer_arguments_are_not_supported(self):
        for name in ('input_gpu_va', 'output_gpu_va', 'gpu_address', 'global_offset'):
            with self.subTest(name=name), self.assertRaises(TypeError):
                payload.prepare(PROGRAM, **{name: 0x100000})

    def test_untrusted_program_structure_fails_closed(self):
        with patch.object(payload.zebin, 'inspect', side_effect=payload.zebin.FormatError('bad')):
            with self.assertRaisesRegex(payload.PayloadError, 'own_program_structure_invalid'):
                payload.prepare(PROGRAM)


class RelocationContractTests(unittest.TestCase):
    def setUp(self):
        self.prepared = payload.prepare(PROGRAM)
        self.records = payload.zebin.inspect(PROGRAM)['relocations']

    def invalid(self, reason, mutate):
        records = deepcopy(self.records)
        mutate(records)
        with self.assertRaisesRegex(payload.PayloadError, reason):
            payload._validate_relocations(self.prepared.isa, records)

    def test_missing_extra_or_non_list_relocations_rejected(self):
        for records in ([], self.records[:1], self.records + self.records[:1], tuple(self.records)):
            with self.subTest(records=records):
                with self.assertRaisesRegex(payload.PayloadError, 'relocation_count_or_type'):
                    payload._validate_relocations(self.prepared.isa, records)

    def test_wrong_order_duplicate_misaligned_and_out_of_range_offsets_rejected(self):
        for offset in (0, 75, 77, 156, 896, 0xffffffffffffffff, True):
            with self.subTest(offset=offset):
                self.invalid('relocation_offset_or_order', lambda records: records[0].update(offset=offset))
        self.invalid('relocation_offset_or_order', lambda records: records.reverse())

    def test_wrong_relocation_width_or_type_rejected(self):
        for kind in (0, 1, 3, 4, True, '2'):
            with self.subTest(kind=kind):
                self.invalid('relocation_type', lambda records: records[0].update(type=kind))
        for width in (0, 8, True, '4'):
            with self.subTest(width=width):
                self.invalid('relocation_width', lambda records: records[0].update(patch_bytes=width))

    def test_other_target_or_table_rejected(self):
        for field in ('target_section', 'table'):
            with self.subTest(field=field):
                self.invalid('relocation_section', lambda records: records[0].update({field: '.other'}))

    def test_symbol_rejected_instead_of_generic_external_linking(self):
        self.invalid('relocation_symbol', lambda records: records[0].update(symbol_name='input_gpu_address'))

    def test_explicit_addend_is_not_silently_accepted(self):
        self.invalid('rela_not_supported', lambda records: records[0].update(explicit_addend=0))

    def test_nonzero_recorded_or_actual_original_value_rejected(self):
        self.invalid('relocation_original_not_zero', lambda records:
                     records[0].update(implicit_addend_raw_hex='40000000'))
        for offset in payload.PATCH_OFFSETS:
            changed = bytearray(self.prepared.isa)
            struct.pack_into('<I', changed, offset, 64)
            with self.subTest(offset=offset), self.assertRaisesRegex(payload.PayloadError, 'original_not_zero'):
                payload._validate_relocations(bytes(changed), self.records)

    def test_invalid_isa_type_size_and_record_type_rejected(self):
        for isa in (self.prepared.isa[:-1], bytearray(self.prepared.isa)):
            with self.assertRaisesRegex(payload.PayloadError, 'isa_size_or_type'):
                payload._validate_relocations(isa, self.records)
        self.invalid('relocation_record_type', lambda records: records.__setitem__(0, None))


class BundleAndCliTests(unittest.TestCase):
    def setUp(self):
        self.prepared = payload.prepare(PROGRAM)

    def test_bundle_hashes_match_all_written_bytes_and_original_fixture_stays_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'new-payload'
            report = payload.write_bundle(self.prepared, destination)
            saved = json.loads((destination / 'PAYLOAD_REPORT.json').read_text(encoding='utf-8'))
            self.assertEqual(report, saved)
            self.assertEqual(len(list(destination.iterdir())), 5)
            for entry in saved['bundle_files']:
                actual = (destination / entry['filename']).read_bytes()
                self.assertEqual(len(actual), entry['bytes'])
                self.assertEqual(hashlib.sha256(actual).hexdigest(), entry['sha256'])
            self.assertNotIn(temporary, json.dumps(saved))
        self.assertEqual(payload.read_own_fixture(), PROGRAM)

    def test_existing_output_directory_is_not_written_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            marker = destination / 'keep.bin'
            marker.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                payload.write_bundle(self.prepared, destination)
            self.assertEqual(list(destination.iterdir()), [marker])
            self.assertEqual(marker.read_bytes(), b'keep')

    def test_noncanonical_prepared_object_cannot_be_exported(self):
        for changed in (replace(self.prepared, isa=b'\0' * 896),
                        replace(self.prepared, cross_thread=b'\0' * 64),
                        replace(self.prepared, inline=b'\1' * 32),
                        replace(self.prepared, global_size_x=32),
                        replace(self.prepared, bindings=())):
            with tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / 'uncreated'
                with self.assertRaisesRegex(payload.PayloadError, 'prepared_object_not_canonical'):
                    payload.write_bundle(changed, destination)
                self.assertFalse(destination.exists())

    def test_default_cli_only_prints_path_free_cpu_report(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = payload.main([])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertIs(report['dispatch_ready'], False)
        self.assertNotIn('bundle_files', report)
        self.assertNotIn(str(MODULE), output.getvalue())

    def test_subclass_cannot_bypass_validation_or_forge_execution_proof(self):
        class ForgedPrepared(payload.PreparedOwnKernel):
            def __eq__(self, other):
                return True

            def report(self):
                return {'dispatch_ready': True, 'native_macos_metal_execution_demonstrated': True}

        attributes = dict(self.prepared.__dict__)
        patched = bytearray(self.prepared.isa)
        struct.pack_into('<I', patched, 76, 64)
        attributes['isa'] = bytes(patched)
        forged = ForgedPrepared(**attributes)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'uncreated'
            with self.assertRaisesRegex(payload.PayloadError, 'prepared_type'):
                payload.write_bundle(forged, destination)
            self.assertFalse(destination.exists())

    def test_cli_rejection_has_stable_reason_and_nonzero_status(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = payload.main(['--count', '4097'])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue()),
                         {'status': 'rejected', 'reason': 'count_outside_preparation_budget'})

    def test_cli_io_failure_does_not_leak_private_path(self):
        output = io.StringIO()
        with patch.object(payload, 'read_own_fixture', side_effect=OSError('/private/user/file')):
            with redirect_stdout(output):
                status = payload.main([])
        self.assertEqual(status, 2)
        self.assertNotIn('/private/user/file', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['reason'], 'fixture_or_output_io_error')

    def test_cli_has_no_execute_or_gpu_pointer_option(self):
        for flag in ('--execute', '--gpu-address'):
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                payload.main([flag])
            self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
