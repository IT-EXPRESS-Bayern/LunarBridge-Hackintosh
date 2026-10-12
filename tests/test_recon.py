# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Check the recon orchestrator: its rules match the pinned sources, its stages
degrade instead of guessing, and its report claims no hardware state."""
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load('recon', 'tools/recon.py')


class RuleTable(unittest.TestCase):
    """Every extraction rule must still match its hash-pinned source."""

    @classmethod
    def setUpClass(cls):
        cls.compare = load('compare_interfaces', 'tools/compare_interfaces.py')

    def test_every_rule_is_well_formed(self):
        kinds = {'int', 'hex', 'range', 'pair', 'version', 'hexlist', 'text'}
        names = set()
        for group, name, reference, pattern, kind in TOOL.EXTRACTION_RULES:
            self.assertIn(kind, kinds, name)
            self.assertNotIn((group, name), names, 'duplicate rule')
            names.add((group, name))
            re.compile(pattern)
            self.assertTrue(reference.startswith(('linux-', 'macos-', 'apple-')), reference)

    def test_every_rule_matches_and_is_pinned(self):
        manifest = {entry['path'] for entry in
                    json.loads((ROOT / 'tests/reference-manifest.json')
                               .read_text(encoding='utf-8'))['files']}
        for group, name, reference, pattern, kind in TOOL.EXTRACTION_RULES:
            with self.subTest(fact=name):
                self.assertIn(reference, manifest, 'rule reads an unpinned file')
                match = re.search(pattern, self.compare.read_reference(reference))
                self.assertIsNotNone(match, 'rule no longer matches: ' + name)
                TOOL.convert(kind, match.groups())

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            TOOL.convert('bitfield', ('1',))

    def test_converters_produce_the_documented_shapes(self):
        self.assertEqual(TOOL.convert('int', ('2004',)), 2004)
        self.assertEqual(TOOL.convert('hex', ('0xc000',)), 0xC000)
        self.assertEqual(TOOL.convert('range', ('15', '8')),
                         {'high_bit': 15, 'low_bit': 8, 'mask': 0xFF00})
        self.assertEqual(TOOL.convert('pair', ('1270', 'FORCEWAKE_ACK_GT_MTL')),
                         {'threshold': 1270, 'register': 'FORCEWAKE_ACK_GT_MTL'})
        self.assertEqual(TOOL.convert('version', ('70', '45', '2')), '70.45.2')
        self.assertEqual(TOOL.convert('hexlist', ('MACRO__(0x6420), MACRO__(0x64A0)',)),
                         ['0x6420', '0x64a0'])

    def test_the_target_device_is_among_the_extracted_ids(self):
        rule = next(entry for entry in TOOL.EXTRACTION_RULES
                    if entry[1] == 'lunarlake_pci_ids')
        match = re.search(rule[3], self.compare.read_reference(rule[2]))
        self.assertIn(TOOL.TARGET_DEVICE['device'], TOOL.convert(rule[4], match.groups()))


class StageInputs(unittest.TestCase):
    """Each stage names what it needs and what it compiles, by real path."""

    def test_source_lists_exist(self):
        for source in TOOL.LIBRARY_SOURCES + TOOL.HOST_SOURCES:
            self.assertTrue((ROOT / source).is_file(), source)

    def test_module_sources_match_the_shipped_interface_build(self):
        workflow = (ROOT / '.github/workflows/interface-diff.yml').read_text(encoding='utf-8')
        for source in TOOL.LIBRARY_SOURCES:
            self.assertIn(source, workflow, 'recon builds a module CI does not')

    def test_host_sources_match_the_windows_host_build(self):
        build = (ROOT / 'gpu-prototype/build_gpu.py').read_text(encoding='utf-8')
        for source in TOOL.HOST_SOURCES:
            self.assertIn(Path(source).name, build, 'recon builds a host file build_gpu.py omits')
        for define in TOOL.HOST_DEFINES:
            self.assertIn(define.removeprefix('-D'), build)

    def test_every_suite_script_exists_and_takes_the_declared_library(self):
        for name, script, requirement in TOOL.TEST_SUITES:
            with self.subTest(suite=name):
                text = (ROOT / script).read_text(encoding='utf-8')
                kind, _, how = (requirement or '').partition(':')
                self.assertIn(kind, ('', 'modules', 'host'))
                if how == 'positional':
                    self.assertIn("add_argument('library'", text)
                elif how == '--library':
                    self.assertIn("add_argument('--library'", text)
                elif how == 'environment':
                    self.assertTrue(any(name in text
                                        for name in TOOL.HOST_LIBRARY_ENVIRONMENT),
                                    'the suite reads none of the library variables')

    def test_coverage_map_names_files_that_exist(self):
        for group, (modules, gap) in TOOL.PORT_COVERAGE.items():
            with self.subTest(group=group):
                self.assertTrue(modules, group)
                for module in modules:
                    self.assertTrue((ROOT / module).is_file(), module)
                self.assertIsInstance(gap, str)

    def test_every_extracted_group_has_a_coverage_entry(self):
        groups = {group for group, *_ in TOOL.EXTRACTION_RULES} | {'pat', 'lrc'}
        self.assertEqual(groups - set(TOOL.PORT_COVERAGE), set())

    def test_macos_probes_are_read_only(self):
        forbidden = ('kextload', 'kextunload', 'kmutil', 'bless', 'csrutil disable',
                     'nvram -d', 'rm', 'dd', 'diskutil')
        for name, command in TOOL.MACOS_PROBES.items():
            with self.subTest(probe=name):
                self.assertNotIn(command[0], forbidden)
                self.assertFalse(any(part.startswith('-d') for part in command[1:]), name)


class Degradation(unittest.TestCase):
    """Nothing is invented when a stage cannot run."""

    def test_probe_reports_a_missing_binary(self):
        result = TOOL.probe(['lunarbridge-does-not-exist'])
        self.assertFalse(result['performed'])
        self.assertIn('not found', result['reason'])

    def test_stages_needing_macos_skip_elsewhere(self):
        if platform.system() == 'Darwin':
            self.skipTest('this check describes the non-macOS path')
        for stage in (TOOL.stage_system, TOOL.stage_device):
            record = stage()
            self.assertFalse(record['performed'])
            self.assertIn('macOS', record['reason'])

    def test_portplan_without_a_library_is_skipped_not_guessed(self):
        record = TOOL.stage_portplan(None)
        self.assertFalse(record['performed'])
        self.assertNotIn('plan', record)

    def test_appledriver_reports_the_directories_it_searched(self):
        with tempfile.TemporaryDirectory() as directory:
            record = TOOL.stage_appledriver([Path(directory)], Path(directory))
        self.assertFalse(record['performed'])
        self.assertEqual(record['searched'], [directory])

    def test_tests_are_skipped_when_their_library_is_missing(self):
        # Only the library-dependent rows: the others would really run here.
        original = TOOL.TEST_SUITES
        TOOL.TEST_SUITES = tuple(row for row in original if row[2])
        try:
            suites = TOOL.stage_tests({'modules': {'built': False}, 'host': {'built': False}})
        finally:
            TOOL.TEST_SUITES = original
        self.assertTrue(suites)
        for name, entry in suites.items():
            self.assertFalse(entry['performed'], name)
            self.assertIn('was not built', entry['reason'])

    def test_a_run_inside_a_suite_refuses_to_recurse(self):
        os.environ[TOOL.NESTED_MARKER] = '1'
        try:
            suites = TOOL.stage_tests({'modules': {'built': True, 'path': 'unused'},
                                       'host': {'built': True, 'path': 'unused'}})
        finally:
            del os.environ[TOOL.NESTED_MARKER]
        self.assertEqual(sorted(suites), sorted(name for name, _, _ in TOOL.TEST_SUITES))
        for name, entry in suites.items():
            self.assertFalse(entry['performed'], name)
            self.assertEqual(entry['reason'], 'nested run inside a suite')


class Report(unittest.TestCase):
    """The report states what was measured and never more than that."""

    def record(self, **stages):
        base = {'generated_by': 'tools/recon.py', 'stages_selected': list(stages),
                'environment': {'system': 'Linux', 'release': '6.0', 'machine': 'x86_64',
                                'macos_version': '', 'python': '3.13.0', 'tools': {}}}
        base.update(stages)
        return base

    def test_a_clean_run_has_no_high_severity_blocker(self):
        record = self.record(
            libraries={'modules': {'built': True}, 'host': {'built': True}},
            tests={'alpha': {'performed': True, 'passed': True, 'tail': ['OK']}},
            repository={'pinned_references': {'total': 2, 'verified': 2, 'missing': [],
                                              'mismatched': []}},
            interface={'performed': True, 'driver_table': {'mismatches': []},
                       'linux_ioctls': 12, 'apple_xe_or_xe2_driver_present': False},
            extract={'performed': True, 'rules': 2, 'extracted': 2, 'drift': [],
                     'facts': {'guc': {'status_register': {'value': 1, 'citation': 'a:1'}}}})
        ranked = TOOL.findings(record)
        high = [finding for finding in ranked
                if finding['severity'] == 'high' and finding['state'] == 'blocked']
        self.assertEqual(high, [])
        self.assertEqual([finding['id'] for finding in ranked if finding['state'] == 'blocked'],
                         ['R14', 'R99'])

    def test_drift_a_failed_build_and_a_failed_suite_all_block(self):
        record = self.record(
            libraries={'modules': {'built': False, 'reason': 'no compiler'}},
            tests={'alpha': {'performed': True, 'passed': False, 'tail': ['FAILED']}},
            repository={'pinned_references': {'total': 2, 'verified': 1,
                                              'missing': ['x'], 'mismatched': []}},
            extract={'performed': True, 'rules': 2, 'extracted': 1, 'facts': {},
                     'drift': [{'name': 'status_register', 'reason': 'pattern'}]})
        blocked = {finding['id'] for finding in TOOL.findings(record)
                   if finding['severity'] == 'high' and finding['state'] == 'blocked'}
        self.assertEqual(blocked, {'R01', 'R02', 'R03', 'R13'})

    def test_the_acceleration_finding_is_always_present_and_negative(self):
        ranked = TOOL.findings(self.record())
        final = [finding for finding in ranked if finding['id'] == 'R99']
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]['state'], 'blocked')
        self.assertIn('no hardware acceleration', final[0]['summary'])

    def test_findings_are_ranked_blockers_first(self):
        ranked = TOOL.findings(self.record(
            repository={'pinned_references': {'total': 1, 'verified': 0, 'missing': ['x'],
                                              'mismatched': []}},
            libraries={'modules': {'built': True}}))
        order = [(finding['severity'], finding['state']) for finding in ranked]
        self.assertEqual(order[0], ('high', 'blocked'))

    def test_the_markdown_carries_the_scope_and_no_hardware_claim(self):
        record = self.record(
            repository={'commit': 'abc', 'dirty': ''},
            portplan={'performed': True, 'plan': {'pat_write_count': 33, 'lrc_write_count': 11,
                                                  'pat_indices': {'write_back': 2},
                                                  'pat_ggtt_max_index': 3,
                                                  'lrc_render_total_bytes': 1,
                                                  'lrc_descriptor_template': 0x119}})
        text = TOOL.summarise(record, TOOL.findings(record))
        self.assertIn('no register was written', text)
        self.assertIn('Nothing here claims hardware acceleration', text)
        self.assertNotIn('accelerated', text)

    def test_register_values_are_rendered_in_hexadecimal_too(self):
        self.assertEqual(TOOL.one_line(0xC000), '49152 (0xc000)')
        self.assertEqual(TOOL.one_line(3), '3')
        self.assertEqual(TOOL.one_line(True), 'True')
        self.assertEqual(TOOL.one_line([]), '—')


class WriteDiscipline(unittest.TestCase):
    """A run writes its report and nothing else."""

    def test_only_the_output_directory_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'run'
            out.mkdir()
            before = {path: path.stat().st_mtime_ns for path in ROOT.rglob('*')
                      if path.is_file() and '.git' not in path.parts
                      and 'build' not in path.parts and '__pycache__' not in path.parts}
            TOOL.stage_extract(out)
            after = {path: path.stat().st_mtime_ns for path in before}
            self.assertEqual([str(path) for path in before if before[path] != after[path]], [])
            self.assertTrue((out / 'upstream-facts.json').is_file())

    def test_the_extract_stage_emits_citations_for_every_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            record = TOOL.stage_extract(Path(directory))
        self.assertTrue(record['performed'])
        self.assertEqual(record['drift'], [])
        for group, facts in record['facts'].items():
            for name, entry in facts.items():
                if name == 'citation':
                    continue
                citation = entry.get('citation') if isinstance(entry, dict) else None
                with self.subTest(fact=group + '.' + name):
                    self.assertTrue(citation or facts.get('citation'))

    def test_the_extract_stage_copies_no_upstream_code(self):
        with tempfile.TemporaryDirectory() as directory:
            TOOL.stage_extract(Path(directory))
            text = (Path(directory) / 'upstream-facts.json').read_text(encoding='utf-8')
        # Quoted source text is capped at one line per fact, never a code block.
        for line in json.loads(text)['facts'].get('device', {}).values():
            if isinstance(line, dict) and 'source_text' in line:
                self.assertLessEqual(len(line['source_text']), 160)
        self.assertNotIn('SPDX-License-Identifier: GPL-2.0', text)
        self.assertNotIn('Copyright © 2021 Intel', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
