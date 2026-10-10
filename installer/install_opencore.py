# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Plan, install or roll back a LunarBridge diagnostic kext in a selected OpenCore EFI."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from macho import inspect_macho

IDENTIFIER = 'de.nordapps.LunarBridge'
BUNDLE = 'LunarBridge.kext'

class InstallerError(ValueError): pass

def digest(data): return hashlib.sha256(data).hexdigest()

def inside(root, path):
    root, path = root.resolve(), path.resolve()
    if root == path or root not in path.parents: raise InstallerError('Ziel liegt außerhalb der ausgewählten EFI')
    return path

def reject_links(path, root):
    path = Path(path)
    for candidate in (path, *path.parents):
        if candidate == root.parent: break
        if candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction()):
            raise InstallerError('Verknüpfungen/Junctions im Installationspfad sind nicht erlaubt')
        if candidate == root: break
    inside(root, path)

def tree_hashes(folder):
    if not folder.is_dir(): raise InstallerError('Kext-Verzeichnis fehlt')
    result = {}
    total = 0
    for path in folder.rglob('*'):
        reject_links(path, folder)
        if path.is_file():
            data = path.read_bytes()
            total += len(data)
            if total > 32 * 1024 * 1024 or len(result) >= 128:
                raise InstallerError('Diagnose-Bundle überschreitet die vorgesehenen Grenzen')
            result[path.relative_to(folder).as_posix()] = digest(data)
    return result

def validate_bundle(source):
    original = Path(source)
    if original.is_symlink() or (hasattr(original, 'is_junction') and original.is_junction()):
        raise InstallerError('Kext-Quelle ist eine Verknüpfung')
    source = original.resolve()
    hashes = tree_hashes(source)
    try: info = plistlib.loads((source/'Contents/Info.plist').read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc: raise InstallerError('Kext-Info.plist fehlt oder ist ungültig') from exc
    if info.get('CFBundleIdentifier') != IDENTIFIER or info.get('CFBundleExecutable') != 'LunarBridge' or \
       info.get('CFBundlePackageType') != 'KEXT': raise InstallerError('Falsche Bundle-Identität')
    try: personality = info['IOKitPersonalities']['LunarBridgeArc130VSamsungDiagnostic']
    except (KeyError, TypeError) as exc: raise InstallerError('Diagnose-Personality fehlt') from exc
    expected = {'IOClass': 'LunarBridgePCI', 'IOProviderClass': 'IOPCIDevice',
                'IOPCIPrimaryMatch': '0x64a08086', 'IOPCISecondaryMatch': '0xc1dd144d',
                'IOMatchCategory': 'LunarBridgeDiagnostic', 'IOUserClientClass': 'LunarBridgeUserClient'}
    if any(personality.get(key) != value for key, value in expected.items()):
        raise InstallerError('Gerätezuordnung oder Match-Kategorie wurde verändert')
    try: inspection = inspect_macho((source/'Contents/MacOS/LunarBridge').read_bytes(), require_kext=True)
    except (OSError, ValueError, UnicodeError, struct.error) as exc: raise InstallerError('Ungültiges natives Kext-Binary: ' + str(exc)) from exc
    if inspection['identifier'] != IDENTIFIER or inspection['version'] != info.get('CFBundleVersion'):
        raise InstallerError('Kmod-Metadaten passen nicht zum Bundle')
    if not isinstance(personality.get('LBEnableMMIORead'), bool) or \
       not isinstance(personality.get('LBDebugLogging'), bool): raise InstallerError('Ungültige Diagnose-Einstellungen')
    return {'source': source, 'hashes': hashes, 'inspection': inspection, 'info': info}

def target_state(efi):
    original = Path(efi)
    if original.is_symlink() or (hasattr(original, 'is_junction') and original.is_junction()):
        raise InstallerError('EFI-Wurzel ist eine Verknüpfung')
    efi = original.resolve()
    if not efi.is_dir(): raise InstallerError('Ausgewählte EFI existiert nicht')
    oc, config = efi/'OC', efi/'OC/config.plist'
    for path in (oc, config, oc/'Kexts', oc/'LunarBridge-Backups', oc/'OpenCore.efi'):
        reject_links(path, efi)
    try:
        before = config.read_bytes()
        data = plistlib.loads(before)
        entries = data['Kernel']['Add']
    except (OSError, KeyError, TypeError, plistlib.InvalidFileException) as exc:
        raise InstallerError('OpenCore-Konfiguration fehlt oder ist ungültig') from exc
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise InstallerError('Kernel/Add ist keine Liste von Treiber-Einträgen')
    pe = (oc/'OpenCore.efi').read_bytes()
    if len(pe) < 64 or pe[:2] != b'MZ': raise InstallerError('OpenCore.efi fehlt oder ist kein PE-Binary')
    pe_offset = struct.unpack_from('<I', pe, 0x3c)[0]
    if pe_offset > len(pe) - 6 or pe[pe_offset:pe_offset+4] != b'PE\0\0' or \
       struct.unpack_from('<H', pe, pe_offset+4)[0] != 0x8664:
        raise InstallerError('OpenCore.efi ist kein x86_64-PE-Binary')
    if data.get('UEFI', {}).get('Quirks', {}).get('DisableSecurityPolicy', False) is not False:
        raise InstallerError('DisableSecurityPolicy widerspricht der Secure-Boot-Anforderung')
    vault = data.get('Misc', {}).get('Security', {}).get('Vault', 'Optional')
    if vault != 'Optional' or (oc/'vault.plist').exists() or (oc/'vault.sig').exists():
        raise InstallerError('Vault-geschützte EFI: Konfigurationsänderung benötigt eine neue gültige Vault-Signatur')
    indices = [i for i, entry in enumerate(entries) if str(entry.get('BundlePath', '')).casefold() == BUNDLE.casefold()]
    if len(indices) > 1: raise InstallerError('Mehrere LunarBridge-Einträge vorhanden')
    existing = oc/'Kexts'/BUNDLE
    reject_links(existing, efi)
    previous_hashes = tree_hashes(existing) if existing.exists() else None
    if existing.exists():
        try: existing_info = plistlib.loads((existing/'Contents/Info.plist').read_bytes())
        except (OSError, plistlib.InvalidFileException) as exc: raise InstallerError('Bestehendes Ziel ist kein bekanntes LunarBridge-Bundle') from exc
        if existing_info.get('CFBundleIdentifier') != IDENTIFIER:
            raise InstallerError('Bestehendes Ziel gehört nicht zu LunarBridge')
    return {'efi': efi, 'oc': oc, 'config': config, 'before': before, 'data': data,
            'indices': indices, 'target': existing, 'previous_hashes': previous_hashes,
            'opencore_sha256': digest(pe)}

def plan(efi, source, enable=False):
    state, bundle = target_state(efi), validate_bundle(source)
    entry = {'Arch': 'x86_64', 'BundlePath': BUNDLE,
             'Comment': 'LunarBridge native diagnostics; GPU execution unavailable', 'Enabled': bool(enable),
             'ExecutablePath': 'Contents/MacOS/LunarBridge', 'PlistPath': 'Contents/Info.plist',
             'MinKernel': '25.0.0', 'MaxKernel': '25.99.99'}
    entries = state['data']['Kernel']['Add']
    if state['indices']: entries[state['indices'][0]] = entry
    else: entries.append(entry)
    after = plistlib.dumps(state['data'], sort_keys=False)
    public = {'operation': 'plan_only', 'bundle': BUNDLE, 'enabled_after_next_boot': bool(enable),
              'mmio_enabled': bundle['info']['IOKitPersonalities']['LunarBridgeArc130VSamsungDiagnostic']['LBEnableMMIORead'],
              'config_before_sha256': digest(state['before']), 'config_after_sha256': digest(after),
              'bundle_hashes': bundle['hashes'], 'opencore_sha256_unchanged': state['opencore_sha256'],
              'firmware_secure_boot_changed': False, 'firmware_secure_boot_verified': False,
              'system_kext_directory_changed': False, 'boot_order_changed': False,
              'native_hardware_tested': False, 'gpu_execution_implemented': False,
              'metal_implemented': False}
    return public, state, bundle, after

def write_atomic(path, data, root):
    reject_links(path, root)
    path = inside(root, path)
    temporary = path.with_name('.' + path.name + '.LunarBridge-' + uuid.uuid4().hex + '.tmp')
    inside(root, temporary)
    try:
        with temporary.open('xb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()

def remove_tree(path, root):
    reject_links(path, root)
    path = inside(root, path)
    if path.exists():
        tree_hashes(path)  # reject embedded links before recursive removal
        shutil.rmtree(path)

def copy_tree(source, destination, root):
    reject_links(destination, root)
    destination = inside(root, destination)
    tree_hashes(source)
    shutil.copytree(source, destination)

def install(efi, source, enable=False):
    public, state, bundle, after = plan(efi, source, enable)
    root, oc, target = state['efi'], state['oc'], state['target']
    nonce = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex
    backup = inside(root, oc/'LunarBridge-Backups'/nonce)
    backup.mkdir(parents=True, exist_ok=False)
    (backup/'config.before.plist').write_bytes(state['before'])
    stage = inside(root, oc/'Kexts'/('.LunarBridge-stage-' + nonce))
    stage.parent.mkdir(parents=True, exist_ok=True)
    receipt = {**public, 'operation': 'install', 'state': 'installing', 'efi_root': str(root),
               'previous_bundle_hashes': state['previous_hashes'], 'nonce': nonce}
    receipt_path = backup/'install-receipt.json'
    write_atomic(receipt_path, json.dumps(receipt, indent=2).encode('utf-8'), root)
    moved_previous = False
    installed_new = False
    try:
        copy_tree(bundle['source'], stage, root)
        if tree_hashes(stage) != bundle['hashes']: raise InstallerError('Kext-Prüfsumme nach Kopieren verändert')
        if digest(state['config'].read_bytes()) != public['config_before_sha256']:
            raise InstallerError('Konfiguration wurde während der Vorbereitung geändert')
        if digest((oc/'OpenCore.efi').read_bytes()) != state['opencore_sha256']:
            raise InstallerError('OpenCore.efi wurde während der Vorbereitung geändert')
        if target.exists() != (state['previous_hashes'] is not None):
            raise InstallerError('Vorhandene Kext wurde während der Vorbereitung hinzugefügt oder entfernt')
        if target.exists():
            if tree_hashes(target) != state['previous_hashes']: raise InstallerError('Vorhandene Kext wurde während der Vorbereitung geändert')
            inside(root, target); inside(root, backup/'previous.kext')
            target.rename(backup/'previous.kext')
            moved_previous = True
        inside(root, stage); inside(root, target)
        stage.rename(target)
        installed_new = True
        write_atomic(state['config'], after, root)
        receipt['state'] = 'installed'
        write_atomic(receipt_path, json.dumps(receipt, indent=2).encode('utf-8'), root)
        return receipt_path
    except Exception:
        if installed_new: remove_tree(target, root)
        if moved_previous: copy_tree(backup/'previous.kext', target, root)
        # Do not overwrite concurrent edits that happened before our config write.
        current = digest(state['config'].read_bytes())
        if current in (public['config_before_sha256'], public['config_after_sha256']):
            write_atomic(state['config'], state['before'], root)
        receipt['state'] = 'interrupted'
        write_atomic(receipt_path, json.dumps(receipt, indent=2).encode('utf-8'), root)
        raise
    finally:
        if stage.exists(): remove_tree(stage, root)

def rollback(efi, receipt_path):
    original = Path(efi)
    if original.is_symlink() or (hasattr(original, 'is_junction') and original.is_junction()):
        raise InstallerError('EFI-Wurzel ist eine Verknüpfung')
    root = original.resolve()
    path = Path(receipt_path)
    reject_links(path, root)
    path = inside(root, path)
    if path.name != 'install-receipt.json' or path.parent.parent != root/'OC/LunarBridge-Backups':
        raise InstallerError('Receipt liegt nicht im vorgesehenen Backup-Verzeichnis')
    receipt = json.loads(path.read_text(encoding='utf-8'))
    if receipt.get('efi_root') != str(root) or receipt.get('nonce') != path.parent.name:
        raise InstallerError('Receipt gehört nicht zur gewählten EFI')
    if receipt.get('state') == 'rolled_back': return
    if receipt.get('state') != 'installed':
        raise InstallerError('Unterbrochene Installation: Backup und aktuellen Stand vor manueller Wiederherstellung prüfen')
    config, target = root/'OC/config.plist', root/'OC/Kexts'/BUNDLE
    for item in (config, target, path.parent/'config.before.plist', path.parent/'previous.kext'):
        reject_links(item, root)
    if digest(config.read_bytes()) != receipt['config_after_sha256'] or tree_hashes(target) != receipt['bundle_hashes']:
        raise InstallerError('Nachträgliche Änderungen erkannt; Rücknahme würde diese überschreiben')
    before = (path.parent/'config.before.plist').read_bytes()
    if digest(before) != receipt['config_before_sha256']: raise InstallerError('Konfigurationsbackup verändert')
    previous = path.parent/'previous.kext'
    if receipt['previous_bundle_hashes'] is not None and tree_hashes(previous) != receipt['previous_bundle_hashes']:
        raise InstallerError('Kext-Backup verändert')
    held_new = path.parent/'rollback-current.kext'
    stage_old = root/'OC/Kexts'/('.LunarBridge-rollback-' + uuid.uuid4().hex)
    reject_links(held_new, root); reject_links(stage_old, root)
    if held_new.exists(): raise InstallerError('Vorheriger Rücknahmeversuch benötigt Prüfung')
    current_config = config.read_bytes()
    moved_new, installed_old = False, False
    try:
        if receipt['previous_bundle_hashes'] is not None: copy_tree(previous, stage_old, root)
        if digest(config.read_bytes()) != receipt['config_after_sha256'] or tree_hashes(target) != receipt['bundle_hashes']:
            raise InstallerError('EFI wurde während der Rücknahmevorbereitung geändert')
        inside(root, target); inside(root, held_new)
        target.rename(held_new)
        moved_new = True
        if stage_old.exists():
            inside(root, stage_old); inside(root, target)
            stage_old.rename(target)
            installed_old = True
        write_atomic(config, before, root)
        receipt['state'] = 'rolled_back'
        write_atomic(path, json.dumps(receipt, indent=2).encode('utf-8'), root)
    except Exception:
        if installed_old: remove_tree(target, root)
        if moved_new:
            inside(root, held_new); inside(root, target)
            held_new.rename(target)
        if digest(config.read_bytes()) in (receipt['config_before_sha256'], receipt['config_after_sha256']):
            write_atomic(config, current_config, root)
        raise
    finally:
        if stage_old.exists(): remove_tree(stage_old, root)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('plan', 'install', 'rollback'))
    parser.add_argument('--efi-root', required=True, type=Path, help='Explicit root containing OC/config.plist')
    parser.add_argument('--kext', type=Path)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--enable', action='store_true', help='Enable diagnostic injection on the next Tahoe boot')
    args = parser.parse_args()
    try:
        if args.operation == 'rollback':
            if not args.receipt: parser.error('--receipt is required for rollback')
            rollback(args.efi_root, args.receipt)
            print('LunarBridge-Dateien und vorherige Konfiguration wiederhergestellt.')
        else:
            if not args.kext: parser.error('--kext is required')
            if args.operation == 'plan': print(json.dumps(plan(args.efi_root, args.kext, args.enable)[0], indent=2))
            else: print('Installation vorbereitet; Receipt: ' + str(install(args.efi_root, args.kext, args.enable)))
    except (InstallerError, OSError) as exc: raise SystemExit(str(exc))

if __name__ == '__main__': main()
