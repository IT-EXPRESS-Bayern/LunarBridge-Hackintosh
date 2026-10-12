// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Independently written interface adapter. The compared interface shapes are
// pinned in ../docs/ARC_INTERFACE_SOURCES.json; no foreign driver code is used.
#include "lb_arc_interface.h"
#include "lb_diagnostics.h"
#include "lb_lrc.h"

static_assert(LBGetArcInterface == LB_ARC_SELECTOR_DESCRIBE, "Arc describe selector drifted");
static_assert(LBCheckArcRequest == LB_ARC_SELECTOR_CHECK, "Arc check selector drifted");
static_assert(LBSelectorCount == LB_ARC_SELECTOR_CHECK + 1, "Selector count drifted");

namespace {
// DRM ioctl directions of the pinned uAPI: DRM_IOW writes only, DRM_IOWR both.
constexpr uint32_t kDirWrite = 1;
constexpr uint32_t kDirReadWrite = 3;

// Request fields an operation may use. Every field outside its own set must be
// zero, so one shared structure cannot smuggle state past per-operation checks.
enum FieldBit : uint32_t {
    FFlags = 1u << 0, FPat = 1u << 1, FSegments = 1u << 2, FSyncs = 1u << 3,
    FEngineClass = 1u << 4, FEngineMask = 1u << 5, FBindMode = 1u << 6,
    FQueryKind = 1u << 7, FMemoryType = 1u << 8, FVmToken = 1u << 9,
    FResourceToken = 1u << 10, FQueueToken = 1u << 11, FGpuAddress = 1u << 12,
    FBytes = 1u << 13, FLrc = 1u << 14, FBatch = 1u << 15, FTimeout = 1u << 16
};

struct OpRow {
    uint32_t op, linuxIoctlNr, linuxConstructs, linuxDirection;
    uint32_t differences, qualifications, allowedFields;
};

// One row per mapped Xe ioctl. linuxIoctlNr, linuxConstructs and
// linuxDirection are verified against the pinned uAPI header by
// tools/compare_interfaces.py; differences record what the macOS shape had to
// change, qualifications what hardware work is still missing afterwards.
constexpr OpRow kOps[LB_ARC_MAX_OPS] = {
    {LBArcOpDeviceQuery, LB_ARC_DRM_COMMAND_BASE + 0x00,
     LBArcLinuxExtensionChain | LBArcLinuxUserPointer, kDirReadWrite,
     LB_ARC_BASE_DIFFERENCES | LBArcDiffNoUserPointer,
     LBArcQualFuseTopology, FQueryKind},
    {LBArcOpResourceCreate, LB_ARC_DRM_COMMAND_BASE + 0x01,
     LBArcLinuxExtensionChain | LBArcLinuxHandleId, kDirReadWrite,
     LB_ARC_BASE_DIFFERENCES | LBArcDiffExplicitPat,
     LBArcQualDeviceMapper, FFlags | FPat | FBytes},
    {LBArcOpResourceMap, LB_ARC_DRM_COMMAND_BASE + 0x02,
     LBArcLinuxExtensionChain | LBArcLinuxHandleId | LBArcLinuxMmapOffset, kDirReadWrite,
     LB_ARC_BASE_DIFFERENCES | LBArcDiffNoMmapOffset,
     LBArcQualDeviceMapper, FResourceToken | FMemoryType},
    {LBArcOpVmCreate, LB_ARC_DRM_COMMAND_BASE + 0x03,
     LBArcLinuxExtensionChain | LBArcLinuxHandleId, kDirReadWrite,
     LB_ARC_BASE_DIFFERENCES, 0, FGpuAddress | FBytes},
    {LBArcOpVmDestroy, LB_ARC_DRM_COMMAND_BASE + 0x04,
     LBArcLinuxHandleId, kDirWrite,
     LB_ARC_BASE_DIFFERENCES, 0, FVmToken},
    {LBArcOpVmBind, LB_ARC_DRM_COMMAND_BASE + 0x05,
     LBArcLinuxExtensionChain | LBArcLinuxUserPointer | LBArcLinuxVectorOfOps |
     LBArcLinuxSyncArray | LBArcLinuxHandleId, kDirWrite,
     LB_ARC_BASE_DIFFERENCES | LBArcDiffNoUserPointer | LBArcDiffFlattenedVector |
     LBArcDiffNoSyncObject | LBArcDiffExplicitPat,
     LBArcQualDeviceMapper | LBArcQualGgttOwnership | LBArcQualPatProgrammed,
     FFlags | FPat | FSegments | FSyncs | FBindMode | FVmToken | FResourceToken |
     FGpuAddress | FBytes},
    {LBArcOpQueueCreate, LB_ARC_DRM_COMMAND_BASE + 0x06,
     LBArcLinuxExtensionChain | LBArcLinuxUserPointer | LBArcLinuxHandleId, kDirReadWrite,
     LB_ARC_BASE_DIFFERENCES | LBArcDiffNoUserPointer,
     LBArcQualGuCRunning | LBArcQualLrcPrepared,
     FEngineClass | FEngineMask | FVmToken | FLrc},
    {LBArcOpQueueDestroy, LB_ARC_DRM_COMMAND_BASE + 0x07,
     LBArcLinuxHandleId, kDirWrite,
     LB_ARC_BASE_DIFFERENCES, LBArcQualGuCRunning, FQueueToken},
    {LBArcOpQueueProperty, LB_ARC_DRM_COMMAND_BASE + 0x08,
     LBArcLinuxExtensionChain | LBArcLinuxHandleId, kDirReadWrite,
     LB_ARC_BASE_DIFFERENCES, LBArcQualGuCRunning, FQueueToken | FQueryKind},
    {LBArcOpExec, LB_ARC_DRM_COMMAND_BASE + 0x09,
     LBArcLinuxExtensionChain | LBArcLinuxUserPointer | LBArcLinuxSyncArray |
     LBArcLinuxHandleId, kDirWrite,
     LB_ARC_BASE_DIFFERENCES | LBArcDiffNoUserPointer | LBArcDiffNoSyncObject |
     LBArcDiffAsyncReference,
     LBArcQualGuCRunning | LBArcQualLrcPrepared | LBArcQualFenceMemory |
     LBArcQualGgttOwnership, FSyncs | FQueueToken | FBatch},
    {LBArcOpFenceWait, LB_ARC_DRM_COMMAND_BASE + 0x0a,
     LBArcLinuxExtensionChain | LBArcLinuxHandleId | LBArcLinuxUserFenceAddr |
     LBArcLinuxInfiniteWait, kDirReadWrite,
     LB_ARC_BASE_DIFFERENCES | LBArcDiffAsyncReference | LBArcDiffBoundedTimeout,
     LBArcQualFenceMemory,
     FFlags | FSyncs | FQueueToken | FTimeout}
};

bool valid_span(const void *pointer, uint64_t bytes) {
    const uintptr_t start = reinterpret_cast<uintptr_t>(pointer);
    return pointer && bytes && bytes <= UINTPTR_MAX - start;
}
bool overlap(const void *a, uint64_t aBytes, const void *b, uint64_t bBytes) {
    const uintptr_t aStart = reinterpret_cast<uintptr_t>(a);
    const uintptr_t bStart = reinterpret_cast<uintptr_t>(b);
    return aStart < bStart + bBytes && bStart < aStart + aBytes;
}
const OpRow *row_for(uint32_t op) {
    for (uint32_t i = 0; i < LB_ARC_MAX_OPS; ++i)
        if (kOps[i].op == op) return &kOps[i];
    return nullptr;
}
void descriptor_from(const OpRow &row, LBArcOpDescriptor *out) {
    LBArcOpDescriptor descriptor{};
    descriptor.op = row.op;
    descriptor.selector = LB_ARC_SELECTOR_CHECK;
    descriptor.structInputBytes = uint32_t(sizeof(LBArcRequest));
    descriptor.structOutputBytes = uint32_t(sizeof(LBArcDecision));
    descriptor.differences = row.differences;
    descriptor.requiredQualifications = row.qualifications;
    descriptor.linuxIoctlNr = row.linuxIoctlNr;
    descriptor.linuxConstructs = row.linuxConstructs;
    descriptor.linuxDirection = row.linuxDirection;
    *out = descriptor;
}
bool page_aligned(uint64_t value) { return (value & (LB_GPU_PAGE - 1)) == 0; }

// Returns the reason for the first field used outside the operation's set.
uint32_t unused_fields_clear(const LBArcRequest *request, uint32_t allowed) {
    const struct { uint32_t bit; uint64_t value; } checks[] = {
        {FFlags, request->flags}, {FPat, request->patIndex},
        {FSegments, request->segmentCount}, {FSyncs, request->syncCount},
        {FEngineClass, request->engineClass}, {FEngineMask, request->engineSubmitMask},
        {FBindMode, request->bindMode}, {FQueryKind, request->queryKind},
        {FMemoryType, request->memoryType}, {FVmToken, request->vmToken},
        {FResourceToken, request->resourceToken}, {FQueueToken, request->queueToken},
        {FGpuAddress, request->gpuAddress}, {FBytes, request->bytes},
        {FLrc, request->lrcGpuAddress}, {FBatch, request->batchAddress},
        {FTimeout, request->timeoutNs}
    };
    for (const auto &check : checks)
        if (!(allowed & check.bit) && check.value) return LBArcReasonReservedField;
    if (request->reservedWords[0] || request->reservedWords[1] || request->reservedWords[2])
        return LBArcReasonReservedField;
    // Array contents outside the declared counts must be clear as well.
    for (uint32_t i = request->segmentCount; i < LB_ARC_MAX_SEGMENTS; ++i)
        if (request->segments[i].address || request->segments[i].bytes)
            return LBArcReasonReservedField;
    for (uint32_t i = request->syncCount; i < LB_ARC_MAX_SYNCS; ++i)
        if (request->syncs[i].type || request->syncs[i].flags ||
            request->syncs[i].address || request->syncs[i].value)
            return LBArcReasonReservedField;
    return LBArcReasonNone;
}

struct Verdict {
    uint32_t result = LBArcOK;
    uint32_t reason = LBArcReasonNone;
};
Verdict refuse(uint32_t result, uint32_t reason) { return Verdict{result, reason}; }

// A sync point must be a user fence: syncobj and timeline-syncobj handles have
// no macOS namespace, and the fence word is polled as a 64-bit value.
Verdict check_sync(const LBArcSyncPoint &sync, bool allowSignal) {
    if (sync.type == LBArcSyncObject || sync.type == LBArcSyncTimelineObject)
        return refuse(LBArcLinuxism, LBArcReasonSyncObject);
    if (sync.type != LBArcSyncUserFence)
        return refuse(LBArcInput, LBArcReasonUnmappedOperation);
    if (!sync.address || (sync.address & 7) || sync.address >= LB_GPU_VA_LIMIT)
        return refuse(LBArcRange, LBArcReasonFenceAddress);
    const uint32_t allowedFlags = allowSignal ? uint32_t(LBArcFlagSignalFence) : 0u;
    if (sync.flags & ~allowedFlags) return refuse(LBArcPolicy, LBArcReasonFlags);
    return Verdict{};
}

// Interface pre-check of the global window; the GGTT planner re-validates it.
Verdict check_window(const LBGgttWindow *window, uint64_t gpuAddress, uint64_t bytes) {
    if (window->abi != LB_GGTT_PLAN_ABI || window->structBytes != sizeof(LBGgttWindow) ||
        window->addressSpaceKind != LB_GGTT_SPACE_GLOBAL ||
        window->reserved[0] || window->reserved[1] || window->reserved[2] ||
        window->gsmBarOffset != LB_GGTT_GSM_BAR_OFFSET ||
        window->gsmBytes != LB_GGTT_GSM_BYTES ||
        window->gpuAddressLimit != LB_GGTT_GUC_TOP ||
        window->reservedLowBytes < LB_GGTT_RESERVED_LOW ||
        window->reservedLowBytes >= window->gpuAddressLimit ||
        !page_aligned(window->reservedLowBytes) ||
        window->gsmBarOffset >= window->mappedBarBytes ||
        window->gsmBytes > window->mappedBarBytes - window->gsmBarOffset)
        return refuse(LBArcPolicy, LBArcReasonWindow);
    if (window->patIndex != LB_GGTT_XE2_WB_PAT || window->patValue != LB_GGTT_XE2_WB_PAT_VALUE)
        return refuse(LBArcPolicy, LBArcReasonPatIndex);
    if (gpuAddress < window->reservedLowBytes || gpuAddress >= window->gpuAddressLimit ||
        bytes > window->gpuAddressLimit - gpuAddress)
        return refuse(LBArcRange, LBArcReasonWindow);
    const uint64_t firstIndex = gpuAddress / LB_GPU_PAGE;
    const uint64_t available = window->gsmBytes / sizeof(uint64_t);
    const uint64_t pages = bytes / LB_GPU_PAGE;
    if (firstIndex >= available || pages > available - firstIndex)
        return refuse(LBArcCapacity, LBArcReasonWindow);
    return Verdict{};
}
}  // namespace

extern "C" uint32_t lb_arc_interface_describe(LBArcInterfaceReport *out, uint32_t outputBytes) {
    if (!out || outputBytes != sizeof(LBArcInterfaceReport)) return LBArcInput;
    if (!valid_span(out, sizeof(*out))) return LBArcRange;
    if (reinterpret_cast<uintptr_t>(out) & 7) return LBArcAlignment;
    LBArcInterfaceReport report{};
    report.abi = LB_ARC_IF_ABI;
    report.structBytes = uint32_t(sizeof(LBArcInterfaceReport));
    report.opCount = LB_ARC_MAX_OPS;
    report.linuxUapiVersion = LB_ARC_LINUX_UAPI_VERSION;
    report.darwinKernelMajor = LB_ARC_DARWIN_KERNEL_MAJOR;
    report.scalarLimit = LB_ARC_SCALAR_LIMIT;
    report.maxSegments = LB_ARC_MAX_SEGMENTS;
    report.maxSyncs = LB_ARC_MAX_SYNCS;
    report.unmappedLinuxIoctls = LB_ARC_UNMAPPED_LINUX_IOCTLS;
    // No GPU execution and no Metal provider exist in this version; the report
    // states that next to the interface table instead of leaving it implied.
    report.gpuExecutionImplemented = 0;
    report.metalProviderImplemented = 0;
    for (uint32_t i = 0; i < LB_ARC_MAX_OPS; ++i) {
        descriptor_from(kOps[i], &report.ops[i]);
        report.differences |= kOps[i].differences;
    }
    *out = report;
    return LBArcOK;
}

extern "C" uint32_t lb_arc_interface_descriptor(uint32_t op, LBArcOpDescriptor *out) {
    if (!out || !valid_span(out, sizeof(*out))) return LBArcInput;
    if (reinterpret_cast<uintptr_t>(out) & 3) return LBArcAlignment;
    const OpRow *row = row_for(op);
    if (!row) return LBArcUnknownOp;
    descriptor_from(*row, out);
    return LBArcOK;
}

extern "C" uint32_t lb_arc_interface_check_call(uint32_t selector, uint32_t scalarInputCount,
                                               uint32_t scalarOutputCount, uint32_t structInputBytes,
                                               uint32_t structOutputBytes, uint32_t asyncReferenceCount,
                                               uint32_t *reasonOut) {
    uint32_t reason = LBArcReasonNone;
    uint32_t result = LBArcOK;
    if (selector != LB_ARC_SELECTOR_DESCRIBE && selector != LB_ARC_SELECTOR_CHECK) {
        result = LBArcUnknownOp;
        reason = LBArcReasonUnmappedOperation;
    } else if (asyncReferenceCount) {
        // A callback port would be the macOS answer to a blocking ioctl; this
        // version implements no asynchronous completion and says so.
        result = LBArcUnsupported;
        reason = LBArcReasonAsyncUnsupported;
    } else if (scalarInputCount || scalarOutputCount) {
        result = LBArcCallShape;
        reason = LBArcReasonScalarCount;
    } else if (structInputBytes == 0xFFFFFFFFu || structOutputBytes == 0xFFFFFFFFu) {
        // kIOUCVariableStructureSize: a size the dispatch table cannot check.
        result = LBArcCallShape;
        reason = LBArcReasonVariableStruct;
    } else {
        const uint32_t expectedIn = selector == LB_ARC_SELECTOR_CHECK
            ? uint32_t(sizeof(LBArcRequest)) : 0u;
        const uint32_t expectedOut = selector == LB_ARC_SELECTOR_CHECK
            ? uint32_t(sizeof(LBArcDecision)) : uint32_t(sizeof(LBArcInterfaceReport));
        if (structInputBytes != expectedIn || structOutputBytes != expectedOut) {
            result = LBArcCallShape;
            reason = LBArcReasonVariableStruct;
        }
    }
    if (reasonOut) *reasonOut = reason;
    return result;
}

extern "C" uint32_t lb_arc_interface_evaluate(const LBArcRequest *request, uint32_t owner,
                                              const LBGgttWindow *window, LBArcDecision *out,
                                              uint32_t outputBytes) {
    // The output buffer is checked first: a refusal must still be reportable.
    if (!out || outputBytes != sizeof(LBArcDecision) || !valid_span(out, sizeof(*out)))
        return LBArcInput;
    if (reinterpret_cast<uintptr_t>(out) & 7) return LBArcAlignment;
    if (!request || !valid_span(request, sizeof(*request))) return LBArcInput;
    if (reinterpret_cast<uintptr_t>(request) & 7) return LBArcAlignment;
    if (overlap(out, sizeof(*out), request, sizeof(*request))) return LBArcAliasing;
    if (window) {
        if (!valid_span(window, sizeof(*window))) return LBArcInput;
        if (reinterpret_cast<uintptr_t>(window) & 7) return LBArcAlignment;
        if (overlap(out, sizeof(*out), window, sizeof(*window))) return LBArcAliasing;
    }

    LBArcDecision decision{};
    decision.abi = LB_ARC_IF_ABI;
    decision.structBytes = uint32_t(sizeof(LBArcDecision));
    decision.op = request->op;

    Verdict verdict{};
    const OpRow *row = nullptr;
    if (request->abi != LB_ARC_IF_ABI || request->structBytes != sizeof(LBArcRequest)) {
        verdict = refuse(LBArcAbi, LBArcReasonVariableStruct);
    } else if (!owner || request->owner != owner) {
        verdict = refuse(LBArcOwner, LBArcReasonHandleNamespace);
    } else if ((row = row_for(request->op)) == nullptr) {
        verdict = refuse(LBArcUnknownOp, LBArcReasonUnmappedOperation);
    } else if (request->segmentCount > LB_ARC_MAX_SEGMENTS ||
               request->syncCount > LB_ARC_MAX_SYNCS) {
        verdict = refuse(LBArcCapacity, LBArcReasonCapacity);
    } else if (request->flags & ~LB_ARC_FLAG_MASK) {
        verdict = refuse(LBArcInput, LBArcReasonFlags);
    } else {
        const uint32_t stray = unused_fields_clear(request, row->allowedFields);
        if (stray) verdict = refuse(LBArcInput, stray);
    }

    if (!verdict.result && row) {
        decision.differences = row->differences;
        decision.requiredQualifications = row->qualifications;
        switch (request->op) {
        case LBArcOpDeviceQuery:
            if (request->queryKind > LBArcQueryAddressSpace) {
                verdict = refuse(LBArcUnsupported, LBArcReasonUnmappedOperation);
                break;
            }
            // Record counts this driver can actually describe: one device
            // snapshot, the two GuC engine classes of the single-LRC profile,
            // or the two separate address spaces (PPGTT and GGTT).
            decision.translatedBytes = request->queryKind == LBArcQueryDeviceSnapshot ? 1 : 2;
            break;
        case LBArcOpResourceCreate:
            if (request->flags & ~uint32_t(LBArcFlagWritable)) {
                verdict = refuse(LBArcPolicy, LBArcReasonFlags);
            } else if (!request->bytes || !page_aligned(request->bytes)) {
                verdict = refuse(LBArcAlignment, LBArcReasonPageAlignment);
            } else if (request->bytes > LB_VM_MAX_BUFFER) {
                verdict = refuse(LBArcCapacity, LBArcReasonCapacity);
            } else if (request->patIndex != LB_GGTT_XE2_WB_PAT) {
                // Linux validates a pat_index table entry; here PAT 2 is the
                // only write-back policy this driver would program itself.
                verdict = refuse(LBArcPolicy, LBArcReasonPatIndex);
            } else {
                decision.translatedBytes = request->bytes;
                decision.pageCount = uint32_t(request->bytes / LB_GPU_PAGE);
                decision.expectedPatValue = LB_GGTT_XE2_WB_PAT_VALUE;
            }
            break;
        case LBArcOpResourceMap:
            if (!request->resourceToken) {
                verdict = refuse(LBArcInput, LBArcReasonMissingToken);
            } else if (request->memoryType != LB_ARC_MEMORY_TYPE_SHARED) {
                // clientMemoryForType exports exactly one type; there is no
                // fake offset to hand out and no second memory window.
                verdict = refuse(LBArcUnsupported, LBArcReasonMmapOffset);
            }
            break;
        case LBArcOpVmCreate:
            if (!request->bytes || !page_aligned(request->bytes) ||
                !page_aligned(request->gpuAddress)) {
                verdict = refuse(LBArcAlignment, LBArcReasonPageAlignment);
            } else if (request->gpuAddress >= LB_GPU_VA_LIMIT ||
                       request->bytes > LB_GPU_VA_LIMIT - request->gpuAddress) {
                verdict = refuse(LBArcRange, LBArcReasonPageAlignment);
            } else {
                decision.translatedGpuAddress = request->gpuAddress;
                decision.translatedBytes = request->bytes;
                decision.pageCount = uint32_t(request->bytes / LB_GPU_PAGE);
            }
            break;
        case LBArcOpVmDestroy:
            if (!request->vmToken) verdict = refuse(LBArcInput, LBArcReasonMissingToken);
            break;
        case LBArcOpVmBind: {
            if (request->bindMode == LBArcBindUserPtr) {
                verdict = refuse(LBArcLinuxism, LBArcReasonUserPointer);
                break;
            }
            if (request->bindMode == LBArcBindUnmapAll ||
                request->bindMode == LBArcBindPrefetch) {
                // UNMAP_ALL needs a kernel-side object list this interface does
                // not own; PREFETCH needs memory regions Lunar Lake has not.
                verdict = refuse(LBArcUnsupported, LBArcReasonUnmappedOperation);
                break;
            }
            if (request->bindMode > LBArcBindPrefetch) {
                verdict = refuse(LBArcInput, LBArcReasonFlags);
                break;
            }
            const bool mapping = request->bindMode == LBArcBindMap;
            if (!request->vmToken || !request->resourceToken) {
                verdict = refuse(LBArcInput, LBArcReasonMissingToken);
                break;
            }
            if (!request->bytes || !page_aligned(request->bytes) ||
                !page_aligned(request->gpuAddress)) {
                verdict = refuse(LBArcAlignment, LBArcReasonPageAlignment);
                break;
            }
            if (request->bytes > LB_VM_MAX_BUFFER) {
                verdict = refuse(LBArcCapacity, LBArcReasonCapacity);
                break;
            }
            if (!request->gpuAddress || request->gpuAddress >= LB_GPU_VA_LIMIT ||
                request->bytes > LB_GPU_VA_LIMIT - request->gpuAddress) {
                verdict = refuse(LBArcRange, LBArcReasonPageAlignment);
                break;
            }
            if (request->patIndex != LB_GGTT_XE2_WB_PAT) {
                verdict = refuse(LBArcPolicy, LBArcReasonPatIndex);
                break;
            }
            // GGTT entries carry no per-page read-only bit, so a bind that is
            // not writable cannot be honoured; an unmap carries no policy.
            if (mapping ? !(request->flags & LBArcFlagWritable)
                        : (request->flags & LBArcFlagWritable)) {
                verdict = refuse(LBArcPolicy, LBArcReasonFlags);
                break;
            }
            if (mapping) {
                if (!request->segmentCount) {
                    verdict = refuse(LBArcInput, LBArcReasonSegmentSum);
                    break;
                }
                uint64_t total = 0;
                for (uint32_t i = 0; i < request->segmentCount; ++i) {
                    const LBDmaSegment &segment = request->segments[i];
                    if (!segment.bytes || !page_aligned(segment.bytes) ||
                        !page_aligned(segment.address)) {
                        verdict = refuse(LBArcAlignment, LBArcReasonPageAlignment);
                        break;
                    }
                    if (!segment.address || segment.address >= LB_GPU_DMA_LIMIT ||
                        segment.bytes > LB_GPU_DMA_LIMIT - segment.address) {
                        verdict = refuse(LBArcRange, LBArcReasonSegmentSum);
                        break;
                    }
                    total += segment.bytes;
                }
                if (verdict.result) break;
                if (total != request->bytes) {
                    verdict = refuse(LBArcInput, LBArcReasonSegmentSum);
                    break;
                }
                decision.acceptedSegments = request->segmentCount;
            } else if (request->segmentCount) {
                // An unmap must not carry addresses: nothing is re-encoded.
                verdict = refuse(LBArcInput, LBArcReasonReservedField);
                break;
            }
            for (uint32_t i = 0; i < request->syncCount; ++i) {
                verdict = check_sync(request->syncs[i], true);
                if (verdict.result) break;
            }
            if (verdict.result) break;
            if (window) {
                verdict = check_window(window, request->gpuAddress, request->bytes);
                if (verdict.result) break;
                decision.ggttWriteCount = uint32_t(request->bytes / LB_GPU_PAGE);
                decision.expectedPatValue = window->patValue;
            }
            decision.translatedGpuAddress = request->gpuAddress;
            decision.translatedBytes = request->bytes;
            decision.pageCount = uint32_t(request->bytes / LB_GPU_PAGE);
            decision.fenceSlots = request->syncCount;
            break;
        }
        case LBArcOpQueueCreate:
            // GuC engine classes of the pinned single-LRC profile only.
            if (request->engineClass != 0 && request->engineClass != 4) {
                verdict = refuse(LBArcUnsupported, LBArcReasonEngineClass);
            } else if (!request->engineSubmitMask || (request->engineSubmitMask & ~0xFFu)) {
                // Own conservative bound; the real mask needs ADS/HW discovery.
                verdict = refuse(LBArcRange, LBArcReasonEngineClass);
            } else if (!request->vmToken) {
                verdict = refuse(LBArcInput, LBArcReasonMissingToken);
            } else if (!request->lrcGpuAddress || !page_aligned(request->lrcGpuAddress) ||
                       request->lrcGpuAddress >= LB_GGTT_GUC_TOP) {
                // The context lives in a GGTT page below the GuC top boundary.
                verdict = refuse(LBArcRange, LBArcReasonPageAlignment);
            } else if (request->lrcGpuAddress < LB_GGTT_RESERVED_LOW) {
                // Same reserved low window the GGTT planner and the LRC port keep.
                verdict = refuse(LBArcPolicy, LBArcReasonWindow);
            }
            break;
        case LBArcOpQueueDestroy:
            if (!request->queueToken) verdict = refuse(LBArcInput, LBArcReasonMissingToken);
            break;
        case LBArcOpQueueProperty:
            if (!request->queueToken) {
                verdict = refuse(LBArcInput, LBArcReasonMissingToken);
            } else if (request->queryKind != LBArcQueuePropertyPhase) {
                verdict = refuse(LBArcUnsupported, LBArcReasonUnmappedOperation);
            } else {
                decision.translatedBytes = 1;
            }
            break;
        case LBArcOpExec: {
            if (!request->queueToken) {
                verdict = refuse(LBArcInput, LBArcReasonMissingToken);
                break;
            }
            if (!request->batchAddress || request->batchAddress >= LB_GPU_VA_LIMIT) {
                verdict = refuse(LBArcRange, LBArcReasonBatchAlignment);
                break;
            }
            if (request->batchAddress & (LB_ARC_BATCH_ALIGNMENT - 1)) {
                verdict = refuse(LBArcAlignment, LBArcReasonBatchAlignment);
                break;
            }
            if (!request->syncCount) {
                verdict = refuse(LBArcInput, LBArcReasonNoCompletion);
                break;
            }
            bool signalled = false;
            for (uint32_t i = 0; i < request->syncCount; ++i) {
                verdict = check_sync(request->syncs[i], true);
                if (verdict.result) break;
                signalled = signalled || (request->syncs[i].flags & LBArcFlagSignalFence);
            }
            if (verdict.result) break;
            if (!signalled) {
                // Without a signalling fence no completion could be observed.
                verdict = refuse(LBArcPolicy, LBArcReasonNoCompletion);
                break;
            }
            decision.translatedGpuAddress = request->batchAddress;
            decision.fenceSlots = request->syncCount;
            break;
        }
        case LBArcOpFenceWait:
            if (request->flags & ~uint32_t(LBArcFlagAbsoluteTime)) {
                verdict = refuse(LBArcPolicy, LBArcReasonFlags);
            } else if (!request->queueToken) {
                verdict = refuse(LBArcInput, LBArcReasonMissingToken);
            } else if (request->syncCount != 1) {
                verdict = refuse(LBArcInput, LBArcReasonNoCompletion);
            } else if ((verdict = check_sync(request->syncs[0], false)).result) {
                break;
            } else if (!request->timeoutNs || request->timeoutNs > LB_ARC_MAX_TIMEOUT_NS) {
                // Linux accepts timeout -1 and waits forever; a kernel wait in
                // this driver stays bounded so a client cannot pin a thread.
                verdict = refuse(LBArcPolicy, LBArcReasonTimeout);
            } else {
                decision.translatedGpuAddress = request->syncs[0].address;
                decision.fenceSlots = 1;
            }
            break;
        default:
            verdict = refuse(LBArcUnknownOp, LBArcReasonUnmappedOperation);
            break;
        }
    }

    if (verdict.result) {
        // A refusal reports its rule, not a partially translated request.
        const uint32_t differences = decision.differences;
        const uint32_t qualifications = decision.requiredQualifications;
        decision = LBArcDecision{};
        decision.abi = LB_ARC_IF_ABI;
        decision.structBytes = uint32_t(sizeof(LBArcDecision));
        decision.op = request->op;
        decision.differences = differences;
        decision.requiredQualifications = qualifications;
    }
    decision.result = verdict.result;
    decision.reason = verdict.reason;
    *out = decision;
    return verdict.result;
}

extern "C" uint32_t lb_arc_interface_queue_registration(const LBArcRequest *request, uint32_t owner,
                                                        LBGuCSchedulerRegistration *out,
                                                        uint32_t outputBytes) {
    if (!out || outputBytes != sizeof(LBGuCSchedulerRegistration) ||
        !valid_span(out, sizeof(*out))) return LBArcInput;
    if (reinterpret_cast<uintptr_t>(out) & 7) return LBArcAlignment;
    if (!request || !valid_span(request, sizeof(*request))) return LBArcInput;
    if (overlap(out, sizeof(*out), request, sizeof(*request))) return LBArcAliasing;
    if (request->op != LBArcOpQueueCreate) return LBArcUnknownOp;
    // Re-run the full interface check; a caller must not reach the GuC shape
    // through a request this module would have refused.
    LBArcDecision decision{};
    const uint32_t status = lb_arc_interface_evaluate(request, owner, nullptr, &decision,
                                                      uint32_t(sizeof(decision)));
    if (status) return status;
    // The descriptor is composed from the checked GGTT address by the LRC
    // port: a client-supplied descriptor could set the privilege and
    // addressing-mode bits itself.
    uint64_t descriptor = 0;
    if (lb_lrc_descriptor(request->lrcGpuAddress, 1, &descriptor) != LBLrcOK)
        return LBArcRange;
    LBGuCSchedulerRegistration registration{};
    registration.lrcDescriptor = descriptor;
    registration.engineClass = request->engineClass;
    registration.engineSubmitMask = request->engineSubmitMask;
    *out = registration;
    return LBArcOK;
}
