#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the pinned Mesa spirv2nir CPU parser; never call a GPU/Metal API."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse
import zipfile

MESA_PIN = "e24dc5bd1e7fe6101bdc866fb16a15a8fcae1aae"
SOURCE_REPOSITORY = "https://github.com/mirror/mesa"
MAX_BYTES = 32 * 1024 * 1024
PACKAGES = {"meson": "1.9.1", "ninja": "1.13.0", "Mako": "1.3.10",
            "MarkupSafe": "3.0.2", "packaging": "25.0", "PyYAML": "6.0.3"}
PURE_WHEEL_HASHES = {
    "meson": "f824ab770c041a202f532f69e114c971918ed2daff7ea56583d80642564598d0",
    "Mako": "baef24a52fc4fc514a0887ac600f9f1cff3d82c61d4d700a1fa84d597b88db59",
    "packaging": "29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484",
}
MAC_WHEEL_HASHES = {
    "ninja": "fa2a8bfc62e31b08f83127d1613d10821775a0eb334197154c4d6067b7068ff1",
    "MarkupSafe": "9778bd8ab0a994ebf6f84c2b949e65736d5575320a17ae8984a77fab08db94cf",
    "PyYAML": "7f047e29dcae44602496db43be01ad42fc6f1cc0d8cd6c83d342306c32270196",
}
MESON_OPTIONS = [
    # This upstream configuration selects real NIR; the only Ninja target is spirv2nir.
    "-Dplatforms=[]", "-Dgallium-drivers=softpipe", "-Dvulkan-drivers=[]", "-Dvulkan-layers=[]",
    "-Dopengl=true", "-Dgles1=disabled", "-Dgles2=disabled", "-Degl=disabled",
    "-Dgbm=disabled", "-Dglx=disabled", "-Dllvm=disabled", "-Dtools=nir",
    "-Dbuild-tests=false", "-Dshader-cache=disabled", "-Dzlib=disabled", "-Dzstd=disabled",
    "-Dspirv-tools=disabled", "-Dvalgrind=disabled", "-Dxmlconfig=disabled", "-Dexpat=disabled",
]


class BuildError(Exception):
    """Fixed, path-free error code suitable for the public report."""


def native_host() -> bool:
    return sys.platform == "darwin" and platform.machine().lower() == "x86_64"


def bounded_file(path: Path, limit: int = MAX_BYTES) -> bytes:
    if not stat.S_ISREG(path.stat().st_mode):
        raise BuildError("non-regular-file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise BuildError("file-type-or-size-rejected")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise BuildError("file-size-rejected")
        return data
    finally:
        os.close(fd)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def qualify_macho(data: bytes) -> dict:
    if len(data) < 32:
        raise BuildError("parser-header-truncated")
    magic, cpu, subtype, filetype, ncmds, command_bytes, flags, reserved = struct.unpack("<IiiIIIII", data[:32])
    if magic != 0xFEEDFACF or cpu != 0x01000007 or filetype != 2:
        raise BuildError("parser-is-not-x86_64-macho-executable")
    if not 1 <= ncmds <= 4096 or command_bytes > len(data) - 32:
        raise BuildError("parser-load-command-range-rejected")
    offset = 32
    end = offset + command_bytes
    for _ in range(ncmds):
        if offset + 8 > end:
            raise BuildError("parser-load-command-truncated")
        command, size = struct.unpack_from("<II", data, offset)
        if size < 8 or size % 4 or size > end - offset:
            raise BuildError("parser-load-command-size-rejected")
        offset += size
    if offset != end:
        raise BuildError("parser-load-command-count-mismatch")
    return {"format": "Mach-O64", "architecture": "x86_64", "file_type": "MH_EXECUTE",
            "bytes": len(data), "sha256": sha256(data)}


def select_wheel(metadata: dict, name: str, version: str) -> dict:
    if metadata.get("info", {}).get("version") != version:
        raise BuildError("package-version-mismatch")
    candidates = []
    for item in metadata.get("urls", []):
        filename = item.get("filename", "")
        if item.get("packagetype") != "bdist_wheel":
            continue
        pure = filename.endswith("-none-any.whl")
        mac = "macosx_" in filename and filename.endswith(("_x86_64.whl", "_universal2.whl"))
        compatible = "cp312-cp312" in filename or "py3-none" in filename
        if pure or mac and compatible:
            candidates.append(item)
    # Prefer a thin Intel wheel when the publisher also provides universal2.
    intel = [item for item in candidates if item["filename"].endswith("_x86_64.whl")]
    if intel:
        candidates = intel
    if len(candidates) != 1:
        raise BuildError("package-wheel-selection-ambiguous")
    item = candidates[0]
    url = urlparse(item["url"])
    digest = item.get("digests", {}).get("sha256", "")
    if url.scheme != "https" or url.hostname != "files.pythonhosted.org" or not re.fullmatch("[0-9a-f]{64}", digest):
        raise BuildError("package-url-or-digest-rejected")
    expected = PURE_WHEEL_HASHES.get(name) or MAC_WHEEL_HASHES.get(name)
    if expected is None or digest != expected:
        raise BuildError("known-wheel-digest-mismatch")
    if type(item.get("size")) is not int or not 0 < item["size"] <= MAX_BYTES:
        raise BuildError("package-size-rejected")
    if Path(item["filename"]).name != item["filename"] or "\\" in item["filename"]:
        raise BuildError("package-filename-rejected")
    return item


def sanitize(text: str, prefixes: list[Path]) -> str:
    for prefix in sorted({str(path.resolve()) for path in prefixes}, key=len, reverse=True):
        text = text.replace(prefix, "<private>").replace(prefix.replace("\\", "/"), "<private>")
    return re.sub(r"(?:/Users/|/home/|[A-Za-z]:[\\/]Users[\\/])[^\s\"']+", "<private>", text)[-6000:]


class Steps:
    def __init__(self, root: Path, prefixes: list[Path]):
        self.root = root
        self.prefixes = prefixes
        self.records: list[dict] = []

    def run(self, name: str, argv: list[str], timeout: float, *, cwd: Path | None = None,
            env: dict | None = None) -> bytes:
        if not re.fullmatch("[a-z0-9-]+", name) or not 0 < timeout <= 900:
            raise BuildError("step-name-or-deadline-rejected")
        begin = time.monotonic()
        destination = self.root / (name + ".log")
        record = {"name": name, "timeout_seconds": timeout, "timed_out": False,
                  "cleanup": "not-needed", "returncode": None}
        self.records.append(record)
        with destination.open("xb") as log:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=os.name != "nt",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                record["timed_out"] = True
                try:
                    if os.name == "nt":
                        killed = subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                                                stdout=log, stderr=log, timeout=5,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
                        record["cleanup"] = "tree-kill-return-" + str(killed.returncode)
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                        record["cleanup"] = "group-kill-requested"
                except (OSError, subprocess.TimeoutExpired):
                    record["cleanup"] = "group-cleanup-unverified"
                    proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    record["cleanup"] = "parent-wait-unverified"
            record["returncode"] = proc.poll()
        record["elapsed_seconds"] = round(time.monotonic() - begin, 3)
        output = bounded_file(destination, 16 * 1024 * 1024)
        if record["timed_out"] or proc.returncode != 0:
            record["diagnostic"] = sanitize(output.decode("utf-8", errors="replace"), self.prefixes)
            raise BuildError(name + "-failed")
        return output


def verify_source(source: Path, steps: Steps) -> dict:
    pin = steps.run("source-commit", ["git", "-C", str(source), "rev-parse", "HEAD"], 15).decode().strip()
    if pin != MESA_PIN:
        raise BuildError("source-commit-mismatch")
    tree = steps.run("source-tree", ["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], 15).decode().strip()
    if not re.fullmatch("[0-9a-f]{40}", tree):
        raise BuildError("source-tree-invalid")
    dirty = steps.run("source-clean", ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"], 30)
    if dirty.strip():
        raise BuildError("source-checkout-not-clean")
    version = bounded_file(source / "VERSION", 128).decode("ascii").strip()
    if version != "26.2.0-devel":
        raise BuildError("source-version-mismatch")
    return {"repository": SOURCE_REPOSITORY, "commit": pin, "tree": tree,
            "version": version, "upstream_signature_verified": False}


def install_packages(root: Path, steps: Steps) -> tuple[dict, list[dict]]:
    wheels = root / "wheels"
    wheels.mkdir()
    installed = root / "python-packages"
    records = []
    deadline = time.monotonic() + 300
    for name, version in PACKAGES.items():
        remaining = min(60, deadline - time.monotonic())
        if remaining <= 0:
            raise BuildError("package-phase-deadline")
        metadata_path = wheels / (name + ".json")
        steps.run("metadata-" + name.lower(), ["curl", "--silent", "--show-error", "--fail",
                  "--location", "--connect-timeout", "10", "--max-time", str(max(1, int(remaining))),
                  "--output", str(metadata_path), f"https://pypi.org/pypi/{name}/{version}/json"], remaining)
        metadata = json.loads(bounded_file(metadata_path, 4 * 1024 * 1024))
        item = select_wheel(metadata, name, version)
        remaining = min(60, deadline - time.monotonic())
        if remaining <= 0:
            raise BuildError("package-phase-deadline")
        target = wheels / item["filename"]
        steps.run("download-" + name.lower(), ["curl", "--silent", "--show-error", "--fail", "--location",
                  "--connect-timeout", "10", "--max-time", str(max(1, int(remaining))),
                  "--output", str(target), item["url"]], remaining)
        data = bounded_file(target)
        if len(data) != item["size"] or sha256(data) != item["digests"]["sha256"]:
            raise BuildError("package-byte-digest-mismatch")
        licenses = []
        with zipfile.ZipFile(target) as archive:
            members = archive.infolist()
            if (len(members) > 5000 or sum(member.file_size for member in members) > 128 * 1024 * 1024
                    or len({member.filename for member in members}) != len(members)):
                raise BuildError("package-expanded-bound-rejected")
            for member in members:
                if (member.filename.startswith(("/", "\\")) or "\\" in member.filename
                        or ".." in Path(member.filename).parts or ":" in member.filename):
                    raise BuildError("package-archive-path-rejected")
            if archive.testzip() is not None:
                raise BuildError("package-crc-mismatch")
            for member in archive.infolist():
                if ".dist-info/" in member.filename and any(word in member.filename.upper() for word in ("LICENSE", "COPYING")):
                    if member.file_size > 128 * 1024:
                        raise BuildError("package-license-size-rejected")
                    license_data = archive.read(member)
                    license_name = name + "-" + sha256(member.filename.encode())[:12] + "-" + Path(member.filename).name
                    (root.parent / "LICENSES" / license_name).write_bytes(license_data)
                    licenses.append({"file": license_name, "sha256": sha256(license_data)})
        records.append({"name": name, "version": version, "filename": item["filename"],
                        "sha256": sha256(data), "bytes": len(data), "licenses": licenses})
    steps.run("install-packages", [sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check",
              "--no-cache-dir", "install", "--no-index",
              "--no-deps", "--find-links", str(wheels), "--target", str(installed)] +
              [name + "==" + version for name, version in PACKAGES.items()], 60)
    ninja_candidates = [installed / "bin/ninja", installed / "ninja/data/bin/ninja"]
    ninja = next((path for path in ninja_candidates if path.is_file()), None)
    if ninja is None:
        raise BuildError("installed-ninja-not-found")
    environment = os.environ.copy()
    environment.update({"PYTHONPATH": str(installed), "PYTHONNOUSERSITE": "1", "NINJA": str(ninja),
                        "PYTHONDONTWRITEBYTECODE": "1"})
    environment.pop("MESA_SPIRV_READ_PATH", None)
    environment.pop("INTEL_JAY", None)
    return environment, records


def qualify_parser_inputs(names: list[str]) -> dict:
    if "upstream/src/compiler/nir/nir.c" not in names:
        raise BuildError("compiled-real-nir-core-missing")
    if any(name.endswith("/nir_stub.c") for name in names):
        raise BuildError("compiled-nir-stub-rejected")
    if any("/gallium/drivers/" in name for name in names):
        raise BuildError("compiled-gallium-driver-rejected")
    return {"real_nir_core_verified": True, "nir_stub_present": False,
            "gallium_driver_inputs_present": False, "graphics_api_executed": False}


def source_reference(source: Path, build: Path, environment: dict, steps: Steps, reference: dict) -> dict:
    database = json.loads(steps.run("compiled-database", [environment["NINJA"], "-C", str(build),
                                   "-t", "compdb-targets", "src/compiler/spirv/spirv2nir"], 30, env=environment))
    tree_data = steps.run("source-blob-list", ["git", "-C", str(source), "ls-tree", "-r", "-z", "HEAD"], 30)
    blobs = {}
    for entry in tree_data.split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            mode, kind, digest = meta.decode("ascii").split()
            if kind == "blob":
                blobs[path.decode("utf-8")] = digest
    files = {}
    license_headers = []
    for entry in database:
        if Path(entry["file"]).suffix.lower() not in (".c", ".cc", ".cpp", ".cxx", ".m", ".mm", ".s"):
            continue
        path = (Path(entry["directory"]) / entry["file"]).resolve()
        if not path.is_relative_to(source) and not path.is_relative_to(build):
            raise BuildError("compiled-source-outside-qualified-roots")
        data = bounded_file(path)
        if path.is_relative_to(source):
            relative = path.relative_to(source).as_posix()
            blob = hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()
            if blob != blobs.get(relative):
                raise BuildError("compiled-source-blob-mismatch")
            label = "upstream/" + relative
            # Preserve original leading source copyright/license blocks as well as Mesa's license document.
            decoded = data[:16384].decode("utf-8", errors="replace")
            blocks = re.findall(r"\A\s*((?:/\*.*?\*/\s*)+)", decoded, flags=re.S)
            if blocks:
                license_headers.append({"source": relative, "leading_header": blocks[0]})
        elif path.is_relative_to(build):
            label = "generated/" + path.relative_to(build).as_posix()
        else:
            raise BuildError("compiled-source-outside-qualified-roots")
        files[label] = {"path": label, "bytes": len(data), "sha256": sha256(data)}
    if not files or "upstream/src/compiler/spirv/spirv2nir.c" not in files:
        raise BuildError("compiled-source-database-incomplete")
    compiler_scope = qualify_parser_inputs(list(files))
    (steps.root.parent / "LICENSES/MESA_COMPILED_HEADERS.json").write_text(json.dumps(license_headers, indent=2) + "\n")
    for name in ("LICENSE", "COPYING", "docs/license.rst"):
        path = source / name
        if path.is_file():
            (steps.root.parent / "LICENSES" / ("Mesa-" + path.name)).write_bytes(bounded_file(path, 1024 * 1024))
    license_dir = source / "licenses"
    if license_dir.is_dir():
        for path in sorted(license_dir.rglob("*")):
            if path.is_file():
                resolved = path.resolve()
                if not resolved.is_relative_to(source):
                    raise BuildError("license-source-outside-qualified-root")
                destination = steps.root.parent / "LICENSES/Mesa-original" / path.relative_to(license_dir)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(bounded_file(resolved, 1024 * 1024))
    return {**reference, "schema_version": 1, "kind": "actual-cpu-parser-compiled-source-reference",
            "compiled_inputs": sorted(files.values(), key=lambda item: item["path"]),
            "compiled_scope": compiler_scope,
            "compiled_input_count": len(files), "compiler": "Apple Clang on native Intel macOS CPU host",
            "harness_source_sha256": sha256(bounded_file(Path(__file__))),
            "license_assets": [{"path": path.relative_to(steps.root.parent).as_posix(),
                                "sha256": sha256(bounded_file(path))}
                               for path in sorted((steps.root.parent / "LICENSES").rglob("*")) if path.is_file()],
            "isa_codegen_executed": False, "shader_parser_executed": False,
            "target_gpu_executed": False}


OWN_SOURCE_PATHS = ("tools/mesa_cpu_ci.py", "tests/test_mesa_cpu.py", ".github/workflows/mesa-cpu.yml")


def macos_sdk_environment(environment: dict, steps: Steps) -> tuple[dict, dict]:
    raw = steps.run("sdk-path", ["xcrun", "--sdk", "macosx", "--show-sdk-path"], 30).decode("utf-8").strip()
    if not raw or any(char in raw for char in ("\n", "\r", "\0")) or not Path(raw).is_absolute():
        raise BuildError("macos-sdk-path-invalid")
    sdk = Path(raw).resolve(strict=True)
    if not sdk.is_dir() or sdk.suffix != ".sdk":
        raise BuildError("macos-sdk-directory-invalid")
    settings = bounded_file(sdk / "SDKSettings.json", 1024 * 1024)
    system_library = bounded_file(sdk / "usr/lib/libSystem.tbd", 4 * 1024 * 1024)
    if not system_library:
        raise BuildError("macos-sdk-system-library-empty")
    version = steps.run("sdk-version", ["xcrun", "--sdk", "macosx", "--show-sdk-version"], 30).decode("ascii").strip()
    metadata = json.loads(settings)
    if (re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", version) is None
            or not isinstance(metadata, dict) or metadata.get("Version") != version):
        raise BuildError("macos-sdk-version-mismatch")
    flags = "-isysroot " + shlex.quote(str(sdk)) + " -arch x86_64"
    qualified = dict(environment)
    qualified.update({"SDKROOT": str(sdk), "CFLAGS": flags, "CXXFLAGS": flags,
                      "OBJCFLAGS": flags, "LDFLAGS": flags})
    return qualified, {"name": sdk.name, "version": version, "architecture": "x86_64",
                       "settings_sha256": sha256(settings), "system_library_tbd_sha256": sha256(system_library),
                       "explicit_compile_and_link_sysroot": True}


def own_source_reference(root: Path, steps: Steps) -> dict:
    commit = os.environ.get("GITHUB_SHA", "")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise BuildError("own-source-github-sha-invalid")
    actual = steps.run("own-source-head", ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], 30).decode("ascii").strip()
    if actual != commit:
        raise BuildError("own-source-commit-mismatch")
    files = []
    for name in OWN_SOURCE_PATHS:
        data = bounded_file(root / name)
        if not data:
            raise BuildError("own-source-file-empty")
        files.append({"path": name, "bytes": len(data), "sha256": sha256(data)})
    return {"own_source_commit": commit, "own_source_commit_reference": "GITHUB_SHA",
            "own_source_files": files}


def qualification_exit_code(report: dict, cli_exit: int) -> int:
    if (type(cli_exit) is not int or cli_exit != 0 or not isinstance(report, dict)
            or report.get("status") != "cpu-parser-qualified"
            or report.get("parser_built") is not True or report.get("parser_help_verified") is not True):
        return 2
    commit = report.get("own_source_commit")
    files = report.get("own_source_files")
    if (not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None
            or not isinstance(files, list) or len(files) != len(OWN_SOURCE_PATHS)):
        return 2
    names = []
    for entry in files:
        if (not isinstance(entry, dict) or entry.get("path") not in OWN_SOURCE_PATHS
                or type(entry.get("bytes")) is not int or entry["bytes"] <= 0
                or not isinstance(entry.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None):
            return 2
        names.append(entry["path"])
    return 0 if set(names) == set(OWN_SOURCE_PATHS) else 2


def write_report(output: Path, report: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output, delete=False) as temporary:
        json.dump(report, temporary, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, output / "report.json")


def main(argv: list[str] | None = None) -> int:
    # Enforce this before parsing paths or making any directory on unsupported hosts.
    if not native_host():
        print(json.dumps({"schema_version": 1, "status": "unsupported-platform", "writes_performed": False,
                          "parser_built": False, "shader_parser_executed": False, "gpu_executed": False}))
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", default="build/mesa-cpu-report")
    args = parser.parse_args(argv)
    workspace = Path.cwd().resolve()
    source = Path(args.source).resolve()
    output = Path(args.out).resolve()
    if (output == workspace or not output.is_relative_to(workspace / "build") or output.exists()
            or output.is_relative_to(source) or source.is_relative_to(output)):
        print("Mesa CPU output destination rejected.", file=sys.stderr)
        return 2
    report = {"schema_version": 1, "kind": "mesa-spirv2nir-cpu-parser-qualification",
              "context": "intel-macos-ci-host" if os.environ.get("GITHUB_ACTIONS") == "true" else "local-intel-macos-cpu-host",
              "status": "unverified", "phase": "prepare",
              "build_configuration": {"target": "src/compiler/spirv/spirv2nir", "meson_options": MESON_OPTIONS,
                                      "upstream_sources_modified": False,
                                      "softpipe_selected_to_enable_real_nir_only": True},
              "source_pin": MESA_PIN, "parser_built": False, "parser_help_verified": False,
              "shader_parser_executed": False, "isa_codegen_executed": False,
              "metal_device_registered": False, "target_gpu_executed": False}
    steps = None
    try:
        output.mkdir(parents=True, exist_ok=False)
        private = output / "private"
        private.mkdir()
        (output / "LICENSES").mkdir()
        steps = Steps(private, [workspace, source, output, Path(sys.prefix), Path.home(), Path(tempfile.gettempdir())])
        report["phase"] = "own-source"
        own_reference = own_source_reference(Path(__file__).resolve().parents[1], steps)
        report.update(own_reference)
        report["steps"] = steps.records
        # Persist the own-source binding before any dependency download or build can begin.
        write_report(output, report)
        report["phase"] = "source"
        reference = verify_source(source, steps)
        reference["context"] = report["context"]
        reference.update(own_reference)
        report["source"] = reference
        report["phase"] = "dependencies"
        environment, packages = install_packages(private, steps)
        report["packages"] = packages
        versions = {}
        versions["meson"] = steps.run("meson-version", [sys.executable, "-m", "mesonbuild.mesonmain", "--version"], 30, env=environment).decode().strip()
        versions["ninja"] = steps.run("ninja-version", [environment["NINJA"], "--version"], 30, env=environment).decode().strip()
        clang = steps.run("find-clang", ["xcrun", "--find", "clang"], 30).decode().strip()
        clang_cpp = steps.run("find-clang-cpp", ["xcrun", "--find", "clang++"], 30).decode().strip()
        environment.update({"CC": clang, "CXX": clang_cpp, "OBJC": clang})
        versions["clang"] = sanitize(steps.run("clang-version", [clang, "--version"], 30).decode(), steps.prefixes)
        report["tool_versions"] = versions
        report["phase"] = "sdk"
        environment, report["sdk"] = macos_sdk_environment(environment, steps)
        steps.prefixes.append(Path(environment["SDKROOT"]))
        build = private / "mesa-build"
        report["phase"] = "configure"
        steps.run("configure", [sys.executable, "-m", "mesonbuild.mesonmain", "setup", str(build), str(source),
                  "--wrap-mode=nodownload", "--buildtype=release"] + MESON_OPTIONS, 180, env=environment)
        report["phase"] = "build"
        steps.run("build-parser", [environment["NINJA"], "-C", str(build), "-j", "3",
                  "src/compiler/spirv/spirv2nir"], 900, env=environment)
        binary = build / "src/compiler/spirv/spirv2nir"
        binary_data = bounded_file(binary)
        report["binary"] = qualify_macho(binary_data)
        report["parser_built"] = True
        report["phase"] = "source-reference"
        compiled = source_reference(source, build, environment, steps, reference)
        (output / "SOURCE_REFERENCE.json").write_text(json.dumps(compiled, indent=2) + "\n")
        report["compiled_input_count"] = compiled["compiled_input_count"]
        report["compiled_scope"] = compiled["compiled_scope"]
        report["phase"] = "help"
        help_data = steps.run("parser-help", [str(binary), "--help"], 30, env=environment)
        if b"Usage:" not in help_data or b"--stage" not in help_data or b"--entry" not in help_data:
            raise BuildError("parser-help-schema-mismatch")
        report["parser_help_verified"] = True
        (output / "parser").mkdir()
        packaged = output / "parser/spirv2nir"
        packaged.write_bytes(binary_data)
        packaged.chmod(0o755)
        report.update({"phase": "complete", "status": "cpu-parser-qualified"})
    except (BuildError, OSError, ValueError, KeyError, TypeError, UnicodeError, zipfile.BadZipFile) as error:
        report["error"] = str(error) if isinstance(error, BuildError) else "controlled-build-or-data-error"
    if steps is None:
        print("Mesa CPU preparation failed.", file=sys.stderr)
        return 2
    if steps is not None:
        report["steps"] = steps.records
    try:
        write_report(output, report)
    except OSError:
        print("Mesa CPU report could not be written.", file=sys.stderr)
        return 2
    print(json.dumps(report))
    return qualification_exit_code(report, 0)


if __name__ == "__main__":
    raise SystemExit(main())
