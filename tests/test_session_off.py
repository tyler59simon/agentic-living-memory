"""The per-session off switch: `alm off` / `alm on` / `alm status` and LIVING_MEMORY_OFF=1 at launch.
Off means the session sees no memory and leaves nothing behind."""
import contextlib
import io
import json
from unittest.mock import patch

from test_core import StoreFixture
from core import runtime
from core.cli import main


class SessionOff(StoreFixture):
    def setUp(self):
        super().setUp()
        self.cwd = str(self.base / 'project')
        self.payload = {'cwd': self.cwd, 'session_id': 'sess-1', 'prompt': 'how does the cache work'}
        env = patch.dict('os.environ', {'CLAUDE_CODE_SESSION_ID': 'sess-1', 'LIVING_MEMORY_OFF': ''})
        env.start();self.addCleanup(env.stop)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--json', *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def hook(self, event, **extra):
        return runtime.process('claude', event, {**self.payload, **extra}, self.root)

    def state(self):
        return json.loads(runtime._pointer(self.root, 'sess-1').read_text(encoding='utf-8'))

    def test_off_on_and_status_use_the_session_from_the_environment(self):
        self.hook('session-start')
        code, result = self.cli('status')
        self.assertEqual((code, result['memory'], result['known']), (0, 'on', True))
        code, result = self.cli('off')
        self.assertEqual((code, result['memory'], result['reason']), (0, 'off', 'switch'))
        self.assertEqual(result['session'], runtime.session_hash('sess-1'))   # never the raw ID
        self.assertEqual(self.cli('status')[1]['memory'], 'off')
        code, result = self.cli('on')
        self.assertEqual((code, result['memory']), (0, 'on'))
        self.assertNotIn('off', self.state());self.assertTrue(self.state()['resumed'])

    def test_explicit_session_flag_and_missing_session(self):
        code, result = self.cli('off', '--session', 'other')
        self.assertEqual((code, result['memory']), (0, 'off'))
        self.assertEqual(self.cli('status')[1]['memory'], 'on')   # sess-1 is untouched
        with patch.dict('os.environ', {'CLAUDE_CODE_SESSION_ID': ''}):
            code, result = self.cli('off')
        self.assertEqual(code, 2);self.assertIn('no_session', result['error']['message'])

    def test_off_session_gets_one_notice_then_nothing_and_leaves_no_trace(self):
        self.note()
        self.hook('session-start');self.cli('off')
        before = self.state()
        delivery = self.root / 'hooks/last-delivery.json';stamp = delivery.read_text(encoding='utf-8')
        self.assertEqual(self.hook('user-prompt'), {'context': runtime.OFF_NOTICE})
        self.assertEqual(self.hook('user-prompt'), {})
        self.assertEqual(self.hook('pre-tool-use', tool_name='Bash', tool_input={'command': 'echo hi'}), {})
        self.assertEqual(self.hook('stop'), {})
        after = self.state()
        self.assertEqual({k: v for k, v in after.items() if k != 'off_told'}, before)   # no open_turns, shown, ledger fields
        self.assertEqual(delivery.read_text(encoding='utf-8'), stamp)
        self.assertEqual(list((self.root / '.turns').glob('usage-*.jsonl')), [])
        self.assertEqual(self.hook('session-start', source='compact'), {'context': runtime.OFF_NOTICE})

    def test_protected_store_files_stay_guarded_while_off(self):
        self.hook('session-start');self.cli('off')
        target = next((self.root / 'domains/demo').glob('*.md'), self.root / 'domains/demo/INDEX.md')
        result = self.hook('pre-tool-use', tool_name='Write', tool_input={'file_path': str(target), 'content': 'x'})
        self.assertIn('deny', result)

    def test_cli_refuses_reads_and_writes_while_off(self):
        self.hook('session-start');self.cli('off')
        for argv in (('search', 'cache'), ('capture', '--session', 'sess-1', '--title', 'T', '--summary', 'S.',
                                           '--tag', 'subject/project', '--tag', 'concern/decision')):
            with self.subTest(argv=argv[0]):
                code, result = self.cli(*argv)
                self.assertEqual(code, 2);self.assertIn('memory_off', result['error']['message'])
        self.assertEqual(self.cli('status')[0], 0)
        self.assertEqual(self.cli('version')[0], 0)
        # Another session is not affected.
        with patch.dict('os.environ', {'CLAUDE_CODE_SESSION_ID': 'sess-2'}):
            self.assertEqual(self.cli('--domain', 'demo', 'search', 'cache')[0], 0)

    def test_subagents_cannot_switch(self):
        code, result = self.cli('--actor', 'subagent', 'off')
        self.assertEqual(code, 2)
        self.assertEqual(self.cli('--actor', 'subagent', 'status')[0], 0)

    def test_environment_turns_the_whole_session_off(self):
        self.note()
        with patch.dict('os.environ', {'LIVING_MEMORY_OFF': '1'}):
            self.assertEqual(self.hook('session-start'), {})
            self.assertEqual(self.hook('user-prompt'), {})
            self.assertFalse(runtime._pointer(self.root, 'sess-1').exists())
            code, result = self.cli('status')
            self.assertEqual((result['memory'], result['reason'], result['known']), ('off', 'env', False))
            code, result = self.cli('on')
            self.assertEqual(code, 2);self.assertIn('env_off', result['error']['message'])
            self.assertEqual(self.cli('search', 'cache')[0], 2)

    def test_first_prompt_after_on_repeats_the_protocol(self):
        self.hook('session-start');self.cli('off');self.hook('user-prompt');self.cli('on')
        context = self.hook('user-prompt')['context']
        self.assertTrue(context.startswith(runtime.BACK_ON))
        self.assertIn('Living Memory session=sess-1', context)
        self.assertNotIn('resumed', self.state());self.assertNotIn('off_told', self.state())
        self.assertFalse(self.hook('user-prompt')['context'].startswith(runtime.BACK_ON))

    def test_hook_logs_for_an_off_session_carry_no_content(self):
        self.hook('session-start');self.cli('off')
        with patch('core.logs.content_allowed', return_value=True):
            self.hook('user-prompt');self.hook('user-prompt')
        rows = [json.loads(line) for f in (self.root / 'logs').glob('*.jsonl') for line in f.read_text(encoding='utf-8').splitlines() if line.strip()]
        hooks = [r for r in rows if r.get('kind') == 'hook' and r.get('event') == 'user-prompt']
        self.assertTrue(hooks)
        self.assertTrue(all('content' not in r for r in hooks))

    def test_render_reads_as_a_sentence(self):
        from core.render import render
        self.assertEqual(render({'ok': True, 'session': 'abc', 'memory': 'off', 'reason': 'switch'}),
                         'Living Memory is off for this session (abc).')
        self.assertIn('LIVING_MEMORY_OFF=1', render({'ok': True, 'session': 'abc', 'memory': 'off', 'reason': 'env', 'known': False}))
