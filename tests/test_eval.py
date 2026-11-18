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

    def mapped(self):
        """The project is a git repo with a codebase map; a negative prompt asked twice, first and later in its session."""
        (self.base / 'project' / '.git').mkdir(parents=True)
        capture('project codebase map', 'Layout: src holds the code.', TAGS, 'demo', self.root, type='map', files=[str((self.base / 'project').resolve())])
        return {'domain': 'demo', 'cwd': str(self.base / 'project'), 'retrieval': [
            {'prompt': 'suggest a good python variable name', 'expect': []},
            {'prompt': 'suggest a good python variable name', 'expect': [], 'first_prompt': False},
        ]}

    def test_the_whole_hook_counts_a_map_a_negative_got(self):
        report = EVAL.evaluate(self.mapped(), self.root, hooked=True)
        r = report['retrieval']
        self.assertEqual(r['false_injection'], 0.0)   # notes only, comparable with earlier runs
        kinds = r['false_injection_by_kind']
        self.assertEqual(kinds['maps']['rate'], 0.5)   # the first prompt of a session gets the map; a later one does not
        self.assertEqual(kinds['notes']['rate'], 0.0)
        self.assertEqual(kinds['any']['rate'], 0.5)   # the later prompt gets nothing at all
        self.assertEqual(kinds['maps']['prompts'][0]['injected'][0][:5], 'demo:')

    def test_the_hook_pass_puts_the_usage_diary_back(self):
        fixture = self.mapped()
        turns = self.root / '.turns'
        before = {p.name: p.read_bytes() for p in turns.glob('usage-*.jsonl')} if turns.exists() else {}
        EVAL.evaluate(fixture, self.root, hooked=True)
        after = {p.name: p.read_bytes() for p in turns.glob('usage-*.jsonl')}
        self.assertEqual(before, after)

    def test_a_second_run_on_the_same_store_measures_the_same(self):
        fixture = self.mapped()
        first = EVAL.evaluate(fixture, self.root, hooked=True)['retrieval']['false_injection_by_kind']
        second = EVAL.evaluate(fixture, self.root, hooked=True)['retrieval']['false_injection_by_kind']
        self.assertEqual({k: v['rate'] for k, v in first.items()}, {k: v['rate'] for k, v in second.items()})
        self.assertEqual(list((self.root / '.turns').glob('*.current.json')), [])   # no eval session state is left behind

    def test_session_start_runs_first_so_listed_titles_suppress_the_overview(self):
        capture('Gateway timeout', 'Raise the gateway timeout for uploads.', TAGS, 'demo', self.root)
        fixture = {'domain': 'demo', 'cwd': str(self.base / 'project'), 'retrieval': [{'prompt': 'write a haiku about autumn leaves', 'expect': []}]}
        kinds = EVAL.evaluate(fixture, self.root, hooked=True)['retrieval']['false_injection_by_kind']
        self.assertEqual(kinds['overview']['rate'], 0.0)

    def test_session_start_does_not_auto_resolve_notes_during_the_measurement(self):
        from unittest.mock import patch
        from core import runtime
        calls = []
        with patch.object(runtime, 'auto_resolve', side_effect=lambda *a, **k: calls.append(a) or []):
            # the eval's own patch wins inside each case, so the real one is never reached
            EVAL.evaluate(self.mapped(), self.root, hooked=True)
        self.assertEqual(calls, [])

    def test_by_kind_is_not_measured_in_place(self):
        report = EVAL.evaluate(self.mapped(), self.root)
        self.assertIsNone(report['retrieval']['false_injection_by_kind'])

    def test_copy_never_touches_the_store(self):
        path = self.base / 'fixture.json'
        path.write_text(json.dumps(self.mapped()), encoding='utf-8')
        snapshot = lambda: {p.relative_to(self.root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        before = snapshot()
        report = EVAL.run(path, self.root, copy=True)
        self.assertEqual(snapshot(), before)
        self.assertEqual(report['retrieval']['false_injection_by_kind']['maps']['rate'], 0.5)
        self.assertIsNone(report['copy'])
        self.assertEqual(report['reindexed'], [])   # a current index is measured as it is

    def test_copy_refreshes_a_stale_index_before_measuring(self):
        # A schema bump leaves the index stale until SessionStart; the copy must not measure the lexical fallback.
        from unittest.mock import patch
        from core import search
        deploy = capture('Deploy the package with install.py', 'Run install.py --plan, then --apply-plan.', TAGS, 'demo', self.root)
        fixture = {'domain': 'demo', 'cwd': str(self.base / 'project'),
                   'retrieval': [{'prompt': 'how do I deploy the package?', 'expect': ['demo:' + deploy['id']]}]}
        path = self.base / 'fixture.json'
        path.write_text(json.dumps(fixture), encoding='utf-8')
        with patch.object(search, 'INDEX_VERSION', search.INDEX_VERSION + 1):
            report = EVAL.run(path, self.root, copy=True)
            live = search._fresh('demo', self.root, search.inventory('demo', self.root, retired=True))
        self.assertIn('demo', report['reindexed'])
        self.assertFalse(live)   # the store itself stays stale: only the copy was rebuilt
        self.assertEqual(report['retrieval']['hit@3'], 1.0)

    def test_stale_ref_is_a_fixture_error(self):
        fixture = {'domain': 'demo', 'cwd': str(self.base / 'project'), 'retrieval': [{'prompt': 'x y', 'expect': ['demo:deadbeef']}]}
        with self.assertRaisesRegex(ValueError, 'does not resolve'):
            EVAL.evaluate(fixture, self.root)


if __name__ == '__main__':
    unittest.main()
