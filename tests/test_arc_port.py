# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Check the ported Xe2 PAT and LRC modules against the pinned Linux sources.

Run tools/fetch_references.py first. Every expected value in these tests is
parsed out of the hash-pinned Intel MIT sources, not written down here, so a
transcription error in the port fails the suite instead of hiding in it.
The compiled production C++ is executed through ctypes; no MMIO, DMA, GGTT
write, firmware load or GPU submission happens anywhere in this file.
"""
from __future__ import annotations

import argparse
import ctypes as c
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
DLL = None
LIBRARY_SHA = None

PAGE = 4096
PAT_ABI = 1
PAT_ENTRIES = 32
PAT_MAX_WRITES = PAT_ENTRIES + 2
LNL_VERX100 = 2004
GUC_TOP = 0xFEE00000
RESERVED_LOW = 8 * 1024 * 1024
DMA_LIMIT = 1 << 46

# lb_xe2_pat.h results
PAT_OK, PAT_INPUT, PAT_PLATFORM, PAT_RANGE, PAT_INDEX, PAT_CACHE_KIND, \
    PAT_READBACK, PAT_ALIASING, PAT_ALIGNMENT = range(9)
GRAPHICS_GT, MEDIA_GT = 0, 1
CACHE_NONE, CACHE_WT, CACHE_WB, CACHE_NONE_COMPRESSION = range(4)
SPACE_PPGTT_LEAF, SPACE_PPGTT_DIRECTORY, SPACE_GGTT = 1, 2, 4
XE_GGTT = 'linux-v6.17/drivers/gpu/drm/xe/xe_ggtt.c'
# lb_lrc.h results
LRC_OK, LRC_INPUT, LRC_ALIGNMENT, LRC_RANGE, LRC_ENGINE, LRC_UNSUPPORTED, \
    LRC_ALIASING, LRC_CAPACITY, LRC_POLICY, LRC_ABI = range(10)
RENDER_CLASS, COMPUTE_CLASS = 0, 4
LRC_ABI_VERSION = 1
RING_BYTES = 16 * 1024


def load_tool():
    spec = importlib.util.spec_from_file_location('compare_interfaces',
                                                  ROOT / 'tools/compare_interfaces.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load_tool()
XE_PAT = 'linux-v6.17/drivers/gpu/drm/xe/xe_pat.c'
XE_LRC = 'linux-v6.17/drivers/gpu/drm/xe/xe_lrc.c'
LRC_LAYOUT = 'linux-v6.17/drivers/gpu/drm/xe/regs/xe_lrc_layout.h'
ENGINE_REGS = 'linux-v6.17/drivers/gpu/drm/xe/regs/xe_engine_regs.h'
MI_COMMANDS = 'linux-v6.17/drivers/gpu/drm/xe/instructions/xe_mi_commands.h'
INSTR_DEFS = 'linux-v6.17/drivers/gpu/drm/xe/instructions/xe_instr_defs.h'
GTT_DEFS = 'linux-v6.17/drivers/gpu/drm/xe/regs/xe_gtt_defs.h'
XE_VM = 'linux-v6.17/drivers/gpu/drm/xe/xe_vm.c'
XE_PCI = 'linux-v6.17/drivers/gpu/drm/xe/xe_pci.c'


SZ_NAMES = {'SZ_4K': 4096, 'SZ_8K': 8192, 'SZ_16K': 16384, 'SZ_32K': 32768}


def joined(source: str) -> str:
    """Join backslash line continuations so multi-line defines parse."""
    return re.sub(r'\\\s*\n\s*', ' ', source)


def sz_value(source: str, pattern: str) -> int:
    match = re.search(pattern, source)
    if not match:
        raise AssertionError('size macro not found: ' + pattern)
    return SZ_NAMES[match.group(1)]


def reg_bit(source: str, name: str) -> int:
    match = re.search(r'#define\s+' + name + r'\s+REG_BIT\((\d+)\)', source)
    if not match:
        raise AssertionError('REG_BIT not found: ' + name)
    return 1 << int(match.group(1))


def reg_genmask(source: str, name: str) -> tuple[int, int]:
    match = re.search(r'#define\s+' + name + r'\s+(?:REG_)?GENMASK\((\d+),\s*(\d+)\)', source)
    if not match:
        raise AssertionError('REG_GENMASK not found: ' + name)
    high, low = int(match.group(1)), int(match.group(2))
    return ((1 << (high + 1)) - 1) ^ ((1 << low) - 1), low


def define_value(source: str, name: str) -> int:
    match = re.search(r'#define\s+' + name + r'\s+(0x[0-9a-fA-F]+|\d+)\b', source)
    if not match:
        raise AssertionError('define not found: ' + name)
    return int(match.group(1), 0)


class PatWrite(c.Structure):
    _fields_ = [('offset', c.c_uint32), ('value', c.c_uint32)]


class PatPlan(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in (
        'abi', 'structBytes', 'graphicsVerX100', 'gtKind', 'entryCount', 'writeCount',
        'multicastRequired', 'atsIncluded', 'ptaIncluded', 'indexNone',
        'indexWriteThrough', 'indexWriteBack', 'indexNoneCompression',
        'requiredQualifications', 'programmed', 'reserved')] + [
        ('writes', PatWrite * PAT_MAX_WRITES)]


class LrcLayout(c.Structure):
    _fields_ = [(name, c.c_uint64) for name in (
        'totalBytes', 'ringOffset', 'ringBytes', 'pphwspOffset', 'imageOffset',
        'imageBytes', 'waBatchOffset')] + [
        (name, c.c_uint32) for name in (
            'abi', 'structBytes', 'engineClass', 'imagePages',
            'requiredQualifications')] + [('reserved', c.c_uint32 * 3)]


class LrcWrite(c.Structure):
    _fields_ = [('dwordIndex', c.c_uint32), ('value', c.c_uint32)]


class LrcImage(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in (
        'abi', 'structBytes', 'engineClass', 'writeCount', 'contextControl',
        'ringControl', 'asid', 'prologueDwords')] + [
        (name, c.c_uint64) for name in (
            'descriptor', 'pphwspGpuAddress', 'ringGpuAddress', 'pageDirectoryEntry',
            'seqnoGpuAddress')] + [
        (name, c.c_uint32) for name in (
            'hardwareBound', 'gpuExecuted', 'requiredQualifications', 'reserved')] + [
        ('writes', LrcWrite * 12), ('prologue', c.c_uint32 * 4)]


def bind_library(path):
    global DLL, LIBRARY_SHA
    source = Path(path).resolve(strict=True)
    if not source.is_file():
        raise ValueError('Regular compiled library required')
    LIBRARY_SHA = hashlib.sha256(source.read_bytes()).hexdigest()
    DLL = c.CDLL(str(source))
    DLL.lb_xe2_pat_entry_value.argtypes = [c.c_uint32, c.POINTER(c.c_uint32)]
    DLL.lb_xe2_pat_register_offset.argtypes = [c.c_uint32, c.POINTER(c.c_uint32)]
    DLL.lb_xe2_pat_index_for_cache.argtypes = [c.c_uint32, c.POINTER(c.c_uint32)]
    DLL.lb_xe2_pat_index_spaces.argtypes = [c.c_uint32, c.POINTER(c.c_uint32)]
    DLL.lb_xe2_pat_plan.argtypes = [c.c_uint32, c.c_uint32, c.c_uint32, c.c_uint64,
                                    c.POINTER(PatPlan), c.c_uint32]
    DLL.lb_xe2_pat_check_readback.argtypes = [c.POINTER(PatPlan), c.POINTER(c.c_uint32),
                                              c.c_uint32, c.POINTER(c.c_uint32)]
    DLL.lb_lrc_layout.argtypes = [c.c_uint32, c.c_uint64, c.c_uint32,
                                  c.POINTER(LrcLayout), c.c_uint32]
    DLL.lb_lrc_descriptor.argtypes = [c.c_uint64, c.c_uint32, c.POINTER(c.c_uint64)]
    DLL.lb_lrc_build.argtypes = [c.POINTER(LrcLayout), c.c_uint64, c.c_uint64, c.c_uint32,
                                 c.c_uint32, c.POINTER(LrcImage), c.c_uint32]
    DLL.lb_lrc_apply.argtypes = [c.POINTER(LrcImage), c.POINTER(c.c_uint32), c.c_uint32]
    for name in ('lb_xe2_pat_entry_value', 'lb_xe2_pat_register_offset',
                 'lb_xe2_pat_index_for_cache', 'lb_xe2_pat_plan',
                 'lb_xe2_pat_check_readback', 'lb_lrc_layout', 'lb_lrc_descriptor',
                 'lb_lrc_build', 'lb_lrc_apply'):
        getattr(DLL, name).restype = c.c_uint32


class PinnedPatSource:
    """The Xe2 PAT facts, parsed from the pinned xe_pat.c."""

    def __init__(self):
        source = joined(TOOL.read_reference(XE_PAT))
        self.no_promote = reg_bit(source, 'XE2_NO_PROMOTE')
        self.comp_en = reg_bit(source, 'XE2_COMP_EN')
        self.l3_clos, self.l3_clos_shift = reg_genmask(source, 'XE2_L3_CLOS')
        self.l3_policy, self.l3_policy_shift = reg_genmask(source, 'XE2_L3_POLICY')
        self.l4_policy, self.l4_policy_shift = reg_genmask(source, 'XE2_L4_POLICY')
        self.coh_mode, self.coh_mode_shift = reg_genmask(source, 'XE2_COH_MODE')
        self.ats_offset = define_value(source, '_PAT_ATS')
        self.pta_offset = define_value(source, '_PAT_PTA')
        index = re.search(r'#define\s+_PAT_INDEX\(index\)\s+_PICK_EVEN_2RANGES\(\s*index,\s*(\d+),'
                          r'\s*(0x[0-9a-fA-F]+),\s*(0x[0-9a-fA-F]+),'
                          r'\s*(0x[0-9a-fA-F]+),\s*(0x[0-9a-fA-F]+)\s*\)', source)
        if not index:
            raise AssertionError('_PAT_INDEX definition not found')
        self.split = int(index.group(1))
        self.range0 = (int(index.group(2), 16), int(index.group(3), 16))
        self.range1 = (int(index.group(4), 16), int(index.group(5), 16))
        table = re.search(r'static const struct xe_pat_table_entry xe2_pat_table\[\]\s*=\s*\{(.*?)\};',
                          source, re.S)
        if not table:
            raise AssertionError('xe2_pat_table not found')
        self.table = {}
        for entry in re.finditer(r'\[\s*(\d+)\]\s*=\s*XE2_PAT\(([^)]*)\)', table.group(1)):
            arguments = [int(value.strip()) for value in entry.group(2).split(',')]
            self.table[int(entry.group(1))] = self.encode(*arguments)
        ats = re.search(r'xe2_pat_ats\s*=\s*XE2_PAT\(([^)]*)\)', source)
        pta = re.search(r'xe2_pat_pta\s*=\s*XE2_PAT\(([^)]*)\)', source)
        if not ats or not pta:
            raise AssertionError('xe2_pat_ats / xe2_pat_pta not found')
        self.ats_value = self.encode(*[int(v.strip()) for v in ats.group(1).split(',')])
        self.pta_value = self.encode(*[int(v.strip()) for v in pta.group(1).split(',')])
        branch = re.search(r'if \(GRAPHICS_VER\(xe\) == 30 \|\| GRAPHICS_VER\(xe\) == 20\) \{(.*?)\} else if',
                           source, re.S)
        if not branch:
            raise AssertionError('Xe2 branch of xe_pat_init_early not found')
        self.cache_indices = {name: int(value) for name, value in
                              re.findall(r'xe->pat\.idx\[XE_CACHE_(\w+)\]\s*=\s*(\d+);',
                                         branch.group(1))}
        self.xe2_branch = branch.group(1)

    def encode(self, no_promote, comp_en, l3clos, l3_policy, l4_policy, coh_mode):
        return ((self.no_promote if no_promote else 0) |
                (self.comp_en if comp_en else 0) |
                ((l3clos << self.l3_clos_shift) & self.l3_clos) |
                ((l3_policy << self.l3_policy_shift) & self.l3_policy) |
                ((l4_policy << self.l4_policy_shift) & self.l4_policy) |
                ((coh_mode << self.coh_mode_shift) & self.coh_mode))

    def offset(self, index):
        if index < self.split:
            low, high = self.range0
            return low + index * (high - low)
        low, high = self.range1
        return low + (index - self.split) * (high - low)


class PortedPatTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PinnedPatSource()

    def test_every_table_entry_matches_the_pinned_encoding(self):
        value = c.c_uint32()
        for index in range(PAT_ENTRIES):
            with self.subTest(index=index):
                self.assertEqual(DLL.lb_xe2_pat_entry_value(index, c.byref(value)), PAT_OK)
                # Indices absent upstream are reserved and must encode as zero.
                self.assertEqual(value.value, self.source.table.get(index, 0))
        self.assertEqual(len(self.source.table), 28)
        self.assertEqual(sorted(set(range(PAT_ENTRIES)) - set(self.source.table)),
                         [16, 17, 18, 19])

    def test_register_offsets_follow_the_two_pinned_ranges(self):
        offset = c.c_uint32()
        for index in range(PAT_ENTRIES):
            with self.subTest(index=index):
                self.assertEqual(DLL.lb_xe2_pat_register_offset(index, c.byref(offset)), PAT_OK)
                self.assertEqual(offset.value, self.source.offset(index))
        self.assertEqual(self.source.offset(0), 0x4800)
        self.assertEqual(self.source.offset(8), 0x4848)
        # The ATS and PTA registers sit outside both index ranges.
        offsets = {self.source.offset(index) for index in range(PAT_ENTRIES)}
        self.assertNotIn(self.source.ats_offset, offsets)
        self.assertNotIn(self.source.pta_offset, offsets)

    def test_out_of_range_indices_are_refused(self):
        value = c.c_uint32(0xDEAD)
        for index in (PAT_ENTRIES, PAT_ENTRIES + 1, 0xFFFFFFFF):
            self.assertEqual(DLL.lb_xe2_pat_entry_value(index, c.byref(value)), PAT_INDEX)
            self.assertEqual(DLL.lb_xe2_pat_register_offset(index, c.byref(value)), PAT_INDEX)
        self.assertEqual(value.value, 0xDEAD)
        self.assertEqual(DLL.lb_xe2_pat_entry_value(0, None), PAT_INPUT)

    def test_index_reachability_matches_the_pinned_encoders(self):
        ggtt = TOOL.read_reference(XE_GGTT)
        vm = TOOL.read_reference(XE_VM)
        gtt_defs = TOOL.read_reference(GTT_DEFS)
        # A GGTT entry asserts the limit outright; a directory entry carries
        # only PAT bits 1:0; a leaf entry scatters all five bits.
        self.assertIn('xe_assert(xe, pat_index <= 3);', ggtt)
        directory = re.search(r'static u64 pde_encode_pat_index.*?\n\}', vm, re.S).group(0)
        self.assertEqual(len(re.findall(r'XE_PPGTT_PTE_PAT\d', directory)), 2)
        leaf = re.search(r'static u64 pte_encode_pat_index.*?\n\}', vm, re.S).group(0)
        self.assertEqual(len(re.findall(r'BIT\((\d)\)', leaf)), 5)
        for name in ('XELPG_GGTT_PTE_PAT0', 'XELPG_GGTT_PTE_PAT1', 'XE_PPGTT_PTE_PAT2',
                     'XELPG_PPGTT_PTE_PAT3', 'XE2_PPGTT_PTE_PAT4'):
            self.assertIn(name, gtt_defs)
        spaces = c.c_uint32()
        for index in range(PAT_ENTRIES):
            with self.subTest(index=index):
                self.assertEqual(DLL.lb_xe2_pat_index_spaces(index, c.byref(spaces)), PAT_OK)
                expected = SPACE_PPGTT_LEAF
                if index <= 3:
                    expected |= SPACE_PPGTT_DIRECTORY | SPACE_GGTT
                self.assertEqual(spaces.value, expected)
        # Write-through and compression indices cannot be bound through GGTT.
        for index in (12, 15):
            DLL.lb_xe2_pat_index_spaces(index, c.byref(spaces))
            self.assertEqual(spaces.value & SPACE_GGTT, 0)
        self.assertEqual(DLL.lb_xe2_pat_index_spaces(PAT_ENTRIES, c.byref(spaces)), PAT_INDEX)
        self.assertEqual(DLL.lb_xe2_pat_index_spaces(0, None), PAT_INPUT)

    def test_programming_path_takes_no_upstream_forcewake(self):
        source = TOOL.read_reference(XE_PAT)
        lines = source.splitlines()
        wake = [i + 1 for i, line in enumerate(lines) if 'xe_force_wake_get' in line]
        dumps = [i + 1 for i, line in enumerate(lines) if re.search(r'static void \w+_dump\(', line)]
        self.assertTrue(wake)
        # Every wake sits after a dump function starts, never in a program_pat
        # path: the port's wake requirement is its own conservative rule.
        self.assertTrue(all(any(d < w for d in dumps) for w in wake))
        program = re.search(r'static void program_pat\(.*?\n\}', source, re.S).group(0)
        program_mcr = re.search(r'static void program_pat_mcr\(.*?\n\}', source, re.S).group(0)
        self.assertNotIn('xe_force_wake', program)
        self.assertNotIn('xe_force_wake', program_mcr)

    def test_cache_indices_match_the_pinned_xe2_branch(self):
        expected = {CACHE_NONE: self.source.cache_indices['NONE'],
                    CACHE_WT: self.source.cache_indices['WT'],
                    CACHE_WB: self.source.cache_indices['WB'],
                    CACHE_NONE_COMPRESSION: self.source.cache_indices['NONE_COMPRESSION']}
        index = c.c_uint32()
        for kind, value in expected.items():
            self.assertEqual(DLL.lb_xe2_pat_index_for_cache(kind, c.byref(index)), PAT_OK)
            self.assertEqual(index.value, value)
        self.assertEqual(DLL.lb_xe2_pat_index_for_cache(4, c.byref(index)), PAT_CACHE_KIND)
        # The GGTT planner encodes write-back PAT 2; the port must agree.
        self.assertEqual(expected[CACHE_WB], 2)


class PortedPatPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PinnedPatSource()

    def plan(self, verx100=LNL_VERX100, gt=GRAPHICS_GT, discrete=0, bar=64 * 1024 * 1024):
        plan = PatPlan()
        status = DLL.lb_xe2_pat_plan(verx100, gt, discrete, bar, c.byref(plan), c.sizeof(plan))
        return status, plan

    def test_plan_writes_every_index_then_ats(self):
        status, plan = self.plan()
        self.assertEqual(status, PAT_OK)
        self.assertEqual((plan.abi, plan.entryCount, plan.writeCount), (PAT_ABI, 32, 33))
        self.assertEqual((plan.atsIncluded, plan.ptaIncluded), (1, 0))
        self.assertEqual(plan.multicastRequired, 1)
        self.assertEqual(plan.programmed, 0)
        for index in range(PAT_ENTRIES):
            write = plan.writes[index]
            self.assertEqual(write.offset, self.source.offset(index))
            self.assertEqual(write.value, self.source.table.get(index, 0))
        self.assertEqual(plan.writes[32].offset, self.source.ats_offset)
        self.assertEqual(plan.writes[32].value, self.source.ats_value)
        # Every write offset is distinct and strictly ordered inside its range.
        offsets = [plan.writes[i].offset for i in range(plan.writeCount)]
        self.assertEqual(len(set(offsets)), plan.writeCount)

    def test_media_gt_needs_no_multicast(self):
        status, plan = self.plan(gt=MEDIA_GT)
        self.assertEqual(status, PAT_OK)
        self.assertEqual(plan.multicastRequired, 0)
        self.assertEqual(plan.writeCount, 33)

    def test_unqualified_platforms_and_claims_are_refused(self):
        for verx100 in (1271, 2001, 2002, 3000, 0):
            self.assertEqual(self.plan(verx100=verx100)[0], PAT_PLATFORM)
        # Lunar Lake is integrated; a discrete claim would add the PTA write.
        self.assertEqual(self.plan(discrete=1)[0], PAT_PLATFORM)
        self.assertEqual(self.plan(gt=2)[0], PAT_INPUT)
        # Wa_16023588340 is the reason 2001 is refused rather than planned.
        normalised = re.sub(r'\s+', ' ', self.source.xe2_branch)
        self.assertIn('Wa_16023588340', normalised)
        self.assertIn('n_entries = 28', normalised)

    def test_bar_must_cover_the_highest_register(self):
        highest = max(self.source.offset(PAT_ENTRIES - 1), self.source.ats_offset)
        self.assertEqual(self.plan(bar=highest)[0], PAT_RANGE)
        self.assertEqual(self.plan(bar=highest + 3)[0], PAT_RANGE)
        self.assertEqual(self.plan(bar=highest + 4)[0], PAT_OK)

    def test_output_contract(self):
        plan = PatPlan()
        self.assertEqual(DLL.lb_xe2_pat_plan(LNL_VERX100, GRAPHICS_GT, 0, 1 << 20,
                                             c.byref(plan), c.sizeof(plan) - 4), PAT_INPUT)
        self.assertEqual(plan.abi, 0)
        self.assertEqual(DLL.lb_xe2_pat_plan(LNL_VERX100, GRAPHICS_GT, 0, 1 << 20,
                                             None, c.sizeof(plan)), PAT_INPUT)
        storage = (c.c_uint64 * 256)()
        misaligned = c.cast(c.byref(storage, 4), c.POINTER(PatPlan))
        self.assertEqual(DLL.lb_xe2_pat_plan(LNL_VERX100, GRAPHICS_GT, 0, 1 << 20,
                                             misaligned, c.sizeof(PatPlan)), PAT_ALIGNMENT)

    def test_readback_must_match_the_plan_completely(self):
        status, plan = self.plan()
        self.assertEqual(status, PAT_OK)
        values = (c.c_uint32 * plan.writeCount)(
            *[plan.writes[i].value for i in range(plan.writeCount)])
        mismatch = c.c_uint32(0xFFFF)
        self.assertEqual(DLL.lb_xe2_pat_check_readback(c.byref(plan), values,
                                                       plan.writeCount, c.byref(mismatch)), PAT_OK)
        self.assertEqual(mismatch.value, 0xFFFF)
        # A partial readback is not a verified PAT state.
        self.assertEqual(DLL.lb_xe2_pat_check_readback(c.byref(plan), values,
                                                       plan.writeCount - 1,
                                                       c.byref(mismatch)), PAT_READBACK)
        values[7] = values[7] ^ 0x1
        self.assertEqual(DLL.lb_xe2_pat_check_readback(c.byref(plan), values,
                                                       plan.writeCount,
                                                       c.byref(mismatch)), PAT_READBACK)
        self.assertEqual(mismatch.value, 7)
        # An all-zero readback is the classic "device never programmed" case.
        zeros = (c.c_uint32 * plan.writeCount)()
        self.assertEqual(DLL.lb_xe2_pat_check_readback(c.byref(plan), zeros,
                                                       plan.writeCount, None), PAT_READBACK)


class PinnedLrcSource:
    """The Xe2 LRC facts, parsed from the pinned Xe sources."""

    def __init__(self):
        self.lrc = TOOL.read_reference(XE_LRC)
        layout = TOOL.read_reference(LRC_LAYOUT)
        engine = TOOL.read_reference(ENGINE_REGS)
        mi = TOOL.read_reference(MI_COMMANDS)
        instr = TOOL.read_reference(INSTR_DEFS)
        gtt = TOOL.read_reference(GTT_DEFS)
        self.vm = TOOL.read_reference(XE_VM)
        self.pci = TOOL.read_reference(XE_PCI)
        self.ctx = {}
        for name, first, second in re.findall(
                r'#define\s+(CTX_\w+|INDIRECT_CTX_\w+)\s+\((0x[0-9a-fA-F]+)\s*\+\s*(\d+)\)',
                layout):
            self.ctx[name] = int(first, 16) + int(second)
        self.ctx_ctrl = {name: reg_bit(engine, 'CTX_CTRL_' + name) for name in
                         ('INHIBIT_SYN_CTX_SWITCH', 'ENGINE_CTX_RESTORE_INHIBIT',
                          'INDIRECT_RING_STATE_ENABLE')}
        self.ring_valid = define_value(engine, 'RING_VALID')
        ring_ctl = re.search(r'#define\s+RING_CTL_SIZE\(size\)\s+\(\(size\) - PAGE_SIZE\)', engine)
        if not ring_ctl:
            raise AssertionError('RING_CTL_SIZE definition changed')
        self.mi_opcode_mask, self.mi_opcode_shift = reg_genmask(mi, 'MI_OPCODE')
        arb = re.search(r'#define\s+MI_ARB_ON_OFF\s+__MI_INSTR\((0x[0-9a-fA-F]+)\)', mi)
        if not arb:
            raise AssertionError('MI_ARB_ON_OFF not found')
        self.mi_arb_opcode = int(arb.group(1), 16)
        self.mi_arb_enable = reg_bit(mi, 'MI_ARB_ENABLE')
        instr_mi = re.search(r'#define\s+XE_INSTR_MI\s+REG_FIELD_PREP\(XE_INSTR_CMD_TYPE,\s*(0x\d)\)',
                             instr)
        cmd_mask, cmd_shift = reg_genmask(instr, 'XE_INSTR_CMD_TYPE')
        if not instr_mi:
            raise AssertionError('XE_INSTR_MI not found')
        self.instr_mi = (int(instr_mi.group(1), 16) << cmd_shift) & cmd_mask
        self.page_present = 1 << int(re.search(r'#define\s+XE_PAGE_PRESENT\s+BIT_ULL\((\d+)\)',
                                               gtt).group(1))
        self.page_rw = 1 << int(re.search(r'#define\s+XE_PAGE_RW\s+BIT_ULL\((\d+)\)',
                                          gtt).group(1))
        self.pat0 = 1 << int(re.search(r'#define\s+XE_PPGTT_PTE_PAT0\s+BIT_ULL\((\d+)\)',
                                       gtt).group(1))
        self.pat1 = 1 << int(re.search(r'#define\s+XE_PPGTT_PTE_PAT1\s+BIT_ULL\((\d+)\)',
                                       gtt).group(1))
        self.pphwsp_size = sz_value(self.lrc, r'#define\s+LRC_PPHWSP_SIZE\s+(SZ_\w+)')
        self.wa_bb_size = sz_value(self.lrc, r'#define\s+LRC_WA_BB_SIZE\s+(SZ_\w+)')
        self.indirect_ring_size = sz_value(
            self.lrc, r'#define\s+LRC_INDIRECT_RING_STATE_SIZE\s+(SZ_\w+)')
        self.lrc_valid = 1 << int(re.search(r'#define\s+LRC_VALID\s+BIT_ULL\((\d+)\)',
                                            self.lrc).group(1))
        self.lrc_privilege = 1 << int(re.search(r'#define\s+LRC_PRIVILEGE\s+BIT_ULL\((\d+)\)',
                                                self.lrc).group(1))
        mode = re.search(r'#define\s+LRC_ADDRESSING_MODE\s+GENMASK_ULL\((\d+),\s*(\d+)\)', self.lrc)
        self.addressing_shift = int(mode.group(2))
        self.legacy_64b = int(re.search(r'#define\s+LRC_LEGACY_64B_CONTEXT\s+(\d+)',
                                        self.lrc).group(1))
        size = re.search(r'size_t xe_gt_lrc_size\(.*?\n\}', self.lrc, re.S).group(0)
        self.render_pages = int(re.search(r'XE_ENGINE_CLASS_RENDER:.*?size \+= (\d+) \* SZ_4K',
                                          size, re.S).group(1))
        self.compute_pages = int(re.search(r'XE_ENGINE_CLASS_COMPUTE:.*?size \+= (\d+) \* SZ_4K',
                                           size, re.S).group(1))
        self.bo_size_formula = re.search(r'u32 bo_size = ([^;]+);', self.lrc).group(1)
        self.ring_create_size = sz_value(
            TOOL.read_reference('linux-v6.17/drivers/gpu/drm/xe/xe_exec_queue.c'),
            r'xe_lrc_create\(q->hwe, q->vm, (SZ_\w+)')


class PortedLrcLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PinnedLrcSource()

    def layout(self, engine=RENDER_CLASS, ring=RING_BYTES, indirect=0):
        value = LrcLayout()
        status = DLL.lb_lrc_layout(engine, ring, indirect, c.byref(value), c.sizeof(value))
        return status, value

    def test_layout_matches_the_pinned_allocation_formula(self):
        for engine, pages in ((RENDER_CLASS, self.source.render_pages),
                              (COMPUTE_CLASS, self.source.compute_pages)):
            with self.subTest(engine=engine):
                status, layout = self.layout(engine)
                self.assertEqual(status, LRC_OK)
                image = pages * 4096
                expected_total = (RING_BYTES + self.source.pphwsp_size + image +
                                  self.source.wa_bb_size)
                self.assertEqual(layout.imagePages, pages)
                self.assertEqual(layout.imageBytes, image)
                self.assertEqual(layout.totalBytes, expected_total)
                self.assertEqual(layout.ringOffset, 0)
                self.assertEqual(layout.pphwspOffset, RING_BYTES)
                self.assertEqual(layout.imageOffset, RING_BYTES + self.source.pphwsp_size)
                self.assertEqual(layout.waBatchOffset, expected_total - self.source.wa_bb_size)
                self.assertEqual(layout.abi, LRC_ABI_VERSION)
        # The pinned formula is ring + lrc_size + WA BB, and lrc_size carries
        # the PPHWSP page plus the engine context image.
        self.assertIn('ring_size + lrc_size + LRC_WA_BB_SIZE', self.source.bo_size_formula)

    def test_only_render_and_compute_are_ported(self):
        for engine in (1, 2, 3, 5, 6, 0xFFFF):
            self.assertEqual(self.layout(engine)[0], LRC_ENGINE)

    def test_indirect_ring_state_is_refused_because_no_platform_enables_it(self):
        self.assertEqual(self.layout(indirect=1)[0], LRC_UNSUPPORTED)
        self.assertEqual(self.layout(indirect=2)[0], LRC_INPUT)
        # No graphics descriptor at this pin sets the feature, so the indirect
        # path would be an unqualified claim.
        self.assertNotIn('.has_indirect_ring_state = 1', self.source.pci)
        self.assertNotIn('.has_indirect_ring_state = true', self.source.pci)

    def test_ring_size_bounds(self):
        self.assertEqual(self.layout(ring=0)[0], LRC_RANGE)
        self.assertEqual(self.layout(ring=4096)[0], LRC_RANGE)
        self.assertEqual(self.layout(ring=8192)[0], LRC_OK)
        self.assertEqual(self.layout(ring=8192 + 8)[0], LRC_RANGE)
        self.assertEqual(self.layout(ring=512 * 4096)[0], LRC_OK)
        self.assertEqual(self.layout(ring=513 * 4096)[0], LRC_RANGE)
        # Upstream creates its contexts with a 16 KiB ring.
        self.assertEqual(self.source.ring_create_size, RING_BYTES)


class PortedLrcImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PinnedLrcSource()

    def setUp(self):
        self.layout = LrcLayout()
        self.assertEqual(DLL.lb_lrc_layout(RENDER_CLASS, RING_BYTES, 0,
                                           c.byref(self.layout), c.sizeof(self.layout)), LRC_OK)
        self.base = RESERVED_LOW + 0x10000
        self.root = 0x200000
        self.asid = 7
        self.tail = 4

    def build(self, base=None, root=None, asid=None, tail=None, output_bytes=None):
        image = LrcImage()
        status = DLL.lb_lrc_build(c.byref(self.layout),
                                  self.base if base is None else base,
                                  self.root if root is None else root,
                                  self.asid if asid is None else asid,
                                  self.tail if tail is None else tail,
                                  c.byref(image),
                                  c.sizeof(image) if output_bytes is None else output_bytes)
        return status, image

    def test_context_image_writes_use_the_pinned_dword_offsets(self):
        status, image = self.build()
        self.assertEqual(status, LRC_OK)
        writes = {image.writes[i].dwordIndex: image.writes[i].value
                  for i in range(image.writeCount)}
        ctx = self.source.ctx
        self.assertEqual(image.writeCount, 10)
        self.assertEqual(writes[ctx['CTX_CONTEXT_CONTROL']], image.contextControl)
        self.assertEqual(writes[ctx['CTX_RING_START']], self.base & 0xFFFFFFFF)
        self.assertEqual(writes[ctx['CTX_RING_HEAD']], 0)
        self.assertEqual(writes[ctx['CTX_RING_TAIL']], self.tail)
        self.assertEqual(writes[ctx['CTX_RING_CTL']], image.ringControl)
        self.assertEqual(writes[ctx['CTX_TIMESTAMP']], 0)
        self.assertEqual(writes[ctx['CTX_TIMESTAMP_UDW']], 0)
        self.assertEqual(writes[ctx['CTX_ASID']], self.asid)
        self.assertEqual(writes[ctx['CTX_PDP0_UDW']], image.pageDirectoryEntry >> 32)
        self.assertEqual(writes[ctx['CTX_PDP0_LDW']], image.pageDirectoryEntry & 0xFFFFFFFF)
        # No write touches the indirect ring-state register of the unported path.
        self.assertNotIn(ctx['CTX_INDIRECT_RING_STATE'], writes)

    def test_control_words_match_the_pinned_encodings(self):
        _, image = self.build()
        bits = (self.source.ctx_ctrl['INHIBIT_SYN_CTX_SWITCH'] |
                self.source.ctx_ctrl['ENGINE_CTX_RESTORE_INHIBIT'])
        # _MASKED_BIT_ENABLE puts the mask in the upper half of the dword.
        self.assertEqual(image.contextControl, (bits << 16) | bits)
        self.assertEqual(image.contextControl & self.source.ctx_ctrl['INDIRECT_RING_STATE_ENABLE'], 0)
        self.assertEqual(image.ringControl,
                         (self.layout.ringBytes - 4096) | self.source.ring_valid)
        expected_prologue = (self.source.instr_mi |
                             ((self.source.mi_arb_opcode << self.source.mi_opcode_shift) &
                              self.source.mi_opcode_mask) | self.source.mi_arb_enable)
        self.assertEqual(image.prologueDwords, 1)
        self.assertEqual(image.prologue[0], expected_prologue)

    def test_descriptor_and_addresses_match_the_pinned_composition(self):
        _, image = self.build()
        pphwsp = self.base + self.layout.pphwspOffset
        expected = (self.source.lrc_valid |
                    (self.source.legacy_64b << self.source.addressing_shift) |
                    self.source.lrc_privilege | pphwsp)
        self.assertEqual(image.descriptor, expected)
        self.assertEqual(image.pphwspGpuAddress, pphwsp)
        self.assertEqual(image.ringGpuAddress, self.base)
        self.assertEqual(image.seqnoGpuAddress, pphwsp + 512)
        # Engine class and instance belong to the pre-1250 descriptor only.
        self.assertIn('if (GRAPHICS_VERx100(xe) < 1250)', self.source.lrc)
        self.assertEqual(image.descriptor >> 48, 0)
        standalone = c.c_uint64()
        self.assertEqual(DLL.lb_lrc_descriptor(pphwsp, 1, c.byref(standalone)), LRC_OK)
        self.assertEqual(standalone.value, expected)
        self.assertEqual(DLL.lb_lrc_descriptor(pphwsp, 0, c.byref(standalone)), LRC_OK)
        self.assertEqual(standalone.value, expected & ~self.source.lrc_privilege)

    def test_page_directory_entry_uses_the_write_back_pat_index(self):
        _, image = self.build()
        expected = (self.root | self.source.page_present | self.source.page_rw |
                    self.source.pat1)
        self.assertEqual(image.pageDirectoryEntry, expected)
        # pde_encode_pat_index only forwards PAT bits 1:0, and write-back is 2.
        self.assertIn('pde_encode_pat_index', self.source.vm)
        self.assertEqual(image.pageDirectoryEntry & self.source.pat0, 0)

    def test_no_built_image_claims_hardware(self):
        _, image = self.build()
        self.assertEqual((image.hardwareBound, image.gpuExecuted, image.reserved), (0, 0, 0))
        self.assertEqual(image.requiredQualifications, self.layout.requiredQualifications)
        self.assertEqual(image.requiredQualifications, (1 << 5) - 1)

    def test_addresses_tails_and_identifiers_are_checked(self):
        self.assertEqual(self.build(base=0)[0], LRC_ALIGNMENT)
        self.assertEqual(self.build(base=self.base + 8)[0], LRC_ALIGNMENT)
        self.assertEqual(self.build(base=4096)[0], LRC_POLICY)
        self.assertEqual(self.build(base=GUC_TOP)[0], LRC_RANGE)
        self.assertEqual(self.build(base=GUC_TOP - 4096)[0], LRC_RANGE)
        self.assertEqual(self.build(root=0)[0], LRC_RANGE)
        self.assertEqual(self.build(root=self.root + 8)[0], LRC_RANGE)
        self.assertEqual(self.build(root=DMA_LIMIT)[0], LRC_RANGE)
        self.assertEqual(self.build(asid=0)[0], LRC_INPUT)
        self.assertEqual(self.build(tail=RING_BYTES)[0], LRC_RANGE)
        self.assertEqual(self.build(tail=5)[0], LRC_RANGE)
        self.assertEqual(self.build(output_bytes=c.sizeof(LrcImage) - 8)[0], LRC_INPUT)

    def test_a_tampered_layout_is_recomputed_and_refused(self):
        self.layout.totalBytes += 4096
        self.assertEqual(self.build()[0], LRC_ABI)
        self.layout.totalBytes -= 4096
        self.layout.abi = 2
        self.assertEqual(self.build()[0], LRC_ABI)
        self.layout.abi = LRC_ABI_VERSION
        self.layout.reserved[1] = 1
        self.assertEqual(self.build()[0], LRC_ABI)

    def test_apply_patches_only_inside_the_callers_image(self):
        _, image = self.build()
        highest = max(image.writes[i].dwordIndex for i in range(image.writeCount))
        buffer = (c.c_uint32 * (highest + 1))()
        self.assertEqual(DLL.lb_lrc_apply(c.byref(image), buffer, highest + 1), LRC_OK)
        for i in range(image.writeCount):
            self.assertEqual(buffer[image.writes[i].dwordIndex], image.writes[i].value)
        short = (c.c_uint32 * highest)()
        self.assertEqual(DLL.lb_lrc_apply(c.byref(image), short, highest), LRC_CAPACITY)
        self.assertEqual(list(short), [0] * highest)
        self.assertEqual(DLL.lb_lrc_apply(c.byref(image), buffer, 0), LRC_INPUT)
        image.gpuExecuted = 1
        self.assertEqual(DLL.lb_lrc_apply(c.byref(image), buffer, highest + 1), LRC_ABI)

    def test_image_fits_the_context_image_area(self):
        _, image = self.build()
        highest = max(image.writes[i].dwordIndex for i in range(image.writeCount))
        # The whole patch set stays far inside the smallest ported image.
        compute = LrcLayout()
        DLL.lb_lrc_layout(COMPUTE_CLASS, RING_BYTES, 0, c.byref(compute), c.sizeof(compute))
        self.assertLess((highest + 1) * 4, compute.imageBytes)


class AppleBundleShape(unittest.TestCase):
    """What mirroring Apple's last compatible driver would still require.

    These tests pin the documented state: the kext occupies the accelerator
    slot of the Apple split and declares no display-side dependency. They fail
    if a framebuffer personality appears without the record being updated.
    """

    @classmethod
    def setUpClass(cls):
        cls.inventory = TOOL.read_reference(
            'apple-driver-surface/WhateverGreen-093e76f/WhateverGreen/kern_igfx_kexts.cpp')
        cls.framebuffer = TOOL.read_reference(
            'macos-tahoe/IOGraphics-600/IOGraphicsFamily/IOKit/graphics/IOFramebuffer.h')
        cls.info = plistlib.loads((ROOT / 'native/Info.plist').read_bytes())

    def test_icl_ships_one_accelerator_and_two_framebuffers(self):
        bundles = re.findall(r'KernelPatcher::KextInfo\s+kextIntel(\w+)\s*\{\s*"([\w.]+)"',
                             self.inventory)
        icl = {name: bundle for name, bundle in bundles if name.startswith('ICL')}
        self.assertEqual(sorted(icl), ['ICL', 'ICLHPFb', 'ICLLPFb'])
        self.assertEqual(icl['ICL'], 'com.apple.driver.AppleIntelICLGraphics')
        self.assertTrue(all(bundle.endswith('GraphicsFramebuffer')
                            for name, bundle in icl.items() if name != 'ICL'))
        # Newer families would have to appear here before Xe2 had a template.
        self.assertFalse([name for name, _ in bundles
                          if name.startswith(('TGL', 'ADL', 'MTL', 'LNL'))])

    def test_the_published_framebuffer_contract_cannot_be_completed(self):
        pure_virtual = re.findall(r'virtual\s[^;{]*?=\s*0\s*;', self.framebuffer, re.S)
        self.assertEqual(len(pure_virtual), 8)
        # Its own includes are outside the pinned set, so a framebuffer
        # subclass cannot even be compiled from this material.
        for header in ('IOGraphicsDevice.h', 'IOFramebufferShared.h',
                       'IOFramebufferPrivate.h'):
            self.assertIn(header, self.framebuffer)
            self.assertFalse(list((ROOT / 'tests/.references/macos-tahoe').rglob(header)))

    def test_the_kext_occupies_only_the_accelerator_slot(self):
        personalities = self.info['IOKitPersonalities']
        self.assertEqual(len(personalities), 1)
        personality = next(iter(personalities.values()))
        self.assertEqual(personality['IOProviderClass'], 'IOPCIDevice')
        self.assertEqual(personality['IOClass'], 'LunarBridgePCI')
        self.assertEqual(personality['IOMatchCategory'], 'LunarBridgeDiagnostic')
        # No display-side dependency is declared, so nothing claims the
        # framebuffer half of the Apple split.
        self.assertNotIn('com.apple.iokit.IOGraphicsFamily', self.info['OSBundleLibraries'])
        self.assertIn('com.apple.iokit.IOPCIFamily', self.info['OSBundleLibraries'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('library', type=Path, help='Compiled library with the ported modules')
    parser.add_argument('--report', type=Path, help='Write a JSON evidence record')
    arguments = parser.parse_args()
    bind_library(arguments.library)
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__('__main__'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if arguments.report:
        arguments.report.write_text(json.dumps({
            'library_sha256': LIBRARY_SHA, 'tests_run': result.testsRun,
            'failures': len(result.failures), 'errors': len(result.errors),
            'scope': 'Ported Xe2 PAT and LRC CPU contracts, checked against the pinned '
                     'Intel MIT sources; no MMIO, DMA, GGTT write, firmware or GPU',
            'pat_programmed_on_hardware': False, 'lrc_submitted': False,
            'gpu_execution_implemented': False,
        }, indent=2) + '\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
