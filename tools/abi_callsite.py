# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Read a dispatch call site for the two facts a table alone cannot give.

tools/abi_dispatch.py recovers a table's entries but has to leave two things
open, and says so: how many entries the driver itself believes the table has,
and whether the number a client passes is the array index or that index plus a
base the driver subtracts first. Both are decided in the subclass's own
`externalMethod` override — not in `IOUserClient2022::dispatchExternalMethod`,
which lives in the kernel and is only an imported symbol in a driver.

So this disassembles that one function and reads out:

    bounds check        `cmp $N, <selector>` before a conditional branch
    count argument      the immediate moved into the dispatchArrayCount argument
    selector base       an `add`/`sub` applied to the selector before dispatch
    table reference     the relocation or address at the instruction that loads
                        the array, and the symbol it names
    callee              which dispatch entry point is actually called

Two independent sources for the count — the bounds immediate and the count
argument — are better than one, so both are reported and their agreement is
stated rather than assumed.

The disassembly itself is a means, never an output: this emits numbers,
offsets, register names and symbol names. The listing is parsed and discarded,
and tools/abi_policy.py refuses any record that carries instruction text.

    python3 tools/abi_callsite.py --binary <kext binary> --out callsite.json
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The System V argument registers in order, 32-bit names as a count is passed.
ARGUMENT_REGISTERS_32 = ('edi', 'esi', 'edx', 'ecx', 'r8d', 'r9d')
ARGUMENT_REGISTERS_64 = ('rdi', 'rsi', 'rdx', 'rcx', 'r8', 'r9')
# Arithmetic on these is a frame, never a selector.
STACK_REGISTERS = ('rsp', 'esp', 'rbp', 'ebp')
# Immediates appear either as a two's-complement word (`$0xffffff00`) or with a
# sign (`$-0x2a`), depending on magnitude, so every pattern accepts both.
IMMEDIATE = r'\$(-?0x[0-9a-f]+|-?\d+)'
# `movl $imm, %reg`
MOVE_IMMEDIATE = re.compile(r'^mov[lq]?\s+' + IMMEDIATE + r',\s*%(\w+)')
# `cmpl $imm, %reg`
COMPARE_IMMEDIATE = re.compile(r'^cmp[lq]?\s+' + IMMEDIATE + r',\s*%(\w+)')
# `addl $imm, %reg` / `subl $imm, %reg`
ADJUST_IMMEDIATE = re.compile(r'^(add|sub)[lq]?\s+' + IMMEDIATE + r',\s*%(\w+)')
# A conditional branch that implements a bound.
BOUND_BRANCH = re.compile(r'^(ja|jae|jb|jbe|jg|jge|jl|jle)\b')
# `movq <disp>(%rip), %reg` or `leaq <disp>(%rip), %reg`
RIP_RELATIVE = re.compile(r'^(mov|lea)[lq]?\s+(-?0x[0-9a-f]+)?\(%rip\),\s*%(\w+)')
CALL = re.compile(r'^call[q]?\b')
INSTRUCTION_LINE = re.compile(r'^\s*([0-9a-f]+):\s+(.*?)\s*(?:##.*)?$')
MAX_FUNCTION_LINES = 4000


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DisassemblyUnavailable(Exception):
    """No disassembler on this machine, or it refused the binary."""


def disassembler() -> str:
    for name in ('llvm-objdump', 'objdump', 'otool'):
        path = shutil.which(name)
        if path:
            return path
    raise DisassemblyUnavailable('no llvm-objdump, objdump or otool in PATH')


def disassemble(binary: Path, symbol: str) -> list[tuple[int, str]]:
    """One function as (offset, instruction) pairs, with comments stripped."""
    tool = disassembler()
    name = Path(tool).name
    if name == 'otool':
        command = [tool, '-tV', str(binary)]
    else:
        command = [tool, '-d', '--no-show-raw-insn', '--x86-asm-syntax=att',
                   '--disassemble-symbols=' + symbol, str(binary)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    if result.returncode:
        raise DisassemblyUnavailable(name + ' exited ' + str(result.returncode) + ': '
                                     + result.stderr[:200])
    instructions = []
    started = name != 'otool'
    for line in result.stdout.splitlines()[:MAX_FUNCTION_LINES]:
        if name == 'otool' and symbol in line:
            started = True
            continue
        if not started:
            continue
        match = INSTRUCTION_LINE.match(line)
        if not match:
            continue
        instructions.append((int(match.group(1), 16), ' '.join(match.group(2).split())))
    if not instructions:
        raise DisassemblyUnavailable('no instructions parsed for ' + symbol)
    return instructions


def number(text: str) -> int:
    body = text[1:] if text.startswith('-') else text
    value = int(body, 16) if body.lower().startswith('0x') else int(body)
    return -value if text.startswith('-') else value


def as_signed32(value: int) -> int:
    return value - (1 << 32) if value >= (1 << 31) else value


def candidate_symbols(image, wanted: str | None) -> list[str]:
    """Defined text symbols that could hold a dispatch call site."""
    names = []
    for name, symbol in image.symbols.items():
        if symbol['type'] != 0x0E or not symbol['section']:
            continue
        if wanted:
            if wanted in name:
                names.append(name)
            continue
        if 'externalMethod' in name or 'ExternalMethod' in name:
            names.append(name)
    return sorted(names)


def read_function(binary: Path, image, symbol: str) -> dict:
    """Every fact this function's instructions state about its dispatch table."""
    instructions = disassemble(binary, symbol)
    relocations: dict = {}
    for section in image.sections:
        if not section['flags'] & 0x80000400:
            continue
        for entry in image.relocations(section):
            if entry.get('symbol'):
                relocations[entry['offset']] = entry['symbol']

    bounds, counts, adjustments, references, callees = [], [], [], [], []
    previous = None
    for offset, text in instructions:
        compare = COMPARE_IMMEDIATE.match(text)
        if compare:
            previous = {'offset': offset, 'immediate': number(compare.group(1)),
                        'register': compare.group(2)}
        elif BOUND_BRANCH.match(text) and previous is not None:
            # The mnemonic alone does not say which side is rejected: clang
            # compiles `if (selector >= 11) return` to `cmp $0xa` plus a branch
            # that jumps INTO the body. So both readings are kept - a limit of
            # N means N entries, a limit of N+1 means N + 1 - and agreement
            # with the count argument or the selector adjustment decides which.
            branch = text.split()[0]
            target = text.split()[-1] if text.split()[-1].startswith('0x') else None
            bounds.append({
                'at_offset': previous['offset'], 'branch_offset': offset,
                'branch': branch, 'immediate': previous['immediate'],
                'register': previous['register'],
                'branch_target': number(target) if target else None,
                'rejects_above': branch in ('ja', 'jae', 'jg', 'jge'),
                'implied_values': sorted({previous['immediate'],
                                          previous['immediate'] + 1}),
            })
            previous = None
        else:
            if not COMPARE_IMMEDIATE.match(text):
                previous = None
            move = MOVE_IMMEDIATE.match(text)
            if move and move.group(2) in ARGUMENT_REGISTERS_32 + ARGUMENT_REGISTERS_64:
                value = number(move.group(1))
                if 0 < value < 65536:
                    index = (ARGUMENT_REGISTERS_32 + ARGUMENT_REGISTERS_64).index(
                        move.group(2)) % len(ARGUMENT_REGISTERS_32)
                    counts.append({'at_offset': offset, 'immediate': value,
                                   'register': move.group(2),
                                   'argument_position': index + 1})
            adjust = ADJUST_IMMEDIATE.match(text)
            if adjust and adjust.group(3) not in STACK_REGISTERS:
                raw = number(adjust.group(2))
                signed = as_signed32(raw)
                subtracted = -signed if adjust.group(1) == 'add' else signed
                if subtracted:
                    adjustments.append({'at_offset': offset,
                                        'operation': adjust.group(1),
                                        'immediate': raw,
                                        'subtracted_from_selector': subtracted,
                                        'register': adjust.group(3)})
            rip = RIP_RELATIVE.match(text)
            if rip:
                symbol_name = None
                for probe in range(offset, offset + 8):
                    if probe in relocations:
                        symbol_name = relocations[probe]
                        break
                references.append({'at_offset': offset, 'operation': rip.group(1),
                                   'register': rip.group(3), 'symbol': symbol_name})
            if CALL.match(text):
                symbol_name = None
                for probe in range(offset, offset + 6):
                    if probe in relocations:
                        symbol_name = relocations[probe]
                        break
                callees.append({'at_offset': offset, 'symbol': symbol_name})
    return {'symbol': symbol, 'instruction_count': len(instructions),
            'bounds_checks': bounds, 'count_arguments': counts,
            'selector_adjustments': adjustments, 'table_references': references,
            'calls': callees}


def normalise(register: str) -> str:
    """The 64-bit name of a register, so esi and rsi compare equal."""
    if register in ARGUMENT_REGISTERS_32:
        return ARGUMENT_REGISTERS_64[ARGUMENT_REGISTERS_32.index(register)]
    return register.rstrip('d') if register.startswith('r') else 'r' + register[1:]


def conclude(function: dict) -> dict:
    """What the instructions agree on, and where they do not."""
    from_bounds = sorted({value for entry in function['bounds_checks']
                          for value in entry['implied_values']})
    from_arguments = sorted({entry['immediate'] for entry in function['count_arguments']})
    agreed = sorted(set(from_bounds) & set(from_arguments))
    entry_count = {'value': agreed[0] if len(agreed) == 1 else None,
                   'from_bounds_check': from_bounds,
                   'from_count_argument': from_arguments,
                   'sources_agree': len(agreed) == 1,
                   'note': 'each comparison yields two possible limits because the compiler '
                           'may compare against the limit or one below it; a value that also '
                           'appears as the count argument is the decided one'}
    # Only arithmetic on a register a bounds check actually compares can be a
    # selector adjustment; everything else in the function is unrelated.
    selector_registers = {normalise(entry['register']) for entry in function['bounds_checks']}
    from_adjustments = sorted({entry['subtracted_from_selector']
                               for entry in function['selector_adjustments']
                               if entry['subtracted_from_selector'] > 0
                               and (not selector_registers
                                    or normalise(entry['register']) in selector_registers)})
    # A comparison that is not the table bound can be the base check, so the
    # candidates are every implied value except the decided entry count.
    decided_count = entry_count['value']
    from_lower_bounds = sorted({value for entry in function['bounds_checks']
                                for value in entry['implied_values']
                                if value != decided_count and value > 0})
    both = sorted(set(from_adjustments) & set(from_lower_bounds))
    value = both[0] if len(both) == 1 else (
        from_adjustments[0] if len(from_adjustments) == 1 else None)
    selector_base = {'value': value,
                     'from_adjustment': from_adjustments,
                     'from_lower_bound': from_lower_bounds,
                     'sources_agree': len(both) == 1,
                     'selector_registers': sorted(selector_registers),
                     'note': 'a positive subtraction applied to the selector register before '
                             'dispatch is the base a client adds, and a reject-if-below check '
                             'states the same number independently'}
    if not from_adjustments:
        # No arithmetic touches the selector register, so nothing is subtracted
        # from it and the wire selector is the array index.
        selector_base['value'] = 0
        selector_base['note'] += ('; no adjustment touches the selector register, so the '
                                  'wire selector is the array index')
    tables = sorted({entry['symbol'] for entry in function['table_references']
                     if entry['symbol']})
    callees = sorted({entry['symbol'] for entry in function['calls'] if entry['symbol']})
    return {'entry_count': entry_count, 'selector_base': selector_base,
            'table_symbols': tables, 'callees': callees,
            'dispatch_style': '2022' if any('dispatchExternalMethod' in name
                                            for name in callees)
            else 'legacy or self-dispatched'}


def read(binary: Path, bundle: str | None, symbol: str | None) -> dict:
    macho = load('abi_macho', 'tools/abi_macho.py')
    image = macho.load_image(binary, bundle)
    symbols = candidate_symbols(image, symbol)
    functions, failures = [], []
    for name in symbols[:16]:
        try:
            function = read_function(binary, image, name)
        except DisassemblyUnavailable as error:
            failures.append({'symbol': name, 'reason': str(error)[:200]})
            continue
        function['conclusions'] = conclude(function)
        functions.append(function)
    import hashlib
    return {
        'generated_by': 'tools/abi_callsite.py',
        'binary': {'path': str(binary), 'bundle': bundle,
                   'sha256': hashlib.sha256(binary.read_bytes()).hexdigest()},
        'image': image.describe(),
        'candidate_symbols': symbols[:64],
        'functions': functions,
        'failures': failures,
        'limits': 'Numbers, offsets, registers and symbol names read out of a disassembly '
                  'that is parsed and discarded. No instruction text is reproduced, and the '
                  'conclusions are readings of the code, not the code.',
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--bundle', help='bundle identifier inside a kernel collection')
    parser.add_argument('--symbol', help='substring of the function to read '
                                         '(default: every externalMethod override)')
    parser.add_argument('--out', type=Path)
    arguments = parser.parse_args()
    record = read(arguments.binary, arguments.bundle, arguments.symbol)
    if arguments.out:
        policy = load('abi_policy', 'tools/abi_policy.py')
        policy.write_record(arguments.out, record, 'dispatch call sites')
        print('Wrote ' + str(arguments.out))
    if not record['functions']:
        print('no dispatch call site read; candidates: '
              + ', '.join(record['candidate_symbols'][:4] or ['none']))
        for failure in record['failures'][:3]:
            print('  ' + failure['symbol'] + ': ' + failure['reason'])
        return 1
    for function in record['functions']:
        conclusions = function['conclusions']
        print(function['symbol'])
        print('  entries: ' + str(conclusions['entry_count']['value'])
              + ('  (bounds ' + str(conclusions['entry_count']['from_bounds_check'])
                 + ', argument ' + str(conclusions['entry_count']['from_count_argument']) + ')'))
        print('  selector base: ' + str(conclusions['selector_base']['value']))
        print('  table: ' + ', '.join(conclusions['table_symbols'] or ['unresolved'])
              + '   style: ' + conclusions['dispatch_style'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
