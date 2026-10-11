// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#pragma once
#include "../gpu-prototype/lb_gpu_memory.h"
#ifdef LB_DMA_HOST_TEST
#include "../tests/dma_adapter_fake.h"
#else
#include <IOKit/IODMACommand.h>
#include <IOKit/IOMapper.h>
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/pci/IOPCIDevice.h>
#endif

// Native I/O virtual-address preparation, not GPU page-table binding. The
// caller serializes this context on a client thread, never a workloop gate.
// No address, segment list or PTE is exported through the user-client ABI.
class LBDMAContext {
    static constexpr uint32_t MaxPages = LB_VM_MAX_BUFFER / LB_GPU_PAGE;
    IOMapper *_mapper = nullptr;
    IODMACommand *_command = nullptr;
    IOBufferMemoryDescriptor *_memory = nullptr;
    bool _wired = false, _attached = false, _prepared = false, _ready = false;
    uint32_t _segmentCount = 0, _pteCount = 0;
    uint64_t _bytes = 0;
    LBVmArena _arena{};
    LBVmMapping _mapping{};
    LBDmaSegment _segments[MaxPages]{};
    uint64_t _ptes[MaxPages]{};
public:
    LBDMAContext() = default;
    ~LBDMAContext() = default; // Owner must reset successfully before deletion.
    LBDMAContext(const LBDMAContext &) = delete;
    LBDMAContext &operator=(const LBDMAContext &) = delete;
    IOReturn prepare(IOPCIDevice *device, IOBufferMemoryDescriptor *memory,
                     uint32_t owner, uint64_t bytes);
    IOReturn reset();
    bool ready() const { return _ready; }
    bool drained() const { return !_mapper && !_command && !_memory; }
    uint32_t segments() const { return _ready ? _segmentCount : 0; }
    uint32_t ptes() const { return _ready ? _pteCount : 0; }
    uint64_t bytes() const { return _ready ? _bytes : 0; }
};
