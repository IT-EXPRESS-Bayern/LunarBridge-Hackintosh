# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Map the interface surface of a locally installed Apple graphics driver.

Apple ships no driver for Xe or Xe2, so the newest Intel driver on a macOS
installation is the Ice Lake / Gen11 pair. Its *hardware* programming is
useless for Arc 130V, but its *macOS side* is exactly what this project is
missing: which classes a Metal-capable graphics driver registers, which private
families it links against, which IOKit methods it overrides and how its
personalities are shaped.

This tool reads such a bundle where it already is — on the machine running the
tool — and writes an interface map: bundle metadata, personalities, library
dependencies, the classes the binary defines and the foreign classes it
references, grouped and counted. It copies NO instructions, NO data and NO
binary content into its output; names, counts, offsets and hashes only, so the
result can be committed while the Apple binary stays where Apple put it.

Scope: interoperability analysis of an interface an own driver has to satisfy,
for hardware the vendor does not support. In the EU that is the case
§ 69e UrhG / Art. 6 of the Software Directive describes; the analysis must stay
necessary for interoperability and must not become a copy of the program. This
tool is built to keep it on that side of the line, and it is not legal advice.

Usage on the target machine:
    python3 tools/analyze_apple_driver.py \\
        --kext /System/Library/Extensions/AppleIntelICLGraphics.kext \\
        --out build/apple/icl-graphics-map.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from macho import inspect_macho

# Mangled C++ names start with these; everything else is a plain C symbol.
MANGLED = re.compile(r'^_?_Z')
# IOKit entry points a graphics driver must override for the stack to use it.
IOKIT_OVERRIDES = (
    'init', 'start', 'stop', 'free', 'probe', 'attach', 'detach', 'terminate',
    'newUserClient', 'externalMethod', 'clientMemoryForType', 'clientClose',
    'clientDied', 'getMetaClass', 'setPowerState', 'setProperties',
    'copyProperty', 'message', 'willTerminate', 'didTerminate',
    'registerService', 'matchPropertyTable', 'getWorkLoop',
    # These five decide which dispatch path a user client is on, which is the
    # first thing an independent driver has to match.
    'getTargetAndMethodForIndex', 'getAsyncTargetAndMethodForIndex',
    'getTargetAndTrapForIndex', 'getExternalTrapForIndex', 'dispatchExternalMethod',
)
# An imported (undefined) symbol is the decisive signal: a client on the 2022
# path calls into the kernel's dispatchExternalMethod, which it cannot define.
DISPATCH_PATH_IMPORTS = {
    'dispatchExternalMethod': 'IOUserClient2022 dispatch array',
    'getTargetAndMethodForIndex': 'legacy index-based dispatch',
    'getAsyncTargetAndMethodForIndex': 'legacy asynchronous dispatch',
}
# Foreign class families worth separating in the report.
FAMILIES = ('IOGPU', 'IOAccelerator', 'IOAccel', 'IOSurface', 'IOFramebuffer',
            'IOGraphics', 'IODisplay', 'IOPCIDevice', 'IOService', 'IOUserClient',
            'IOMemoryDescriptor', 'IODMACommand', 'IOBufferMemoryDescriptor',
            'OSMetaClass', 'OSObject', 'IOInterruptEventSource', 'IOTimerEventSource',
            'IOCommandGate', 'IOWorkLoop', 'IOLock', 'IOMapper')


def demangle_components(symbol: str) -> list[str]:
    """Split an Itanium-mangled nested name into its components.

    Only the length-prefixed name part is parsed: that is all this tool needs
    and it avoids depending on c++filt being present.
    """
    marker = symbol.find('_Z')
    if marker < 0:
        return []
    body = symbol[marker + 2:]
    if body.startswith('N'):
        body = body[1:]
    components: list[str] = []
    while body and body[0].isdigit():
        length = 0
        while body and body[0].isdigit():
            length = length * 10 + int(body[0])
            body = body[1:]
        if length > len(body):
            break
        components.append(body[:length])
        body = body[length:]
    return components


def symbol_class_and_member(symbol: str) -> tuple[str, str]:
    components = demangle_components(symbol)
    if len(components) >= 2:
        return '::'.join(components[:-1]), components[-1]
    if components:
        return '', components[0]
    return '', symbol


def run_nm(binary: Path, nm: str, flags: list[str]) -> list[str]:
    result = subprocess.run([nm, *flags, str(binary)], capture_output=True, text=True,
                            timeout=180)
    if result.returncode:
        raise RuntimeError('nm failed: ' + result.stderr.strip()[:200])
    names = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts:
            names.append(parts[-1])
    return names


def group_symbols(names: list[str]) -> dict:
    classes: dict[str, list[str]] = {}
    plain: list[str] = []
    for name in names:
        if not MANGLED.search(name):
            plain.append(name)
            continue
        owner, member = symbol_class_and_member(name)
        classes.setdefault(owner or '<global>', []).append(member)
    return {
        'class_count': len([key for key in classes if key != '<global>']),
        'classes': {key: sorted(set(value)) for key, value in sorted(classes.items())},
        'plain_symbols': sorted(set(plain)),
    }


def metaclass_owners(names: list[str]) -> list[str]:
    """Classes whose OSMetaClass instance appears in the symbol list."""
    owners = set()
    for name in names:
        if 'gMetaClass' not in name:
            continue
        owner, member = symbol_class_and_member(name)
        if member.startswith('gMetaClass') and owner:
            owners.add(owner)
    return sorted(owners)


def family_references(names: list[str]) -> dict:
    hits: dict[str, set[str]] = {family: set() for family in FAMILIES}
    other = set()
    for name in names:
        if not MANGLED.search(name):
            continue
        owner, _ = symbol_class_and_member(name)
        if not owner:
            continue
        for family in FAMILIES:
            if owner.startswith(family):
                hits[family].add(owner)
                break
        else:
            other.add(owner)
    return {'by_family': {family: sorted(value) for family, value in hits.items() if value},
            'other_classes': sorted(other)}


def analyze(bundle: Path, nm: str | None) -> dict:
    info_path = bundle / 'Contents/Info.plist'
    if not info_path.is_file():
        raise SystemExit('No Contents/Info.plist in ' + str(bundle))
    info = plistlib.loads(info_path.read_bytes())
    executable = info.get('CFBundleExecutable')
    binary = bundle / 'Contents/MacOS' / executable if executable else None
    record: dict = {
        'generated_by': 'tools/analyze_apple_driver.py',
        'scope': 'Interface surface only: names, counts and hashes. No instruction bytes, '
                 'no data and no binary content from the analysed bundle are reproduced here, '
                 'and the bundle itself is neither copied nor redistributed.',
        'analysed_on': {'system': platform.system(), 'macos_version': platform.mac_ver()[0],
                        'machine': platform.machine()},
        'bundle': {
            'path': str(bundle),
            'identifier': info.get('CFBundleIdentifier'),
            'version': info.get('CFBundleVersion'),
            'short_version': info.get('CFBundleShortVersionString'),
            'executable': executable,
            'info_plist_sha256': hashlib.sha256(info_path.read_bytes()).hexdigest(),
        },
        'library_dependencies': dict(sorted((info.get('OSBundleLibraries') or {}).items())),
        'personalities': {},
        'plugins': sorted(path.name for path in (bundle / 'Contents/PlugIns').glob('*')
                          if (bundle / 'Contents/PlugIns').is_dir()),
    }
    for name, personality in sorted((info.get('IOKitPersonalities') or {}).items()):
        record['personalities'][name] = {
            key: (value if isinstance(value, (str, int, bool)) else str(type(value).__name__))
            for key, value in sorted(personality.items())
        }
    if not binary or not binary.is_file():
        record['binary'] = {'present': False}
        return record
    data = binary.read_bytes()
    record['binary'] = {
        'present': True, 'path': str(binary), 'bytes': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
    }
    try:
        record['binary']['macho'] = inspect_macho(data, require_kext=False)
    except (ValueError, KeyError) as error:
        record['binary']['macho_error'] = str(error)[:200]
    if not nm:
        record['symbols'] = {'available': False,
                             'reason': 'nm not found; run this on the machine holding the bundle'}
        return record
    defined = run_nm(binary, nm, ['-g', '-U'])
    undefined = run_nm(binary, nm, ['-u'])
    record['symbols'] = {
        'available': True, 'nm': nm,
        'defined_count': len(defined), 'undefined_count': len(undefined),
        'defined': group_symbols(defined),
        'referenced': family_references(undefined),
        'classes_defined_by_this_bundle': metaclass_owners(defined),
        'foreign_metaclasses_referenced': metaclass_owners(undefined),
    }
    overrides: dict[str, list[str]] = {}
    for owner, members in record['symbols']['defined']['classes'].items():
        hits = sorted(set(members) & set(IOKIT_OVERRIDES))
        if hits:
            overrides[owner] = hits
    record['symbols']['iokit_overrides'] = overrides
    # Which dispatch path this bundle is on, read from what it imports rather
    # than from what it defines.
    imported = set()
    for name in undefined:
        for marker in DISPATCH_PATH_IMPORTS:
            if marker in name:
                imported.add(marker)
    record['symbols']['dispatch_path_imports'] = {
        marker: DISPATCH_PATH_IMPORTS[marker] for marker in sorted(imported)}
    record['symbols']['dispatch_path'] = (
        'IOUserClient2022 dispatch array' if 'dispatchExternalMethod' in imported
        else 'legacy index-based dispatch'
        if imported else 'undetermined: neither dispatch entry point is imported')
    return record


def summarise(record: dict) -> str:
    lines = ['# Apple driver interface map', '',
             'Bundle: ' + str(record['bundle'].get('identifier')),
             'Version: ' + str(record['bundle'].get('version')),
             'Analysed on macOS ' + (record['analysed_on'].get('macos_version') or 'unknown'),
             '', '## Library dependencies']
    for name, version in record['library_dependencies'].items():
        lines.append('- ' + name + ' ' + str(version))
    lines += ['', '## Personalities']
    for name, personality in record['personalities'].items():
        lines.append('- ' + name + ': IOClass=' + str(personality.get('IOClass')) +
                     ', IOProviderClass=' + str(personality.get('IOProviderClass')) +
                     ', IOMatchCategory=' + str(personality.get('IOMatchCategory')) +
                     ', IOUserClientClass=' + str(personality.get('IOUserClientClass')))
    symbols = record.get('symbols', {})
    if symbols.get('available'):
        lines += ['', '## Classes this bundle defines']
        for owner in symbols['classes_defined_by_this_bundle']:
            members = symbols['defined']['classes'].get(owner, [])
            lines.append('- ' + owner + ' (' + str(len(members)) + ' symbols)')
        lines += ['', '## IOKit methods overridden']
        for owner, members in sorted(symbols.get('iokit_overrides', {}).items()):
            lines.append('- ' + owner + ': ' + ', '.join(members))
        lines += ['', '## Foreign classes referenced']
        for family, owners in sorted(symbols['referenced']['by_family'].items()):
            lines.append('- ' + family + ': ' + ', '.join(owners))
    else:
        lines += ['', 'Symbols unavailable: ' + str(symbols.get('reason'))]
    lines += ['', 'No instruction or data bytes of the analysed bundle appear in this map.']
    return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--kext', required=True, type=Path,
                        help='Path to the installed .kext bundle to analyse')
    parser.add_argument('--out', required=True, type=Path, help='JSON interface map to write')
    parser.add_argument('--markdown', type=Path, help='Optional human-readable summary')
    parser.add_argument('--nm', help='nm binary to use (default: the one on PATH)')
    arguments = parser.parse_args()
    nm = arguments.nm or shutil.which('nm')
    record = analyze(arguments.kext.resolve(), nm)
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print('Wrote ' + str(arguments.out))
    if arguments.markdown:
        arguments.markdown.parent.mkdir(parents=True, exist_ok=True)
        arguments.markdown.write_text(summarise(record), encoding='utf-8')
        print('Wrote ' + str(arguments.markdown))
    if not record.get('symbols', {}).get('available'):
        print('Symbols were not read; the map holds bundle metadata only.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
