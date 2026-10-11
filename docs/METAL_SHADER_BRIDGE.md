# Metal shader compiler bridge

The separate Intel-macOS CI job compiles our own
`gpu-toolchain/xe2_compute_probe.metal` to Apple AIR, builds the exact external
metal2vulkan commit `43c46ac8a24adf1a6e872b8a52c706ec9614fad0`, and translates
AIR to Vulkan 1.2 Shader SPIR-V plus reflection. This is a compiler integration.
Native Arc 130V execution, a system Metal provider, display acceleration and
WindowServer integration remain unverified. The first actual compiler run,
[38127106743](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Hackintosh/actions/runs/38127106743),
returned `unverified / toolchain / STEP_TIMEOUT` before converter building or
shader compilation. Its artifact hash and exact source were checked; the
workflow's original green result did not establish compiler success. The
workflow now fails when the compiler report remains unverified.

The second actual run,
[38129324218](https://github.com/IT-EXPRESS-Bayern/LunarBridge-Hackintosh/actions/runs/38129324218),
qualified discovery and version checks for Xcode's Metal compiler, then returned
`unverified / converter-build / STEP_TIMEOUT` after the 300-second Cargo deadline.
That report does not identify whether dependency resolution or compilation was
still active. No AIR or Shader SPIR-V output was produced by that attempt.

The 52 Windows offline boundary tests passed locally with zero failures,
errors or skips. They include the actual no-write Windows host guard;
process-group cleanup uses controlled process doubles and an actual held-pipe
CPU regression supervised by a separate 13-second deadline. These checks do
not execute a native Mac compiler or GPU. The ignored local report is
`tests/metal-shader-test-report.json`.

The MSL input and the existing OpenCL fixture use the same unsigned integer
formula: `(input[index] * 1664525 + 1013904223) ^ (0xA5A55A5A + index)` when
`index < count`. MSL binds input, output and a four-byte constant count through
Metal buffers 0, 1 and 2. This functional interface does not make Apple's AIR
argument ABI interchangeable with the existing OpenCL Xe2 binary payload.

The harness checks the actual AIR bitcode container, its `air64-apple-macosx`
target, kernel metadata and entry name. It bounds SPIR-V headers and
instructions, requires Shader/GLCompute/Logical/GLSL450, one `main` entry,
fixed local size `[64,1,1]` and descriptor set 0/bindings 0–2. A separate
`spirv-val --target-env vulkan1.2` invocation supplies the full external
structural validation. Reflection version 55 comes from the pinned Rust
implementation, whose source hash is checked; the prose reflection guide still
lists version 49. Reflection must name our Metal entry, three buffers with the
expected access and an integer four-byte count. Footprints require typed
static ranges, affine access terms and a Boolean unbounded flag. Integer sizes
and strides must fit the Rust u32/u64 fields; empty/incomplete footprints,
overflow and duplicate or unordered stride sources are refused. A 4096-element
vector cap bounds harness resource use. Valid translation
does not prove shader semantics by execution.

On an Intel Mac with a fresh external checkout inside this repository's
ignored `build/` directory, run:

```sh
python3 -B tools/metal_shader_ci.py --context target-probe \
  --converter-source build/metal2vulkan-source
```

The checkout must be clean and at the exact commit. Rust 1.87 or newer,
Xcode's Metal compiler, `llvm-dis` and `spirv-val` must be available. The
CLI exits before argument parsing or file writes on Windows or non-Intel
macOS. Every run uses new private scratch space and a new artifact directory.
An existing artifact directory is refused to avoid reusing old evidence.

Probe subprocesses have a maximum 60-second execution deadline. After each
step, cleanup attempts to kill the complete process group and reap the parent
within five seconds, then joins each pipe reader for at most five seconds.
An active reader owns its buffered stream; the caller never synchronously
closes a stream whose read is still blocked. Failed kill/reap/drain returns
an unverified cleanup error and cannot become compiler success.
Metal discovery and version checks each allow 60 seconds and record separate
fixed stages with timeout and completion details. Our MSL is compiled to AIR
and inspected through `llvm-dis` before Cargo starts. Actual MSL, AIR and LLVM
output hashes are retained even if the later converter build fails; partial
AIR evidence cannot become verified Shader SPIR-V or GPU execution.
Cargo dependency resolution has a 300-second deadline and records an actual
generated lockfile when available. The converter build uses `cargo build
--locked` with a 900-second deadline and rejects changes to that lockfile.
Sanitized progress records only bounded phase categories, counters and elapsed
time, without raw log lines, package names or local paths. In an expressly
enabled, ephemeral GitHub CI environment,
an unavailable compiler or cleanly terminated initial probe timeout permits
one `xcodebuild -downloadComponent MetalToolchain` attempt, also bounded to
300 seconds. Its trigger, completion and elapsed time are recorded. Cleanup
errors and failed probes after this attempt remain terminal; target probes
do not download components. There are no Security Policy,
EFI, SIP, driver-installation or GPU-control operations. Missing prerequisites
or failed phases return 2 and `unverified`; they cannot set shader or GPU success.

The artifact contains only our generated MSL/AIR/SPIR-V/reflection, the
original converter LGPL license, generated Cargo.lock, dependency provenance
and a sanitized report. It records the actual converter source-tree digest,
compiler and validator executable hashes, tool versions, resolved dependency
checksums/licenses, own-source hashes and output hashes. External converter
source and raw compiler output are excluded. Existing OpenCL fixtures stay
unchanged. Cargo.lock is generated from the selected source before the locked
build, so Rust/crates
and LLVM versions still need comparison when reproducing an earlier run.

Own code is GPL-3.0-only. The external converter retains its original
LGPL-3.0-or-later license and is an independently built checkout dependency.
The implementation uses its documented CLI and `serde` reflection feature;
it does not publish Apple SDK/compiler binaries or captured system shaders.
The next backend stage is Shader SPIR-V to ANV/NIR Xe2 machine code with a
native memory/submission path. This compiler bridge supplies neither that
backend nor the private macOS system-provider ABI. The pinned
[Mesa backend seams](METAL_BACKEND_SEAMS.md) describe the separate CPU parser,
descriptor lowering, BRW compiler and native submission work.

Primary contracts: [metal2vulkan CLI](https://github.com/steelbrain/metal2vulkan/blob/43c46ac8a24adf1a6e872b8a52c706ec9614fad0/src/main.rs),
[reflection Rust schema](https://github.com/steelbrain/metal2vulkan/blob/43c46ac8a24adf1a6e872b8a52c706ec9614fad0/src/reflect/mod.rs),
[external tool runner](https://github.com/steelbrain/metal2vulkan/blob/43c46ac8a24adf1a6e872b8a52c706ec9614fad0/src/tools.rs),
[Apple Metal libraries](https://developer.apple.com/documentation/metal/metal-libraries).
