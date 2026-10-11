// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Executes the production adapter with deterministic IOKit/MMIO doubles only.
#include "../native/LBForceWakeAdapter.h"

namespace {
constexpr uintptr_t FakeBase = 0x20000000;
constexpr uint64_t FullBarBytes = 0x400000;
constexpr uint32_t GraphicsGmd = (20u << 22) | (4u << 14) | 4u;
constexpr uint32_t MediaGmd = (20u << 22) | 1u;
struct Model {
    uint32_t scenario, fault, order, openCalls, openSuccesses, closeCalls;
    uint32_t pciRetains, pciReleases, references, mapCalls, mapReleases;
    uint32_t opened, mapped, claimPossible, badSequence, badRegister, rootFlushErrors;
    uint32_t flushStreak, flushAttempts, flushSuccesses, flushFailures;
    uint32_t requestAttempts, releaseAttempts, successfulControlWrites;
    uint32_t graphicsReads, mediaReads, controlReads, ackReads, sleeps, pmCalls;
    uint32_t lastMapReleaseOrder, lastCloseOrder, lastPCIReleaseOrder;
    uint32_t vendor, device, subvendor, subdevice, classCode, command, pmcsr, powerState;
    uint32_t graphicsGmd, mediaGmd, control, ack, failedRequestSideEffects;
    uint8_t pmOffset;
    uint64_t barBytes, timeUs, requestTimeUs, releaseTimeUs;
    IOService *owner;
} model{};
IOMemoryMap map;

void event() { ++model.order; }
bool activeResources() { return model.mapped || model.opened || model.references; }
void checkAccess(uintptr_t base) {
    event();
    if (base != FakeBase || !model.mapped || !model.opened || model.references != 1)
        ++model.badSequence;
}
void initializeModel(uint32_t scenario) {
    model = {};
    model.scenario = model.fault = scenario;
    model.vendor = 0x8086; model.device = 0x64a0;
    model.subvendor = 0x144d; model.subdevice = 0xc1dd;
    model.classCode = 0x030000; model.command = 2; model.powerState = 2;
    model.pmOffset = 0x40; model.barBytes = FullBarBytes;
    model.graphicsGmd = GraphicsGmd; model.mediaGmd = MediaGmd;
    switch (scenario) {
    case 2: model.vendor = 0x1002; break;
    case 3: model.device = 0x64b0; break;
    case 4: model.subvendor = 0x8086; break;
    case 5: model.subdevice = 0xc1de; break;
    case 6: model.classCode = 0x020000; break;
    case 7: model.command = 0; break;
    case 8: model.pmcsr = 3; break;
    case 9: model.powerState = 1; break;
    case 11: model.pmOffset = 0x3c; break;
    case 15: model.barBytes = 0x380d8f; break;
    case 16: model.graphicsGmd = 0; break;
    case 17: model.graphicsGmd = UINT32_MAX; break;
    case 18: model.mediaGmd = 1; break;
    case 19: model.mediaGmd = UINT32_MAX; break;
    case 20: model.mediaGmd = 0; break;
    case 21: model.graphicsGmd = 19u << 22; break;
    case 22: model.control = 1; model.ack = 1; break;
    case 23: model.ack = 1; break;
    case 34: model.control = UINT32_MAX; break;
    case 35: model.ack = UINT32_MAX; break;
    case 45: model.mediaGmd = 19u << 22; break;
    case 46: model.pmcsr = 0xffff; break;
    case 47: model.command = 0xffff; break;
    case 48: model.classCode = 0x038000; break;
    case 51: model.control = model.ack = 0x400000; break;
    default: break;
    }
}
void clearFaultForRetry() {
    model.fault = 0;
    model.vendor = 0x8086; model.device = 0x64a0;
    model.subvendor = 0x144d; model.subdevice = 0xc1dd;
    model.classCode = 0x030000; model.command = 2;
    model.pmcsr = 0; model.powerState = 2; model.barBytes = FullBarBytes;
}
}

uint64_t IOMemoryMap::getLength() const { return model.barBytes; }
uintptr_t IOMemoryMap::getVirtualAddress() const { return model.fault == 14 ? 0 : FakeBase; }
void IOMemoryMap::release() {
    event();
    if (!model.mapped || !model.opened || model.references != 1 || model.claimPossible)
        ++model.badSequence;
    model.mapped = 0;
    ++model.mapReleases;
    model.lastMapReleaseOrder = model.order;
}
uint16_t IOPCIDevice::configRead16(uint8_t offset) {
    switch (offset) {
    case kIOPCIConfigVendorID: return static_cast<uint16_t>(model.vendor);
    case kIOPCIConfigDeviceID: return static_cast<uint16_t>(model.device);
    case kIOPCIConfigSubSystemVendorID: return static_cast<uint16_t>(model.subvendor);
    case kIOPCIConfigSubSystemID: return static_cast<uint16_t>(model.subdevice);
    case kIOPCIConfigCommand: return static_cast<uint16_t>(model.command);
    default:
        if (offset == static_cast<uint8_t>(model.pmOffset + 4))
            return static_cast<uint16_t>(model.pmcsr);
        ++model.badRegister; return 0xffff;
    }
}
uint32_t IOPCIDevice::configRead32(uint8_t offset) {
    if (offset != kIOPCIConfigRevisionID) { ++model.badRegister; return UINT32_MAX; }
    return model.classCode << 8;
}
bool IOPCIDevice::findPCICapability(uint8_t capability, uint8_t *offset) {
    ++model.pmCalls;
    if (capability != kIOPCIPowerManagementCapability || !offset) { ++model.badRegister; return false; }
    if (model.fault == 10) return false;
    *offset = model.pmOffset;
    return true;
}
bool IOPCIDevice::open(IOService *owner) {
    event(); ++model.openCalls;
    if (!owner || model.opened || model.references || model.mapped) ++model.badSequence;
    if (model.fault == 12) return false;
    model.opened = 1; model.owner = owner; ++model.openSuccesses;
    return true;
}
bool IOPCIDevice::isOpen(IOService *owner) const { return model.opened && owner == model.owner; }
void IOPCIDevice::close(IOService *owner) {
    event(); ++model.closeCalls;
    if (!isOpen(owner) || model.mapped || model.references != 1 || model.claimPossible)
        ++model.badSequence;
    model.opened = 0; model.owner = nullptr;
    model.lastCloseOrder = model.order;
}
void IOPCIDevice::retain() {
    event(); ++model.pciRetains;
    if (!model.opened || model.references || model.mapped) ++model.badSequence;
    ++model.references;
}
void IOPCIDevice::release() {
    event(); ++model.pciReleases;
    if (model.opened || model.mapped || model.references != 1 || model.claimPossible)
        ++model.badSequence;
    if (model.references) --model.references;
    model.lastPCIReleaseOrder = model.order;
}
uint32_t IOPCIDevice::getPowerState() const { return model.powerState; }
IOMemoryMap *IOPCIDevice::mapDeviceMemoryWithRegister(uint8_t reg, uint32_t options) {
    event(); ++model.mapCalls;
    if (reg != kIOPCIConfigBaseAddress0 || options != kIOMapInhibitCache) ++model.badRegister;
    if (!model.opened || model.references != 1 || model.mapped) ++model.badSequence;
    if (model.fault == 13) return nullptr;
    model.mapped = 1;
    if (model.fault == 29) model.pmcsr = 3; // Fresh post-map preflight must reject.
    return &map;
}

uint32_t LBAdapterRead32(uintptr_t base, uint32_t offset) {
    checkAccess(base);
    if (model.flushStreak != 4) ++model.rootFlushErrors;
    model.flushStreak = 0;
    if (offset == 0xd8c) {
        ++model.graphicsReads;
        if (model.fault == 30) model.command = 0;
        if (model.fault == 32) model.vendor = 0x1002;
        if (model.fault == 36) model.barBytes = 0x130033;
        if (model.fault == 41) model.powerState = 1;
        return model.graphicsGmd;
    }
    if (offset == 0x380d8c) { ++model.mediaReads; return model.mediaGmd; }
    if (offset == LB_FW_CONTROL) { ++model.controlReads; return model.control; }
    if (offset != LB_FW_ACK) { ++model.badRegister; return UINT32_MAX; }
    ++model.ackReads;
    if (model.requestAttempts && !model.releaseAttempts) {
        if (model.fault == 25) return UINT32_MAX;
        if (model.fault == 43) return model.ack & ~1u;
        if (model.fault == 24 && model.timeUs - model.requestTimeUs >= 3000) model.ack |= 1;
    }
    if (model.releaseAttempts) {
        if (model.fault == 44) return UINT32_MAX;
        if (model.fault == 28) model.ack |= 1;
        if (model.fault == 24 && model.timeUs - model.releaseTimeUs >= 2000) model.ack &= ~1u;
        if (!(model.ack & 1u)) model.claimPossible = 0;
    }
    return model.ack;
}

bool LBAdapterWrite32(uintptr_t base, uint32_t offset, uint32_t value) {
    checkAccess(base);
    if (offset == LB_LNL_READ_FLUSH) {
        ++model.flushAttempts;
        if (value != 0 || model.flushStreak >= 4) ++model.badRegister;
        if (model.fault == 49 && model.flushAttempts == 2) { ++model.flushFailures; return false; }
        ++model.flushStreak; ++model.flushSuccesses;
        return true;
    }
    if (offset != LB_FW_CONTROL || (value != LB_FW_REQUEST && value != LB_FW_RELEASE)) {
        ++model.badRegister; return false;
    }
    if (model.flushStreak) ++model.rootFlushErrors;
    if (value == LB_FW_REQUEST) {
        ++model.requestAttempts;
        if (model.fault == 50) return false; // No effect; ownership must still be cleaned up.
        model.control |= 1u; model.claimPossible = 1;
        model.requestTimeUs = model.timeUs;
        if (model.fault != 24 && model.fault != 43 && model.fault != 42) model.ack |= 1u;
        if (model.fault == 31) model.pmcsr = 3;
        if (model.fault == 33) model.vendor = 0x1002;
        if (model.fault == 26) { ++model.failedRequestSideEffects; return false; }
    } else {
        ++model.releaseAttempts;
        if (model.fault == 27) return false;
        model.control &= ~1u;
        model.releaseTimeUs = model.timeUs;
        if (model.fault != 24 && model.fault != 28 && model.fault != 44) model.ack &= ~1u;
    }
    ++model.successfulControlWrites;
    return true;
}
void IOSleep(uint32_t milliseconds) {
    if (milliseconds != 1 || !model.opened || !model.mapped || model.references != 1)
        ++model.badSequence;
    ++model.sleeps;
    model.timeUs += uint64_t(milliseconds) * 1000;
    if (model.fault == 42 && model.sleeps == 1) model.timeUs = 0;
}
void clock_get_uptime(uint64_t *ticks) {
    if (!ticks) { ++model.badSequence; return; }
    // Fault42 jumps backwards after a delayed first poll.
    if (model.fault == 42 && !model.sleeps) model.timeUs = 1000;
    *ticks = model.timeUs * 1000;
}
void absolutetime_to_nanoseconds(uint64_t ticks, uint64_t *nanoseconds) {
    if (!nanoseconds) { ++model.badSequence; return; }
    *nanoseconds = ticks;
}

extern "C" LNL_API int lb_forcewake_adapter_test_run(uint32_t scenario, LBForceWakeAdapterTestReport *out) {
    if (!out || scenario > 51) return 0;
    initializeModel(scenario);
    IOPCIDevice device;
    IOService owner;
    LBDeviceSnapshot snapshot{};
    snapshot.policy = scenario == 40 ? LBOptInMissing : LBReadAllowed;
    LBForceWakeAdapter adapter;
    adapter.initialize();
    adapter.sample(scenario == 37 ? nullptr : &device, scenario == 38 ? nullptr : &owner,
                   scenario != 1, scenario == 39 ? nullptr : &snapshot);
    const auto sampled = adapter.info();
    const uint32_t sampledMapReleases = model.mapReleases, sampledClose = model.closeCalls;
    const uint32_t sampledPCIReleases = model.pciReleases, sampledPCIRetains = model.pciRetains;
    const uint32_t heldSample = activeResources() ? 1u : 0u;
    const uint32_t first = adapter.reset();
    const uint32_t heldFirst = activeResources() ? 1u : 0u;
    const uint32_t retainedRetry = heldSample && heldFirst && !model.mapReleases &&
        !model.closeCalls && !model.pciReleases && model.references == 1;
    clearFaultForRetry();
    const uint32_t second = adapter.reset();
    const auto final = adapter.info();
    *out = {1, scenario, sampled.primaryStatus, sampled.cleanupStatus, snapshot.policy,
        sampled.completed, sampled.attempted, sampled.resourcesRetained,
        sampled.reads, sampled.flushWrites, sampled.requestWrites, sampled.leaseReferences,
        snapshot.flags, snapshot.capabilities, snapshot.gmdRawGraphics, snapshot.gmdRawMedia,
        sampled.graphicsIP, sampled.mediaIP, sampledMapReleases, sampledClose,
        sampledPCIReleases, sampledPCIRetains, heldSample, first, heldFirst, second,
        adapter.drained() ? 1u : 0u, final.resourcesRetained, final.reads, final.flushWrites,
        final.requestWrites, final.leaseReferences, final.gpuExecuted, final.metalVerified,
        model.badSequence, model.badRegister, model.rootFlushErrors, model.mapCalls,
        model.mapReleases, model.openCalls, model.openSuccesses, model.closeCalls,
        model.pciRetains, model.pciReleases, model.requestAttempts, model.releaseAttempts,
        model.successfulControlWrites, model.graphicsReads, model.mediaReads,
        model.controlReads, model.ackReads, model.sleeps, model.pmCalls,
        model.lastMapReleaseOrder, model.lastCloseOrder, model.lastPCIReleaseOrder,
        model.control, model.ack, model.failedRequestSideEffects, retainedRetry,
        model.flushAttempts, model.flushSuccesses, model.flushFailures, final.completed};
    return 1;
}
