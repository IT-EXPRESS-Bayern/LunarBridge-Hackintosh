# Native GPU design and source seams

The goal is native macOS Tahoe on Lunar Lake, initially Arc 130V `8086:64A0`.
This is a porting hypothesis backed by source inspection, not a working driver.
GPU execution, Metal integration and display acceleration are not implemented.

## What the current Windows package contains

The inspected adapter used Intel version `32.0.101.9033` and the INF section
`LNL_IAG_wNext_Dynamic`. Read-only PE inspection distinguished these components:

| Installed component | Function | Native macOS consequence |
| --- | --- | --- |
| `igdkmdn64.sys` | WDDM kernel miniport | NT kernel imports cannot execute inside XNU |
| `igd12umd64.dll` | Direct3D 12 loader | Its Windows API/loader layer must be replaced |
| `igd12um64xe2.dll` | Xe2-specific D3D12 backend | Useful proof of architecture-specific backend separation; no source obtained |
| `igvk64.dll` | Vulkan ICD | Ships Windows OS dependencies; cannot simply load as a macOS ICD |
| `igc64.dll`, `igdfcl64.dll` | Compiler components | Intel publishes independent IGC source; binary conversion is not the source port |
| `igdrcl64.dll`, `ze_intel_gpu64.dll` | OpenCL/Level Zero runtime | Kernel allocation/submission interfaces still need an XNU backend |

Only names and PE import/export metadata were inspected. This does not recover
the proprietary implementation or its full call graph. Driver binaries and
machine-specific paths are excluded from this public repository.

## Compare the closest Apple-driver adaptations

Ice Lake/Gen11 is the last Intel iGPU family covered by the normal Hackintosh
Intel support path. [Dortania's Intel GPU guide](https://dortania.github.io/GPU-Buyers-Guide/modern-gpus/intel-gpu.html)
is the baseline, not proof that the Arc 130V can reuse that accelerator.

[WhateverXe](https://github.com/b00t0x/WhateverXe/tree/61a449255d385aeb468f64e5d5042e126f387b90)
provides a more useful open comparison for Xe-LP. Its author reports Metal,
OpenGL and video tests on selected Tiger Lake/Alder Lake-N systems under Big
Sur build 20G1443. At this pinned revision, Tahoe and internal displays are
planned; Xe2 and later architectures are explicitly outside its planned scope.

Read `WhateverXe/kern_xelp_devices.hpp` for backend selection,
`kern_xelp_memory_layout.hpp`, `kern_xelp_queue_memory.hpp`,
`kern_tgl_ppgtt.hpp` and `kern_tgl_csb.hpp` for memory/queue assumptions,
and the `kern_display13_*` files for display generation adaptation.
Adding `64A0` to its selection table would not supply these missing backends.

NootedGreen is an experimental comparison with invasive Apple-driver and
userspace patches. Its pinned license has a non-profit distribution/support
condition. No NootedGreen code is included or adapted into LunarBridge.

## Native Xe2 route

```text
macOS Metal calls and AIR shaders
    -> native macOS GPU-driver / Metal-provider integration [missing]
    -> metal2vulkan: AIR -> Vulkan Shader SPIR-V [external alpha component]
    -> Mesa ANV/NIR Intel backend: SPIR-V -> Xe2 code [XNU port missing]
    -> native user/kernel bridge: CPU buffers/traces [source implemented]
       GPU VA, DMA binding, queues and fences [missing]
    -> Xe platform/GuC logic adapted to XNU [missing]
    -> Arc 130V
```

This uses actual Xe2 code generation. It does not pretend that the Arc becomes
Xe-LP when fewer engines are enabled. It also does not route through MoltenVK:
MoltenVK itself requires an existing Metal driver, so using it here would create
a circular dependency.

[`metal2vulkan`](https://github.com/steelbrain/metal2vulkan/tree/43c46ac8a24adf1a6e872b8a52c706ec9614fad0)
supplies an AIR-to-SPIR-V translator and reflection API. It remains alpha.
It does not supply a macOS hardware Vulkan driver, GPU memory manager or Metal
device registration. Shader translation alone is not native GPU acceleration.

[`Intel IGC`](https://github.com/intel/intel-graphics-compiler/tree/6f81427a0be7c7c017d5be3f049dabec393cf788)
is an additional Xe2 compiler reference. It targets the OpenCL/compute stack.
Its OpenCL SPIR-V contracts are not interchangeable with the Vulkan Shader
SPIR-V output of metal2vulkan. IGC is therefore not a drop-in replacement for
the ANV/NIR Vulkan compiler in the route above.

## Exact source seams to adapt

Linux reference: v6.17, commit
`e5f0a698b34ed76002dc5cff3804a61c80233a7a`. These selected Xe files carry MIT notices;
other kernel dependencies must be reviewed individually before reuse.

| Piece | Source/function | Adaptation needed |
| --- | --- | --- |
| Identification | `include/drm/intel/pciids.h`, `INTEL_LNL_IDS`; `xe_pci.c`, `lnl_desc`, `read_gmdid` | Metadata implemented; native diagnostic source adds explicit opt-in GMD reads with preflight. Native power/MMIO/topology qualification still absent |
| GPU memory | `drivers/gpu/drm/xe/xe_bo.c`, `xe_ggtt.c`, `xe_vm.c`, `xe_vm_create_ioctl`, `xe_vm_bind_ioctl` | Replace DRM/TTM allocation, DMA and page ownership with XNU memory/IOKit interfaces; retain Xe2 page-table semantics |
| Execution | `xe_exec_queue.c`, `xe_exec_queue_create_ioctl`; `xe_exec.c`, `xe_exec_ioctl` | Provide validated native IOUserClient operations for queue creation, submission, lifetime and completion |
| Firmware/GuC | `xe_uc_fw.c`, `xe_guc.c`, `xe_guc_init`; `xe_guc_ct.c`; `xe_guc_submit.c`, `xe_guc_submit_init` | Port firmware transport, scheduling and synchronization; media can remain out of the first milestone |
| Userspace ABI | `include/uapi/drm/xe_drm.h` | IOUserClient CPU-buffer/snapshot/trace source implemented. A DRM `ioctl()` is not an IOKit call; GPU memory-bind, exec and fences remain missing |
| Compute runtime | Intel compute-runtime `shared/source/os_interface/linux/xe/ioctl_helper_xe.cpp`, `shared/source/compiler_interface/compiler_interface.cpp` | Another explicit OS seam; OpenCL-only execution would be an intermediate test, not Metal desktop acceleration |
| Presentation | IOKit framebuffer, display mode/link initialization, IOAccel/IOSurface coordination | A computed buffer still needs a complete macOS display/presentation path |

Intel compute-runtime source reference:
[`891b96dd9fc2a9bcb4aa15364762deaeef7297d4`](https://github.com/intel/compute-runtime/tree/891b96dd9fc2a9bcb4aa15364762deaeef7297d4).

## Smallest useful milestone

The real Windows Vulkan adapter reported one queue in a family with graphics
and compute, four extra compute queues, and separate transfer/video families.
These are logical API capabilities, not a physical fuse/topology reading and
not a macOS compatibility test.

Start with one user execution queue, one address space and bounded buffers.
Keep video, ray tracing, protected content and asynchronous extra queues outside
the initial milestone. Internal firmware/VM-bind dependencies must still be
handled: omitting user features does not prove that every associated hardware
initialization step can be omitted.

[Mesa's ANV documentation](https://docs.mesa3d.org/drivers/anv.html) describes
reduced queue configuration and debugging with compression disabled. Those
Linux-side settings demonstrate separable features; they do not install a
native macOS driver. GuC submission remains part of the reference Xe path.

Acceptance ladder:

1. Read the real GPU version/topology safely on a native macOS boot.
2. Allocate/bind an isolated GPU buffer and complete a bounded queue operation.
3. Execute a simple Xe2 compute shader and compare all result bytes.
4. Integrate a real Metal provider and pass `Metal-Minimaltest.mm`.
5. Present correct frames through the laptop panel, then test sleep/wake.

Metadata, bounded trace/preflight code, native IOKit diagnostic/CPU-buffer source,
collector and OpenCore installer have been implemented. Five native translation
units compile to macOS objects. No step in this native hardware ladder has passed.
The native diagnostic Kext has been linked in the macOS build workflow and has
not been loaded on the target; it does not bind GPU memory or execute a GPU
queue. Firmware UEFI Secure Boot remains a separate
required acceptance check; Apple's `SecureBootModel` is a different mechanism.

See [native backend](NATIVE_BACKEND.md), [diagnostics](DIAGNOSTICS.md),
[installer](INSTALLER.md) and [validation](VALIDATION.md) for the implemented boundary
and the next qualification steps.
