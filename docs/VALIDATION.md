# Validation and integration on 2026-10-11

The public source layout was built and checked on Windows x86_64 with Zig
0.15.2 and external MacKernelSDK headers. All 15 compiler invocations exited
with code zero and no diagnostics. Outputs were one Windows test DLL and fourteen
x86_64 macOS Mach-O object files; these are local build outputs excluded from Git.

| Check | Result | Scope |
| --- | --- | --- |
| C++ host execution | 8 tests passed | All 65,536 PCI device IDs, vendor rejection, platform limits, 20,000 synthetic GMD values, null/invalid inputs and BAR bounds |
| C++ diagnostic policies/traces | 12 tests passed | MMIO prerequisites, invalid config values, 5,000 buffer-size/budget cases, zeroed wire output, trace retention/cursors, corruption and overflow |
| GPU memory / GuC foundation | 31 tests passed | Owner isolation, token lifetime, 48-bit VA/46-bit DMA bounds, exact PTE vectors, table boundaries, atomic staging, CSS lengths/versions, HXG direction/types and shadow-ring wrap/corruption |
| Native DMA adapter | 30 tests passed | Executed actual adapter C++ with fake IOKit: exact mapper, preparation failures, segment bounds, complete/detach/unwire order and temporary/persistent cleanup failure |
| DMA ownership quarantine | 10 tests passed | Actual eight-slot table, owner identity, atomic transfer, exclusive retry, retained accounting and commit only after successful adapter cleanup; no native kernel concurrency |
| Forcewake protocol | 29 tests passed | Actual production C++ with register callbacks; masked paired writes, foreign request refusal, post-read deadlines, all-ones, partial writes, timeout, reference integrity and retained failed cleanup |
| Native Forcewake adapter | 43 tests / 52 scenarios passed | Actual adapter with explicit IOKit doubles; GMD/IP/BAR/D0 guards, four root flush writes per read, held map/open/PCI until confirmed cleanup; no native hardware |
| Trace/collector tools | 29 tests passed | ABI/length/order checks, forcewake status and retained cleanup, privacy, DMA double opt-in, error triage, simulated IOKit failures and real object-format checks |
| GPU OpenCore installer | 23 tests passed | Isolated EFI transactions and failures, concurrent edits, policy/Vault refusal, DMA/forcewake flag types, effective MMIO/flush exposure and module boundaries |
| Own shader toolchain | 37 offline tests passed | Actual recorded own Windows GPU result bytes; device qualification, bounded ELF/Zebin and relocation validation; no GPU initialized by tests |
| Own Xe2 CPU payload | 37 offline tests passed | Authenticated own Zebin, 64/32/32-byte layout, unresolved pointer holes, original zero ImplicitArgs relocations and bounded planned group dimensions; no GPU job |
| Metal shader compiler harness | 64 offline tests passed | Bounded MSL/AIR/Shader SPIR-V/reflection and supervised process cleanup; controlled doubles, no native compiler or GPU execution |
| GGTT planner | 31 tests passed / 132 C++ calls | Actual DMA/PPGTT to GGTT plan with independent PAT encoding, separate reservations, atomic failure and explicit missing hardware qualifications |
| GuC scheduler codec | 25 tests passed | Actual 12-DW serial registration, FastRequest/event/error parsing, revision/owner/epoch and bounded fence ledger; CPU messages only |
| Kernel build contract | 8 tests passed | Kernel-only stack-check flags and real Mach-O import boundaries; target load and stack budget remain open |
| Mesa CPU build harness | 29 tests passed | Host guards, source and wheel provenance, actual process timeout and parser format boundaries; real Intel CI parser build pending |
| Arc interface contract | 51 tests passed | Actual compiled `lb_arc_interface` C++ through ctypes on Linux/clang 18.1.3: wire sizes, operation table, external-method call shapes, per-operation accept/reject matrix, owner binding, buffer aliasing and user-space decoding; no MMIO, DMA, firmware or GPU |
| Arc interface source comparison | 13 tests passed, 0 mismatches | Pinned Linux v6.17 Xe uAPI (12 ioctls, 11 mapped) against the published Apple xnu / IOGraphics / IOPCIFamily contracts; the compiled table verified against those sources; the public Apple Intel inventory ends at Gen11 |
| Ported Xe2 PAT and LRC contracts | 26 tests passed | Compiled `lb_xe2_pat` and `lb_lrc` C++ against values parsed live from the pinned Intel MIT sources: 32 PAT entries, 32 register offsets, the 33-write plan with readback check, LRC layout, 10 context-image writes, descriptor, PPGTT root and ring prologue, plus the Apple ICL bundle shape this kext mirrors; no MMIO, GGTT write, firmware or submission |
| Pinned GPU test references | 44 SHA256 values verified | Exact external inputs including forcewake, MMIO and LNL workaround association; no vendored Linux driver |
| macOS object format | Fourteen Mach-O objects verified | Actual x86_64 object compilation and parser inspection; these objects are rejected by the installer |
| Native macOS link workflow | Real diagnostic Kext linked | Pinned external SDK and macOS linker; source commit, hashes and unsigned status in VALIDATION.json; no target-hardware loading |
| Native bundle installer integration | Passed in a synthetic EFI under Windows | Actual CI bundle planned/installed/rolled back; config bytes restored; no real EFI modified |
| Packaged CLI and corresponding source | Passed for the exact recorded build | Packaged plan/install/rollback CLI executed in a synthetic EFI; corresponding-source hash count, binary hash and archive CRC recorded in VALIDATION.json |

The Arc interface and Arc port suites were executed in a Linux x86_64 container, not on
the Windows host, and are therefore counted separately from the Windows total
below. They are wired into the Intel macOS CI profile and the new Linux
interface workflow; no CI run of them is recorded yet. They establish a CPU
interface contract only: an accepted request shape is neither a submission nor
GPU execution, and the new selectors have not been called on target hardware.

The public hardware fixture is a documented device-ID fixture, not a captured
machine inventory or a native device test. I2C source and build qualification
now belong to the independent LunarBridge-I2C repository and are not counted
as graphics-repository tests.

All **444 graphics-repository core Windows tests passed** without skips in the
recorded run of 2026-10-11; the suites added later are listed above with their
own environments and counts rather than folded into that figure. The installer fixtures contain
no runnable macOS/EFI code. Simulated IOKit calls are marked as simulated and
cannot establish native device access. No real EFI or external SSD was changed.

No native Samsung hardware test has passed. No actual GPU register was read
by these Windows host tests, and the Arc 130V Metal driver remains unimplemented.
Display, sleep/wake, firmware Secure Boot and the external-SSD dual-boot
installation remain unvalidated.

An own OpenCL shader actually ran once on this Samsung's Arc 130V under Windows.
All 64 output values matched the independent reference. Its own 5,728-byte
Zebin program includes a 896-byte Xe2 code section and 1,464-byte SPIR-V section.
The [source-bound proof and fixtures](../gpu-toolchain/README.md) retain the
executed probe version; the revised public CLI was not rerun on the GPU.
This Windows result is distinct from native macOS execution and Metal.
The [native bring-up](GPU_BRINGUP.md) is explicitly prepared without firmware
Secure Boot for its first attempt; native Kext admission remains separate.

The separate [Metal workflow 38114157507](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Hackintosh/actions/runs/38114157507)
at source `c4872003430ff804977ac76356f7f0537677a12b` built the real x86_64
`Metal-Minimaltest.mm` executable on Intel macOS 26.6.1 and executed its shader.
All 65,536 outputs matched, with completion handler and completed command status
observed and no CPU fallback. Its additional Windows job passed 18 harness
boundary tests. These are counted separately from the 444 core tests above.
Both downloaded report archives passed hash and CRC checks; all five distinct
source files were compared with that exact Git commit. The binary itself is not
uploaded. [METAL_VALIDATION.json](METAL_VALIDATION.json) records the evidence and
keeps Samsung identity, Arc 130V backend, driver support and display proof false.

The final diagnostic build uses project-owned module entry glue and links no
static SDK library. It was built with Apple clang on an Intel macOS 26 runner
and has no code signature. Its exact source commit differs from later documentation
commits; the commit and hashes are retained in VALIDATION.json and the packaged
SOURCE-REFERENCE.json. The successful link is separate from target-device loading.

The [native build 38132276514](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Hackintosh/actions/runs/38132276514)
from `3b7550d154ee017487c1858591af2d02f4cbed05` includes the Xe2 memory and
GuC CPU helpers, native DMA adapter, eight-slot cleanup quarantine and new
Forcewake protocol/adapter, GGTT planner and GuC scheduler codec. Both diagnostic and optional Forcewake profiles
passed. Their defined symbols were inspected in the actual 78,960-byte kernel
binary. All 28
corresponding-source hashes were verified from the downloaded installer package
and compared byte-for-byte with that exact Git commit. The actual packaged CLI
completed plan, install and rollback for both profiles in separate synthetic EFIs; each original config
bytes were restored. Internal task and plan files are absent.

DMA preparation is disabled in the delivered bundle and requires explicit
build-time and probe-time opt-in. Neither DMA operation nor quarantine cleanup
has been exercised on the Samsung. The linked symbols do not establish GPU
page-table binding, GuC startup, GPU execution or Metal acceleration.

The machine-readable [validation record](VALIDATION.json) includes artifact
hashes and explicit test boundaries. Compiler commands containing local
machine paths and generated binaries are excluded from the public repository.

The GGTT planner and GuC scheduler codec are now included in the host DLL, macOS object compilation and native build recipe. Their production code passed its tests in that integrated DLL. The reviewed native binary above includes these two helpers; its exact source commit remains explicit. Native global GGTT ownership, PAT programming, GuC firmware startup, ADS/LRC/queues and hardware submission remain unimplemented.

Fourteen fresh kernel objects were cross-compiled after adding the upstream kernel build flag `-fno-stack-check`. Every header was verified against the pinned SDK tree, and those objects contain no Darwin userspace stack-probe symbol. The historical 755ba093 CI binary had that import. The actual 3b7550d Apple-clang build now proves its removal from both imports and definitions; native kernel admission and stack budget remain unqualified.
