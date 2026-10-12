// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#include "LunarBridgeDriver.h"
#include "../gpu-prototype/lb_arc_interface.h"
#include <IOKit/pci/IOPCIDevice.h>
#include <libkern/c++/OSBoolean.h>
#include <kern/clock.h>
#include <libkern/libkern.h>

#if !defined(__x86_64__)
#error The current Lunar Lake diagnostic backend requires x86_64.
#endif

OSDefineMetaClassAndStructors(LunarBridgePCI, IOService)
OSDefineMetaClassAndStructors(LunarBridgeUserClient, IOUserClient)

bool LunarBridgePCI::init(OSDictionary *dictionary) {
    if (!IOService::init(dictionary)) return false;
    _lock = IOLockAlloc();
    if (!_lock) return false;
    _hardwareLock = IOLockAlloc();
    if (!_hardwareLock) return false;
    _forcewake.initialize();
    lb_trace_init(&_trace);
    _snapshot.abi = LB_ABI_VERSION;
    _snapshot.structBytes = sizeof(_snapshot);
    _snapshot.capabilities = LBPciSnapshot | LBTraceAvailable | LBCpuSharedBuffer;
    return true;
}

void LunarBridgePCI::trace(uint32_t event, IOReturn status, uint32_t context,
                           uint64_t arg0, uint64_t arg1) {
    if (!_lock) return;
    uint64_t ticks = 0;
    clock_get_uptime(&ticks);
    IOLockLock(_lock);
    const uint64_t sequence = lb_trace_push(&_trace, ticks, event, static_cast<uint32_t>(status), context, arg0, arg1);
    const bool print = (_verbose || status != kIOReturnSuccess || event == LBStart ||
                        event == LBReady || event == LBStop) && _printed < 256;
    if (print) ++_printed;
    IOLockUnlock(_lock);
    if (print)
        IOLog("[LunarBridge] seq=%llu event=%u status=0x%08x context=%u a0=%llu a1=%llu\n",
              static_cast<unsigned long long>(sequence), event, static_cast<uint32_t>(status), context,
              static_cast<unsigned long long>(arg0), static_cast<unsigned long long>(arg1));
}

void LunarBridgePCI::publishSnapshot() {
    setProperty("LBDriverVersion", "0.2.0d1");
    setProperty("LBDriverABI", LB_ABI_VERSION, 32);
    setProperty("LBPCIIdentifier", (_snapshot.device << 16) | _snapshot.vendor, 32);
    setProperty("LBSubsystemIdentifier", (_snapshot.subdevice << 16) | _snapshot.subvendor, 32);
    setProperty("LBRevision", _snapshot.revision, 32);
    setProperty("LBBarBytes", _snapshot.barBytes, 64);
    setProperty("LBMMIOPolicy", _snapshot.policy, 32);
    setProperty("LBGmdSamplesValid", _snapshot.capabilities & LBGmdSample ? true : false);
    setProperty("LBActualMMIOPerformed", _snapshot.flags & 16 ? true : false);
    setProperty("LBGraphicsIP", _snapshot.graphics.version_x100, 32);
    setProperty("LBMediaIP", _snapshot.media.version_x100, 32);
    setProperty("LBTraceCapacity", LB_TRACE_CAPACITY, 32);
    setProperty("LBCpuBufferMaxBytes", LB_BUFFER_MAX_BYTES, 64);
    setProperty("LBEnableDMAPreparation", _dmaProbe);
    setProperty("LBDMAMappingVerifiedOnTarget", false);
    setProperty("LBDMATeardownQuarantined", false);
    setProperty("LBDMAQuarantineSlots", static_cast<uint64_t>(0), 32);
    setProperty("LBDMAResourcesDrained", false);
    setProperty("LBGPUExecutionSupported", false);
    setProperty("LBMetalSupported", false);
    setProperty("LBStage", "native-diagnostic-source; hardware validation pending");
    const auto &wake = _forcewake.info();
    setProperty("LBEnableForceWakeProbe", wake.enabled != 0);
    setProperty("LBForceWakeCompleted", wake.completed != 0);
    setProperty("LBForceWakeStatus", wake.primaryStatus, 32);
    setProperty("LBForceWakeCleanupStatus", wake.cleanupStatus, 32);
    setProperty("LBForceWakeResourcesRetained", wake.resourcesRetained != 0);
    setProperty("LBLunarLakeReadFlushWrites", wake.flushWrites, 32);
}

bool LunarBridgePCI::start(IOService *provider) {
    if (!IOService::start(provider)) return false;
    auto pci = OSDynamicCast(IOPCIDevice, provider);
    if (!pci) { trace(LBStart, kIOReturnBadArgument); return false; }
    auto verbose = OSDynamicCast(OSBoolean, getProperty("LBDebugLogging"));
    _verbose = verbose && verbose->isTrue();
    auto dma = OSDynamicCast(OSBoolean, getProperty("LBEnableDMAPreparation"));
    _dmaProbe = dma && dma->isTrue();
    trace(LBStart);
    _snapshot.vendor = pci->configRead16(kIOPCIConfigVendorID);
    _snapshot.device = pci->configRead16(kIOPCIConfigDeviceID);
    _snapshot.subvendor = pci->configRead16(kIOPCIConfigSubSystemVendorID);
    _snapshot.subdevice = pci->configRead16(kIOPCIConfigSubSystemID);
    const uint32_t classRevision = pci->configRead32(kIOPCIConfigRevisionID);
    _snapshot.revision = classRevision & 0xff;
    _snapshot.classCode = classRevision >> 8;
    if (_snapshot.vendor != 0x8086 || _snapshot.device != 0x64a0 ||
        _snapshot.subvendor != 0x144d || _snapshot.subdevice != 0xc1dd) {
        trace(LBPciIdentified, kIOReturnUnsupported);
        return false;
    }
    trace(LBPciIdentified, kIOReturnSuccess, 0, _snapshot.device, _snapshot.revision);
    auto memory = pci->getDeviceMemoryWithRegister(kIOPCIConfigBaseAddress0);
    _snapshot.barBytes = memory ? memory->getLength() : 0;
    _snapshot.pciCommand = pci->configRead16(kIOPCIConfigCommand);
    uint8_t pmOffset = 0;
    const bool pmPresent = pci->findPCICapability(kIOPCIPowerManagementCapability, &pmOffset) &&
                           pmOffset >= 0x40 && pmOffset <= 0xf8;
    _snapshot.pmcsr = pmPresent ? pci->configRead16(static_cast<uint8_t>(pmOffset + 4)) : 0xffff;
    auto optIn = OSDynamicCast(OSBoolean, getProperty("LBEnableMMIORead"));
    auto wakeOptIn = OSDynamicCast(OSBoolean, getProperty("LBEnableForceWakeProbe"));
    const bool wakeEnabled = wakeOptIn && wakeOptIn->isTrue();
    const bool enabled = (optIn && optIn->isTrue()) || wakeEnabled;
    _snapshot.flags = (pmPresent ? 1u : 0u) | (enabled ? 2u : 0u) |
                      (_dmaProbe ? 32u : 0u) | (wakeEnabled ? 64u : 0u);
    LBMMIOInput input{_snapshot.vendor, _snapshot.device, _snapshot.subvendor, _snapshot.subdevice,
                      _snapshot.classCode, _snapshot.pciCommand, _snapshot.pmcsr,
                      pci->getPowerState(), enabled ? 1u : 0u, pmPresent ? 1u : 0u};
    uint32_t gfxOffset = 0, mediaOffset = 0;
    _snapshot.policy = lb_mmio_preflight(&input, _snapshot.barBytes, &gfxOffset, &mediaOffset);
    IOLockLock(_hardwareLock);
    _forcewake.sample(pci, this, wakeEnabled, &_snapshot);
    if (!_forcewake.drained()) {
        // Exactly one service reference owns a possibly active request, BAR
        // mapping and provider-open token. A failed put must not unload them.
        retain();
        _forcewakeRetained = true;
    }
    const auto wake = _forcewake.info();
    IOLockUnlock(_hardwareLock);
    if (_snapshot.flags & 16) {
        trace(LBGmdRead, kIOReturnSuccess, 0, _snapshot.gmdRawGraphics, _snapshot.gmdRawMedia);
        if (!(_snapshot.capabilities & LBGmdSample)) trace(LBGmdInvalid, kIOReturnNotReady);
    } else {
        trace(LBReadBlocked, enabled ? kIOReturnNotReady : kIOReturnSuccess, 0, _snapshot.policy);
        if (enabled && _snapshot.policy == LBBarTooSmall) trace(LBMapFailed, kIOReturnNotReady);
        if (wake.primaryStatus == LBWakeInvalidMMIO) trace(LBGmdInvalid, kIOReturnNotReady);
    }
    if (wake.enabled && (wake.attempted || wake.primaryStatus != LBWakeIdle))
        trace(wake.completed ? LBForceWakeFinished : LBForceWakeFailed,
              wake.completed ? kIOReturnSuccess : kIOReturnNotReady, 0, wake.primaryStatus, wake.cleanupStatus);
    publishSnapshot();
    IOLockLock(_lock);
    _stopping = false;
    IOLockUnlock(_lock);
    trace(LBReady);
    registerService();
    return true;
}

void LunarBridgePCI::stop(IOService *provider) {
    IOLockLock(_lock);
    _stopping = true;
    const bool drained = _clients == 0 && _dmaSlots.retained() == 0;
    IOLockUnlock(_lock);
    retryForcewake();
    setProperty("LBDMAResourcesDrained", drained);
    trace(LBStop);
    IOService::stop(provider);
}

void LunarBridgePCI::free() {
    // An unresolved adapter holds its own service reference; free is reached
    // only after the hardware-lock protected release path has drained it.
    if (_hardwareLock) { IOLockFree(_hardwareLock); _hardwareLock = nullptr; }
    if (_lock) { IOLockFree(_lock); _lock = nullptr; }
    IOService::free();
}

bool LunarBridgePCI::snapshot(LBDeviceSnapshot *out) {
    if (!out || !_lock) return false;
    IOLockLock(_lock);
    const bool ready = !_stopping;
    if (ready) *out = _snapshot;
    IOLockUnlock(_lock);
    return ready;
}

bool LunarBridgePCI::traceBatch(uint64_t since, LBTraceBatchResult *out) {
    if (!_lock) return false;
    IOLockLock(_lock);
    const bool result = lb_trace_read(&_trace, since, out);
    IOLockUnlock(_lock);
    return result;
}

bool LunarBridgePCI::forcewakeInfo(LBForceWakeInfo *out) {
    if (!out || !_hardwareLock) return false;
    IOLockLock(_hardwareLock);
    *out = _forcewake.info();
    IOLockUnlock(_hardwareLock);
    return true;
}

void LunarBridgePCI::retryForcewake() {
    if (!_hardwareLock) return;
    retain(); // Keep this alive while dropping a transferred adapter reference.
    IOLockLock(_hardwareLock);
    const bool pending = !_forcewake.drained();
    const uint32_t status = pending ? _forcewake.reset() : LBWakeIdle;
    const bool dropHold = _forcewakeRetained && _forcewake.drained();
    if (dropHold) _forcewakeRetained = false;
    const auto wake = _forcewake.info();
    IOLockUnlock(_hardwareLock);
    if (pending) {
        setProperty("LBForceWakeCompleted", wake.completed != 0);
        setProperty("LBForceWakeCleanupStatus", wake.cleanupStatus, 32);
        setProperty("LBForceWakeResourcesRetained", wake.resourcesRetained != 0);
        setProperty("LBLunarLakeReadFlushWrites", wake.flushWrites, 32);
        trace(wake.resourcesRetained ? LBForceWakeCleanupFailed : LBForceWakeCleanup,
              wake.resourcesRetained ? kIOReturnNotReady : kIOReturnSuccess, 0, status);
    }
    if (dropHold) release(); // Never release the last reference under a lock.
    release();
}

bool LunarBridgePCI::admitClient(uint32_t *id, uint32_t *slot) {
    if (!id || !slot) return false;
    IOLockLock(_lock);
    int reserved = -1;
    if (!_stopping && _clients < LB_MAX_CLIENTS && _nextClient != 0)
        reserved = _dmaSlots.reserve(_nextClient);
    const bool available = reserved >= 0;
    if (available) { ++_clients; *id = _nextClient++; *slot = static_cast<uint32_t>(reserved); }
    IOLockUnlock(_lock);
    return available;
}

void LunarBridgePCI::releaseClient(uint32_t id, uint32_t slot) {
    IOLockLock(_lock);
    if (_dmaSlots.releaseClient(slot, id) && _clients) --_clients;
    IOLockUnlock(_lock);
}

bool LunarBridgePCI::quarantineClient(uint32_t id, uint32_t slot, LBDMAContext *context,
                                      IOBufferMemoryDescriptor *buffer, uint64_t bytes) {
    retain(); // A concurrent retry may release the transferred slot reference.
    IOLockLock(_lock);
    const bool transferred = _dmaSlots.hold(slot, id, context, buffer, bytes);
    const uint32_t held = _dmaSlots.retained();
    IOLockUnlock(_lock);
    if (transferred) {
        // Transfer the client's already-held service reference to this slot.
        // Byte/client reservations stay occupied until completion succeeds.
        setProperty("LBDMATeardownQuarantined", true);
        setProperty("LBDMAQuarantineSlots", held, 32);
        setProperty("LBDMAResourcesDrained", false);
    }
    release(); // Outside the service lock, after the last access to this.
    return transferred;
}

void LunarBridgePCI::retryQuarantine() {
    // Called before admission on an administrative client thread, never from
    // the workloop or under either client/service lock. No new device mapping.
    retain();
    for (uint32_t slot = 0; slot < LBDMAQuarantine::Capacity; ++slot) {
        LBDMAQuarantine::Held held{};
        IOLockLock(_lock);
        const bool stopped = _stopping;
        const bool claimed = !stopped && _dmaSlots.beginRetry(slot, &held);
        IOLockUnlock(_lock);
        if (stopped) break;
        if (!claimed) continue;
        const IOReturn status = held.context->reset();
        IOLockLock(_lock);
        const bool committed = _dmaSlots.finishRetry(slot, held.owner, status == kIOReturnSuccess);
        if (committed && status == kIOReturnSuccess) {
            _reservedBytes -= held.bytes;
            --_clients;
        }
        const uint32_t remaining = _dmaSlots.retained();
        IOLockUnlock(_lock);
        setProperty("LBDMATeardownQuarantined", remaining != 0);
        setProperty("LBDMAQuarantineSlots", remaining, 32);
        trace(status == kIOReturnSuccess && committed ? LBDmaReleased : LBDmaReleaseFailed,
              committed ? status : kIOReturnInternalError, held.owner, held.bytes);
        if (committed && status == kIOReturnSuccess) {
            held.buffer->release();
            delete held.context;
            release(); // Exactly one transferred slot/service reference.
        }
    }
    release(); // Last operation: loop-wide temporary service reference.
}

bool LunarBridgePCI::reserveBuffer(uint64_t requested, uint64_t *rounded) {
    IOLockLock(_lock);
    const bool ok = !_stopping && lb_buffer_request(requested, _reservedBytes, rounded);
    if (ok) _reservedBytes += *rounded;
    IOLockUnlock(_lock);
    return ok;
}

void LunarBridgePCI::releaseBuffer(uint64_t bytes) {
    IOLockLock(_lock);
    if (bytes <= _reservedBytes) _reservedBytes -= bytes;
    IOLockUnlock(_lock);
}

IOReturn LunarBridgePCI::prepareDMA(LBDMAContext *context, IOBufferMemoryDescriptor *memory,
                                   uint32_t owner, uint64_t bytes) {
    if (!context || !memory) return kIOReturnBadArgument;
    IOLockLock(_lock);
    if (_stopping) { IOLockUnlock(_lock); return kIOReturnNotReady; }
    if (!_dmaProbe) { IOLockUnlock(_lock); return kIOReturnSuccess; }
    auto pci = OSDynamicCast(IOPCIDevice, getProvider());
    if (pci) pci->retain();
    IOLockUnlock(_lock);
    if (!pci) return kIOReturnNotReady;
    IOReturn status = context->prepare(pci, memory, owner, bytes);
    pci->release();
    IOLockLock(_lock);
    const bool stopped = _stopping;
    IOLockUnlock(_lock);
    if (stopped && status == kIOReturnSuccess) {
        status = context->reset();
        if (status == kIOReturnSuccess) status = kIOReturnNotReady;
    }
    trace(status == kIOReturnSuccess ? LBDmaPrepared : LBDmaFailed,
          status, owner, bytes, context->segments());
    return status;
}

bool LunarBridgeUserClient::initWithTask(task_t task, void *token, UInt32 type, OSDictionary *properties) {
    if (type != 0 || clientHasPrivilege(token, kIOClientPrivilegeAdministrator) != kIOReturnSuccess)
        return false;
    return IOUserClient::initWithTask(task, token, type, properties);
}

bool LunarBridgeUserClient::start(IOService *provider) {
    if (!IOUserClient::start(provider)) return false;
    _owner = OSDynamicCast(LunarBridgePCI, provider);
    if (!_owner) return false;
    _owner->retain();
    _lock = IOLockAlloc();
    _dma = new LBDMAContext();
    _owner->retryQuarantine();
    _owner->retryForcewake();
    if (!_lock || !_dma || !_owner->admitClient(&_context, &_slot)) {
        _owner->trace(LBClientRejected, kIOReturnNoResources);
        return false;
    }
    _admitted = true;
    _closed = false;
    _owner->trace(LBClientOpened, kIOReturnSuccess, _context);
    return true;
}

IOReturn LunarBridgeUserClient::externalMethod(uint32_t selector, IOExternalMethodArguments *args,
                                               IOExternalMethodDispatch *, OSObject *, void *) {
    static IOExternalMethodDispatch methods[LBSelectorCount] = {
        {invoke, 0, 0, 0, sizeof(LBDeviceSnapshot)},
        {invoke, 1, 0, 1, 0},
        {invoke, 0, 0, 0, sizeof(LBBufferInfo)},
        {invoke, 1, 0, 0, sizeof(LBTraceBatchResult)},
        {invoke, 0, 0, 0, 0},
        {invoke, 0, 0, 0, sizeof(LBDMAInfo)},
        {invoke, 0, 0, 0, sizeof(LBForceWakeInfo)},
        {invoke, 0, 0, 0, sizeof(LBArcInterfaceReport)},
        {invoke, 0, sizeof(LBArcRequest), 0, sizeof(LBArcDecision)},
        {invoke, 0, 0, 0, sizeof(LBArcPortReport)}
    };
    if (!args || selector >= LBSelectorCount) {
        traceRequest(LBMethodRejected, kIOReturnBadArgument, selector);
        return kIOReturnBadArgument;
    }
    // No indirect input/output descriptors: all supported wire payloads fit in 800 bytes.
    if (args->structureInputDescriptor || args->structureOutputDescriptor || args->asyncWakePort) {
        traceRequest(LBMethodRejected, kIOReturnBadArgument, selector);
        return kIOReturnBadArgument;
    }
    const IOReturn result = IOUserClient::externalMethod(selector, args, &methods[selector], this,
                            reinterpret_cast<void *>(static_cast<uintptr_t>(selector)));
    if (result != kIOReturnSuccess && result != kIOReturnUnsupported)
        traceRequest(LBMethodRejected, result, selector);
    return result;
}

void LunarBridgeUserClient::traceRequest(uint32_t event, IOReturn status, uint64_t selector) {
    if (!_lock) return;
    IOLockLock(_lock);
    auto owner = _owner;
    const uint32_t context = _context;
    if (owner) owner->retain();
    IOLockUnlock(_lock);
    if (owner) {
        owner->trace(event, status, context, selector);
        owner->release(); // A last service reference is never released under _lock.
    }
}

IOReturn LunarBridgeUserClient::invoke(OSObject *target, void *reference, IOExternalMethodArguments *args) {
    auto client = OSDynamicCast(LunarBridgeUserClient, target);
    if (!client || !client->_lock) return kIOReturnNotReady;
    IOLockLock(client->_lock);
    const IOReturn result = client->_closed || !client->_owner ? kIOReturnNotReady :
        client->perform(static_cast<uint32_t>(reinterpret_cast<uintptr_t>(reference)), args);
    IOLockUnlock(client->_lock);
    return result;
}

IOReturn LunarBridgeUserClient::perform(uint32_t selector, IOExternalMethodArguments *args) {
    switch (selector) {
        case LBGetSnapshot:
            return args->structureOutput && _owner->snapshot(static_cast<LBDeviceSnapshot *>(args->structureOutput))
                ? kIOReturnSuccess : kIOReturnNotReady;
        case LBAllocateBuffer: {
            if (!args->scalarInput || !args->scalarOutput) return kIOReturnBadArgument;
            const uint64_t requested = args->scalarInput[0];
            _owner->trace(LBBufferRequested, kIOReturnSuccess, _context, requested);
            if (_buffer) return kIOReturnExclusiveAccess;
            uint64_t bytes = 0;
            if (!_owner->reserveBuffer(requested, &bytes)) {
                _owner->trace(LBBufferFailed, kIOReturnNoResources, _context, requested);
                return kIOReturnNoResources;
            }
            _buffer = IOBufferMemoryDescriptor::inTaskWithOptions(kernel_task,
                       kIOMemoryKernelUserShared | kIODirectionInOut | kIOMapCopybackCache, bytes, 4096);
            if (!_buffer || !_buffer->getBytesNoCopy()) {
                if (_buffer) { _buffer->release(); _buffer = nullptr; }
                _owner->releaseBuffer(bytes);
                _owner->trace(LBBufferFailed, kIOReturnNoMemory, _context, bytes);
                return kIOReturnNoMemory;
            }
            bzero(_buffer->getBytesNoCopy(), static_cast<size_t>(bytes));
            _bytes = bytes;
            const IOReturn dmaStatus = _owner->prepareDMA(_dma, _buffer, _context, bytes);
            if (dmaStatus != kIOReturnSuccess) {
                _owner->trace(LBBufferFailed, dmaStatus, _context, bytes);
                _closed = true;
                releaseResources();
                return dmaStatus;
            }
            args->scalarOutput[0] = bytes;
            _owner->trace(LBBufferAllocated, kIOReturnSuccess, _context, bytes);
            return kIOReturnSuccess;
        }
        case LBGetBufferInfo: {
            if (!_buffer || !args->structureOutput) return kIOReturnNotReady;
            *static_cast<LBBufferInfo *>(args->structureOutput) = {_bytes, LB_ABI_VERSION, 4096, 1, 0};
            return kIOReturnSuccess;
        }
        case LBReadTrace:
            if (!args->scalarInput || !args->structureOutput) return kIOReturnBadArgument;
            return _owner->traceBatch(args->scalarInput[0], static_cast<LBTraceBatchResult *>(args->structureOutput))
                ? kIOReturnSuccess : kIOReturnBadArgument;
        case LBGetDMAInfo:
            if (!_buffer || !args->structureOutput) return kIOReturnNotReady;
            *static_cast<LBDMAInfo *>(args->structureOutput) = {
                _dma->bytes(), LB_ABI_VERSION, _dma->segments(), _dma->ptes(),
                _dma->ready() ? 1u : 0u, _dma->ready() ? 1u : 0u, 0};
            return kIOReturnSuccess;
        case LBGetForceWakeInfo:
            return args->structureOutput &&
                _owner->forcewakeInfo(static_cast<LBForceWakeInfo *>(args->structureOutput))
                ? kIOReturnSuccess : kIOReturnNotReady;
        case LBGetArcInterface: {
            if (!args->structureOutput) return kIOReturnBadArgument;
            // Build in aligned local storage: an inline Mach message payload
            // is only 4-byte aligned, and the module requires 8.
            LBArcInterfaceReport report{};
            if (lb_arc_interface_describe(&report, sizeof(report)) != LBArcOK)
                return kIOReturnInternalError;
            *static_cast<LBArcInterfaceReport *>(args->structureOutput) = report;
            _owner->trace(LBArcInterfaceQueried, kIOReturnSuccess, _context, report.opCount);
            return kIOReturnSuccess;
        }
        case LBCheckArcRequest: {
            if (!args->structureInput || !args->structureOutput ||
                args->structureInputSize != sizeof(LBArcRequest)) return kIOReturnBadArgument;
            LBArcRequest request = *static_cast<const LBArcRequest *>(args->structureInput);
            // Ownership comes from this user client, never from the request:
            // Linux derives it from the open render node, so a client-supplied
            // owner id would be the one field that could forge another client.
            if (request.owner) return kIOReturnBadArgument;
            request.owner = _context;
            LBArcDecision decision{};
            // The verdict is the payload: a refused request is answered with
            // its reason, and no selector of this ABI submits GPU work.
            const uint32_t verdict = lb_arc_interface_evaluate(&request, _context, nullptr,
                                                               &decision, sizeof(decision));
            if (decision.structBytes != sizeof(decision)) return kIOReturnInternalError;
            *static_cast<LBArcDecision *>(args->structureOutput) = decision;
            _owner->trace(verdict ? LBArcRequestRejected : LBArcRequestAccepted,
                          kIOReturnSuccess, _context, decision.op,
                          (uint64_t(verdict) << 32) | decision.reason);
            return kIOReturnSuccess;
        }
        case LBGetArcPortPlan: {
            if (!args->structureOutput) return kIOReturnBadArgument;
            LBArcPortReport report{};
            if (lb_arc_interface_port_report(&report, sizeof(report)) != LBArcOK)
                return kIOReturnInternalError;
            *static_cast<LBArcPortReport *>(args->structureOutput) = report;
            // Reading the plan programs nothing: both state fields stay zero.
            _owner->trace(LBArcPortQueried, kIOReturnSuccess, _context,
                          report.patWriteCount, report.lrcRenderTotalBytes);
            return kIOReturnSuccess;
        }
        case LBSubmit:
            _owner->trace(LBSubmitUnsupported, kIOReturnUnsupported, _context);
            return kIOReturnUnsupported;
        default: return kIOReturnBadArgument;
    }
}

IOReturn LunarBridgeUserClient::clientMemoryForType(UInt32 type, IOOptionBits *options,
                                                   IOMemoryDescriptor **memory) {
    if (!_lock || !options || !memory || type != 0) return kIOReturnBadArgument;
    *memory = nullptr;
    IOLockLock(_lock);
    if (_closed || !_buffer) { IOLockUnlock(_lock); return kIOReturnNotReady; }
    _buffer->retain();
    *memory = _buffer;
    *options = kIOMapAnywhere | kIOMapDefaultCache;
    _owner->trace(LBBufferMappingRequested, kIOReturnSuccess, _context, _bytes);
    IOLockUnlock(_lock);
    return kIOReturnSuccess;
}

IOReturn LunarBridgeUserClient::clientClose() {
    if (_lock) {
        IOLockLock(_lock);
        if (!_closed && _owner) _owner->trace(LBClientClosed, kIOReturnSuccess, _context);
        _closed = true;
        IOLockUnlock(_lock);
    }
    terminate();
    return kIOReturnSuccess;
}

IOReturn LunarBridgeUserClient::clientDied() { return clientClose(); }

void LunarBridgeUserClient::releaseResources() {
    if (_buffer) {
        const bool wasPrepared = _dma->ready();
        const IOReturn dmaStatus = _dma->reset();
        if (dmaStatus != kIOReturnSuccess) {
            if (_owner) _owner->trace(LBDmaReleaseFailed, dmaStatus, _context, _bytes);
            if (_owner && _admitted && _owner->quarantineClient(_context, _slot, _dma, _buffer, _bytes)) {
                // The pre-reserved slot now owns all references and accounting.
                _owner = nullptr; _dma = nullptr; _buffer = nullptr;
                _bytes = 0; _admitted = false;
            }
            return;
        }
        if (wasPrepared && _owner) _owner->trace(LBDmaReleased, kIOReturnSuccess, _context, _bytes);
        _buffer->release(); _buffer = nullptr;
        if (_owner) {
            _owner->releaseBuffer(_bytes);
            _owner->trace(LBBufferReleased, kIOReturnSuccess, _context, _bytes);
        }
        _bytes = 0;
    }
    if (_admitted && _owner) { _owner->releaseClient(_context, _slot); _admitted = false; }
}

void LunarBridgeUserClient::stop(IOService *provider) {
    if (_lock) { IOLockLock(_lock); _closed = true; releaseResources(); IOLockUnlock(_lock); }
    IOUserClient::stop(provider);
}

void LunarBridgeUserClient::free() {
    releaseResources();
    // An impossible ownership-table mismatch must not destroy outstanding
    // mappings. Normal failure paths transfer them to a reachable service slot.
    if (_buffer || (_dma && !_dma->drained())) return;
    if (_dma) { delete _dma; _dma = nullptr; }
    if (_owner) { _owner->release(); _owner = nullptr; }
    if (_lock) { IOLockFree(_lock); _lock = nullptr; }
    IOUserClient::free();
}
