# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Execute compiled C++ against verified upstream metadata and boundary cases."""
import ctypes as c
import json
from pathlib import Path
import random
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent
DLL = c.CDLL(str(Path(sys.argv.pop(1)).resolve()))

class Descriptor(c.Structure):
    _fields_ = [(n, c.c_uint32) for n in ('device_id', 'dma_address_bits', 'max_gt_per_tile', 'needs_scratch')]

class Version(c.Structure):
    _fields_ = [(n, c.c_uint32) for n in ('architecture', 'release', 'revision', 'version_x100')]

DLL.lnl_describe_pci.argtypes = [c.c_uint32, c.c_uint32, c.POINTER(Descriptor)]
DLL.lnl_decode_gmd.argtypes = [c.c_uint32, c.POINTER(Version)]
DLL.lnl_gmd_read_offset.argtypes = [c.c_uint32, c.c_uint64, c.POINTER(c.c_uint32)]
for fn in (DLL.lnl_describe_pci, DLL.lnl_decode_gmd, DLL.lnl_gmd_read_offset):
    fn.restype = c.c_int

LINUX = OUT / 'tests' / '.references' / 'linux-v6.17'
PCI = (LINUX / 'include/drm/intel/pciids.h').read_text(encoding='utf-8')
REGS = (LINUX / 'drivers/gpu/drm/xe/regs/xe_gt_regs.h').read_text(encoding='utf-8')
XE = (LINUX / 'drivers/gpu/drm/xe/xe_pci.c').read_text(encoding='utf-8')

class Tests(unittest.TestCase):
    def test_all_device_ids_against_linux(self):
        macro = re.search(r'#define INTEL_LNL_IDS\(MACRO__, \.\.\.\)(.*?)(?=\n\n)', PCI, re.S).group(1)
        expected = {int(v, 16) for v in re.findall(r'MACRO__\((0x[0-9A-Fa-f]+)', macro)}
        self.assertEqual(len(expected), 3)
        actual = {d for d in range(65536) if DLL.lnl_describe_pci(0x8086, d, c.byref(Descriptor()))}
        self.assertEqual(actual, expected)
        for vendor in range(65536):
            if vendor != 0x8086:
                self.assertEqual(DLL.lnl_describe_pci(vendor, 0x64a0, c.byref(Descriptor())), 0)

    def test_documented_hardware_fixture_is_recognised(self):
        hardware = json.loads((OUT / 'tests' / 'fixtures' / 'hardware.json').read_text(encoding='utf-8-sig'))
        encoded = json.dumps(hardware)
        self.assertRegex(encoded.upper(), r'VEN_8086&DEV_64A0')
        desc = Descriptor()
        self.assertEqual(DLL.lnl_describe_pci(0x8086, 0x64a0, c.byref(desc)), 1)
        self.assertEqual(desc.device_id, 0x64a0)

    def test_descriptor_matches_linux_limits(self):
        body = re.search(r'struct xe_device_desc lnl_desc = \{(.*?)\n\};', XE, re.S).group(1)
        desc = Descriptor()
        DLL.lnl_describe_pci(0x8086, 0x64a0, c.byref(desc))
        self.assertEqual(desc.dma_address_bits, int(re.search(r'dma_mask_size = (\d+)', body).group(1)))
        self.assertEqual(desc.max_gt_per_tile, int(re.search(r'max_gt_per_tile = (\d+)', body).group(1)))
        self.assertIn('.needs_scratch = true', body)
        self.assertEqual(desc.needs_scratch, 1)

    def test_random_registers_against_upstream_bitfields(self):
        masks = []
        for name in ['GMD_ID_ARCH_MASK', 'GMD_ID_RELEASE_MASK', 'GMD_ID_REVID']:
            match = re.search(name + r'\s+REG_GENMASK\((\d+), (\d+)\)', REGS)
            masks.append(tuple(map(int, match.groups())))
        rng = random.Random(20261010)
        for _ in range(20000):
            raw = rng.getrandbits(32)
            bits = f'{raw:032b}'
            expected = [int(bits[31 - hi:32 - lo], 2) for hi, lo in masks]
            result = Version()
            accepted = DLL.lnl_decode_gmd(raw, c.byref(result))
            if expected[0] == 0 or raw == 0xffffffff:
                self.assertEqual(accepted, 0)
            else:
                self.assertEqual(accepted, 1)
                self.assertEqual([result.architecture, result.release, result.revision], expected)
                self.assertEqual(result.version_x100, expected[0] * 100 + expected[1])

    def test_decoding_fixture_is_not_a_hardware_read(self):
        # Synthetic Xe2 20.4 revision 7, with reserved bits set to expose mask errors.
        raw = (20 << 22) | (4 << 14) | 0x3fc0 | 7
        value = Version()
        self.assertEqual(DLL.lnl_decode_gmd(raw, c.byref(value)), 1)
        self.assertEqual((value.architecture, value.release, value.revision, value.version_x100), (20, 4, 7, 2004))

    def test_bar_boundary_matches_upstream_offsets(self):
        graphics = int(re.search(r'#define GMD_ID\s+XE_REG\((0x[0-9a-f]+)\)', REGS).group(1), 16)
        media = graphics + int(re.search(r'MEDIA_GT_GSI_OFFSET\s+(0x[0-9a-f]+)', REGS).group(1), 16)
        for gt, offset in [(0, graphics), (1, media)]:
            for size in [0, offset, offset + 3]:
                result = c.c_uint32(0x12345678)
                self.assertEqual(DLL.lnl_gmd_read_offset(gt, size, c.byref(result)), 0)
                self.assertEqual(result.value, 0x12345678)
            for size in [offset + 4, (1 << 64) - 1]:
                result = c.c_uint32()
                self.assertEqual(DLL.lnl_gmd_read_offset(gt, size, c.byref(result)), 1)
                self.assertEqual(result.value, offset)
                self.assertEqual(result.value % 4, 0)

    def test_invalid_inputs_do_not_modify_outputs(self):
        desc = Descriptor(1, 2, 3, 4)
        for vendor, device in [(0x8086, 0), (0x10de, 0x64a0), (0x10000, 0x64a0), (0x8086, 0x10000)]:
            self.assertEqual(DLL.lnl_describe_pci(vendor, device, c.byref(desc)), 0)
            self.assertEqual(bytes(desc), bytes(Descriptor(1, 2, 3, 4)))
        version = Version(1, 2, 3, 4)
        for raw in [0, 0xffffffff, 63]:
            self.assertEqual(DLL.lnl_decode_gmd(raw, c.byref(version)), 0)
            self.assertEqual(bytes(version), bytes(Version(1, 2, 3, 4)))
        for gt in [2, 0xffffffff]:
            result = c.c_uint32(42)
            self.assertEqual(DLL.lnl_gmd_read_offset(gt, (1 << 64) - 1, c.byref(result)), 0)
            self.assertEqual(result.value, 42)

    def test_null_outputs(self):
        self.assertEqual(DLL.lnl_describe_pci(0x8086, 0x64a0, None), 0)
        self.assertEqual(DLL.lnl_decode_gmd(20 << 22, None), 0)
        self.assertEqual(DLL.lnl_gmd_read_offset(0, 1 << 32, None), 0)

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    report = {'test_type': 'Executed Windows DLL built from C++; exhaustive PCI selection, 20000 synthetic GMD fixtures, upstream comparison and BAR boundaries',
              'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'successful': result.wasSuccessful(), 'synthetic_fixtures_only_for_gmd': True,
              'actual_gpu_registers_read': False, 'metal_hardware_tests': 0}
    (ROOT / 'test-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
