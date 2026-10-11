// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#include "LBDMAQuarantine.h"

int LBDMAQuarantine::reserve(uint32_t owner) {
    if (!owner) return -1;
    for (uint32_t i = 0; i < Capacity; ++i)
        if (_slots[i].state != Free && _slots[i].held.owner == owner) return -1;
    for (uint32_t i = 0; i < Capacity; ++i) {
        if (_slots[i].state == Free) {
            _slots[i].held.owner = owner;
            _slots[i].state = Client;
            return static_cast<int>(i);
        }
    }
    return -1;
}

bool LBDMAQuarantine::releaseClient(uint32_t slot, uint32_t owner) {
    if (slot >= Capacity || !owner || _slots[slot].state != Client ||
        _slots[slot].held.owner != owner) return false;
    _slots[slot] = {};
    return true;
}

bool LBDMAQuarantine::hold(uint32_t slot, uint32_t owner, LBDMAContext *context,
                         IOBufferMemoryDescriptor *buffer, uint64_t bytes) {
    if (slot >= Capacity || !owner || !context || !buffer || context->drained() ||
        !bytes || bytes > LB_VM_MAX_BUFFER || bytes % LB_GPU_PAGE ||
        buffer->getLength() != bytes || _slots[slot].state != Client ||
        _slots[slot].held.owner != owner) return false;
    for (uint32_t i = 0; i < Capacity; ++i)
        if (_slots[i].held.context == context || _slots[i].held.buffer == buffer) return false;
    _slots[slot].held = {context, buffer, bytes, owner};
    _slots[slot].state = Quarantine;
    return true;
}

bool LBDMAQuarantine::beginRetry(uint32_t slot, Held *out) {
    if (slot >= Capacity || !out || _slots[slot].state != Quarantine) return false;
    *out = _slots[slot].held;
    _slots[slot].state = Retry;
    return true;
}

bool LBDMAQuarantine::finishRetry(uint32_t slot, uint32_t owner, bool cleanupSucceeded) {
    if (slot >= Capacity || !owner || _slots[slot].state != Retry ||
        _slots[slot].held.owner != owner) return false;
    if (cleanupSucceeded && !_slots[slot].held.context->drained()) return false;
    if (cleanupSucceeded) _slots[slot] = {};
    else _slots[slot].state = Quarantine;
    return true;
}

uint32_t LBDMAQuarantine::retained() const {
    uint32_t count = 0;
    for (uint32_t i = 0; i < Capacity; ++i)
        if (_slots[i].state == Quarantine || _slots[i].state == Retry) ++count;
    return count;
}
