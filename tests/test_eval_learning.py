"""scripts/eval_learning.py and scripts/agents/run_agent.py: fixtures, isolation, measures and the agent adapter."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVAL = _load('eval_learning', REPO/'scripts'/'eval_learning.py')
AGENT = _load('run_agent', REPO/'scripts'/'agents'/'run_agent.py')

# A stand-in agent: reads the task on stdin, acts through the real CLI, prints one JSON report.
FAKE = r'''
import json, os, subprocess, sys
from pathlib import Path
behavior = sys.argv[1]
task = json.loads(sys.stdin.read())
def cli(*args):
    out = subprocess.run([sys.executable, task['cli'], '--root', task['root'], '--domain', task['domain'], *args],
                         capture_output=True, text=True, encoding='utf-8')
    return json.loads(out.stdout) if out.stdout.strip().startswith('{') else {}
report = {'outcome': 'succeeded', 'usage': 'used'}
if behavior == 'env':
    report.update(root=os.environ.get('LIVING_MEMORY_ROOT'), nested='CLAUDECODE' in os.environ, cwd=os.getcwd(),
                  objective=task['objective'], phase=task['phase'])
elif behavior == 'damage':
    for p in Path(task['root'], 'domains', 'demo', 'notes').rglob('*.md'):
        if 'Unrelated' in p.read_text(encoding='utf-8'): p.unlink()
elif behavior.startswith('learn'):
    cli('rebuild', 'demo')
    report['usage'] = 'none'
    if task['phase'] == 'training':
        method = 'agent_assertion' if behavior == 'learn-weak' else 'tool_check'
        exclusion = [] if behavior == 'learn-noexcl' else ['--do-not-use-when', 'The note file is missing: restore it instead']
        rec = cli('learn', 'record', '--objective', 'Repair a stale index', '--family', 'alm.index',
                  '--criterion', 'search returns the note', '--result', 'pass', '--method', method, '--observer', 'search',
                  '--excerpt', 'search returned the edited note', '--limitations', 'one store', '--outcome', 'succeeded')
        cand = cli('learn', 'candidate', 'create', '--title', 'Rebuild a stale index', '--summary', 'Rebuild when search lags.',
                   '--use-when', 'Search lags behind intact canonical notes', *exclusion, '--step', 'CMD rebuild DOMAIN',
                   '--step', "CMD search 'TERM' and confirm the note", '--done-when', 'The note is returned',
                   '--fallback', 'Run doctor; never delete canonical notes', '--evidence', rec['evidence'][0]['id'])
        report['candidate_id'] = cand['record']['id']
print(json.dumps(report))
'''


class Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='alm-eval-learning-test-')
        cls.fake = Path(cls.tmp.name)/'fake_agent.py'
        cls.fake.write_text(FAKE, encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        # The guard reads the user's configured store; tests must not depend on it.
        guard = patch.object(EVAL, 'real_store_fingerprint', return_value=None)
        guard.start(); self.addCleanup(guard.stop)

    def agent(self, behavior):
        return [sys.executable, str(self.fake), behavior]

    def run_eval(self, *args, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return EVAL.run(*args, **kw)

    def test_self_test_passes_every_scenario_and_condition(self):
        result = self.run_eval()
        tests = [r for r in result['results'] if r['phase'] == 'test']
        self.assertEqual({(r['scenario'], r['mode']) for r in tests}, {(s, m) for s in EVAL.SCENARIOS for m in EVAL.MODES})
        self.assertTrue(all(r['checked_success'] for r in result['results']))
        self.assertFalse(any(r['collateral'] or r['misapplied'] or r['false_success'] for r in tests))
        self.assertTrue(all(r['procedure_available'] == (r['mode'] != 'baseline') for r in tests))

    def test_doing_nothing_fails_every_scenario_and_counts_claims(self):
        result = self.run_eval(self.agent('nothing'), learned_from='scripted', label='idle')
        tests = [r for r in result['results'] if r['phase'] == 'test']
        self.assertFalse(any(r['checked_success'] for r in tests))
        self.assertTrue(all(r['false_success'] for r in tests))
        lookalike = {r['mode']: r['misapplied'] for r in tests if r['scenario'] == 'missing-note'}
        self.assertEqual(lookalike, {'baseline': False, 'manual': True, 'learned': True})
        summary = EVAL.summarize(result['results'])
        learned = next(c for c in summary['conditions'] if c['mode'] == 'learned')
        self.assertEqual((learned['agent'], learned['checked'], learned['misapplied']), ('idle', 0, '1/1'))

    def test_isolation_environment_and_blinded_task(self):
        context = EVAL.setup(Path(self.tmp.name)/'iso'/'store', 'stale-index', 'held-out-0', 'manual')
        root = Path(self.tmp.name)/'iso'/'store'
        with patch.dict(os.environ, {'CLAUDECODE': '1', 'LIVING_MEMORY_ROOT': '/real/store'}):
            report = EVAL.invoke(self.agent('env'), EVAL.request_for(root, context, 'test', 60), str(root.parent), 60)
        self.assertEqual(Path(report['root']), root)
        self.assertFalse(report['nested'])
        self.assertEqual(Path(report['cwd']).resolve(), root.parent.resolve())
        for word in ('manual', 'learned', 'baseline', 'runbook'):
            self.assertNotIn(word, report['objective'])
        # The trial folder is bound, so the agent's own hooks resolve the demo domain there.
        bindings = json.loads((root/'bindings.json').read_text(encoding='utf-8'))
        self.assertEqual(bindings['domains']['demo']['prefixes'], [str(root.parent.resolve())])

    def test_variants_differ_and_collateral_is_detected(self):
        self.assertNotEqual(EVAL.terms('training')[:2], EVAL.terms('held-out-0')[:2])
        result = self.run_eval(self.agent('damage'), learned_from='scripted', label='damage',
                               scenarios=('restore',), modes=('baseline',))
        row = result['results'][0]
        self.assertEqual(len(row['collateral']), 3)
        self.assertFalse(row['checked_success'])

    def test_agent_authored_learning_admits_only_eligible_candidates(self):
        good = self.run_eval(self.agent('learn'), label='good', scenarios=('stale-index',), modes=('learned',))
        training, test = good['results']
        self.assertEqual(good['learned_from'], 'agent')
        self.assertTrue(training['checked_success'] and training['eligible'] and training['admitted'])
        self.assertEqual(training['candidate']['title'], 'Rebuild a stale index')
        self.assertTrue(test['procedure_available'] and test['checked_success'])
        weak = self.run_eval(self.agent('learn-weak'), label='weak', scenarios=('stale-index',), modes=('learned',))
        training, test = weak['results']
        self.assertFalse(training['eligible'] or training['admitted'])
        self.assertTrue(training['evaluation_errors'])
        self.assertFalse(test['procedure_available'])
        loose = self.run_eval(self.agent('learn-noexcl'), label='loose', scenarios=('stale-index',), modes=('learned',))
        training, test = loose['results']
        self.assertTrue(training['checked_success'])
        self.assertFalse(training['eligible'] or training['admitted'])
        self.assertTrue(any('No exclusions' in e for e in training['evaluation_errors']))
        self.assertFalse(test['procedure_available'])

    def test_out_lines_and_summary(self):
        out = Path(self.tmp.name)/'runs.jsonl'
        self.run_eval(self.agent('nothing'), learned_from='scripted', label='idle', scenarios=('stale-plan',), out=str(out))
        rows = EVAL.load([out])
        self.assertEqual([r['phase'] for r in rows], ['training']+['test']*3)
        summary = EVAL.summarize(rows)
        self.assertEqual(sorted(c['mode'] for c in summary['conditions']), sorted(EVAL.MODES))
        text = EVAL.markdown(summary)
        self.assertIn('| idle | stale-plan | 0/1 | 0/1 | 0/1 |', text)


class Adapter(unittest.TestCase):
    TASK = dict(objective='Fix it', root='R', domain='demo', cli='lm.py', phase='test')

    def test_prompt_is_identical_across_conditions_and_training_asks_for_a_candidate(self):
        self.assertEqual(AGENT.prompt(self.TASK), AGENT.prompt(dict(self.TASK, mode='learned', procedure={'id': 'x'})))
        self.assertNotIn('learn record', AGENT.prompt(self.TASK))
        training = AGENT.prompt(dict(self.TASK, phase='training'))
        self.assertIn('learn record', training)
        self.assertIn('candidate_id', training)

    def test_parse_claude_wrapper(self):
        wrapper = dict(result='done {"outcome": "succeeded"}', total_cost_usd=0.02, num_turns=7,
                       usage=dict(input_tokens=10, cache_creation_input_tokens=5, cache_read_input_tokens=100, output_tokens=40),
                       modelUsage={'claude-haiku-5-5': {}})
        text, metrics = AGENT.parse(json.dumps(wrapper))
        self.assertEqual(AGENT.last_json(text), {'outcome': 'succeeded'})
        self.assertEqual(metrics, dict(input_tokens=115, output_tokens=40, cost_usd=0.02, turns=7, models=['claude-haiku-5-5']))

    def test_parse_codex_jsonl(self):
        events = [dict(type='thread.started', thread_id='t'),
                  dict(type='item.completed', item=dict(type='agent_message', text='thinking')),
                  dict(type='item.completed', item=dict(type='agent_message', text='{"outcome": "failed"}')),
                  dict(type='turn.completed', usage=dict(input_tokens=300, cached_input_tokens=200, output_tokens=30))]
        text, metrics = AGENT.parse('\n'.join(json.dumps(e) for e in events)+'\nnot json\n')
        self.assertEqual(AGENT.last_json(text), {'outcome': 'failed'})
        self.assertEqual(metrics, dict(input_tokens=300, output_tokens=30))

    def test_plain_output_and_prompt_delivery(self):
        echo = 'import sys,json; text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read(); print(json.dumps({"got": text[:20]}))'
        prompt = 'line one\nline "two"'
        for template in ([sys.executable, '-c', echo], [sys.executable, '-c', echo, '{prompt}']):
            code, out, err = AGENT.run(template, prompt, timeout=60)
            self.assertEqual(code, 0, err)
            self.assertEqual(AGENT.last_json(AGENT.parse(out)[0]), {'got': prompt[:20]})

    def test_timeout_kills_the_agent(self):
        code, _, err = AGENT.run([sys.executable, '-c', 'import time; time.sleep(30)'], '', timeout=1)
        self.assertEqual(code, 124)
        self.assertIn('timed out', err)


if __name__ == '__main__':
    unittest.main()
