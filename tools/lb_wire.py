# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Versioned, bounded native diagnostic ABI and event decoding."""
import ctypes as c
import re

class Version(c.Structure):
    _fields_ = [(n, c.c_uint32) for n in ('architecture', 'release', 'revision', 'version_x100')]

class Snapshot(c.Structure):
    _fields_ = [('barBytes', c.c_uint64)] + [(n, c.c_uint32) for n in (
        'abi', 'structBytes', 'vendor', 'device', 'subvendor', 'subdevice', 'revision',
        'classCode', 'pciCommand', 'pmcsr', 'flags', 'policy')] + \
        [('graphics', Version), ('media', Version)] + [(n, c.c_uint32) for n in (
        'gmdRawGraphics', 'gmdRawMedia', 'capabilities', 'reserved')]

class TraceRecord(c.Structure):
    _fields_ = [(n, c.c_uint64) for n in ('sequence', 'ticks', 'arg0', 'arg1')] + \
               [(n, c.c_uint32) for n in ('event', 'status', 'context', 'reserved')]

class TraceBatch(c.Structure):
    _fields_ = [('abi', c.c_uint32), ('count', c.c_uint32), ('oldestSequence', c.c_uint64),
               ('nextSequence', c.c_uint64), ('overwritten', c.c_uint64), ('records', TraceRecord * 16)]

class BufferInfo(c.Structure):
    _fields_ = [('bytes', c.c_uint64)] + [(n, c.c_uint32) for n in ('abi', 'alignment', 'cpuShared', 'gpuBound')]

class DMAInfo(c.Structure):
    _fields_ = [('bytes', c.c_uint64)] + [(n, c.c_uint32) for n in (
        'abi', 'segments', 'pteEntries', 'deviceMapperUsed', 'dmaPrepared', 'gpuBound')]

class ForceWakeInfo(c.Structure):
    _fields_ = [(n, c.c_uint32) for n in (
        'abi', 'structBytes', 'enabled', 'attempted', 'preflight', 'primaryStatus',
        'cleanupStatus', 'completed', 'reads', 'requestWrites', 'flushWrites', 'lastAck',
        'polls', 'resourcesRetained', 'gpuExecuted', 'metalVerified', 'graphicsIP',
        'mediaIP', 'leaseReferences', 'reserved')]

EVENTS = {
    1: 'driver_start', 2: 'pci_identified', 3: 'mmio_read_blocked', 4: 'bar_mapping_failed',
    5: 'gmd_registers_read', 6: 'gmd_values_invalid', 7: 'diagnostics_ready', 8: 'driver_stop',
    20: 'client_opened', 21: 'client_closed', 22: 'cpu_buffer_requested',
    23: 'cpu_buffer_allocated', 24: 'cpu_buffer_failed', 25: 'cpu_mapping_requested',
    26: 'method_rejected', 27: 'gpu_submit_unsupported', 28: 'buffer_reference_released',
    29: 'client_rejected', 40: 'dma_prepared', 41: 'dma_preparation_failed',
    42: 'dma_reference_released', 43: 'dma_release_failed',
    50: 'forcewake_handshake_completed', 51: 'forcewake_handshake_failed',
    52: 'forcewake_cleanup_completed', 53: 'forcewake_cleanup_failed'
}
WAKE_STATUS = {
    0: 'success', 1: 'invalid_callbacks', 2: 'invalid_state', 3: 'existing_request_or_pending_cleanup',
    4: 'register_access_failed', 5: 'invalid_all_ones_mmio', 6: 'ack_timeout',
    7: 'clock_not_monotonic', 8: 'not_attempted', 9: 'unexpected_graphics_or_media_ip'
}
POLICIES = {
    0: 'allowed_by_preflight', 1: 'explicit_opt_in_missing', 2: 'unsupported_pci',
    3: 'wrong_subsystem', 4: 'wrong_device_class', 5: 'pci_memory_decode_off',
    6: 'pci_power_unknown', 7: 'pci_not_d0', 8: 'provider_not_on', 9: 'bar_too_small',
    10: 'provider_busy', 11: 'invalid_input'
}

def record_dict(row):
    return {'sequence': int(row.sequence), 'ticks': int(row.ticks), 'event': int(row.event),
            'name': EVENTS.get(row.event, 'unknown_event'), 'status': f'0x{row.status:08x}',
            'context': int(row.context), 'arg0': int(row.arg0), 'arg1': int(row.arg1)}

def decode_snapshot(data):
    if len(data) != c.sizeof(Snapshot): raise ValueError('Snapshot length mismatch')
    s = Snapshot.from_buffer_copy(data)
    if s.abi != 1 or s.structBytes != c.sizeof(Snapshot) or s.reserved:
        raise ValueError('Snapshot ABI mismatch')
    result = {name: int(getattr(s, name)) for name, _ in s._fields_ if name not in {'graphics', 'media'}}
    for name in ('graphics', 'media'):
        v = getattr(s, name)
        result[name] = {key: int(getattr(v, key)) for key, _ in v._fields_}
    result['policy_name'] = POLICIES.get(s.policy, 'unknown_policy')
    result['claims_gpu_execution'] = bool(s.capabilities & 0x10000)
    result['claims_metal'] = bool(s.capabilities & 0x20000)
    result['metal_execution_verified'] = False
    result['dma_preparation_enabled'] = bool(s.flags & 32)
    result['forcewake_probe_enabled'] = bool(s.flags & 64)
    return result

def decode_forcewake_info(data):
    if len(data) != c.sizeof(ForceWakeInfo): raise ValueError('Forcewake info length mismatch')
    w = ForceWakeInfo.from_buffer_copy(data)
    if w.abi != 1 or w.structBytes != 80 or w.reserved or w.gpuExecuted or w.metalVerified or \
       any(getattr(w, n) not in (0, 1) for n in ('enabled', 'attempted', 'completed', 'resourcesRetained')) or \
       w.primaryStatus not in WAKE_STATUS or w.cleanupStatus not in WAKE_STATUS or w.leaseReferences > 65535:
        raise ValueError('Forcewake info ABI/flags mismatch')
    if w.attempted and not w.enabled or w.leaseReferences and not w.resourcesRetained or \
       w.completed != int(w.attempted and w.primaryStatus == 0 and w.cleanupStatus == 0) or \
       w.completed and (w.resourcesRetained or w.leaseReferences or w.lastAck == 0xffffffff or w.lastAck & 1) or \
       w.flushWrites < 4 * w.reads or w.polls > w.reads or \
       not w.enabled and (w.requestWrites or w.attempted or w.resourcesRetained):
        raise ValueError('Forcewake info state mismatch')
    return {'enabled': bool(w.enabled), 'attempted': bool(w.attempted), 'completed': bool(w.completed),
            'preflight': int(w.preflight), 'primary_status': WAKE_STATUS[w.primaryStatus],
            'cleanup_status': WAKE_STATUS[w.cleanupStatus], 'reads': int(w.reads),
            'request_writes': int(w.requestWrites), 'lnl_read_flush_writes': int(w.flushWrites),
            'last_ack': int(w.lastAck), 'polls': int(w.polls),
            'resources_retained': bool(w.resourcesRetained), 'lease_references': int(w.leaseReferences),
            'graphics_ip': int(w.graphicsIP), 'media_ip': int(w.mediaIP),
            'gpu_execution_tested': False, 'metal_execution_verified': False}

def decode_dma_info(data):
    if len(data) != c.sizeof(DMAInfo): raise ValueError('DMA info length mismatch')
    d = DMAInfo.from_buffer_copy(data)
    if d.abi != 1 or d.gpuBound != 0 or d.deviceMapperUsed not in (0, 1) or \
       d.dmaPrepared not in (0, 1) or d.deviceMapperUsed != d.dmaPrepared:
        raise ValueError('DMA info ABI/flags mismatch')
    if d.dmaPrepared:
        if not d.bytes or d.bytes > 1024*1024 or d.bytes % 4096 or \
           d.segments != d.bytes // 4096 or d.pteEntries != d.segments:
            raise ValueError('DMA info page/count mismatch')
    elif d.bytes or d.segments or d.pteEntries:
        raise ValueError('DMA info inactive state mismatch')
    return {'bytes': int(d.bytes), 'segments': int(d.segments), 'pte_entries': int(d.pteEntries),
            'device_mapper_used': bool(d.deviceMapperUsed), 'dma_prepared': bool(d.dmaPrepared),
            'gpu_bound': False, 'gpu_execution_tested': False, 'dma_cleanup_verified': False}

def decode_batch(data):
    if len(data) != c.sizeof(TraceBatch): raise ValueError('Trace length mismatch')
    b = TraceBatch.from_buffer_copy(data)
    if b.abi != 1 or b.count > 16 or b.oldestSequence < 1 or b.nextSequence < b.oldestSequence:
        raise ValueError('Trace ABI/count mismatch')
    rows = list(b.records[:b.count])
    if rows and (rows[0].sequence < b.oldestSequence or b.nextSequence != rows[-1].sequence + 1):
        raise ValueError('Trace cursor mismatch')
    if any(row.reserved for row in rows) or any(y.sequence != x.sequence + 1 for x, y in zip(rows, rows[1:])) or \
       any(bytes(row) != bytes(c.sizeof(TraceRecord)) for row in b.records[b.count:]):
        raise ValueError('Trace ordering/reserved mismatch')
    return {'oldest_sequence': int(b.oldestSequence), 'next_sequence': int(b.nextSequence),
            'overwritten': int(b.overwritten), 'records': [record_dict(row) for row in rows]}

LOG_PATTERN = re.compile(r'\[LunarBridge\] seq=(\d{1,20}) event=(\d{1,10}) status=0x([0-9a-fA-F]{8}) '
                         r'context=(\d{1,10}) a0=(\d{1,20}) a1=(\d{1,20})(?:\s|$)')

def parse_kernel_messages(messages):
    rows = []
    for message in messages:
        if not isinstance(message, str): continue
        match = LOG_PATTERN.search(message)
        if not match: continue
        seq, event, status, context, a0, a1 = match.groups()
        values = [int(seq), int(event), int(context), int(a0), int(a1)]
        if any(v >= 2**64 for v in (values[0], values[3], values[4])) or \
           any(v >= 2**32 for v in (values[1], values[2])): continue
        row = TraceRecord(values[0], 0, values[3], values[4], values[1], int(status, 16), values[2], 0)
        rows.append(record_dict(row))
    return rows[-256:]
