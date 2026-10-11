// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#pragma once
#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IOLocks.h>
#include "../gpu-prototype/lb_diagnostics.h"
#include "LBDMAContext.h"
#include "LBDMAQuarantine.h"
#include "LBForceWakeAdapter.h"

class LunarBridgePCI : public IOService {
    OSDeclareDefaultStructors(LunarBridgePCI)
    IOLock *_lock = nullptr;
    IOLock *_hardwareLock = nullptr;
    LBForceWakeAdapter _forcewake;
    bool _forcewakeRetained = false;
    LBDeviceSnapshot _snapshot{};
    LBTraceRing _trace{};
    LBDMAQuarantine _dmaSlots;
    uint64_t _reservedBytes = 0;
    uint32_t _clients = 0, _nextClient = 1, _printed = 0;
    bool _stopping = true, _verbose = false, _dmaProbe = false;
    void publishSnapshot();
public:
    bool init(OSDictionary *dictionary = nullptr) override;
    bool start(IOService *provider) override;
    void stop(IOService *provider) override;
    void free() override;
    void trace(uint32_t event, IOReturn status = kIOReturnSuccess,
               uint32_t context = 0, uint64_t arg0 = 0, uint64_t arg1 = 0);
    bool snapshot(LBDeviceSnapshot *out);
    bool traceBatch(uint64_t since, LBTraceBatchResult *out);
    bool forcewakeInfo(LBForceWakeInfo *out);
    void retryForcewake();
    bool admitClient(uint32_t *id, uint32_t *slot);
    void releaseClient(uint32_t id, uint32_t slot);
    bool quarantineClient(uint32_t id, uint32_t slot, LBDMAContext *context,
                          IOBufferMemoryDescriptor *buffer, uint64_t bytes);
    void retryQuarantine();
    bool reserveBuffer(uint64_t requested, uint64_t *rounded);
    void releaseBuffer(uint64_t bytes);
    IOReturn prepareDMA(LBDMAContext *context, IOBufferMemoryDescriptor *memory,
                        uint32_t owner, uint64_t bytes);
};

class LunarBridgeUserClient : public IOUserClient {
    OSDeclareDefaultStructors(LunarBridgeUserClient)
    IOLock *_lock = nullptr;
    LunarBridgePCI *_owner = nullptr;
    IOBufferMemoryDescriptor *_buffer = nullptr;
    LBDMAContext *_dma = nullptr;
    uint64_t _bytes = 0;
    uint32_t _context = 0, _slot = LBDMAQuarantine::Capacity;
    bool _admitted = false, _closed = true;
    static IOReturn invoke(OSObject *target, void *reference, IOExternalMethodArguments *args);
    IOReturn perform(uint32_t selector, IOExternalMethodArguments *args);
    void traceRequest(uint32_t event, IOReturn status, uint64_t selector);
    void releaseResources();
public:
    bool initWithTask(task_t task, void *securityToken, UInt32 type, OSDictionary *properties) override;
    bool start(IOService *provider) override;
    void stop(IOService *provider) override;
    void free() override;
    IOReturn clientClose() override;
    IOReturn clientDied() override;
    IOReturn externalMethod(uint32_t selector, IOExternalMethodArguments *arguments,
                           IOExternalMethodDispatch *dispatch = nullptr,
                           OSObject *target = nullptr, void *reference = nullptr) override;
    IOReturn clientMemoryForType(UInt32 type, IOOptionBits *options,
                                IOMemoryDescriptor **memory) override;
};
