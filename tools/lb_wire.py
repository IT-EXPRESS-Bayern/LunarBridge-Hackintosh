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

class ArcOpDescriptor(c.Structure):
    _fields_ = [(n, c.c_uint32) for n in (
        'op', 'selector', 'scalarInputCount', 'scalarOutputCount', 'structInputBytes',
        'structOutputBytes', 'differences', 'requiredQualifications', 'linuxIoctlNr',
        'linuxConstructs', 'linuxDirection', 'reserved')]

class ArcInterfaceReport(c.Structure):
    _fields_ = [(n, c.c_uint32) for n in (
        'abi', 'structBytes', 'opCount', 'differences', 'linuxUapiVersion', 'darwinKernelMajor',
        'scalarLimit', 'maxSegments', 'maxSyncs', 'unmappedLinuxIoctls',
        'gpuExecutionImplemented', 'metalProviderImplemented')] + [('ops', ArcOpDescriptor * 11)]

class ArcDecision(c.Structure):
    _fields_ = [(n, c.c_uint32) for n in (
        'abi', 'structBytes', 'op', 'result', 'reason', 'differences',
        'requiredQualifications', 'acceptedSegments')] + [
        (n, c.c_uint64) for n in ('translatedBytes', 'translatedGpuAddress')] + [
        (n, c.c_uint32) for n in (
            'pageCount', 'ggttWriteCount', 'expectedPatValue', 'fenceSlots',
            'hardwareBound', 'gpuExecuted', 'metalVerified', 'reserved')]

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
    52: 'forcewake_cleanup_completed', 53: 'forcewake_cleanup_failed',
    60: 'arc_interface_queried', 61: 'arc_request_accepted', 62: 'arc_request_rejected'
}
WAKE_STATUS = {
    0: 'success', 1: 'invalid_callbacks', 2: 'invalid_state', 3: 'existing_request_or_pending_cleanup',
    4: 'register_access_failed', 5: 'invalid_all_ones_mmio', 6: 'ack_timeout',
    7: 'clock_not_monotonic', 8: 'not_attempted', 9: 'unexpected_graphics_or_media_ip'
}
ARC_OPERATIONS = {
    1: 'device_query', 2: 'resource_create', 3: 'resource_map', 4: 'vm_create',
    5: 'vm_destroy', 6: 'vm_bind', 7: 'queue_create', 8: 'queue_destroy',
    9: 'queue_property', 10: 'exec', 11: 'fence_wait'
}
ARC_RESULTS = {
    0: 'accepted', 1: 'invalid_input', 2: 'abi_mismatch', 3: 'wrong_owner',
    4: 'unknown_operation', 5: 'invalid_call_shape', 6: 'linux_only_construct',
    7: 'unsupported', 8: 'out_of_range', 9: 'misaligned', 10: 'over_capacity',
    11: 'policy_refused', 12: 'aliasing_buffers'
}
ARC_REASONS = {
    0: 'none', 1: 'field_outside_operation', 2: 'user_pointer', 3: 'sync_object',
    4: 'mmap_offset', 5: 'handle_namespace', 6: 'variable_structure', 7: 'scalar_count',
    8: 'async_unsupported', 9: 'pat_index', 10: 'engine_class', 11: 'segment_sum',
    12: 'page_alignment', 13: 'fence_address', 14: 'timeout', 15: 'unmapped_operation',
    16: 'missing_token', 17: 'global_window', 18: 'batch_alignment', 19: 'capacity',
    20: 'flags', 21: 'no_completion_signal'
}
ARC_DIFFERENCES = {
    1 << 0: 'fixed_structure_size', 1 << 1: 'no_extension_chain', 1 << 2: 'no_user_pointer',
    1 << 3: 'flattened_vector', 1 << 4: 'no_sync_object', 1 << 5: 'no_mmap_offset',
    1 << 6: 'owner_bound_token', 1 << 7: 'explicit_pat', 1 << 8: 'async_reference',
    1 << 9: 'bounded_timeout', 1 << 10: 'no_render_node', 1 << 11: 'entitlement_gate'
}
ARC_QUALIFICATIONS = {
    1 << 0: 'device_mapper', 1 << 1: 'ggtt_ownership', 1 << 2: 'pat_programmed',
    1 << 3: 'guc_running', 1 << 4: 'lrc_prepared', 1 << 5: 'fence_memory',
    1 << 6: 'fuse_topology', 1 << 7: 'metal_provider', 1 << 8: 'display_engine'
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

def _named_bits(value, names):
    if value & ~sum(names): raise ValueError('Unknown Arc interface bit reported')
    return [name for bit, name in sorted(names.items()) if value & bit]

def decode_arc_interface(data):
    if len(data) != c.sizeof(ArcInterfaceReport): raise ValueError('Arc interface length mismatch')
    r = ArcInterfaceReport.from_buffer_copy(data)
    if r.abi != 1 or r.structBytes != c.sizeof(ArcInterfaceReport) or r.opCount != 11 or \
       r.gpuExecutionImplemented or r.metalProviderImplemented:
        raise ValueError('Arc interface ABI mismatch')
    operations = []
    for row in r.ops[:r.opCount]:
        if row.op not in ARC_OPERATIONS or row.reserved or row.scalarInputCount or \
           row.scalarOutputCount or row.linuxDirection not in (1, 2, 3):
            raise ValueError('Arc operation descriptor mismatch')
        operations.append({
            'operation': ARC_OPERATIONS[row.op], 'selector': int(row.selector),
            'struct_input_bytes': int(row.structInputBytes),
            'struct_output_bytes': int(row.structOutputBytes),
            'linux_ioctl_nr': int(row.linuxIoctlNr),
            'linux_direction': {1: 'write', 2: 'read', 3: 'read_write'}[row.linuxDirection],
            'differences': _named_bits(row.differences, ARC_DIFFERENCES),
            'missing_qualifications': _named_bits(row.requiredQualifications, ARC_QUALIFICATIONS)})
    return {'abi': int(r.abi), 'operation_count': int(r.opCount),
            'linux_uapi_version': f'{r.linuxUapiVersion >> 16}.{r.linuxUapiVersion & 0xffff}',
            'darwin_kernel_major': int(r.darwinKernelMajor),
            'scalar_limit': int(r.scalarLimit), 'max_segments': int(r.maxSegments),
            'max_syncs': int(r.maxSyncs), 'unmapped_linux_ioctls': int(r.unmappedLinuxIoctls),
            'differences': _named_bits(r.differences, ARC_DIFFERENCES),
            'operations': operations, 'gpu_execution_implemented': False,
            'metal_provider_implemented': False}

def decode_arc_decision(data):
    if len(data) != c.sizeof(ArcDecision): raise ValueError('Arc decision length mismatch')
    d = ArcDecision.from_buffer_copy(data)
    if d.abi != 1 or d.structBytes != c.sizeof(ArcDecision) or d.op not in ARC_OPERATIONS or \
       d.result not in ARC_RESULTS or d.reason not in ARC_REASONS or d.reserved or \
       d.hardwareBound or d.gpuExecuted or d.metalVerified:
        raise ValueError('Arc decision ABI mismatch')
    if d.result and (d.translatedBytes or d.translatedGpuAddress or d.pageCount or
                     d.ggttWriteCount or d.fenceSlots or d.acceptedSegments):
        raise ValueError('Refused Arc request reported translated values')
    return {'operation': ARC_OPERATIONS[d.op], 'result': ARC_RESULTS[d.result],
            'reason': ARC_REASONS[d.reason],
            'differences': _named_bits(d.differences, ARC_DIFFERENCES),
            'missing_qualifications': _named_bits(d.requiredQualifications, ARC_QUALIFICATIONS),
            'accepted_segments': int(d.acceptedSegments),
            'translated_bytes': int(d.translatedBytes),
            'translated_gpu_address': int(d.translatedGpuAddress),
            'page_count': int(d.pageCount), 'ggtt_write_count': int(d.ggttWriteCount),
            'expected_pat_value': int(d.expectedPatValue), 'fence_slots': int(d.fenceSlots),
            'hardware_bound': False, 'gpu_executed': False, 'metal_verified': False}

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
