# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Map the whole installed macOS graphics stack: both halves and their pairing.

Acceleration on macOS is a pair. A kernel driver owns the hardware and exposes a
user client; a userspace Metal driver plug-in opens that client and submits work
through it. Neither half's interface is published, and an independent driver has
to satisfy both sides. This walks everything installed on the machine that
belongs to either half, maps each bundle's interface surface, and then pairs the
halves so the contract between them can be read as one thing.

    python3 tools/extract_macos_graphics.py --out build/recon/graphics-inventory.json

Read-only, on the machine that lawfully holds the installed copy. Per bundle it
records identity, version, linked families, IOKit personalities, plug-ins,
binary size and digest, Mach-O structure, the classes it defines, the foreign
classes it references, the IOKit entry points it imports and the Objective-C
surface it publishes. It records no instruction byte, and copies no bundle.

Without `nm` the symbol half is reported as unavailable instead of guessed.
"""
from __future__ import annotations

import argparse
import importlib.util
import platform
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Where either half of the stack lives on a current macOS install.
SEARCH_ROOTS = (
    ('kernel', '/System/Library/Extensions'),
    ('kernel', '/Library/Extensions'),
    ('driverkit', '/System/Library/DriverExtensions'),
    ('driverkit', '/Library/DriverExtensions'),
    ('userspace', '/System/Library/Frameworks'),
    ('userspace', '/System/Library/PrivateFrameworks'),
    ('userspace', '/System/Library/Extensions'),
)
# A bundle belongs to the graphics stack if its name carries one of these.
GRAPHICS_NAMES = ('Graphics', 'IOGPU', 'IOAccelerator', 'IOSurface', 'AGX', 'AGDC',
                  'Metal', 'MTL', 'GPU', 'Display', 'OpenGL', 'OpenCL', 'CoreDisplay',
                  'IOGraphics', 'SkyLight', 'QuartzCore', 'GPUCompiler', 'DriverKit')
BUNDLE_SUFFIXES = ('.kext', '.bundle', '.framework', '.dext', '.plugin')
# IOKit entry points a userspace driver must import to talk to a kernel driver.
IOKIT_ENTRY_POINTS = ('IOServiceOpen', 'IOServiceClose', 'IOConnectCallMethod',
                      'IOConnectCallScalarMethod', 'IOConnectCallStructMethod',
                      'IOConnectCallAsyncMethod', 'IOConnectCallAsyncScalarMethod',
                      'IOConnectCallAsyncStructMethod', 'IOConnectMapMemory',
                      'IOConnectMapMemory64', 'IOConnectUnmapMemory64',
                      'IOConnectSetNotificationPort', 'IOConnectAddClient',
                      'IOServiceGetMatchingService', 'IOServiceGetMatchingServices',
                      'IORegistryEntryCreateCFProperty', 'IODataQueueDequeue',
                      'IOServiceAuthorize', 'IOCreateReceivePort')
OBJC_CLASS = re.compile(r'^_OBJC_(METACLASS|CLASS)_\$_(.+)$')
MAX_BUNDLES = 400


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_graphics(path: Path) -> bool:
    name = path.name
    return any(keyword.lower() in name.lower() for keyword in GRAPHICS_NAMES)


def discover(roots) -> list[dict]:
    """Every graphics-related bundle under the search roots, including plug-ins."""
    found, seen = [], set()
    for half, root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.suffix not in BUNDLE_SUFFIXES or not entry.is_dir():
                continue
            if not is_graphics(entry):
                continue
            if str(entry) in seen:
                continue
            seen.add(str(entry))
            found.append({'half': half, 'path': entry, 'kind': entry.suffix.lstrip('.')})
            plugins = entry / 'Contents/PlugIns'
            if plugins.is_dir():
                for plugin in sorted(plugins.iterdir()):
                    if plugin.suffix in BUNDLE_SUFFIXES and str(plugin) not in seen:
                        seen.add(str(plugin))
                        found.append({'half': 'userspace', 'path': plugin,
                                      'kind': plugin.suffix.lstrip('.'),
                                      'plugin_of': entry.name})
            if len(found) >= MAX_BUNDLES:
                return found
    return found


def objc_surface(defined: list[str], undefined: list[str]) -> dict:
    """The Objective-C classes a bundle publishes and the protocols it needs."""
    classes, metaclasses, referenced = set(), set(), set()
    for name in defined:
        match = OBJC_CLASS.match(name)
        if match:
            (metaclasses if match.group(1) == 'METACLASS' else classes).add(match.group(2))
    for name in undefined:
        match = OBJC_CLASS.match(name)
        if match:
            referenced.add(match.group(2))
    return {'classes_defined': sorted(classes)[:200],
            'metaclasses_defined': sorted(metaclasses)[:200],
            'classes_referenced': sorted(referenced)[:200],
            'class_count': len(classes)}


def iokit_imports(undefined: list[str]) -> list[str]:
    """Which IOKit entry points this bundle actually calls."""
    names = {name.lstrip('_') for name in undefined}
    return [entry for entry in IOKIT_ENTRY_POINTS if entry in names]


def map_bundle(entry: dict, analyzer, nm: str | None) -> dict:
    path: Path = entry['path']
    record: dict = {'path': str(path), 'name': path.name, 'half': entry['half'],
                    'kind': entry['kind']}
    if 'plugin_of' in entry:
        record['plugin_of'] = entry['plugin_of']
    try:
        mapped = analyzer.analyze(path, nm)
    except SystemExit as error:
        record['mapped'] = False
        record['reason'] = str(error)[:200]
        return record
    except Exception as error:
        record['mapped'] = False
        record['reason'] = type(error).__name__ + ': ' + str(error)[:200]
        return record
    record['mapped'] = True
    record['bundle'] = mapped.get('bundle')
    record['library_dependencies'] = mapped.get('library_dependencies')
    record['personalities'] = mapped.get('personalities')
    record['plugins'] = mapped.get('plugins')
    binary = mapped.get('binary') or {}
    record['binary'] = {key: binary.get(key) for key in ('present', 'bytes', 'sha256')}
    macho = binary.get('macho') or {}
    record['macho'] = {'filetype': macho.get('filetype'),
                       'sections': macho.get('section_names', [])[:64],
                       'has_code_signature': macho.get('has_code_signature')}
    symbols = mapped.get('symbols') or {}
    if not symbols.get('available'):
        record['symbols'] = {'available': False, 'reason': symbols.get('reason')}
        return record
    defined = macho.get('defined_symbols') or []
    undefined = macho.get('undefined_symbols') or []
    record['symbols'] = {
        'available': True,
        'defined_count': symbols.get('defined_count'),
        'undefined_count': symbols.get('undefined_count'),
        'classes_defined_by_this_bundle': symbols.get('classes_defined_by_this_bundle'),
        'foreign_metaclasses_referenced': symbols.get('foreign_metaclasses_referenced'),
        'iokit_overrides': symbols.get('iokit_overrides'),
        'family_references': symbols.get('referenced'),
        'objc': objc_surface(defined, undefined),
        'iokit_imports': iokit_imports(undefined),
    }
    return record


def pair_halves(records: list[dict]) -> list[dict]:
    """Match each kernel driver with the userspace driver that drives it.

    The pairing is what carries the submission contract: a Metal plug-in named
    after a kernel driver, or living inside it, opens that driver's user client.
    """
    kernel = [record for record in records if record.get('half') == 'kernel']
    userspace = [record for record in records if record.get('half') == 'userspace']
    pairs = []
    for driver in kernel:
        stem = driver['name'].replace('.kext', '')
        partners = []
        for client in userspace:
            name = client['name']
            if client.get('plugin_of') == driver['name'] or stem in name \
                    or name.replace('MTLDriver', '').replace('.bundle', '') == stem:
                partners.append({'path': client['path'],
                                 'iokit_imports': (client.get('symbols') or {})
                                 .get('iokit_imports', []),
                                 'objc_class_count': ((client.get('symbols') or {})
                                                      .get('objc') or {}).get('class_count')})
        personalities = driver.get('personalities') or {}
        user_clients = sorted({str(values.get('IOUserClientClass'))
                               for values in personalities.values()
                               if isinstance(values, dict) and values.get('IOUserClientClass')})
        pairs.append({
            'kernel_bundle': driver.get('bundle', {}).get('identifier') or driver['name'],
            'kernel_path': driver['path'],
            'user_client_classes': user_clients,
            'linked_families': sorted((driver.get('library_dependencies') or {}).keys()),
            'userspace_partners': partners,
            'paired': bool(partners),
        })
    return pairs


def summarise(record: dict) -> dict:
    mapped = [entry for entry in record['bundles'] if entry.get('mapped')]
    families: dict = {}
    for entry in mapped:
        for family in (entry.get('library_dependencies') or {}):
            families[family] = families.get(family, 0) + 1
    overrides: dict = {}
    for entry in mapped:
        for owner, methods in ((entry.get('symbols') or {}).get('iokit_overrides') or {}).items():
            overrides.setdefault(owner, sorted(set(methods)))
    return {
        'bundles_found': len(record['bundles']),
        'bundles_mapped': len(mapped),
        'symbols_available': sum(1 for entry in mapped
                                 if (entry.get('symbols') or {}).get('available')),
        'kernel_bundles': sum(1 for entry in mapped if entry['half'] == 'kernel'),
        'userspace_bundles': sum(1 for entry in mapped if entry['half'] == 'userspace'),
        'linked_families': dict(sorted(families.items(), key=lambda item: -item[1])),
        'classes_with_iokit_overrides': overrides,
        'pairs': len(record['pairs']),
        'paired': sum(1 for pair in record['pairs'] if pair['paired']),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path,
                        default=ROOT / 'build/recon/graphics-inventory.json')
    parser.add_argument('--root', action='append', type=Path,
                        help='an extra directory to search (repeatable)')
    arguments = parser.parse_args()

    analyzer = load('analyze_apple_driver', 'tools/analyze_apple_driver.py')
    policy = load('abi_policy', 'tools/abi_policy.py')
    nm = shutil.which('nm')
    roots = list(SEARCH_ROOTS) + [('kernel', str(path)) for path in (arguments.root or [])]
    entries = discover(roots)
    record: dict = {
        'generated_by': 'tools/extract_macos_graphics.py',
        'host': {'system': platform.system(), 'macos_version': platform.mac_ver()[0]},
        'symbol_tool': nm,
        'searched': [root for _, root in roots],
        'bundles': [map_bundle(entry, analyzer, nm) for entry in entries],
    }
    record['pairs'] = pair_halves(record['bundles'])
    record['summary'] = summarise(record)
    if not entries:
        record['note'] = ('no graphics bundle found under the search roots; run this on the '
                          'macOS installation that holds them')
    policy.write_record(arguments.out, record, 'graphics inventory')
    summary = record['summary']
    print('Wrote ' + str(arguments.out))
    print(str(summary['bundles_mapped']) + ' of ' + str(summary['bundles_found'])
          + ' bundles mapped (' + str(summary['kernel_bundles']) + ' kernel, '
          + str(summary['userspace_bundles']) + ' userspace); '
          + str(summary['paired']) + ' of ' + str(summary['pairs']) + ' kernel drivers paired')
    for family, count in list(summary['linked_families'].items())[:12]:
        print('  ' + str(count) + '×  ' + family)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
