# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Synthetic file-format fixtures. They contain no runnable macOS/EFI driver."""
from pathlib import Path
import plistlib
import struct

ROOT = Path(__file__).resolve().parents[1]

def kernel_fixture(kind=11, cpu=0x01000007, identifier='de.nordapps.LunarBridge',
                   version='0.2.0d1', initializer=True):
    # Minimal structural sections/symbols for parser and transaction tests only.
    text_at, init_at, data_at = 384, 400, 408
    symbol_at, strings_at = 608, 656
    strings = b'\0_kmod_info\0__start\0__stop\0'
    data = bytearray(strings_at + len(strings))
    struct.pack_into('<8I', data, 0, 0xfeedfacf, cpu, 3, kind, 2, 336, 0, 0)
    struct.pack_into('<II16sQQQQiiII', data, 32, 0x19, 312, b'__TEXT',
                     0x1000, 224, text_at, 224, 7, 3, 3, 0)
    sections = [(b'__text', 0x1000, 16, text_at, 0x80000400),
                (b'__mod_init_func' if initializer else b'__other', 0x1010, 8, init_at, 9),
                (b'__data', 0x1018, 200, data_at, 0)]
    for i, (name, address, size, offset, flags) in enumerate(sections):
        struct.pack_into('<16s16sQQ8I', data, 104 + i * 80, name, b'__TEXT',
                         address, size, offset, 3, 0, 0, flags, 0, 0, 0)
    struct.pack_into('<6I', data, 344, 2, 24, symbol_at, 3, strings_at, len(strings))
    struct.pack_into('<II', data, data_at + 8, 1, 0)
    for offset, value in ((16, identifier), (80, version)):
        encoded = value.encode('ascii')
        if len(encoded) > 63: raise ValueError('Fixture metadata too long')
        data[data_at + offset:data_at + offset + len(encoded)] = encoded
    for i, (name, section, address) in enumerate((('_kmod_info', 3, 0x1018),
                                                  ('__start', 1, 0x1000), ('__stop', 1, 0x1008))):
        struct.pack_into('<IBBHQ', data, symbol_at + i * 16,
                         strings.index(name.encode('ascii') + b'\0'), 0x0f, section, 0, address)
    data[strings_at:] = strings
    return bytes(data)

def bundle_fixture(parent, name='source.kext'):
    source = parent/name
    (source/'Contents/MacOS').mkdir(parents=True)
    (source/'Contents/Info.plist').write_bytes((ROOT/'native/Info.plist').read_bytes())
    (source/'Contents/MacOS/LunarBridge').write_bytes(kernel_fixture())
    return source

def efi_fixture(parent):
    efi = parent/'EFI'
    (efi/'OC/Kexts').mkdir(parents=True)
    pe = bytearray(128)
    pe[:2] = b'MZ'
    struct.pack_into('<I', pe, 0x3c, 64)
    pe[64:68] = b'PE\0\0'
    struct.pack_into('<H', pe, 68, 0x8664)
    (efi/'OC/OpenCore.efi').write_bytes(pe)
    config = {'Kernel': {'Add': [{'Arch': 'Any', 'BundlePath': 'Lilu.kext', 'Enabled': True,
               'ExecutablePath': 'Contents/MacOS/Lilu', 'PlistPath': 'Contents/Info.plist',
               'MinKernel': '', 'MaxKernel': '', 'Comment': 'preserved fixture'}]},
              'UEFI': {'Quirks': {'DisableSecurityPolicy': False}},
              'Misc': {'Security': {'Vault': 'Optional', 'SecureBootModel': 'Default'}},
              'NVRAM': {'Add': {'fixture-guid': {'boot-args': '-v'}}}}
    (efi/'OC/config.plist').write_bytes(plistlib.dumps(config, sort_keys=False))
    return efi
