# Validation on 2026-10-10

The public source layout was built and checked on Windows x86_64 with Zig
0.15.2 and external MacKernelSDK headers. All six compiler invocations exited
with code zero and no diagnostics. Outputs were one Windows test DLL and five
x86_64 macOS Mach-O object files; these are local build outputs excluded from Git.

| Check | Result | Scope |
| --- | --- | --- |
| C++ host execution | 8 tests passed | All 65,536 PCI device IDs, vendor rejection, platform limits, 20,000 synthetic GMD values, null/invalid inputs and BAR bounds |
| C++ diagnostic policies/traces | 12 tests passed | MMIO prerequisites, invalid config values, 5,000 buffer-size/budget cases, zeroed wire output, trace retention/cursors, corruption and overflow |
| I2C patch checks | 8 tests passed | Four documented Samsung device matches, rejection of foreign vendors/subsystems, unchanged existing configuration and Linux-derived controller timings |
| Trace/collector tools | 13 tests passed | ABI/length/order checks, exact privacy allowlist, bounded log decoding, error triage, simulated IOKit failures, CPU mapping/unmapping failures and real object-format checks |
| OpenCore installer | 18 tests passed | Synthetic PE/kernel format fixtures in isolated temporary EFI trees, plan/update/rollback, injected copy/config/receipt failures, concurrent edits and security/Vault refusal |
| Pinned test references | 7 SHA256 values verified | Exact external inputs identified in the reference manifest |
| macOS object format | Five Mach-O objects verified | Actual x86_64 object compilation and parser inspection; these objects are rejected by the installer |
| Native macOS link workflow | Real diagnostic Kext linked | Pinned external SDK and macOS linker; source commit, hashes and unsigned status in VALIDATION.json; no target-hardware loading |
| Native bundle installer integration | Passed in a synthetic EFI under Windows | Actual CI bundle planned/installed/rolled back; config bytes restored; no real EFI modified |

The public hardware fixture is a documented device-ID fixture, not a captured
machine inventory or a native device test. The I2C patch has also passed
`git apply --check` against the pinned upstream source.

All **59 Windows tests passed** without skips. The installer fixtures contain
no runnable macOS/EFI code. Simulated IOKit calls are marked as simulated and
cannot establish native device access. No real EFI or external SSD was changed.

No native macOS hardware test has passed. No actual GPU register was read,
no GPU operation was submitted by these host tests, and Metal acceleration
has not been implemented. The future `Metal-Minimaltest.mm` has not been built
or executed. Display, sleep/wake, firmware Secure Boot and the external-SSD
dual-boot installation remain unvalidated.

The machine-readable [validation record](VALIDATION.json) includes artifact
hashes and explicit test boundaries. Compiler commands containing local
machine paths and generated binaries are excluded from the public repository.
