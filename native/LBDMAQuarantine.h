// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#pragma once
#include "LBDMAContext.h"

// Pure ownership table. The service serializes every operation with its lock;
// it performs IOKit completion and object releases outside that lock.
class LBDMAQuarantine {
public:
    static constexpr uint32_t Capacity = 8;
    struct Held {
        LBDMAContext *context = nullptr;
        IOBufferMemoryDescriptor *buffer = nullptr;
        uint64_t bytes = 0;
        uint32_t owner = 0;
    };
private:
    enum State : uint32_t { Free, Client, Quarantine, Retry };
    struct Slot { Held held{}; State state = Free; };
    Slot _slots[Capacity]{};
public:
    int reserve(uint32_t owner);
    bool releaseClient(uint32_t slot, uint32_t owner);
    bool hold(uint32_t slot, uint32_t owner, LBDMAContext *context,
              IOBufferMemoryDescriptor *buffer, uint64_t bytes);
    bool beginRetry(uint32_t slot, Held *out);
    bool finishRetry(uint32_t slot, uint32_t owner, bool cleanupSucceeded);
    uint32_t retained() const;
};
