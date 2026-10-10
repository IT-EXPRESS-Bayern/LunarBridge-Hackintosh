// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Page-table fields: Intel MIT Xe source, Copyright 2024 Intel Corporation.
#include "lb_gpu_memory.h"

namespace {
bool valid_pat(uint32_t index) { return index < 32 && (index < 16 || index > 19); }
bool valid_range(uint64_t address, uint64_t bytes) {
    return bytes && !(address & (LB_GPU_PAGE - 1)) && !(bytes & (LB_GPU_PAGE - 1)) &&
           address < LB_GPU_VA_LIMIT && bytes <= LB_GPU_VA_LIMIT - address;
}
uint64_t leaf_pat(uint32_t index) {
    return ((index & 1) ? uint64_t(1) << 3 : 0) |
           ((index & 2) ? uint64_t(1) << 4 : 0) |
           ((index & 4) ? uint64_t(1) << 7 : 0) |
           ((index & 8) ? uint64_t(1) << 62 : 0) |
           ((index & 16) ? uint64_t(1) << 61 : 0);
}
uint32_t check_arena(const LBVmArena *arena, uint32_t owner) {
    if (!arena || !owner) return LBVmInput;
    if (arena->owner != owner) return LBVmOwner;
    if (!arena->nextToken || arena->reserved[0] || arena->reserved[1] ||
        arena->count > LB_VM_MAX_MAPPINGS || arena->begin < 65536 ||
        arena->end > LB_GPU_VA_LIMIT - 65536 || arena->end <= arena->begin ||
        !valid_range(arena->begin, arena->end - arena->begin)) return LBVmCorrupt;
    uint64_t used = 0, end = arena->begin;
    for (uint32_t i = 0; i < arena->count; ++i) {
        const auto &m = arena->mappings[i];
        if (!m.token || m.token >= arena->nextToken || m.owner != owner || m.reserved ||
            !valid_pat(m.patIndex) || m.writable > 1 || !valid_range(m.gpuAddress, m.bytes) ||
            m.bytes > LB_VM_MAX_BUFFER || m.gpuAddress < end ||
            m.gpuAddress >= arena->end || m.bytes > arena->end - m.gpuAddress ||
            used > LB_VM_BYTE_BUDGET - m.bytes) return LBVmCorrupt;
        for (uint32_t j = 0; j < i; ++j)
            if (arena->mappings[j].token == m.token) return LBVmCorrupt;
        end = m.gpuAddress + m.bytes;
        used += m.bytes;
    }
    return used == arena->reservedBytes ? LBVmOK : LBVmCorrupt;
}
}

extern "C" uint32_t lb_xe2_page_entry(uint32_t kind, uint64_t address,
                                      uint32_t pat, uint32_t writable, uint64_t *out) {
    if (!out || kind > LBGgtt4K || writable > 1 || !valid_pat(pat)) return LBVmInput;
    if (address & (LB_GPU_PAGE - 1)) return LBVmAlignment;
    if (address >= LB_GPU_DMA_LIMIT) return LBVmRange;
    uint64_t flags = 1;
    if (kind == LBPpgtt4K) {
        flags |= leaf_pat(pat) | (writable ? 2 : 0);
    } else if (kind == LBPpgttDirectory) {
        // Linux directory encoding deliberately uses only PAT[1:0].
        if (pat > 3 || writable != 1) return LBVmInput;
        flags |= 2 | leaf_pat(pat);
    } else {
        // GGTT has its own PAT positions and no per-entry read-only bit.
        if (pat > 3 || writable != 1) return LBVmInput;
        flags |= ((pat & 1) ? uint64_t(1) << 52 : 0) |
                 ((pat & 2) ? uint64_t(1) << 53 : 0);
    }
    *out = address | flags;
    return LBVmOK;
}

extern "C" uint32_t lb_xe2_page_indices(uint64_t address, uint32_t *indices) {
    if (!indices) return LBVmInput;
    if (address >= LB_GPU_VA_LIMIT) return LBVmRange;
    for (uint32_t i = 0; i < 5; ++i) indices[i] = uint32_t((address >> (12 + 9 * i)) & 511);
    return LBVmOK;
}

extern "C" uint32_t lb_xe2_table_plan(uint64_t address, uint64_t bytes, LBPageTablePlan *out) {
    if (!out || !bytes || bytes > LB_VM_MAX_BUFFER) return LBVmInput;
    if ((address | bytes) & (LB_GPU_PAGE - 1)) return LBVmAlignment;
    if (!valid_range(address, bytes)) return LBVmRange;
    LBPageTablePlan result{};
    result.rootLevel = 4;
    result.levels = 5;
    result.leafEntries = uint32_t(bytes / LB_GPU_PAGE);
    for (uint32_t i = 0; i < 4; ++i) {
        const uint32_t shift = 21 + 9 * i;
        result.pagesPerLevel[i] = uint32_t(((address + bytes - 1) >> shift) - (address >> shift) + 1);
        result.totalPages += result.pagesPerLevel[i];
    }
    result.pagesPerLevel[4] = 1;
    ++result.totalPages;
    *out = result;
    return LBVmOK;
}

extern "C" uint32_t lb_vm_init(LBVmArena *arena, uint32_t owner, uint64_t begin, uint64_t end) {
    if (!arena || !owner || end <= begin) return LBVmInput;
    if ((begin | end) & (LB_GPU_PAGE - 1)) return LBVmAlignment;
    if (begin < 65536 || end > LB_GPU_VA_LIMIT - 65536) return LBVmRange;
    // Initialization is for a fresh, zeroed object. Reinitializing an existing
    // arena would discard ownership and could make a stale token valid again.
    const auto *previous = reinterpret_cast<const uint8_t *>(arena);
    for (uint32_t i = 0; i < sizeof(*arena); ++i) if (previous[i]) return LBVmCorrupt;
    *arena = {};
    arena->begin = begin;
    arena->end = end;
    arena->owner = owner;
    arena->nextToken = 1;
    return LBVmOK;
}

extern "C" uint32_t lb_vm_reserve(LBVmArena *arena, uint32_t owner, uint64_t bytes,
                                  uint32_t pat, uint32_t writable, LBVmMapping *out) {
    const uint32_t status = check_arena(arena, owner);
    if (status) return status;
    if (!out || !bytes || bytes > LB_VM_MAX_BUFFER || !valid_pat(pat) || writable > 1) return LBVmInput;
    if (bytes & (LB_GPU_PAGE - 1)) return LBVmAlignment;
    if (arena->count == LB_VM_MAX_MAPPINGS || arena->nextToken == UINT64_MAX ||
        bytes > LB_VM_BYTE_BUDGET - arena->reservedBytes) return LBVmCapacity;
    uint64_t address = arena->begin;
    uint32_t position = 0;
    for (; position < arena->count; ++position) {
        const auto &m = arena->mappings[position];
        if (bytes <= m.gpuAddress - address) break;
        address = m.gpuAddress + m.bytes;
    }
    if (address >= arena->end || bytes > arena->end - address) return LBVmCapacity;
    LBVmMapping result{arena->nextToken, address, bytes, owner, pat, writable, 0};
    for (uint32_t i = arena->count; i > position; --i) arena->mappings[i] = arena->mappings[i - 1];
    arena->mappings[position] = result;
    ++arena->count;
    ++arena->nextToken;
    arena->reservedBytes += bytes;
    *out = result;
    return LBVmOK;
}

extern "C" uint32_t lb_vm_release(LBVmArena *arena, uint32_t owner, uint64_t token) {
    const uint32_t status = check_arena(arena, owner);
    if (status) return status;
    if (!token) return LBVmInput;
    for (uint32_t i = 0; i < arena->count; ++i) {
        if (arena->mappings[i].token != token) continue;
        arena->reservedBytes -= arena->mappings[i].bytes;
        for (uint32_t j = i + 1; j < arena->count; ++j) arena->mappings[j - 1] = arena->mappings[j];
        arena->mappings[--arena->count] = {};
        return LBVmOK;
    }
    return LBVmNotFound;
}

extern "C" uint32_t lb_vm_stage_ptes(const LBVmArena *arena, uint32_t owner, uint64_t token,
                                     const LBDmaSegment *segments, uint32_t segmentCount,
                                     uint64_t *entries, uint32_t capacity) {
    const uint32_t status = check_arena(arena, owner);
    if (status) return status;
    if (!token || !segments || !entries || !segmentCount || segmentCount > 256) return LBVmInput;
    const LBVmMapping *mapping = nullptr;
    for (uint32_t i = 0; i < arena->count; ++i)
        if (arena->mappings[i].token == token) { mapping = &arena->mappings[i]; break; }
    if (!mapping) return LBVmNotFound;
    if (capacity < mapping->bytes / LB_GPU_PAGE) return LBVmCapacity;
    uint64_t total = 0;
    for (uint32_t i = 0; i < segmentCount; ++i) {
        const auto &s = segments[i];
        if (!s.bytes || ((s.address | s.bytes) & (LB_GPU_PAGE - 1))) return LBVmAlignment;
        if (s.address >= LB_GPU_DMA_LIMIT || s.bytes > LB_GPU_DMA_LIMIT - s.address ||
            s.bytes > mapping->bytes - total) return LBVmRange;
        // A buffer's page list cannot alias itself; separate VM owners never share this list.
        for (uint32_t j = 0; j < i; ++j) {
            const auto &p = segments[j];
            if (s.address < p.address + p.bytes && p.address < s.address + s.bytes) return LBVmRange;
        }
        total += s.bytes;
    }
    if (total != mapping->bytes) return LBVmRange;
    uint32_t index = 0;
    const uint64_t flags = 1 | (mapping->writable ? 2 : 0) | leaf_pat(mapping->patIndex);
    for (uint32_t i = 0; i < segmentCount; ++i)
        for (uint64_t offset = 0; offset < segments[i].bytes; offset += LB_GPU_PAGE)
            entries[index++] = (segments[i].address + offset) | flags;
    return LBVmOK;
}
