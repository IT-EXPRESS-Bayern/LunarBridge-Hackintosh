// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#include "LBDMAContext.h"

IOReturn LBDMAContext::reset() {
    _ready = false;
    // On a teardown error retain the descriptor/mapper. Releasing pinned
    // memory after failed completion would invalidate an outstanding IOVA.
    // The caller retains its budget reservation and records the failure.
    if (_prepared) {
        const IOReturn status = _command->complete();
        if (status != kIOReturnSuccess) return status;
        _prepared = false;
    }
    if (_attached) {
        const IOReturn status = _command->clearMemoryDescriptor(false);
        if (status != kIOReturnSuccess) return status;
        _attached = false;
    }
    if (_wired) {
        const IOReturn status = _memory->complete(kIODirectionInOut);
        if (status != kIOReturnSuccess) return status;
        _wired = false;
    }
    if (_command) { _command->release(); _command = nullptr; }
    if (_memory) { _memory->release(); _memory = nullptr; }
    if (_mapper) { _mapper->release(); _mapper = nullptr; }
    _segmentCount = _pteCount = 0;
    _bytes = 0;
    _arena = {};
    _mapping = {};
    for (uint32_t i = 0; i < MaxPages; ++i) { _segments[i] = {}; _ptes[i] = 0; }
    return kIOReturnSuccess;
}

IOReturn LBDMAContext::prepare(IOPCIDevice *device, IOBufferMemoryDescriptor *memory,
                             uint32_t owner, uint64_t bytes) {
    if (_command || _memory || _mapper) return kIOReturnBusy;
    if (!device || !memory || !owner || !bytes || bytes > LB_VM_MAX_BUFFER ||
        bytes % LB_GPU_PAGE || memory->getLength() != bytes) return kIOReturnBadArgument;
    // Require the mapper associated with this exact PCI provider. No physical
    // segment/system-mapper fallback and no inferred IOMMU availability.
    _mapper = IOMapper::copyMapperForDevice(device);
    if (!_mapper) return kIOReturnUnsupported;
    _memory = memory;
    _memory->retain();
    _bytes = bytes;
    IOReturn status = _memory->prepare(kIODirectionInOut);
    if (status == kIOReturnSuccess) {
        _wired = true;
        _command = IODMACommand::withSpecification(kIODMACommandOutputHost64, 46,
            LB_GPU_PAGE, IODMACommand::kMapped, bytes, LB_GPU_PAGE, _mapper);
        if (!_command) status = kIOReturnNoMemory;
    }
    if (status == kIOReturnSuccess) {
        status = _command->setMemoryDescriptor(_memory, false);
        if (status == kIOReturnSuccess) _attached = true;
    }
    if (status == kIOReturnSuccess) {
        status = _command->prepare(0, bytes, true, true);
        if (status == kIOReturnSuccess) _prepared = true;
    }
    UInt64 offset = 0;
    uint32_t used = 0;
    while (status == kIOReturnSuccess && offset < bytes && used < MaxPages) {
        IODMACommand::Segment64 segment{};
        UInt32 count = 1;
        const UInt64 previous = offset;
        status = _command->gen64IOVMSegments(&offset, &segment, &count);
        if (status == kIOReturnSuccess &&
            (count != 1 || offset <= previous || offset > bytes ||
             segment.fLength != offset - previous || segment.fLength != LB_GPU_PAGE))
            status = kIOReturnBadArgument;
        if (status == kIOReturnSuccess) _segments[used++] = {segment.fIOVMAddr, segment.fLength};
    }
    if (status == kIOReturnSuccess && offset != bytes) status = kIOReturnNoSpace;
    if (status == kIOReturnSuccess) {
        // CPU-only staging. PAT zero is a format input, not proof of PAT/cache
        // programming. Never publish these PTEs or a root to the device.
        if (lb_vm_init(&_arena, owner, 65536, 16*1024*1024) != LBVmOK ||
            lb_vm_reserve(&_arena, owner, bytes, 0, 1, &_mapping) != LBVmOK ||
            lb_vm_stage_ptes(&_arena, owner, _mapping.token, _segments, used,
                             _ptes, MaxPages) != LBVmOK)
            status = kIOReturnBadArgument;
    }
    if (status != kIOReturnSuccess) {
        const IOReturn cleanup = reset();
        return cleanup == kIOReturnSuccess ? status : cleanup;
    }
    _segmentCount = used;
    _pteCount = bytes / LB_GPU_PAGE;
    _ready = true;
    return kIOReturnSuccess;
}
