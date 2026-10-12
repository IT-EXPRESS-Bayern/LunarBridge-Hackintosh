// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Layout, register offsets, control values, descriptor bits and the first ring
// instruction are derived from Intel MIT source (Linux v6.17 Xe driver); see
// ../docs/ARC_PORT_SOURCES.json and ../LICENSES/MIT-Intel.txt.
#include "lb_lrc.h"

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
bool page_aligned(uint64_t value) { return (value & (LB_GPU_PAGE - 1)) == 0; }

// _MASKED_BIT_ENABLE(bits): the mask goes into the upper half of the dword.
constexpr uint32_t masked_enable(uint32_t bits) { return (bits << 16) | bits; }

// Engine context image pages beyond the PPHWSP page, from xe_gt_lrc_size()
// for GRAPHICS_VER >= 20.
uint32_t image_pages(uint32_t engineClass) {
    switch (engineClass) {
    case LBLrcRenderClass: return 3;
    case LBLrcComputeClass: return 2;
    default: return 0;
    }
}
// The context image is addressed in dwords; its own writes must stay inside it.
uint32_t highest_write_index() {
    uint32_t highest = LB_CTX_CONTEXT_CONTROL;
    const uint32_t indices[] = {LB_CTX_RING_HEAD, LB_CTX_RING_TAIL, LB_CTX_RING_START,
                                LB_CTX_RING_CTL, LB_CTX_TIMESTAMP, LB_CTX_TIMESTAMP_UDW,
                                LB_CTX_ASID, LB_CTX_PDP0_UDW, LB_CTX_PDP0_LDW};
    for (uint32_t index : indices)
        if (index > highest) highest = index;
    return highest;
}
}  // namespace

extern "C" uint32_t lb_lrc_layout(uint32_t engineClass, uint64_t ringBytes,
                                  uint32_t indirectRingState, LBLrcLayout *out,
                                  uint32_t outputBytes) {
    if (!out || outputBytes != sizeof(LBLrcLayout) || !valid_span(out, sizeof(*out)))
        return LBLrcInput;
    if (reinterpret_cast<uintptr_t>(out) & 7) return LBLrcAlignment;
    if (indirectRingState > 1) return LBLrcInput;
    // No graphics_desc at the pinned revision sets has_indirect_ring_state, so
    // the indirect page and its INDIRECT_CTX_* registers stay unported.
    if (indirectRingState) return LBLrcUnsupported;
    const uint32_t pages = image_pages(engineClass);
    if (!pages) return LBLrcEngine;
    if (!page_aligned(ringBytes) || ringBytes < LB_LRC_MIN_RING_BYTES ||
        ringBytes > LB_LRC_MAX_RING_BYTES) return LBLrcRange;

    LBLrcLayout layout{};
    layout.abi = LB_LRC_ABI;
    layout.structBytes = uint32_t(sizeof(LBLrcLayout));
    layout.engineClass = engineClass;
    layout.imagePages = pages;
    // Allocation order upstream: ring, PPHWSP, context image, then the
    // per-context workaround batch at the very end of the object.
    layout.ringOffset = 0;
    layout.ringBytes = ringBytes;
    layout.pphwspOffset = ringBytes;
    layout.imageOffset = ringBytes + LB_LRC_PPHWSP_BYTES;
    layout.imageBytes = uint64_t(pages) * LB_GPU_PAGE;
    layout.totalBytes = ringBytes + LB_LRC_PPHWSP_BYTES + layout.imageBytes +
                        LB_LRC_WA_BB_BYTES;
    layout.waBatchOffset = layout.totalBytes - LB_LRC_WA_BB_BYTES;
    layout.requiredQualifications = LB_LRC_REQUIRED_QUALIFICATIONS;
    *out = layout;
    return LBLrcOK;
}

extern "C" uint32_t lb_lrc_descriptor(uint64_t pphwspGpuAddress, uint32_t privileged,
                                      uint64_t *out) {
    if (!out || privileged > 1 || (reinterpret_cast<uintptr_t>(out) & 7)) return LBLrcInput;
    if (!pphwspGpuAddress || !page_aligned(pphwspGpuAddress)) return LBLrcAlignment;
    // The descriptor carries a 32-bit GGTT address; the global space this
    // driver may use ends at the GuC top boundary.
    if (pphwspGpuAddress >= LB_GGTT_GUC_TOP) return LBLrcRange;
    uint64_t descriptor = LB_LRC_VALID |
        (LB_LRC_LEGACY_64B_CONTEXT << LB_LRC_ADDRESSING_MODE_SHIFT);
    // Upstream sets LRC_PRIVILEGE whenever the context has a VM, which for
    // this port is always: there is no global-GTT-only context here.
    if (privileged) descriptor |= LB_LRC_PRIVILEGE;
    *out = descriptor | pphwspGpuAddress;
    return LBLrcOK;
}

extern "C" uint32_t lb_lrc_build(const LBLrcLayout *layout, uint64_t ggttAddress,
                                 uint64_t pageDirectoryAddress, uint32_t asid,
                                 uint32_t ringTail, LBLrcImage *out, uint32_t outputBytes) {
    if (!out || outputBytes != sizeof(LBLrcImage) || !valid_span(out, sizeof(*out)) ||
        !layout || !valid_span(layout, sizeof(*layout))) return LBLrcInput;
    if ((reinterpret_cast<uintptr_t>(out) | reinterpret_cast<uintptr_t>(layout)) & 7)
        return LBLrcAlignment;
    if (overlap(out, sizeof(*out), layout, sizeof(*layout))) return LBLrcAliasing;
    if (layout->abi != LB_LRC_ABI || layout->structBytes != sizeof(LBLrcLayout) ||
        layout->reserved[0] || layout->reserved[1] || layout->reserved[2])
        return LBLrcAbi;

    // Recompute the layout from its own inputs: a caller-supplied layout is
    // never trusted to describe a consistent allocation.
    LBLrcLayout expected{};
    const uint32_t status = lb_lrc_layout(layout->engineClass, layout->ringBytes, 0,
                                          &expected, uint32_t(sizeof(expected)));
    if (status) return status;
    if (expected.totalBytes != layout->totalBytes || expected.ringOffset != layout->ringOffset ||
        expected.pphwspOffset != layout->pphwspOffset || expected.imageOffset != layout->imageOffset ||
        expected.imageBytes != layout->imageBytes || expected.waBatchOffset != layout->waBatchOffset ||
        expected.imagePages != layout->imagePages ||
        expected.requiredQualifications != layout->requiredQualifications) return LBLrcAbi;

    if (!ggttAddress || !page_aligned(ggttAddress)) return LBLrcAlignment;
    if (ggttAddress >= LB_GGTT_GUC_TOP ||
        layout->totalBytes > LB_GGTT_GUC_TOP - ggttAddress) return LBLrcRange;
    // The whole allocation must sit above the reserved low GGTT window that
    // the GGTT planner keeps for firmware.
    if (ggttAddress < LB_GGTT_RESERVED_LOW) return LBLrcPolicy;
    if (!pageDirectoryAddress || !page_aligned(pageDirectoryAddress) ||
        pageDirectoryAddress >= LB_GPU_DMA_LIMIT) return LBLrcRange;
    // has_asid is set for Xe2, and upstream writes CTX_ASID for every context
    // that has a VM; a zero id would silently share address-space state.
    if (!asid) return LBLrcInput;
    // The tail is a byte offset inside the ring and advances in dwords.
    if (ringTail >= layout->ringBytes || (ringTail & 3)) return LBLrcRange;

    const uint64_t pphwspAddress = ggttAddress + layout->pphwspOffset;
    const uint64_t ringAddress = ggttAddress + layout->ringOffset;
    uint64_t descriptor = 0;
    const uint32_t descriptorStatus = lb_lrc_descriptor(pphwspAddress, 1, &descriptor);
    if (descriptorStatus) return descriptorStatus;
    // The PPGTT root uses the directory encoding with the write-back PAT index
    // the PAT port programs; GGTT and PPGTT formats are not interchangeable.
    uint64_t pageDirectoryEntry = 0;
    const uint32_t pdeStatus = lb_xe2_page_entry(LBPpgttDirectory, pageDirectoryAddress,
                                                 LB_PAT_INDEX_WB, 1, &pageDirectoryEntry);
    if (pdeStatus) return LBLrcRange;

    LBLrcImage image{};
    image.abi = LB_LRC_ABI;
    image.structBytes = uint32_t(sizeof(LBLrcImage));
    image.engineClass = layout->engineClass;
    image.contextControl = masked_enable(LB_CTX_CTRL_INHIBIT_SYN_CTX_SWITCH |
                                         LB_CTX_CTRL_ENGINE_CTX_RESTORE_INHIBIT);
    image.ringControl = uint32_t(layout->ringBytes - LB_GPU_PAGE) | LB_RING_VALID;
    image.asid = asid;
    image.descriptor = descriptor;
    image.pphwspGpuAddress = pphwspAddress;
    image.ringGpuAddress = ringAddress;
    image.pageDirectoryEntry = pageDirectoryEntry;
    image.seqnoGpuAddress = pphwspAddress + LB_LRC_SEQNO_OFFSET;
    image.requiredQualifications = layout->requiredQualifications;
    image.hardwareBound = 0;
    image.gpuExecuted = 0;

    // Context-image writes in the order upstream performs them.
    image.writes[image.writeCount++] = {LB_CTX_CONTEXT_CONTROL, image.contextControl};
    image.writes[image.writeCount++] = {LB_CTX_RING_START, uint32_t(ringAddress)};
    image.writes[image.writeCount++] = {LB_CTX_RING_HEAD, 0};
    image.writes[image.writeCount++] = {LB_CTX_RING_TAIL, ringTail};
    image.writes[image.writeCount++] = {LB_CTX_RING_CTL, image.ringControl};
    image.writes[image.writeCount++] = {LB_CTX_TIMESTAMP, 0};
    image.writes[image.writeCount++] = {LB_CTX_TIMESTAMP_UDW, 0};
    image.writes[image.writeCount++] = {LB_CTX_PDP0_UDW,
                                        uint32_t(pageDirectoryEntry >> 32)};
    image.writes[image.writeCount++] = {LB_CTX_PDP0_LDW,
                                        uint32_t(pageDirectoryEntry & 0xFFFFFFFFu)};
    image.writes[image.writeCount++] = {LB_CTX_ASID, asid};
    // The first ring instruction upstream writes is arbitration enable.
    image.prologue[image.prologueDwords++] = LB_MI_ARB_ON_OFF | LB_MI_ARB_ENABLE;
    *out = image;
    return LBLrcOK;
}

extern "C" uint32_t lb_lrc_control_words(uint64_t ringBytes, uint32_t *contextControl,
                                         uint32_t *ringControl) {
    if (!contextControl || !ringControl ||
        ((reinterpret_cast<uintptr_t>(contextControl) |
          reinterpret_cast<uintptr_t>(ringControl)) & 3)) return LBLrcInput;
    if (!page_aligned(ringBytes) || ringBytes < LB_LRC_MIN_RING_BYTES ||
        ringBytes > LB_LRC_MAX_RING_BYTES) return LBLrcRange;
    *contextControl = masked_enable(LB_CTX_CTRL_INHIBIT_SYN_CTX_SWITCH |
                                    LB_CTX_CTRL_ENGINE_CTX_RESTORE_INHIBIT);
    *ringControl = uint32_t(ringBytes - LB_GPU_PAGE) | LB_RING_VALID;
    return LBLrcOK;
}

extern "C" uint32_t lb_lrc_apply(const LBLrcImage *image, uint32_t *imageDwords,
                                 uint32_t dwordCapacity) {
    if (!image || !imageDwords || !dwordCapacity ||
        !valid_span(image, sizeof(*image)) ||
        !valid_span(imageDwords, uint64_t(dwordCapacity) * sizeof(uint32_t)))
        return LBLrcInput;
    if ((reinterpret_cast<uintptr_t>(image) & 7) ||
        (reinterpret_cast<uintptr_t>(imageDwords) & 3)) return LBLrcAlignment;
    if (overlap(imageDwords, uint64_t(dwordCapacity) * sizeof(uint32_t),
                image, sizeof(*image))) return LBLrcAliasing;
    if (image->abi != LB_LRC_ABI || image->structBytes != sizeof(LBLrcImage) ||
        image->reserved || image->hardwareBound || image->gpuExecuted ||
        !image->writeCount || image->writeCount > LB_LRC_MAX_WRITES ||
        image->prologueDwords > LB_LRC_MAX_PROLOGUE) return LBLrcAbi;
    // Every write must land inside the caller's buffer; a short buffer is a
    // capacity error, never a partially patched context.
    for (uint32_t i = 0; i < image->writeCount; ++i)
        if (image->writes[i].dwordIndex >= dwordCapacity) return LBLrcCapacity;
    if (highest_write_index() >= dwordCapacity) return LBLrcCapacity;
    for (uint32_t i = 0; i < image->writeCount; ++i)
        imageDwords[image->writes[i].dwordIndex] = image->writes[i].value;
    return LBLrcOK;
}
