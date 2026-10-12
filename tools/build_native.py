# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Build a real diagnostic kext using the macOS linker, without installing it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from macho import inspect_macho

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', required=True, type=Path)
    parser.add_argument('--out', type=Path, default=Path('build/native'))
    parser.add_argument('--debug-log', action='store_true')
    parser.add_argument('--mmio-read', action='store_true', help='Opt in to the guarded one-time GMD register sample')
    parser.add_argument('--dma-prepare', action='store_true', help='Opt in to device-mapper DMA preparation; no GPU binding or submission')
    parser.add_argument('--forcewake-probe', action='store_true', help='Opt in to a bounded main-GT wake/release handshake; includes LNL GMD read flush writes')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('Kext-Linken benötigt den macOS-Linker. Unter Windows build_gpu.py für Objektprüfungen verwenden.')
    root = Path(__file__).resolve().parents[1]
    args.out = args.out.resolve()
    args.sdk = args.sdk.resolve()
    clang = subprocess.check_output(['xcrun', '--find', 'clang++'], text=True).strip()
    linker = subprocess.check_output(['xcrun', '--find', 'ld'], text=True).strip()
    objects = args.out / 'objects'
    objects.mkdir(parents=True, exist_ok=True)
    commands = []
    sources = [root/'gpu-prototype/lnl_platform.cpp', root/'gpu-prototype/lb_diagnostics.cpp',
               root/'gpu-prototype/lb_gpu_memory.cpp', root/'gpu-prototype/lb_ggtt_plan.cpp',
               root/'gpu-prototype/lb_guc_transport.cpp', root/'gpu-prototype/lb_guc_scheduler.cpp',
               root/'gpu-prototype/lb_guc_firmware.cpp',
               root/'gpu-prototype/lb_xe2_pat.cpp', root/'gpu-prototype/lb_lrc.cpp',
               root/'gpu-prototype/lb_arc_interface.cpp',
               root/'gpu-prototype/lb_forcewake.cpp', root/'native/LBForceWakeAdapter.cpp',
               root/'native/LBDMAContext.cpp', root/'native/LBDMAQuarantine.cpp',
               root/'native/LunarBridgeDriver.cpp', root/'native/KmodInfo.cpp']
    common = [clang, '-arch', 'x86_64', '-mmacosx-version-min=12.0', '-std=c++17', '-O2',
              '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', '-fno-exceptions', '-fno-rtti',
              # XNU xnu-12377.1.9 iokit/conf/Makefile.x86_64 also uses this
              # for kernel code. It excludes the userspace Darwin stack probe;
              # kernel stack-size budgeting remains a separate requirement.
              '-ffreestanding', '-fapple-kext', '-mkernel', '-fno-stack-check',
              '-DKERNEL', '-DKERNEL_PRIVATE', '-D__KERNEL__',
              '-mno-sse', '-mno-sse2', '-mno-mmx', '-mno-red-zone', '-nostdinc', '-nostdinc++',
              '-I', str(args.sdk/'Headers'), '-UTARGET_IPHONE_SIMULATOR', '-UTARGET_OS_NANO']
    for source in sources:
        command = common + ['-c', str(source), '-o', str(objects/(source.stem+'.o'))]
        subprocess.run(command, check=True, timeout=120)
        commands.append(command)
    kext = args.out / 'LunarBridge.kext'
    binary = kext / 'Contents/MacOS/LunarBridge'
    binary.parent.mkdir(parents=True, exist_ok=True)
    command = [linker, '-arch', 'x86_64', '-kext', '-undefined', 'dynamic_lookup', '-no_uuid', '-o', str(binary)]
    command += [str(objects/(s.stem+'.o')) for s in sources]
    subprocess.run(command, check=True, timeout=120)
    commands.append(command)
    inspection = inspect_macho(binary.read_bytes(), require_kext=True)
    if inspection['identifier'] != 'de.nordapps.LunarBridge' or inspection['version'] != '0.2.0d1':
        raise ValueError('Linked kmod metadata mismatch')
    info = plistlib.loads((root/'native/Info.plist').read_bytes())
    personality = info['IOKitPersonalities']['LunarBridgeArc130VSamsungDiagnostic']
    personality['LBDebugLogging'] = args.debug_log
    personality['LBEnableMMIORead'] = args.mmio_read or args.forcewake_probe
    personality['LBEnableForceWakeProbe'] = args.forcewake_probe
    personality['LBEnableDMAPreparation'] = args.dma_prepare
    (kext/'Contents/Info.plist').write_bytes(plistlib.dumps(info, sort_keys=False))
    report = {'artifact': 'LunarBridge.kext', 'stage': 'unsigned native diagnostic build',
              'source_commit': os.environ.get('GITHUB_SHA'), 'macos_version': platform.mac_ver()[0],
              'compiler_version': subprocess.check_output([clang, '--version'], text=True).splitlines()[0],
              'entry_glue': 'project-owned KmodInfo.cpp; no static SDK library linked',
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'inspection': inspection,
              'mmio_enabled': args.mmio_read or args.forcewake_probe, 'debug_logging': args.debug_log,
              'forcewake_probe_enabled': args.forcewake_probe, 'native_forcewake_hardware_tested': False,
              'dma_preparation_enabled': args.dma_prepare,
              'native_dma_hardware_tested': False,
              'native_hardware_tested': False, 'gpu_execution_implemented': False,
              'metal_implemented': False, 'firmware_secure_boot_verified': False,
              'commands': commands}
    (args.out/'native-build-report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    source_names = ('native/LunarBridgeDriver.cpp', 'native/LunarBridgeDriver.h',
                    'native/LBDMAContext.cpp', 'native/LBDMAContext.h',
                    'native/LBDMAQuarantine.cpp', 'native/LBDMAQuarantine.h',
                    'native/LBForceWakeAdapter.cpp', 'native/LBForceWakeAdapter.h',
                    'gpu-prototype/lb_forcewake.cpp', 'gpu-prototype/lb_forcewake.h',
                    'native/KmodInfo.cpp', 'native/Info.plist', 'gpu-prototype/lnl_platform.cpp',
                    'gpu-prototype/lnl_platform.h', 'gpu-prototype/lb_diagnostics.cpp',
                    'gpu-prototype/lb_diagnostics.h', 'gpu-prototype/SOURCE-NOTICES.txt',
                    'gpu-prototype/lb_gpu_memory.cpp', 'gpu-prototype/lb_gpu_memory.h',
                    'gpu-prototype/lb_ggtt_plan.cpp', 'gpu-prototype/lb_ggtt_plan.h',
                    'gpu-prototype/lb_guc_transport.cpp', 'gpu-prototype/lb_guc_transport.h',
                    'gpu-prototype/lb_guc_scheduler.cpp', 'gpu-prototype/lb_guc_scheduler.h',
                    'gpu-prototype/lb_xe2_pat.cpp', 'gpu-prototype/lb_xe2_pat.h',
                    'gpu-prototype/lb_lrc.cpp', 'gpu-prototype/lb_lrc.h',
                    'gpu-prototype/lb_arc_interface.cpp', 'gpu-prototype/lb_arc_interface.h',
                    'gpu-prototype/lb_guc_firmware.cpp', 'gpu-prototype/lb_guc_firmware.h',
                    'tools/build_native.py')
    reference = {key: report[key] for key in ('source_commit', 'macos_version', 'compiler_version',
                 'entry_glue', 'binary_sha256', 'mmio_enabled', 'debug_logging', 'dma_preparation_enabled',
                 'forcewake_probe_enabled', 'native_forcewake_hardware_tested',
                 'native_hardware_tested', 'gpu_execution_implemented', 'metal_implemented')}
    reference['source_files_sha256'] = {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                                      for name in source_names}
    reference['source_repository'] = 'https://github.com/IT-EXPRESS-Bayern/LunarBridge-Hackintosh'
    reference['sdk_repository'] = 'https://github.com/acidanthera/MacKernelSDK'
    (args.out/'SOURCE-REFERENCE.json').write_text(json.dumps(reference, indent=2)+'\n', encoding='utf-8')
    archive = args.out/'LunarBridge-Installer.zip'
    paths = [(p, p.relative_to(args.out).as_posix()) for p in kext.rglob('*') if p.is_file()]
    paths += [(root/name, name) for name in source_names]
    paths += [(args.out/'SOURCE-REFERENCE.json', 'SOURCE-REFERENCE.json')]
    paths += [(root/name, name) for name in ('installer/install_opencore.py', 'tools/macho.py',
              'tools/collect_macos.py', 'tools/analyze_trace.py', 'tools/lb_wire.py',
              'docs/INSTALLER.md', 'docs/DIAGNOSTICS.md', 'docs/NATIVE_BACKEND.md',
              'docs/GPU_DESIGN.md', 'docs/HARDWARE_SUPPORT.md', 'docs/LICENSING.md',
              'docs/GPU_FOUNDATION.md', 'docs/GPU_BRINGUP.md', 'docs/GGTT_BINDING.md',
              'docs/GGTT_BINDING_SOURCES.json', 'docs/GUC_SCHEDULER.md',
              'gpu-prototype/GUC_SCHEDULER_SOURCES.json', 'docs/ARCHITECTURE.md', 'docs/PORTING_GUIDE.md',
              'docs/DMA_BACKEND.md', 'docs/DRIVER_MODULES.md',
              'docs/VALIDATION.md', 'docs/VALIDATION.json',
              'docs/ARC_INTERFACE_DIFF.md', 'docs/ARC_INTERFACE_DIFF.json',
              'docs/ARC_INTERFACE_SOURCES.json', 'tools/compare_interfaces.py',
              'docs/ARC_PORT_FROM_LINUX.md', 'docs/ARC_PORT_SOURCES.json',
              'README.md', 'README.de.md',
              '.github/ISSUE_TEMPLATE/native-diagnostics.yml',
              'LICENSE', 'NOTICE', 'LICENSE-STATUS.txt', 'LICENSES/MIT-Intel.txt')]
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for source, name in paths: z.write(source, name)
    print('Kext und Installer-Archiv gebaut. Keine Installation, Signierung oder Geräteprobe ausgeführt.')

if __name__ == '__main__': main()
