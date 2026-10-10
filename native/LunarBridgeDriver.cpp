// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#include "LunarBridgeDriver.h"
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
    const uint64_t sequence = lb_trace_push(&_trace, ticks, event, status, context, arg0, arg1);
    const bool print = (_verbose || status != kIOReturnSuccess || event == LBStart ||
                        event == LBReady || event == LBStop) && _printed < 256;
    if (print) ++_printed;
    IOLockUnlock(_lock);
    if (print)
        IOLog("[LunarBridge] seq=%llu event=%u status=0x%08x context=%u a0=%llu a1=%llu\n",
              static_cast<unsigned long long>(sequence), event, status, context,
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
    setProperty("LBGPUExecutionSupported", false);
    setProperty("LBMetalSupported", false);
    setProperty("LBStage", "native-diagnostic-source; hardware validation pending");
}

bool LunarBridgePCI::start(IOService *provider) {
    if (!IOService::start(provider)) return false;
    auto pci = OSDynamicCast(IOPCIDevice, provider);
    if (!pci) { trace(LBStart, kIOReturnBadArgument); return false; }
    auto verbose = OSDynamicCast(OSBoolean, getProperty("LBDebugLogging"));
    _verbose = verbose && verbose->isTrue();
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
    const bool enabled = optIn && optIn->isTrue();
    _snapshot.flags = (pmPresent ? 1u : 0u) | (enabled ? 2u : 0u);
    LBMMIOInput input{_snapshot.vendor, _snapshot.device, _snapshot.subvendor, _snapshot.subdevice,
                      _snapshot.classCode, _snapshot.pciCommand, _snapshot.pmcsr,
                      pci->getPowerState(), enabled ? 1u : 0u, pmPresent ? 1u : 0u};
    uint32_t gfxOffset = 0, mediaOffset = 0;
    _snapshot.policy = lb_mmio_preflight(&input, _snapshot.barBytes, &gfxOffset, &mediaOffset);
    if (_snapshot.policy == LBReadAllowed) {
        if (!pci->open(this)) {
            _snapshot.policy = LBProviderBusy;
            trace(LBReadBlocked, kIOReturnExclusiveAccess, 0, _snapshot.policy);
        } else {
            // Recheck power/decode after opening. Never force D0 or enable PCI writes.
            input.command = pci->configRead16(kIOPCIConfigCommand);
            input.pmcsr = pci->configRead16(static_cast<uint8_t>(pmOffset + 4));
            input.providerPowerState = pci->getPowerState();
            input.vendor = pci->configRead16(kIOPCIConfigVendorID);
            _snapshot.pciCommand = input.command;
            _snapshot.pmcsr = input.pmcsr;
            _snapshot.policy = lb_mmio_preflight(&input, _snapshot.barBytes, &gfxOffset, &mediaOffset);
            if (_snapshot.policy != LBReadAllowed) {
                trace(LBReadBlocked, kIOReturnNotReady, 0, _snapshot.policy);
            } else {
                auto map = pci->mapDeviceMemoryWithRegister(kIOPCIConfigBaseAddress0,
                                                            kIOMapInhibitCache | kIOMapReadOnly);
                if (!map) trace(LBMapFailed, kIOReturnNoMemory);
                else {
                    input.command = pci->configRead16(kIOPCIConfigCommand);
                    input.pmcsr = pci->configRead16(static_cast<uint8_t>(pmOffset + 4));
                    input.providerPowerState = pci->getPowerState();
                    input.vendor = pci->configRead16(kIOPCIConfigVendorID);
                    _snapshot.pciCommand = input.command;
                    _snapshot.pmcsr = input.pmcsr;
                    _snapshot.policy = lb_mmio_preflight(&input, map->getLength(), &gfxOffset, &mediaOffset);
                    const uintptr_t base = map->getVirtualAddress();
                    if (_snapshot.policy != LBReadAllowed || base == 0) {
                        trace(LBReadBlocked, kIOReturnBadArgument, 0, _snapshot.policy);
                    } else {
                        _snapshot.gmdRawGraphics = *reinterpret_cast<volatile uint32_t *>(base + gfxOffset);
                        _snapshot.gmdRawMedia = *reinterpret_cast<volatile uint32_t *>(base + mediaOffset);
                        _snapshot.flags |= 16;
                        trace(LBGmdRead, kIOReturnSuccess, 0, _snapshot.gmdRawGraphics, _snapshot.gmdRawMedia);
                        const bool gfx = lnl_decode_gmd(_snapshot.gmdRawGraphics, &_snapshot.graphics);
                        const bool media = lnl_decode_gmd(_snapshot.gmdRawMedia, &_snapshot.media);
                        if (gfx && media) _snapshot.capabilities |= LBGmdSample;
                        else trace(LBGmdInvalid, kIOReturnNotReady, 0, gfx, media);
                    }
                    map->release();
                }
            }
            pci->close(this);
        }
    } else trace(LBReadBlocked, enabled ? kIOReturnNotReady : kIOReturnSuccess, 0, _snapshot.policy);
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
    IOLockUnlock(_lock);
    trace(LBStop);
    IOService::stop(provider);
}

void LunarBridgePCI::free() {
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

bool LunarBridgePCI::admitClient(uint32_t *id) {
    IOLockLock(_lock);
    const bool available = !_stopping && _clients < LB_MAX_CLIENTS && _nextClient != 0;
    if (available) { ++_clients; *id = _nextClient++; }
    IOLockUnlock(_lock);
    return available;
}

void LunarBridgePCI::releaseClient() {
    IOLockLock(_lock);
    if (_clients) --_clients;
    IOLockUnlock(_lock);
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
    if (!_lock || !_owner->admitClient(&_context)) {
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
        {invoke, 0, 0, 0, 0}
    };
    if (!args || selector >= LBSelectorCount) {
        if (_owner) _owner->trace(LBMethodRejected, kIOReturnBadArgument, _context, selector);
        return kIOReturnBadArgument;
    }
    // No indirect input/output descriptors: all supported wire payloads fit in 800 bytes.
    if (args->structureInputDescriptor || args->structureOutputDescriptor || args->asyncWakePort) {
        if (_owner) _owner->trace(LBMethodRejected, kIOReturnBadArgument, _context, selector);
        return kIOReturnBadArgument;
    }
    const IOReturn result = IOUserClient::externalMethod(selector, args, &methods[selector], this,
                            reinterpret_cast<void *>(static_cast<uintptr_t>(selector)));
    if (result != kIOReturnSuccess && result != kIOReturnUnsupported && _owner)
        _owner->trace(LBMethodRejected, result, _context, selector);
    return result;
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
        _buffer->release(); _buffer = nullptr;
        if (_owner) {
            _owner->releaseBuffer(_bytes);
            _owner->trace(LBBufferReleased, kIOReturnSuccess, _context, _bytes);
        }
        _bytes = 0;
    }
    if (_admitted && _owner) { _owner->releaseClient(); _admitted = false; }
}

void LunarBridgeUserClient::stop(IOService *provider) {
    if (_lock) { IOLockLock(_lock); _closed = true; releaseResources(); IOLockUnlock(_lock); }
    IOUserClient::stop(provider);
}

void LunarBridgeUserClient::free() {
    releaseResources();
    if (_owner) { _owner->release(); _owner = nullptr; }
    if (_lock) { IOLockFree(_lock); _lock = nullptr; }
    IOUserClient::free();
}
