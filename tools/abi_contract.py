# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""The published ground truth that makes a recovered user-client ABI readable.

Everything here is parsed out of hash-pinned Apple open source — xnu's
IOUserClient header and implementation, OSMetaClass, IOReturn and mach/error —
so no value in it is a guess, and a changed upstream line fails a test instead
of silently moving a constant.

Three things come out of it:

1. The dispatch structures' exact x86_64 layout. A static table of these in a
   compiled driver has a recognisable shape, which is what the static recovery
   scans for.
2. The validation contract xnu applies to every external method call: which
   check runs in what order, and which return code each failure produces.
3. From that contract, the probe oracle: what an observed return code proves
   about a selector on a live driver. One entry matters most — an entitlement
   is checked only after all four shape checks have passed, so a
   kIOReturnNotPrivileged answer reveals a selector's exact argument shape
   without the handler ever running.

The oracle is why much of this ABI can be recovered by observation rather than
disassembly, and why that observation can be bounded and non-destructive.
"""
from __future__ import annotations

import re

# x86_64 System V: size and alignment of the field types these structures use.
TYPES = {
    'pointer': (8, 8),
    'uint64_t': (8, 8),
    'uint32_t': (4, 4),
    'unsigned int': (4, 4),
    'uint8_t': (1, 1),
}
POINTER_TYPES = ('IOExternalMethodAction', 'const char*', 'const char *', 'void *',
                 'struct ExpansionData *', 'const OSMetaClass *', 'const OSSymbol *')
IOUSERCLIENT_H = 'macos-tahoe/xnu-12377.121.6/iokit/IOKit/IOUserClient.h'
IOUSERCLIENT_CPP = 'macos-tahoe/xnu-12377.121.6/iokit/Kernel/IOUserClient.cpp'
OSMETACLASS_H = 'macos-tahoe/xnu-12377.121.6/libkern/libkern/c++/OSMetaClass.h'
IORETURN_H = 'macos-tahoe/xnu-12377.121.6/iokit/IOKit/IOReturn.h'
MACH_ERROR_H = 'macos-tahoe/xnu-12377.121.6/osfmk/mach/error.h'
COMMENT = re.compile(r'/\*.*?\*/|//[^\n]*', re.S)


class ContractError(Exception):
    """A pinned source no longer says what this module needs it to say."""


def line_of(source: str, text: str) -> int:
    index = source.find(text)
    if index < 0:
        raise ContractError('text not found in pinned source: ' + text[:60])
    return source.count('\n', 0, index) + 1


def cite(relative: str, source: str, text: str) -> str:
    return relative + ':' + str(line_of(source, text))


def layout(fields: list[tuple[str, str]]) -> dict:
    """Offsets, size and padding of a C structure under the x86_64 System V ABI."""
    offset, alignment, placed = 0, 1, []
    for type_name, name in fields:
        kind = 'pointer' if (type_name in POINTER_TYPES or type_name.endswith('*')) \
            else type_name
        if kind not in TYPES:
            raise ContractError('unknown field type: ' + type_name)
        size, align = TYPES[kind]
        padding = (-offset) % align
        if padding:
            placed.append({'name': '(padding)', 'offset': offset, 'bytes': padding,
                           'type': None})
            offset += padding
        placed.append({'name': name, 'offset': offset, 'bytes': size, 'type': type_name})
        offset += size
        alignment = max(alignment, align)
    tail = (-offset) % alignment
    if tail:
        placed.append({'name': '(tail padding)', 'offset': offset, 'bytes': tail,
                       'type': None})
        offset += tail
    return {'fields': placed, 'size': offset, 'alignment': alignment,
            'abi': 'x86_64 System V: each field at the next multiple of its alignment, '
                   'structure size rounded up to its strongest alignment'}


def declared_fields(body: str, label: str) -> list[tuple[str, str]]:
    """Data members of a declaration body, as (type, name), in source order.

    Multi-word types (`struct ExpansionData *`, `unsigned int`) and storage
    qualifiers are what make this more than a split, and function declarations,
    friends and preprocessor lines are what make it a filter.
    """
    fields = []
    for line in COMMENT.sub('', body).splitlines():
        line = line.strip()
        if not line or not line.endswith(';') or '(' in line:
            continue
        if line.startswith(('friend', 'static', '#', '}', 'typedef', 'using', 'public',
                            'private', 'protected')):
            continue
        declaration = line[:-1].strip()
        for qualifier in ('mutable ', 'volatile '):
            if declaration.startswith(qualifier):
                declaration = declaration[len(qualifier):]
        match = re.match(r'^(.*?[\s*])(\w+)$', declaration)
        if not match:
            raise ContractError('unparsed member in ' + label + ': ' + line)
        type_name = ' '.join(match.group(1).split())
        fields.append((type_name, match.group(2)))
    if not fields:
        raise ContractError('no data members parsed for ' + label)
    return fields


def struct_fields(source: str, name: str) -> list[tuple[str, str]]:
    match = re.search(r'struct\s+' + name + r'\s*\{(.*?)\n\};', source, re.S)
    if not match:
        raise ContractError('struct not found: ' + name)
    return declared_fields(match.group(1), name)


def define_value(source: str, name: str) -> int:
    match = re.search(r'#define\s+' + name + r'\s+(?:\(([^)]*)\)|(0x[0-9a-fA-F]+|\d+))',
                      source)
    if not match:
        raise ContractError('define not found: ' + name)
    text = (match.group(1) or match.group(2)).strip()
    return int(text, 0)


def enum_value(source: str, name: str) -> int:
    match = re.search(name + r'\s*=\s*(0x[0-9a-fA-F]+|\d+)', source)
    if not match:
        raise ContractError('enum constant not found: ' + name)
    return int(match.group(1), 0)


def return_codes(ioreturn: str, error: str, names: tuple) -> dict:
    """Compute kIOReturn* numbers from the macros, rather than hardcoding them."""
    system_shift = re.search(r'#define\s+err_system\(x\)\s+.*?<<\s*(\d+)', error)
    sub_shift = re.search(r'#define\s+err_sub\(x\)\s+.*?<<\s*(\d+)', error)
    if not system_shift or not sub_shift:
        raise ContractError('err_system / err_sub no longer shift a constant')
    system = re.search(r'#define\s+sys_iokit\s+err_system\((0x[0-9a-fA-F]+)\)', ioreturn)
    sub = re.search(r'#define\s+sub_iokit_common\s+err_sub\((\d+)\)', ioreturn)
    if not system or not sub:
        raise ContractError('sys_iokit / sub_iokit_common no longer defined as expected')
    base = ((int(system.group(1), 16) & 0x3F) << int(system_shift.group(1))) | \
           ((int(sub.group(1)) & 0xFFF) << int(sub_shift.group(1)))
    codes = {'kIOReturnSuccess': {
        'value': 0, 'hex': '0x00000000', 'code': 0,
        'citation': cite(IORETURN_H, ioreturn, '#define kIOReturnSuccess'),
        'note': 'KERN_SUCCESS, which is zero; it is not an iokit_common_err code'}}
    for name in names:
        if name in codes:
            continue
        match = re.search(r'#define\s+' + name + r'\s+iokit_common_err\((0x[0-9a-fA-F]+)\)',
                          ioreturn)
        if not match:
            raise ContractError('return code not found: ' + name)
        value = base | int(match.group(1), 16)
        codes[name] = {'value': value, 'hex': '0x%08x' % value,
                       'code': int(match.group(1), 16),
                       'citation': cite(IORETURN_H, ioreturn, '#define ' + name)}
    return codes


def dispatch_contract(implementation: str) -> dict:
    """The ordered checks xnu applies, read out of dispatchExternalMethod itself."""
    body = re.search(r'IOUserClient2022::dispatchExternalMethod\(.*?\n\}', implementation, re.S)
    if not body:
        raise ContractError('IOUserClient2022::dispatchExternalMethod not found')
    text = body.group(0)
    # checkEntitlement appears twice, as the test and as the argument of the
    # entitlement call, so keep each field at its first occurrence only.
    order, seen = [], set()
    for name in re.findall(r'dispatch->(\w+)', text):
        if name not in seen:
            seen.add(name)
            order.append(name)
    checks = [name for name in order if name.startswith('check')]
    expected = ['checkScalarInputCount', 'checkStructureInputSize', 'checkScalarOutputCount',
                'checkStructureOutputSize', 'checkEntitlement']
    if checks != expected:
        raise ContractError('the check order changed upstream: ' + ', '.join(checks))
    if not re.search(r'if \(selector >= dispatchArrayCount\) \{\s*return kIOReturnBadArgument;',
                     text):
        raise ContractError('the out-of-range selector answer changed upstream')
    if not re.search(r'if \(args->asyncWakePort && !dispatch->allowAsync\) \{\s*'
                     r'return kIOReturnBadArgument;', text):
        raise ContractError('the asynchronous-port answer changed upstream')
    if not re.search(r'if \(dispatch->checkEntitlement\) \{\s*'
                     r'if \(!IOCurrentTaskHasEntitlement\(dispatch->checkEntitlement\)\) \{\s*'
                     r'return kIOReturnNotPrivileged;', text):
        raise ContractError('the entitlement answer changed upstream')
    if not re.search(r'\} else \{\s*err = kIOReturnNoCompletion;', text):
        raise ContractError('the empty-handler answer changed upstream')
    return {
        'function': 'IOUserClient2022::dispatchExternalMethod',
        'citation': IOUSERCLIENT_CPP + ':' +
                    str(implementation.count('\n', 0, body.start()) + 1),
        'steps': [
            {'step': 1, 'condition': 'selector >= dispatchArrayCount',
             'answer': 'kIOReturnBadArgument'},
            {'step': 2, 'condition': 'checkScalarInputCount mismatch',
             'answer': 'kIOReturnBadArgument'},
            {'step': 3, 'condition': 'checkStructureInputSize mismatch',
             'answer': 'kIOReturnBadArgument'},
            {'step': 4, 'condition': 'checkScalarOutputCount mismatch',
             'answer': 'kIOReturnBadArgument'},
            {'step': 5, 'condition': 'checkStructureOutputSize mismatch',
             'answer': 'kIOReturnBadArgument'},
            {'step': 6, 'condition': 'asyncWakePort set while allowAsync is false',
             'answer': 'kIOReturnBadArgument'},
            {'step': 7, 'condition': 'checkEntitlement set and the task lacks it',
             'answer': 'kIOReturnNotPrivileged'},
            {'step': 8, 'condition': 'function is null',
             'answer': 'kIOReturnNoCompletion'},
            {'step': 9, 'condition': 'otherwise', 'answer': 'the handler runs'},
        ],
        'variable_size_escape': 'a check field equal to kIOUCVariableStructureSize accepts '
                                'any count or size at that position',
    }


def probe_oracle(contract: dict) -> list[dict]:
    """What an observed answer proves — and which probes never reach a handler."""
    return [
        {'observation': 'kIOReturnNotPrivileged',
         'proves': 'the selector exists, every one of its four shape checks passed, and it is '
                   'entitlement-gated',
         'handler_ran': False,
         'use': 'the one answer that confirms an exact argument shape with no side effect: '
                'sweep shapes against an entitlement-gated selector and read the shape off '
                'the first NotPrivileged'},
        {'observation': 'kIOReturnNoCompletion',
         'proves': 'the selector exists with a null function pointer and the shape matched; '
                   'the driver dispatches it itself',
         'handler_ran': False,
         'use': 'marks table entries the driver handles outside the table'},
        {'observation': 'kIOReturnBadArgument',
         'proves': 'either the selector is out of range or one of the four shape checks or the '
                   'async rule failed — the two are indistinguishable from the answer alone',
         'handler_ran': False,
         'use': 'bounds the table only in combination with a positive answer at a higher '
                'selector; never read as "this selector does not exist"'},
        {'observation': 'kIOReturnUnsupported',
         'proves': 'the client reached the legacy, non-table path and has no method at this '
                   'index, so this client does not use a 2022 dispatch array',
         'handler_ran': False,
         'use': 'identifies which dispatch style a client uses before any further probing'},
        {'observation': 'anything else',
         'proves': 'the shape matched and the handler executed',
         'handler_ran': True,
         'use': 'informative but no longer side-effect free: outside a probe whose effect on '
                'this selector is already understood, stop here'},
        {'observation': 'kIOReturnNotPermitted on the legacy path',
         'proves': 'a foreground-only method reached by a task the window server denies',
         'handler_ran': False,
         'use': 'context only; cited because the legacy path can answer this before any '
                'method runs',
         'citation': contract['citation']},
    ]


def metaclass_layout(header: str) -> dict:
    """Where a compiled OSMetaClass record keeps the facts we need from it."""
    match = re.search(r'class OSMetaClass : public OSMetaClassBase\s*\{(.*?)\npublic:', header,
                      re.S)
    if not match:
        raise ContractError('the OSMetaClass private section moved')
    order = declared_fields(match.group(1), 'OSMetaClass')
    names = [name for _, name in order]
    expected = ['reserved', 'superClassLink', 'className', 'classSize', 'instanceCount']
    if names != expected:
        raise ContractError('OSMetaClass field order changed: ' + ', '.join(names))
    fields = [('pointer', 'vtable'), ('pointer', 'reserved'), ('pointer', 'superClassLink'),
              ('pointer', 'className'), ('unsigned int', 'classSize'),
              ('unsigned int', 'instanceCount')]
    computed = layout(fields)
    return {
        'citation': cite(OSMETACLASS_H, header, 'const OSMetaClass *superClassLink'),
        'declared_order': names,
        'assumed': 'OSMetaClassBase contributes one vtable pointer and no data member, so the '
                   'first declared field follows it at offset 8. This is an assumption about '
                   'the compiled layout, not a statement from the header.',
        'layout': computed,
        'reads': {
            'class_name': 'follow className to an OSSymbol and read its string',
            'super_class': 'follow superClassLink to the superclass metaclass, across bundles',
            'instance_size': 'classSize is the one struct-layout fact a binary gives up '
                             'directly: how large an instance of that class is',
        },
        'self_checks': [
            'every recovered classSize is a multiple of 8 and between 64 bytes and 64 KiB',
            'every superClassLink chain terminates at a metaclass whose className is OSObject',
            'the number of recovered records equals the number of gMetaClass symbols, where '
            'symbols are present',
        ],
    }


def contract(read_reference) -> dict:
    """Build the whole published contract from the pinned sources."""
    header = read_reference(IOUSERCLIENT_H)
    implementation = read_reference(IOUSERCLIENT_CPP)
    metaclass_header = read_reference(OSMETACLASS_H)
    ioreturn = read_reference(IORETURN_H)
    error = read_reference(MACH_ERROR_H)

    dispatch_tables = {}
    for name in ('IOExternalMethodDispatch', 'IOExternalMethodDispatch2022'):
        fields = struct_fields(header, name)
        computed = layout(fields)
        padding = [field for field in computed['fields'] if field['type'] is None]
        dispatch_tables[name] = {
            'citation': cite(IOUSERCLIENT_H, header, 'struct ' + name + ' {'),
            'declared_fields': [{'type': type_name, 'name': field}
                                for type_name, field in fields],
            'layout': computed,
            'scan_signature': {
                'entry_bytes': computed['size'],
                'interior_padding': [{'offset': field['offset'], 'bytes': field['bytes']}
                                     for field in padding],
                'note': 'interior padding bytes are zero in a static const table, which with '
                        'the bounded check fields is what a scanner matches on',
            },
        }
    sentinels = {
        'kIOUCVariableStructureSize': {
            'value': enum_value(header, 'kIOUCVariableStructureSize'),
            'citation': cite(IOUSERCLIENT_H, header, 'kIOUCVariableStructureSize')},
        'kIOExternalMethodScalarInputCountMax': {
            'value': enum_value(header, 'kIOExternalMethodScalarInputCountMax'),
            'citation': cite(IOUSERCLIENT_H, header,
                             'kIOExternalMethodScalarInputCountMax'),
            'documentary': True,
            'note': 'the header states these constants are documentary, because the '
                    'scalarInputCount and scalarOutputCount fields reflect the actual '
                    'number passed; xnu enforces only the per-selector equality checks, '
                    'so 16 is a scan heuristic and not a wire limit'},
        'kIOExternalMethodScalarOutputCountMax': {
            'value': enum_value(header, 'kIOExternalMethodScalarOutputCountMax'),
            'citation': cite(IOUSERCLIENT_H, header,
                             'kIOExternalMethodScalarOutputCountMax')},
    }
    codes = return_codes(ioreturn, error,
                         ('kIOReturnSuccess', 'kIOReturnBadArgument',
                          'kIOReturnNotPrivileged', 'kIOReturnNoCompletion',
                          'kIOReturnUnsupported', 'kIOReturnNotPermitted'))
    validation = dispatch_contract(implementation)
    return {
        'generated_by': 'tools/abi_contract.py',
        'pins': {'xnu': 'apple-oss-distributions/xnu xnu-12377.121.6'},
        'dispatch_structures': dispatch_tables,
        'sentinels': sentinels,
        'return_codes': codes,
        'validation': validation,
        'probe_oracle': probe_oracle(validation),
        'metaclass': metaclass_layout(metaclass_header),
        'scope': 'Published Apple open source, parsed. Describes the interface an independent '
                 'driver must satisfy and the answers a live driver gives; contains no part of '
                 'any closed Apple driver.',
    }


def main() -> int:
    import argparse
    import importlib.util
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('compare_interfaces',
                                                  root / 'tools/compare_interfaces.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, help='write the contract as JSON')
    arguments = parser.parse_args()
    record = contract(module.read_reference)
    if arguments.out:
        policy_spec = importlib.util.spec_from_file_location('abi_policy',
                                                             root / 'tools/abi_policy.py')
        policy = importlib.util.module_from_spec(policy_spec)
        policy_spec.loader.exec_module(policy)
        policy.write_record(arguments.out, record, 'published ABI contract')
        print('Wrote ' + str(arguments.out))
    else:
        print(json.dumps(record, indent=2))
    tables = record['dispatch_structures']
    print('IOExternalMethodDispatch2022: '
          + str(tables['IOExternalMethodDispatch2022']['layout']['size']) + ' bytes per entry; '
          + str(len(record['probe_oracle'])) + ' oracle rules; classSize at offset '
          + str(next(field['offset'] for field in record['metaclass']['layout']['fields']
                     if field['name'] == 'classSize')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
