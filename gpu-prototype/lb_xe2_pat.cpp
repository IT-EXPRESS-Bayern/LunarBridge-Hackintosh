// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Table values and register offsets derived from Intel MIT source:
// Linux v6.17 drivers/gpu/drm/xe/xe_pat.c (Copyright 2023 Intel Corporation).
// See ../docs/ARC_PORT_SOURCES.json and ../LICENSES/MIT-Intel.txt.
#include "lb_xe2_pat.h"

namespace {
// XE2_PAT(no_promote, comp_en, l3clos, l3_policy, l4_policy, coh_mode) with
// XE2_NO_PROMOTE = bit 10, XE2_COMP_EN = bit 9, XE2_L3_CLOS = bits 7:6,
// XE2_L3_POLICY = bits 5:4, XE2_L4_POLICY = bits 3:2, XE2_COH_MODE = bits 1:0.
constexpr uint32_t pat(uint32_t noPromote, uint32_t compEn, uint32_t l3clos,
                       uint32_t l3Policy, uint32_t l4Policy, uint32_t cohMode) {
    return (noPromote ? 1u << 10 : 0u) | (compEn ? 1u << 9 : 0u) |
           ((l3clos & 3u) << 6) | ((l3Policy & 3u) << 4) |
           ((l4Policy & 3u) << 2) | (cohMode & 3u);
}

// xe2_pat_table[] in table order. Indices 16..19 are absent upstream and are
// therefore zero here: the source states reserved entries must be programmed
// with maximum caching and minimum coherency, which is the all-zero encoding.
constexpr uint32_t kTable[LB_PAT_ENTRIES] = {
    pat(0, 0, 0, 0, 3, 0), pat(0, 0, 0, 0, 3, 2), pat(0, 0, 0, 0, 3, 3),
    pat(0, 0, 0, 3, 3, 0), pat(0, 0, 0, 3, 0, 2), pat(0, 0, 0, 3, 3, 2),
    pat(1, 0, 0, 1, 3, 0), pat(0, 0, 0, 3, 0, 3), pat(0, 0, 0, 3, 0, 0),
    pat(0, 1, 0, 0, 3, 0), pat(0, 1, 0, 3, 0, 0), pat(1, 1, 0, 1, 3, 0),
    pat(0, 1, 0, 3, 3, 0), pat(0, 0, 0, 0, 0, 0), pat(0, 1, 0, 0, 0, 0),
    pat(1, 1, 0, 1, 1, 0),
    0, 0, 0, 0,  // 16..19 reserved
    pat(0, 0, 1, 0, 3, 0), pat(0, 1, 1, 0, 3, 0), pat(0, 0, 1, 0, 3, 2),
    pat(0, 0, 1, 0, 3, 3), pat(0, 0, 2, 0, 3, 0), pat(0, 1, 2, 0, 3, 0),
    pat(0, 0, 2, 0, 3, 2), pat(0, 0, 2, 0, 3, 3), pat(0, 0, 3, 0, 3, 0),
    pat(0, 1, 3, 0, 3, 0), pat(0, 0, 3, 0, 3, 2), pat(0, 0, 3, 0, 3, 3)
};
// xe2_pat_ats and xe2_pat_pta are single entries, not table indices.
constexpr uint32_t kAtsValue = pat(0, 0, 0, 0, 3, 3);
constexpr uint32_t kPtaValue = pat(0, 0, 0, 0, 3, 0);

// The write-back index must stay the one the GGTT planner already encodes.
static_assert(kTable[LB_PAT_INDEX_WB] == pat(0, 0, 0, 0, 3, 3), "WB entry drifted");
static_assert(kTable[LB_PAT_INDEX_WB] == 0x0F, "WB encoding drifted");
static_assert(kTable[LB_PAT_INDEX_NONE] == 0x3C, "uncached entry drifted");
static_assert(kTable[LB_PAT_INDEX_WT] == 0x614, "write-through entry drifted");
static_assert(kTable[LB_PAT_INDEX_NONE_COMPRESSION] == 0x23C, "compression entry drifted");
static_assert(kAtsValue == kTable[LB_PAT_INDEX_WB], "ATS equals the WB encoding upstream");

bool valid_span(const void *pointer, uint64_t bytes) {
    const uintptr_t start = reinterpret_cast<uintptr_t>(pointer);
    return pointer && bytes && bytes <= UINTPTR_MAX - start;
}
bool overlap(const void *a, uint64_t aBytes, const void *b, uint64_t bBytes) {
    const uintptr_t aStart = reinterpret_cast<uintptr_t>(a);
    const uintptr_t bStart = reinterpret_cast<uintptr_t>(b);
    return aStart < bStart + bBytes && bStart < aStart + aBytes;
}
uint32_t index_offset(uint32_t index) {
    return index < LB_PAT_RANGE_SPLIT
        ? LB_PAT_INDEX_RANGE0 + index * 4
        : LB_PAT_INDEX_RANGE1 + (index - LB_PAT_RANGE_SPLIT) * 4;
}
}  // namespace

extern "C" uint32_t lb_xe2_pat_entry_value(uint32_t index, uint32_t *out) {
    if (!out || (reinterpret_cast<uintptr_t>(out) & 3)) return LBPatInput;
    if (index >= LB_PAT_ENTRIES) return LBPatIndex;
    *out = kTable[index];
    return LBPatOK;
}

extern "C" uint32_t lb_xe2_pat_register_offset(uint32_t index, uint32_t *out) {
    if (!out || (reinterpret_cast<uintptr_t>(out) & 3)) return LBPatInput;
    if (index >= LB_PAT_ENTRIES) return LBPatIndex;
    *out = index_offset(index);
    return LBPatOK;
}

extern "C" uint32_t lb_xe2_pat_index_for_cache(uint32_t cacheKind, uint32_t *out) {
    if (!out || (reinterpret_cast<uintptr_t>(out) & 3)) return LBPatInput;
    switch (cacheKind) {
    case LBPatCacheNone: *out = LB_PAT_INDEX_NONE; return LBPatOK;
    case LBPatCacheWriteThrough: *out = LB_PAT_INDEX_WT; return LBPatOK;
    case LBPatCacheWriteBack: *out = LB_PAT_INDEX_WB; return LBPatOK;
    case LBPatCacheNoneCompression: *out = LB_PAT_INDEX_NONE_COMPRESSION; return LBPatOK;
    default: return LBPatCacheKind;
    }
}

extern "C" uint32_t lb_xe2_pat_index_spaces(uint32_t index, uint32_t *spaces) {
    if (!spaces || (reinterpret_cast<uintptr_t>(spaces) & 3)) return LBPatInput;
    if (index >= LB_PAT_ENTRIES) return LBPatIndex;
    // Five scattered PAT bits in a leaf entry; two in a directory or GGTT one.
    uint32_t reachable = LBPatSpacePpgttLeaf;
    if (index <= LB_PAT_DIRECTORY_MAX_INDEX) reachable |= LBPatSpacePpgttDirectory;
    if (index <= LB_PAT_GGTT_MAX_INDEX) reachable |= LBPatSpaceGgtt;
    *spaces = reachable;
    return LBPatOK;
}

extern "C" uint32_t lb_xe2_pat_plan(uint32_t graphicsVerX100, uint32_t gtKind,
                                    uint32_t discrete, uint64_t mappedBarBytes,
                                    LBPatPlan *out, uint32_t outputBytes) {
    if (!out || outputBytes != sizeof(LBPatPlan) || !valid_span(out, sizeof(*out)) ||
        discrete > 1) return LBPatInput;
    if (reinterpret_cast<uintptr_t>(out) & 7) return LBPatAlignment;
    // Narrow by design: only the qualified Lunar Lake graphics version. Xe2_HPG
    // 2001 additionally disables CLOS3 through Wa_16023588340 and would need a
    // 28-entry plan plus its own qualification.
    if (graphicsVerX100 != LB_PAT_LNL_VERX100) return LBPatPlatform;
    if (gtKind != LBPatGraphicsGt && gtKind != LBPatMediaGt) return LBPatInput;
    // Lunar Lake is integrated; a discrete claim contradicts the target device
    // and would add the PTA write, so it is refused instead of accommodated.
    if (discrete) return LBPatPlatform;
    if (mappedBarBytes <= LB_PAT_HIGHEST_OFFSET ||
        mappedBarBytes - LB_PAT_HIGHEST_OFFSET < 4) return LBPatRange;

    LBPatPlan plan{};
    plan.abi = LB_PAT_ABI;
    plan.structBytes = uint32_t(sizeof(LBPatPlan));
    plan.graphicsVerX100 = graphicsVerX100;
    plan.gtKind = gtKind;
    plan.entryCount = LB_PAT_ENTRIES;
    // Linux uses multicast writes for the graphics GT and plain MMIO for the
    // media GT; the steering itself is not part of this plan.
    plan.multicastRequired = gtKind == LBPatGraphicsGt ? 1u : 0u;
    plan.indexNone = LB_PAT_INDEX_NONE;
    plan.indexWriteThrough = LB_PAT_INDEX_WT;
    plan.indexWriteBack = LB_PAT_INDEX_WB;
    plan.indexNoneCompression = LB_PAT_INDEX_NONE_COMPRESSION;
    plan.requiredQualifications = LB_PAT_REQUIRED_QUALIFICATIONS;
    plan.programmed = 0;

    for (uint32_t index = 0; index < LB_PAT_ENTRIES; ++index)
        plan.writes[plan.writeCount++] = {index_offset(index), kTable[index]};
    // xe->pat.pat_ats is set for every Xe2 device; pat_pta only for discrete,
    // which this port refuses above. ptaIncluded therefore stays 0 and the
    // _PAT_PTA value remains available for a later discrete port.
    plan.writes[plan.writeCount++] = {LB_PAT_ATS_OFFSET, kAtsValue};
    plan.atsIncluded = 1;
    plan.ptaIncluded = 0;
    (void)kPtaValue;
    *out = plan;
    return LBPatOK;
}

extern "C" uint32_t lb_xe2_pat_check_readback(const LBPatPlan *plan, const uint32_t *values,
                                              uint32_t count, uint32_t *firstMismatch) {
    if (!plan || !values || !count || count > LB_PAT_MAX_WRITES ||
        !valid_span(plan, sizeof(*plan)) ||
        !valid_span(values, uint64_t(count) * sizeof(uint32_t))) return LBPatInput;
    if ((reinterpret_cast<uintptr_t>(plan) & 7) ||
        (reinterpret_cast<uintptr_t>(values) & 3)) return LBPatAlignment;
    if (firstMismatch) {
        if (reinterpret_cast<uintptr_t>(firstMismatch) & 3) return LBPatAlignment;
        if (overlap(firstMismatch, sizeof(*firstMismatch), plan, sizeof(*plan)) ||
            overlap(firstMismatch, sizeof(*firstMismatch), values,
                    uint64_t(count) * sizeof(uint32_t))) return LBPatAliasing;
    }
    if (plan->abi != LB_PAT_ABI || plan->structBytes != sizeof(LBPatPlan) ||
        plan->writeCount > LB_PAT_MAX_WRITES || plan->reserved) return LBPatInput;
    // A partial readback is never accepted as a verified PAT state.
    if (count != plan->writeCount) return LBPatReadback;
    for (uint32_t i = 0; i < count; ++i) {
        if (values[i] == plan->writes[i].value) continue;
        if (firstMismatch) *firstMismatch = i;
        return LBPatReadback;
    }
    return LBPatOK;
}
