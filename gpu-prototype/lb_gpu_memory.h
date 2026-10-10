// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Xe page-table fields adapted from Intel MIT source; see SOURCE-NOTICES.txt.
#pragma once
#include "lnl_platform.h"

constexpr uint64_t LB_GPU_PAGE = 4096;
constexpr uint64_t LB_GPU_VA_LIMIT = uint64_t(1) << 48;
constexpr uint64_t LB_GPU_DMA_LIMIT = uint64_t(1) << 46;
constexpr uint32_t LB_VM_MAX_MAPPINGS = 32;
constexpr uint64_t LB_VM_MAX_BUFFER = 1024 * 1024;
constexpr uint64_t LB_VM_BYTE_BUDGET = 8 * LB_VM_MAX_BUFFER;

enum LBVmResult : uint32_t {
    LBVmOK = 0, LBVmInput = 1, LBVmAlignment = 2, LBVmRange = 3,
    LBVmCapacity = 4, LBVmOwner = 5, LBVmNotFound = 6, LBVmCorrupt = 7
};
enum LBPageEntryKind : uint32_t { LBPpgtt4K = 0, LBPpgttDirectory = 1, LBGgtt4K = 2 };

struct LBDmaSegment { uint64_t address, bytes; };
struct LBVmMapping {
    uint64_t token, gpuAddress, bytes;
    uint32_t owner, patIndex, writable, reserved;
};
struct LBVmArena {
    uint64_t begin, end, nextToken, reservedBytes;
    uint32_t owner, count, reserved[2];
    LBVmMapping mappings[LB_VM_MAX_MAPPINGS];
};
struct LBPageTablePlan {
    uint32_t rootLevel, levels, leafEntries, totalPages;
    uint32_t pagesPerLevel[5], reserved;
};

// These APIs operate only on CPU-owned metadata/staging arrays. They neither
// allocate DMA memory nor install tables, publish a root, invalidate TLBs or
// write MMIO. Addresses must come from a future qualified kernel DMA mapper.
// The kernel caller owns the arena and serializes every call; no user blob is
// accepted as an arena or segment list. Rejected calls leave outputs untouched.
// lb_vm_init requires a fresh, entirely zeroed arena; it cannot reset live state.
extern "C" {
LNL_API uint32_t lb_xe2_page_entry(uint32_t kind, uint64_t dmaAddress,
                                 uint32_t patIndex, uint32_t writable, uint64_t *out);
LNL_API uint32_t lb_xe2_page_indices(uint64_t gpuAddress, uint32_t *fiveIndices);
LNL_API uint32_t lb_xe2_table_plan(uint64_t gpuAddress, uint64_t bytes, LBPageTablePlan *out);
LNL_API uint32_t lb_vm_init(LBVmArena *arena, uint32_t owner, uint64_t begin, uint64_t end);
LNL_API uint32_t lb_vm_reserve(LBVmArena *arena, uint32_t owner, uint64_t bytes,
                             uint32_t patIndex, uint32_t writable, LBVmMapping *out);
LNL_API uint32_t lb_vm_release(LBVmArena *arena, uint32_t owner, uint64_t token);
LNL_API uint32_t lb_vm_stage_ptes(const LBVmArena *arena, uint32_t owner, uint64_t token,
                                const LBDmaSegment *segments, uint32_t segmentCount,
                                uint64_t *entries, uint32_t entryCapacity);
}
