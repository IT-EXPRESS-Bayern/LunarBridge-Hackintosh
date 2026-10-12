// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#pragma once
#include "lnl_platform.h"
#include "lb_forcewake.h"

constexpr uint32_t LB_ABI_VERSION = 1;
constexpr uint32_t LB_TRACE_CAPACITY = 128;
constexpr uint32_t LB_TRACE_BATCH = 16;
constexpr uint64_t LB_BUFFER_MAX_BYTES = 1024 * 1024;
constexpr uint64_t LB_BUFFER_POOL_BYTES = 8 * LB_BUFFER_MAX_BYTES;
constexpr uint32_t LB_MAX_CLIENTS = 8;

enum LBEvent : uint32_t {
    LBStart = 1, LBPciIdentified = 2, LBReadBlocked = 3, LBMapFailed = 4,
    LBGmdRead = 5, LBGmdInvalid = 6, LBReady = 7, LBStop = 8,
    LBClientOpened = 20, LBClientClosed = 21, LBBufferRequested = 22,
    LBBufferAllocated = 23, LBBufferFailed = 24, LBBufferMappingRequested = 25,
    LBMethodRejected = 26, LBSubmitUnsupported = 27, LBBufferReleased = 28,
    LBClientRejected = 29, LBDmaPrepared = 40, LBDmaFailed = 41,
    LBDmaReleased = 42, LBDmaReleaseFailed = 43,
    LBForceWakeFinished = 50, LBForceWakeFailed = 51,
    LBForceWakeCleanup = 52, LBForceWakeCleanupFailed = 53,
    LBArcInterfaceQueried = 60, LBArcRequestAccepted = 61,
    LBArcRequestRejected = 62, LBArcPortQueried = 63
};

enum LBMMIOReason : uint32_t {
    LBReadAllowed = 0, LBOptInMissing = 1, LBUnsupportedPCI = 2,
    LBWrongSubsystem = 3, LBWrongClass = 4, LBMemoryDecodeOff = 5,
    LBPowerUnknown = 6, LBNotD0 = 7, LBProviderNotOn = 8,
    LBBarTooSmall = 9, LBProviderBusy = 10, LBBadInput = 11
};

enum LBCapability : uint32_t {
    LBPciSnapshot = 1, LBTraceAvailable = 2, LBCpuSharedBuffer = 4,
    LBGmdSample = 8,
    // Reserved for future real implementations. Never advertised by this version.
    LBGpuExecution = 1 << 16, LBMetal = 1 << 17
};

struct LBMMIOInput {
    uint32_t vendor, device, subvendor, subdevice, classCode, command;
    uint32_t pmcsr, providerPowerState;
    uint32_t optIn, pmCapabilityPresent;
};

struct LBDeviceSnapshot {
    uint64_t barBytes;
    uint32_t abi, structBytes, vendor, device, subvendor, subdevice;
    uint32_t revision, classCode, pciCommand, pmcsr, flags, policy;
    LnlGmdVersion graphics, media;
    uint32_t gmdRawGraphics, gmdRawMedia, capabilities, reserved;
};

struct LBTraceRecord {
    uint64_t sequence, ticks, arg0, arg1;
    uint32_t event, status, context, reserved;
};

struct LBTraceBatchResult {
    uint32_t abi, count;
    uint64_t oldestSequence, nextSequence, overwritten;
    LBTraceRecord records[LB_TRACE_BATCH];
};

struct LBTraceRing {
    uint64_t nextSequence, overwritten;
    uint32_t count, reserved;
    LBTraceRecord records[LB_TRACE_CAPACITY];
};

// Selectors 0 to 6 keep their ABI-1 input/output shapes unchanged. Selectors 7
// to 9 are additive: they publish the Arc interface table, check one request
// shape against it, and report what the ported Xe2 modules would program.
// None of them submits work; LBSubmit stays unsupported.
enum LBSelector : uint32_t {
    LBGetSnapshot = 0, LBAllocateBuffer = 1, LBGetBufferInfo = 2,
    LBReadTrace = 3, LBSubmit = 4, LBGetDMAInfo = 5,
    LBGetForceWakeInfo = 6, LBGetArcInterface = 7, LBCheckArcRequest = 8,
    LBGetArcPortPlan = 9, LBSelectorCount = 10
};

struct LBBufferInfo {
    uint64_t bytes;
    uint32_t abi, alignment, cpuShared, gpuBound;
};

struct LBDMAInfo {
    uint64_t bytes;
    uint32_t abi, segments, pteEntries, deviceMapperUsed, dmaPrepared, gpuBound;
};

static_assert(sizeof(LBDeviceSnapshot) == 104, "Snapshot wire ABI");
static_assert(sizeof(LBTraceRecord) == 48, "Trace wire ABI");
static_assert(sizeof(LBTraceBatchResult) == 800, "Trace batch wire ABI");
static_assert(sizeof(LBBufferInfo) == 24, "Buffer wire ABI");
static_assert(sizeof(LBDMAInfo) == 32, "DMA info wire ABI without addresses");

extern "C" {
LNL_API uint32_t lb_mmio_preflight(const LBMMIOInput *input, uint64_t barBytes,
                                 uint32_t *graphicsOffset, uint32_t *mediaOffset);
LNL_API int lb_buffer_request(uint64_t requested, uint64_t reserved, uint64_t *rounded);
LNL_API int lb_trace_init(LBTraceRing *ring);
// The native caller serializes the ring with a sleepable IOLock.
LNL_API uint64_t lb_trace_push(LBTraceRing *ring, uint64_t ticks, uint32_t event,
                             uint32_t status, uint32_t context, uint64_t arg0, uint64_t arg1);
// since == 0 means oldest available. Other values are exclusive cursors.
LNL_API int lb_trace_read(const LBTraceRing *ring, uint64_t since, LBTraceBatchResult *out);
}
