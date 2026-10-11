// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
#include "../native/LBDMAContext.h"
#include "../native/LBDMAQuarantine.h"

namespace {
struct Statistics {
    uint32_t fault, faultConsumed, mapperCopies, mapperReleases, memoryRetains, memoryReleases;
    uint32_t wireCalls, unwireCalls, commandCreated, commandReleased, attachCalls, detachCalls;
    uint32_t prepareCalls, completeCalls, segmentCalls, badSequence, wired, attached, prepared;
    uint64_t bytes;
} stats{};
IOMapper mapper;
IODMACommand command;
bool fail(uint32_t code) {
    if (stats.fault != code || stats.faultConsumed) return false;
    ++stats.faultConsumed;
    return true;
}
}
void IOBufferMemoryDescriptor::retain() { ++stats.memoryRetains; }
void IOBufferMemoryDescriptor::release() { if (stats.wired) ++stats.badSequence; ++stats.memoryReleases; }
IOReturn IOBufferMemoryDescriptor::prepare(uint32_t direction) {
    ++stats.wireCalls;
    if (direction != kIODirectionInOut || stats.wired) ++stats.badSequence;
    if (fail(2)) return kIOReturnError;
    stats.wired = 1;
    return kIOReturnSuccess;
}
IOReturn IOBufferMemoryDescriptor::complete(uint32_t direction) {
    ++stats.unwireCalls;
    if (direction != kIODirectionInOut || !stats.wired || stats.prepared || stats.attached) ++stats.badSequence;
    if (stats.fault == 23 || fail(18)) return kIOReturnError;
    stats.wired = 0;
    return kIOReturnSuccess;
}
IOMapper *IOMapper::copyMapperForDevice(IOPCIDevice *device) {
    ++stats.mapperCopies;
    if (!device || fail(1)) return nullptr;
    return &mapper;
}
void IOMapper::release() {
    if (stats.prepared || stats.wired || stats.attached) ++stats.badSequence;
    ++stats.mapperReleases;
}
IODMACommand *IODMACommand::withSpecification(uint32_t format, uint8_t bits, UInt64 segment,
                                            MappingOptions options, UInt64 bytes,
                                            UInt32 alignment, IOMapper *selected) {
    if (format != kIODMACommandOutputHost64 || bits != 46 || segment != 4096 ||
        options != kMapped || !bytes || bytes > 1048576 || alignment != 4096 || selected != &mapper)
        { ++stats.badSequence; return nullptr; }
    if (fail(3)) return nullptr;
    stats.bytes = bytes;
    ++stats.commandCreated;
    return &command;
}
IOReturn IODMACommand::setMemoryDescriptor(IOBufferMemoryDescriptor *, bool autoPrepare) {
    ++stats.attachCalls;
    if (autoPrepare || !stats.wired || stats.attached) ++stats.badSequence;
    if (fail(4)) return kIOReturnError;
    stats.attached = 1;
    return kIOReturnSuccess;
}
IOReturn IODMACommand::prepare(UInt64 offset, UInt64 bytes, bool flush, bool synchronize) {
    ++stats.prepareCalls;
    if (offset || bytes != stats.bytes || !flush || !synchronize || !stats.attached || !stats.wired)
        ++stats.badSequence;
    if (fail(5)) return kIOReturnError;
    stats.prepared = 1;
    return kIOReturnSuccess;
}
IOReturn IODMACommand::complete() {
    ++stats.completeCalls;
    if (!stats.prepared || !stats.attached || !stats.wired) ++stats.badSequence;
    if (stats.fault == 21 || fail(16)) return kIOReturnError;
    stats.prepared = 0;
    return kIOReturnSuccess;
}
IOReturn IODMACommand::clearMemoryDescriptor(bool autoComplete) {
    ++stats.detachCalls;
    if (autoComplete || stats.prepared || !stats.attached) ++stats.badSequence;
    if (stats.fault == 22 || fail(17)) return kIOReturnError;
    stats.attached = 0;
    return kIOReturnSuccess;
}
IOReturn IODMACommand::gen64IOVMSegments(UInt64 *offset, Segment64 *segment, UInt32 *count) {
    ++stats.segmentCalls;
    if (!stats.prepared || !stats.wired || *count != 1) ++stats.badSequence;
    if (fail(6) || (*offset && fail(15))) return kIOReturnError;
    segment->fIOVMAddr = 0x100000 + *offset;
    segment->fLength = 4096;
    if (stats.fault == 7) segment->fLength = 2048;
    if (stats.fault == 8) segment->fIOVMAddr = uint64_t(1) << 46;
    if (stats.fault == 9) segment->fIOVMAddr = 0x100000;
    if (stats.fault == 10) ++segment->fIOVMAddr;
    if (stats.fault == 11) *count = 0;
    if (stats.fault == 14) segment->fLength = 0;
    if (stats.fault != 12) *offset += 4096;
    if (stats.fault == 13) *offset = stats.bytes + 4096;
    return kIOReturnSuccess;
}
void IODMACommand::release() {
    if (stats.prepared || stats.attached || stats.wired) ++stats.badSequence;
    ++stats.commandReleased;
}

struct LBAdapterTestReport {
    uint32_t synthetic, prepareStatus, ready, segments, ptes, reentryStatus;
    uint32_t firstReset, secondReset, finalReady, badSequence;
    uint32_t mapperCopies, mapperReleases, memoryRetains, memoryReleases;
    uint32_t wireCalls, unwireCalls, commandCreated, commandReleased;
    uint32_t attachCalls, detachCalls, prepareCalls, completeCalls, segmentCalls;
    uint32_t heldAfterFirstReset, finalHeld, reserved;
};
extern "C" LNL_API int lb_dma_test_run(uint32_t fault, uint64_t bytes, uint32_t owner,
                                     uint64_t descriptorBytes, LBAdapterTestReport *out) {
    if (!out || fault > 23) return 0;
    stats = {};
    stats.fault = fault;
    IOPCIDevice device;
    IOBufferMemoryDescriptor memory(descriptorBytes);
    LBDMAContext context;
    const IOReturn status = context.prepare(fault == 19 ? nullptr : &device,
                                            fault == 20 ? nullptr : &memory, owner, bytes);
    const bool ready = context.ready();
    const uint32_t segments = context.segments(), ptes = context.ptes();
    const IOReturn reentry = ready ? context.prepare(&device, &memory, owner, bytes) : kIOReturnSuccess;
    const IOReturn first = context.reset();
    const uint32_t held = stats.wired || stats.prepared || stats.attached;
    const IOReturn second = context.reset();
    *out = {1, status, ready ? 1u : 0u, segments, ptes, reentry, first, second,
        context.ready() ? 1u : 0u, stats.badSequence, stats.mapperCopies, stats.mapperReleases,
        stats.memoryRetains, stats.memoryReleases, stats.wireCalls, stats.unwireCalls,
        stats.commandCreated, stats.commandReleased, stats.attachCalls, stats.detachCalls,
        stats.prepareCalls, stats.completeCalls, stats.segmentCalls, held,
        stats.wired || stats.prepared || stats.attached, 0};
    return 1;
}

// Executes the same service ownership table with explicit fake IOKit objects.
// The test owns stack contexts throughout; no macOS object or device is used.
extern "C" LNL_API int lb_dma_quarantine_test_run(uint32_t scenario, uint32_t *out) {
    if (!out || scenario > 9) return 0;
    stats = {};
    LBDMAQuarantine table;
    LBDMAQuarantine::Held held{};
    LBDMAContext context;
    IOPCIDevice device;
    IOBufferMemoryDescriptor memory(4096);
    const int slot = table.reserve(1);
    bool ok = slot == 0;
    switch (scenario) {
    case 0:
        ok = ok && table.reserve(0) == -1 && table.reserve(1) == -1 && table.reserve(2) == 1;
        break;
    case 1:
        for (uint32_t id = 2; id <= 8; ++id) ok = ok && table.reserve(id) == static_cast<int>(id - 1);
        ok = ok && table.reserve(9) == -1 && table.reserve(0xFFFFFFFF) == -1;
        break;
    case 2:
        ok = ok && !table.releaseClient(8, 1) && !table.releaseClient(slot, 0) &&
            !table.releaseClient(slot, 2) && table.releaseClient(slot, 1) &&
            !table.releaseClient(slot, 1) && table.reserve(2) == slot;
        break;
    case 3:
        ok = ok && !table.hold(slot, 1, &context, &memory, 4096) &&
            !table.hold(slot, 1, nullptr, &memory, 4096) && table.releaseClient(slot, 1);
        break;
    case 4:
    case 5:
    case 6:
    case 7:
    case 8:
        ok = ok && context.prepare(&device, &memory, 1, 4096) == kIOReturnSuccess;
        stats.fault = 21;
        ok = ok && context.reset() != kIOReturnSuccess && !context.ready() && !context.drained();
        if (scenario == 7) {
            ok = ok && !table.hold(slot, 2, &context, &memory, 4096) &&
                !table.hold(slot, 1, &context, nullptr, 4096) &&
                !table.hold(slot, 1, &context, &memory, 8192) &&
                !table.hold(slot, 1, &context, &memory, 0) &&
                !table.hold(8, 1, &context, &memory, 4096) && table.retained() == 0;
        }
        ok = ok && table.hold(slot, 1, &context, &memory, 4096) && table.retained() == 1 &&
            !table.releaseClient(slot, 1) && !table.hold(slot, 1, &context, &memory, 4096);
        if (scenario == 8) {
            const int second = table.reserve(2);
            ok = ok && second == 1 && !table.hold(second, 2, &context, &memory, 4096) &&
                table.releaseClient(second, 2);
        }
        ok = ok && !table.beginRetry(slot, nullptr) && !table.beginRetry(8, &held) &&
            table.beginRetry(slot, &held) && held.context == &context && held.buffer == &memory &&
            held.bytes == 4096 && held.owner == 1 && table.retained() == 1;
        if (scenario == 5) {
            ok = ok && !table.beginRetry(slot, &held) && !table.releaseClient(slot, 1) &&
                !table.finishRetry(slot, 2, false) && !table.finishRetry(slot, 1, true);
        }
        if (scenario == 6) {
            ok = ok && context.reset() != kIOReturnSuccess && table.finishRetry(slot, 1, false) &&
                table.retained() == 1 && !table.releaseClient(slot, 1) && table.beginRetry(slot, &held);
        }
        stats.fault = 0;
        ok = ok && context.reset() == kIOReturnSuccess && context.drained() &&
            table.finishRetry(slot, 1, true) && table.retained() == 0 &&
            !table.finishRetry(slot, 1, true) && !table.beginRetry(slot, &held) && table.reserve(3) == slot;
        break;
    case 9:
        ok = ok && !table.beginRetry(slot, &held) && !table.finishRetry(slot, 1, false);
        for (uint32_t id = 2; id <= 8; ++id) ok = ok && table.reserve(id) == static_cast<int>(id - 1);
        for (uint32_t id = 1; id <= 8; ++id) ok = ok && table.releaseClient(id - 1, id);
        ok = ok && table.reserve(0xFFFFFFFF) == 0;
        break;
    }
    // Explicit owner cleanup; the context destructor performs no hidden retry.
    stats.fault = 0;
    if (!context.drained()) context.reset();
    out[0] = 1; // simulated IOKit
    out[1] = ok ? 1u : 0u;
    out[2] = table.retained();
    out[3] = stats.badSequence;
    out[4] = stats.mapperCopies == stats.mapperReleases ? 1u : 0u;
    out[5] = stats.memoryRetains == stats.memoryReleases ? 1u : 0u;
    return 1;
}
