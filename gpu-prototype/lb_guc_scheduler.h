// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// GuC wire facts: Intel MIT, Copyright 2014-2024 Intel Corporation.
// See GUC_SCHEDULER_SOURCES.json and LICENSES/MIT-Intel.txt.
#pragma once
#include "lb_guc_transport.h"

// CPU transaction codec for the pinned Xe v6.17 single-LRC KMD profile.
// These phases record local commits / supplied protocol messages only.
// No phase proves a running GuC, a registered queue or executed GPU work.
// Caller serializes access, owns CT sequencing, and retains all real buffers.
constexpr uint32_t LB_GUC_SCHEDULER_LEDGER = 16;

enum LBGuCSchedulerResult : uint32_t {
    LBGuCSchedulerOK = 0, LBGuCSchedulerInput = 1,
    LBGuCSchedulerOwner = 2, LBGuCSchedulerEpoch = 3,
    LBGuCSchedulerState = 4, LBGuCSchedulerCount = 5,
    LBGuCSchedulerFormat = 6, LBGuCSchedulerFence = 7,
    LBGuCSchedulerFull = 8, LBGuCSchedulerStale = 9,
    LBGuCSchedulerCorrupt = 10, LBGuCSchedulerUnexpectedReply = 11
};

enum LBGuCSchedulerPhase : uint32_t {
    LBGuCFresh = 0, LBGuCRegisterCommitted = 1,
    LBGuCEnableAwaitEvent = 2, LBGuCEnabledEventObserved = 3,
    LBGuCDisableAwaitEvent = 4, LBGuCDisabledEventObserved = 5,
    LBGuCDeregisterAwaitEvent = 6, LBGuCDeregisterEventObserved = 7,
    LBGuCFaultObserved = 8
};

enum LBGuCSchedulerOperation : uint32_t {
    LBGuCRegister = 1, LBGuCEnable = 2, LBGuCSubmit = 3,
    LBGuCDisable = 4, LBGuCDeregister = 5
};

struct LBGuCSchedulerLedgerEntry {
    uint32_t fence;
    uint32_t operation;
};

struct LBGuCSchedulerContext {
    uint64_t epoch;
    uint64_t revision;
    uint32_t owner;
    uint32_t contextId;
    uint32_t phase;
    uint32_t ledgerCount;
    LBGuCSchedulerLedgerEntry ledger[LB_GUC_SCHEDULER_LEDGER];
    uint32_t reserved[4];
};

struct LBGuCSchedulerRegistration {
    // Caller-supplied descriptor. Only the pinned LNL wire shape is checked;
    // GGTT ownership, memory contents, DMA lifetime and engine mask are not.
    uint64_t lrcDescriptor;
    uint32_t engineClass;       // GuC class: render = 0 or compute = 4.
    uint32_t engineSubmitMask;  // Logical mask from actual HW/ADS discovery.
    uint32_t reserved[2];
};

struct LBGuCSchedulerPacket {
    uint64_t epoch;
    uint64_t revision;
    uint32_t owner;
    uint32_t contextId;
    uint32_t operation;
    uint32_t reserved;
    LBCtMessage message;
};

struct LBGuCSchedulerObservation {
    uint32_t operation;
    uint32_t action;
    uint32_t hxgType;
    uint32_t contextId;
    uint32_t runnableState;
    uint32_t error;
    uint32_t hint;
    uint32_t reserved;
};

extern "C" {
// Initialize only a zero-filled context. Epoch is a caller's local generation,
// absent from the wire: it cannot distinguish old events for a reused GuC ID.
LNL_API uint32_t lb_guc_scheduler_init(LBGuCSchedulerContext *context,
                                      uint32_t owner, uint32_t contextId,
                                      uint64_t epoch);
// Encode without changing the context. Packet / input / context must be
// disjoint. A failure leaves all outputs unchanged. Registration is required
// only for Register and must be null for other operations. Fence bit15 must
// be set, matching Xe's untracked FastRequest CT convention.
LNL_API uint32_t lb_guc_scheduler_encode(const LBGuCSchedulerContext *context,
                                        uint32_t owner, uint64_t epoch,
                                        uint32_t operation,
                                        const LBGuCSchedulerRegistration *registration,
                                        uint32_t fence, LBGuCSchedulerPacket *packet);
// Commit after a caller's successful CT enqueue; this function sends nothing.
// Failed/replayed/malformed proposals leave state unchanged. Packets are not
// authenticated capabilities: only trusted driver-owned packets may be used.
// Each commit retains
// a fence in the bounded ledger; there is no automatic eviction or ID reuse.
LNL_API uint32_t lb_guc_scheduler_commit(LBGuCSchedulerContext *context,
                                        uint32_t owner, uint64_t epoch,
                                        const LBGuCSchedulerPacket *packet);
// Parse supplied G2H messages into local observations. Only MODE_DONE,
// DEREGISTER_DONE and correlated FastRequest failure are supported. A Fast
// Request has no success/busy/retry reply. Output and state update only after
// validation. Fault observations retain the ledger and require backend reset.
LNL_API uint32_t lb_guc_scheduler_receive(LBGuCSchedulerContext *context,
                                         uint32_t owner, uint64_t epoch,
                                         const LBCtMessage *message,
                                         LBGuCSchedulerObservation *observation);
}
