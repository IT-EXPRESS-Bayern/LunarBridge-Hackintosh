#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Offline format/host/deadline boundaries; no Mesa compile or GPU claim."""
import contextlib
import importlib.util
import io
import hashlib
import json
from pathlib import Path
import struct
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("mesa_cpu_ci", Path(__file__).resolve().parents[1] / "tools/mesa_cpu_ci.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def macho(cpu=0x01000007, kind=2, commands=1, command_bytes=24):
    return struct.pack("<IiiIIIII", 0xFEEDFACF, cpu, 3, kind, commands, command_bytes, 0, 0) + struct.pack("<IIQQ", 0x80000028, 24, 0, 0)


def metadata():
    return {"info": {"version": "1.9.1"}, "urls": [{"filename": "meson-1.9.1-py3-none-any.whl",
        "packagetype": "bdist_wheel", "url": "https://files.pythonhosted.org/packages/meson.whl",
        "digests": {"sha256": module.PURE_WHEEL_HASHES["meson"]}, "size": 1030356}]}


class CpuBoundaries(unittest.TestCase):
    def test_windows_guard_runs_before_argparse_or_writes(self):
        with patch.object(module, "native_host", return_value=False), patch.object(module.Path, "mkdir", side_effect=AssertionError("unexpected write")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(module.main(["--invalid", "private-user-path"]), 2)
            report = json.loads(output.getvalue())
            self.assertEqual(report["status"], "unsupported-platform")
            self.assertFalse(report["writes_performed"])
            self.assertNotIn("private-user-path", output.getvalue())

    def test_arm_macos_is_not_intel_ci_host(self):
        with patch.object(module.sys, "platform", "darwin"), patch.object(module.platform, "machine", return_value="arm64"):
            self.assertFalse(module.native_host())

    def test_macho_intel_execute_format(self):
        self.assertEqual(module.qualify_macho(macho())["file_type"], "MH_EXECUTE")

    def test_own_source_provenance_persisted_before_dependencies(self):
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in module.OWN_SOURCE_PATHS:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"offline-boundary-source\n")
            class FakeSteps:
                head = commit
                def __init__(self, *args, **kwargs):
                    self.records = []
                def run(self, *args, **kwargs):
                    return (self.head + "\n").encode("ascii")
            steps = FakeSteps()
            with patch.dict(module.os.environ, {"GITHUB_SHA": commit}):
                reference = module.own_source_reference(root, steps)
                self.assertEqual(reference["own_source_commit"], commit)
                self.assertEqual(len(reference["own_source_files"]), 3)
                for item in reference["own_source_files"]:
                    self.assertEqual(item["sha256"], hashlib.sha256(b"offline-boundary-source\n").hexdigest())
                    self.assertEqual(item["bytes"], 24)
                report = {**reference, "status": "cpu-parser-qualified", "parser_built": True,
                          "parser_help_verified": True}
                module.write_report(root, {**report, "status": "unverified", "phase": "own-source"})
                persisted = json.loads((root / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(persisted["own_source_files"], reference["own_source_files"])
                self.assertEqual(persisted["phase"], "own-source")
                steps.head = "b" * 40
                with self.assertRaisesRegex(module.BuildError, "own-source-commit-mismatch"):
                    module.own_source_reference(root, steps)
            with patch.dict(module.os.environ, {"GITHUB_SHA": "not-a-commit"}), patch.object(module, "bounded_file", side_effect=AssertionError("source read before SHA guard")):
                with self.assertRaisesRegex(module.BuildError, "own-source-github-sha-invalid"):
                    module.own_source_reference(root, steps)
            def first_dependency(*args, **kwargs):
                early = json.loads((root / "build/guarded-report/report.json").read_text(encoding="utf-8"))
                self.assertEqual(early["own_source_commit"], commit)
                self.assertEqual(early["own_source_files"], reference["own_source_files"])
                self.assertEqual(early["phase"], "own-source")
                self.assertEqual(early["status"], "unverified")
                raise module.BuildError("offline-stop-before-dependencies")
            with patch.dict(module.os.environ, {"GITHUB_SHA": commit}), patch.object(module, "native_host", return_value=True), patch.object(module.Path, "cwd", return_value=root), patch.object(module, "__file__", str(root / "tools/mesa_cpu_ci.py")), patch.object(module, "Steps", FakeSteps), patch.object(module, "verify_source", return_value={"commit": module.MESA_PIN}), patch.object(module, "install_packages", side_effect=first_dependency), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(["--source", str(root / "source"), "--out", str(root / "build/guarded-report")]), 2)

    def test_ci_qualification_requires_exit_status_and_complete_provenance(self):
        files = [{"path": name, "bytes": 24,
                  "sha256": hashlib.sha256(b"offline-boundary-source\n").hexdigest()}
                 for name in module.OWN_SOURCE_PATHS]
        report = {"own_source_commit": "a" * 40, "own_source_files": files,
                  "status": "cpu-parser-qualified", "parser_built": True, "parser_help_verified": True}
        self.assertEqual(module.qualification_exit_code(report, 0), 0)
        for status in (2, 1, True, "0"):
            with self.subTest(cli_exit=status):
                self.assertEqual(module.qualification_exit_code(report, status), 2)
        for invalid in ({**report, "status": "unverified"}, {**report, "own_source_files": []},
                        {**report, "parser_built": False}, {**report, "parser_help_verified": False},
                        {**report, "own_source_commit": "not-a-commit"},
                        {**report, "own_source_files": [files[0], files[0], files[2]]}):
            with self.subTest(invalid_report=invalid):
                self.assertEqual(module.qualification_exit_code(invalid, 0), 2)

    def test_macho_arm_executable_rejected(self):
        with self.assertRaises(module.BuildError):
            module.qualify_macho(macho(cpu=0x0100000C))

    def test_macho_dylib_is_not_parser(self):
        with self.assertRaises(module.BuildError):
            module.qualify_macho(macho(kind=6))

    def test_macho_truncated_header_rejected(self):
        with self.assertRaises(module.BuildError):
            module.qualify_macho(macho()[:31])

    def test_macho_load_command_oversized_rejected(self):
        with self.assertRaises(module.BuildError):
            module.qualify_macho(macho(command_bytes=28))

    def test_macho_count_mismatch_rejected(self):
        with self.assertRaises(module.BuildError):
            module.qualify_macho(macho(commands=2))

    def test_exact_pure_wheel_digest_selected(self):
        self.assertEqual(module.select_wheel(metadata(), "meson", "1.9.1")["size"], 1030356)

    def test_changed_package_version_rejected(self):
        with self.assertRaises(module.BuildError):
            module.select_wheel(metadata(), "meson", "1.9.2")

    def test_unknown_wheel_host_rejected(self):
        data = metadata()
        data["urls"][0]["url"] = "https://example.invalid/meson.whl"
        with self.assertRaises(module.BuildError):
            module.select_wheel(data, "meson", "1.9.1")

    def test_float_wheel_size_rejected(self):
        data = metadata()
        data["urls"][0]["size"] = 1030356.0
        with self.assertRaises(module.BuildError):
            module.select_wheel(data, "meson", "1.9.1")

    def test_ambiguous_wheel_selection_rejected(self):
        data = metadata()
        data["urls"].append(dict(data["urls"][0]))
        with self.assertRaises(module.BuildError):
            module.select_wheel(data, "meson", "1.9.1")

    def test_changed_known_wheel_digest_rejected(self):
        data = metadata()
        data["urls"][0]["digests"]["sha256"] = "0" * 64
        with self.assertRaises(module.BuildError):
            module.select_wheel(data, "meson", "1.9.1")

    def test_bounded_regular_file_size_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large"
            path.write_bytes(b"12345")
            with self.assertRaises(module.BuildError):
                module.bounded_file(path, 4)

    def test_directory_input_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(module.os, "open", side_effect=AssertionError("directory opened")):
            with self.assertRaises(module.BuildError):
                module.bounded_file(Path(directory))

    def test_private_path_is_removed_from_diagnostic(self):
        synthetic_windows_path = "C:" + "\\Users\\" + "private-person\\file"
        text = module.sanitize("error /Users/private-person/project/file and " + synthetic_windows_path, [])
        self.assertNotIn("private-person", text)

    def test_real_process_timeout_is_bounded_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            steps = module.Steps(Path(directory), [Path(directory)])
            with self.assertRaises(module.BuildError):
                steps.run("timeout", [sys.executable, "-c", "import time; time.sleep(20)"], 0.15)
            self.assertTrue(steps.records[0]["timed_out"])
            self.assertIsNotNone(steps.records[0]["returncode"])
            self.assertLess(steps.records[0]["elapsed_seconds"], 12)

    def test_compiled_source_outside_roots_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            build = root / "build"
            source.mkdir()
            build.mkdir()
            class FakeSteps:
                def run(self, name, *args, **kwargs):
                    if name == "compiled-database":
                        return json.dumps([{"directory": str(root), "file": "outside.c"}]).encode()
                    return b""
            with patch.object(module, "bounded_file", side_effect=AssertionError("outside file read")):
                with self.assertRaises(module.BuildError):
                    module.source_reference(source, build, {"NINJA": "not-executed"}, FakeSteps(), {})

    def test_sdk_sysroot_reaches_compile_and_link_flags_without_mutating_input(self):
        with tempfile.TemporaryDirectory(prefix="cpu sdk boundary ") as directory:
            sdk = Path(directory) / "MacOSX26.6.sdk"
            (sdk / "usr/lib").mkdir(parents=True)
            (sdk / "SDKSettings.json").write_bytes(b'{"Version":"26.6"}')
            (sdk / "usr/lib/libSystem.tbd").write_bytes(b"offline-sdk-metadata-boundary\n")
            class FakeSteps:
                def run(self, name, *args, **kwargs):
                    return (str(sdk) if name == "sdk-path" else "26.6").encode()
            original = {"CFLAGS": "unqualified-inherited-value"}
            environment, report = module.macos_sdk_environment(original, FakeSteps())
            self.assertEqual(original["CFLAGS"], "unqualified-inherited-value")
            self.assertEqual(environment["SDKROOT"], str(sdk.resolve()))
            for name in ("CFLAGS", "CXXFLAGS", "OBJCFLAGS", "LDFLAGS"):
                self.assertEqual(shlex.split(environment[name]), ["-isysroot", str(sdk.resolve()), "-arch", "x86_64"])
            self.assertEqual(report["version"], "26.6")
            self.assertEqual(report["system_library_tbd_sha256"], hashlib.sha256(b"offline-sdk-metadata-boundary\n").hexdigest())
            self.assertNotIn(directory, json.dumps(report))

    def test_sdk_path_or_version_error_rejected_before_build(self):
        class RelativeSteps:
            def run(self, *args, **kwargs):
                return b"relative.sdk"
        with patch.object(module, "bounded_file", side_effect=AssertionError("metadata read before path guard")):
            with self.assertRaisesRegex(module.BuildError, "macos-sdk-path-invalid"):
                module.macos_sdk_environment({}, RelativeSteps())
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory) / "MacOSX26.6.sdk"
            (sdk / "usr/lib").mkdir(parents=True)
            (sdk / "SDKSettings.json").write_bytes(b'{"Version":"26.5"}')
            (sdk / "usr/lib/libSystem.tbd").write_bytes(b"offline-sdk-metadata-boundary\n")
            class VersionSteps:
                def run(self, name, *args, **kwargs):
                    return (str(sdk) if name == "sdk-path" else "26.6").encode()
            with self.assertRaisesRegex(module.BuildError, "macos-sdk-version-mismatch"):
                module.macos_sdk_environment({}, VersionSteps())


if __name__ == "__main__":
    unittest.main(verbosity=2)
