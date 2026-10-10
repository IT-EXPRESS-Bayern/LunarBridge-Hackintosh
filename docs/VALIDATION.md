# Validation on 2026-10-10

The public source layout was built and checked on Windows x86_64 with Zig
0.15.2 and external MacKernelSDK headers. All three compiler invocations exited
with code zero and no diagnostics. Outputs were one Windows test DLL and two
x86_64 macOS Mach-O object files; these are local build outputs excluded from Git.

| Check | Result | Scope |
| --- | --- | --- |
| C++ host execution | 8 tests passed | All 65,536 PCI device IDs, vendor rejection, platform limits, 20,000 synthetic GMD values, null/invalid inputs and BAR bounds |
| I2C patch checks | 8 tests passed | Four documented Samsung device matches, rejection of foreign vendors/subsystems, unchanged existing configuration and Linux-derived controller timings |
| Pinned test references | 7 SHA256 values verified | Exact external inputs identified in the reference manifest |
| macOS object format | Two Mach-O objects verified | Object compilation only; no kext linking, signing or loading |

The public hardware fixture is a documented device-ID fixture, not a captured
machine inventory or a native device test. The I2C patch has also passed
`git apply --check` against the pinned upstream source.

No native macOS hardware test has passed. No actual GPU register was read,
no GPU operation was submitted by these host tests, and Metal acceleration
has not been implemented. The future `Metal-Minimaltest.mm` has not been built
or executed. Display, sleep/wake, firmware Secure Boot and the external-SSD
dual-boot installation remain unvalidated.

The machine-readable [validation record](VALIDATION.json) includes artifact
hashes and explicit test boundaries. Compiler commands containing local
machine paths and generated binaries are excluded from the public repository.
