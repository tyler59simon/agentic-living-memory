"""Functionality simulation (scripts/simulate.py): every scenario in tests/scenarios passes on each harness it
declares, in-process, and the harness reports a regression instead of passing it."""
import importlib.util, json, os, unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('simulate', REPO / 'scripts' / 'simulate.py')
simulate = importlib.util.module_from_spec(spec); spec.loader.exec_module(simulate)


class Scenarios(unittest.TestCase):
    def test_every_scenario_passes(self):
        scenarios = simulate.load('all')
        self.assertGreaterEqual(len(scenarios), 12)
        before = os.environ.get('LIVING_MEMORY_ROOT')
        report = simulate.simulate(scenarios)
        self.assertEqual(os.environ.get('LIVING_MEMORY_ROOT'), before)  # the sandbox selection does not leak
        for run in report['runs']:
            with self.subTest(scenario=run['scenario'], harness=run['harness']):
                self.assertTrue(run['ok'], simulate.summary({**report, 'runs': [run]}))

    def test_a_broken_gate_is_reported(self):
        scenario = [s for s in simulate.load('retrieval-gate')][0]
        keep_nothing = lambda query, root, trace=None: (lambda hits: [])
        with patch('core.runtime.relevance_gate', keep_nothing):
            report = simulate.simulate([scenario], {'claude'})
        failures = [f for s in report['runs'][0]['steps'] for f in s['failures']]
        self.assertFalse(report['ok'])
        self.assertTrue(any(f.startswith('inject: expected [\'@cache\']') for f in failures), failures)


class Helpers(unittest.TestCase):
    def test_command_name_skips_global_options(self):
        self.assertEqual(simulate.command_name(['--actor', 'subagent', '--json', 'search', 'x']), 'search')
        self.assertEqual(simulate.command_name(['read', '@a']), 'read')

    def test_subset(self):
        self.assertIsNone(simulate.subset({'a': {'b': 1}}, {'a': {'b': 1, 'c': 2}, 'd': 3}))
        self.assertEqual(simulate.subset({'a': [1]}, {'a': [1, 2]}), 'a: expected [1], got [1, 2]')

    def test_scenarios_are_well_formed(self):
        kinds = {'hook', 'cli', 'fault', 'session'}
        for path in sorted(simulate.SCENARIOS.glob('*.json')):
            scenario = json.loads(path.read_text(encoding='utf-8'))
            with self.subTest(path.name):
                self.assertEqual(scenario['name'], path.stem)
                self.assertTrue(set(scenario.get('harnesses', simulate.HARNESSES)) <= set(simulate.HARNESSES))
                for step in scenario['steps']:
                    self.assertTrue(kinds & set(step), step)


if __name__ == '__main__':
    unittest.main()
