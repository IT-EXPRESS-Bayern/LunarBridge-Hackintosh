# Validation and integration on 2026-10-11

The public source layout was built and checked on Windows x86_64 with Zig
0.15.2 and external MacKernelSDK headers. All eleven compiler invocations exited
with code zero and no diagnostics. Outputs were one Windows test DLL and ten
x86_64 macOS Mach-O object files; these are local build outputs excluded from Git.

| Check | Result | Scope |
| --- | --- | --- |
| C++ host execution | 8 tests passed | All 65,536 PCI device IDs, vendor rejection, platform limits, 20,000 synthetic GMD values, null/invalid inputs and BAR bounds |
| C++ diagnostic policies/traces | 12 tests passed | MMIO prerequisites, invalid config values, 5,000 buffer-size/budget cases, zeroed wire output, trace retention/cursors, corruption and overflow |
| GPU memory / GuC foundation | 31 tests passed | Owner isolation, token lifetime, 48-bit VA/46-bit DMA bounds, exact PTE vectors, table boundaries, atomic staging, CSS lengths/versions, HXG direction/types and shadow-ring wrap/corruption |
| Native DMA adapter | 30 tests passed | Executed actual adapter C++ with fake IOKit: exact mapper, preparation failures, segment bounds, complete/detach/unwire order and temporary/persistent cleanup failure |
| DMA ownership quarantine | 10 tests passed | Actual eight-slot table, owner identity, atomic transfer, exclusive retry, retained accounting and commit only after successful adapter cleanup; no native kernel concurrency |
| Trace/collector tools | 19 tests passed | ABI/length/order checks, exact privacy allowlist, DMA double opt-in, bounded log decoding, error triage, simulated IOKit failures, CPU mapping/unmapping failures and real object-format checks |
| GPU OpenCore installer | 21 tests passed | Synthetic PE/kernel format fixtures in isolated temporary EFI trees, plan/update/rollback, injected failures, concurrent edits, policy/Vault refusal, DMA flag types and rejection of extra hardware personalities/nested kexts |
| Pinned GPU test references | 12 SHA256 values verified | Exact external GPU inputs identified in the reference manifest; independent I2C inputs moved out |
| macOS object format | Ten Mach-O objects verified | Actual x86_64 object compilation and parser inspection; these objects are rejected by the installer |
| Native macOS link workflow | Real diagnostic Kext linked | Pinned external SDK and macOS linker; source commit, hashes and unsigned status in VALIDATION.json; no target-hardware loading |
| Native bundle installer integration | Passed in a synthetic EFI under Windows | Actual CI bundle planned/installed/rolled back; config bytes restored; no real EFI modified |
| Packaged CLI and corresponding source | Passed for the exact recorded build | Packaged plan/install/rollback CLI executed in a synthetic EFI; corresponding-source hash count, binary hash and archive CRC recorded in VALIDATION.json |

The public hardware fixture is a documented device-ID fixture, not a captured
machine inventory or a native device test. I2C source and build qualification
now belong to the independent LunarBridge-I2C repository and are not counted
as graphics-repository tests.

All **131 graphics-repository Windows tests passed** without skips. The installer fixtures contain
no runnable macOS/EFI code. Simulated IOKit calls are marked as simulated and
cannot establish native device access. No real EFI or external SSD was changed.

No native macOS hardware test has passed. No actual GPU register was read,
no GPU operation was submitted by these host tests, and Metal acceleration
has not been implemented. The future `Metal-Minimaltest.mm` has not been built
or executed. Display, sleep/wake, firmware Secure Boot and the external-SSD
dual-boot installation remain unvalidated.

The final diagnostic build uses project-owned module entry glue and links no
static SDK library. It was built with Apple clang on an Intel macOS 26 runner
and has no code signature. Its exact source commit differs from later documentation
commits; the commit and hashes are retained in VALIDATION.json and the packaged
SOURCE-REFERENCE.json. The successful link is separate from target-device loading.

The build from `cb47a2de119440731a44a14105df4a4192613123` includes the Xe2
memory and GuC CPU helpers. Their defined symbols were inspected in the actual
kernel binary, and all 16 corresponding-source hashes were verified from the
downloaded installer package. Internal task and plan files are absent. These
symbols do not establish GPU page-table binding, GuC startup or GPU execution.
That earlier artifact predates the native DMA adapter and its cleanup quarantine.
Their current source compiles into real objects; a new native link/build must
be recorded against its exact source commit before package delivery.

The machine-readable [validation record](VALIDATION.json) includes artifact
hashes and explicit test boundaries. Compiler commands containing local
machine paths and generated binaries are excluded from the public repository.
