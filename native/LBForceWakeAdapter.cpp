// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// LNL read flush WA15015404425 and Forcewake protocol from Linux Xe v6.17.
#include "LBForceWakeAdapter.h"
#if !defined(LB_FORCEWAKE_HOST_TEST)
#include <IOKit/IOLib.h>
#include <kern/clock.h>
static uint32_t LBAdapterRead32(uintptr_t base, uint32_t offset) {
    return *reinterpret_cast<volatile uint32_t *>(base + offset);
}
static bool LBAdapterWrite32(uintptr_t base, uint32_t offset, uint32_t value) {
    *reinterpret_cast<volatile uint32_t *>(base + offset) = value;
    return true;
}
#endif

void LBForceWakeAdapter::initialize() {
    _info.abi = 1;
    _info.structBytes = sizeof(_info);
    _info.primaryStatus = _info.cleanupStatus = LBWakeIdle;
    _info.preflight = LBOptInMissing;
    lb_forcewake_init(&_wake);
}

bool LBForceWakeAdapter::guard(uint32_t offset) {
    if (!_pci || !_owner || !_map || !_opened || !_pci->isOpen(_owner) || !_base ||
        offset > _map->getLength() || _map->getLength() - offset < 4 ||
        (offset & 3) || _pmOffset < 0x40 || _pmOffset > 0xf8) return false;
    const uint32_t classCode = _pci->configRead32(kIOPCIConfigRevisionID) >> 8;
    const uint16_t command = _pci->configRead16(kIOPCIConfigCommand);
    const uint16_t pmcsr = _pci->configRead16(static_cast<uint8_t>(_pmOffset + 4));
    return _pci->configRead16(kIOPCIConfigVendorID) == 0x8086 &&
           _pci->configRead16(kIOPCIConfigDeviceID) == 0x64a0 &&
           _pci->configRead16(kIOPCIConfigSubSystemVendorID) == 0x144d &&
           _pci->configRead16(kIOPCIConfigSubSystemID) == 0xc1dd &&
           (classCode == 0x030000 || classCode == 0x038000) && command != 0xffff &&
           (command & 2) && pmcsr != 0xffff && !(pmcsr & 3) && _pci->getPowerState() == 2;
}

bool LBForceWakeAdapter::read32(uint32_t offset, uint32_t *value) {
    // No user-supplied offsets: exactly GMD main/media and FW CTL/ACK.
    if (!value || (offset != 0xd8c && offset != 0x380d8c &&
                   offset != LB_FW_CONTROL && offset != LB_FW_ACK) ||
        !guard(offset) || !guard(LB_LNL_READ_FLUSH) ||
        _info.flushWrites > UINT32_MAX - 4) return false;
    // WA15015404425: four root-tile dummy writes BEFORE EVERY MMIO read.
    // Inhibit-cache volatile accesses on x86 preserve device access ordering.
    for (uint32_t i = 0; i < 4; ++i) {
        if (!LBAdapterWrite32(_base, LB_LNL_READ_FLUSH, 0)) return false;
        ++_info.flushWrites;
    }
    *value = LBAdapterRead32(_base, offset);
    if (offset == 0xd8c || offset == 0x380d8c) ++_gmdReads;
    return true;
}

bool LBForceWakeAdapter::write32(uint32_t offset, uint32_t value) {
    if (offset != LB_FW_CONTROL || (value != LB_FW_REQUEST && value != LB_FW_RELEASE) ||
        !guard(offset)) return false;
    return LBAdapterWrite32(_base, offset, value);
}

bool LBForceWakeAdapter::readThunk(void *p, uint32_t o, uint32_t *v) {
    return static_cast<LBForceWakeAdapter *>(p)->read32(o, v);
}
bool LBForceWakeAdapter::writeThunk(void *p, uint32_t o, uint32_t v) {
    return static_cast<LBForceWakeAdapter *>(p)->write32(o, v);
}
bool LBForceWakeAdapter::sleepThunk(void *p, uint32_t us) {
    auto adapter = static_cast<LBForceWakeAdapter *>(p);
    if (us != 1000 || !adapter->guard(LB_FW_ACK)) return false;
    IOSleep(1);
    return true;
}
uint64_t LBForceWakeAdapter::clockThunk(void *) {
    uint64_t ticks = 0, ns = 0;
    clock_get_uptime(&ticks);
    absolutetime_to_nanoseconds(ticks, &ns);
    return ns / 1000;
}
LBRegisterIO LBForceWakeAdapter::io() {
    return {this, readThunk, writeThunk, sleepThunk, clockThunk};
}

void LBForceWakeAdapter::update() {
    _info.reads = _wake.reads + _gmdReads;
    _info.requestWrites = _wake.writes;
    _info.lastAck = _wake.lastAck;
    _info.polls = _wake.polls;
    _info.leaseReferences = _wake.references;
    _info.resourcesRetained = drained() ? 0u : 1u;
}

void LBForceWakeAdapter::closeMapping() {
    // Never destroy a mapping while our request may still be active.
    if (!lb_forcewake_drained(&_wake)) return;
    if (_map) { _map->release(); _map = nullptr; }
    _base = 0;
    if (_opened && _pci && _owner) _pci->close(_owner);
    _opened = false;
    if (_pci) { _pci->release(); _pci = nullptr; }
    _owner = nullptr;
}

void LBForceWakeAdapter::sample(IOPCIDevice *pci, IOService *owner, bool forcewake,
                               LBDeviceSnapshot *snapshot) {
    _info.enabled = forcewake ? 1u : 0u;
    if (!pci || !owner || !snapshot || !drained() || _info.attempted) return;
    _info.preflight = snapshot->policy;
    if (snapshot->policy != LBReadAllowed) return;
    if (!pci->findPCICapability(kIOPCIPowerManagementCapability, &_pmOffset) ||
        _pmOffset < 0x40 || _pmOffset > 0xf8) {
        snapshot->policy = _info.preflight = LBPowerUnknown;
        return;
    }
    if (!pci->open(owner)) {
        snapshot->policy = _info.preflight = LBProviderBusy;
        return;
    }
    _pci = pci; _pci->retain(); _owner = owner; _opened = true;
    _map = pci->mapDeviceMemoryWithRegister(kIOPCIConfigBaseAddress0, kIOMapInhibitCache);
    _base = _map ? _map->getVirtualAddress() : 0;
    if (!_map || !_base || _map->getLength() < 0x380d90) {
        snapshot->policy = _info.preflight = LBBarTooSmall;
        _info.primaryStatus = LBWakeIOFailed;
        closeMapping(); update(); return;
    }
    LBMMIOInput current{pci->configRead16(kIOPCIConfigVendorID), pci->configRead16(kIOPCIConfigDeviceID),
        pci->configRead16(kIOPCIConfigSubSystemVendorID), pci->configRead16(kIOPCIConfigSubSystemID),
        pci->configRead32(kIOPCIConfigRevisionID) >> 8, pci->configRead16(kIOPCIConfigCommand),
        pci->configRead16(static_cast<uint8_t>(_pmOffset + 4)), pci->getPowerState(), 1, 1};
    uint32_t gfxOffset = 0, mediaOffset = 0;
    snapshot->policy = _info.preflight = lb_mmio_preflight(&current, _map->getLength(), &gfxOffset, &mediaOffset);
    snapshot->pciCommand = current.command;
    snapshot->pmcsr = current.pmcsr;
    if (snapshot->policy != LBReadAllowed) {
        _info.primaryStatus = LBWakeIOFailed;
        closeMapping(); update(); return;
    }
    uint32_t gfx = 0, media = 0;
    if (!read32(gfxOffset, &gfx) || !read32(mediaOffset, &media)) {
        _info.primaryStatus = LBWakeIOFailed;
        closeMapping(); update(); return;
    }
    snapshot->gmdRawGraphics = gfx;
    snapshot->gmdRawMedia = media;
    snapshot->flags |= 16; // Reads occurred; validity is the separate capability.
    if (gfx == UINT32_MAX || media == UINT32_MAX) {
        _info.primaryStatus = LBWakeInvalidMMIO;
        closeMapping(); update(); return;
    }
    const bool graphicsValid = lnl_decode_gmd(gfx, &snapshot->graphics);
    const bool mediaValid = media == 0 || lnl_decode_gmd(media, &snapshot->media);
    if (graphicsValid && mediaValid) snapshot->capabilities |= LBGmdSample;
    _info.graphicsIP = snapshot->graphics.version_x100;
    _info.mediaIP = snapshot->media.version_x100;
    if (forcewake) {
        _info.attempted = 1;
        if (!graphicsValid || !mediaValid || _info.graphicsIP != 2004 ||
            (media != 0 && _info.mediaIP != 2000)) {
            _info.primaryStatus = LBWakeUnsupportedIP;
        } else {
            auto callbacks = io();
            _info.primaryStatus = lb_forcewake_get(&_wake, &callbacks);
            // An error from get may follow a request write. Cleanup is mandatory.
            _info.cleanupStatus = _wake.owned ? lb_forcewake_put(&_wake, &callbacks) : LBWakeIdle;
            _info.completed = _info.primaryStatus == LBWakeOK && _info.cleanupStatus == LBWakeOK;
        }
    }
    closeMapping(); update();
}

uint32_t LBForceWakeAdapter::reset() {
    if (drained()) return LBWakeIdle;
    auto callbacks = io();
    const uint32_t result = _wake.owned ? lb_forcewake_put(&_wake, &callbacks) : LBWakeIdle;
    _info.cleanupStatus = result;
    _info.completed = _info.primaryStatus == LBWakeOK && result == LBWakeOK;
    closeMapping(); update();
    return result;
}
