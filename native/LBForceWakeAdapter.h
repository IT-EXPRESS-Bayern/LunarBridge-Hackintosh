// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#pragma once
#if defined(LB_FORCEWAKE_HOST_TEST)
#include "../tests/forcewake_adapter_fake.h"
#else
#include <IOKit/IOService.h>
#include <IOKit/pci/IOPCIDevice.h>
#endif
#include "../gpu-prototype/lb_forcewake.h"
#include "../gpu-prototype/lb_diagnostics.h"

// A sleepable service lock serializes all calls. This adapter owns the BAR
// mapping and exclusive provider open until its own request is released.
// It never changes PCI command, power state, firmware, GGTT or engine registers.
class LBForceWakeAdapter {
    IOPCIDevice *_pci = nullptr;
    IOService *_owner = nullptr;
    IOMemoryMap *_map = nullptr;
    uintptr_t _base = 0;
    uint8_t _pmOffset = 0;
    uint32_t _gmdReads = 0;
    LBForceWakeState _wake{};
    LBForceWakeInfo _info{};
    bool _opened = false;
    bool guard(uint32_t offset);
    bool read32(uint32_t offset, uint32_t *value);
    bool write32(uint32_t offset, uint32_t value);
    void closeMapping();
    LBRegisterIO io();
    static bool readThunk(void *, uint32_t, uint32_t *);
    static bool writeThunk(void *, uint32_t, uint32_t);
    static bool sleepThunk(void *, uint32_t);
    static uint64_t clockThunk(void *);
    void update();
public:
    void initialize();
    // The MMIO sample already requires four LNL dummy writes before each read.
    void sample(IOPCIDevice *, IOService *, bool forcewake, LBDeviceSnapshot *);
    uint32_t reset();
    bool drained() const { return _pci == nullptr && _map == nullptr && !_opened; }
    const LBForceWakeInfo &info() const { return _info; }
};
