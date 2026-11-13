"""scripts/eval_retrieval.py: metrics over the real retrieval, session-start and signal code."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from test_core import StoreFixture
from core.notes import capture, short_ref

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('eval_retrieval', REPO / 'scripts' / 'eval_retrieval.py')
EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVAL)
SYNTHETIC = REPO / 'tests' / 'fixtures' / 'eval_synthetic.json'
TAGS = ['subject/project', 'concern/decision']


class SyntheticFixture(unittest.TestCase):
    def _tmp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);return tmp.name
    def test_known_good_result(self):
        report = EVAL.run(SYNTHETIC)
        r, s, g = report['retrieval'], report['session'], report['signals']
        self.assertEqual((r['cases'], r['positive'], r['negative']), (7, 5, 2))
        self.assertEqual(r['hit@3'], 1.0)
        self.assertEqual(r['false_injection'], 0.0)
        self.assertEqual(r['forbid_violations'], [])
        self.assertLessEqual(s['chars'], s['budget'])
        self.assertGreater(s['fallback_chars'], s['chars'])  # the full protocol is larger than the installed-block lines
        self.assertEqual(s['index_coverage'], 1.0)
        self.assertEqual(g['false_arm'], 0.0)
        # The knowledge base: questions only the fixture's docs and code answer, and no excerpt for the negatives
        # (these, the retrieval negatives and the long-prompt negatives).
        src = report['sources']
        self.assertEqual((src['positive'], src['negative']), (5, 12))
        self.assertEqual((src['hit@3'], src['prompt_recall'], src['false_injection']), (1.0, 1.0, 0.0))
        self.assertEqual((src['misses'], src['prompt_misses'], src['false_injections']), ([], [], []))
        self.assertEqual(g['wrong'], [])
        self.assertEqual(g['per_kind']['preference'], {'precision': 1.0, 'recall': 1.0, 'cases': 2})

    def test_misses_and_false_arms_are_counted(self):
        fixture = json.loads(SYNTHETIC.read_text(encoding='utf-8'))
        fixture['retrieval'] = [
            {'prompt': 'how do I deploy the package?', 'expect': ['@webcam'], 'forbid': ['@deploy']},  # wrong on purpose
            {'prompt': 'the webcam is still not detected', 'expect': ['@webcam']},
            {'prompt': 'webcam still not detected again', 'expect': []},  # injects, so a false injection
            {'prompt': 'recommend a good pasta recipe', 'expect': []},
        ]
        fixture['signals'] = [{'prompt': 'I prefer tabs', 'kind': None}, {'prompt': 'please summarize', 'kind': 'decision'}]
        path = Path(self._tmp()) / 'f.json'
        path.write_text(json.dumps(fixture), encoding='utf-8')
        report = EVAL.run(path)
        r, g = report['retrieval'], report['signals']
        self.assertEqual(r['hit@3'], 0.5)
        self.assertEqual(r['false_injection'], 0.5)
        self.assertEqual(len(r['misses']), 1)
        self.assertEqual(len(r['forbid_violations']), 1)
        self.assertEqual(g['false_arm'], 1.0)
        self.assertEqual(g['per_kind']['decision']['recall'], 0.0)
        self.assertEqual(g['per_kind']['preference']['precision'], 0.0)

    def test_unknown_synthetic_key_fails(self):
        fixture = json.loads(SYNTHETIC.read_text(encoding='utf-8'))
        fixture['retrieval'] = [{'prompt': 'deploy', 'expect': ['@missing']}]
        path = Path(self._tmp()) / 'f.json'
        path.write_text(json.dumps(fixture), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '@missing'):
            EVAL.run(path)


class RealStoreMode(StoreFixture):
    def test_refs_short_refs_and_titles_resolve(self):
        deploy = capture('Deploy the package with install.py', 'Run install.py --plan, then --apply-plan.', TAGS, 'demo', self.root)
        webcam = capture('Laptop webcam not detected', 'BIOS reset did not help.', TAGS, 'demo', self.root, type='bug')
        fixture = {'domain': 'demo', 'cwd': str(self.base / 'project'), 'retrieval': [
            {'prompt': 'how do I deploy the package?', 'expect': [short_ref('demo', deploy['id'])], 'forbid': ['Laptop webcam not detected']},
            {'prompt': 'webcam not detected', 'expect': ['demo:' + webcam['id']]},
        ]}
        before = sorted(p.read_bytes() for p in (self.root / 'domains' / 'demo' / 'notes').glob('*.md'))
        report = EVAL.evaluate(fixture, self.root)
        self.assertEqual(report['retrieval']['hit@3'], 1.0)
        self.assertEqual(report['session']['index_coverage'], 1.0)
        after = sorted(p.read_bytes() for p in (self.root / 'domains' / 'demo' / 'notes').glob('*.md'))
        self.assertEqual(before, after)  # canonical notes untouched

    def test_stale_ref_is_a_fixture_error(self):
        fixture = {'domain': 'demo', 'cwd': str(self.base / 'project'), 'retrieval': [{'prompt': 'x y', 'expect': ['demo:deadbeef']}]}
        with self.assertRaisesRegex(ValueError, 'does not resolve'):
            EVAL.evaluate(fixture, self.root)


if __name__ == '__main__':
    unittest.main()
