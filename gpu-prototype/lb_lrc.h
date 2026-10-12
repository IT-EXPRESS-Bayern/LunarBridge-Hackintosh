// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Derived from Intel MIT source: Linux v6.17 drivers/gpu/drm/xe/xe_lrc.c,
// regs/xe_lrc_layout.h, regs/xe_engine_regs.h, xe_execlist.c, xe_vm.c and
// instructions/xe_mi_commands.h, Copyright 2021-2024 Intel Corporation.
// Files, hashes and line citations: ../docs/ARC_PORT_SOURCES.json.
// License text: ../LICENSES/MIT-Intel.txt. Own code is GPL-3.0-only.
#pragma once
#include "lb_ggtt_plan.h"
#include "lb_xe2_pat.h"

// Xe2 logical ring context (LRC) for Arc 130V render and compute, ported from
// the Linux Arc driver.
//
// Linux builds this image inside a GEM object it allocates, maps and pins,
// then hands the engine a descriptor. A macOS port has none of that: the image
// has to be built in memory the kext owns, at GGTT addresses the kext reserved
// itself, and every address inside the image is a GGTT address, not a system
// or DMA address. This module is that step: it computes the layout, produces
// the exact context-image dword writes, the ring control word, the PPGTT root
// entry, the first ring instruction and the context descriptor.
//
// It performs NO MMIO, no GGTT write, no submission. A built image is a CPU
// buffer: the GuC has not seen it, and nothing here proves a context runs.
constexpr uint32_t LB_LRC_ABI = 1;
constexpr uint64_t LB_LRC_PPHWSP_BYTES = LB_GPU_PAGE;
constexpr uint64_t LB_LRC_WA_BB_BYTES = LB_GPU_PAGE;
// xe_execlist.c and xe_exec_queue.c both create their LRCs with SZ_16K.
constexpr uint64_t LB_LRC_DEFAULT_RING_BYTES = 16 * 1024;
// RING_CTL_SIZE(size) encodes (size - PAGE_SIZE). A single-page ring would
// encode as zero, which this port refuses rather than interpret; the upper
// bound is this project's own conservative cap, not an upstream limit.
// The size must also be a power of two: xe_lrc_ring_space() computes
// ((head - tail - 1) & (size - 1)) + 1, which is only correct for one.
constexpr uint64_t LB_LRC_MIN_RING_BYTES = 2 * LB_GPU_PAGE;
constexpr uint64_t LB_LRC_MAX_RING_BYTES = 512 * LB_GPU_PAGE;
constexpr uint32_t LB_LRC_MAX_WRITES = 12;
constexpr uint32_t LB_LRC_MAX_PROLOGUE = 4;
// RING_TAIL carries the tail in TAIL_ADDR = bits 20:3, so the tail is
// 8-byte granular and anything in bits 2:0 is lost. RING_HEAD is 4-byte
// granular (HEAD_ADDR = bits 20:2). Upstream reconciles a 4-byte payload with
// that granularity by appending MI_NOOP in xe_lrc_write_ring(), which is why
// the ring prologue here is two dwords, not one.
constexpr uint64_t LB_LRC_TAIL_ALIGNMENT = 8;
constexpr uint64_t LB_LRC_TAIL_LIMIT = 0x1FFFF8;

// Context-image dword indices from regs/xe_lrc_layout.h. Each one is the
// documented "register offset + 1", because the image stores a register
// address followed by its value.
constexpr uint32_t LB_CTX_CONTEXT_CONTROL = 0x02 + 1;
constexpr uint32_t LB_CTX_RING_HEAD = 0x04 + 1;
constexpr uint32_t LB_CTX_RING_TAIL = 0x06 + 1;
constexpr uint32_t LB_CTX_RING_START = 0x08 + 1;
constexpr uint32_t LB_CTX_RING_CTL = 0x0a + 1;
constexpr uint32_t LB_CTX_TIMESTAMP = 0x22 + 1;
constexpr uint32_t LB_CTX_TIMESTAMP_UDW = 0x24 + 1;
constexpr uint32_t LB_CTX_INDIRECT_RING_STATE = 0x26 + 1;
constexpr uint32_t LB_CTX_ASID = 0x2e + 1;
constexpr uint32_t LB_CTX_PDP0_UDW = 0x30 + 1;
constexpr uint32_t LB_CTX_PDP0_LDW = 0x32 + 1;

// Driver-defined offsets inside the PPHWSP page (xe_lrc.c).
constexpr uint32_t LB_LRC_SEQNO_OFFSET = 512;
constexpr uint32_t LB_LRC_START_SEQNO_OFFSET = LB_LRC_SEQNO_OFFSET + 8;
constexpr uint32_t LB_LRC_CTX_JOB_TIMESTAMP_OFFSET = LB_LRC_START_SEQNO_OFFSET + 8;
constexpr uint32_t LB_LRC_ENGINE_ID_OFFSET = 1024;
constexpr uint32_t LB_LRC_PARALLEL_OFFSET = 2048;

// CTX_CTRL_* bits from regs/xe_engine_regs.h, applied as a masked write
// (_MASKED_BIT_ENABLE puts the mask in the upper half of the dword).
constexpr uint32_t LB_CTX_CTRL_ENGINE_CTX_RESTORE_INHIBIT = 1u << 0;
constexpr uint32_t LB_CTX_CTRL_INHIBIT_SYN_CTX_SWITCH = 1u << 3;
constexpr uint32_t LB_CTX_CTRL_INDIRECT_RING_STATE_ENABLE = 1u << 4;
constexpr uint32_t LB_RING_VALID = 0x00000001;

// Context descriptor bits from xe_lrc.c. LRC_ENGINE_CLASS / LRC_ENGINE_INSTANCE
// are deliberately absent: upstream sets them only below GRAPHICS_VERx100 1250,
// and Lunar Lake is 2004.
// This is the GuC-path descriptor. xe_guc_submit.c registers exactly these
// bits, split into hwlrca_lo/hi. An execlist path would additionally OR a
// software context id into XEHP_SW_CTX_ID (bits 54:39 on Xe2, id 0 reserved
// for the kernel context), which overlaps the pre-1250 engine-instance bits —
// so those upper bits are not free, and this port leaves them clear.
constexpr uint64_t LB_LRC_SW_CTX_ID_SHIFT = 39;
constexpr uint64_t LB_LRC_SW_CTX_ID_WIDTH = 16;
constexpr uint64_t LB_LRC_VALID = 1ull << 0;
constexpr uint64_t LB_LRC_PRIVILEGE = 1ull << 8;
constexpr uint64_t LB_LRC_ADDRESSING_MODE_SHIFT = 3;
constexpr uint64_t LB_LRC_LEGACY_64B_CONTEXT = 3;

// MI_ARB_ON_OFF | MI_ARB_ENABLE: command type 0, opcode 0x8 in bits 28:23.
constexpr uint32_t LB_MI_ARB_ON_OFF = 0x8u << 23;
constexpr uint32_t LB_MI_ARB_ENABLE = 1u << 0;
constexpr uint32_t LB_MI_BATCH_BUFFER_END = 0xAu << 23;
constexpr uint32_t LB_MI_NOOP = 0u;

enum LBLrcResult : uint32_t {
    LBLrcOK = 0, LBLrcInput = 1, LBLrcAlignment = 2, LBLrcRange = 3,
    LBLrcEngine = 4, LBLrcUnsupported = 5, LBLrcAliasing = 6,
    LBLrcCapacity = 7, LBLrcPolicy = 8, LBLrcAbi = 9
};

// GuC and Xe engine-class numbering agree for these two classes.
enum LBLrcEngineClass : uint32_t { LBLrcRenderClass = 0, LBLrcComputeClass = 4 };

// Hardware work a built image still needs before it could ever run.
enum LBLrcQualification : uint32_t {
    LBLrcQualGgttReservation = 1 << 0,  // image and ring bound in global space
    LBLrcQualPpgttRoot = 1 << 1,        // a real, populated page-table root
    LBLrcQualGuCRunning = 1 << 2,       // authenticated firmware with a CT queue
    LBLrcQualFenceMemory = 1 << 3,      // PPHWSP seqno readable by both sides
    LBLrcQualEngineReset = 1 << 4       // reset and error handling
};
constexpr uint32_t LB_LRC_REQUIRED_QUALIFICATIONS = (1 << 5) - 1;

struct LBLrcLayout {
    uint64_t totalBytes, ringOffset, ringBytes, pphwspOffset;
    uint64_t imageOffset, imageBytes, waBatchOffset;
    uint32_t abi, structBytes, engineClass, imagePages;
    uint32_t requiredQualifications, reserved[3];
};

struct LBLrcWrite {
    uint32_t dwordIndex, value;
};

struct LBLrcImage {
    uint32_t abi, structBytes, engineClass, writeCount;
    uint32_t contextControl, ringControl, asid, prologueDwords;
    uint64_t descriptor, pphwspGpuAddress, ringGpuAddress;
    uint64_t pageDirectoryEntry, seqnoGpuAddress;
    uint32_t hardwareBound, gpuExecuted, requiredQualifications, reserved;
    LBLrcWrite writes[LB_LRC_MAX_WRITES];
    uint32_t prologue[LB_LRC_MAX_PROLOGUE];
};

static_assert(sizeof(LBLrcLayout) == 88, "LRC layout wire ABI");
static_assert(sizeof(LBLrcWrite) == 8, "LRC write wire ABI");
static_assert(sizeof(LBLrcImage) == 200, "LRC image wire ABI");

extern "C" {
// Compute the LRC allocation layout for one engine class and ring size.
// Only the direct ring-state path is produced: no graphics IP descriptor at
// the pinned Linux revision enables indirect ring state, so an indirect
// layout would be an unqualified claim and is refused instead.
LNL_API uint32_t lb_lrc_layout(uint32_t engineClass, uint64_t ringBytes,
                              uint32_t indirectRingState, LBLrcLayout *out,
                              uint32_t outputBytes);
// Compose the context descriptor for a PPGTT context from the GGTT address of
// its PPHWSP page. Engine class and instance are not part of it on Xe2.
LNL_API uint32_t lb_lrc_descriptor(uint64_t pphwspGpuAddress, uint32_t privileged,
                                  uint64_t *out);
// Build the context-image writes, ring control word, PPGTT root entry, first
// ring instruction and descriptor. `ggttAddress` is the GGTT address of the
// whole allocation; `pageDirectoryAddress` is the DMA address of the PPGTT
// root page. Failure leaves every output byte unchanged.
LNL_API uint32_t lb_lrc_build(const LBLrcLayout *layout, uint64_t ggttAddress,
                             uint64_t pageDirectoryAddress, uint32_t asid,
                             uint32_t ringTail, LBLrcImage *out, uint32_t outputBytes);
// The two control words a context of this ring size carries, without needing
// an allocation or an address. Used by the diagnostic port report.
LNL_API uint32_t lb_lrc_control_words(uint64_t ringBytes, uint32_t *contextControl,
                                     uint32_t *ringControl);
// Apply a built image to a caller-owned context-image buffer of `dwords`
// dwords. The buffer must be the context image itself, not the allocation.
LNL_API uint32_t lb_lrc_apply(const LBLrcImage *image, uint32_t *imageDwords,
                             uint32_t dwordCapacity);
}
