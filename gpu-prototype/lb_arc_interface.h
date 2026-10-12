// SPDX-License-Identifier: GPL-3.0-only
// Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
// Interface facts referenced here are pinned in ../docs/ARC_INTERFACE_SOURCES.json:
// Linux v6.17 `include/uapi/drm/xe_drm.h` (Intel MIT, see ../LICENSES/MIT-Intel.txt)
// and Apple's published xnu / IOGraphics / IOPCIFamily headers (APSL).
// No Apple or Intel driver code is reproduced; only interface shapes are compared.
#pragma once
#include "lb_ggtt_plan.h"
#include "lb_guc_scheduler.h"

// Arc 130V operation set expressed in the macOS external-method shape.
//
// Linux Xe reaches the same hardware through 12 render-node ioctls whose
// structures carry extension chains, user pointers, DRM handles, syncobjs and
// fake mmap offsets. An XNU user client receives a selector plus already
// copied-in scalars and ONE fixed-size structure; it cannot follow a user
// pointer, has no syncobj namespace, no render node and no mmap offset. This
// module is that interface boundary: it declares the macOS-shaped contract per
// operation, records which Linux construct each operation had to drop, and
// validates/translates a request onto the existing CPU contracts
// (lb_gpu_memory, lb_ggtt_plan, lb_guc_scheduler).
//
// Nothing here touches MMIO, DMA, firmware or a GPU. An accepted request is a
// validated interface shape, never a submission and never GPU execution.
constexpr uint32_t LB_ARC_IF_ABI = 1;
constexpr uint32_t LB_ARC_MAX_OPS = 11;
constexpr uint32_t LB_ARC_MAX_SEGMENTS = 16;
constexpr uint32_t LB_ARC_MAX_SYNCS = 4;
// Pinned reference versions, reported so a client cannot guess the comparison
// base: Linux 6.17 uAPI and the published xnu 12377 (macOS 26) IOKit contract.
constexpr uint32_t LB_ARC_LINUX_UAPI_VERSION = (6u << 16) | 17u;
constexpr uint32_t LB_ARC_DARWIN_KERNEL_MAJOR = 26;
// DRM_COMMAND_BASE from the pinned include/uapi/drm/drm.h.
constexpr uint32_t LB_ARC_DRM_COMMAND_BASE = 0x40;
// kIOExternalMethodScalarInputCountMax / ...OutputCountMax from the pinned
// xnu iokit/IOKit/IOUserClient.h. Scalars above this never reach a method.
constexpr uint32_t LB_ARC_SCALAR_LIMIT = 16;
// A kernel wait must be bounded; Linux allows timeout -1 (wait forever).
constexpr uint64_t LB_ARC_MAX_TIMEOUT_NS = 2000000000ull;
// Own conservative ISA/batch rule: Xe2 kernels are 64-byte aligned.
constexpr uint64_t LB_ARC_BATCH_ALIGNMENT = 64;
// The user client exports exactly one memory type (clientMemoryForType == 0).
constexpr uint32_t LB_ARC_MEMORY_TYPE_SHARED = 0;
// User-client selectors carrying this interface. lb_arc_interface.cpp asserts
// that they stay identical to the LBSelector enum of the diagnostic ABI.
constexpr uint32_t LB_ARC_SELECTOR_DESCRIBE = 7;
constexpr uint32_t LB_ARC_SELECTOR_CHECK = 8;
// Pinned Xe ioctls without a mapped operation: DRM_XE_OBSERVATION only.
constexpr uint32_t LB_ARC_UNMAPPED_LINUX_IOCTLS = 1;

enum LBArcOp : uint32_t {
    LBArcOpDeviceQuery = 1,       // DRM_XE_DEVICE_QUERY            0x00
    LBArcOpResourceCreate = 2,    // DRM_XE_GEM_CREATE              0x01
    LBArcOpResourceMap = 3,       // DRM_XE_GEM_MMAP_OFFSET         0x02
    LBArcOpVmCreate = 4,          // DRM_XE_VM_CREATE               0x03
    LBArcOpVmDestroy = 5,         // DRM_XE_VM_DESTROY              0x04
    LBArcOpVmBind = 6,            // DRM_XE_VM_BIND                 0x05
    LBArcOpQueueCreate = 7,       // DRM_XE_EXEC_QUEUE_CREATE       0x06
    LBArcOpQueueDestroy = 8,      // DRM_XE_EXEC_QUEUE_DESTROY      0x07
    LBArcOpQueueProperty = 9,     // DRM_XE_EXEC_QUEUE_GET_PROPERTY 0x08
    LBArcOpExec = 10,             // DRM_XE_EXEC                    0x09
    LBArcOpFenceWait = 11         // DRM_XE_WAIT_USER_FENCE         0x0a
    // DRM_XE_OBSERVATION 0x0b is deliberately not mapped; see ARC_INTERFACE_DIFF.md.
};

enum LBArcResult : uint32_t {
    LBArcOK = 0, LBArcInput = 1, LBArcAbi = 2, LBArcOwner = 3,
    LBArcUnknownOp = 4, LBArcCallShape = 5, LBArcLinuxism = 6,
    LBArcUnsupported = 7, LBArcRange = 8, LBArcAlignment = 9,
    LBArcCapacity = 10, LBArcPolicy = 11, LBArcAliasing = 12
};

// Why a request was refused. Reported next to the result so a rejection can be
// traced to one concrete interface rule instead of a generic bad-argument.
enum LBArcReason : uint32_t {
    LBArcReasonNone = 0, LBArcReasonReservedField = 1, LBArcReasonUserPointer = 2,
    LBArcReasonSyncObject = 3, LBArcReasonMmapOffset = 4, LBArcReasonHandleNamespace = 5,
    LBArcReasonVariableStruct = 6, LBArcReasonScalarCount = 7, LBArcReasonAsyncUnsupported = 8,
    LBArcReasonPatIndex = 9, LBArcReasonEngineClass = 10, LBArcReasonSegmentSum = 11,
    LBArcReasonPageAlignment = 12, LBArcReasonFenceAddress = 13, LBArcReasonTimeout = 14,
    LBArcReasonUnmappedOperation = 15, LBArcReasonMissingToken = 16, LBArcReasonWindow = 17,
    LBArcReasonBatchAlignment = 18, LBArcReasonCapacity = 19, LBArcReasonFlags = 20,
    LBArcReasonNoCompletion = 21
};

// Linux constructs that the pinned uAPI structure of an operation contains.
// tools/compare_interfaces.py verifies every bit against the pinned header.
enum LBArcLinuxConstruct : uint32_t {
    LBArcLinuxExtensionChain = 1 << 0,  // __u64 extensions
    LBArcLinuxUserPointer = 1 << 1,     // __u64 holding a user pointer
    LBArcLinuxVectorOfOps = 1 << 2,     // union { single op; __u64 vector_of_binds }
    LBArcLinuxSyncArray = 1 << 3,       // struct drm_xe_sync array, syncobj capable
    LBArcLinuxHandleId = 1 << 4,        // per-file 32-bit id namespace
    LBArcLinuxMmapOffset = 1 << 5,      // fake offset for a later mmap
    LBArcLinuxUserFenceAddr = 1 << 6,   // user VA polled by the kernel
    LBArcLinuxInfiniteWait = 1 << 7     // __s64 timeout, -1 waits forever
};

// How the macOS interface answers those constructs.
enum LBArcDifference : uint32_t {
    LBArcDiffFixedStructSize = 1 << 0,   // one checked structure size, no variable blob
    LBArcDiffNoExtensionChain = 1 << 1,  // extensions are not followed, MBZ instead
    LBArcDiffNoUserPointer = 1 << 2,     // copied-in data only
    LBArcDiffFlattenedVector = 1 << 3,   // bounded inline array replaces the vector
    LBArcDiffNoSyncObject = 1 << 4,      // no drm_syncobj namespace
    LBArcDiffNoMmapOffset = 1 << 5,      // clientMemoryForType replaces mmap offsets
    LBArcDiffOwnerBoundToken = 1 << 6,   // owner-bound tokens replace file-local ids
    LBArcDiffExplicitPat = 1 << 7,       // PAT index is explicit and checked here
    LBArcDiffAsyncReference = 1 << 8,    // completion rides the IOKit async reference
    LBArcDiffBoundedTimeout = 1 << 9,    // kernel waits must be bounded
    LBArcDiffNoRenderNode = 1 << 10,     // IOService user client, no /dev/dri node
    LBArcDiffEntitlementGate = 1 << 11   // dispatch-level entitlement, not DRM auth
};
// Every operation carries these: the macOS call shape itself forces them.
constexpr uint32_t LB_ARC_BASE_DIFFERENCES = LBArcDiffFixedStructSize |
    LBArcDiffNoExtensionChain | LBArcDiffOwnerBoundToken |
    LBArcDiffNoRenderNode | LBArcDiffEntitlementGate;

// Hardware work that is still missing before an accepted shape could run.
enum LBArcQualification : uint32_t {
    LBArcQualDeviceMapper = 1 << 0,   // addresses from a qualified kernel DMA mapper
    LBArcQualGgttOwnership = 1 << 1,  // global GGTT ownership and TLB handling
    LBArcQualPatProgrammed = 1 << 2,  // PAT index 2 actually programmed on the device
    LBArcQualGuCRunning = 1 << 3,     // authenticated, running GuC with a CT queue
    LBArcQualLrcPrepared = 1 << 4,    // real LRC / ADS memory
    LBArcQualFenceMemory = 1 << 5,    // fence page mapped for GPU and CPU
    LBArcQualFuseTopology = 1 << 6,   // real EU / subslice / HWConfig values
    LBArcQualMetalProvider = 1 << 7,  // a registered Metal device provider
    LBArcQualDisplayEngine = 1 << 8   // display engine and WindowServer path
};

enum LBArcRequestFlag : uint32_t {
    LBArcFlagWritable = 1 << 0,     // mapping is writable; GGTT has no RO bit
    LBArcFlagSignalFence = 1 << 1,  // sync point is signalled, not waited on
    LBArcFlagAbsoluteTime = 1 << 2  // timeout is absolute, still bounded
};
constexpr uint32_t LB_ARC_FLAG_MASK = LBArcFlagWritable | LBArcFlagSignalFence |
                                      LBArcFlagAbsoluteTime;

// Mirrors DRM_XE_VM_BIND_OP_MAP / _UNMAP. USERPTR, UNMAP_ALL and PREFETCH have
// no counterpart here and are refused with their own reasons.
enum LBArcBindMode : uint32_t {
    LBArcBindMap = 0, LBArcBindUnmap = 1,
    LBArcBindUserPtr = 2, LBArcBindUnmapAll = 3, LBArcBindPrefetch = 4
};
// Mirrors DRM_XE_SYNC_TYPE_*; only the user fence survives the port.
enum LBArcSyncType : uint32_t {
    LBArcSyncObject = 0, LBArcSyncTimelineObject = 1, LBArcSyncUserFence = 2
};
// Only the bounded query kinds this driver can actually answer.
enum LBArcQueryKind : uint32_t {
    LBArcQueryDeviceSnapshot = 0, LBArcQueryEngineClasses = 1, LBArcQueryAddressSpace = 2
};
// Only the bounded queue property this driver can actually answer.
enum LBArcQueuePropertyKind : uint32_t { LBArcQueuePropertyPhase = 0 };

struct LBArcOpDescriptor {
    uint32_t op, selector, scalarInputCount, scalarOutputCount;
    uint32_t structInputBytes, structOutputBytes;
    uint32_t differences, requiredQualifications;
    uint32_t linuxIoctlNr, linuxConstructs, linuxDirection, reserved;
};

struct LBArcInterfaceReport {
    uint32_t abi, structBytes, opCount, differences;
    uint32_t linuxUapiVersion, darwinKernelMajor, scalarLimit, maxSegments;
    uint32_t maxSyncs, unmappedLinuxIoctls, gpuExecutionImplemented, metalProviderImplemented;
    LBArcOpDescriptor ops[LB_ARC_MAX_OPS];
};

struct LBArcSyncPoint {
    uint32_t type, flags;
    uint64_t address, value;
};

// One flat, copied-in request for every operation. Fields a given operation
// does not use must be zero: a shared structure without that rule would let a
// caller smuggle state past the per-operation checks.
struct LBArcRequest {
    uint32_t abi, structBytes, op, owner;
    uint32_t flags, patIndex, segmentCount, syncCount;
    uint32_t engineClass, engineSubmitMask, bindMode, queryKind;
    uint32_t memoryType, reservedWords[3];
    uint64_t vmToken, resourceToken, queueToken, gpuAddress;
    uint64_t bytes, lrcGpuAddress, batchAddress, timeoutNs;
    LBDmaSegment segments[LB_ARC_MAX_SEGMENTS];
    LBArcSyncPoint syncs[LB_ARC_MAX_SYNCS];
};

// Address-free verdict. hardwareBound / gpuExecuted / metalVerified stay 0 in
// this version; an accepted interface shape is not a hardware operation.
// For LBArcOpDeviceQuery and LBArcOpQueueProperty, translatedBytes carries the
// number of fixed-size records the answer would consist of, not a byte count.
struct LBArcDecision {
    uint32_t abi, structBytes, op, result;
    uint32_t reason, differences, requiredQualifications, acceptedSegments;
    uint64_t translatedBytes, translatedGpuAddress;
    uint32_t pageCount, ggttWriteCount, expectedPatValue, fenceSlots;
    uint32_t hardwareBound, gpuExecuted, metalVerified, reserved;
};

static_assert(sizeof(LBArcOpDescriptor) == 48, "Arc op descriptor wire ABI");
static_assert(sizeof(LBArcInterfaceReport) == 48 + 11 * 48, "Arc interface report wire ABI");
static_assert(sizeof(LBArcSyncPoint) == 24, "Arc sync point wire ABI");
static_assert(sizeof(LBArcRequest) == 480, "Arc request wire ABI");
static_assert(sizeof(LBArcDecision) == 80, "Arc decision wire ABI");

extern "C" {
// Publish the whole operation table. Output is written only after validation.
LNL_API uint32_t lb_arc_interface_describe(LBArcInterfaceReport *out, uint32_t outputBytes);
// One operation's descriptor; unknown operations are refused, never defaulted.
LNL_API uint32_t lb_arc_interface_descriptor(uint32_t op, LBArcOpDescriptor *out);
// Check the actual external-method call shape before any body is parsed. The
// arguments are the IOExternalMethodArguments counts the kernel already filled
// in. Variable structure sizes and async references are refused here.
LNL_API uint32_t lb_arc_interface_check_call(uint32_t selector, uint32_t scalarInputCount,
                                            uint32_t scalarOutputCount, uint32_t structInputBytes,
                                            uint32_t structOutputBytes, uint32_t asyncReferenceCount,
                                            uint32_t *reasonOut);
// Validate and translate one copied-in request. `window` is optional and only
// used by LBArcOpVmBind, where it must satisfy the existing GGTT contract; the
// window check here is an interface pre-check, and lb_ggtt_build_plan stays the
// authority that re-validates everything before any write plan exists.
// A refused request receives a complete rejection verdict with its reason; only
// an invalid output buffer leaves the output untouched. Inputs must not overlap
// outputs, and a refusal never reports translated values.
LNL_API uint32_t lb_arc_interface_evaluate(const LBArcRequest *request, uint32_t owner,
                                          const LBGgttWindow *window, LBArcDecision *out,
                                          uint32_t outputBytes);
// Derive the GuC registration input of an accepted queue-create request: the
// request carries the GGTT address of the context, and the descriptor is
// composed by the LRC port, not taken from the client. This registers nothing.
LNL_API uint32_t lb_arc_interface_queue_registration(const LBArcRequest *request, uint32_t owner,
                                                    LBGuCSchedulerRegistration *out,
                                                    uint32_t outputBytes);
}
