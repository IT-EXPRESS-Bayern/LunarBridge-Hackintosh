# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Compare the Linux Arc 130V driver interface with the macOS Tahoe one.

Inputs are the hash-pinned upstream sources fetched by tools/fetch_references.py:
the Linux v6.17 Xe uAPI for Arc 130V and Apple's published xnu / IOGraphics /
IOPCIFamily headers for macOS 26, plus the public kext inventory that names
Apple's newest Intel graphics driver. The extraction is deliberately narrow and
fails loudly: an expected entity that is missing is an error, never a default.

With --library the compiled lb_arc_interface table is read through ctypes and
verified against the extracted upstream facts, so the driver's own claims about
ioctl numbers, directions and Linux constructs cannot drift from the sources.
Nothing here inspects a running system, a GPU or any Apple binary.
"""
from __future__ import annotations

import argparse
import ctypes as c
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
REFERENCES = ROOT / 'tests/.references'
MANIFEST = ROOT / 'tests/reference-manifest.json'

LINUX_UAPI = 'linux-v6.17/include/uapi/drm/xe_drm.h'
LINUX_DRM = 'linux-v6.17/include/uapi/drm/drm.h'
LINUX_IOCTL_TABLE = 'linux-v6.17/drivers/gpu/drm/xe/xe_device.c'
XNU_USER_CLIENT = 'macos-tahoe/xnu-12377.121.6/iokit/IOKit/IOUserClient.h'
XNU_DMA_COMMAND = 'macos-tahoe/xnu-12377.121.6/iokit/IOKit/IODMACommand.h'
IOACCEL_CLIENT = 'macos-tahoe/IOGraphics-600/IOGraphicsFamily/IOKit/graphics/IOAccelClientConnect.h'
IOACCEL_SURFACE = 'macos-tahoe/IOGraphics-600/IOGraphicsFamily/IOKit/graphics/IOAccelSurfaceConnect.h'
IOPCI_DEVICE = 'macos-tahoe/IOPCIFamily-726.100.6/IOKit/pci/IOPCIDevice.h'
APPLE_KEXTS = 'apple-driver-surface/WhateverGreen-093e76f/WhateverGreen/kern_igfx_kexts.cpp'

# Linux construct bits; identical to LBArcLinuxConstruct in lb_arc_interface.h.
EXTENSION_CHAIN, USER_POINTER, VECTOR_OF_OPS, SYNC_ARRAY, HANDLE_ID, \
    MMAP_OFFSET, USER_FENCE_ADDR, INFINITE_WAIT = (1 << bit for bit in range(8))
CONSTRUCT_NAMES = {
    EXTENSION_CHAIN: 'extension_chain', USER_POINTER: 'user_pointer',
    VECTOR_OF_OPS: 'vector_of_ops', SYNC_ARRAY: 'sync_array', HANDLE_ID: 'handle_id',
    MMAP_OFFSET: 'mmap_offset', USER_FENCE_ADDR: 'user_fence_address',
    INFINITE_WAIT: 'infinite_wait',
}
# Field names the pinned uAPI documents as user pointers.
POINTER_FIELDS = {'data', 'syncs', 'instances', 'vector_of_binds', 'userptr'}
HANDLE_FIELDS = {'handle', 'vm_id', 'exec_queue_id'}
DIRECTIONS = {'DRM_IOW': 1, 'DRM_IOR': 2, 'DRM_IOWR': 3}
# Operation names of lb_arc_interface.h in table order, with their Xe ioctl.
MAPPED_OPERATIONS = [
    ('LBArcOpDeviceQuery', 'DEVICE_QUERY'), ('LBArcOpResourceCreate', 'GEM_CREATE'),
    ('LBArcOpResourceMap', 'GEM_MMAP_OFFSET'), ('LBArcOpVmCreate', 'VM_CREATE'),
    ('LBArcOpVmDestroy', 'VM_DESTROY'), ('LBArcOpVmBind', 'VM_BIND'),
    ('LBArcOpQueueCreate', 'EXEC_QUEUE_CREATE'), ('LBArcOpQueueDestroy', 'EXEC_QUEUE_DESTROY'),
    ('LBArcOpQueueProperty', 'EXEC_QUEUE_GET_PROPERTY'), ('LBArcOpExec', 'EXEC'),
    ('LBArcOpFenceWait', 'WAIT_USER_FENCE'),
]
OPERATION_COUNT = len(MAPPED_OPERATIONS)
COMMENT = re.compile(r'/\*.*?\*/|//[^\n]*', re.S)
# Difference bits of lb_arc_interface.h that answer exactly one Linux
# construct. Each pair must hold in both directions: a difference claimed
# without its construct overstates the port, a construct without its difference
# means the macOS interface silently inherited something it cannot carry.
# Differences outside this map (fixed structure size, owner-bound tokens, no
# render node, entitlement gate, explicit PAT, async reference) follow from the
# macOS call shape or from this project's own policy, not from one field.
CONSTRUCT_DIFFERENCE_PAIRS = (
    ('LBArcDiffNoUserPointer', 1 << 2, USER_POINTER),
    ('LBArcDiffFlattenedVector', 1 << 3, VECTOR_OF_OPS),
    ('LBArcDiffNoSyncObject', 1 << 4, SYNC_ARRAY),
    ('LBArcDiffNoMmapOffset', 1 << 5, MMAP_OFFSET),
    ('LBArcDiffBoundedTimeout', 1 << 9, INFINITE_WAIT),
)
# Differences every operation must carry; they come from the call shape itself.
BASE_DIFFERENCES = (1 << 0) | (1 << 1) | (1 << 6) | (1 << 10) | (1 << 11)


class ExtractionError(RuntimeError):
    """A pinned source did not contain an entity this comparison requires."""


def read_reference(relative: str) -> str:
    """Read one pinned reference and verify it against the manifest hash."""
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    entries = {entry['path']: entry for entry in manifest['files']}
    if relative not in entries:
        raise ExtractionError('Reference not pinned in the manifest: ' + relative)
    path = REFERENCES / relative
    if not path.is_file():
        raise ExtractionError('Missing reference ' + relative +
                              '; run tools/fetch_references.py first')
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != entries[relative]['sha256']:
        raise ExtractionError('SHA256 mismatch for pinned reference ' + relative)
    return data.decode('utf-8', 'strict')


def struct_body(source: str, name: str) -> str:
    match = re.search(r'^struct\s+' + re.escape(name) + r'\s*\{(.*?)^\};', source,
                      re.S | re.M)
    if not match:
        raise ExtractionError('Structure not found: ' + name)
    return COMMENT.sub('', match.group(1))


def struct_fields(body: str) -> list[dict]:
    fields = []
    for declaration in re.finditer(
            r'(__u8|__u16|__u32|__u64|__s32|__s64|struct\s+\w+)\s+(\w+)\s*(\[\s*(\d+)\s*\])?\s*;',
            body):
        kind, field, _, count = declaration.groups()
        fields.append({'type': ' '.join(kind.split()), 'name': field,
                       'count': int(count) if count else 1})
    if not fields:
        raise ExtractionError('Structure body without fields')
    return fields


def constructs_of(name: str, fields: list[dict]) -> int:
    names = {field['name'] for field in fields}
    constructs = 0
    if 'extensions' in names:
        constructs |= EXTENSION_CHAIN
    if names & POINTER_FIELDS:
        constructs |= USER_POINTER
    if 'vector_of_binds' in names:
        constructs |= VECTOR_OF_OPS
    if 'syncs' in names:
        constructs |= SYNC_ARRAY
    if names & HANDLE_FIELDS:
        constructs |= HANDLE_ID
    if name.endswith('gem_mmap_offset') and 'offset' in names:
        constructs |= MMAP_OFFSET
    if name.endswith('wait_user_fence') and {'addr', 'mask'} <= names:
        constructs |= USER_FENCE_ADDR
    if any(field['type'] == '__s64' and field['name'] == 'timeout' for field in fields):
        constructs |= INFINITE_WAIT
    return constructs


def extract_linux() -> dict:
    uapi = read_reference(LINUX_UAPI)
    drm = read_reference(LINUX_DRM)
    table = read_reference(LINUX_IOCTL_TABLE)
    base_match = re.search(r'#define\s+DRM_COMMAND_BASE\s+(0x[0-9a-fA-F]+)', drm)
    if not base_match:
        raise ExtractionError('DRM_COMMAND_BASE not found in the pinned drm.h')
    base = int(base_match.group(1), 16)
    offsets = {name: int(value, 16) for name, value in
               re.findall(r'#define\s+DRM_XE_([A-Z_0-9]+)\s+(0x0[0-9a-f])\s*$', uapi, re.M)}
    ioctls = {}
    for name, direction, structure in re.findall(
            r'#define\s+DRM_IOCTL_XE_([A-Z_0-9]+)\s+(DRM_IOW|DRM_IOR|DRM_IOWR)'
            r'\(DRM_COMMAND_BASE \+ DRM_XE_[A-Z_0-9]+,\s*struct\s+(\w+)\)', uapi):
        if name not in offsets:
            raise ExtractionError('ioctl without an offset define: ' + name)
        fields = struct_fields(struct_body(uapi, structure))
        constructs = constructs_of(structure, fields)
        ioctls[name] = {
            'offset': offsets[name], 'ioctl_nr': base + offsets[name],
            'direction': DIRECTIONS[direction], 'direction_macro': direction,
            'structure': structure, 'field_count': len(fields),
            'fields': [field['name'] for field in fields],
            'constructs': constructs,
            'construct_names': sorted(CONSTRUCT_NAMES[bit] for bit in CONSTRUCT_NAMES
                                      if constructs & bit),
        }
    if not ioctls:
        raise ExtractionError('No Xe ioctl definitions extracted')
    # The driver-side table confirms that every uAPI ioctl is actually served,
    # and records the permission model each entry uses.
    served = dict(re.findall(r'DRM_IOCTL_DEF_DRV\(XE_([A-Z_0-9]+),\s*\w+,\s*(DRM_\w+)\)',
                             COMMENT.sub('', table).replace('\n', ' ')))
    missing = sorted(set(ioctls) - set(served))
    if missing:
        raise ExtractionError('uAPI ioctls absent from the driver table: ' + ', '.join(missing))
    permissions = sorted(set(served.values()))
    sync_types = dict(re.findall(r'#define\s+DRM_XE_SYNC_TYPE_(\w+)\s+(0x\d)', uapi))
    bind_ops = dict(re.findall(r'#define\s+DRM_XE_VM_BIND_OP_(\w+)\s+(0x\d)', uapi))
    bind_op_fields = struct_fields(struct_body(uapi, 'drm_xe_vm_bind_op'))
    return {
        'pin': 'torvalds/linux e5f0a698b34ed76002dc5cff3804a61c80233a7a (v6.17)',
        'drm_command_base': base,
        'ioctl_count': len(ioctls),
        'served_by_driver_table': len(served),
        'permission_model': permissions,
        'ioctls': ioctls,
        'sync_types': sync_types,
        'bind_operations': bind_ops,
        'bind_op_fields': [field['name'] for field in bind_op_fields],
        'bind_op_pat_index': any(field['name'] == 'pat_index' for field in bind_op_fields),
    }


def extract_macos() -> dict:
    user_client = read_reference(XNU_USER_CLIENT)
    dma_command = read_reference(XNU_DMA_COMMAND)
    accel_client = read_reference(IOACCEL_CLIENT)
    accel_surface = read_reference(IOACCEL_SURFACE)
    pci_device = read_reference(IOPCI_DEVICE)

    def one(pattern: str, source: str, label: str) -> str:
        match = re.search(pattern, source, re.S)
        if not match:
            raise ExtractionError('Not found in the pinned Apple headers: ' + label)
        return match.group(1)

    arguments_body = COMMENT.sub('', one(r'struct IOExternalMethodArguments\s*\{(.*?)\};',
                                         user_client, 'IOExternalMethodArguments'))
    argument_fields = re.findall(r'(\w+)\s*\**\s*(\w+)\s*(?:\[\s*\d+\s*\])?\s*;', arguments_body)
    dispatch_body = COMMENT.sub('', one(r'struct IOExternalMethodDispatch\s*\{(.*?)\};',
                                        user_client, 'IOExternalMethodDispatch'))
    dispatch_2022 = COMMENT.sub('', one(r'struct IOExternalMethodDispatch2022\s*\{(.*?)\};',
                                        user_client, 'IOExternalMethodDispatch2022'))
    scalar_in = int(one(r'kIOExternalMethodScalarInputCountMax\s*=\s*(\d+)', user_client,
                        'kIOExternalMethodScalarInputCountMax'))
    scalar_out = int(one(r'kIOExternalMethodScalarOutputCountMax\s*=\s*(\d+)', user_client,
                         'kIOExternalMethodScalarOutputCountMax'))
    variable_size = int(one(r'kIOUCVariableStructureSize\s*=\s*(0x[0-9a-fA-F]+)', user_client,
                            'kIOUCVariableStructureSize'), 16)
    surface_methods = COMMENT.sub('', one(r'enum eIOAccelSurfaceMethods\s*\{(.*?)\};',
                                          accel_surface, 'eIOAccelSurfaceMethods'))
    client_types = COMMENT.sub('', one(r'enum eIOAcceleratorClientTypes\s*\{(.*?)\};',
                                       accel_client, 'eIOAcceleratorClientTypes'))
    return {
        'pins': {
            'xnu': 'apple-oss-distributions/xnu xnu-12377.121.6',
            'iographics': 'apple-oss-distributions/IOGraphics IOGraphics-600',
            'iopcifamily': 'apple-oss-distributions/IOPCIFamily IOPCIFamily-726.100.6',
        },
        'external_method_arguments_fields': [name for _, name in argument_fields],
        'has_structure_input_descriptor': 'structureInputDescriptor' in arguments_body,
        'has_async_reference': 'asyncReference' in arguments_body,
        'dispatch_check_fields': re.findall(r'(check\w+)', dispatch_body),
        'dispatch_2022_extra_fields': [field for field in ('allowAsync', 'checkEntitlement')
                                       if field in dispatch_2022],
        'scalar_input_count_max': scalar_in,
        'scalar_output_count_max': scalar_out,
        'variable_structure_size': variable_size,
        'accelerator_client_types': [entry.strip().rstrip(',') for entry in
                                     client_types.strip().split('\n') if entry.strip()],
        'accelerator_surface_methods': surface_method_names(surface_methods),
        'accelerator_surface_method_count': len(surface_method_names(surface_methods)),
        'accelerator_surface_method_terminator': surface_method_terminator(surface_methods),
        'highest_public_surface_selector': len(surface_method_names(surface_methods)) - 1,
        'public_accelerator_interface': 'IOAccelSurface locking, shaping and flushing only; '
                                        'no public command-submission or page-table interface',
        'dma_command_segment_api': 'gen64IOVMSegments' in dma_command,
        'pci_memory_mapping_api': 'mapDeviceMemoryWithRegister' in pci_device,
    }


def surface_method_entries(body: str) -> list[str]:
    cleaned = COMMENT.sub('', body)
    return [entry.strip().rstrip(',').strip()
            for entry in re.split(r'[,\n]', cleaned) if entry.strip()]


def surface_method_terminator(body: str) -> str | None:
    """The trailing `kIOAccelNum...` member, which is the count and not a method."""
    entries = surface_method_entries(body)
    last = entries[-1] if entries else ''
    return last if re.match(r'kIOAccelNum\w+', last) else None


def surface_method_names(body: str) -> list[str]:
    """The methods themselves: every member except a trailing count terminator."""
    entries = surface_method_entries(body)
    if entries and surface_method_terminator(body):
        return entries[:-1]
    return entries


def extract_apple_driver_surface() -> dict:
    inventory = read_reference(APPLE_KEXTS)
    bundles = re.findall(r'KernelPatcher::KextInfo\s+kextIntel(\w+)\s*\{\s*"([\w.]+)"', inventory)
    paths = dict(re.findall(r'pathIntel(\w+)\[\]\s*\{\s*"([^"]+)"', inventory))
    if not bundles:
        raise ExtractionError('No Apple Intel graphics kext identifiers extracted')
    newest = [name for name, _ in bundles if name.startswith('ICL')]
    if not newest:
        raise ExtractionError('Expected ICL entries in the Apple Intel kext inventory')
    return {
        'pin': 'acidanthera/WhateverGreen 093e76fec5655001f38b367dd22459c756ee89fa',
        'kext_count': len(bundles),
        'bundle_identifiers': [bundle for _, bundle in bundles],
        'newest_generation': 'ICL (Gen11)',
        'newest_accelerator_bundle': 'com.apple.driver.AppleIntelICLGraphics',
        'newest_framebuffer_bundles': [bundle for name, bundle in bundles
                                       if name.startswith('ICL') and 'Fb' in name],
        'newest_paths': {name: path for name, path in paths.items() if name.startswith('ICL')},
        'xe_or_xe2_driver_present': any('TGL' in name or 'ADL' in name or 'Xe' in name
                                        or 'LNL' in name for name, _ in bundles),
        'note': 'Public inventory of Apple-shipped Intel graphics kexts. The newest family is '
                'Ice Lake / Gen11; no Apple driver exists for Gen12, Xe or Xe2. Apple binaries '
                'are neither read nor redistributed here.',
    }


class Descriptor(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in (
        'op', 'selector', 'scalarInputCount', 'scalarOutputCount', 'structInputBytes',
        'structOutputBytes', 'differences', 'requiredQualifications', 'linuxIoctlNr',
        'linuxConstructs', 'linuxDirection', 'reserved')]


class Report(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in (
        'abi', 'structBytes', 'opCount', 'differences', 'linuxUapiVersion',
        'darwinKernelMajor', 'scalarLimit', 'maxSegments', 'maxSyncs',
        'unmappedLinuxIoctls', 'gpuExecutionImplemented', 'metalProviderImplemented')] + [
        ('ops', Descriptor * OPERATION_COUNT)]


def verify_driver_table(library: Path, linux: dict, macos: dict) -> dict:
    """Read the compiled interface table and check it against the sources."""
    binary = library.resolve(strict=True)
    module = c.CDLL(str(binary))
    module.lb_arc_interface_describe.argtypes = [c.POINTER(Report), c.c_uint32]
    module.lb_arc_interface_describe.restype = c.c_uint32
    report = Report()
    status = module.lb_arc_interface_describe(c.byref(report), c.sizeof(Report))
    if status:
        raise ExtractionError('lb_arc_interface_describe refused the report buffer')
    mismatches = []
    rows = []
    if report.opCount != OPERATION_COUNT:
        mismatches.append({'operation': '*', 'field': 'opCount',
                           'driver': report.opCount, 'source': OPERATION_COUNT})
    if report.scalarLimit != macos['scalar_input_count_max']:
        mismatches.append({'operation': '*', 'field': 'scalarLimit',
                           'driver': report.scalarLimit,
                           'source': macos['scalar_input_count_max']})
    unmapped = sorted(set(linux['ioctls']) - {name for _, name in MAPPED_OPERATIONS})
    if report.unmappedLinuxIoctls != len(unmapped):
        mismatches.append({'operation': '*', 'field': 'unmappedLinuxIoctls',
                           'driver': report.unmappedLinuxIoctls, 'source': len(unmapped)})
    for index, (operation, ioctl) in enumerate(MAPPED_OPERATIONS):
        row = report.ops[index]
        source = linux['ioctls'][ioctl]
        for field, driver_value, source_value in (
                ('op', row.op, index + 1),
                ('linuxIoctlNr', row.linuxIoctlNr, source['ioctl_nr']),
                ('linuxDirection', row.linuxDirection, source['direction']),
                ('linuxConstructs', row.linuxConstructs, source['constructs']),
                ('baseDifferences', row.differences & BASE_DIFFERENCES, BASE_DIFFERENCES)):
            if driver_value != source_value:
                mismatches.append({'operation': operation, 'field': field,
                                   'driver': driver_value, 'source': source_value})
        for name, difference, construct in CONSTRUCT_DIFFERENCE_PAIRS:
            claimed = bool(row.differences & difference)
            present = bool(source['constructs'] & construct)
            if claimed != present:
                mismatches.append({'operation': operation, 'field': name,
                                   'driver': claimed, 'source': present})
        rows.append({
            'operation': operation, 'linux_ioctl': 'DRM_IOCTL_XE_' + ioctl,
            'linux_structure': source['structure'], 'linux_ioctl_nr': source['ioctl_nr'],
            'linux_direction_macro': source['direction_macro'],
            'linux_constructs': source['construct_names'],
            'macos_selector': row.selector,
            'macos_struct_input_bytes': row.structInputBytes,
            'macos_struct_output_bytes': row.structOutputBytes,
            'differences': row.differences,
            'required_qualifications': row.requiredQualifications,
        })
    return {
        'library': str(binary),
        'library_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'interface_abi': report.abi,
        'report_bytes': report.structBytes,
        'operation_count': report.opCount,
        'union_of_differences': report.differences,
        'linux_uapi_version': f'{report.linuxUapiVersion >> 16}.{report.linuxUapiVersion & 0xFFFF}',
        'darwin_kernel_major': report.darwinKernelMajor,
        'unmapped_linux_ioctls': unmapped,
        'gpu_execution_implemented': bool(report.gpuExecutionImplemented),
        'metal_provider_implemented': bool(report.metalProviderImplemented),
        'operations': rows,
        'mismatches': mismatches,
    }


def mapping_from_sources(linux: dict) -> dict:
    """The operation mapping as it follows from the pinned sources alone."""
    rows = []
    for index, (operation, ioctl) in enumerate(MAPPED_OPERATIONS):
        source = linux['ioctls'][ioctl]
        rows.append({
            'operation': operation, 'op_value': index + 1,
            'linux_ioctl': 'DRM_IOCTL_XE_' + ioctl,
            'linux_structure': source['structure'],
            'linux_ioctl_nr': source['ioctl_nr'],
            'linux_direction_macro': source['direction_macro'],
            'linux_field_count': source['field_count'],
            'linux_constructs': source['construct_names'],
        })
    return {
        'mapped': rows,
        'unmapped': sorted(set(linux['ioctls']) - {name for _, name in MAPPED_OPERATIONS}),
        'macos_carrier': 'One user-client selector with a fixed 480-byte request and an 80-byte '
                         'verdict; see docs/ARC_INTERFACE_DIFF.md.',
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--library', type=Path,
                        help='Compiled library exporting lb_arc_interface_describe')
    parser.add_argument('--out', type=Path, default=ROOT / 'docs/ARC_INTERFACE_DIFF.json')
    arguments = parser.parse_args()
    linux = extract_linux()
    macos = extract_macos()
    apple = extract_apple_driver_surface()
    record = {
        'generated_by': 'tools/compare_interfaces.py',
        'scope': 'Interface shapes only. No Apple or Intel driver code is copied, no system '
                 'is inspected, no GPU is used. Driver loading, GPU execution and Metal remain '
                 'unimplemented.',
        'linux_arc_130v_driver': linux,
        'macos_tahoe_driver_contract': macos,
        'apple_intel_driver_inventory': apple,
        'operation_mapping': mapping_from_sources(linux),
    }
    if arguments.library:
        record['driver_table_verification'] = verify_driver_table(arguments.library, linux, macos)
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(record, indent=2, sort_keys=False) + '\n',
                             encoding='utf-8')
    mismatches = record.get('driver_table_verification', {}).get('mismatches', [])
    print('Linux Xe ioctls extracted: ' + str(linux['ioctl_count']))
    print('Apple Intel graphics kexts in the public inventory: ' + str(apple['kext_count']))
    print('Wrote ' + str(arguments.out))
    if mismatches:
        for mismatch in mismatches:
            print('MISMATCH ' + json.dumps(mismatch), file=sys.stderr)
        return 1
    if arguments.library:
        print('Driver table verified against the pinned sources: no mismatches')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
