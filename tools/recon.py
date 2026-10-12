# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""LunarBridge recon: collect every checkable fact about this port in one run.

Eight stages, each of which either runs or records why it did not. Nothing is
written outside the chosen output directory, no system setting is changed, and
no Apple binary is copied: the Apple stage reads bundles where they are and
keeps names, counts and hashes only.

    environment  toolchain, OS, SIP and boot-args state
    repository   commit, dirty files, pinned-reference hashes, build, tests
    interface    the Xe uAPI versus the macOS contract, compiled table verified
    portplan     what the ported PAT and LRC modules would program
    extract      the upstream hardware facts, re-derived with citations
    appledriver  the installed Apple graphics drivers and their interfaces
    system       the live graphics stack: families, loaded kexts, displays
    device       the Arc 130V itself and this project's own driver on it

The run ends in a ranked findings list: what is established, what is blocked,
and which single next step each blocker needs. A finding never claims hardware
state that was not measured in this run.

    python3 tools/recon.py --out build/recon
    python3 tools/recon.py --out build/recon --stages environment,repository
    python3 tools/recon.py --out build/recon --kext-root /System/Library/Extensions
"""
from __future__ import annotations

import argparse
import ctypes as c
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
STAGES = ('environment', 'repository', 'interface', 'portplan', 'extract',
          'appledriver', 'system', 'device')
# Read-only commands only. Nothing in this list changes system state.
MACOS_PROBES = {
    'sw_vers': ['sw_vers'],
    'sip_status': ['csrutil', 'status'],
    'boot_args': ['nvram', 'boot-args'],
    'loaded_kexts': ['kextstat', '-l'],
    'displays': ['system_profiler', '-json', 'SPDisplaysDataType'],
}
# Apple graphics bundles worth mapping, newest Intel generation first.
KEXT_CANDIDATES = (
    'AppleIntelICLGraphics.kext', 'AppleIntelICLLPGraphicsFramebuffer.kext',
    'AppleIntelICLHPGraphicsFramebuffer.kext', 'AppleIntelKBLGraphics.kext',
    'AppleIntelCFLGraphicsFramebuffer.kext', 'IOGPUFamily.kext',
    'IOAcceleratorFamily2.kext', 'IOGraphicsFamily.kext', 'IOSurface.kext',
)
# The CPU modules the interface and port suites link against.
LIBRARY_SOURCES = ('gpu-prototype/lb_gpu_memory.cpp', 'gpu-prototype/lb_ggtt_plan.cpp',
                   'gpu-prototype/lb_guc_transport.cpp', 'gpu-prototype/lb_guc_scheduler.cpp',
                   'gpu-prototype/lb_arc_interface.cpp', 'gpu-prototype/lb_xe2_pat.cpp',
                   'gpu-prototype/lb_lrc.cpp')
# The host-test library: the same modules plus the IOKit-facing code behind its
# fake adapters. Mirrors gpu-prototype/build_gpu.py without the Windows entry.
HOST_SOURCES = ('gpu-prototype/lnl_platform.cpp', 'gpu-prototype/lb_diagnostics.cpp',
                'gpu-prototype/lb_gpu_memory.cpp', 'gpu-prototype/lb_ggtt_plan.cpp',
                'gpu-prototype/lb_xe2_pat.cpp', 'gpu-prototype/lb_lrc.cpp',
                'gpu-prototype/lb_arc_interface.cpp', 'gpu-prototype/lb_guc_transport.cpp',
                'gpu-prototype/lb_guc_scheduler.cpp', 'gpu-prototype/lb_guc_firmware.cpp',
                'gpu-prototype/lb_forcewake.cpp', 'native/LBDMAContext.cpp',
                'native/LBDMAQuarantine.cpp', 'tests/dma_adapter_fake.cpp',
                'native/LBForceWakeAdapter.cpp', 'tests/forcewake_adapter_fake.cpp')
HOST_DEFINES = ('-DLB_DMA_HOST_TEST', '-DLB_FORCEWAKE_HOST_TEST')
# The variables the host suites read to find a library built outside build/host.
HOST_LIBRARY_ENVIRONMENT = ('LB_HOST_LIBRARY', 'LB_FORCEWAKE_LIBRARY',
                            'LUNARBRIDGE_FORCEWAKE_TEST_LIBRARY')
# (name, script, which library it needs and how it takes it)
TEST_SUITES = (
    ('arc_interface', 'tests/test_arc_interface.py', 'modules:positional'),
    ('arc_port', 'tests/test_arc_port.py', 'modules:positional'),
    ('interface_diff', 'tests/test_interface_diff.py', 'modules:positional'),
    ('guc_scheduler', 'tests/test_guc_scheduler.py', 'modules:positional'),
    ('ggtt_plan', 'tests/test_ggtt_plan.py', 'modules:--library'),
    ('forcewake', 'tests/test_forcewake.py', 'host:environment'),
    ('forcewake_adapter', 'tests/test_forcewake_adapter.py', 'host:environment'),
    ('dma_adapter', 'tests/test_dma_adapter.py', 'host:environment'),
    ('dma_quarantine', 'tests/test_dma_quarantine.py', 'host:environment'),
    ('apple_driver_analysis', 'tests/test_apple_driver_analysis.py', None),
    ('abi_policy', 'tests/test_abi_policy.py', None),
    ('abi_dispatch', 'tests/test_abi_dispatch.py', None),
    ('abi_layout_vs_clang', 'tests/test_abi_layout_against_clang.py', None),
    ('abi_callsite', 'tests/test_abi_callsite.py', None),
    ('abi_correlation', 'tests/test_abi_correlation.py', None),
    ('platform_collector', 'tests/test_collect_platform.py', None),
    ('graphics_inventory', 'tests/test_macos_graphics_inventory.py', None),
    ('installer', 'tests/test_installer.py', None),
    ('kernel_build_contract', 'tests/test_kernel_build_contract.py', None),
    ('mesa_cpu', 'tests/test_mesa_cpu.py', None),
    ('metal_shader', 'tests/test_metal_shader.py', None),
    ('recon', 'tests/test_recon.py', None),
)
TARGET_DEVICE = {'vendor': '0x8086', 'device': '0x64a0',
                 'subsystem': '0x144dc1dd', 'name': 'Intel Arc 130V (Lunar Lake)'}
OWN_BUNDLE_ID = 'de.nordapps.LunarBridge'
NESTED_MARKER = 'LB_RECON_IN_SUITE'
# Which code in this repository stands behind each extracted group, and what is
# still missing in it. An empty gap means the group needs nothing further here.
PORT_COVERAGE = {
    'device': (('native/LunarBridgeDriver.cpp', 'gpu-prototype/LunarLakePCIProbe.cpp'), ''),
    'pat': (('gpu-prototype/lb_xe2_pat.cpp',),
            'the plan is never written: no readback from a loaded driver'),
    'lrc': (('gpu-prototype/lb_lrc.cpp',),
            'the context image is built in RAM and never submitted'),
    'pagetables': (('gpu-prototype/lb_ggtt_plan.cpp',),
                   'GGTT entries are planned; no 4-level PPGTT root is built'),
    'forcewake': (('gpu-prototype/lb_forcewake.cpp', 'native/LBForceWakeAdapter.cpp'),
                  'the handshake stays behind the opt-in probe and is unqualified'),
    'guc': (('gpu-prototype/lb_guc_transport.cpp', 'gpu-prototype/lb_guc_scheduler.cpp',
             'gpu-prototype/lb_guc_firmware.cpp'),
            'transport codec and scheduler state only; no firmware load sequence'),
    'userclient': (('native/LunarBridgeDriver.cpp', 'gpu-prototype/lb_arc_interface.cpp',
                    'tools/abi_dispatch.py', 'tools/abi_callsite.py'),
                   'our own selector table satisfies the published validation contract; the '
                   'recovered Apple tables are not yet compared against it'),
    'metaclass': (('native/LunarBridgeDriver.cpp', 'tools/abi_macho.py'),
                  'the class and vtable contract an accelerator must satisfy is readable but '
                  'not yet implemented against'),
}
XE = 'linux-v6.17/drivers/gpu/drm/xe/'
# The upstream facts this port stands on, each with the pattern that proves it.
# A rule that stops matching is drift in the pinned source, reported as such.
# (group, name, reference, pattern, kind)
EXTRACTION_RULES = (
    ('device', 'lunarlake_pci_ids', 'linux-v6.17/include/drm/intel/pciids.h',
     r'#define INTEL_LNL_IDS\(MACRO__, \.\.\.\)((?:[^\n]*\\\n)*[^\n]*)', 'hexlist'),
    ('device', 'graphics_verx100', XE + 'xe_pci.c', r'\{\s*(\d+),\s*"Xe2_LPG"', 'int'),
    ('device', 'xe2_va_bits', XE + 'xe_pci.c',
     r'#define XE2_GFX_FEATURES(?:.|\n)*?\.va_bits = (\d+)', 'int'),
    ('device', 'xe2_vm_max_level', XE + 'xe_pci.c',
     r'#define XE2_GFX_FEATURES(?:.|\n)*?\.vm_max_level = (\d+)', 'int'),
    ('device', 'lnl_dma_mask_size', XE + 'xe_pci.c',
     r'lnl_desc = \{(?:.|\n)*?\.dma_mask_size = (\d+)', 'int'),
    ('device', 'lnl_needs_scratch', XE + 'xe_pci.c',
     r'lnl_desc = \{(?:.|\n)*?\.needs_scratch = (\w+)', 'text'),
    ('device', 'lnl_max_gt_per_tile', XE + 'xe_pci.c',
     r'lnl_desc = \{(?:.|\n)*?\.max_gt_per_tile = (\d+)', 'int'),

    ('pagetables', 'ggtt_pat_index_max', XE + 'xe_ggtt.c',
     r'xe_assert\(xe, pat_index <= (\d+)\)', 'int'),
    ('pagetables', 'page_present_bit', XE + 'regs/xe_gtt_defs.h',
     r'#define XE_PAGE_PRESENT\s+BIT_ULL\((\d+)\)', 'int'),
    ('pagetables', 'page_rw_bit', XE + 'regs/xe_gtt_defs.h',
     r'#define XE_PAGE_RW\s+BIT_ULL\((\d+)\)', 'int'),
    ('pagetables', 'ppgtt_pte_pat0_bit', XE + 'regs/xe_gtt_defs.h',
     r'#define XE_PPGTT_PTE_PAT0\s+BIT_ULL\((\d+)\)', 'int'),
    ('pagetables', 'ppgtt_pte_pat1_bit', XE + 'regs/xe_gtt_defs.h',
     r'#define XE_PPGTT_PTE_PAT1\s+BIT_ULL\((\d+)\)', 'int'),
    ('pagetables', 'ppgtt_pte_pat2_bit', XE + 'regs/xe_gtt_defs.h',
     r'#define XE_PPGTT_PTE_PAT2\s+BIT_ULL\((\d+)\)', 'int'),

    ('forcewake', 'ack_timeout_ms', XE + 'xe_force_wake.c',
     r'#define XE_FORCE_WAKE_ACK_TIMEOUT_MS\s+(\d+)', 'int'),
    ('forcewake', 'gt_wake_register', XE + 'xe_force_wake.c',
     r'init_domain\(fw, XE_FW_DOMAIN_ID_GT,\s*\n\s*(FORCEWAKE_\w+),', 'text'),
    # Lunar Lake is 2004, so the GT domain acknowledges through the MTL register.
    ('forcewake', 'gt_ack_register_selector', XE + 'xe_force_wake.c',
     r'if \(xe->info\.graphics_verx100 >= (\d+)\) \{\s*\n\s*init_domain\([^;]*?'
     r'\n\s*(FORCEWAKE_ACK_\w+)\);', 'pair'),

    ('guc', 'status_register', XE + 'regs/xe_guc_regs.h',
     r'#define GUC_STATUS\s+XE_REG\((0x[0-9a-fA-F]+)\)', 'hex'),
    ('guc', 'ukernel_status_mask', XE + 'regs/xe_guc_regs.h',
     r'#define\s+GS_UKERNEL_MASK\s+REG_GENMASK\((\d+), (\d+)\)', 'range'),
    ('guc', 'bootrom_status_mask', XE + 'regs/xe_guc_regs.h',
     r'#define\s+GS_BOOTROM_MASK\s+REG_GENMASK\((\d+), (\d+)\)', 'range'),
    ('guc', 'soft_scratch_base', XE + 'regs/xe_guc_regs.h',
     r'#define SOFT_SCRATCH\(n\)\s+XE_REG\((0x[0-9a-fA-F]+)', 'hex'),
    ('guc', 'soft_scratch_count', XE + 'regs/xe_guc_regs.h',
     r'#define SOFT_SCRATCH_COUNT\s+(\d+)', 'int'),
    ('guc', 'rsa_scratch_base', XE + 'regs/xe_guc_regs.h',
     r'#define UOS_RSA_SCRATCH\(i\)\s+XE_REG\((0x[0-9a-fA-F]+)', 'hex'),
    ('guc', 'rsa_scratch_count', XE + 'regs/xe_guc_regs.h',
     r'#define UOS_RSA_SCRATCH_COUNT\s+(\d+)', 'int'),
    ('guc', 'dma_addr_low_register', XE + 'regs/xe_guc_regs.h',
     r'#define DMA_ADDR_0_LOW\s+XE_REG\((0x[0-9a-fA-F]+)\)', 'hex'),
    ('guc', 'dma_copy_size_register', XE + 'regs/xe_guc_regs.h',
     r'#define DMA_COPY_SIZE\s+XE_REG\((0x[0-9a-fA-F]+)\)', 'hex'),
    ('guc', 'dma_control_register', XE + 'regs/xe_guc_regs.h',
     r'#define DMA_CTRL\s+XE_REG\((0x[0-9a-fA-F]+)\)', 'hex'),
    ('guc', 'start_dma_bit', XE + 'regs/xe_guc_regs.h',
     r'#define\s+START_DMA\s+REG_BIT\((\d+)\)', 'int'),
    ('guc', 'load_time_warn_ms', XE + 'xe_guc.c',
     r'#define GUC_LOAD_TIME_WARN_MS\s+(\d+)', 'int'),
    ('guc', 'lunarlake_guc_version', XE + 'xe_uc_fw.c',
     r'fw_def\(LUNARLAKE,\s+GT_TYPE_ANY,\s+major_ver\(xe,\s+guc,\s+lnl,\s+'
     r'(\d+), (\d+), (\d+)\)', 'version'),
    ('guc', 'ctb_header_dwords', XE + 'abi/guc_communication_ctb_abi.h',
     r'#define GUC_CTB_HDR_LEN\s+(\d+)u', 'int'),
    ('guc', 'ctb_max_dwords', XE + 'abi/guc_communication_ctb_abi.h',
     r'#define\s+GUC_CTB_MAX_DWORDS\s+(\d+)', 'int'),
)


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(command: list[str], timeout: int = 60, cwd: Path | None = None) -> dict:
    """Run one read-only command and record what it said."""
    binary = shutil.which(command[0])
    if not binary:
        return {'performed': False, 'reason': 'not found: ' + command[0]}
    try:
        result = subprocess.run([binary, *command[1:]], capture_output=True, text=True,
                                timeout=timeout, check=False, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as error:
        return {'performed': False, 'reason': type(error).__name__ + ': ' + str(error)[:120]}
    return {'performed': True, 'command': command, 'exit_code': result.returncode,
            'stdout': result.stdout[:20000], 'stderr': result.stderr[:2000]}


def stage_environment() -> dict:
    tools = {}
    for name in ('clang++', 'gcc', 'python3', 'nm', 'otool', 'xcrun', 'kextstat',
                 'ioreg', 'system_profiler', 'csrutil', 'git', 'zig'):
        path = shutil.which(name)
        tools[name] = path or None
    record = {
        'system': platform.system(), 'release': platform.release(),
        'machine': platform.machine(), 'python': platform.python_version(),
        'macos_version': platform.mac_ver()[0], 'tools': tools,
        'is_macos': platform.system() == 'Darwin',
    }
    if record['is_macos']:
        for name in ('sw_vers', 'sip_status', 'boot_args'):
            record[name] = probe(MACOS_PROBES[name])
    else:
        record['macos_probes'] = {'performed': False,
                                  'reason': 'not running on macOS; run this stage on the target'}
    return record


def build_libraries(out: Path, build: bool) -> dict:
    """Build the two libraries every later stage reads: CPU modules and host test."""
    libraries = {'modules': {'built': False}, 'host': {'built': False}}
    if not build:
        for entry in libraries.values():
            entry['reason'] = 'build not requested'
        return libraries
    compiler = shutil.which('clang++') or shutil.which('g++')
    if not compiler:
        for entry in libraries.values():
            entry['reason'] = 'no C++ compiler found'
        return libraries
    for key, sources, defines in (('modules', LIBRARY_SOURCES, ()),
                                  ('host', HOST_SOURCES, HOST_DEFINES)):
        libraries[key] = build_library(out, key, compiler, sources, defines)
    return libraries


def stage_repository() -> dict:
    record: dict = {}
    for key, command in (('commit', ['git', 'rev-parse', 'HEAD']),
                         ('branch', ['git', 'rev-parse', '--abbrev-ref', 'HEAD']),
                         ('dirty', ['git', 'status', '--porcelain'])):
        # Always the repository this script lives in, not the caller's directory.
        result = probe(command, cwd=ROOT)
        record[key] = result.get('stdout', '').strip() if result.get('performed') else None
    manifest = json.loads((ROOT / 'tests/reference-manifest.json').read_text(encoding='utf-8'))
    verified, missing, mismatched = 0, [], []
    for entry in manifest['files']:
        path = ROOT / 'tests/.references' / entry['path']
        if not path.is_file():
            missing.append(entry['path'])
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256']:
            verified += 1
        else:
            mismatched.append(entry['path'])
    record['pinned_references'] = {'total': len(manifest['files']), 'verified': verified,
                                   'missing': missing, 'mismatched': mismatched}
    return record


def build_library(out: Path, name: str, compiler: str, sources: tuple,
                  defines: tuple) -> dict:
    """Build one shared library from repository sources, nothing else."""
    suffix = '.dylib' if platform.system() == 'Darwin' else '.so'
    library = out / ('lib' + name + suffix)
    command = [compiler, '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
               '-fno-exceptions', '-fno-rtti', '-fPIC',
               '-dynamiclib' if platform.system() == 'Darwin' else '-shared', *defines,
               *[str(ROOT / source) for source in sources], '-o', str(library)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {'built': False, 'reason': type(error).__name__ + ': ' + str(error)[:160]}
    record = {'built': result.returncode == 0, 'compiler': compiler,
              'exit_code': result.returncode, 'source_count': len(sources),
              'path': str(library) if result.returncode == 0 else None,
              'stderr': result.stderr[:4000]}
    if result.returncode == 0:
        record['sha256'] = hashlib.sha256(library.read_bytes()).hexdigest()
    return record


def stage_tests(libraries: dict) -> dict:
    # tests/test_recon.py exercises this function, and this stage runs that
    # suite, so a run inside a suite must not start another one.
    if os.environ.get(NESTED_MARKER) == '1':
        return {name: {'performed': False, 'reason': 'nested run inside a suite'}
                for name, _, _ in TEST_SUITES}
    suites = {}
    for name, script, requirement in TEST_SUITES:
        kind, _, how = (requirement or '').partition(':')
        library = (libraries.get(kind) or {}).get('path') if kind else None
        if kind and not library:
            suites[name] = {'performed': False,
                            'reason': 'the ' + kind + ' library was not built'}
            continue
        arguments = [sys.executable, '-B', str(ROOT / script)]
        environment = {**os.environ, NESTED_MARKER: '1'}
        if how == 'positional':
            arguments.append(library)
        elif how == '--library':
            arguments += ['--library', library]
        elif how == 'environment':
            # Point the host suites at this build instead of the Windows output.
            environment.update({name: library for name in HOST_LIBRARY_ENVIRONMENT})
        try:
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=1800,
                                    check=False, cwd=ROOT, env=environment)
        except (OSError, subprocess.SubprocessError) as error:
            suites[name] = {'performed': False,
                            'reason': type(error).__name__ + ': ' + str(error)[:160]}
            continue
        output = (result.stderr or result.stdout).strip().splitlines()
        suites[name] = {'performed': True, 'exit_code': result.returncode,
                        'passed': result.returncode == 0, 'library': kind or None,
                        'tail': output[-3:]}
    return suites


def stage_interface(library: str | None, out: Path) -> dict:
    tool = load('compare_interfaces', 'tools/compare_interfaces.py')
    record: dict = {}
    try:
        linux = tool.extract_linux()
        macos = tool.extract_macos()
        apple = tool.extract_apple_driver_surface()
    except Exception as error:  # extraction errors are findings, not crashes
        return {'performed': False, 'reason': type(error).__name__ + ': ' + str(error)[:200]}
    record['performed'] = True
    record['linux_ioctls'] = linux['ioctl_count']
    record['linux_permission_model'] = linux['permission_model']
    record['macos_scalar_limit'] = macos['scalar_input_count_max']
    record['macos_dispatch_fields'] = macos['dispatch_check_fields']
    record['macos_dispatch_2022'] = macos['dispatch_2022_extra_fields']
    record['apple_intel_kexts'] = apple['kext_count']
    record['apple_newest_generation'] = apple['newest_generation']
    record['apple_xe_or_xe2_driver_present'] = apple['xe_or_xe2_driver_present']
    record['operation_mapping'] = tool.mapping_from_sources(linux)
    if library:
        try:
            verification = tool.verify_driver_table(Path(library), linux, macos)
            record['driver_table'] = {
                'operation_count': verification['operation_count'],
                'mismatches': verification['mismatches'],
                'gpu_execution_implemented': verification['gpu_execution_implemented'],
                'metal_provider_implemented': verification['metal_provider_implemented'],
            }
        except Exception as error:
            record['driver_table'] = {'performed': False,
                                      'reason': type(error).__name__ + ': ' + str(error)[:200]}
    (out / 'interface-extraction.json').write_text(json.dumps(record, indent=2) + '\n',
                                                   encoding='utf-8')
    return record


def stage_portplan(library: str | None) -> dict:
    """Ask the compiled port what it would program, and decode it as the kext does."""
    if not library:
        return {'performed': False, 'reason': 'library not built'}
    # The wire module owns the layout; re-declaring it here would invite drift.
    wire = load('lb_wire', 'tools/lb_wire.py')
    module = c.CDLL(library)
    module.lb_arc_interface_port_report.argtypes = [c.POINTER(wire.ArcPortReport), c.c_uint32]
    module.lb_arc_interface_port_report.restype = c.c_uint32
    report = wire.ArcPortReport()
    status = module.lb_arc_interface_port_report(c.byref(report), c.sizeof(report))
    if status:
        return {'performed': False, 'reason': 'port report refused with status ' + str(status)}
    try:
        decoded = wire.decode_arc_port_report(bytes(report))
    except ValueError as error:
        return {'performed': False, 'reason': 'port report rejected: ' + str(error)}
    # Every number here is CPU state computed in this process; say so next to it.
    decoded['measured_on_hardware'] = False
    return {'performed': True, 'plan': decoded}


def convert(kind: str, groups: tuple):
    if kind == 'int':
        return int(groups[0])
    if kind == 'hex':
        return int(groups[0], 16)
    if kind == 'range':
        return {'high_bit': int(groups[0]), 'low_bit': int(groups[1]),
                'mask': ((1 << (int(groups[0]) - int(groups[1]) + 1)) - 1) << int(groups[1])}
    if kind == 'pair':
        return {'threshold': int(groups[0]), 'register': groups[1]}
    if kind == 'version':
        return '.'.join(groups)
    if kind == 'hexlist':
        return ['0x' + value.lower() for value in re.findall(r'0x([0-9A-Fa-f]{4})', groups[0])]
    if kind != 'text':
        raise ValueError('unknown extraction kind: ' + kind)
    return groups[0].strip()


def stage_extract(out: Path) -> dict:
    """Re-derive the upstream facts this port rests on, each with its citation."""
    tool = load('compare_interfaces', 'tools/compare_interfaces.py')
    facts: dict = {}
    drift: list[dict] = []
    for group, name, reference, pattern, kind in EXTRACTION_RULES:
        try:
            source = tool.read_reference(reference)
        except Exception as error:
            drift.append({'group': group, 'name': name, 'reference': reference,
                          'reason': type(error).__name__ + ': ' + str(error)[:160]})
            continue
        match = re.search(pattern, source)
        if not match:
            drift.append({'group': group, 'name': name, 'reference': reference,
                          'reason': 'pattern no longer matches the pinned source'})
            continue
        value = convert(kind, match.groups())
        facts.setdefault(group, {})[name] = {
            'value': value, **({'hex': hex(value)} if kind == 'hex' else {}),
            'citation': reference + ':' + str(source.count('\n', 0, match.start()) + 1),
            'source_text': ' '.join(match.group(0).split())[:160],
        }
    # The macOS half of the ground truth: the validation contract every external
    # method call goes through, parsed from the same pinned xnu source.
    try:
        contract_module = load('abi_contract', 'tools/abi_contract.py')
        published = contract_module.contract(tool.read_reference)
        dispatch = published['dispatch_structures']
        facts['userclient'] = {
            'dispatch_2022_entry_bytes': {
                'value': dispatch['IOExternalMethodDispatch2022']['layout']['size'],
                'citation': dispatch['IOExternalMethodDispatch2022']['citation']},
            'dispatch_legacy_entry_bytes': {
                'value': dispatch['IOExternalMethodDispatch']['layout']['size'],
                'citation': dispatch['IOExternalMethodDispatch']['citation']},
            'variable_structure_sentinel': {
                'value': published['sentinels']['kIOUCVariableStructureSize']['value'],
                'citation': published['sentinels']['kIOUCVariableStructureSize']['citation']},
            'scalar_input_count_max': {
                'value': published['sentinels']['kIOExternalMethodScalarInputCountMax']['value'],
                'citation':
                    published['sentinels']['kIOExternalMethodScalarInputCountMax']['citation']},
            'validation_steps': {'value': len(published['validation']['steps']),
                                 'citation': published['validation']['citation']},
            'probe_oracle_rules': {'value': len(published['probe_oracle']),
                                   'citation': published['validation']['citation']},
            'entitlement_checked_after_shape': {
                'value': True, 'citation': published['validation']['citation'],
                'note': 'so a NotPrivileged answer confirms an argument shape with no '
                        'side effect'},
        }
        for name, code in published['return_codes'].items():
            facts['userclient']['return_' + name] = {'value': code['hex'],
                                                     'citation': code['citation']}
        metaclass = published['metaclass']
        facts['metaclass'] = {
            'record_bytes': {'value': metaclass['layout']['size'],
                             'citation': metaclass['citation']},
            'assumption': {'value': metaclass['assumed'], 'citation': metaclass['citation']},
        }
        for field in metaclass['layout']['fields']:
            facts['metaclass']['offset_' + field['name']] = {
                'value': field['offset'], 'citation': metaclass['citation']}
    except Exception as error:
        drift.append({'group': 'userclient', 'name': 'published_contract',
                      'reference': 'tools/abi_contract.py',
                      'reason': type(error).__name__ + ': ' + str(error)[:200]})

    # The PAT and LRC facts are parsed by the same code the port suite verifies
    # against, so the report and the test can never disagree about them.
    try:
        port = load('test_arc_port', 'tests/test_arc_port.py')
        pat, lrc = port.PinnedPatSource(), port.PinnedLrcSource()
        facts['pat'] = {
            'bit_positions': {'no_promote': pat.no_promote, 'comp_en': pat.comp_en,
                              'l3_clos': pat.l3_clos, 'l3_policy': pat.l3_policy,
                              'l4_policy': pat.l4_policy, 'coh_mode': pat.coh_mode},
            'register_ranges': {'index_split': pat.split, 'range0': list(pat.range0),
                                'range1': list(pat.range1), 'ats_offset': pat.ats_offset,
                                'pta_offset': pat.pta_offset},
            'table': {str(index): value for index, value in sorted(pat.table.items())},
            'ats_value': pat.ats_value, 'pta_value': pat.pta_value,
            'cache_indices': pat.cache_indices,
            'citation': 'linux-v6.17/drivers/gpu/drm/xe/xe_pat.c',
        }
        facts['lrc'] = {
            'context_dword_indices': dict(sorted(lrc.ctx.items())),
            'context_control_bits': lrc.ctx_ctrl, 'ring_valid': lrc.ring_valid,
            'tail_mask': lrc.tail_addr, 'tail_shift': lrc.tail_shift,
            'head_mask': lrc.head_addr, 'head_shift': lrc.head_shift,
            'pphwsp_size': lrc.pphwsp_size, 'wa_bb_size': lrc.wa_bb_size,
            'indirect_ring_state_size': lrc.indirect_ring_size,
            'image_pages': {'render': lrc.render_pages, 'compute': lrc.compute_pages},
            'bo_size_formula': ' '.join(lrc.bo_size_formula.split()),
            'descriptor_bits': {'valid': lrc.lrc_valid, 'privilege': lrc.lrc_privilege,
                                'addressing_shift': lrc.addressing_shift,
                                'legacy_64b': lrc.legacy_64b},
            'ring_create_size': lrc.ring_create_size,
            'prologue': {'mi_arb_opcode': lrc.mi_arb_opcode,
                         'mi_arb_enable': lrc.mi_arb_enable, 'mi_noop': lrc.mi_noop,
                         'instruction_type': lrc.instr_mi},
            'citation': 'linux-v6.17/drivers/gpu/drm/xe/xe_lrc.c',
        }
    except Exception as error:
        drift.append({'group': 'pat/lrc', 'name': 'pinned_source_parsers',
                      'reference': 'tests/test_arc_port.py',
                      'reason': type(error).__name__ + ': ' + str(error)[:200]})
    record = {'performed': True, 'rules': len(EXTRACTION_RULES),
              # A group's shared 'citation' key is provenance, not a fact.
              'extracted': sum(len(set(group) - {'citation'}) for group in facts.values()),
              'drift': drift, 'facts': facts,
              'scope': 'Values and citations from hash-pinned sources. No upstream code is '
                       'copied into this repository.'}
    (out / 'upstream-facts.json').write_text(json.dumps(record, indent=2) + '\n',
                                             encoding='utf-8')
    return record


def stage_appledriver(roots: list[Path], out: Path) -> dict:
    tool = load('analyze_apple_driver', 'tools/analyze_apple_driver.py')
    nm = shutil.which('nm')
    found, maps = [], {}
    for root in roots:
        if not root.is_dir():
            continue
        for name in KEXT_CANDIDATES:
            bundle = root / name
            if not bundle.is_dir():
                continue
            found.append(str(bundle))
            try:
                record = tool.analyze(bundle, nm)
            except SystemExit as error:
                maps[name] = {'performed': False, 'reason': str(error)[:200]}
                continue
            except Exception as error:
                maps[name] = {'performed': False,
                              'reason': type(error).__name__ + ': ' + str(error)[:200]}
                continue
            maps[name] = record
            target = out / 'apple' / (name.replace('.kext', '') + '.json')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    if not found:
        return {'performed': False, 'searched': [str(root) for root in roots],
                'reason': 'no Apple graphics bundle found; run this stage on the macOS install'}
    summary = {}
    for name, record in maps.items():
        symbols = record.get('symbols', {}) if isinstance(record, dict) else {}
        summary[name] = {
            'identifier': record.get('bundle', {}).get('identifier') if isinstance(record, dict) else None,
            'libraries': list((record.get('library_dependencies') or {}).keys())
            if isinstance(record, dict) else [],
            'classes_defined': symbols.get('classes_defined_by_this_bundle', []),
            'iokit_overrides': symbols.get('iokit_overrides', {}),
            'symbols_available': bool(symbols.get('available')),
        }
    return {'performed': True, 'bundles_found': found, 'summary': summary,
            'symbol_tool': nm, 'apple_binary_copied': False}


def stage_system() -> dict:
    if platform.system() != 'Darwin':
        return {'performed': False, 'reason': 'not running on macOS'}
    record = {'performed': True}
    kexts = probe(MACOS_PROBES['loaded_kexts'])
    record['loaded_kexts'] = kexts
    if kexts.get('performed'):
        interesting = [line.strip() for line in kexts['stdout'].splitlines()
                       if any(key in line for key in ('Graphics', 'IOGPU', 'IOAccelerator',
                                                      'IOSurface', 'AGX', OWN_BUNDLE_ID))]
        record['graphics_kexts_loaded'] = interesting
        record['own_driver_loaded'] = any(OWN_BUNDLE_ID in line for line in interesting)
    record['displays'] = probe(MACOS_PROBES['displays'], timeout=120)
    record['accelerator_nodes'] = probe(['ioreg', '-l', '-w', '0', '-c', 'IOAccelerator'],
                                        timeout=120)
    record['gpu_nodes'] = probe(['ioreg', '-l', '-w', '0', '-c', 'IOGPU'], timeout=120)
    return record


def stage_device() -> dict:
    if platform.system() != 'Darwin':
        return {'performed': False, 'reason': 'not running on macOS'}
    record: dict = {'performed': True, 'target': TARGET_DEVICE}
    pci = probe(['ioreg', '-l', '-w', '0', '-c', 'IOPCIDevice'], timeout=120)
    record['pci_nodes'] = pci
    # ioreg prints the configuration-space words as little-endian byte blobs,
    # so 0x64a0 reads back as a064 and 0x8086 as 8680.
    haystack = pci.get('stdout', '').lower() if pci.get('performed') else ''
    matches = {'device_id_bytes': 'a064' in haystack, 'vendor_id_bytes': '8680' in haystack,
               'subsystem_bytes': 'ddc14d14' in haystack}
    record['identity_matches'] = matches
    record['device_present'] = matches['device_id_bytes'] and matches['vendor_id_bytes']
    record['device_tree_gfx0'] = probe(['ioreg', '-l', '-w', '0', '-p', 'IODeviceTree',
                                        '-n', 'GFX0'], timeout=120)
    collector = load('collect_macos', 'tools/collect_macos.py')
    try:
        report = collector.collect_iokit()
    except Exception as error:
        record['own_driver_report'] = {'performed': False,
                                       'reason': type(error).__name__ + ': ' + str(error)[:200]}
        return record
    record['own_driver_report'] = {'performed': True, 'iokit_status': report.get('iokit_status'),
                                   'has_snapshot': 'snapshot' in report,
                                   'arc_interface': report.get('arc_interface'),
                                   'arc_port_plan': report.get('arc_port_plan'),
                                   'errors': {key: value for key, value in report.items()
                                              if key.endswith('_error')}}
    return record


def findings(record: dict) -> list[dict]:
    """Rank what this run established and what each blocker needs next."""
    out: list[dict] = []

    def add(identifier, severity, state, summary, evidence, next_step):
        out.append({'id': identifier, 'severity': severity, 'state': state,
                    'summary': summary, 'evidence': evidence, 'next_step': next_step})

    repository = record.get('repository', {})
    pinned = repository.get('pinned_references') or {}
    if pinned:
        complete = not pinned.get('missing') and not pinned.get('mismatched')
        add('R01', 'high' if not complete else 'info',
            'established' if complete else 'blocked',
            f"{pinned.get('verified')} of {pinned.get('total')} pinned sources verified",
            {'missing': pinned.get('missing', [])[:5],
             'mismatched': pinned.get('mismatched', [])[:5]},
            'none' if complete else 'run tools/fetch_references.py before trusting any extraction')
    libraries = record.get('libraries') or {}
    if libraries:
        unbuilt = [name for name, entry in libraries.items() if not entry.get('built')]
        add('R02', 'high' if unbuilt else 'info', 'blocked' if unbuilt else 'established',
            'the CPU modules and the host-test library build with -Werror'
            if not unbuilt else 'these libraries do not build: ' + ', '.join(unbuilt),
            {name: {'built': entry.get('built'), 'reason': entry.get('reason'),
                    'stderr': (entry.get('stderr') or '')[:400]}
             for name, entry in libraries.items()},
            'none' if not unbuilt else 'fix the build; every claim below depends on it')
    suites = record.get('tests', {})
    failed = [name for name, value in suites.items()
              if value.get('performed') and not value.get('passed')]
    if suites:
        add('R03', 'high' if failed else 'info', 'blocked' if failed else 'established',
            f'{len(suites) - len(failed)} of {len(suites)} suites passed',
            {'failed': failed, 'tails': {name: suites[name].get('tail') for name in failed}},
            'none' if not failed else 'fix the failing suites; they gate every claim below')

    interface = record.get('interface', {})
    if interface.get('performed'):
        table = interface.get('driver_table') or {}
        mismatches = table.get('mismatches')
        add('R04', 'high' if mismatches else 'info',
            'blocked' if mismatches else 'established',
            'compiled interface table matches the pinned sources'
            if not mismatches else 'interface table drifted from the sources',
            {'mismatches': (mismatches or [])[:5],
             'ioctls': interface.get('linux_ioctls')},
            'none' if not mismatches else 'reconcile lb_arc_interface with the pinned uAPI')
        add('R05', 'info', 'established',
            'no Apple driver exists for Xe or Xe2'
            if not interface.get('apple_xe_or_xe2_driver_present')
            else 'an Apple Xe/Xe2 driver appeared in the inventory',
            {'apple_intel_kexts': interface.get('apple_intel_kexts'),
             'newest_generation': interface.get('apple_newest_generation')},
            'treat the macOS side as a contract to satisfy, not a driver to adapt')

    plan = record.get('portplan', {})
    if plan.get('performed'):
        detail = plan['plan']
        add('R06', 'info', 'established',
            f"port plan holds {detail['pat_write_count']} PAT writes and an "
            f"{detail['lrc_write_count']}-write context image",
            {'pat_indices': detail['pat_indices'],
             'pat_ggtt_max_index': detail['pat_ggtt_max_index'],
             'lrc_render_total_bytes': detail['lrc_render_total_bytes'],
             'descriptor_template': hex(detail['lrc_descriptor_template'])},
            'compare it against a real readback once the MMIO opt-in is qualified')

    extract = record.get('extract', {})
    if extract.get('performed'):
        drift = extract.get('drift') or []
        add('R13', 'high' if drift else 'info', 'blocked' if drift else 'established',
            f"{extract.get('extracted')} upstream facts re-derived from "
            f"{extract.get('rules')} rules with citations"
            if not drift else f'{len(drift)} extraction rules no longer match their source',
            {'drift': drift[:5], 'groups': {name: len(group)
                                            for name, group in extract['facts'].items()}},
            'none' if not drift else 'the pinned source moved: re-read it and update the rule '
                                     'and the port together')
        # Which extracted group has code in this repository, and what is still
        # missing in it. Module paths are checked, so a moved file shows up here.
        coverage = {}
        for group in sorted(extract['facts']):
            modules, gap = PORT_COVERAGE.get(group, ((), 'nothing in this repository'))
            coverage[group] = {
                'modules': [module for module in modules if (ROOT / module).is_file()],
                'missing_modules': [module for module in modules
                                    if not (ROOT / module).is_file()],
                'gap': gap,
            }
        open_groups = [group for group, entry in coverage.items() if entry['gap']]
        # Open work, not a defect: a run only fails on drift, a broken build or
        # a failing suite, never on the part of the port that is still missing.
        add('R14', 'medium' if open_groups else 'info',
            'blocked' if open_groups else 'established',
            'extracted facts with work left in this repository: ' + ', '.join(open_groups)
            if open_groups else 'every extracted group has code behind it',
            {'coverage': coverage,
             'guc_firmware_version': (extract['facts'].get('guc', {})
                                      .get('lunarlake_guc_version', {}).get('value'))},
            'order: PPGTT root over the GGTT planner, then the GuC load sequence '
            '(RSA upload, DMA_CTRL, GUC_STATUS poll), each with its own readback test')

    apple = record.get('appledriver', {})
    if apple.get('performed'):
        families = sorted({library for entry in apple['summary'].values()
                           for library in entry['libraries']
                           if 'IOGPU' in library or 'IOAccelerator' in library
                           or 'IOGraphics' in library or 'IOSurface' in library})
        add('R07', 'high', 'established',
            'the installed Apple drivers name the private families a Metal-capable '
            'driver links against',
            {'families': families,
             'bundles': list(apple['summary'].keys())},
            'declare the needed family in the own Info.plist only together with an '
            'implementation of its contract')
        overrides = {name: entry['iokit_overrides'] for name, entry in apple['summary'].items()
                     if entry['iokit_overrides']}
        if overrides:
            add('R08', 'high', 'established',
                'the IOKit methods an Apple graphics driver overrides are now enumerated',
                {'overrides': overrides},
                'turn the override list into the implementation plan for the own classes')
        if not any(entry['symbols_available'] for entry in apple['summary'].values()):
            add('R09', 'medium', 'blocked',
                'bundles were found but no symbol tool was available',
                {'symbol_tool': apple.get('symbol_tool')},
                'rerun this stage on the macOS install with nm in PATH')
    elif apple:
        add('R07', 'medium', 'blocked', 'no Apple graphics bundle was reachable',
            {'searched': apple.get('searched', [])},
            'run the appledriver stage on the macOS installation that holds the drivers')

    system = record.get('system', {})
    if system.get('performed'):
        add('R10', 'info', 'established' if system.get('own_driver_loaded') else 'blocked',
            'this project\'s driver is loaded' if system.get('own_driver_loaded')
            else 'this project\'s driver is not loaded',
            {'graphics_kexts_loaded': system.get('graphics_kexts_loaded', [])[:10]},
            'none' if system.get('own_driver_loaded')
            else 'install the unsigned bundle in a test EFI and check the load path first')
    device = record.get('device', {})
    if device.get('performed'):
        add('R11', 'high', 'established' if device.get('device_present') else 'blocked',
            'the target GPU is present in the device tree' if device.get('device_present')
            else 'the target GPU was not found in the device tree',
            {'target': TARGET_DEVICE},
            'none' if device.get('device_present')
            else 'check the PCI path and the firmware configuration on the target')
        own = device.get('own_driver_report', {})
        if own.get('performed'):
            add('R12', 'high', 'established',
                'the loaded driver answered its diagnostic selectors',
                {'iokit_status': own.get('iokit_status'),
                 'arc_interface_present': bool(own.get('arc_interface')),
                 'arc_port_plan_present': bool(own.get('arc_port_plan'))},
                'read back PAT and compare it with the port plan')

    # Nothing in this tool can establish acceleration; say it once, always.
    add('R99', 'info', 'blocked', 'no hardware acceleration is established by this run',
        {'reason': 'no register was written, no queue registered, no Metal device created'},
        'the order stays: load, PAT readback, GGTT binding, PPGTT root, GuC load, '
        'submission with fence completion, then the Metal provider')
    order = {'high': 0, 'medium': 1, 'info': 2}
    return sorted(out, key=lambda finding: (order[finding['severity']],
                                            finding['state'] != 'blocked', finding['id']))


def one_line(value) -> str:
    """Render a nested value small enough to read in a table cell."""
    if isinstance(value, dict):
        return ', '.join(name + '=' + one_line(item) for name, item in value.items())[:600]
    if isinstance(value, list):
        return ', '.join(one_line(item) for item in value)[:600] or '—'
    if isinstance(value, bool) or not isinstance(value, int):
        return str(value)
    # Register offsets and bit masks only read as values in hexadecimal.
    return str(value) + (' (' + hex(value) + ')' if value >= 256 else '')


def summarise(record: dict, ranked: list[dict]) -> str:
    environment = record['environment']
    lines = ['<!-- SPDX-License-Identifier: GPL-3.0-only -->',
             '# LunarBridge recon report', '',
             '| | |', '| --- | --- |',
             '| Host | ' + environment['system'] + ' ' +
             (environment.get('macos_version') or environment['release']) + ' · ' +
             environment['machine'] + ' |',
             '| Commit | ' + str((record.get('repository') or {}).get('commit')) + ' |',
             '| Working tree | ' +
             ('clean' if not (record.get('repository') or {}).get('dirty') else 'modified') + ' |',
             '| Stages | ' + ', '.join(record['stages_selected']) + ' |',
             '', '## Stages', '']
    for stage in STAGES:
        value = record.get(stage)
        if value is None:
            lines.append('- `' + stage + '`: not selected')
            continue
        performed = value.get('performed', True) if isinstance(value, dict) else True
        reason = value.get('reason') if isinstance(value, dict) else None
        lines.append('- `' + stage + '`: ' + ('ran' if performed
                                              else 'skipped — ' + str(reason)))
    libraries = record.get('libraries') or {}
    if libraries:
        lines += ['', '## Build and tests', '', '| Library | Built | Sources | SHA-256 |',
                  '| --- | --- | --- | --- |']
        for name, entry in libraries.items():
            lines.append('| ' + name + ' | ' + ('yes' if entry.get('built') else
                                                'no — ' + str(entry.get('reason'))) + ' | ' +
                         str(entry.get('source_count', '—')) + ' | ' +
                         (entry.get('sha256', '—')[:16]) + ' |')
    suites = record.get('tests') or {}
    if suites:
        lines += ['', '| Suite | Result |', '| --- | --- |']
        for name, entry in suites.items():
            if not entry.get('performed'):
                lines.append('| ' + name + ' | skipped — ' + str(entry.get('reason')) + ' |')
                continue
            lines.append('| ' + name + ' | ' + ('passed' if entry['passed'] else 'FAILED') +
                         ' · ' + one_line(entry.get('tail', [])[-1:]) + ' |')
    facts = (record.get('extract') or {}).get('facts') or {}
    if facts:
        lines += ['', '## Upstream facts, re-derived with citations', '']
        lines.append('Full values, including anything shortened here, are in `recon.json`.')
        for group in sorted(facts):
            shared = facts[group].get('citation')
            lines += ['', '### ' + group, '']
            if shared:
                lines += ['Parsed from `' + shared + '` by the same code '
                          '`tests/test_arc_port.py` verifies the port against.', '']
            lines += ['| Fact | Value | Source |', '| --- | --- | --- |']
            for name, entry in sorted(facts[group].items()):
                if name == 'citation':
                    continue
                if isinstance(entry, dict) and 'citation' in entry and 'value' in entry:
                    lines.append('| ' + name + ' | ' + one_line(entry['value']) +
                                 ' | `' + entry['citation'] + '` |')
                else:
                    lines.append('| ' + name + ' | ' + one_line(entry) + ' | `' +
                                 str(shared or '—') + '` |')
    apple = record.get('appledriver') or {}
    if apple.get('performed'):
        lines += ['## Apple interface surface', '',
                  '| Bundle | Links | Classes | Overrides |', '| --- | --- | --- | --- |']
        for name, entry in apple['summary'].items():
            lines.append('| ' + name + ' | ' + one_line(entry['libraries']) + ' | ' +
                         one_line(entry['classes_defined']) + ' | ' +
                         one_line(entry['iokit_overrides']) + ' |')
        lines += ['', 'Names, counts and hashes only; no Apple binary was copied.', '']
    lines += ['## Findings', '']
    for finding in ranked:
        lines += ['### ' + finding['id'] + ' · ' + finding['severity'] + ' · ' +
                  finding['state'], '', finding['summary'], '']
        for name, value in (finding['evidence'] or {}).items():
            lines.append('- ' + name + ': ' + one_line(value))
        lines += ['', 'Next: ' + finding['next_step'], '']
    lines += ['## Scope', '',
              'Read-only: no system setting was changed, no register was written and no Apple '
              'binary was copied. Every number above was produced in this run or read from a '
              'hash-pinned source. Nothing here claims hardware acceleration.']
    return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=ROOT / 'build/recon')
    parser.add_argument('--stages', default=','.join(STAGES),
                        help='comma-separated subset of: ' + ', '.join(STAGES))
    parser.add_argument('--kext-root', action='append', type=Path, default=None,
                        help='where to look for Apple graphics bundles (repeatable)')
    parser.add_argument('--no-build', action='store_true', help='skip building the CPU modules')
    parser.add_argument('--no-tests', action='store_true', help='skip running the test suites')
    arguments = parser.parse_args()
    selected = [stage.strip() for stage in arguments.stages.split(',') if stage.strip()]
    unknown = [stage for stage in selected if stage not in STAGES]
    if unknown:
        raise SystemExit('Unknown stage(s): ' + ', '.join(unknown))
    out = arguments.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    roots = arguments.kext_root or [Path('/System/Library/Extensions'),
                                    Path('/System/Library/DriverExtensions')]

    record: dict = {'generated_by': 'tools/recon.py', 'stages_selected': selected,
                    'scope': 'Read-only collection. No system change, no register write, no '
                             'Apple binary copied, no hardware claim without a measurement.'}
    record['environment'] = stage_environment()
    libraries: dict = {}
    if {'repository', 'interface', 'portplan'} & set(selected):
        libraries = build_libraries(out, build=not arguments.no_build)
        record['libraries'] = libraries
    modules = (libraries.get('modules') or {}).get('path')
    if 'repository' in selected:
        record['repository'] = stage_repository()
        if not arguments.no_tests:
            record['tests'] = stage_tests(libraries)
    if 'interface' in selected:
        record['interface'] = stage_interface(modules, out)
    if 'portplan' in selected:
        record['portplan'] = stage_portplan(modules)
    if 'extract' in selected:
        record['extract'] = stage_extract(out)
    if 'appledriver' in selected:
        record['appledriver'] = stage_appledriver(roots, out)
    if 'system' in selected:
        record['system'] = stage_system()
    if 'device' in selected:
        record['device'] = stage_device()
    ranked = findings(record)
    record['findings'] = ranked
    (out / 'recon.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    (out / 'RECON.md').write_text(summarise(record, ranked), encoding='utf-8')
    blocked = [finding for finding in ranked
               if finding['state'] == 'blocked' and finding['severity'] == 'high']
    print('Wrote ' + str(out / 'recon.json') + ' and ' + str(out / 'RECON.md'))
    print(str(len(ranked)) + ' findings; ' + str(len(blocked)) +
          ' high-severity blockers decide the exit code')
    for finding in ranked[:6]:
        print('  ' + finding['id'] + ' [' + finding['severity'] + '/' + finding['state'] +
              '] ' + finding['summary'])
    return 1 if blocked else 0


if __name__ == '__main__':
    raise SystemExit(main())
