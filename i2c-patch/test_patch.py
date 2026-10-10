# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Host-side checks of a source patch; these never load a driver or touch hardware."""
import copy
import json
from pathlib import Path
import plistlib
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / 'i2c-patch'
ORIGINAL = ROOT / 'tests' / '.references' / 'VoodooI2C' / 'VoodooI2C' / 'VoodooI2C' / 'Info.plist'
NAME = 'VoodooI2CPCILunarLakeSamsungController'


def matches_register(value, expressions):
    for expression in expressions.split():
        parts = expression.split('&')
        expected = int(parts[0], 16)
        mask = int(parts[1], 16) if len(parts) == 2 else 0xffffffff
        if value & mask == expected & mask:
            return True
    return False


def matches(personality, vendor, device, subvendor, subdevice):
    return matches_register((device << 16) | vendor, personality['IOPCIPrimaryMatch']) and \
        matches_register((subdevice << 16) | subvendor, personality['IOPCISecondaryMatch'])


class PatchChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = plistlib.loads(ORIGINAL.read_bytes())
        cls.patched = plistlib.loads((FOLDER / 'Info.plist').read_bytes())
        cls.personality = cls.patched['IOKitPersonalities'][NAME]
        cls.linux = (ROOT / 'tests' / '.references' / 'linux-v6.17' / 'drivers' / 'mfd' / 'intel-lpss-pci.c').read_text()

    def test_patch_preserves_all_existing_configuration(self):
        without_new = copy.deepcopy(self.patched)
        del without_new['IOKitPersonalities'][NAME]
        self.assertEqual(self.original, without_new)

    def test_documented_i2c_fixture_is_matched(self):
        snapshot = json.loads((ROOT / 'tests' / 'fixtures' / 'hardware.json').read_text(encoding='utf-8-sig'))
        ids = []
        for device in snapshot['devices']:
            if 'Serial IO I2C' not in (device.get('Name') or ''):
                continue
            value = device['HardwareID'][0]
            found = re.search(r'VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})&SUBSYS_([0-9A-F]{4})([0-9A-F]{4})', value)
            self.assertIsNotNone(found)
            vendor, devid, subdevice, subvendor = [int(x, 16) for x in found.groups()]
            self.assertTrue(matches(self.personality, vendor, devid, subvendor, subdevice))
            ids.append(devid)
        self.assertEqual(sorted(ids), [0xa878, 0xa879, 0xa87a, 0xa87b])

    def test_full_device_id_space_has_only_four_matches(self):
        found = [devid for devid in range(65536) if matches(self.personality, 0x8086, devid, 0x144d, 0xc1dd)]
        self.assertEqual(found, [0xa878, 0xa879, 0xa87a, 0xa87b])

    def test_foreign_pci_vendor_is_rejected(self):
        for vendor in (0x0000, 0x10de, 0x1022, 0x8087, 0xffff):
            self.assertFalse(matches(self.personality, vendor, 0xa878, 0x144d, 0xc1dd))

    def test_other_laptop_subsystems_are_rejected(self):
        for vendor, device in ((0x144d, 0xc1dc), (0x144d, 0xc1de), (0x1028, 0xc1dd), (0xffff, 0xffff)):
            self.assertFalse(matches(self.personality, 0x8086, 0xa878, vendor, device))

    def test_linux_descriptors_agree_for_each_controller(self):
        for device in (0xa878, 0xa879, 0xa87a, 0xa87b):
            expression = rf'PCI_VDEVICE\(INTEL, 0x{device:x}\).*?&([a-z0-9_]+)'
            found = re.search(expression, self.linux)
            self.assertIsNotNone(found)
            self.assertEqual(found.group(1), 'ehl_i2c_info')

    def test_timing_values_are_derived_from_linux_descriptor(self):
        ehl = re.search(r'struct intel_lpss_platform_info ehl_i2c_info = \{(.*?)\};', self.linux, re.S).group(1)
        clock = int(re.search(r'\.clk_rate = (\d+)', ehl).group(1))
        self.assertEqual(self.personality['I2CClkRate'], clock)
        self.assertIn('&bxt_i2c_node', ehl)
        timing = re.search(r'property_entry bxt_i2c_properties\[\] = \{(.*?)\};', self.linux, re.S).group(1)
        for linux_name, plist_name in (
            ('i2c-sda-hold-time-ns', 'I2CSdaHoldTimeNs'),
            ('i2c-sda-falling-time-ns', 'I2CSdaFallingTimeNs'),
            ('i2c-scl-falling-time-ns', 'I2CSclFallingTimeNs'),
        ):
            value = int(re.search(rf'"{linux_name}", (\d+)', timing).group(1))
            self.assertEqual(self.personality[plist_name], value)

    def test_all_timing_keys_are_consumed_by_upstream_code(self):
        src = ROOT / 'tests' / '.references' / 'VoodooI2C' / 'VoodooI2C' / 'VoodooI2C'
        constants = (src / 'VoodooI2CController' / 'VoodooI2CControllerConstants.hpp').read_text()
        driver = (src / 'VoodooI2CController' / 'VoodooI2CControllerDriver.cpp').read_text()
        for key in ('I2CClkRate', 'I2CSdaHoldTimeNs', 'I2CSdaFallingTimeNs', 'I2CSclFallingTimeNs'):
            found = re.search(rf'#define\s+(\w+)\s+"{key}"', constants)
            self.assertIsNotNone(found, key)
            self.assertIn('getNumProperty(' + found.group(1) + ')', driver)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PatchChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'test_type': 'Windows host-side source/matching checks; no macOS hardware tests',
              'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'successful': result.wasSuccessful(), 'macos_driver_loaded': False}
    (FOLDER / 'test-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
