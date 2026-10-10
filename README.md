# LunarBridge-Hackintosh

**Experimental native macOS driver research for Intel Lunar Lake.**

[Deutsche Projektbeschreibung](README.de.md) ·
[Public repository](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Hackintosh)

LunarBridge investigates a native macOS path for Lunar Lake laptops, initially
Intel Arc 130V (`8086:64A0`) and Samsung Galaxy Book5 Pro I2C controllers.
The project is at the source research and host validation stage.
**There is no working Arc Metal driver, bootable EFI or installable GPU kext yet.**

Latest [validation](docs/VALIDATION.md): **16 host tests passed**, three
successful compiles, **zero native macOS hardware tests**.

## Current work

| Component | Implemented | Validation |
| --- | --- | --- |
| Lunar Lake PCI identification | C++ platform metadata, three Linux-listed device IDs | Compiled DLL; exhaustive PCI matching |
| GMD_ID decoder | Graphics/media offsets, version fields, mapping bounds | 20,000 synthetic values and boundary cases |
| IOKit integration seam | Read-only PCI configuration function | Compiled to x86_64 Mach-O object; not linked/loaded |
| Samsung I2C | Four PCI IDs, Samsung subsystem filter and Linux-derived timings | Eight source/matching checks; no macOS device test |
| Arc GPU execution / Metal / display | Research only | Zero native macOS hardware tests |

The first GPU milestone is a correctly completed operation through one user
execution queue. Removing asynchronous queues, media support and compression
from the initial scope may reduce work; it does not make a Xe-LP driver capable
of executing Xe2 instructions.

## Source layout

```text
gpu-prototype/       Portable metadata, IOKit seam and future Metal test
i2c-patch/           Experimental VoodooI2C source patch, not a release kext
tests/fixtures/     Public hardware ID fixture; no machine dump
tests/reference-manifest.json   Pinned external inputs and SHA256
tools/              Reference download and validation helpers
docs/               Native GPU architecture, hardware matrix and licensing
```

## Build and check under Windows

Use Python 3 and the official portable Zig 0.15.2 compiler. No system-wide
installation is required. MacKernelSDK headers are an external dependency.

```powershell
python tools/fetch_references.py
python gpu-prototype/build_gpu.py --compiler C:/path/to/zig.exe --sdk C:/path/to/MacKernelSDK --host-dir build/host --object-dir build/macos
python gpu-prototype/test_gpu.py build/host/lnl_host.dll
python i2c-patch/test_patch.py
```

This builds a Windows DLL and **two macOS object files**. It does not build,
sign, install or load a macOS GPU driver. Source checks and synthetic fixtures
cannot establish physical GPU compatibility.

`gpu-prototype/Metal-Minimaltest.mm` is a future native macOS acceptance test:
it requires an actual Metal provider and validates command completion and
65,536 computed values. It has not been compiled or run on this project.

## Native design

See [GPU architecture](docs/GPU_DESIGN.md) and
[hardware support](docs/HARDWARE_SUPPORT.md). macOS Tahoe on the external SSD,
Windows on the internal disk and firmware UEFI Secure Boot remain acceptance
requirements. This repository does not substitute a VM for native macOS.

## License

New LunarBridge work is licensed under **GNU GPL version 3 only**
(`GPL-3.0-only`); see [LICENSE](LICENSE). Commercial use and paid support are
allowed. Distribution of covered modified versions must comply with the GPL,
including its corresponding-source requirements.

Intel MIT notices and the VoodooI2C upstream GPL conditions are preserved.
External research references retain their own licenses. See
[licensing and commercial use](docs/LICENSING.md), [NOTICE](NOTICE) and
[license scope](LICENSE-STATUS.txt).
