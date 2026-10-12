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
import re
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
# An entitlement name is the one literal string from the analysed binary that
# this project records, because an independent driver cannot interoperate
# without declaring the same identifier. It is therefore bounded to that shape:
# reverse-DNS, printable, short. Anything else is reported by length only.
ENTITLEMENT_SHAPE = re.compile(r'^[A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+){2,}$')
ENTITLEMENT_MAX = 128
MINIMUM_ENTRIES = 2
MAX_TABLES = 64
# How far past a strictly anchored run to keep reading, flagging as it goes.
EXTENSION_LIMIT = 16


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

    def _entitlement(self, value: int) -> tuple[bool, str | None, dict]:
        """The entitlement identifier, if it is shaped like one."""
        if value == 0:
            return True, None, {}
        # The field is valid when it points into string data at all. Whether the
        # text is reproduced is a separate question, answered by its shape.
        if not self.image.is_cstring_address(value):
            return False, None, {}
        text = self.image.cstring_at(value, ENTITLEMENT_MAX + 1)
        if text is None:
            return True, None, {'entitlement_unreadable': True,
                                'entitlement_bytes': None,
                                'entitlement_reason': 'longer than '
                                                      + str(ENTITLEMENT_MAX)
                                                      + ' characters, or not printable'}
        if not ENTITLEMENT_SHAPE.match(text):
            return True, None, {'entitlement_unreadable': True,
                                'entitlement_bytes': len(text),
                                'entitlement_reason': 'not shaped like an identifier'}
        return True, text, {}

    def entry(self, blob: bytes, base: int, style: str,
              relocations: dict | None = None, strict: bool = True) -> dict | None:
        """One candidate entry, or None when it cannot be a dispatch entry.

        A linked image holds addresses in its pointer fields. A relocatable
        object holds zero there and names the target in a relocation, so both
        are accepted and the record says which one was read.

        Only structural facts can reject an entry: a pointer that resolves
        nowhere, an entitlement that is not a string. Zero padding and bounded
        counts are compiler habits, not ABI guarantees, so in strict mode they
        anchor a run and outside it they are recorded as anomalies instead of
        discarding a real table.
        """
        relocations = relocations or {}
        spec = self.styles[style]
        if base + spec['size'] > len(blob):
            return None
        offsets = spec['offsets']
        chunk = blob[base:base + spec['size']]
        padding_nonzero = any(any(chunk[offset:offset + size])
                              for offset, size in spec['padding'])
        if padding_nonzero and strict:
            return None
        function = struct.unpack_from('<Q', chunk, offsets['function'])[0]
        checks, implausible = {}, []
        for name, limit in (('checkScalarInputCount', self.scalar_max),
                            ('checkStructureInputSize', STRUCTURE_SIZE_LIMIT),
                            ('checkScalarOutputCount', self.scalar_max),
                            ('checkStructureOutputSize', STRUCTURE_SIZE_LIMIT)):
            value = struct.unpack_from('<I', chunk, offsets[name])[0]
            if not self._count_ok(value, limit):
                if strict:
                    return None
                implausible.append(name)
            checks[name] = value
        allow_async = None
        if 'allowAsync' in offsets:
            allow_async = chunk[offsets['allowAsync']]
            if allow_async not in (0, 1):
                if strict:
                    return None
                implausible.append('allowAsync')
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
        extra: dict = {}
        if entitlement_value == 0 and entitlement_relocation:
            entitlement_ok, entitlement = True, None
        else:
            entitlement_ok, entitlement, extra = self._entitlement(entitlement_value)
        if not entitlement_ok:
            return None
        return {
            'padding_nonzero': padding_nonzero,
            'implausible_fields': implausible,
            **extra,
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
        # xnu compares these fields for exact equality, so a value is a
        # required count or size - not an upper bound.
        if value == self.sentinel:
            return {'variable': True, 'value': value,
                    'meaning': 'any count or size is accepted at this position'}
        return {'variable': False, 'value': value,
                'meaning': 'exactly this count or size is required'}

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
                entry = self.entry(blob, cursor, style, relocations, strict=True)
                if entry is None:
                    break
                entries.append(entry)
                cursor += size
            if len(entries) >= minimum_entries:
                # A real table can continue past an entry whose padding or
                # counts look unusual; extend, flag, and let the reader judge.
                extensions = 0
                while extensions < EXTENSION_LIMIT:
                    entry = self.entry(blob, cursor, style, relocations, strict=False)
                    if entry is None or not (entry['handler']['address']
                                             or entry['handler']['symbol']):
                        break
                    entry['anchored'] = False
                    entries.append(entry)
                    cursor += size
                    extensions += 1
            if len(entries) >= minimum_entries and any(
                    item['handler']['address'] or item['handler']['symbol']
                    for item in entries):
                for index, entry in enumerate(entries):
                    # The recovered number is the array index. The selector a
                    # client passes may be this plus a base the subclass
                    # subtracts before dispatching, which only the call site
                    # shows, so the wire number stays unknown here.
                    entry['array_index'] = index
                    entry['wire_selector'] = None
                    entry.setdefault('anchored', True)
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
                    'selector_base': 'unknown: read the subclass externalMethod call site '
                                     'to learn whether a base is subtracted',
                    'pointer_form': self._pointer_form(entries, relocations),
                    'padding_anomalies': sum(1 for entry in entries
                                             if entry['padding_nonzero']),
                    'implausible_entries': sum(1 for entry in entries
                                               if entry['implausible_fields']),
                    'extended_entries': sum(1 for entry in entries
                                            if not entry.get('anchored', True)),
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


    def _pointer_form(self, entries: list[dict], relocations: dict) -> str:
        """How this image stores the pointers — detected, not assumed."""
        kinds = {entry['handler']['kind'] for entry in entries}
        kinds.discard('null')
        if kinds == {'relocation'}:
            return 'classic_relocations'
        if kinds == {'executable'}:
            return 'rebased_addresses'
        if kinds == {'unchecked'}:
            return 'unverifiable: no executable section in this image'
        if not kinds:
            return 'all_null'
        if self.image.has_chained_fixups:
            return 'mixed_with_chained_fixups_present'
        return 'mixed: ' + ', '.join(sorted(kinds))


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
    if table.get('padding_anomalies'):
        score -= 1
        reasons.append(str(table['padding_anomalies']) + ' entries carry non-zero padding, '
                       'which is unusual but not forbidden')
    if table.get('implausible_entries'):
        score -= 1
        reasons.append(str(table['implausible_entries']) + ' entries hold counts outside the '
                       'documented bounds')
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
            plan.append({'array_index': entry['array_index'], 'shape': shape,
                         'expect': 'kIOReturnNotPrivileged',
                         'expect_value': codes['kIOReturnNotPrivileged']['hex'],
                         'handler_runs': False,
                         'note': 'entitlement is checked after all four shape checks, so this '
                                 'confirms the shape with no side effect'})
        elif not entry['handler']['address']:
            plan.append({'array_index': entry['array_index'], 'shape': shape,
                         'expect': 'kIOReturnNoCompletion',
                         'expect_value': codes['kIOReturnNoCompletion']['hex'],
                         'handler_runs': False,
                         'note': 'null handler: the driver dispatches this itself'})
        else:
            plan.append({'array_index': entry['array_index'], 'shape': shape,
                         'expect': 'kIOReturnBadArgument for every wrong shape',
                         'expect_value': codes['kIOReturnBadArgument']['hex'],
                         'handler_runs': True,
                         'note': 'confirming the matching shape would run the handler; probe '
                                 'only wrong shapes unless the effect is understood'})
    return plan


def correlate(record: dict, callsite: dict) -> dict:
    """Let the call site decide what the table alone could not.

    A table gives entries; the driver's own override gives how many it believes
    there are and which number a client passes. Matching the two turns
    `array_index` into a wire selector and either confirms the scanned run or
    shows that it over-read.
    """
    functions = []
    for function in callsite.get('functions', []):
        conclusions = function.get('conclusions') or {}
        functions.append({
            'symbol': function['symbol'],
            'entry_count': conclusions.get('entry_count', {}).get('value'),
            'selector_base': conclusions.get('selector_base', {}).get('value'),
            'table_symbols': conclusions.get('table_symbols', []),
            'dispatch_style': conclusions.get('dispatch_style'),
        })
    for table in record.get('tables', []):
        match = None
        for function in functions:
            if table['symbol_at_table'] and table['symbol_at_table'] in function['table_symbols']:
                match = function
                break
        if match is None:
            for function in functions:
                if function['entry_count'] == table['entry_count']:
                    match = function
                    break
        if match is None:
            table['call_site'] = {'matched': False,
                                  'reason': 'no override names this array or agrees with its '
                                            'length'}
            continue
        base = match['selector_base']
        table['call_site'] = {
            'matched': True, 'symbol': match['symbol'],
            'matched_by': 'array symbol' if table['symbol_at_table'] in match['table_symbols']
            else 'entry count',
            'entry_count': match['entry_count'],
            'selector_base': base,
            'dispatch_style': match['dispatch_style'],
            'entry_count_agrees': match['entry_count'] == table['entry_count'],
            'scan_over_read': (match['entry_count'] is not None
                               and match['entry_count'] < table['entry_count']),
        }
        if base is not None:
            table['selector_base'] = base
            for entry in table['entries']:
                entry['wire_selector'] = entry['array_index'] + base
    record['call_site_correlated'] = bool(functions)
    return record


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
    return {
        'generated_by': 'tools/abi_dispatch.py',
        'binary': {'path': str(path), 'bundle': bundle,
                   'sha256': macho.digest(path)},
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
        'literal_content': 'The only literal string taken from the analysed binary is an '
                           'entitlement identifier, and only when it is shaped like one '
                           '(reverse-DNS, printable, at most ' + str(ENTITLEMENT_MAX)
                           + ' characters). An independent driver cannot interoperate without '
                             'declaring the same identifier; anything else resolving at that '
                             'pointer is recorded by length alone.',
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
    parser.add_argument('--with-callsite', action='store_true',
                        help="also read the driver's own externalMethod override, which "
                             'decides the wire selectors and the authoritative entry count')
    arguments = parser.parse_args()

    macho = load('abi_macho', 'tools/abi_macho.py')
    if arguments.list_bundles:
        data, _handle = macho.map_file(arguments.binary)
        image = macho.open_image(data, str(arguments.binary))
        for entry in image.fileset_entries:
            print(entry['identifier'])
        print(str(len(image.fileset_entries)) + ' bundles in ' + str(arguments.binary))
        return 0

    record = recover(arguments.binary, arguments.bundle, arguments.minimum_entries)
    if arguments.with_callsite:
        callsite_module = load('abi_callsite', 'tools/abi_callsite.py')
        try:
            record['call_site_record'] = callsite_module.read(arguments.binary,
                                                              arguments.bundle, None)
            correlate(record, record['call_site_record'])
        except Exception as error:
            record['call_site_record'] = {'performed': False,
                                          'reason': type(error).__name__ + ': '
                                                    + str(error)[:200]}
    if arguments.out:
        policy = load('abi_policy', 'tools/abi_policy.py')
        policy.write_record(arguments.out, record, 'recovered dispatch tables')
        print('Wrote ' + str(arguments.out))
    print(str(record['table_count']) + ' candidate table(s) in '
          + record['image']['filetype_name'] + ' ' + record['image']['architecture'])
    for table in record['tables'][:8]:
        line = ('  ' + table['section'] + ' @ ' + table['address_hex'] + '  '
                + str(table['entry_count']) + ' × ' + str(table['entry_bytes']) + 'B  '
                + table['style'] + '  confidence ' + table['confidence']['level']
                + ('  (' + table['symbol_at_table'] + ')' if table['symbol_at_table'] else ''))
        site = table.get('call_site') or {}
        if site.get('matched'):
            line += ('  base ' + str(site['selector_base'])
                     + ('  count agrees' if site['entry_count_agrees']
                        else '  call site says ' + str(site['entry_count'])))
        print(line)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
