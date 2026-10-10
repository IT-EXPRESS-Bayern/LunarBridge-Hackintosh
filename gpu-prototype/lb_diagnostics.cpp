// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#include "lb_diagnostics.h"

extern "C" uint32_t lb_mmio_preflight(const LBMMIOInput *p, uint64_t barBytes,
                                      uint32_t *gfx, uint32_t *media) {
    if (!p || !gfx || !media) return LBBadInput;
    if (p->optIn != 1) return LBOptInMissing;
    if (p->vendor != 0x8086 || p->device != 0x64a0) return LBUnsupportedPCI;
    if (p->subvendor != 0x144d || p->subdevice != 0xc1dd) return LBWrongSubsystem;
    if (p->classCode != 0x030000 && p->classCode != 0x038000) return LBWrongClass;
    if (p->command >= 0xffff || p->pmcsr > 0xffff) return LBBadInput;
    if (!(p->command & 0x2)) return LBMemoryDecodeOff;
    if (p->pmCapabilityPresent != 1 || p->pmcsr == 0xffff) return LBPowerUnknown;
    if ((p->pmcsr & 3) != 0) return LBNotD0;
    if (p->providerPowerState != 2) return LBProviderNotOn;
    uint32_t a, b;
    if (!lnl_gmd_read_offset(LnlRenderGt, barBytes, &a) ||
        !lnl_gmd_read_offset(LnlMediaGt, barBytes, &b)) return LBBarTooSmall;
    *gfx = a;
    *media = b;
    return LBReadAllowed;
}

extern "C" int lb_buffer_request(uint64_t requested, uint64_t reserved, uint64_t *rounded) {
    if (!rounded || requested == 0 || requested > LB_BUFFER_MAX_BYTES ||
        reserved > LB_BUFFER_POOL_BYTES) return 0;
    const uint64_t bytes = (requested + 4095u) & ~uint64_t(4095u);
    if (bytes > LB_BUFFER_MAX_BYTES || bytes > LB_BUFFER_POOL_BYTES - reserved) return 0;
    *rounded = bytes;
    return 1;
}

extern "C" int lb_trace_init(LBTraceRing *ring) {
    if (!ring) return 0;
    ring->nextSequence = 1;
    ring->overwritten = 0;
    ring->count = 0;
    ring->reserved = 0;
    // Unused record storage is never exposed by lb_trace_read.
    return 1;
}

extern "C" uint64_t lb_trace_push(LBTraceRing *ring, uint64_t ticks, uint32_t event,
                                   uint32_t status, uint32_t context, uint64_t arg0, uint64_t arg1) {
    if (!ring || ring->nextSequence == 0 || ring->nextSequence == UINT64_MAX ||
        ring->count > LB_TRACE_CAPACITY) return 0;
    const uint64_t sequence = ring->nextSequence++;
    LBTraceRecord &record = ring->records[(sequence - 1) % LB_TRACE_CAPACITY];
    record = {sequence, ticks, arg0, arg1, event, status, context, 0};
    if (ring->count < LB_TRACE_CAPACITY) ++ring->count;
    else ++ring->overwritten;
    return sequence;
}

extern "C" int lb_trace_read(const LBTraceRing *ring, uint64_t since, LBTraceBatchResult *out) {
    if (!ring || !out || ring->nextSequence == 0 || ring->count > LB_TRACE_CAPACITY ||
        ring->nextSequence <= ring->count || since >= ring->nextSequence) return 0;
    const uint64_t oldest = ring->nextSequence - ring->count;
    const uint64_t begin = since < oldest ? oldest : since + 1;
    LBTraceBatchResult result{};
    result.abi = LB_ABI_VERSION;
    result.oldestSequence = oldest;
    result.nextSequence = begin;
    result.overwritten = ring->overwritten;
    for (uint64_t sequence = begin; sequence < ring->nextSequence && result.count < LB_TRACE_BATCH;
         ++sequence) {
        const LBTraceRecord &record = ring->records[(sequence - 1) % LB_TRACE_CAPACITY];
        if (record.sequence != sequence) return 0;
        result.records[result.count++] = record;
        result.nextSequence = sequence + 1;
    }
    *out = result;
    return 1;
}
