# Public-Metal compute test

`gpu-prototype/Metal-Minimaltest.mm` exercises the selected macOS host's default Metal provider with an actual GPU shader. It builds the shader and pipeline, allocates three shared buffers, submits 65536 additions, observes the completion handler and completed command status, then compares every output against the expected value. The CPU initializes inputs and checks results; it never fills the output with a computed replacement. Command completion is bounded to 20 seconds.

Shader/library/pipeline, buffer, queue/encoder, command timeout/error and result mismatch failures use fixed machine-readable codes. Absence of a Metal provider yields `state=unverified`, `code=NO_METAL_PROVIDER` and no GPU success. No device name, RegistryID, device path, serial number, user identity, raw buffer data or framework/compiler error descriptions enter the published report. [Apple: command status](https://developer.apple.com/documentation/metal/mtlcommandbuffer/status), [completion handler](https://developer.apple.com/documentation/metal/mtlcommandbuffer/addcompletedhandler(_:))

## Build and run

An x86_64 macOS process with Apple command-line tools and the macOS SDK is required. From this repository:

```sh
python3 -B tools/metal_smoke_ci.py --context target-probe --build-root build/metal-smoke-private --out build/metal-smoke-report.json
```

The runner compiles Objective-C++ with `-arch x86_64`, ARC and Apple Foundation/Metal frameworks. Each invocation selects a newly created private `run-*` directory; a stale executable is never reused. The report records the actual selected source SHA-256 values, observed Git HEAD, compiler/SDK/macOS versions, executable SHA-256 and the checked x86_64 `MH_EXECUTE` code/entry metadata. Source hashes cover the test, runner, workflow and this document. No absolute build path or raw diagnostic log is published.

Tool queries have 10-second limits, compilation 55 seconds and the process probe 45 seconds. Timed-out process groups are killed, with at most five additional seconds to confirm termination. A failed probe preserves an already established build/hash result. Exit codes: 0 compute passed; 1 build or probe failed; 2 unsupported host or no Metal provider; 3 report selection/write failure. Windows exits before argument parsing or file-helper imports and writes no report, build directory or binary, even if output arguments are supplied.

## CI evidence and limits

The separate `metal-smoke.yml` runs on `macos-26-intel` with `context=ci-host`, read-only repository permissions and pinned actions. A built executable without a Metal provider retains the build evidence and reports unverified compute; shader/command/data failures fail the job. Only the sanitized JSON report is uploaded; raw logs, private build sources, binaries and working lists are excluded.

`target-probe` labels an explicitly requested local probe; it does not identify the selected GPU. Both contexts always leave `target_hardware_identity_verified`, `arc130v_backend_verified`, `native_driver_support_verified` and `display_acceleration_verified` false. A CI-host compute pass establishes that this acceptance test works on that host's Metal provider. It does not establish a functioning Arc 130V driver, Samsung boot, WindowServer acceleration, HDR, video acceleration, performance or sleep/wake.

At the preparation stage, only Windows guard and synthetic protocol/failure boundaries have been checked locally. A native build/compute result may be cited only after the real CI run is reviewed. No driver installation or firmware/system-policy change occurs.
