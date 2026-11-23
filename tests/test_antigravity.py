import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from core import runtime, wiring
from core.capture import finalize, status
from core.governance import apply
from core.init_domain import init_domain
from core.notes import capture
from core.repository import read_json
from hooks import antigravity_hooks as agy
from integrations import setup
from test_hook_adapters import run


class AntigravityLifecycle(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.cwd = base / 'project'
        self.cwd.mkdir()
        self.root = base / 'store'
        env = patch.dict(os.environ, {
            'LIVING_MEMORY_ROOT': str(self.root),
            'LIVING_MEMORY_NO_BACKGROUND': '1',
            'LIVING_MEMORY_OFF': '',
        })
        env.start()
        self.addCleanup(env.stop)
        init_domain('demo', [str(self.cwd)], self.root)
        (self.root / 'tag_taxonomy.json').write_text(
            json.dumps({'tags': ['subject/project', 'concern/knowledge']}), encoding='utf-8')
        self.session = 'probe-session'
        self.artifact = base / 'antigravity-cli' / 'brain' / self.session
        self.transcript = self.artifact / '.system_generated' / 'logs' / 'transcript_full.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.base = {
            'conversationId': self.session,
            'cwd': str(self.cwd),
            'artifactDirectoryPath': str(self.artifact),
            'transcriptPath': str(self.transcript),
        }

    def hook(self, event, **fields):
        return agy.handle(event, {**self.base, **fields})

    def prompt(self, index, text, kind='USER_INPUT'):
        with self.transcript.open('a', encoding='utf-8') as fd:
            print(json.dumps({'step_index': index, 'type': kind, 'content': text}), file=fd)
        return self.hook('PreInvocation', invocationNum=0, initialNumSteps=index + 1, lastUserInput=text)

    def stop(self, execution=0, **extra):
        fields = {'fullyIdle': True, 'terminationReason': 'NO_TOOL_CALL', 'executionNum': execution}
        return self.hook('Stop', **{**fields, **extra})

    def state(self):
        return read_json(runtime._pointer(self.root, self.session))

    def test_real_recall_and_identical_prompts_with_background_wakeup(self):
        capture('Quartz door access', 'The quartz door access phrase is FROSTEDPINE.',
                ['subject/project', 'concern/knowledge'], 'demo', self.root)
        prompt = 'What is the quartz door access phrase?'
        start = self.hook('SessionStart', lastUserInput=prompt)
        self.assertIn('Living Memory session=', json.dumps(start))
        result = self.prompt(0, prompt)
        self.assertIn('FROSTEDPINE', json.dumps(result))
        first = self.state()['turn']
        self.assertEqual(self.hook('PreInvocation', invocationNum=1, initialNumSteps=4, lastUserInput=prompt), {})
        self.assertEqual(self.hook('PreInvocation', invocationNum=0, initialNumSteps=1, lastUserInput=prompt), {})
        self.assertEqual(self.stop(), {'decision': 'allow'})
        self.assertEqual(self.prompt(4, prompt, 'SYSTEM_MESSAGE'), {})
        self.assertEqual(self.state()['turn'], first)
        self.assertIn('injectSteps', self.prompt(7, prompt))
        self.assertNotEqual(self.state()['turn'], first)
        self.assertNotIn(prompt, json.dumps(self.state()))

    def test_stop_continues_once_and_keeps_the_capture_turn(self):
        prompt = 'Remember that the quartz door opens only on Tuesdays.'
        self.prompt(0, prompt)
        turn = self.state()['turn']
        self.assertEqual(self.stop(fullyIdle=False), {'decision': 'allow'})
        outcome = self.stop()
        self.assertEqual(outcome['decision'], 'continue')
        self.assertIn('capture', outcome['reason'])
        self.assertEqual(self.stop(), {'decision': 'allow'})
        self.assertEqual(self.hook('PreInvocation', invocationNum=2, initialNumSteps=7, lastUserInput=prompt), {})
        self.assertEqual(self.state()['turn'], turn)
        finalize(self.session, turn, root=self.root)
        self.assertEqual(self.stop(1, continuationActive=False), {'decision': 'allow'})
        self.assertEqual(self.state()['antigravity_turn']['phase'], 'closed')
        self.assertTrue(status(self.session, turn, self.root))

    def test_failed_stop_closes_without_continuation(self):
        self.prompt(0, 'Remember that the quartz door opens only on Tuesdays.')
        self.assertEqual(self.stop(terminationReason='ERROR', error='model failed'), {'decision': 'allow'})
        self.assertEqual(self.state()['antigravity_turn']['phase'], 'closed')

    def test_turn_marker_stays_unchanged_while_memory_is_off(self):
        self.prompt(0, 'hello')
        self.stop()
        marker = self.state()['antigravity_turn']
        runtime._update(self.root, self.session, lambda state: state.update(off=True))
        result = self.prompt(4, 'Remember that memory is off.')
        self.assertIn('Living Memory is off', json.dumps(result))
        self.assertEqual(self.stop(), {'decision': 'allow'})
        self.assertEqual(self.state()['antigravity_turn'], marker)

    def test_cli_only_and_unambiguous_workspace_fallback(self):
        for surface in ('antigravity', 'antigravity-ide'):
            artifact = str(self.artifact).replace('antigravity-cli', surface)
            with patch('core.storage.resolve_root') as resolve:
                self.assertEqual(self.hook('SessionStart', artifactDirectoryPath=artifact, lastUserInput='hello'), {})
                self.assertEqual(self.hook('SessionStart', cwd='', workspacePaths=[str(self.cwd), str(self.root)],
                                           lastUserInput='hello'), {})
                resolve.assert_not_called()
        self.assertIn('injectSteps', self.hook('SessionStart', cwd='', workspacePaths=[str(self.cwd)],
                                              lastUserInput='hello'))

    def test_tool_guard_and_native_permission_delegation(self):
        self.hook('SessionStart', lastUserInput='hello')
        for name, arg in (('write_to_file', 'TargetFile'), ('write_file', 'Path'), ('edit_file', 'Path')):
            result = self.hook('PreToolUse', toolCall={
                'name': name, 'args': {arg: str(self.root / 'bindings.json')},
            })
            self.assertEqual(result['decision'], 'deny')
        for args in (None, [], {'CommandLine': 'alm --json search test', 'Cwd': str(self.cwd)}):
            result = self.hook('PreToolUse', toolCall={'name': 'run_command', 'args': args})
            self.assertEqual(result, {'decision': 'allow'})

    def test_observed_child_start_only_gets_subagent_context(self):
        self.assertEqual(self.hook('SessionStart', lastUserInput=''), {})
        result = self.hook('PreInvocation', invocationNum=0, initialNumSteps=0, lastUserInput='')
        self.assertIn('subagents may only search/read', json.dumps(result))
        command = f'"{sys.executable}" "{runtime.SCRIPT}" --json capture --title Example'
        self.assertEqual(self.hook('PreToolUse', toolCall={
            'name': 'run_command', 'args': {'CommandLine': command},
        })['decision'], 'deny')
        self.assertEqual(self.stop(), {'decision': 'allow'})

    def test_wire_json_and_fail_open(self):
        payload = {**self.base, 'lastUserInput': 'memory'}
        with patch('core.runtime.process', return_value={'context': 'Cache λ'}):
            code, output, error = run('antigravity_hooks', 'SessionStart', payload)
        self.assertEqual((code, error), (0, ''))
        self.assertEqual(json.loads(output)['injectSteps'][0]['systemMessage']['systemMessage'], 'Cache λ')
        code, output, error = run('antigravity_hooks', 'PreToolUse', ['bad payload'])
        self.assertEqual((code, error), (0, ''))
        self.assertEqual(json.loads(output), {'decision': 'allow'})


class AntigravitySetup(unittest.TestCase):
    def test_named_group_ownership_and_doctor(self):
        folder = tempfile.TemporaryDirectory(prefix='antigravity config ')
        self.addCleanup(folder.cleanup)
        home = Path(folder.name)
        env = patch.dict(os.environ, {'ANTIGRAVITY_HOME': str(home)})
        env.start()
        self.addCleanup(env.stop)
        path = home / 'hooks.json'
        unrelated = {'my-hooks': {'enabled': False, 'Stop': [{'command': 'echo other'}]}}
        path.write_text(json.dumps(unrelated), encoding='utf-8')

        def apply_setup(**kwargs):
            plan = setup.setup_plan('antigravity', home, **kwargs)
            return apply(home, plan['plan_id'], plan['digest'])

        apply_setup()
        document = read_json(path)
        self.assertEqual(document['my-hooks'], unrelated['my-hooks'])
        self.assertFalse((home / 'AGENTS.md').exists())
        self.assertTrue(wiring.check('antigravity', setup)['handlers_current'])
        matcher = document[setup.ANTIGRAVITY_GROUP]['PreToolUse'][0]['matcher']
        self.assertIn('write_file', matcher.split('|'))
        path.write_text('{', encoding='utf-8')
        self.assertTrue(wiring.broken({'antigravity': wiring.check('antigravity', setup)}))
        path.write_text(json.dumps(document), encoding='utf-8')
        apply_setup()
        self.assertEqual(read_json(path), document)

        document['added-later'] = {'SessionStart': [{'command': 'echo hello'}]}
        document[setup.ANTIGRAVITY_GROUP]['enabled'] = False
        path.write_text(json.dumps(document), encoding='utf-8')
        self.assertTrue(wiring.broken({'antigravity': wiring.check('antigravity', setup)}))
        with self.assertRaisesRegex(ValueError, 'modified managed hook group'):
            setup.setup_plan('antigravity', home, uninstall=True)
        apply_setup(uninstall=True, adopt=True)
        self.assertEqual(read_json(path), {**unrelated, 'added-later': document['added-later']})


if __name__ == '__main__':
    unittest.main()
