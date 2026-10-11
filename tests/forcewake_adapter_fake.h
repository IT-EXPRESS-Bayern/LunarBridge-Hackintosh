// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Test API doubles only. No IOKit runtime, device or actual MMIO is used.
#pragma once
#include <stdint.h>

constexpr uint8_t kIOPCIConfigVendorID = 0x00;
constexpr uint8_t kIOPCIConfigDeviceID = 0x02;
constexpr uint8_t kIOPCIConfigCommand = 0x04;
constexpr uint8_t kIOPCIConfigRevisionID = 0x08;
constexpr uint8_t kIOPCIConfigBaseAddress0 = 0x10;
constexpr uint8_t kIOPCIConfigSubSystemVendorID = 0x2c;
constexpr uint8_t kIOPCIConfigSubSystemID = 0x2e;
constexpr uint8_t kIOPCIPowerManagementCapability = 0x01;
constexpr uint32_t kIOMapInhibitCache = 0x100;

// Distinct types avoid an ODR collision with the existing DMA IOKit doubles.
namespace LBForceWakeHost {
class IOService {};
class IOMemoryMap {
public:
    uint64_t getLength() const;
    uintptr_t getVirtualAddress() const;
    void release();
};
class IOPCIDevice {
public:
    uint16_t configRead16(uint8_t offset);
    uint32_t configRead32(uint8_t offset);
    bool findPCICapability(uint8_t capability, uint8_t *offset);
    bool open(IOService *owner);
    bool isOpen(IOService *owner) const;
    void close(IOService *owner);
    void retain();
    void release();
    uint32_t getPowerState() const;
    IOMemoryMap *mapDeviceMemoryWithRegister(uint8_t reg, uint32_t options);
};
}
using IOService = LBForceWakeHost::IOService;
using IOMemoryMap = LBForceWakeHost::IOMemoryMap;
using IOPCIDevice = LBForceWakeHost::IOPCIDevice;

uint32_t LBAdapterRead32(uintptr_t base, uint32_t offset);
bool LBAdapterWrite32(uintptr_t base, uint32_t offset, uint32_t value);
void clock_get_uptime(uint64_t *ticks);
void absolutetime_to_nanoseconds(uint64_t ticks, uint64_t *nanoseconds);
void IOSleep(uint32_t milliseconds);

// All report words are bounded counters/statuses, not addresses. synthetic=1
// distinguishes this host execution from any native hardware evidence.
struct LBForceWakeAdapterTestReport {
    uint32_t synthetic, scenario, samplePrimary, sampleCleanup, samplePolicy;
    uint32_t sampleCompleted, sampleAttempted, sampleResourcesRetained;
    uint32_t sampleReads, sampleFlushWrites, sampleRequestWrites, sampleLeaseReferences;
    uint32_t snapshotFlags, snapshotCapabilities, gmdRawGraphics, gmdRawMedia;
    uint32_t graphicsIP, mediaIP, mapReleasesAfterSample, closeCallsAfterSample;
    uint32_t pciReleasesAfterSample, pciRetainsAfterSample, heldAfterSample;
    uint32_t firstReset, heldAfterFirstReset, secondReset, finalDrained;
    uint32_t finalResourcesRetained, finalReads, finalFlushWrites, finalRequestWrites;
    uint32_t finalLeaseReferences, gpuExecuted, metalVerified, badSequence;
    uint32_t badRegister, rootFlushErrors, mapCalls, mapReleases;
    uint32_t openCalls, openSuccesses, closeCalls, pciRetains, pciReleases;
    uint32_t requestAttempts, releaseAttempts, successfulControlWrites;
    uint32_t gmdGraphicsReads, gmdMediaReads, controlReads, ackReads, sleeps;
    uint32_t pmCapabilityCalls, lastMapReleaseOrder, lastCloseOrder, lastPCIReleaseOrder;
    uint32_t finalControl, finalAck, failedRequestSideEffects, retainedDuringRetry;
    uint32_t flushWriteAttempts, flushWriteSuccesses, flushWriteFailures, finalCompleted;
};
static_assert(sizeof(LBForceWakeAdapterTestReport) == 256, "Synthetic adapter report ABI");
