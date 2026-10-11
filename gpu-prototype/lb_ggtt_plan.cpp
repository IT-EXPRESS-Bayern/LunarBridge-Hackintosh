// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Independently written CPU planner. Intel MIT numeric/format references:
// Linux e5f0a698b34ed76002dc5cff3804a61c80233a7a, xe_ggtt.c,
// xe_gtt_defs.h, xe_pat.c and xe_wopcm.c; see GGTT_BINDING_SOURCES.json.
#include "lb_ggtt_plan.h"

namespace {
bool valid_span(const void *pointer, uint64_t bytes) {
    const uintptr_t start = reinterpret_cast<uintptr_t>(pointer);
    return pointer && bytes && bytes <= UINTPTR_MAX - start;
}
bool overlap(const void *a, uint64_t aBytes, const void *b, uint64_t bBytes) {
    const uintptr_t aStart = reinterpret_cast<uintptr_t>(a);
    const uintptr_t bStart = reinterpret_cast<uintptr_t>(b);
    return aStart < bStart + bBytes && bStart < aStart + aBytes;
}
const LBVmMapping *mapping_for(const LBVmArena *arena, uint64_t token) {
    // Called only after the existing Core checked owner, count and corruption.
    for (uint32_t i = 0; i < arena->count; ++i)
        if (arena->mappings[i].token == token) return &arena->mappings[i];
    return nullptr;
}
}

extern "C" uint32_t lb_ggtt_build_plan(
    const LBVmArena *sourceArena, uint64_t sourceToken,
    const LBVmArena *ggttArena, uint64_t ggttToken, uint32_t owner,
    const LBDmaSegment *segments, uint32_t segmentCount,
    const uint64_t *stagedPpgtt, uint32_t stagedCount,
    const LBGgttWindow *window, LBGgttPlan *out, uint32_t outputBytes) {
    if (!sourceArena || !ggttArena || !owner || !sourceToken || !ggttToken ||
        !segments || !stagedPpgtt || !window || !out ||
        !segmentCount || segmentCount > LB_GGTT_MAX_WRITES ||
        !stagedCount || stagedCount > LB_GGTT_MAX_WRITES ||
        outputBytes != sizeof(LBGgttPlan)) return LBVmInput;
    const uint64_t segmentBytes = uint64_t(segmentCount) * sizeof(LBDmaSegment);
    const uint64_t stagedBytes = uint64_t(stagedCount) * sizeof(uint64_t);
    if (!valid_span(sourceArena, sizeof(*sourceArena)) ||
        !valid_span(ggttArena, sizeof(*ggttArena)) ||
        !valid_span(segments, segmentBytes) || !valid_span(stagedPpgtt, stagedBytes) ||
        !valid_span(window, sizeof(*window)) || !valid_span(out, sizeof(*out))) return LBVmRange;
    if ((reinterpret_cast<uintptr_t>(sourceArena) | reinterpret_cast<uintptr_t>(ggttArena) |
         reinterpret_cast<uintptr_t>(segments) | reinterpret_cast<uintptr_t>(stagedPpgtt) |
         reinterpret_cast<uintptr_t>(window) | reinterpret_cast<uintptr_t>(out)) & 7) return LBVmAlignment;
    if (overlap(sourceArena, sizeof(*sourceArena), ggttArena, sizeof(*ggttArena)) ||
        overlap(out, sizeof(*out), sourceArena, sizeof(*sourceArena)) ||
        overlap(out, sizeof(*out), ggttArena, sizeof(*ggttArena)) ||
        overlap(out, sizeof(*out), segments, segmentBytes) ||
        overlap(out, sizeof(*out), stagedPpgtt, stagedBytes) ||
        overlap(out, sizeof(*out), window, sizeof(*window))) return LBGgttAliasing;

    // Reuse actual Core validation, including exact sum, segment alignment,
    // DMA-46 bounds, no self-alias and owner/token/corrupt-arena checks.
    uint64_t expected[LB_GGTT_MAX_WRITES]{};
    uint32_t status = lb_vm_stage_ptes(sourceArena, owner, sourceToken,
        segments, segmentCount, expected, LB_GGTT_MAX_WRITES);
    if (status) return status;
    const LBVmMapping *source = mapping_for(sourceArena, sourceToken);
    if (!source) return LBVmNotFound;
    const uint32_t count = uint32_t(source->bytes / LB_GPU_PAGE);
    if (stagedCount != count) return LBGgttPteMismatch;
    for (uint32_t i = 0; i < count; ++i)
        if (stagedPpgtt[i] != expected[i]) return LBGgttPteMismatch;

    // The target arena is a distinct GLOBAL address-space reservation. The
    // Core validates its accounting separately, not by trusting a copied VA.
    status = lb_vm_stage_ptes(ggttArena, owner, ggttToken,
        segments, segmentCount, expected, LB_GGTT_MAX_WRITES);
    if (status) return status;
    const LBVmMapping *target = mapping_for(ggttArena, ggttToken);
    if (!target) return LBVmNotFound;
    if (source->bytes != target->bytes) return LBVmRange;
    // GGTT has no per-entry read-only protection. Never silently widen a
    // source read-only mapping or introduce aliases with another cache policy.
    if (source->writable != 1 || target->writable != 1 ||
        source->patIndex != LB_GGTT_XE2_WB_PAT || target->patIndex != LB_GGTT_XE2_WB_PAT)
        return LBGgttPolicy;

    if (window->abi != LB_GGTT_PLAN_ABI || window->structBytes != sizeof(*window) ||
        window->addressSpaceKind != LB_GGTT_SPACE_GLOBAL ||
        window->reserved[0] || window->reserved[1] || window->reserved[2] ||
        window->gsmBarOffset != LB_GGTT_GSM_BAR_OFFSET || window->gsmBytes != LB_GGTT_GSM_BYTES ||
        window->gpuAddressLimit != LB_GGTT_GUC_TOP ||
        window->reservedLowBytes < LB_GGTT_RESERVED_LOW ||
        window->reservedLowBytes >= window->gpuAddressLimit ||
        (window->reservedLowBytes & (LB_GPU_PAGE - 1))) return LBGgttWindowInvalid;
    if (window->patIndex != LB_GGTT_XE2_WB_PAT || window->patValue != LB_GGTT_XE2_WB_PAT_VALUE)
        return LBGgttPolicy;
    if (window->gsmBarOffset >= window->mappedBarBytes ||
        window->gsmBytes > window->mappedBarBytes - window->gsmBarOffset) return LBGgttWindowInvalid;
    // Never consider a PPGTT range an implicit GGTT reservation. The entire
    // supplied target arena must stay within the explicit reserved GGTT space.
    if (ggttArena->begin < window->reservedLowBytes ||
        ggttArena->end > window->gpuAddressLimit ||
        target->gpuAddress < window->reservedLowBytes ||
        target->gpuAddress >= window->gpuAddressLimit ||
        target->bytes > window->gpuAddressLimit - target->gpuAddress) return LBGgttWindowInvalid;
    const uint64_t firstIndex = target->gpuAddress / LB_GPU_PAGE;
    const uint64_t availableEntries = window->gsmBytes / sizeof(uint64_t);
    if (firstIndex >= availableEntries || count > availableEntries - firstIndex) return LBGgttWindowInvalid;
    const uint64_t relativeBegin = firstIndex * sizeof(uint64_t);
    const uint64_t relativeBytes = uint64_t(count) * sizeof(uint64_t);
    if (relativeBegin > window->gsmBytes || relativeBytes > window->gsmBytes - relativeBegin ||
        relativeBegin > UINT64_MAX - window->gsmBarOffset) return LBGgttWindowInvalid;
    const uint64_t barBegin = window->gsmBarOffset + relativeBegin;
    if (barBegin >= window->mappedBarBytes || relativeBytes > window->mappedBarBytes - barBegin)
        return LBGgttWindowInvalid;

    LBGgttPlan result{};
    result.abi = LB_GGTT_PLAN_ABI;
    result.structBytes = sizeof(result);
    result.owner = owner;
    result.sourceToken = sourceToken;
    result.ggttToken = ggttToken;
    result.sourceGPUAddress = source->gpuAddress;
    result.ggttGPUAddress = target->gpuAddress;
    result.bytes = source->bytes;
    result.firstPteIndex = firstIndex;
    result.barWriteBegin = barBegin;
    result.barWriteEndExclusive = barBegin + relativeBytes;
    result.writeCount = count;
    result.patIndex = LB_GGTT_XE2_WB_PAT;
    result.expectedPatValue = LB_GGTT_XE2_WB_PAT_VALUE;
    result.requiredQualifications = LB_GGTT_REQUIRED_QUALIFICATIONS;
    uint32_t index = 0;
    for (uint32_t i = 0; i < segmentCount; ++i) {
        for (uint64_t offset = 0; offset < segments[i].bytes; offset += LB_GPU_PAGE) {
            uint64_t pte = 0;
            status = lb_xe2_page_entry(LBGgtt4K, segments[i].address + offset,
                LB_GGTT_XE2_WB_PAT, 1, &pte);
            if (status) return status;
            // System DMA: GGTT DM bit 1 must be clear; PAT1 is bit 53. This
            // catches a future Core encoder regression before committing.
            const uint64_t expectedGgtt = (segments[i].address + offset) | uint64_t(1) | (uint64_t(1) << 53);
            if (pte != expectedGgtt) return LBGgttPteMismatch;
            result.writes[index] = {barBegin + uint64_t(index) * sizeof(uint64_t), pte};
            ++index;
        }
    }
    if (index != count) return LBVmCorrupt;
    // Single logical commit, not a lock-free atomic memory store. Caller
    // serialization is required; no hardware-bound/flush/ownership bit is set.
    *out = result;
    return LBVmOK;
}
