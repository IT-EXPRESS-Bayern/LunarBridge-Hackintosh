// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Derived from Intel MIT source: Linux v6.17 drivers/gpu/drm/xe/xe_pat.c,
// regs/xe_reg_defs.h and drivers/gpu/drm/i915/i915_reg_defs.h,
// Copyright 2023-2024 Intel Corporation. Exact files, hashes and line
// citations are in ../docs/ARC_PORT_SOURCES.json; license text in
// ../LICENSES/MIT-Intel.txt. Own code remains GPL-3.0-only.
#pragma once
#include "lnl_platform.h"

// Xe2 / Lunar Lake PAT programming, ported from the Linux Arc driver.
//
// Linux programs the PAT inside xe_gt_init_early() through a per-platform ops
// struct; the Xe2 path writes 32 index registers plus one ATS register, uses
// multicast writes on a graphics GT and plain MMIO on a media GT, and only
// programs the PTA register on discrete devices. None of that structure
// survives a port by itself: this module turns it into an explicit, checkable
// register write plan plus the readback test, so the macOS side can hold
// forcewake, own the BAR and verify the result instead of writing blind.
//
// This module performs NO MMIO. It produces the plan and validates a readback
// the caller collected. A produced plan is not a programmed device.
constexpr uint32_t LB_PAT_ABI = 1;
// Lunar Lake: GRAPHICS_VERx100 2004 (Xe2_LPG). xe_pat.c routes every
// GRAPHICS_VER 20 and 30 device to the Xe2 table; this port intentionally
// accepts only the qualified Lunar Lake version.
constexpr uint32_t LB_PAT_LNL_VERX100 = 2004;
// ARRAY_SIZE(xe2_pat_table): 32 index registers, including the reserved gap.
constexpr uint32_t LB_PAT_ENTRIES = 32;
// Indices 16..19 are reserved in the Xe2 table and stay all-zero, which the
// source documents as maximum caching with minimum coherency.
constexpr uint32_t LB_PAT_RESERVED_FIRST = 16;
constexpr uint32_t LB_PAT_RESERVED_LAST = 19;
constexpr uint32_t LB_PAT_MAX_WRITES = LB_PAT_ENTRIES + 2;
// _PAT_INDEX(index) is two evenly spaced ranges; _PAT_ATS and _PAT_PTA sit
// between them. The highest offset reached is _PAT_INDEX(31) = 0x48A4.
constexpr uint32_t LB_PAT_INDEX_RANGE0 = 0x4800;
constexpr uint32_t LB_PAT_INDEX_RANGE1 = 0x4848;
constexpr uint32_t LB_PAT_RANGE_SPLIT = 8;
constexpr uint32_t LB_PAT_ATS_OFFSET = 0x47FC;
constexpr uint32_t LB_PAT_PTA_OFFSET = 0x4820;
constexpr uint32_t LB_PAT_HIGHEST_OFFSET = LB_PAT_INDEX_RANGE1 +
    (LB_PAT_ENTRIES - LB_PAT_RANGE_SPLIT - 1) * 4;

enum LBPatResult : uint32_t {
    LBPatOK = 0, LBPatInput = 1, LBPatPlatform = 2, LBPatRange = 3,
    LBPatIndex = 4, LBPatCacheKind = 5, LBPatReadback = 6, LBPatAliasing = 7,
    LBPatAlignment = 8
};

enum LBPatGtKind : uint32_t { LBPatGraphicsGt = 0, LBPatMediaGt = 1 };

// xe_pat_init_early() records these indices for the generic cache modes.
enum LBPatCacheKind : uint32_t {
    LBPatCacheNone = 0, LBPatCacheWriteThrough = 1, LBPatCacheWriteBack = 2,
    LBPatCacheNoneCompression = 3, LBPatCacheKindCount = 4
};
constexpr uint32_t LB_PAT_INDEX_NONE = 3;
constexpr uint32_t LB_PAT_INDEX_WT = 15;
constexpr uint32_t LB_PAT_INDEX_WB = 2;
constexpr uint32_t LB_PAT_INDEX_NONE_COMPRESSION = 12;

// Hardware work the plan cannot do for the caller. All of it is still missing.
enum LBPatQualification : uint32_t {
    LBPatQualForcewake = 1 << 0,      // GT wake held across every write
    LBPatQualMappedBar = 1 << 1,      // BAR mapped and long enough
    LBPatQualMulticast = 1 << 2,      // MCR steering set for a graphics GT
    LBPatQualReadback = 1 << 3,       // values read back and compared
    LBPatQualPowerState = 1 << 4      // device in D0 with memory decode on
};
constexpr uint32_t LB_PAT_REQUIRED_QUALIFICATIONS = (1 << 5) - 1;

struct LBPatWrite {
    uint32_t offset, value;
};

struct LBPatPlan {
    uint32_t abi, structBytes, graphicsVerX100, gtKind;
    uint32_t entryCount, writeCount, multicastRequired, atsIncluded;
    uint32_t ptaIncluded, indexNone, indexWriteThrough, indexWriteBack;
    uint32_t indexNoneCompression, requiredQualifications, programmed, reserved;
    LBPatWrite writes[LB_PAT_MAX_WRITES];
};

static_assert(sizeof(LBPatWrite) == 8, "PAT write wire ABI");
static_assert(sizeof(LBPatPlan) == 64 + LB_PAT_MAX_WRITES * 8, "PAT plan wire ABI");

extern "C" {
// Encoded Xe2 table value of one index; reserved indices encode as zero.
LNL_API uint32_t lb_xe2_pat_entry_value(uint32_t index, uint32_t *out);
// MMIO offset of one index register, as _PAT_INDEX() computes it.
LNL_API uint32_t lb_xe2_pat_register_offset(uint32_t index, uint32_t *out);
// Index Linux records for a generic cache mode on Xe2.
LNL_API uint32_t lb_xe2_pat_index_for_cache(uint32_t cacheKind, uint32_t *out);
// Build the full ordered write plan: indices ascending, then ATS, then PTA on
// a discrete device. Fails for any platform, GT kind or BAR this port has not
// qualified; a failure leaves every output byte unchanged.
LNL_API uint32_t lb_xe2_pat_plan(uint32_t graphicsVerX100, uint32_t gtKind,
                                uint32_t discrete, uint64_t mappedBarBytes,
                                LBPatPlan *out, uint32_t outputBytes);
// Compare a caller-collected readback against the plan. The values must be in
// plan order and complete; a mismatch names the first offending write index.
LNL_API uint32_t lb_xe2_pat_check_readback(const LBPatPlan *plan, const uint32_t *values,
                                          uint32_t count, uint32_t *firstMismatch);
}
