// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Register constants/protocol derived from MIT-licensed Linux v6.17 Xe.
// Intel copyright and notices are retained in SOURCE-NOTICES.txt.
#pragma once
#include "lnl_platform.h"

constexpr uint32_t LB_FW_CONTROL = 0xa188;
constexpr uint32_t LB_FW_ACK = 0x0dfc;
constexpr uint32_t LB_FW_REQUEST = 0x10001;
constexpr uint32_t LB_FW_RELEASE = 0x10000;
constexpr uint32_t LB_LNL_READ_FLUSH = 0x130030;
constexpr uint64_t LB_FW_TIMEOUT_US = 50000;

enum LBWakeResult : uint32_t {
    LBWakeOK = 0, LBWakeBadIO = 1, LBWakeBadState = 2, LBWakeBusy = 3,
    LBWakeIOFailed = 4, LBWakeInvalidMMIO = 5, LBWakeTimeout = 6,
    LBWakeClockFailed = 7, LBWakeIdle = 8, LBWakeUnsupportedIP = 9
};

// Callers serialize this state. A failed write may have reached the device;
// owned/unresolved therefore become true BEFORE issuing a request.
struct LBForceWakeState {
    uint32_t magic, references, owned, awake, unresolved;
    uint32_t reads, writes, polls, lastAck, reserved[3];
};

struct LBRegisterIO {
    void *context;
    bool (*read32)(void *, uint32_t, uint32_t *);
    bool (*write32)(void *, uint32_t, uint32_t);
    bool (*sleepUs)(void *, uint32_t);
    uint64_t (*nowUs)(void *);
};

// Address-free user-client output. This confirms a wake handshake only;
// neither GPU command execution nor Metal is inferred from it.
struct LBForceWakeInfo {
    uint32_t abi, structBytes, enabled, attempted, preflight;
    uint32_t primaryStatus, cleanupStatus, completed, reads, requestWrites;
    uint32_t flushWrites, lastAck, polls, resourcesRetained, gpuExecuted;
    uint32_t metalVerified, graphicsIP, mediaIP, leaseReferences, reserved;
};
static_assert(sizeof(LBForceWakeInfo) == 80, "Forcewake wire ABI");
static_assert(sizeof(LBForceWakeState) == 48, "Forcewake state layout");

extern "C" {
LNL_API uint32_t lb_forcewake_init(LBForceWakeState *state);
LNL_API uint32_t lb_forcewake_get(LBForceWakeState *state, const LBRegisterIO *io);
LNL_API uint32_t lb_forcewake_put(LBForceWakeState *state, const LBRegisterIO *io);
LNL_API int lb_forcewake_drained(const LBForceWakeState *state);
}
