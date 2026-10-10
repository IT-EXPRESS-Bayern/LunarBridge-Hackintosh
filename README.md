# LunarBridge-Hackintosh

**Experimental native macOS driver research for Intel Lunar Lake.**

[Deutsche Projektbeschreibung](README.de.md) ·
[Public repository](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Hackintosh)

LunarBridge investigates a native macOS path for Lunar Lake laptops, initially
Intel Arc 130V (`8086:64A0`) and Samsung Galaxy Book5 Pro I2C controllers.
The project has native diagnostic/CPU-buffer source and host validation tools.
**There is no working Arc Metal driver, bootable EFI or installable GPU kext yet.**

Latest [validation](docs/VALIDATION.md): **59 Windows host tests passed**, six
successful compiler invocations and five macOS objects. The native macOS workflow
has linked a real diagnostic Kext; **zero target macOS hardware tests have passed**.

## Current work

| Component | Implemented | Validation |
| --- | --- | --- |
| Lunar Lake PCI identification | C++ platform metadata, three Linux-listed device IDs | Compiled DLL; exhaustive PCI matching |
| GMD_ID decoder | Graphics/media offsets, version fields, mapping bounds | 20,000 synthetic values and boundary cases |
| Native IOKit diagnostic service | Narrow Samsung/Arc personality, PCI snapshot, opt-in GMD read preflight | Compiled against external SDK headers; no native MMIO/device test |
| Native CPU memory client | Administrator-only IOUserClient, isolated zeroed buffers, size/client limits | Source compiled; allocation policy executed under Windows; native lifecycle untested |
| Tracker and traces | ABI 1, bounded sequence ring, fixed events/status/context, macOS collector and triage | C++ ring tests and simulated IOKit/decoder/privacy tests; no target-device report |
| OpenCore installer | Explicit EFI plan/install/rollback, checksums, backups and conflict detection | 18 isolated transaction/format tests; no real EFI modified |
| Samsung I2C | Four PCI IDs, Samsung subsystem filter and Linux-derived timings | Eight source/matching checks; no macOS device test |
| Arc GPU execution / Metal / display | Research only | Zero native macOS hardware tests |

The first GPU milestone is a correctly completed operation through one user
execution queue. Removing asynchronous queues, media support and compression
from the initial scope may reduce work; it does not make a Xe-LP driver capable
of executing Xe2 instructions.

## Source layout

```text
native/             IOKit diagnostic service, CPU user client and Kmod metadata
gpu-prototype/      Portable metadata, preflight/trace code and future Metal test
installer/          OpenCore plan/install/rollback
i2c-patch/           Experimental VoodooI2C source patch, not a release kext
tests/fixtures/     Public hardware ID fixture; no machine dump
tests/reference-manifest.json   Pinned external inputs and SHA256
tools/              Native build, macOS collector, trace analysis and reference tools
docs/               GPU design, native backend, installer, diagnostics and validation
.github/            Native build workflow and diagnostic issue template
```

## Build and check under Windows

Use Python 3 and the official portable Zig 0.15.2 compiler. No system-wide
installation is required. MacKernelSDK headers are an external dependency.

```powershell
python tools/fetch_references.py
python gpu-prototype/build_gpu.py --compiler C:/path/to/zig.exe --sdk C:/path/to/MacKernelSDK --host-dir build/host --object-dir build/macos
python gpu-prototype/test_gpu.py build/host/lnl_host.dll
python gpu-prototype/test_diagnostics.py build/host/lnl_host.dll
python i2c-patch/test_patch.py
python tests/test_trace_tools.py
python tests/test_installer.py
```

This builds a Windows DLL and **five macOS object files**. It does not link,
sign, install or load a macOS GPU driver. Source checks and synthetic fixtures
cannot establish physical GPU compatibility.

## Native diagnostic build, installer and traces

On macOS with Xcode Command Line Tools and the external SDK:

```sh
python3 tools/build_native.py --sdk /path/to/MacKernelSDK --out build/native
```

The prepared GitHub workflow also uses an Intel macOS build runner and a pinned
SDK checkout. A successful native link/build and archive checks are recorded
with their exact source commit in [validation](docs/VALIDATION.json).
Building does not load a kext or test the Arc GPU. See [installer](docs/INSTALLER.md)
for the genuine kernel bundle and installer archive with corresponding source, and
[diagnostics](docs/DIAGNOSTICS.md) for collection and failure tracking.

MMIO is off by default. The native CPU buffer is not GPU-bound; GPU submission
returns `Unsupported`. Neither CPU mapping nor PCI identification is Metal acceleration.
The installer preserves Secure Boot settings and refuses policy bypass or
Vault-protected modifications. Firmware trust and actual boot behavior remain
separate target-device checks.

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
