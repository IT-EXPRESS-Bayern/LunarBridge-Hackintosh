# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Installer transaction/format tests in disposable directories; no real EFI."""
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'installer'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_opencore as installer
from installer_fixtures import bundle_fixture, efi_fixture, kernel_fixture
from macho import inspect_macho

class InstallerTests(unittest.TestCase):
    def setUp(self):
        base = (ROOT/'build/installer-tests').resolve()
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='LunarBridge-', dir=base)
        self.folder = Path(self.temp.name).resolve()
        if base not in self.folder.parents: raise RuntimeError('Unexpected test directory')
        self.addCleanup(self.temp.cleanup)
        self.efi = efi_fixture(self.folder)
        self.source = bundle_fixture(self.folder)
        self.config = self.efi/'OC/config.plist'
        self.target = self.efi/'OC/Kexts/LunarBridge.kext'
        self.before = self.config.read_bytes()

    def change_config(self, change):
        data = plistlib.loads(self.config.read_bytes())
        change(data)
        self.config.write_bytes(plistlib.dumps(data, sort_keys=False))

    def previous(self):
        old = bundle_fixture(self.efi/'OC/Kexts', 'LunarBridge.kext')
        (old/'Contents/previous-marker.txt').write_text('previous fixture', encoding='utf-8')
        return installer.tree_hashes(old)

    def assert_no_staging(self):
        self.assertEqual(list((self.efi/'OC/Kexts').glob('.LunarBridge-*')), [])

    def test_plan_has_no_file_or_directory_mutations(self):
        before = {p.relative_to(self.efi).as_posix(): p.read_bytes() if p.is_file() else None
                  for p in self.efi.rglob('*')}
        result, state, _, after = installer.plan(self.efi, self.source)
        self.assertEqual(result['operation'], 'plan_only')
        self.assertFalse(result['enabled_after_next_boot'])
        self.assertFalse(plistlib.loads(after)['Kernel']['Add'][-1]['Enabled'])
        self.assertEqual(before, {p.relative_to(self.efi).as_posix(): p.read_bytes() if p.is_file() else None
                                 for p in self.efi.rglob('*')})
        self.assertEqual(state['before'], self.before)

    def test_install_preserves_other_settings_and_rollback_restores_exact_bytes(self):
        pe_before = (self.efi/'OC/OpenCore.efi').read_bytes()
        receipt = installer.install(self.efi, self.source)
        current = plistlib.loads(self.config.read_bytes())
        original = plistlib.loads(self.before)
        for key in ('UEFI', 'Misc', 'NVRAM'): self.assertEqual(current[key], original[key])
        self.assertEqual(current['Kernel']['Add'][0], original['Kernel']['Add'][0])
        self.assertEqual(installer.tree_hashes(self.target), installer.tree_hashes(self.source))
        self.assertEqual((self.efi/'OC/OpenCore.efi').read_bytes(), pe_before)
        self.assertFalse(current['Kernel']['Add'][-1]['Enabled'])
        self.assertEqual(current['Kernel']['Add'][-1]['MinKernel'], '25.0.0')
        self.assertEqual(json.loads(receipt.read_text())['state'], 'installed')
        installer.rollback(self.efi, receipt)
        installer.rollback(self.efi, receipt)
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertFalse(self.target.exists())
        self.assertEqual(json.loads(receipt.read_text())['state'], 'rolled_back')
        self.assert_no_staging()

    def test_update_existing_bundle_and_entry_then_restore_both(self):
        old_hashes = self.previous()
        self.change_config(lambda d: d['Kernel']['Add'].append({'BundlePath': 'lunarbridge.kext', 'Enabled': False}))
        before = self.config.read_bytes()
        receipt = installer.install(self.efi, self.source, enable=True)
        entries = plistlib.loads(self.config.read_bytes())['Kernel']['Add']
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[1]['Enabled'])
        installer.rollback(self.efi, receipt)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(installer.tree_hashes(self.target), old_hashes)
        self.assert_no_staging()

    def test_explicit_enable_and_mmio_plan_flags(self):
        info_path = self.source/'Contents/Info.plist'
        info = plistlib.loads(info_path.read_bytes())
        info['IOKitPersonalities']['LunarBridgeArc130VSamsungDiagnostic']['LBEnableMMIORead'] = True
        info_path.write_bytes(plistlib.dumps(info))
        result, _, _, _ = installer.plan(self.efi, self.source, enable=True)
        self.assertTrue(result['enabled_after_next_boot'])
        self.assertTrue(result['mmio_enabled'])
        self.assertFalse(result['firmware_secure_boot_changed'])
        self.assertFalse(result['firmware_secure_boot_verified'])

    def test_dma_opt_in_requires_a_real_boolean_and_is_visible_in_plan(self):
        path = self.source/'Contents/Info.plist'
        original = path.read_bytes()
        for value in (1, 'true', []):
            info = plistlib.loads(original)
            info['IOKitPersonalities']['LunarBridgeArc130VSamsungDiagnostic']['LBEnableDMAPreparation'] = value
            path.write_bytes(plistlib.dumps(info))
            with self.assertRaises(installer.InstallerError): installer.plan(self.efi, self.source)
            self.assertEqual(self.config.read_bytes(), self.before)
        info['IOKitPersonalities']['LunarBridgeArc130VSamsungDiagnostic']['LBEnableDMAPreparation'] = True
        path.write_bytes(plistlib.dumps(info))
        result, _, _, _ = installer.plan(self.efi, self.source)
        self.assertTrue(result['dma_preparation_enabled'])
        self.assertFalse(result['enabled_after_next_boot'])

    def test_forcewake_opt_in_requires_boolean_without_efi_mutations(self):
        path = self.source/'Contents/Info.plist'
        original = path.read_bytes()
        for value in (1, 'true', []):
            info = plistlib.loads(original)
            info['IOKitPersonalities']['LunarBridgeArc130VSamsungDiagnostic']['LBEnableForceWakeProbe'] = value
            path.write_bytes(plistlib.dumps(info))
            with self.assertRaises(installer.InstallerError): installer.plan(self.efi, self.source)
            self.assertEqual(self.config.read_bytes(), self.before)
            self.assertFalse(self.target.exists())

    def test_forcewake_plan_exposes_effective_mmio_writes_but_no_execution(self):
        path = self.source/'Contents/Info.plist'
        info = plistlib.loads(path.read_bytes())
        personality = info['IOKitPersonalities']['LunarBridgeArc130VSamsungDiagnostic']
        personality['LBEnableMMIORead'] = False
        personality['LBEnableForceWakeProbe'] = True
        path.write_bytes(plistlib.dumps(info))
        result, _, _, _ = installer.plan(self.efi, self.source)
        self.assertTrue(result['mmio_enabled'])
        self.assertTrue(result['forcewake_probe_enabled'])
        self.assertTrue(result['lnl_read_flush_writes_enabled'])
        self.assertFalse(result['enabled_after_next_boot'])
        self.assertFalse(result['firmware_secure_boot_changed'])
        self.assertEqual(self.config.read_bytes(), self.before)

    def test_refuses_security_policy_bypass_and_vault_without_writes(self):
        for value in (True, 1, 'false'):
            self.change_config(lambda d: d['UEFI']['Quirks'].update(DisableSecurityPolicy=value))
            before = self.config.read_bytes()
            with self.assertRaises(installer.InstallerError): installer.plan(self.efi, self.source)
            self.assertEqual(self.config.read_bytes(), before)
        self.config.write_bytes(self.before)
        for vault in ('Basic', 'Secure', 'unknown'):
            self.change_config(lambda d: d['Misc']['Security'].update(Vault=vault))
            with self.assertRaises(installer.InstallerError): installer.plan(self.efi, self.source)
        self.config.write_bytes(self.before)
        (self.efi/'OC/vault.sig').write_bytes(b'synthetic signature')
        with self.assertRaises(installer.InstallerError): installer.plan(self.efi, self.source)
        self.assertFalse(self.target.exists())

    def test_refuses_objects_wrong_architecture_and_kmod_metadata(self):
        binary = self.source/'Contents/MacOS/LunarBridge'
        cases = [dict(kind=1), dict(cpu=0x0100000c), dict(identifier='foreign.bundle'),
                 dict(version='9.9'), dict(initializer=False)]
        for options in cases:
            binary.write_bytes(kernel_fixture(**options))
            with self.assertRaises(installer.InstallerError): installer.plan(self.efi, self.source)
        self.assertEqual(self.config.read_bytes(), self.before)

    def test_rejects_corrupt_macho_bounds_and_missing_kernel_symbol(self):
        data = kernel_fixture()
        for bad in (data[:31], data[:360], data[:-10], data.replace(b'_kmod_info', b'_unknown__')):
            with self.assertRaises(ValueError): inspect_macho(bad, require_kext=True)
        actual = inspect_macho(data, require_kext=True)
        self.assertEqual(actual['identifier'], 'de.nordapps.LunarBridge')
        self.assertFalse(actual['native_load_verified'])

    def test_rejects_foreign_existing_bundle_and_duplicate_entries(self):
        self.previous()
        path = self.target/'Contents/Info.plist'
        info = plistlib.loads(path.read_bytes())
        info['CFBundleIdentifier'] = 'foreign.bundle'
        path.write_bytes(plistlib.dumps(info))
        with self.assertRaises(installer.InstallerError): installer.plan(self.efi, self.source)
        installer.remove_tree(self.target, self.efi)
        self.change_config(lambda d: d['Kernel']['Add'].extend(
            [{'BundlePath': 'LunarBridge.kext'}, {'BundlePath': 'lunarbridge.kext'}]))
        with self.assertRaises(installer.InstallerError): installer.plan(self.efi, self.source)

    def test_bundle_personality_cannot_match_another_device_or_default_category(self):
        path = self.source/'Contents/Info.plist'
        original = path.read_bytes()
        for key, value in [('IOPCIPrimaryMatch', '0x9a498086'), ('IOPCISecondaryMatch', '0xffffffff'),
                           ('IOMatchCategory', 'IOService'), ('IOUserClientClass', 'foreign')]:
            info = plistlib.loads(original)
            info['IOKitPersonalities']['LunarBridgeArc130VSamsungDiagnostic'][key] = value
            path.write_bytes(plistlib.dumps(info))
            with self.assertRaises(installer.InstallerError): installer.plan(self.efi, self.source)

    def test_gpu_installer_rejects_extra_hardware_personality_without_changes(self):
        path = self.source/'Contents/Info.plist'
        info = plistlib.loads(path.read_bytes())
        info['IOKitPersonalities']['EmbeddedI2C'] = {'IOClass': 'AnotherController',
                                                   'IOPCIPrimaryMatch': '0xa8788086'}
        path.write_bytes(plistlib.dumps(info))
        with self.assertRaises(installer.InstallerError): installer.install(self.efi, self.source)
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertFalse((self.efi/'OC/LunarBridge-Backups').exists())

    def test_gpu_installer_rejects_nested_hardware_kext_without_changes(self):
        bundle_fixture(self.source/'Contents/PlugIns', 'OtherHardware.kext')
        with self.assertRaises(installer.InstallerError): installer.install(self.efi, self.source)
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertFalse((self.efi/'OC/LunarBridge-Backups').exists())

    def test_failed_copy_cleans_staging_and_preserves_original(self):
        old = self.previous()
        def fail_copy(source, destination, root):
            installer.inside(root, destination).mkdir()
            (destination/'partial.txt').write_text('partial fixture')
            raise OSError('injected copy failure')
        with mock.patch.object(installer, 'copy_tree', side_effect=fail_copy):
            with self.assertRaises(OSError): installer.install(self.efi, self.source)
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertEqual(installer.tree_hashes(self.target), old)
        self.assert_no_staging()

    def test_config_write_failure_restores_previous_bundle_and_config(self):
        old = self.previous()
        write = installer.write_atomic
        failed = False
        def fail_once(path, data, root):
            nonlocal failed
            if path == self.config and not failed:
                failed = True
                raise OSError('injected config failure')
            return write(path, data, root)
        with mock.patch.object(installer, 'write_atomic', side_effect=fail_once):
            with self.assertRaises(OSError): installer.install(self.efi, self.source)
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertEqual(installer.tree_hashes(self.target), old)
        self.assert_no_staging()

    def test_final_receipt_failure_also_reverts_new_installation(self):
        write = installer.write_atomic
        def fail_receipt(path, data, root):
            if path.name == 'install-receipt.json' and json.loads(data)['state'] == 'installed':
                raise OSError('injected receipt failure')
            return write(path, data, root)
        with mock.patch.object(installer, 'write_atomic', side_effect=fail_receipt):
            with self.assertRaises(OSError): installer.install(self.efi, self.source)
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertFalse(self.target.exists())
        receipts = list((self.efi/'OC/LunarBridge-Backups').glob('*/install-receipt.json'))
        self.assertEqual(json.loads(receipts[0].read_text())['state'], 'interrupted')

    def test_concurrent_config_change_is_retained_and_install_rejected(self):
        old = self.previous()
        copy = installer.copy_tree
        modified = self.before + b'\n'
        def change_during_copy(source, destination, root):
            copy(source, destination, root)
            self.config.write_bytes(modified)
        with mock.patch.object(installer, 'copy_tree', side_effect=change_during_copy):
            with self.assertRaises(installer.InstallerError): installer.install(self.efi, self.source)
        self.assertEqual(self.config.read_bytes(), modified)
        self.assertEqual(installer.tree_hashes(self.target), old)
        self.assert_no_staging()

    def test_concurrent_bundle_removal_does_not_become_a_successful_update(self):
        self.previous()
        copy = installer.copy_tree
        def remove_during_copy(source, destination, root):
            copy(source, destination, root)
            installer.remove_tree(self.target, self.efi)
        with mock.patch.object(installer, 'copy_tree', side_effect=remove_during_copy):
            with self.assertRaises(installer.InstallerError): installer.install(self.efi, self.source)
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertFalse(self.target.exists())

    def test_rollback_refuses_later_edits_to_config_or_binary(self):
        receipt = installer.install(self.efi, self.source)
        installed = self.config.read_bytes()
        self.config.write_bytes(installed + b'\n')
        with self.assertRaises(installer.InstallerError): installer.rollback(self.efi, receipt)
        self.config.write_bytes(installed)
        binary = self.target/'Contents/MacOS/LunarBridge'
        binary.write_bytes(binary.read_bytes() + b'changed')
        with self.assertRaises(installer.InstallerError): installer.rollback(self.efi, receipt)
        self.assertEqual(self.config.read_bytes(), installed)

    def test_rollback_write_failure_keeps_current_installation(self):
        self.previous()
        receipt = installer.install(self.efi, self.source)
        current = self.config.read_bytes()
        hashes = installer.tree_hashes(self.target)
        write = installer.write_atomic
        failed = False
        def fail_once(path, data, root):
            nonlocal failed
            if path == self.config and not failed:
                failed = True
                raise OSError('injected rollback failure')
            return write(path, data, root)
        with mock.patch.object(installer, 'write_atomic', side_effect=fail_once):
            with self.assertRaises(OSError): installer.rollback(self.efi, receipt)
        self.assertEqual(self.config.read_bytes(), current)
        self.assertEqual(installer.tree_hashes(self.target), hashes)
        self.assertEqual(json.loads(receipt.read_text())['state'], 'installed')
        self.assert_no_staging()

    def test_rollback_refuses_modified_backup_and_wrong_receipt_location(self):
        receipt = installer.install(self.efi, self.source)
        (receipt.parent/'config.before.plist').write_bytes(b'changed backup')
        with self.assertRaises(installer.InstallerError): installer.rollback(self.efi, receipt)
        with self.assertRaises(installer.InstallerError): installer.rollback(self.efi, self.config)

    def test_path_bounds_and_raw_link_detection_before_mutations(self):
        for path in (self.efi, self.folder/'outside', self.efi/'OC/../../../outside'):
            with self.assertRaises(installer.InstallerError): installer.inside(self.efi, path)
        # Exercise link refusal without requiring Windows symlink privileges.
        with mock.patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda p: p == self.target):
            with self.assertRaises(installer.InstallerError): installer.plan(self.efi, self.source)
            with self.assertRaises(installer.InstallerError): installer.remove_tree(self.target, self.efi)
        self.assertEqual(self.config.read_bytes(), self.before)

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(InstallerTests))
    (ROOT/'tests/installer-test-report.json').write_text(json.dumps({
        'test_type': 'Isolated installer transactions, injected failures and synthetic Mach-O/PE format fixtures',
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'successful': result.wasSuccessful(),
        'real_efi_modified': False, 'native_macos_hardware_tests': 0,
    }, indent=2)+'\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
