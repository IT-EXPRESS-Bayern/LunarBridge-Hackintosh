// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Independent CPU codec; Intel MIT wire facts, Copyright 2014-2024 Intel.
// See GUC_SCHEDULER_SOURCES.json and LICENSES/MIT-Intel.txt.
#include "lb_guc_scheduler.h"

namespace {
constexpr uint32_t kRegister = 0x4502;
constexpr uint32_t kDeregister = 0x4503;
constexpr uint32_t kSchedule = 0x1000;
constexpr uint32_t kModeSet = 0x1001;
constexpr uint32_t kModeDone = 0x1002;
constexpr uint32_t kDeregisterDone = 0x4600;
constexpr uint32_t kFastRequest = 2u << 28;
constexpr uint32_t kGuCOrigin = 1u << 31;
constexpr uint64_t kLnlDescriptorMask = 0xfffff000ULL | 0x119ULL;

uint32_t action_for(uint32_t operation) {
    switch (operation) {
        case LBGuCRegister: return kRegister;
        case LBGuCEnable: case LBGuCDisable: return kModeSet;
        case LBGuCSubmit: return kSchedule;
        case LBGuCDeregister: return kDeregister;
        default: return 0;
    }
}

bool overlap(const void *a, uint64_t aBytes, const void *b, uint64_t bBytes) {
    if (!a || !b) return false;
    const uintptr_t left = reinterpret_cast<uintptr_t>(a);
    const uintptr_t right = reinterpret_cast<uintptr_t>(b);
    if (aBytes > UINTPTR_MAX - left || bBytes > UINTPTR_MAX - right) return true;
    return left < right + bBytes && right < left + aBytes;
}

uint32_t validate(const LBGuCSchedulerContext *context, uint32_t owner, uint64_t epoch) {
    if (!context || !owner || !epoch) return LBGuCSchedulerInput;
    if (!context->owner || !context->epoch || !context->revision ||
        context->contextId >= 65535 || context->phase > LBGuCFaultObserved ||
        context->ledgerCount > LB_GUC_SCHEDULER_LEDGER)
        return LBGuCSchedulerCorrupt;
    for (uint32_t item : context->reserved)
        if (item) return LBGuCSchedulerCorrupt;
    for (uint32_t i = 0; i < LB_GUC_SCHEDULER_LEDGER; ++i) {
        const auto &entry = context->ledger[i];
        if (i >= context->ledgerCount) {
            if (entry.fence || entry.operation) return LBGuCSchedulerCorrupt;
            continue;
        }
        if (entry.fence > 0xffff || !(entry.fence & 0x8000) || !action_for(entry.operation))
            return LBGuCSchedulerCorrupt;
        for (uint32_t j = 0; j < i; ++j)
            if (context->ledger[j].fence == entry.fence) return LBGuCSchedulerCorrupt;
    }
    if ((context->phase == LBGuCFresh) != (context->ledgerCount == 0))
        return LBGuCSchedulerCorrupt;
    if (context->ledgerCount) {
        if (context->ledger[0].operation != LBGuCRegister) return LBGuCSchedulerCorrupt;
        for (uint32_t i = 1; i < context->ledgerCount; ++i)
            if (context->ledger[i].operation == LBGuCRegister) return LBGuCSchedulerCorrupt;
        const uint32_t last = context->ledger[context->ledgerCount - 1].operation;
        if ((context->phase == LBGuCRegisterCommitted && (context->ledgerCount != 1 || last != LBGuCRegister)) ||
            (context->phase == LBGuCEnableAwaitEvent && last != LBGuCEnable) ||
            (context->phase == LBGuCEnabledEventObserved && last != LBGuCEnable && last != LBGuCSubmit) ||
            ((context->phase == LBGuCDisableAwaitEvent || context->phase == LBGuCDisabledEventObserved) && last != LBGuCDisable) ||
            ((context->phase == LBGuCDeregisterAwaitEvent || context->phase == LBGuCDeregisterEventObserved) && last != LBGuCDeregister))
            return LBGuCSchedulerCorrupt;
    }
    if (owner != context->owner) return LBGuCSchedulerOwner;
    if (epoch != context->epoch) return LBGuCSchedulerEpoch;
    if (context->revision == UINT64_MAX) return LBGuCSchedulerFull;
    return LBGuCSchedulerOK;
}

uint32_t next_phase(uint32_t phase, uint32_t operation) {
    switch (operation) {
        case LBGuCRegister:
            if (phase == LBGuCFresh) return LBGuCRegisterCommitted;
            break;
        case LBGuCEnable:
            if (phase == LBGuCRegisterCommitted || phase == LBGuCDisabledEventObserved)
                return LBGuCEnableAwaitEvent;
            break;
        case LBGuCSubmit:
            if (phase == LBGuCEnabledEventObserved) return phase;
            break;
        case LBGuCDisable:
            if (phase == LBGuCEnabledEventObserved) return LBGuCDisableAwaitEvent;
            break;
        case LBGuCDeregister:
            if (phase == LBGuCRegisterCommitted || phase == LBGuCDisabledEventObserved)
                return LBGuCDeregisterAwaitEvent;
            break;
        default: break;
    }
    return UINT32_MAX;
}

bool registration_valid(const LBGuCSchedulerRegistration &input) {
    // LNL (>12.50) carries no engine class/instance in upper descriptor bits.
    // xe_lrc_init sets valid and legacy-64-bit addressing, optionally PPGTT.
    return !input.reserved[0] && !input.reserved[1] &&
        (input.engineClass == 0 || input.engineClass == 4) && input.engineSubmitMask &&
        !(input.lrcDescriptor & ~kLnlDescriptorMask) &&
        (input.lrcDescriptor & 0x19) == 0x19 &&
        (input.lrcDescriptor & 0xfffff000ULL) != 0;
}

bool same_packet(const LBGuCSchedulerPacket &a, const LBGuCSchedulerPacket &b) {
    if (a.epoch != b.epoch || a.revision != b.revision || a.owner != b.owner ||
        a.contextId != b.contextId || a.operation != b.operation || a.reserved != b.reserved ||
        a.message.fence != b.message.fence || a.message.count != b.message.count)
        return false;
    for (uint32_t i = 0; i < LB_CT_MAX_DATA; ++i)
        if (a.message.data[i] != b.message.data[i]) return false;
    return true;
}
}

uint32_t lb_guc_scheduler_init(LBGuCSchedulerContext *context, uint32_t owner,
                               uint32_t contextId, uint64_t epoch) {
    if (!context || !owner || !epoch || contextId >= 65535) return LBGuCSchedulerInput;
    // Check members rather than padding: callers supply a zero-filled POD.
    if (context->epoch || context->revision || context->owner || context->contextId ||
        context->phase || context->ledgerCount) return LBGuCSchedulerState;
    for (const auto &entry : context->ledger)
        if (entry.fence || entry.operation) return LBGuCSchedulerCorrupt;
    for (uint32_t item : context->reserved)
        if (item) return LBGuCSchedulerCorrupt;
    LBGuCSchedulerContext result{};
    result.owner = owner;
    result.contextId = contextId;
    result.epoch = epoch;
    result.revision = 1;
    *context = result;
    return LBGuCSchedulerOK;
}

uint32_t lb_guc_scheduler_encode(const LBGuCSchedulerContext *context, uint32_t owner,
                                 uint64_t epoch, uint32_t operation,
                                 const LBGuCSchedulerRegistration *registration,
                                 uint32_t fence, LBGuCSchedulerPacket *packet) {
    if (!packet || overlap(context, sizeof(*context), packet, sizeof(*packet)) ||
        overlap(registration, sizeof(*registration), packet, sizeof(*packet)) ||
        overlap(context, sizeof(*context), registration, sizeof(*registration)))
        return LBGuCSchedulerInput;
    uint32_t error = validate(context, owner, epoch);
    if (error) return error;
    if (!action_for(operation)) return LBGuCSchedulerInput;
    if (next_phase(context->phase, operation) == UINT32_MAX) return LBGuCSchedulerState;
    if (context->ledgerCount == LB_GUC_SCHEDULER_LEDGER) return LBGuCSchedulerFull;
    // Keep room for Disable and Deregister even when repeated Submit encodes
    // consume all regular ledger credits. There is no silent fence eviction.
    const uint32_t cleanup = operation == LBGuCEnable || operation == LBGuCSubmit ? 2 :
                             operation == LBGuCDisable ? 1 : 0;
    if (context->ledgerCount + 1 + cleanup > LB_GUC_SCHEDULER_LEDGER)
        return LBGuCSchedulerFull;
    if (fence > 0xffff || !(fence & 0x8000)) return LBGuCSchedulerFence;
    for (uint32_t i = 0; i < context->ledgerCount; ++i)
        if (context->ledger[i].fence == fence) return LBGuCSchedulerFence;
    if ((operation == LBGuCRegister) != (registration != nullptr)) return LBGuCSchedulerInput;
    if (registration && !registration_valid(*registration)) return LBGuCSchedulerFormat;
    LBGuCSchedulerPacket result{};
    result.epoch = epoch;
    result.revision = context->revision;
    result.owner = owner;
    result.contextId = context->contextId;
    result.operation = operation;
    result.message.fence = fence;
    result.message.data[0] = kFastRequest | action_for(operation);
    if (operation == LBGuCRegister) {
        result.message.count = 12;
        result.message.data[1] = 1; // CONTEXT_REGISTRATION_FLAG_KMD
        result.message.data[2] = context->contextId;
        result.message.data[3] = registration->engineClass;
        result.message.data[4] = registration->engineSubmitMask;
        // Single-LRC registration has no parallel WQ: dwords5..9 stay zero.
        result.message.data[10] = static_cast<uint32_t>(registration->lrcDescriptor);
        result.message.data[11] = static_cast<uint32_t>(registration->lrcDescriptor >> 32);
    } else {
        result.message.count = operation == LBGuCEnable || operation == LBGuCDisable ? 3 : 2;
        result.message.data[1] = context->contextId;
        if (operation == LBGuCEnable) result.message.data[2] = 1;
    }
    *packet = result;
    return LBGuCSchedulerOK;
}

uint32_t lb_guc_scheduler_commit(LBGuCSchedulerContext *context, uint32_t owner,
                                 uint64_t epoch, const LBGuCSchedulerPacket *packet) {
    if (!packet || overlap(context, sizeof(*context), packet, sizeof(*packet)))
        return LBGuCSchedulerInput;
    uint32_t error = validate(context, owner, epoch);
    if (error) return error;
    if (packet->owner != owner) return LBGuCSchedulerOwner;
    if (packet->epoch != epoch) return LBGuCSchedulerEpoch;
    if (packet->revision != context->revision) return LBGuCSchedulerStale;
    if (!packet->message.count || packet->message.count > 12) return LBGuCSchedulerCount;
    LBGuCSchedulerRegistration registration{};
    if (packet->operation == LBGuCRegister) {
        registration.lrcDescriptor = packet->message.data[10] |
            (static_cast<uint64_t>(packet->message.data[11]) << 32);
        registration.engineClass = packet->message.data[3];
        registration.engineSubmitMask = packet->message.data[4];
    }
    LBGuCSchedulerPacket expected{};
    error = lb_guc_scheduler_encode(context, owner, epoch, packet->operation,
        packet->operation == LBGuCRegister ? &registration : nullptr,
        packet->message.fence, &expected);
    if (error) return error;
    if (!same_packet(*packet, expected)) return LBGuCSchedulerFormat;
    LBGuCSchedulerContext result = *context;
    result.phase = next_phase(context->phase, packet->operation);
    result.ledger[result.ledgerCount++] = {packet->message.fence, packet->operation};
    ++result.revision;
    *context = result;
    return LBGuCSchedulerOK;
}

uint32_t lb_guc_scheduler_receive(LBGuCSchedulerContext *context, uint32_t owner,
                                  uint64_t epoch, const LBCtMessage *message,
                                  LBGuCSchedulerObservation *observation) {
    if (!message || !observation || overlap(context, sizeof(*context), message, sizeof(*message)) ||
        overlap(context, sizeof(*context), observation, sizeof(*observation)) ||
        overlap(message, sizeof(*message), observation, sizeof(*observation)))
        return LBGuCSchedulerInput;
    uint32_t error = validate(context, owner, epoch);
    if (error) return error;
    if (!message->count || message->count > 3) return LBGuCSchedulerCount;
    if (message->fence > 0xffff || !(message->data[0] & kGuCOrigin))
        return LBGuCSchedulerFormat;
    const uint32_t type = (message->data[0] >> 28) & 7;
    if (type != 1 && type != 6) return LBGuCSchedulerUnexpectedReply;
    if (context->phase == LBGuCFaultObserved) return LBGuCSchedulerState;
    LBGuCSchedulerContext result = *context;
    LBGuCSchedulerObservation observed{};
    observed.hxgType = type;
    observed.contextId = context->contextId;
    if (type == 6) {
        if (message->count != 1) return LBGuCSchedulerCount;
        if (!(message->fence & 0x8000)) return LBGuCSchedulerFence;
        bool found = false;
        for (uint32_t i = 0; i < context->ledgerCount; ++i) {
            if (context->ledger[i].fence == message->fence) {
                observed.operation = context->ledger[i].operation;
                found = true;
                break;
            }
        }
        if (!found) return LBGuCSchedulerFence;
        observed.action = action_for(observed.operation);
        observed.error = message->data[0] & 0xffff;
        observed.hint = (message->data[0] >> 16) & 0xfff;
        result.phase = LBGuCFaultObserved;
    } else {
        if (message->data[0] & 0x0fff0000) return LBGuCSchedulerFormat;
        observed.action = message->data[0] & 0xffff;
        const uint32_t required = observed.action == kModeDone ? 3 :
                                  observed.action == kDeregisterDone ? 2 : 0;
        if (!required) return LBGuCSchedulerUnexpectedReply;
        if (message->count != required) return LBGuCSchedulerCount;
        if (message->data[1] != context->contextId) return LBGuCSchedulerOwner;
        if (observed.action == kModeDone) {
            observed.runnableState = message->data[2];
            if (context->phase == LBGuCEnableAwaitEvent && observed.runnableState == 1) {
                observed.operation = LBGuCEnable;
                result.phase = LBGuCEnabledEventObserved;
            } else if (context->phase == LBGuCDisableAwaitEvent && observed.runnableState == 0) {
                observed.operation = LBGuCDisable;
                result.phase = LBGuCDisabledEventObserved;
            } else return LBGuCSchedulerState;
        } else {
            if (context->phase != LBGuCDeregisterAwaitEvent) return LBGuCSchedulerState;
            observed.operation = LBGuCDeregister;
            result.phase = LBGuCDeregisterEventObserved;
        }
    }
    ++result.revision;
    *context = result;
    *observation = observed;
    return LBGuCSchedulerOK;
}
