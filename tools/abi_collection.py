# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Read a kext out of a kernel collection without extracting anything.

On a current macOS install a graphics kext's bundle may hold only an Info.plist:
the executable lives inside a Kernel Collection. That is not an obstacle, and
nothing needs unpacking — a collection is an `MH_FILESET` whose
`LC_FILESET_ENTRY` records name each bundle and where its image starts, and
Apple's own `nm` understands that layout: it walks the entries and prints a
`Symbols for <bundle id>:` header before each one's symbols.

So this module answers three questions about a collection, all read-only:

    which bundles does it carry, at which address and offset
    does this machine's nm understand filesets at all
    what does one bundle define and import

The collection is memory-mapped, never read into a buffer, and nm's output is
streamed and filtered to the requested bundle rather than collected whole: a
System collection runs to hundreds of megabytes and hundreds of entries.

    python3 tools/abi_collection.py --collection <path.kc> --list
    python3 tools/abi_collection.py --collection <path.kc> \\
        --bundle com.apple.driver.AppleIntelICLGraphics --out symbols.json
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS_HEADER = re.compile(r'^Symbols for (.+?):\s*$')
# nm prints `<address> <type> <name>` for a defined symbol and `   U <name>`
# or a bare name for an undefined one, depending on the flags it was given.
SYMBOL_LINE = re.compile(r'^(?:([0-9a-fA-F]+)\s+)?([A-Za-z])?\s*(\S+)$')
MAX_SYMBOLS_PER_BUNDLE = 200_000


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CollectionError(Exception):
    """The collection, or the tool meant to read it, is not usable."""


def bundles(path: Path) -> list[dict]:
    """Every bundle the collection carries, with its address and offset."""
    macho = load('abi_macho', 'tools/abi_macho.py')
    data, handle = macho.map_file(path)
    try:
        image = macho.open_image(data, str(path))
        if image.filetype != macho.MH_FILESET:
            raise CollectionError('not a kernel collection: '
                                  + image.describe()['filetype_name'])
        entries = [{'identifier': entry['identifier'], 'vmaddr': entry['vmaddr'],
                    'vmaddr_hex': hex(entry['vmaddr']), 'fileoff': entry['fileoff']}
                   for entry in image.fileset_entries]
    finally:
        if handle is not None:
            data.close()
            handle.close()
    return sorted(entries, key=lambda entry: entry['identifier'])


def close_process(process) -> None:
    """Stop the child and close its pipes.

    These readers stop as soon as they have what they need, so the child is
    still writing: without this, every bundle leaks two file descriptors.
    """
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    if process.poll() is None:
        process.kill()
    process.wait()


def nm_path() -> str:
    for name in ('nm', 'llvm-nm'):
        found = shutil.which(name)
        if found:
            return found
    raise CollectionError('no nm in PATH; install the command line tools')


def nm_understands_filesets(collection: Path, nm: str | None = None) -> dict:
    """Probe the capability instead of assuming it.

    Older nm implementations do not walk a fileset, so the header line is the
    evidence that this one does.
    """
    tool = nm or nm_path()
    policy = load('abi_policy', 'tools/abi_policy.py')
    command = [tool, '-arch', 'x86_64', '-g', '-U', str(collection)]
    policy.assert_tool_invocation(command)
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
    except OSError as error:
        return {'supported': False, 'reason': str(error)[:160], 'tool': tool}
    headers = 0
    try:
        for line in process.stdout:
            if SYMBOLS_HEADER.match(line):
                headers += 1
                if headers >= 2:
                    break
    finally:
        close_process(process)
    return {'supported': headers > 0, 'tool': tool,
            'headers_seen': headers,
            'reason': None if headers else
            'nm printed no "Symbols for <bundle>:" header, so it did not walk the fileset'}


def symbols(collection: Path, identifier: str, nm: str | None = None) -> dict:
    """One bundle's defined and imported symbols, streamed out of the collection."""
    tool = nm or nm_path()
    policy = load('abi_policy', 'tools/abi_policy.py')
    result: dict = {'identifier': identifier, 'tool': tool,
                    'defined': [], 'undefined': [],
                    'bundles_seen': 0, 'truncated': False}
    for flags, key in ((['-g', '-U'], 'defined'), (['-u'], 'undefined')):
        command = [tool, '-arch', 'x86_64', *flags, str(collection)]
        policy.assert_tool_invocation(command)
        names, wanted, seen = [], False, 0
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
        except OSError as error:
            raise CollectionError('nm failed: ' + str(error)[:160])
        try:
            for line in process.stdout:
                header = SYMBOLS_HEADER.match(line)
                if header:
                    seen += 1
                    wanted = header.group(1).strip() == identifier
                    continue
                if not wanted:
                    continue
                match = SYMBOL_LINE.match(line.strip())
                if not match or not match.group(3):
                    continue
                names.append(match.group(3))
                if len(names) >= MAX_SYMBOLS_PER_BUNDLE:
                    result['truncated'] = True
                    break
        finally:
            close_process(process)
        result[key] = sorted(set(names))
        result['bundles_seen'] = max(result['bundles_seen'], seen)
    if not result['defined'] and not result['undefined']:
        result['reason'] = ('no symbols labelled for this bundle; check the identifier '
                            'against --list, and that nm walks filesets at all')
    return result


def describe(collection: Path, identifier: str | None, nm: str | None = None) -> dict:
    """Everything this module can say about a collection, as one record."""
    macho = load('abi_macho', 'tools/abi_macho.py')
    entries = bundles(collection)
    record: dict = {
        'generated_by': 'tools/abi_collection.py',
        'collection': {'path': str(collection), 'sha256': macho.digest(collection),
                       'bundle_count': len(entries)},
        'bundles': entries[:1024],
        'graphics_bundles': [entry for entry in entries
                             if any(keyword in entry['identifier']
                                    for keyword in ('Graphics', 'IOGPU', 'IOAccelerator',
                                                    'IOSurface', 'AGX', 'Display'))],
        'limits': 'Bundle identifiers, addresses, offsets and symbol names. The collection is '
                  'mapped read-only and never copied; no image is extracted and no byte of one '
                  'is reproduced.',
    }
    try:
        record['nm_fileset_support'] = nm_understands_filesets(collection, nm)
    except CollectionError as error:
        record['nm_fileset_support'] = {'supported': False, 'reason': str(error)[:160]}
    if identifier:
        if record['nm_fileset_support'].get('supported'):
            record['symbols'] = symbols(collection, identifier, nm)
        else:
            record['symbols'] = {'identifier': identifier, 'performed': False,
                                 'reason': 'nm does not walk filesets on this machine; '
                                           'read the entry with tools/abi_dispatch.py '
                                           '--bundle instead, which parses it directly'}
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--collection', required=True, type=Path)
    parser.add_argument('--bundle', help='read this bundle\'s symbols')
    parser.add_argument('--list', action='store_true', help='list the bundles and stop')
    parser.add_argument('--out', type=Path)
    arguments = parser.parse_args()

    if arguments.list:
        for entry in bundles(arguments.collection):
            print(entry['vmaddr_hex'] + '  ' + entry['identifier'])
        return 0
    record = describe(arguments.collection, arguments.bundle)
    if arguments.out:
        policy = load('abi_policy', 'tools/abi_policy.py')
        policy.write_record(arguments.out, record, 'collection record')
        print('Wrote ' + str(arguments.out))
    print(str(record['collection']['bundle_count']) + ' bundles, '
          + str(len(record['graphics_bundles'])) + ' graphics-related; nm walks filesets: '
          + str(record['nm_fileset_support'].get('supported')))
    found = record.get('symbols') or {}
    if found.get('defined') or found.get('undefined'):
        print(found['identifier'] + ': ' + str(len(found['defined'])) + ' defined, '
              + str(len(found['undefined'])) + ' imported')
    elif found:
        print(found.get('identifier', '') + ': ' + str(found.get('reason')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
