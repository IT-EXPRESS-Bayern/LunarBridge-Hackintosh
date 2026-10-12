# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Collect only LunarBridge diagnostic data on native macOS; no system changes."""
import argparse
import ctypes as c
import json
from pathlib import Path
import platform
import plistlib
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lb_wire import Snapshot, TraceBatch, BufferInfo, DMAInfo, ForceWakeInfo, ArcInterfaceReport, decode_snapshot, decode_batch, decode_dma_info, decode_forcewake_info, decode_arc_interface, parse_kernel_messages
from analyze_trace import markdown

def command(args):
    try: proc = subprocess.run(args, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired: return 124, b''
    return proc.returncode, proc.stdout

def filtered_registry(nodes):
    result = []
    if isinstance(nodes, list):
        for node in nodes: result.extend(filtered_registry(node))
    elif isinstance(nodes, dict):
        allowed = {'LBDriverVersion', 'LBDriverABI', 'LBPCIIdentifier', 'LBSubsystemIdentifier',
                   'LBRevision', 'LBBarBytes', 'LBMMIOPolicy', 'LBGmdSamplesValid', 'LBActualMMIOPerformed',
                   'LBGraphicsIP', 'LBMediaIP', 'LBTraceCapacity', 'LBCpuBufferMaxBytes',
                   'LBGPUExecutionSupported', 'LBMetalSupported', 'LBStage', 'LBEnableMMIORead', 'LBDebugLogging',
                   'LBEnableDMAPreparation', 'LBDMAMappingVerifiedOnTarget', 'LBDMATeardownQuarantined',
                   'LBDMAQuarantineSlots', 'LBDMAResourcesDrained', 'LBEnableForceWakeProbe',
                   'LBForceWakeCompleted', 'LBForceWakeStatus', 'LBForceWakeCleanupStatus',
                   'LBForceWakeResourcesRetained', 'LBLunarLakeReadFlushWrites'}
        properties = {key: value for key, value in nodes.items() if key in allowed and
                      isinstance(value, (bool, int, str))}
        if properties: result.append(properties)
        result.extend(filtered_registry(nodes.get('IORegistryEntryChildren', [])))
    return result

def cpu_memory_roundtrip(kit, connection, task, call, dma_required=False):
    input_size, output_size = (c.c_uint64 * 1)(4096), (c.c_uint64 * 1)()
    output_count = c.c_uint32(1)
    status = kit.IOConnectCallMethod(connection, 1, input_size, 1, None, 0,
        output_size, c.byref(output_count), None, None)
    if status: raise RuntimeError(f'CPU allocation: 0x{status:08x}')
    if output_count.value != 1 or output_size[0] != 4096:
        raise ValueError('CPU allocation output mismatch')
    info = BufferInfo.from_buffer_copy(call(2, BufferInfo()))
    if info.abi != 1 or info.bytes != 4096 or info.alignment != 4096 or info.gpuBound != 0 or info.cpuShared != 1:
        raise ValueError('CPU buffer contract mismatch')
    dma_info = None
    if dma_required:
        dma_info = decode_dma_info(call(5, DMAInfo()))
        if not dma_info['dma_prepared'] or dma_info['bytes'] != 4096:
            raise ValueError('DMA preparation was not observed')
    kit.IOConnectMapMemory64.argtypes = [c.c_uint32, c.c_uint32, c.c_uint32,
        c.POINTER(c.c_uint64), c.POINTER(c.c_uint64), c.c_uint32]
    kit.IOConnectMapMemory64.restype = c.c_uint32
    kit.IOConnectUnmapMemory64.argtypes = [c.c_uint32, c.c_uint32, c.c_uint32, c.c_uint64]
    kit.IOConnectUnmapMemory64.restype = c.c_uint32
    address, size = c.c_uint64(), c.c_uint64()
    status = kit.IOConnectMapMemory64(connection, 0, task, c.byref(address), c.byref(size), 1)
    if status: raise RuntimeError(f'CPU mapping: 0x{status:08x}')
    try:
        if not address.value or size.value != 4096: raise ValueError('CPU mapping size mismatch')
        if c.string_at(address.value, 4096) != bytes(4096): raise ValueError('CPU buffer was not zeroed')
        block = (c.c_uint8 * 4096).from_address(address.value)
        block[0], block[4095] = 0x4c, 0x42
        if (block[0], block[4095]) != (0x4c, 0x42): raise ValueError('CPU roundtrip mismatch')
    finally:
        unmap_status = kit.IOConnectUnmapMemory64(connection, 0, task, address.value)
        if unmap_status: raise RuntimeError(f'CPU unmapping: 0x{unmap_status:08x}')
    result = {'state': 'passed', 'bytes': 4096, 'gpu_bound': False, 'gpu_execution_tested': False,
              'cpu_unmap_status': '0x00000000'}
    if dma_info is not None: result['dma_preparation'] = dma_info
    return result

def collect_iokit(memory_test=False, dma_test=False):
    kit = c.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
    system = c.CDLL('/usr/lib/libSystem.B.dylib')
    task = c.c_uint32.in_dll(system, 'mach_task_self_').value
    kit.IOServiceMatching.argtypes = [c.c_char_p]; kit.IOServiceMatching.restype = c.c_void_p
    kit.IOServiceGetMatchingServices.argtypes = [c.c_uint32, c.c_void_p, c.POINTER(c.c_uint32)]
    kit.IOServiceGetMatchingServices.restype = c.c_uint32
    kit.IOIteratorNext.argtypes = [c.c_uint32]; kit.IOIteratorNext.restype = c.c_uint32
    kit.IOObjectRelease.argtypes = [c.c_uint32]
    kit.IOServiceOpen.argtypes = [c.c_uint32, c.c_uint32, c.c_uint32, c.POINTER(c.c_uint32)]
    kit.IOServiceOpen.restype = c.c_uint32
    kit.IOServiceClose.argtypes = [c.c_uint32]
    kit.IOConnectCallMethod.argtypes = [c.c_uint32, c.c_uint32, c.POINTER(c.c_uint64), c.c_uint32,
        c.c_void_p, c.c_size_t, c.POINTER(c.c_uint64), c.POINTER(c.c_uint32), c.c_void_p, c.POINTER(c.c_size_t)]
    kit.IOConnectCallMethod.restype = c.c_uint32
    iterator = c.c_uint32()
    status = kit.IOServiceGetMatchingServices(0, kit.IOServiceMatching(b'LunarBridgePCI'), c.byref(iterator))
    if status: return {'iokit_status': f'0x{status:08x}'}
    service = 0
    connection = c.c_uint32()
    try:
        service = kit.IOIteratorNext(iterator.value)
        if not service: return {'iokit_status': 'service_not_found'}
        status = kit.IOServiceOpen(service, task, 0, c.byref(connection))
        if status: return {'iokit_status': f'0x{status:08x}', 'administrator_required': True}
        def call(selector, structure, scalars=()):
            size = c.c_size_t(c.sizeof(structure))
            inputs = (c.c_uint64 * len(scalars))(*scalars) if scalars else None
            result = kit.IOConnectCallMethod(connection.value, selector, inputs, len(scalars), None, 0,
                None, None, c.byref(structure), c.byref(size))
            if result: raise RuntimeError(f'IOKit selector {selector}: 0x{result:08x}')
            if size.value != c.sizeof(structure): raise ValueError('Native output size mismatch')
            return bytes(structure)
        report = {'iokit_status': 'success', 'snapshot': decode_snapshot(call(0, Snapshot())),
                  'trace': [], 'trace_overwritten': 0, 'cpu_memory_test': 'not_requested',
                  'dma_preparation_test': 'not_requested'}
        dma_enabled = report['snapshot']['dma_preparation_enabled']
        if report['snapshot']['forcewake_probe_enabled']:
            try: report['forcewake'] = decode_forcewake_info(call(6, ForceWakeInfo()))
            except (OSError, RuntimeError, ValueError) as exc: report['forcewake_error'] = str(exc)
        # Interface table of the loaded driver; reading it submits no GPU work.
        try: report['arc_interface'] = decode_arc_interface(call(7, ArcInterfaceReport()))
        except (OSError, RuntimeError, ValueError) as exc: report['arc_interface_error'] = str(exc)
        if dma_test and not dma_enabled:
            report['dma_preparation_test'] = {'state': 'blocked', 'reason': 'driver_dma_opt_in_disabled'}
        elif memory_test and dma_enabled and not dma_test:
            report['cpu_memory_test'] = {'state': 'blocked', 'reason': 'explicit_dma_test_required'}
        elif memory_test or dma_test:
            try:
                result = cpu_memory_roundtrip(kit, connection.value, task, call, dma_required=dma_test)
                report['cpu_memory_test'] = result
                if dma_test: report['dma_preparation_test'] = result
            except (OSError, RuntimeError, ValueError) as exc:
                report['cpu_memory_test'] = {'state': 'failed', 'error': str(exc), 'gpu_execution_tested': False}
                if dma_test: report['dma_preparation_test'] = report['cpu_memory_test']
        cursor = 0
        for _ in range(8):
            try: batch = decode_batch(call(3, TraceBatch(), (cursor,)))
            except (OSError, RuntimeError, ValueError) as exc:
                report['trace_error'] = str(exc)
                break
            report['trace'].extend(batch['records'])
            report['trace_overwritten'] = max(report['trace_overwritten'], batch['overwritten'])
            if not batch['records']: break
            cursor = batch['next_sequence'] - 1
        return report
    finally:
        if connection.value: kit.IOServiceClose(connection.value)
        if service: kit.IOObjectRelease(service)
        kit.IOObjectRelease(iterator.value)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--memory-test', action='store_true', help='Test the private 4 KiB CPU buffer; never submit GPU work')
    parser.add_argument('--dma-test', action='store_true', help='Explicit 4 KiB IOKit DMA preparation probe; requires driver opt-in; never bind GPU memory')
    parser.add_argument('--minutes', type=int, default=10, choices=range(1, 61))
    args = parser.parse_args()
    if sys.platform != 'darwin': raise SystemExit('Dieser Sammler benötigt natives macOS; hier wurde keine Geräteprobe ausgeführt.')
    report = {'schema': 1, 'synthetic': False, 'collector_platform': platform.machine(),
              'metal_execution_verified': False, 'firmware_secure_boot_verified': False}
    for name, option in [('macos_version', '-productVersion'), ('macos_build', '-buildVersion')]:
        status, data = command(['/usr/bin/sw_vers', option])
        if status == 0: report[name] = data.decode('utf-8', 'replace').strip()
    try: report.update(collect_iokit(args.memory_test, args.dma_test))
    except (OSError, RuntimeError, ValueError) as exc: report['iokit_error'] = str(exc)
    status, data = command(['/usr/sbin/ioreg', '-r', '-c', 'LunarBridgePCI', '-a'])
    if status == 0:
        try: report['registry'] = filtered_registry(plistlib.loads(data))
        except plistlib.InvalidFileException: report['registry'] = []
    status, data = command(['/usr/bin/log', 'show', '--last', f'{args.minutes}m', '--style', 'json',
                            '--predicate', 'eventMessage CONTAINS "[LunarBridge]"'])
    report['kernel_events'] = []
    if status == 0:
        try:
            entries = json.loads(data)
            report['kernel_events'] = parse_kernel_messages([r.get('eventMessage', '') for r in entries if isinstance(r, dict)])
        except (ValueError, TypeError): report['kernel_log_parse_failed'] = True
    else: report['kernel_log_status'] = status
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'diagnostics.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
    (args.out / 'FEHLERBERICHT.md').write_text(markdown(report), encoding='utf-8', newline='\n')
    print('LunarBridge-Bericht gespeichert: ' + str(args.out))
    if not report.get('snapshot'): raise SystemExit(2)

if __name__ == '__main__': main()
