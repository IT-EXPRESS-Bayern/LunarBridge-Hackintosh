// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Minimal API doubles only for execution of the actual adapter under Windows.
#pragma once
#include <stdint.h>
using UInt64 = uint64_t;
using UInt32 = uint32_t;
using IOReturn = uint32_t;
constexpr IOReturn kIOReturnSuccess = 0, kIOReturnError = 0xe00002bc;
constexpr IOReturn kIOReturnBusy = 0xe00002d5, kIOReturnBadArgument = 0xe00002c2;
constexpr IOReturn kIOReturnUnsupported = 0xe00002c7, kIOReturnNoMemory = 0xe00002bd;
constexpr IOReturn kIOReturnNoSpace = 0xe00002db;
constexpr uint32_t kIODirectionInOut = 3, kIODMACommandOutputHost64 = 64;
class IOPCIDevice {};
class IOBufferMemoryDescriptor {
    uint64_t _length;
public:
    explicit IOBufferMemoryDescriptor(uint64_t length) : _length(length) {}
    uint64_t getLength() const { return _length; }
    void retain();
    void release();
    IOReturn prepare(uint32_t);
    IOReturn complete(uint32_t);
};
class IOMapper {
public:
    static IOMapper *copyMapperForDevice(IOPCIDevice *device);
    void release();
};
class IODMACommand {
public:
    enum MappingOptions { kMapped = 0 };
    struct Segment64 { UInt64 fIOVMAddr, fLength; };
    static IODMACommand *withSpecification(uint32_t, uint8_t, UInt64, MappingOptions,
                                          UInt64, UInt32, IOMapper *);
    IOReturn setMemoryDescriptor(IOBufferMemoryDescriptor *, bool);
    IOReturn prepare(UInt64, UInt64, bool, bool);
    IOReturn complete();
    IOReturn clearMemoryDescriptor(bool);
    IOReturn gen64IOVMSegments(UInt64 *, Segment64 *, UInt32 *);
    void release();
};
