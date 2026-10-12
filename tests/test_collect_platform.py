# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""The platform collector must reduce what it reads to facts, keep identifiers
out of the record, derive its capabilities correctly, and skip rather than guess."""
import importlib.util
from pathlib import Path
import platform
import plistlib
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load('collect_platform', 'tools/collect_platform.py')
POLICY = load('abi_policy', 'tools/abi_policy.py')
COMPARE = load('compare_interfaces', 'tools/compare_interfaces.py')


class ReducesValues(unittest.TestCase):
    def test_a_short_blob_becomes_a_number(self):
        self.assertEqual(TOOL.reduce_value('device-id', b'\xa0\x64\x00\x00'),
                         {'bytes': 4, 'value': 0x64A0, 'hex': '0x64a0'})

    def test_a_long_blob_becomes_length_and_digest(self):
        reduced = TOOL.reduce_value('AAPL,ig-platform-id', bytes(range(64)))
        self.assertEqual(reduced['bytes'], 64)
        self.assertEqual(len(reduced['sha256']), 64)
        self.assertNotIn('value', reduced)

    def test_an_edid_is_never_reproduced(self):
        edid = bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00] + [0x41] * 120)
        reduced = TOOL.reduce_value('IODisplayEDID', edid)
        self.assertEqual(POLICY.check_record({'IODisplayEDID': reduced}), [])
        self.assertNotIn('value', reduced)

    def test_nested_values_are_reduced_and_bounded(self):
        reduced = TOOL.reduce_value('node', {'a': b'\x01', 'b': ['x' * 900], 'c': {'d': 2}})
        self.assertEqual(reduced['a']['value'], 1)
        self.assertLessEqual(len(reduced['b'][0]), 400)
        self.assertEqual(reduced['c']['d'], 2)

    def test_a_reduced_tree_passes_the_policy_gate(self):
        node = {'vendor-id': b'\x86\x80\x00\x00', 'device-id': b'\xa0\x64\x00\x00',
                'AAPL,ig-platform-id': bytes(range(256)), 'name': 'GFX0'}
        reduced = {key: TOOL.reduce_value(key, value) for key, value in node.items()}
        self.assertEqual(POLICY.check_record(reduced), [])


class WalksRegistryTrees(unittest.TestCase):
    def test_children_are_visited_with_their_depth(self):
        tree = [{'IOClass': 'Root', 'IORegistryEntryChildren': [
            {'IOClass': 'Child', 'IORegistryEntryChildren': [{'IOClass': 'Leaf'}]}]}]
        seen = [(depth, node['IOClass']) for depth, node in TOOL.walk(tree)]
        self.assertEqual(seen, [(0, 'Root'), (1, 'Child'), (2, 'Leaf')])

    def test_malformed_entries_are_skipped_not_raised(self):
        tree = [{'IOClass': 'Root', 'IORegistryEntryChildren': ['nonsense', 7, None]}]
        self.assertEqual(len(list(TOOL.walk(tree))), 1)


class DecodesSip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flags = TOOL.csr_flags(COMPARE.read_reference)

    def test_the_flags_come_from_the_pinned_header(self):
        self.assertEqual(self.flags['citation'], TOOL.CSR_HEADER)
        names = [name for names in self.flags['bits'].values() for name in names]
        for required in ('CSR_ALLOW_UNTRUSTED_KEXTS', 'CSR_ALLOW_UNRESTRICTED_DTRACE',
                         'CSR_ALLOW_TASK_FOR_PID'):
            self.assertIn(required, names)

    def test_sip_fully_enabled_allows_nothing(self):
        decoded = TOOL.decode_csr(0, self.flags)
        self.assertEqual(decoded['allowed'], [])
        self.assertIn('CSR_ALLOW_UNTRUSTED_KEXTS', decoded['denied'])

    def test_a_word_is_split_into_allowed_and_denied(self):
        decoded = TOOL.decode_csr(0x01 | 0x20, self.flags)
        self.assertIn('CSR_ALLOW_UNTRUSTED_KEXTS', decoded['allowed'])
        self.assertIn('CSR_ALLOW_UNRESTRICTED_DTRACE', decoded['allowed'])
        self.assertIn('CSR_ALLOW_DESTRUCTIVE_DTRACE',
                      decoded['aliases']['CSR_ALLOW_UNRESTRICTED_DTRACE'])
        self.assertIn('CSR_ALLOW_TASK_FOR_PID', decoded['denied'])
        self.assertEqual(decoded['hex'], '0x21')


class ReadsOpenCoreWithoutIdentifiers(unittest.TestCase):
    def config(self, directory: Path) -> Path:
        target = directory / 'EFI/OC'
        target.mkdir(parents=True)
        plist = {
            '#Revision': {'Version': 'REL-102-2026-01-01'},
            'DeviceProperties': {'Add': {
                'PciRoot(0x0)/Pci(0x2,0x0)': {'AAPL,ig-platform-id': b'\x00\x00\x00\x64',
                                              'device-id': b'\xa0\x64\x00\x00',
                                              'framebuffer-patch-enable': 1},
                'PciRoot(0x0)/Pci(0x1f,0x3)': {'layout-id': 7}}},
            'Kernel': {'Add': [{'BundlePath': 'Lilu.kext', 'Enabled': True,
                                'ExecutablePath': 'Contents/MacOS/Lilu'},
                               {'BundlePath': 'WhateverGreen.kext', 'Enabled': True}],
                       'Patch': [{'Comment': 'one'}, {'Comment': 'two'}],
                       'Block': [], 'Quirks': {'DisableLinkeditJettison': True}},
            'NVRAM': {'Add': {'7C436110-AB2A-4BBB-A880-FE41995C9F82': {
                'boot-args': '-v debug=0x100 keepsyms=1',
                'csr-active-config': b'\x21\x00\x00\x00'}}},
            'PlatformInfo': {'Generic': {'SystemSerialNumber': 'C02XXXXXXXXX',
                                         'MLB': 'C02XXXXXXXXXXXXXX',
                                         'SystemUUID': '00000000-0000-0000-0000-000000000000',
                                         'ROM': b'\x11\x22\x33\x44\x55\x66'}},
        }
        (target / 'config.plist').write_bytes(plistlib.dumps(plist))
        return directory

    def test_gpu_properties_and_kexts_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            record = TOOL.read_opencore(self.config(Path(directory)))
        self.assertTrue(record['performed'])
        self.assertEqual(record['kernel_patch_count'], 2)
        self.assertEqual([entry['bundle'] for entry in record['kexts_added']],
                         ['Lilu.kext', 'WhateverGreen.kext'])
        self.assertIn('PciRoot(0x0)/Pci(0x2,0x0)', record['gpu_device_properties'])
        gpu = record['gpu_device_properties']['PciRoot(0x0)/Pci(0x2,0x0)']
        self.assertEqual(gpu['device-id']['hex'], '0x64a0')
        self.assertEqual(record['boot_args'], '-v debug=0x100 keepsyms=1')

    def test_unrelated_devices_are_left_out(self):
        with tempfile.TemporaryDirectory() as directory:
            record = TOOL.read_opencore(self.config(Path(directory)))
        self.assertNotIn('PciRoot(0x0)/Pci(0x1f,0x3)', record['gpu_device_properties'])

    def test_no_identifier_reaches_the_record(self):
        with tempfile.TemporaryDirectory() as directory:
            record = TOOL.read_opencore(self.config(Path(directory)))
        # The note names what is deliberately not read, so compare without it.
        flat = repr({key: value for key, value in record.items() if key != 'note'})
        for secret in ('C02XXXXXXXXX', 'SystemUUID', 'MLB', '112233445566'):
            self.assertNotIn(secret, flat)
        self.assertEqual(POLICY.check_record(record), [])

    def test_a_missing_config_is_reported_not_invented(self):
        with tempfile.TemporaryDirectory() as directory:
            record = TOOL.read_opencore(Path(directory))
        self.assertFalse(record['performed'])
        self.assertIn('config.plist', record['reason'])


class DerivesCapabilities(unittest.TestCase):
    def test_an_open_machine_reports_what_it_permits(self):
        record = {'security': {'sip': {'value': 0x21, 'hex': '0x21',
                                       'allowed': ['CSR_ALLOW_UNTRUSTED_KEXTS',
                                                   'CSR_ALLOW_UNRESTRICTED_DTRACE'],
                                       'denied': ['CSR_ALLOW_TASK_FOR_PID']}},
                  'pci': {'performed': True, 'target_present': True, 'device_count': 12,
                          'target_driver_attached': False},
                  'graphics_stack': {'performed': True, 'accelerator_present': False},
                  'loaded': {'performed': True, 'own_driver_loaded': False}}
        derived = {entry['capability']: entry for entry in TOOL.capabilities(record)}
        self.assertTrue(derived['load an unsigned kernel extension']['possible'])
        self.assertTrue(derived['trace the kernel with dtrace']['possible'])
        self.assertFalse(derived['attach a debugger to a system process']['possible'])
        self.assertTrue(derived['observe the target GPU in the device tree']['possible'])
        self.assertFalse(derived['watch an Apple driver on this GPU']['possible'])
        self.assertIn('supported', derived['watch an Apple driver on this GPU']['unblock'])

    def test_a_locked_machine_says_what_has_to_change(self):
        record = {'security': {'sip': {'value': 0, 'hex': '0x0', 'allowed': [],
                                       'denied': ['CSR_ALLOW_UNTRUSTED_KEXTS']}}}
        derived = {entry['capability']: entry for entry in TOOL.capabilities(record)}
        entry = derived['load an unsigned kernel extension']
        self.assertFalse(entry['possible'])
        self.assertIn('recovery', entry['unblock'])

    def test_nothing_is_claimed_when_the_word_was_not_read(self):
        derived = TOOL.capabilities({'security': {'sip': {}}})
        self.assertEqual([entry['capability'] for entry in derived], ['read the SIP word'])
        self.assertFalse(derived[0]['possible'])


class SkipsRatherThanGuesses(unittest.TestCase):
    def test_every_macos_stage_skips_with_a_reason_elsewhere(self):
        if platform.system() == 'Darwin':
            self.skipTest('this check describes the non-macOS path')
        for stage in (TOOL.stage_identity, TOOL.stage_graphics_stack, TOOL.stage_loaded,
                      TOOL.stage_collections, TOOL.stage_displays):
            record = stage()
            self.assertFalse(record['performed'], stage.__name__)
            self.assertIn('macOS', record['reason'], stage.__name__)

    def test_a_missing_command_is_reported(self):
        result = TOOL.run(['lunarbridge-absent-command'])
        self.assertFalse(result['performed'])
        self.assertIn('not found', result['reason'])

    def test_the_nvram_allowlist_excludes_identifiers(self):
        for key in TOOL.NVRAM_KEYS:
            self.assertIsNone(TOOL.NVRAM_SKIP_PATTERN.search(key), key)
        for forbidden in ('SystemSerialNumber', 'MLB', 'ROM', 'SystemUUID', 'fmm-mobileme-token'):
            self.assertIsNotNone(TOOL.NVRAM_SKIP_PATTERN.search(forbidden), forbidden)


if __name__ == '__main__':
    unittest.main(verbosity=2)
