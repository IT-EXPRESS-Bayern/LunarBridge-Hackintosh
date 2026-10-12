# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""The gate every ABI recovery record passes before it may be written or committed.

Interface recovery on a lawfully installed copy is what § 69e UrhG, Art. 6 of the
EU Software Directive and 17 U.S.C. § 1201(f) permit: observe an interface in
order to make an independent program interoperate. What none of them permit is
reproducing the analysed program. The difference is mechanical, so it is checked
mechanically here rather than left to discipline:

    allowed   names, counts, offsets, sizes, selector numbers, bit positions,
              version strings, SHA-256 digests, UUIDs, verdicts, citations
    refused   instruction or data bytes from the analysed binary, disassembly
              listings, decompiled or copied source, embedded blobs

A record that trips a rule is not written. `check_record` returns every
violation with its path so the tool that produced it can be fixed; nothing is
silently stripped, because silent stripping hides a tool that collects too much.
"""
from __future__ import annotations

import re

# Three or more space-separated hex byte pairs: the shape of a memory dump or a
# disassembly listing's byte column ("48 89 e5", "0f 1f 44 00").
BYTE_RUN = re.compile(r'(?:\b[0-9a-fA-F]{2}\b[ \t,]+){2,}\b[0-9a-fA-F]{2}\b')
# A bare hex blob longer than a 64-bit word, outside the digest fields below.
HEX_BLOB = re.compile(r'(?:0[xX])?[0-9a-fA-F]{17,}')
# A base64 run long enough to carry data. Mangled C++ symbols and file paths
# are also long runs of this alphabet: a path has '/' separators (and 'c++' in
# an xnu path even supplies a '+'), a symbol has neither '+' nor padding. So a
# match counts only when it carries '=' padding, or a '+' with no path or
# version punctuation around it.
BASE64_BLOB = re.compile(r'[A-Za-z0-9+/]{32,}={0,2}')
# Keys whose value is a digest or identifier of a fixed hexadecimal length.
DIGEST_KEYS = ('sha256', 'sha1', 'md5', 'uuid', 'digest', 'hash', 'cdhash')
DIGEST_LENGTHS = (32, 40, 64)
# Keys a recovery tool must never emit at all, whatever their value.
REFUSED_KEYS = ('bytes_dump', 'hexdump', 'disassembly', 'disasm', 'instructions',
                'opcodes', 'machine_code', 'decompiled', 'source_code', 'listing',
                'raw', 'blob', 'payload', 'firmware')
# A mnemonic followed by machine operand syntax - a register, an immediate, a
# hexadecimal displacement or an indirection. Several mnemonics are also
# ordinary English words ('and', 'or', 'test'), so a bare number after one of
# them is not enough: the operand itself has to look like machine code.
DISASSEMBLY = re.compile(
    r'\b(?:mov|lea|call|jmp|ret|push|pop|add|sub|cmp|test|xor|and|or|shl|shr|'
    r'imul|idiv|inc|dec|nop|ud2|int3)[qlwb]?\b[ \t]+'
    r'(?:[*$]?-?%[a-z0-9]+|\$0x[0-9a-fA-F]+|\*?-?0x[0-9a-fA-F]+(?:\(|\b)|'
    r'-?0x[0-9a-fA-F]+\(%[a-z0-9]+\))')
MAX_STRING = 4096


def _is_digest_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in DIGEST_KEYS)


def _check_string(path: str, key: str, value: str, violations: list[dict]) -> None:
    def refuse(rule: str, detail: str) -> None:
        violations.append({'path': path, 'rule': rule, 'detail': detail})

    if len(value) > MAX_STRING:
        refuse('string_too_long',
               'a ' + str(len(value)) + '-character value is a blob, not a fact')
    match = BYTE_RUN.search(value)
    if match:
        refuse('byte_run', 'looks like dumped bytes at ' + repr(match.group(0)[:24]))
    match = DISASSEMBLY.search(value)
    if match:
        refuse('disassembly', 'looks like an instruction listing at '
               + repr(match.group(0)[:32]))
    for match in BASE64_BLOB.finditer(value):
        text = match.group(0)
        encoded = text.endswith('=') or ('+' in text and '/' not in text)
        if encoded:
            refuse('base64_blob', 'embedded encoded data at ' + repr(text[:16] + '…'))
            break
    for match in HEX_BLOB.finditer(value):
        text = match.group(0)
        digits = text[2:] if text[:2].lower() == '0x' else text
        if _is_digest_key(key) and len(digits) in DIGEST_LENGTHS:
            continue
        refuse('hex_blob', 'hexadecimal run of ' + str(len(digits))
               + ' digits is wider than an address or a word: ' + repr(text[:20] + '…'))


def check_record(record, path: str = '$') -> list[dict]:
    """Every reason this record may not be written, each with its location."""
    violations: list[dict] = []
    if isinstance(record, (bytes, bytearray, memoryview)):
        violations.append({'path': path, 'rule': 'raw_bytes',
                           'detail': 'binary content from the analysed file'})
        return violations
    if isinstance(record, dict):
        for key, value in record.items():
            if not isinstance(key, str):
                violations.append({'path': path, 'rule': 'non_string_key',
                                   'detail': repr(key)[:40]})
                continue
            child = path + '.' + key
            if key.lower() in REFUSED_KEYS:
                violations.append({'path': child, 'rule': 'refused_key',
                                   'detail': 'this field may never be emitted'})
                continue
            if isinstance(value, str):
                _check_string(child, key, value, violations)
            else:
                violations.extend(check_record(value, child))
        return violations
    if isinstance(record, (list, tuple)):
        for index, value in enumerate(record):
            child = path + '[' + str(index) + ']'
            if isinstance(value, str):
                _check_string(child, path.rsplit('.', 1)[-1], value, violations)
            else:
                violations.extend(check_record(value, child))
        return violations
    if isinstance(record, str):
        _check_string(path, path.rsplit('.', 1)[-1], record, violations)
        return violations
    if isinstance(record, (int, float, bool)) or record is None:
        return violations
    violations.append({'path': path, 'rule': 'unsupported_type',
                       'detail': type(record).__name__ + ' cannot be checked'})
    return violations


class PolicyError(Exception):
    """Raised instead of writing a record that carries more than facts."""


def assert_clean(record, label: str = 'record') -> None:
    violations = check_record(record)
    if not violations:
        return
    lines = [label + ' violates the ABI recovery policy:']
    for violation in violations[:20]:
        lines.append('  ' + violation['path'] + ': ' + violation['rule'] + ' — '
                     + violation['detail'])
    if len(violations) > 20:
        lines.append('  … and ' + str(len(violations) - 20) + ' more')
    raise PolicyError('\n'.join(lines))


SCOPE_NOTE = (
    'Interface facts only: names, counts, offsets, sizes, selector numbers and digests, '
    'recovered from a locally installed copy for the sole purpose of making an independent '
    'driver interoperate. No instruction or data bytes, no disassembly and no code from the '
    'analysed binary are reproduced, and no analysed binary is copied or redistributed.'
)


def write_record(path, record, label: str = 'record') -> None:
    """Write a recovery record as JSON, or refuse and explain why."""
    import json
    from pathlib import Path

    if isinstance(record, dict) and 'scope' not in record:
        record = {**record, 'scope': SCOPE_NOTE}
    assert_clean(record, label)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2, sort_keys=False) + '\n', encoding='utf-8')
