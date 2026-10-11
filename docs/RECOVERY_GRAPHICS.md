# Graphics interfaces found in the macOS recovery image

[![Recovery parser CI](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Hackintosh/actions/workflows/recovery-analysis.yml/badge.svg?branch=research%2Fmetal-contract)](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Hackintosh/actions/workflows/recovery-analysis.yml)
![Scope: offline metadata](https://img.shields.io/badge/Scope-offline%20metadata-blue)
![Native Arc acceleration: unverified](https://img.shields.io/badge/Native%20Arc%20acceleration-unverified-orange)

**The existing recovery image contains useful Metal and kernel graphics
interfaces. We have read them on Windows before installing macOS. These
observations do not implement or validate an Arc 130V graphics driver.**

Deutsch: Im vorhandenen Recovery-Abbild sind Metal, IOAccelerator, IOSurface,
CoreDisplay und drei ältere Intel-Framebuffer untersuchbar. Der eigene Parser
und die Befunde sind öffentlich; die extrahierten Apple-Dateien bleiben lokal.
Die native Xe2-Ausführung, der Metal-Provider und die beschleunigte Anzeige
bleiben noch zu implementieren und auf der Zielhardware zu prüfen.

## Actual inputs and verification

The inspected `BaseSystem.dmg` has **960,530,321 bytes** and SHA-256
`edddd0d5869caaa12e29e6996a04f11590280580976a119dbd42c24fa62fe18e`.
Its `SystemVersion.plist` reports **26.6.2 / 25G83**. That plist was previously
read with two independent parsers; the current whole-image hash matches that
same image. This analysis did not reverify Apple's signature.

We enumerated **69,746 catalog entries**, then extracted **26 selected files /
1,096,382,775 bytes** into a private workspace. Every extracted size and hash
was rechecked; the source image's hash matched after extraction. No mount,
Apple executable, installation or GPU submission was performed.

The read-only [analysis tool](../tools/recovery_graphics.py) processes:

* Two x86_64 `MH_FILESET` kernel collections with **95 and 204 entries**.
* A main dyld cache and its subcache, with paired UUIDs and **719 image records**.
* Load commands, selected nlist tables, prelink plists and export tries.
* Selected Objective-C names and **75 raw declared ivar offsets** in Metal.
* Metal library magic and bounded LLVM bitcode-marker observations.

It passed **32 synthetic input and format tests on Windows** at the time of
this report. The CI workflow tests those formats on Windows and Linux without
Apple payloads. These tests verify parser boundaries, not macOS hardware.

[RECOVERY_GRAPHICS.json](RECOVERY_GRAPHICS.json) records original-file hashes,
versions, references, symbol candidates, qualification limits and missing work.

## What is actually present

| Component | Observed content | Development use |
| --- | --- | --- |
| Metal 373.7 | 1,517 exports; 32 `MTLIOAccel*` class names; 75 declared ivar offsets | Identify device, resource, queue and encoder seams |
| IOAccelerator 487.4.3 | 223 exports, including device/context/resource/command queue functions | Investigate the connection between user space and the kernel accelerator |
| IOSurface 393.5.8 | 612 exports | Investigate shared surface storage and accelerator binding |
| CoreDisplay | 554 exports, including surface presentation | Investigate the final display path |
| IOSurfaceAccelerator | 109 exports | Investigate surface transfers and display-related operations |
| IOAcceleratorFamily2 | Kernel image and dependency metadata | Investigate accelerator classes and the user-client contract |
| Intel framebuffer 24.0.5 | KBL, CFL and ICLLP images embedded in the recovery kernel collection | Compare older display integration, matching and dependencies |

The framework binaries are in the dyld cache; their absence as separate files
does not mean the framework is absent. Likewise, the Intel framebuffer images
are in a kernel collection rather than separate executable files in each Kext.

The three inspected Intel personalities list older primary PCI IDs. None of
their literal `IOPCIPrimaryMatch` values is `0x64A08086`, our Arc 130V ID.
We have not validated the register, shader ISA or firmware compatibility of
these older framebuffer implementations with Lunar Lake.

## Concrete seams for a native implementation

1. **Device discovery and plugin loading.** Metal exports `MTLCopyAllDevices`
   and `MTLCreateSystemDefaultDevice`. Its data contains `MetalPluginName`,
   `MetalPluginClassName` and the `MTLIOAccelDevice` class name. These are useful
   landmarks for investigating provider discovery. Matching strings or exposing
   a PCI device does not establish a working provider.
2. **Device and resource state.** Exported ivar metadata includes
   `MTLIOAccelDevice._deviceRef`, `._sharedRef`, `._acceleratorPort` and
   `._acceleratorService`. In this cache, their raw declared offsets are 712,
   720, 728 and 736 bytes respectively. These are observations from one binary,
   not offsets to write into arbitrary runtime objects. Runtime adjustment,
   class inheritance and field types remain unqualified.
3. **Command submission.** IOAccelerator exports
   `IOAccelCommandQueueCreateWithQoS` and
   `IOAccelCommandQueueSubmitCommandBuffers`. Kernel nlist tables contain
   context, resource, device and fence symbols. Method selectors, structure
   layouts, ownership rules and validation must be recovered before adapting
   LunarBridge's existing GuC scheduler. Our own diagnostic selector numbers
   are not assumed to be Apple's accelerator ABI.
4. **Memory and surfaces.** `IOSurfaceCreate`, `IOSurfaceBindAccel` and
   `MTLIOAccelBuffer` provide concrete landmarks. The missing native GPU-VA,
   PAT, GGTT ownership, lifetime and DMA integration still have to be supplied
   by the Xe2 backend. A CPU-mapped buffer does not establish GPU binding.
5. **Display.** CoreDisplay exports presentation functions, while the older
   framebuffer personalities depend on IOAcceleratorFamily2, IOGraphicsFamily
   and IOPCIFamily. Their metadata exposes panel, power and display features.
   The Lunar Lake display engine and Samsung panel route require their own
   implementation; those older feature names do not validate their registers.
6. **Shader compilation.** Four `.metallib` resources have an `MTLB` header;
   the `.rtlib` resources have archive headers. Bitcode markers occur in them.
   We have not qualified their bitcode structure or obtained usable Xe2 machine
   code from those libraries. The compiler, resource reflection and device
   execution contracts remain separate work.

The earlier CPU-side [GGTT planner](GGTT_BINDING.md),
[GuC scheduler](GUC_SCHEDULER.md) and
[shader bridge](METAL_SHADER_BRIDGE.md) remain the relevant Xe2 foundations.
They must be connected to actual native memory, firmware and provider behavior.

The separate compiler run [38141140014](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Hackintosh/actions/runs/38141140014)
at source `96758804644985fecbd944ad303e122af060f557` passed 74 Windows harness
tests. Its real Metal and LLVM commands both returned zero; the checked
temporary compiler directory was removed. The next failure was
`AIR_KERNEL_METADATA_MISSING`. Original AIR retention and conversion remain
unqualified. This is a distinct failure from the earlier private-path refusal.

## Reproduce with your own image

Use a local recovery image obtained through the Apple recovery flow. A
portable 7-Zip build supporting DMG and HFS+ is sufficient to extract the
selected files. Keep Apple payloads outside the repository.

From the repository root, this PowerShell example extracts the relevant
groups into ignored `build/recovery-input/`:

```powershell
$recoveryMembers = @(
  'macOS Base System/System/Library/dyld/dyld_shared_cache_x86_64*'
  'macOS Base System/System/Library/KernelCollections/*.kc'
  'macOS Base System/System/Library/Extensions/IOAcceleratorFamily2.kext/Contents/Info.plist'
  'macOS Base System/System/Library/Extensions/IOSurface.kext/Contents/Info.plist'
  'macOS Base System/System/Library/Extensions/IOAccelerator2D.plugin/Contents/Info.plist'
  'macOS Base System/System/Library/Extensions/IOAccelerator2D.plugin/Contents/MacOS/IOAccelerator2D'
  'macOS Base System/System/Library/Frameworks/Metal.framework/Versions/A/Resources/*.metallib'
  'macOS Base System/System/Library/Frameworks/Metal.framework/Versions/A/Resources/*.rtlib'
  'macOS Base System/System/Library/Frameworks/Metal.framework/Versions/A/Resources/Info.plist'
  'macOS Base System/System/Library/Frameworks/IOSurface.framework/Versions/A/Resources/Info.plist'
  'macOS Base System/System/Library/PrivateFrameworks/IOAccelerator.framework/Versions/A/Resources/Info.plist'
  'macOS Base System/System/Library/PrivateFrameworks/IOSurfaceAccelerator.framework/Versions/A/Resources/Info.plist'
)
& C:/tools/7z.exe x BaseSystem.dmg -bd -y -obuild/recovery-input @recoveryMembers
if ($LASTEXITCODE -ne 0) { throw 'Extraction failed' }
python -B -X utf8 tools/recovery_graphics.py --extracted build/recovery-input --report build/recovery-graphics.json
python -B -X utf8 tests/test_recovery_graphics.py --report build/recovery-graphics-test-report.json
```

The parser supports the observed modern **x86_64** cache layout with named
subcaches and little-endian 64-bit Mach-O. It rejects unsupported layouts,
missing subcaches, UUID mismatches and invalid ranges. A newer OS image can
require explicit parser changes. It is not a general DMG extractor or a
complete Objective-C runtime reconstruction.

For the full `InstallAssistant.pkg`, first check package completion and
integrity. The partial package inspected locally already had a valid XAR
table checksum and a `SharedSupport.dmg` entry. Logical file size was reserved
in advance, so it did not prove a complete download. Installer package version
and the installed macOS version must be verified separately.

## Contributing for another GPU

Record the exact OS image hash/build, GPU architecture and PCI/subsystem IDs.
Run the static tool on your own extracted files and retain the original local
bytes. Compare symbol and dependency observations before proposing a contract.
For each proposed adapter, specify its resource, queue, synchronization,
shader and display responsibilities, and its target test conditions.

An export name or string observation is an **interface candidate**. A decoded
method and structure is still not a validated hardware implementation. Keep
static analysis, synthetic tests, native builds and real target-device results
distinct in contributions. Commit original analysis code and reports; keep
Apple binaries, internal build-path strings, raw memory and private machine
data out of shared artifacts. LunarBridge's original analysis code is GPLv3.

Format references used in this implementation are pinned in the JSON report:

* [Apple dyld cache format](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/include/mach-o/dyld_cache_format.h)
* [Apple Mach-O and fileset definitions](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/EXTERNAL_HEADERS/mach-o/loader.h)
* [Apple Objective-C runtime metadata](https://github.com/apple-oss-distributions/objc4/blob/fb265098298302243cd7eeaa1f63f0ba7786dd9a/runtime/objc-runtime-new.h)

The reference headers retain their Apple license; they are not redistributed
or relicensed by this repository. A working native Metal provider, accelerated
WindowServer and Arc 130V execution have not been demonstrated by this analysis.
