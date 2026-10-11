# GuC single-LRC scheduler codec

`gpu-prototype/lb_guc_scheduler.h/.cpp` implement a bounded, portable CPU
codec for the missing GuC queue commands and replies. The implementation
uses the existing `LBCtMessage` interface and CT encoder; it does not create a
second CT ring or perform MMIO, firmware, DMA or GPU operations. The current
scope is one serial Render/Compute KMD context for the pinned Linux v6.17 Xe
protocol profile. Multi-LRC work queues are excluded.

The 25 tests execute the real compiled C++ functions under Windows, including
the literal registration frame, transport interoperability, wrong owners,
stale proposals, malformed counts, reply/state mismatches and retained cleanup
credits. Intel-macOS compilation and physical GuC registration remain
unverified until separate evidence is obtained. No codec phase or passing
test establishes a Metal provider, GPU submission or executed work.

| Command | Action | HXG dwords | Input |
| --- | --- | --- | --- |
| Register single context | `0x4502` | 12 | KMD flag, ID, GuC engine class/mask, zero serial WQ fields, descriptor low/high |
| Set scheduling mode | `0x1001` | 3 | ID, enable `1` or disable `0` |
| Schedule context | `0x1000` | 2 | ID |
| Deregister context | `0x4503` | 2 | ID |
| Mode-done event | `0x1002` | 3 | ID, runnable state |
| Deregister-done event | `0x4600` | 2 | ID |

The registration enum names the descriptor low dword at index 10 and reports
an 11-dword enum length. The actual `__register_exec_queue` sender also emits
the descriptor high dword: its packet is **12 dwords**. This codec follows
the sender. A nonparallel context has no scheduler WQ descriptor, base or
size; dwords 5–9 are zero. Hardware ring/context state is in the LRC instead.

Xe sends these scheduler commands as Host-origin **FastRequest** HXG
messages, type `2`. Their CT fence has bit 15 set, marking the request
untracked; no registration success response is expected. Mode-done and
deregister-done are separate GuC-origin events, type `1`. A supplied generic
FastRequest error is type `6`, exactly one dword, with 12-bit hint and 16-bit
error; the retained fence identifies the failed operation. Busy, Retry and
Success responses cannot promote this codec's FastRequest state.

The receive path accepts the exact minimal event sizes and zero event DATA0
for this profile. These are deliberately strict codec limits: the upstream
event handlers accept minimum payload lengths and do not define additional
legacy event DATA0 fields. Future event extensions require a newly reviewed
profile. Events correlate by context ID and pending mode, not CT fence.

`lb_guc_scheduler_init` requires a zero-filled context, a nonzero local owner
and epoch, and a GuC ID below `65535`. `encode` builds a proposal without
changing state. After the caller enqueues the frame, `commit` validates the
proposal and advances the local revision. Replay, an intervening state
change, malformed fields or insufficient credits leave the context unchanged.
All failing encodes and receives preserve their output bytes. Input/output
objects must be disjoint. The caller must serialize every operation; these
are staged transactions, not thread synchronization primitives.

Packets are not authenticated capabilities. The real driver must keep them
private and immutable between validation and enqueue/commit. A changed but
otherwise valid descriptor cannot be qualified by this CPU codec. In a real
transport, an unexpected commit failure after enqueue requires quarantine or
reset; it cannot roll back a frame that firmware may already have read.

The state sequence records only local commits and supplied messages:
`Fresh → RegisterCommitted → EnableAwaitEvent → EnabledEventObserved →
DisableAwaitEvent → DisabledEventObserved → DeregisterAwaitEvent →
DeregisterEventObserved`. Submit is permitted only in `EnabledEventObserved`.
An observed failure moves to `FaultObserved` and retains all ledger entries.
There is no `actualRegistered` or GPU-completion flag.

Sixteen distinct FastRequest fences are retained without eviction. Ordinary
Enable/Submit commands reserve two remaining slots for Disable/Deregister;
Disable reserves one. The finite ledger is suitable for an initial bounded
queue probe, not a continuous production scheduler. Even deregister-done
does not release a buffer or erase the history. Production integration needs
a channel-wide retirement/reset rule and real memory ownership/lifetime.

The epoch is a local generation absent from GuC messages. It rejects stale
API proposals but cannot identify an old event bearing a newly reused GuC ID.
IDs must not be reused until the real transport has completed the required
drain/reset and all buffer references have been resolved. A different owner
number is not a substitute for kernel-managed resource access control.

For Lunar Lake, `xe_lrc_init` sets the valid bit and legacy 64-bit addressing
bits, optionally the PPGTT bit. Engine class/instance embedding occurs only
below graphics version 12.50. The codec checks this LNL descriptor shape and
a nonzero page address, preserving the caller's Render `0` or Compute `4`
GuC class and logical engine mask. It does not infer fused engines, accept
media/GSC engines or prove GGTT allocation, contents or safe DMA ownership.
The backend must apply actual GGTT bounds and match the descriptor/mask to
its owned LRC and discovered engines before enqueueing anything.

Firmware release and submission compatibility are distinct. The pinned
source recommends LNL GuC `70.45.2` and rejects release older than `70.29.2`;
its `GUC_SUBMIT_VER` comes from the separate CSS `submission_version` field.
For example, global scheduling KLV initialization is gated on submission
version `1.1.0`. This codec does not authenticate either field, assert that
all GuC 70 images share an ABI, or infer submission version from release.
The later native backend must record the real firmware's authenticity,
loaded/running state, submission version and accepted contract.

Before a native queue probe is possible, the driver still needs valid GuC
boot/authentication, ADS engine mappings/golden context data, actual LRC and
ring images, owned GGTT/PPGTT/IOMMU buffers, CT credits and memory barriers,
notification/interrupt handling, timeouts and a safe quiesce/reset path.
Firmware Secure Boot being disabled does not provide these conditions.
Scheduling a context does not validate commands or supply Metal/WindowServer
registration, a shader compiler, display modes or completion fences.

To test a separately built CPU library containing the scheduler and existing
CT implementation:

```sh
clang++ -std=c++17 -dynamiclib gpu-prototype/lb_guc_scheduler.cpp \
  gpu-prototype/lb_guc_transport.cpp -o build/libguc-scheduler-tests.dylib
python3 -B tests/test_guc_scheduler.py build/libguc-scheduler-tests.dylib
```

The command is a portable CPU test recipe; local macOS execution has not yet
been performed. The ignored report is `tests/guc-scheduler-test-report.json`.
It contains actual library/source hashes and test counts with hardware and
Metal verification explicitly false. No identifiers or supplied addresses
are exported in it. Own source is GPL-3.0-only; the Intel MIT reference
notices are retained separately. The public manifest records hashes and
source URLs, without vendoring the Linux implementation.

Primary pinned contracts:
[scheduler sender and event handlers](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_guc_submit.c),
[action IDs and offsets](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/abi/guc_actions_abi.h),
[HXG message semantics](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/abi/guc_messages_abi.h),
[CT FastRequest sequencing](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_guc_ct.c),
[firmware interface](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_guc_fwif.h),
[LRC descriptor](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_lrc.c),
[firmware version handling](https://github.com/torvalds/linux/blob/e5f0a698b34ed76002dc5cff3804a61c80233a7a/drivers/gpu/drm/xe/xe_uc_fw.c).
