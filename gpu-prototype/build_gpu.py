# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Build host-test DLL and macOS object files; does not link/install a kext."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--compiler', required=True, type=Path, help='Verified portable zig.exe')
p.add_argument('--sdk', required=True, type=Path, help='MacKernelSDK directory')
p.add_argument('--host-dir', required=True, type=Path)
p.add_argument('--object-dir', required=True, type=Path)
args = p.parse_args()
root = Path(__file__).resolve().parent
args.host_dir.mkdir(parents=True, exist_ok=True)
args.object_dir.mkdir(parents=True, exist_ok=True)
common = [str(args.compiler.resolve()), 'c++', '-std=c++17', '-O2', '-Wall',
          '-Wextra', '-Werror', '-fno-exceptions', '-fno-rtti']
dll = args.host_dir.resolve() / 'lnl_host.dll'
commands = [common + ['-target', 'x86_64-windows-gnu', '-shared', '-nostdlib',
                     '-DLB_DMA_HOST_TEST',
                     '-DLB_FORCEWAKE_HOST_TEST',
                     '-Wl,--entry,DllMain', str(root / 'lnl_platform.cpp'),
                     str(root / 'lb_diagnostics.cpp'),
                     str(root / 'lb_gpu_memory.cpp'), str(root / 'lb_ggtt_plan.cpp'),
                     str(root / 'lb_guc_transport.cpp'), str(root / 'lb_guc_scheduler.cpp'),
                     str(root / 'lb_guc_firmware.cpp'),
                     str(root / 'lb_forcewake.cpp'),
                     str(root.parent/'native/LBDMAContext.cpp'),
                     str(root.parent/'native/LBDMAQuarantine.cpp'),
                     str(root.parent/'tests/dma_adapter_fake.cpp'),
                     str(root.parent/'native/LBForceWakeAdapter.cpp'),
                     str(root.parent/'tests/forcewake_adapter_fake.cpp'),
                     str(root / 'host_dll_entry.cpp'), '-o', str(dll)]]
sources = [(name, root / (name + '.cpp')) for name in
           ['lnl_platform', 'LunarLakePCIProbe', 'lb_diagnostics', 'lb_gpu_memory',
            'lb_ggtt_plan', 'lb_guc_transport', 'lb_guc_scheduler', 'lb_guc_firmware', 'lb_forcewake']]
sources += [(name, root.parent / 'native' / (name + '.cpp')) for name in
            ['LBDMAContext', 'LBDMAQuarantine', 'LBForceWakeAdapter', 'LunarBridgeDriver', 'KmodInfo']]
for name, source in sources:
    command = common + ['-target', 'x86_64-macos', '-mmacosx-version-min=12.0',
                        '-ffreestanding', '-c', str(source),
                        '-o', str(args.object_dir.resolve() / (name + '.o'))]
    command += ['-DKERNEL', '-DKERNEL_PRIVATE', '-D__KERNEL__', '-mkernel',
                '-fapple-kext', '-mno-sse', '-mno-sse2', '-mno-mmx', '-mno-red-zone',
                '-nostdinc', '-nostdinc++', '-I', str(args.sdk.resolve() / 'Headers'),
                '-Wno-unused-parameter', '-UTARGET_IPHONE_SIMULATOR', '-UTARGET_OS_NANO']
    commands.append(command)
results = []
for command in commands:
    proc = subprocess.run(command, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=90)
    results.append({'command': command, 'exit_code': proc.returncode,
                    'stdout': proc.stdout, 'stderr': proc.stderr})
    print(Path(command[command.index('-o') + 1]).name + ': exit ' + str(proc.returncode), flush=True)
    if proc.returncode:
        print(proc.stderr, flush=True)
        break
artifacts = []
for path in [dll, *(args.object_dir / (name + '.o') for name, _ in sources)]:
    if path.exists():
        data = path.read_bytes()
        artifacts.append({'name': path.name, 'bytes': len(data),
                          'sha256': hashlib.sha256(data).hexdigest(), 'magic_hex': data[:4].hex()})
report = {'compiler_version': subprocess.check_output([str(args.compiler.resolve()), 'version'],
                  text=True).strip(), 'commands': results, 'artifacts': artifacts,
          'all_compiles_succeeded': len(results) == len(commands) and all(r['exit_code'] == 0 for r in results),
          'macos_object_count': len(sources),
          'loadable_kext_built': False, 'macos_hardware_tested': False,
          'metal_acceleration_implemented': False}
(root / 'build-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
raise SystemExit(0 if report['all_compiles_succeeded'] else 1)
