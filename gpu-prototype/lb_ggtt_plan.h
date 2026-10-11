// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Xe format references are Intel MIT; see ../docs/GGTT_BINDING_SOURCES.json
// and ../LICENSES/MIT-Intel.txt. No MMIO access is implemented here.
#pragma once
#include "lb_gpu_memory.h"

constexpr uint32_t LB_GGTT_PLAN_ABI = 1;
constexpr uint32_t LB_GGTT_MAX_WRITES = LB_VM_MAX_BUFFER / LB_GPU_PAGE;
constexpr uint64_t LB_GGTT_GSM_BAR_OFFSET = 8 * 1024 * 1024;
constexpr uint64_t LB_GGTT_GSM_BYTES = 8 * 1024 * 1024;
constexpr uint64_t LB_GGTT_GUC_TOP = 0xFEE00000;
// Own conservative lower reservation: pinned MAX_WOPCM_SIZE (8 MiB), rather
// than assuming the default LNL WOPCM size (2 MiB) equals current BIOS state.
constexpr uint64_t LB_GGTT_RESERVED_LOW = 8 * 1024 * 1024;
constexpr uint32_t LB_GGTT_SPACE_GLOBAL = 1;
constexpr uint32_t LB_GGTT_XE2_WB_PAT = 2;
constexpr uint32_t LB_GGTT_XE2_WB_PAT_VALUE = 0x0f;

enum LBGgttResult : uint32_t {
    // 0..7 propagate the existing LBVmResult unchanged.
    LBGgttPteMismatch = 8, LBGgttWindowInvalid = 9,
    LBGgttPolicy = 10, LBGgttAliasing = 11
};
enum LBGgttQualification : uint32_t {
    LBGgttQualDeviceMapper = 1 << 0,
    LBGgttQualGlobalOwnership = 1 << 1,
    LBGgttQualMappedBar = 1 << 2,
    LBGgttQualPat = 1 << 3,
    LBGgttQualWopcm = 1 << 4,
    LBGgttQualNativeWrite = 1 << 5,
    LBGgttQualBarrierAndTlb = 1 << 6
};
constexpr uint32_t LB_GGTT_REQUIRED_QUALIFICATIONS = (1 << 7) - 1;

struct LBGgttWindow {
    uint64_t mappedBarBytes, gsmBarOffset, gsmBytes, reservedLowBytes, gpuAddressLimit;
    uint32_t abi, structBytes, addressSpaceKind, patIndex, patValue, reserved[3];
};
struct LBGgttWrite {
    uint64_t barOffset, pte;
};
struct LBGgttPlan {
    uint64_t sourceToken, ggttToken, sourceGPUAddress, ggttGPUAddress;
    uint64_t bytes, firstPteIndex, barWriteBegin, barWriteEndExclusive;
    uint32_t abi, structBytes, owner, writeCount, patIndex, expectedPatValue;
    uint32_t requiredQualifications, hardwareBound, flushCompleted, globalOwnershipVerified;
    uint32_t reserved[2];
    LBGgttWrite writes[LB_GGTT_MAX_WRITES];
};
static_assert(sizeof(LBGgttWindow) == 72, "GGTT CPU window ABI");
static_assert(sizeof(LBGgttWrite) == 16, "GGTT CPU write ABI");
static_assert(sizeof(LBGgttPlan) == 4208, "GGTT CPU plan ABI");

// CPU-only joining of a source DMA/PPGTT reservation and a SEPARATE GGTT
// reservation. The caller owns and serializes both kernel arenas, the mapped
// DMA segments, source staged PPGTT entries and output storage; no user blob
// or physical-address fallback is accepted through a user-client interface.
// Both reservations must be writable WB/PAT2 system memory. The staged PTEs
// are checked against existing lb_vm_stage_ptes before their address bits are
// encoded independently as GGTT PTEs. Raw PPGTT bits are NEVER copied to GGTT.
// Output is committed only after full validation; all failures preserve every
// output byte and all inputs. Output may not overlap any input, and the arenas
// may not alias each other. Counts/capacity never imply allocated storage:
// caller must provide valid kernel memory for the declared bounded arrays.
// metadata ownership is checked; actual GLOBAL hardware ownership is missing.
// No native adapter export, BAR mapping/read/write, PAT programming, scratch
// clear, WOPCM selection, barrier, TLB invalidation or GPU execution occurs.
extern "C" LNL_API uint32_t lb_ggtt_build_plan(
    const LBVmArena *sourceArena, uint64_t sourceToken,
    const LBVmArena *ggttArena, uint64_t ggttToken, uint32_t owner,
    const LBDmaSegment *segments, uint32_t segmentCount,
    const uint64_t *stagedPpgtt, uint32_t stagedCount,
    const LBGgttWindow *window, LBGgttPlan *out, uint32_t outputBytes);
