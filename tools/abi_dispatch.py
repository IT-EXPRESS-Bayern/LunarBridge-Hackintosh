# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Recover a driver's external-method table: the ABI our own driver must offer.

A user client's dispatch table is a static array of IOExternalMethodDispatch or
IOExternalMethodDispatch2022 in a read-only data section. Every field in it is
data, not code, so the table can be read without interpreting a single
instruction: selector by index, the four argument checks, the asynchronous flag,
the entitlement string, and the handler's address — which the symbol table names
when symbols are present.

The structure layout, the sentinel values and the bounds all come from
tools/abi_contract.py, which parses them out of pinned xnu source. Nothing in
this scanner is a magic number, so when Apple changes the structure the contract
changes with it and this tool follows.

    python3 tools/abi_dispatch.py --binary <kext binary> --out dispatch.json
    python3 tools/abi_dispatch.py --binary <collection.kc> \\
        --bundle com.apple.driver.AppleIntelICLGraphics --out dispatch.json

Output is names, numbers and addresses. No instruction byte is read, no listing
is produced, and no part of the analysed binary is reproduced.
"""
from __future__ import annotations

import argparse
import importlib.util
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Sections where a static const array of dispatch entries can land, in the order
# a compiler is most likely to have placed it.
CANDIDATE_SECTIONS = (('__DATA_CONST', '__const'), ('__DATA', '__const'),
                      ('__TEXT', '__const'), ('__DATA_CONST', '__data'),
                      ('__CONST', '__constdata'))
# A structure size above this is not an argument check but a coincidence.
STRUCTURE_SIZE_LIMIT = 1 << 24
MINIMUM_ENTRIES = 2
MAX_TABLES = 64


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def field_offsets(structure: dict) -> dict:
    """Name to offset, from the contract's computed layout."""
    return {field['name']: field['offset'] for field in structure['layout']['fields']
            if field['type'] is not None}


def padding_ranges(structure: dict) -> list[tuple[int, int]]:
    return [(field['offset'], field['bytes'])
            for field in structure['layout']['fields'] if field['type'] is None]


class Scanner:
    """Finds dispatch tables in one image, using the published contract."""

    def __init__(self, image, contract: dict):
        self.image = image
        self.contract = contract
        self.sentinel = contract['sentinels']['kIOUCVariableStructureSize']['value']
        self.scalar_max = contract['sentinels']['kIOExternalMethodScalarInputCountMax']['value']
        self.styles = {}
        for style, name in (('2022', 'IOExternalMethodDispatch2022'),
                            ('legacy', 'IOExternalMethodDispatch')):
            structure = contract['dispatch_structures'][name]
            self.styles[style] = {
                'structure': name,
                'size': structure['layout']['size'],
                'offsets': field_offsets(structure),
                'padding': padding_ranges(structure),
            }
        self.symbol_index = image.address_index()
        self.executable = image.executable_ranges()

    def _count_ok(self, value: int, limit: int) -> bool:
        return value == self.sentinel or value <= limit

    def _pointer_ok(self, value: int) -> tuple[bool, str]:
        if value == 0:
            return True, 'null'
        if self.image.is_executable_address(value):
            return True, 'executable'
        if not self.executable:
            # Nothing in this image is marked executable, so an address cannot be
            # checked against code. Say that rather than accept it silently.
            return True, 'unchecked'
        if self.image.has_chained_fixups and self.image.section_of_address(value) is None:
            return False, 'chained_or_foreign'
        return False, 'not_executable'

    def _entitlement(self, value: int) -> tuple[bool, str | None]:
        if value == 0:
            return True, None
        text = self.image.cstring_at(value)
        if text is None:
            return False, None
        return True, text

    def entry(self, blob: bytes, base: int, style: str,
              relocations: dict | None = None) -> dict | None:
        """One candidate entry, or None when it cannot be a dispatch entry.

        A linked image holds addresses in its pointer fields. A relocatable
        object holds zero there and names the target in a relocation, so both
        are accepted and the record says which one was read.
        """
        relocations = relocations or {}
        spec = self.styles[style]
        if base + spec['size'] > len(blob):
            return None
        offsets = spec['offsets']
        chunk = blob[base:base + spec['size']]
        for offset, size in spec['padding']:
            if any(chunk[offset:offset + size]):
                return None
        function = struct.unpack_from('<Q', chunk, offsets['function'])[0]
        checks = {}
        for name, limit in (('checkScalarInputCount', self.scalar_max),
                            ('checkStructureInputSize', STRUCTURE_SIZE_LIMIT),
                            ('checkScalarOutputCount', self.scalar_max),
                            ('checkStructureOutputSize', STRUCTURE_SIZE_LIMIT)):
            value = struct.unpack_from('<I', chunk, offsets[name])[0]
            if not self._count_ok(value, limit):
                return None
            checks[name] = value
        allow_async = None
        if 'allowAsync' in offsets:
            allow_async = chunk[offsets['allowAsync']]
            if allow_async not in (0, 1):
                return None
        entitlement_value = 0
        if 'checkEntitlement' in offsets:
            entitlement_value = struct.unpack_from('<Q', chunk, offsets['checkEntitlement'])[0]
        handler_relocation = relocations.get(base + offsets['function'])
        if function == 0 and handler_relocation:
            pointer_ok, pointer_kind = True, 'relocation'
        else:
            pointer_ok, pointer_kind = self._pointer_ok(function)
        if not pointer_ok:
            return None
        entitlement_relocation = None
        if 'checkEntitlement' in offsets:
            entitlement_relocation = relocations.get(base + offsets['checkEntitlement'])
        if entitlement_value == 0 and entitlement_relocation:
            entitlement_ok, entitlement = True, None
        else:
            entitlement_ok, entitlement = self._entitlement(entitlement_value)
        if not entitlement_ok:
            return None
        return {
            'checks': {
                'scalar_input_count': self.describe_check(checks['checkScalarInputCount']),
                'structure_input_size': self.describe_check(checks['checkStructureInputSize']),
                'scalar_output_count': self.describe_check(checks['checkScalarOutputCount']),
                'structure_output_size': self.describe_check(checks['checkStructureOutputSize']),
            },
            'allow_async': bool(allow_async) if allow_async is not None else None,
            'entitlement': entitlement,
            'entitlement_symbol': entitlement_relocation,
            'handler': {'address': function, 'address_hex': hex(function),
                        'kind': pointer_kind,
                        'symbol': handler_relocation
                        or self.symbol_index.get(function)},
        }

    def describe_check(self, value: int) -> dict:
        if value == self.sentinel:
            return {'variable': True, 'value': value,
                    'meaning': 'any count or size is accepted at this position'}
        return {'variable': False, 'value': value}

    def run(self, minimum_entries: int = MINIMUM_ENTRIES) -> list[dict]:
        tables = []
        for segment, name in CANDIDATE_SECTIONS:
            section = self.image.section(segment, name)
            if not section or not section['bytes']:
                continue
            try:
                blob = self.image.read_section(section)
            except Exception:
                continue
            relocations = {entry['offset']: entry['symbol']
                           for entry in self.image.relocations(section)
                           if entry.get('symbol')}
            for style in ('2022', 'legacy'):
                tables.extend(self._scan_section(section, blob, style, minimum_entries,
                                                 relocations))
        tables.sort(key=lambda table: (-table['entry_count'], table['address']))
        return tables[:MAX_TABLES]

    def _scan_section(self, section: dict, blob: bytes, style: str,
                      minimum_entries: int, relocations: dict) -> list[dict]:
        spec = self.styles[style]
        size = spec['size']
        found, position = [], 0
        while position + size * minimum_entries <= len(blob):
            entries = []
            cursor = position
            while True:
                entry = self.entry(blob, cursor, style, relocations)
                if entry is None:
                    break
                entries.append(entry)
                cursor += size
            if len(entries) >= minimum_entries and any(
                    item['handler']['address'] or item['handler']['symbol']
                    for item in entries):
                for index, entry in enumerate(entries):
                    entry['selector'] = index
                address = section['address'] + position
                found.append({
                    'style': style,
                    'structure': spec['structure'],
                    'section': section['segment'] + ',' + section['name'],
                    'file_offset': section['offset'] + position,
                    'address': address,
                    'address_hex': hex(address),
                    'entry_bytes': size,
                    'entry_count': len(entries),
                    'symbol_at_table': self.symbol_index.get(address),
                    'named_handlers': sum(1 for entry in entries if entry['handler']['symbol']),
                    'entitlement_gated': sum(1 for entry in entries
                                             if entry['entitlement']
                                             or entry['entitlement_symbol']),
                    'entries': entries,
                })
                position = cursor
                continue
            position += 8
        return found


def confidence(table: dict) -> dict:
    """How much this table should be trusted, and why."""
    reasons = []
    score = 0
    if table['entry_count'] >= 4:
        score += 2
        reasons.append('a run of ' + str(table['entry_count']) + ' consecutive valid entries')
    if table['symbol_at_table']:
        score += 2
        reasons.append('a symbol names the array itself')
    if table['named_handlers']:
        score += 2
        reasons.append(str(table['named_handlers']) + ' handlers resolve to symbol names')
    if table['entitlement_gated']:
        score += 1
        reasons.append(str(table['entitlement_gated']) + ' entries carry a readable '
                       'entitlement string')
    if table['style'] == '2022':
        score += 1
        reasons.append('the 2022 structure has interior padding, which a coincidence rarely '
                       'satisfies')
    level = 'high' if score >= 5 else 'medium' if score >= 3 else 'low'
    return {'level': level, 'score': score, 'reasons': reasons,
            'caveat': 'a table is a hypothesis until the dispatch call site is read or a live '
                      'probe answers as this table predicts'}


def probe_plan(table: dict, contract: dict) -> list[dict]:
    """How to confirm this table on a live driver without running a handler."""
    codes = contract['return_codes']
    plan = []
    for entry in table['entries']:
        checks = entry['checks']
        shape = {
            'scalar_input_count': checks['scalar_input_count']['value']
            if not checks['scalar_input_count']['variable'] else 'any',
            'structure_input_size': checks['structure_input_size']['value']
            if not checks['structure_input_size']['variable'] else 'any',
            'scalar_output_count': checks['scalar_output_count']['value']
            if not checks['scalar_output_count']['variable'] else 'any',
            'structure_output_size': checks['structure_output_size']['value']
            if not checks['structure_output_size']['variable'] else 'any',
        }
        if entry['entitlement']:
            plan.append({'selector': entry['selector'], 'shape': shape,
                         'expect': 'kIOReturnNotPrivileged',
                         'expect_value': codes['kIOReturnNotPrivileged']['hex'],
                         'handler_runs': False,
                         'note': 'entitlement is checked after all four shape checks, so this '
                                 'confirms the shape with no side effect'})
        elif not entry['handler']['address']:
            plan.append({'selector': entry['selector'], 'shape': shape,
                         'expect': 'kIOReturnNoCompletion',
                         'expect_value': codes['kIOReturnNoCompletion']['hex'],
                         'handler_runs': False,
                         'note': 'null handler: the driver dispatches this itself'})
        else:
            plan.append({'selector': entry['selector'], 'shape': shape,
                         'expect': 'kIOReturnBadArgument for every wrong shape',
                         'expect_value': codes['kIOReturnBadArgument']['hex'],
                         'handler_runs': True,
                         'note': 'confirming the matching shape would run the handler; probe '
                                 'only wrong shapes unless the effect is understood'})
    return plan


def recover(path: Path, bundle: str | None, minimum_entries: int = MINIMUM_ENTRIES) -> dict:
    macho = load('abi_macho', 'tools/abi_macho.py')
    contract_module = load('abi_contract', 'tools/abi_contract.py')
    compare = load('compare_interfaces', 'tools/compare_interfaces.py')
    contract = contract_module.contract(compare.read_reference)

    image = macho.load_image(path, bundle)
    scanner = Scanner(image, contract)
    tables = scanner.run(minimum_entries)
    for table in tables:
        table['confidence'] = confidence(table)
        table['probe_plan'] = probe_plan(table, contract)
    import hashlib
    return {
        'generated_by': 'tools/abi_dispatch.py',
        'binary': {'path': str(path), 'bundle': bundle,
                   'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()},
        'image': image.describe(),
        'contract_pins': contract['pins'],
        'entry_sizes': {style: spec['size'] for style, spec in scanner.styles.items()},
        'tables': tables,
        'table_count': len(tables),
        'collection_bundles': sorted(entry['identifier']
                                     for entry in image.fileset_entries)[:512]
        if image.fileset_entries else [],
        'limits': 'Data fields only. Handler addresses are recorded, never their code; a '
                  'recovered table is a hypothesis about the interface, not a copy of it.',
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--binary', required=True, type=Path,
                        help='a kext binary, or a kernel collection with --bundle')
    parser.add_argument('--bundle', help='bundle identifier inside a kernel collection')
    parser.add_argument('--out', type=Path, help='write the recovered tables as JSON')
    parser.add_argument('--minimum-entries', type=int, default=MINIMUM_ENTRIES,
                        help='shortest run of entries to report as a table')
    parser.add_argument('--list-bundles', action='store_true',
                        help='list the bundles in a collection and stop')
    arguments = parser.parse_args()

    macho = load('abi_macho', 'tools/abi_macho.py')
    if arguments.list_bundles:
        image = macho.open_image(arguments.binary.read_bytes(), str(arguments.binary))
        for entry in image.fileset_entries:
            print(entry['identifier'])
        print(str(len(image.fileset_entries)) + ' bundles in ' + str(arguments.binary))
        return 0

    record = recover(arguments.binary, arguments.bundle, arguments.minimum_entries)
    if arguments.out:
        policy = load('abi_policy', 'tools/abi_policy.py')
        policy.write_record(arguments.out, record, 'recovered dispatch tables')
        print('Wrote ' + str(arguments.out))
    print(str(record['table_count']) + ' candidate table(s) in '
          + record['image']['filetype_name'] + ' ' + record['image']['architecture'])
    for table in record['tables'][:8]:
        print('  ' + table['section'] + ' @ ' + table['address_hex'] + '  '
              + str(table['entry_count']) + ' × ' + str(table['entry_bytes']) + 'B  '
              + table['style'] + '  confidence ' + table['confidence']['level']
              + ('  (' + table['symbol_at_table'] + ')' if table['symbol_at_table'] else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
