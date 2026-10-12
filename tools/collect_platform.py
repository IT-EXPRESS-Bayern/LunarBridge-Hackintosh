# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Everything the target machine itself can tell us, from boot to acceleration.

Run on the macOS installation that holds the hardware. Read-only: it asks the
system questions with Apple's own tools and reduces the answers to facts. It
changes no setting, loads nothing, and writes only into the chosen output file.

    sudo python3 tools/collect_platform.py --out build/recon/platform.json

Root is not required for most of it; where it helps, the record says so. What
comes out answers, in one file, the questions an unfinished driver keeps asking:

    identity        which Mac this claims to be, and which macOS build
    security        what the current SIP word actually permits us to do
    boot            the boot-args and the injected device properties in force
    pci             the GPU as the device tree sees it: identity, BARs, driver
    graphics_stack  which graphics families are live, and which are missing
    loaded          the graphics drivers in the kernel right now
    collections     which graphics bundles each kernel collection carries
    displays        what the display pipeline reports
    capabilities    derived: what this machine will and will not let us try next

Binary properties are reduced, never reproduced: a value of eight bytes or less
becomes a number, anything longer becomes its length and SHA-256. That keeps
EDIDs, serials and injected blobs out of the record while still identifying them.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import platform
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSR_HEADER = 'macos-tahoe/xnu-12377.121.6/bsd/sys/csr.h'
# NVRAM variables worth recording. Everything else is skipped rather than
# filtered, because NVRAM also holds identifiers that are nobody's business.
NVRAM_KEYS = ('boot-args', 'csr-active-config', 'bootercfg', 'efi-boot-device-data',
              'SystemAudioVolume', 'prev-lang:kbd', 'nvda_drv')
NVRAM_SKIP_PATTERN = re.compile(r'serial|uuid|mlb|rom|address|key|token|secret', re.I)
# Property keys on a PCI node that identify the device or an injection.
PCI_IDENTITY = ('vendor-id', 'device-id', 'subsystem-id', 'subsystem-vendor-id',
                'revision-id', 'class-code', 'IOName', 'name', 'model', 'compatible',
                'acpi-path', 'pcidebug', 'built-in')
PCI_INJECTED = re.compile(r'^(AAPL|AAPL00|agdc|framebuffer|device_type|hda-gfx|'
                          r'enable-|disable-|force|igfx|rebuild)', re.I)
GRAPHICS_CLASSES = ('IOAccelerator', 'IOGPU', 'IOFramebuffer', 'IOSurfaceRoot',
                    'IOAccelerationUserClient', 'IOGraphicsDevice')
GRAPHICS_KEYWORDS = ('Graphics', 'IOGPU', 'IOAccelerator', 'IOSurface', 'AGX', 'AGDC',
                     'AppleGPU', 'GPUWrangler', 'MTL', 'Metal', 'LunarBridge')
KERNEL_COLLECTIONS = ('/System/Library/KernelCollections/BootKernelExtensions.kc',
                      '/System/Library/KernelCollections/SystemKernelExtensions.kc',
                      '/System/Library/KernelCollections/AuxiliaryKernelExtensions.kc',
                      '/Library/KernelCollections/AuxiliaryKernelExtensions.kc')
OWN_BUNDLE_ID = 'de.nordapps.LunarBridge'
MAX_PROPERTY_BYTES = 8


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(command: list[str], timeout: int = 120) -> dict:
    """One read-only command, with its outcome recorded either way."""
    binary = shutil.which(command[0]) or (command[0] if Path(command[0]).is_file() else None)
    if not binary:
        return {'performed': False, 'reason': 'not found: ' + command[0]}
    try:
        result = subprocess.run([binary, *command[1:]], capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {'performed': False, 'reason': type(error).__name__ + ': ' + str(error)[:160]}
    return {'performed': True, 'command': command, 'exit_code': result.returncode,
            'stdout': result.stdout, 'stderr': result.stderr[:2000].decode('utf-8', 'replace')}


def text_of(result: dict, limit: int = 20000) -> str:
    if not result.get('performed'):
        return ''
    return result['stdout'][:limit].decode('utf-8', 'replace')


def reduce_value(key: str, value):
    """A property as a fact: numbers stay, blobs become length and digest."""
    if isinstance(value, bytes):
        if len(value) <= MAX_PROPERTY_BYTES:
            number = int.from_bytes(value, 'little')
            return {'bytes': len(value), 'value': number, 'hex': hex(number)}
        return {'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest(),
                'note': 'content not reproduced'}
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value
    if isinstance(value, str):
        return value[:400]
    if isinstance(value, list):
        return [reduce_value(key, item) for item in value[:32]]
    if isinstance(value, dict):
        return {str(name): reduce_value(str(name), item)
                for name, item in list(value.items())[:64]}
    return str(type(value).__name__)


def ioreg_plist(arguments: list[str]) -> dict:
    """ioreg in archive form, parsed as a plist instead of scraped as text."""
    result = run(['ioreg', '-a', '-l', '-w', '0', *arguments])
    if not result.get('performed'):
        return result
    if not result['stdout'].strip():
        return {'performed': False, 'reason': 'ioreg returned nothing for ' + ' '.join(arguments)}
    try:
        parsed = plistlib.loads(result['stdout'])
    except Exception as error:
        return {'performed': False,
                'reason': 'unparsable ioreg plist: ' + type(error).__name__ + ' '
                          + str(error)[:120]}
    return {'performed': True, 'nodes': parsed if isinstance(parsed, list) else [parsed]}


def walk(nodes, depth: int = 0):
    """Every node of an ioreg tree, with its depth."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield depth, node
        children = node.get('IORegistryEntryChildren') or []
        yield from walk(children, depth + 1)


def csr_flags(read_reference) -> dict:
    """The SIP bit names, parsed from the pinned xnu header."""
    source = read_reference(CSR_HEADER)
    flags = {}
    for name, shift in re.findall(r'#define\s+(CSR_ALLOW_\w+)\s+\(1 << (\d+)\)', source):
        flags.setdefault(int(shift), []).append(name)
    if not flags:
        raise RuntimeError('no CSR_ALLOW flags in the pinned csr.h')
    return {'citation': CSR_HEADER, 'bits': {str(bit): names
                                             for bit, names in sorted(flags.items())}}


def decode_csr(word: int, flags: dict) -> dict:
    # A bit can carry two names - xnu keeps CSR_ALLOW_DESTRUCTIVE_DTRACE beside
    # CSR_ALLOW_UNRESTRICTED_DTRACE and marks the first deprecated - so the last
    # declared alias is the current one, and the aliases are reported with it.
    allowed, denied, aliases = [], [], {}
    for bit, names in flags['bits'].items():
        name = names[-1]
        if len(names) > 1:
            aliases[name] = names[:-1]
        (allowed if word & (1 << int(bit)) else denied).append(name)
    return {'value': word, 'hex': hex(word), 'allowed': allowed, 'denied': denied,
            'aliases': aliases}


def stage_identity() -> dict:
    record = {'host': {'system': platform.system(), 'release': platform.release(),
                       'machine': platform.machine(),
                       'macos_version': platform.mac_ver()[0]}}
    if platform.system() != 'Darwin':
        record['performed'] = False
        record['reason'] = 'not running on macOS; run this on the target machine'
        return record
    record['performed'] = True
    record['sw_vers'] = text_of(run(['sw_vers']), 2000)
    record['uname'] = text_of(run(['uname', '-a']), 500)
    tree = ioreg_plist(['-c', 'IOPlatformExpertDevice'])
    if tree.get('performed'):
        for _, node in walk(tree['nodes']):
            if node.get('IOClass') in ('IOPlatformExpertDevice', 'IOACPIPlatformExpert') \
                    or 'board-id' in node or 'model' in node:
                record['platform_node'] = {
                    key: reduce_value(key, node[key])
                    for key in ('model', 'board-id', 'manufacturer', 'product-name',
                                'target-type', 'IOPlatformSerialNumber', 'version',
                                'system-type', 'compatible')
                    if key in node and 'Serial' not in key}
                break
    else:
        record['platform_node'] = tree
    return record


def stage_security(flags: dict) -> dict:
    if platform.system() != 'Darwin':
        return {'performed': False, 'reason': 'not running on macOS'}
    record = {'performed': True}
    record['csrutil_status'] = text_of(run(['csrutil', 'status']), 1000).strip()
    record['csrutil_authenticated_root'] = text_of(
        run(['csrutil', 'authenticated-root', 'status']), 500).strip()
    nvram = run(['nvram', 'csr-active-config'])
    word = None
    if nvram.get('performed'):
        text = text_of(nvram, 500)
        match = re.search(r'csr-active-config\s+(.+)', text)
        if match:
            raw = match.group(1).strip()
            record['csr_active_config_raw'] = raw[:64]
            # nvram prints binary data as %xx escapes, little-endian word.
            escaped = re.findall(r'%([0-9a-fA-F]{2})', raw)
            if escaped:
                word = int.from_bytes(bytes(int(byte, 16) for byte in escaped), 'little')
            else:
                literal = ''.join(ch for ch in raw if ch.isprintable())
                word = int.from_bytes(literal.encode('latin-1')[:4], 'little') if literal else None
    record['sip'] = decode_csr(word, flags) if word is not None else {
        'performed': False, 'reason': 'csr-active-config not readable; '
                                      'csrutil status above is the authority'}
    record['sip_flag_source'] = flags['citation']
    return record


def stage_boot(efi: Path | None) -> dict:
    if platform.system() != 'Darwin':
        return {'performed': False, 'reason': 'not running on macOS'}
    record: dict = {'performed': True, 'nvram': {}}
    for key in NVRAM_KEYS:
        result = run(['nvram', key])
        if not result.get('performed') or result['exit_code']:
            record['nvram'][key] = None
            continue
        text = text_of(result, 2000)
        value = text.split('\t', 1)[1].strip() if '\t' in text else text.strip()
        record['nvram'][key] = value[:600]
    record['nvram_note'] = ('only these variables are read; identifiers and keys are never '
                            'collected')
    if efi:
        record['opencore'] = read_opencore(efi)
    else:
        record['opencore'] = {'performed': False,
                              'reason': 'no --efi path given; pass the mounted ESP to record '
                                        'the GPU-relevant parts of config.plist'}
    return record


def read_opencore(efi: Path) -> dict:
    """The GPU-relevant parts of an OpenCore config, with identifiers removed."""
    config = efi / 'EFI/OC/config.plist'
    if not config.is_file():
        return {'performed': False, 'reason': 'no EFI/OC/config.plist under ' + str(efi)}
    try:
        data = config.read_bytes()
        parsed = plistlib.loads(data)
    except Exception as error:
        return {'performed': False, 'reason': type(error).__name__ + ': ' + str(error)[:160]}
    kernel = parsed.get('Kernel') or {}
    nvram = ((parsed.get('NVRAM') or {}).get('Add') or {}).get(
        '7C436110-AB2A-4BBB-A880-FE41995C9F82') or {}
    properties = ((parsed.get('DeviceProperties') or {}).get('Add') or {})
    gpu_properties = {}
    for device, values in properties.items():
        if 'Pci(0x2,0x0)' in device or 'GFX' in device.upper() or 'Pci(0x0,0x0)' in device:
            gpu_properties[device] = {str(key): reduce_value(str(key), value)
                                      for key, value in values.items()}
    return {
        'performed': True,
        'config_sha256': hashlib.sha256(data).hexdigest(),
        'opencore_version': str(parsed.get('#Revision', {}).get('Version', 'unknown'))[:40]
        if isinstance(parsed.get('#Revision'), dict) else 'unknown',
        'kexts_added': [{'bundle': str(entry.get('BundlePath', ''))[:80],
                         'enabled': bool(entry.get('Enabled')),
                         'executable': str(entry.get('ExecutablePath', ''))[:80]}
                        for entry in (kernel.get('Add') or [])][:64],
        'kernel_patch_count': len(kernel.get('Patch') or []),
        'kernel_block_count': len(kernel.get('Block') or []),
        'kernel_quirks': {str(key): reduce_value(str(key), value)
                          for key, value in (kernel.get('Quirks') or {}).items()},
        'boot_args': str(nvram.get('boot-args', ''))[:400],
        'csr_active_config': reduce_value('csr-active-config', nvram.get('csr-active-config'))
        if 'csr-active-config' in nvram else None,
        'gpu_device_properties': gpu_properties,
        'note': 'serials, MLB, ROM and SystemUUID are not read from this file',
    }


def stage_pci(target_device: dict) -> dict:
    if platform.system() != 'Darwin':
        return {'performed': False, 'reason': 'not running on macOS'}
    tree = ioreg_plist(['-c', 'IOPCIDevice'])
    if not tree.get('performed'):
        return tree
    devices, target = [], None
    for _, node in walk(tree['nodes']):
        if 'vendor-id' not in node and 'device-id' not in node:
            continue
        identity = {key: reduce_value(key, node[key]) for key in PCI_IDENTITY if key in node}
        injected = {str(key): reduce_value(str(key), value) for key, value in node.items()
                    if PCI_INJECTED.match(str(key))}
        entry = {
            'name': str(node.get('IOName') or node.get('name') or '')[:64],
            'class': str(node.get('IOClass') or '')[:64],
            'provider_class': str(node.get('IOProviderClass') or '')[:64],
            'identity': identity,
            'injected_properties': injected,
            'bars': reduce_value('assigned-addresses', node.get('assigned-addresses')),
            'ranges_present': 'ranges' in node,
            'driver_attached': bool(node.get('IOClass')),
            'power_state': node.get('IOPowerManagement', {}).get('CurrentPowerState')
            if isinstance(node.get('IOPowerManagement'), dict) else None,
        }
        devices.append(entry)
        vendor = identity.get('vendor-id', {})
        device = identity.get('device-id', {})
        if isinstance(vendor, dict) and isinstance(device, dict) \
                and vendor.get('value') == int(target_device['vendor'], 16) \
                and device.get('value') == int(target_device['device'], 16):
            target = entry
    return {'performed': True, 'device_count': len(devices),
            'devices': devices[:64], 'target': target,
            'target_present': target is not None,
            'target_driver_attached': bool(target and target['driver_attached'])}


def stage_graphics_stack() -> dict:
    if platform.system() != 'Darwin':
        return {'performed': False, 'reason': 'not running on macOS'}
    record: dict = {'performed': True, 'families': {}}
    for family in GRAPHICS_CLASSES:
        tree = ioreg_plist(['-c', family])
        if not tree.get('performed'):
            record['families'][family] = {'present': False, 'reason': tree.get('reason')}
            continue
        found = []
        for _, node in walk(tree['nodes']):
            name = str(node.get('IOClass') or node.get('name') or '')
            if not name:
                continue
            found.append({'class': name[:64],
                          'provider_class': str(node.get('IOProviderClass') or '')[:64],
                          'match_category': str(node.get('IOMatchCategory') or '')[:64],
                          'user_client_class': str(node.get('IOUserClientClass') or '')[:64],
                          'bundle': str(node.get('CFBundleIdentifier') or '')[:80]})
        record['families'][family] = {'present': bool(found), 'nodes': found[:32]}
    record['accelerator_present'] = any(
        entry.get('present') for name, entry in record['families'].items()
        if name in ('IOAccelerator', 'IOGPU'))
    return record


def stage_loaded() -> dict:
    if platform.system() != 'Darwin':
        return {'performed': False, 'reason': 'not running on macOS'}
    result = run(['kextstat', '-l'])
    if not result.get('performed'):
        return result
    rows = []
    for line in text_of(result, 200000).splitlines():
        if not any(keyword in line for keyword in GRAPHICS_KEYWORDS):
            continue
        fields = line.split()
        if len(fields) < 6:
            continue
        rows.append({'index': fields[0], 'references': fields[1],
                     'bundle': fields[5][:80], 'version': fields[6].strip('()')[:32]
                     if len(fields) > 6 else ''})
    return {'performed': True, 'graphics_kexts': rows,
            'own_driver_loaded': any(OWN_BUNDLE_ID in row['bundle'] for row in rows),
            'total_graphics_kexts': len(rows)}


def stage_collections() -> dict:
    if platform.system() != 'Darwin':
        return {'performed': False, 'reason': 'not running on macOS'}
    record: dict = {'performed': True, 'collections': {}}
    for path in KERNEL_COLLECTIONS:
        if not Path(path).exists():
            record['collections'][path] = {'present': False}
            continue
        digest = hashlib.sha256()
        size = 0
        try:
            with open(path, 'rb') as handle:
                for block in iter(lambda: handle.read(1 << 20), b''):
                    digest.update(block)
                    size += len(block)
        except OSError as error:
            record['collections'][path] = {'present': True,
                                           'reason': 'unreadable: ' + str(error)[:80]}
            continue
        entry = {'present': True, 'bytes': size, 'sha256': digest.hexdigest()}
        listing = run(['kmutil', 'inspect', '--collection-path', path, '--no-header'], 300)
        if listing.get('performed') and not listing['exit_code']:
            bundles = []
            for line in text_of(listing, 400000).splitlines():
                if any(keyword in line for keyword in GRAPHICS_KEYWORDS):
                    bundles.append(' '.join(line.split())[:120])
            entry['graphics_bundles'] = bundles[:128]
            entry['graphics_bundle_count'] = len(bundles)
        else:
            entry['listing'] = {'performed': False,
                                'reason': listing.get('reason')
                                or ('kmutil inspect exited '
                                    + str(listing.get('exit_code')))}
        record['collections'][path] = entry
    return record


def stage_displays() -> dict:
    if platform.system() != 'Darwin':
        return {'performed': False, 'reason': 'not running on macOS'}
    result = run(['system_profiler', '-json', 'SPDisplaysDataType'], 180)
    if not result.get('performed'):
        return result
    import json as _json
    try:
        parsed = _json.loads(text_of(result, 400000) or '{}')
    except ValueError as error:
        return {'performed': False, 'reason': 'unparsable system_profiler output: '
                                              + str(error)[:120]}
    reduced = []
    for entry in parsed.get('SPDisplaysDataType', [])[:8]:
        reduced.append({key: reduce_value(key, value) for key, value in entry.items()
                        if not isinstance(value, (list, dict))
                        or key.startswith('spdisplays')})
    return {'performed': True, 'adapters': reduced}


def capabilities(record: dict) -> list[dict]:
    """What this machine will let us try next, and what has to change first."""
    out = []
    sip = (record.get('security') or {}).get('sip') or {}
    allowed = set(sip.get('allowed') or [])

    def add(name, possible, reason, unblock):
        out.append({'capability': name, 'possible': possible, 'reason': reason,
                    'unblock': unblock})

    if sip.get('performed') is False or not sip:
        add('read the SIP word', False, sip.get('reason', 'not collected'),
            'run this on the target machine')
    else:
        add('load an unsigned kernel extension',
            'CSR_ALLOW_UNTRUSTED_KEXTS' in allowed,
            'csr-active-config = ' + str(sip.get('hex')),
            'boot to recovery and set csr-active-config so untrusted kexts are allowed')
        add('trace the kernel with dtrace',
            'CSR_ALLOW_UNRESTRICTED_DTRACE' in allowed,
            'unrestricted dtrace bit ' + ('set' if 'CSR_ALLOW_UNRESTRICTED_DTRACE' in allowed
                                          else 'clear'),
            'allow unrestricted dtrace in the SIP word; userspace pid probes and the '
            'unified log work without it')
        add('attach a debugger to a system process',
            'CSR_ALLOW_TASK_FOR_PID' in allowed,
            'task_for_pid bit ' + ('set' if 'CSR_ALLOW_TASK_FOR_PID' in allowed else 'clear'),
            'allow task_for_pid in the SIP word')
    pci = record.get('pci') or {}
    if pci.get('performed'):
        add('observe the target GPU in the device tree', bool(pci.get('target_present')),
            'target ' + ('found' if pci.get('target_present') else 'not found')
            + ' among ' + str(pci.get('device_count')) + ' PCI nodes',
            'none' if pci.get('target_present') else 'check the PCI path and firmware setup')
        add('watch an Apple driver on this GPU', bool(pci.get('target_driver_attached')),
            'no driver is attached to the target node' if not pci.get('target_driver_attached')
            else 'a driver is attached',
            'nothing to do: Apple ships no Xe2 driver, so runtime observation of a vendor '
            'driver has to happen on a machine whose GPU is supported')
    stack = record.get('graphics_stack') or {}
    if stack.get('performed'):
        add('observe a live accelerator interface', bool(stack.get('accelerator_present')),
            'IOAccelerator/IOGPU nodes ' + ('present' if stack.get('accelerator_present')
                                            else 'absent'),
            'none' if stack.get('accelerator_present')
            else 'capture the family interface on a Mac with a supported GPU and compare')
    loaded = record.get('loaded') or {}
    if loaded.get('performed'):
        add('read our own driver through its selectors', bool(loaded.get('own_driver_loaded')),
            'LunarBridge ' + ('loaded' if loaded.get('own_driver_loaded') else 'not loaded'),
            'none' if loaded.get('own_driver_loaded')
            else 'install the unsigned bundle and load it once the SIP word permits it')
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=ROOT / 'build/recon/platform.json')
    parser.add_argument('--efi', type=Path,
                        help='a mounted EFI system partition, to record the GPU-relevant '
                             'parts of EFI/OC/config.plist')
    arguments = parser.parse_args()

    compare = load('compare_interfaces', 'tools/compare_interfaces.py')
    policy = load('abi_policy', 'tools/abi_policy.py')
    flags = csr_flags(compare.read_reference)

    record: dict = {'generated_by': 'tools/collect_platform.py',
                    'read_only': True,
                    'root': hasattr(__import__('os'), 'geteuid')
                    and __import__('os').geteuid() == 0}
    record['identity'] = stage_identity()
    record['security'] = stage_security(flags)
    record['boot'] = stage_boot(arguments.efi)
    record['pci'] = stage_pci({'vendor': '0x8086', 'device': '0x64a0'})
    record['graphics_stack'] = stage_graphics_stack()
    record['loaded'] = stage_loaded()
    record['collections'] = stage_collections()
    record['displays'] = stage_displays()
    record['capabilities'] = capabilities(record)

    policy.write_record(arguments.out, record, 'platform record')
    print('Wrote ' + str(arguments.out))
    for entry in record['capabilities']:
        print(('  yes ' if entry['possible'] else '  no  ') + entry['capability']
              + ' — ' + entry['reason'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
