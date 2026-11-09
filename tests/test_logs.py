"""Operational log (logs/log-*.jsonl): hook runs, command runs and swallowed exceptions; metadata only."""
import contextlib, io, json, os, sys, unittest
from unittest.mock import patch
from test_core import StoreFixture
from core import logs, runtime
from core.cli import main
from core.notes import capture

TAGS = ['subject/project', 'concern/decision']
SECRET_WORDS = ('zebra', 'quokka')


class LogFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        level = patch.dict(os.environ, {'LIVING_MEMORY_LOG_LEVEL': '', 'LIVING_MEMORY_LOG_CONTENT': ''})
        level.start(); self.addCleanup(level.stop)
        logs.use(self.root)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--domain', 'demo', *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def payload(self, session='s1', **extra):
        return {'cwd': str(self.base / 'project'), 'session_id': session, **extra}

    def text(self):
        return ''.join(p.read_text(encoding='utf-8') for p in (self.root / 'logs').glob('log-*.jsonl'))

    def rows(self, **filters):
        return logs.entries(self.root, filters.pop('level', 'debug'), limit=1000, **filters)[0]


class Log(LogFixture):
    def test_lines_are_json_and_levels_filter(self):
        logs.write('debug', 'internal', 'a')
        logs.write('info', 'internal', 'b')
        logs.write('error', 'internal', 'c', extra=1)
        lines = self.text().splitlines()
        self.assertEqual([json.loads(l)['event'] for l in lines], ['b', 'c'])  # default level is info
        row = json.loads(lines[1])
        self.assertEqual((row['level'], row['kind'], row['extra']), ('error', 'internal', 1))
        self.assertIn('ts', row); self.assertIn('pid', row)
        with patch.dict(os.environ, {'LIVING_MEMORY_LOG_LEVEL': 'warning'}):
            logs.write('info', 'internal', 'd'); logs.write('warning', 'internal', 'e')
        with patch.dict(os.environ, {'LIVING_MEMORY_LOG_LEVEL': 'off'}):
            logs.write('error', 'internal', 'f')
        self.assertEqual([r['event'] for r in self.rows()], ['e', 'c', 'b'])  # newest first
        self.assertEqual([r['event'] for r in self.rows(level='error')], ['c'])

    def test_an_unwritable_log_never_raises(self):
        (self.root / 'logs').write_text('not a folder')
        logs.write('error', 'internal', 'x')
        logs.swallowed('x', ValueError('boom'))

    def test_swallowed_redacts_and_keeps_only_frames(self):
        def fail():
            secret = 'sk-' + 'a' * 40  # source line text must not reach the log
            raise ValueError('token api_key=' + secret)
        try: fail()
        except ValueError as exc: logs.swallowed('test.site', exc)
        row = self.rows()[0]
        self.assertEqual((row['level'], row['kind'], row['event'], row['error']['type']), ('warning', 'internal', 'test.site', 'ValueError'))
        self.assertNotIn('a' * 40, self.text())
        self.assertIn('redacted', row['error']['message'])
        self.assertTrue(any(f.endswith(' fail') for f in row['error']['frames']), row['error']['frames'])
        self.assertNotIn('source line', self.text())

    def test_hook_runs_are_logged_without_prompt_text(self):
        capture('Cache eviction policy', 'The cache uses LRU eviction.', TAGS, 'demo', self.root)
        runtime.process('claude', 'session-start', self.payload(), self.root)
        runtime.process('claude', 'user-prompt', self.payload(prompt=f'cache eviction policy {SECRET_WORDS[0]} {SECRET_WORDS[1]}'), self.root)
        hooks = self.rows(kind='hook')
        self.assertEqual([r['event'] for r in hooks], ['user-prompt', 'session-start'])
        prompt = hooks[0]
        self.assertEqual((prompt['harness'], prompt['domain'], prompt['session'], prompt['outcome']),
                         ('claude', 'demo', runtime.session_hash('s1'), ['context']))
        self.assertGreater(prompt['context_chars'], 0); self.assertIsInstance(prompt['ms'], float)
        text = self.text()
        for word in SECRET_WORDS + ('Cache eviction', 'LRU', 's1"'):
            self.assertNotIn(word, text)

    def test_a_failing_hook_logs_an_error_and_still_raises(self):
        with patch('core.runtime._process', side_effect=RuntimeError('broken')):
            with self.assertRaises(RuntimeError):runtime.process('claude', 'user-prompt', self.payload(prompt='x'), self.root)
        row = self.rows(kind='hook')[0]
        self.assertEqual((row['level'], row['error']['type'], row['error']['message']), ('error', 'RuntimeError', 'broken'))

    def test_housekeeping_failures_are_logged(self):
        with patch('core.sync.sync_all', side_effect=OSError('vault locked')):
            self.assertEqual(runtime.process('claude', 'session-end', self.payload(), self.root), {})
        internal = self.rows(kind='internal')
        self.assertEqual([(r['event'], r['error']['message']) for r in internal], [('runtime.sync_vaults', 'vault locked')])

    def test_command_runs_are_logged(self):
        note = capture('Cache eviction policy', 'LRU.', TAGS, 'demo', self.root)
        self.assertEqual(self.cli('read', 'demo:' + note['id'])[0], 0)
        code, _ = self.cli('update', 'demo:' + note['id'], '--body', 'TTL too.', '--expected-revision', '0' * 64)
        self.assertEqual(code, 6)
        self.assertEqual(self.cli('--actor', 'viewer', 'read', 'demo:' + note['id'])[0], 0)  # debug: not kept at info
        with patch.dict(os.environ, {'LIVING_MEMORY_LOG_LEVEL': 'debug'}):
            self.assertEqual(self.cli('--actor', 'viewer', 'read', 'demo:' + note['id'])[0], 0)
        commands = self.rows(kind='command')
        self.assertEqual([(r['event'], r['level'], r['exit_code']) for r in commands],
                         [('read', 'debug', 0), ('update', 'warning', 6), ('read', 'info', 0)])
        self.assertEqual(commands[0]['actor'], 'viewer')
        self.assertEqual(commands[1]['error']['code'], 'stale')
        self.assertEqual((commands[2]['actor'], commands[2]['domain']), ('main', 'demo'))
        self.assertNotIn('TTL too', self.text())  # never the arguments

    def test_logs_command_filters_and_is_not_itself_logged(self):
        logs.write('warning', 'internal', 'site.a'); logs.write('info', 'hook', 'stop')
        code, out = self.cli('logs', '--level', 'warning')
        self.assertEqual(code, 0, out)
        self.assertEqual(([r['event'] for r in out['entries']], out['matched'], out['level_setting']), (['site.a'], 1, 'info'))
        self.assertEqual([r['event'] for r in self.cli('logs', '--kind', 'hook')[1]['entries']], ['stop'])
        self.assertEqual(self.cli('logs', '--event', 'site.a', '--limit', '0')[1]['matched'], 1)
        self.assertFalse(self.rows(kind='command'))
        self.assertEqual(self.cli('--actor', 'subagent', 'logs')[0], 0)  # read-only
        self.assertIn('logs', runtime.ALLOWED_SUBCOMMANDS); self.assertNotIn('logs', runtime.MUTATING_SUBCOMMANDS)

    def test_prune_and_old_days_are_ignored(self):
        logs.write('error', 'internal', 'today')
        old = self.root / 'logs' / 'log-2020-01-01.jsonl'
        old.write_text(json.dumps({'ts': '2020-01-01T00:00:00+00:00', 'level': 'error', 'kind': 'internal', 'event': 'old'}) + '\n', encoding='utf-8')
        (self.root / 'logs' / 'log-2020-01-01.lock').write_bytes(b'\0')
        self.assertEqual([r['event'] for r in logs.entries(self.root, days=30)[0]], ['today'])
        self.assertEqual(logs.prune(self.root), 2)
        self.assertFalse(old.exists()); self.assertTrue(list((self.root / 'logs').glob('log-*.jsonl')))

    def test_doctor_reports_recent_errors(self):
        logs.write('error', 'internal', 'site.b', error={'type': 'OSError', 'message': 'disk'})
        code, report = self.cli('doctor')
        self.assertEqual(report['logs']['errors_7d'], 1)
        self.assertEqual(report['logs']['last_error']['event'], 'site.b')
        self.assertTrue(any('errors logged' in w for w in report['warnings']), report['warnings'])


class Decisions(LogFixture):
    """At debug, each hook line carries why the hook did what it did: refs, numbers and reason codes, never text."""
    def setUp(self):
        super().setUp()
        debug = patch.dict(os.environ, {'LIVING_MEMORY_LOG_LEVEL': 'debug'})
        debug.start(); self.addCleanup(debug.stop)
        self.project = self.base / 'project'

    def hook(self, event, **extra):
        result = runtime.process('claude', event, self.payload(**extra), self.root)
        return result, self.rows(kind='hook')[0]

    def edit(self, path, **extra):
        return self.hook('pre-tool-use', tool_name='Edit', tool_input={'file_path': str(path)}, **extra)

    def memory(self, rest):
        return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'

    def test_prompt_detail(self):
        cache = capture('Cache eviction policy', 'The cache uses LRU eviction.', TAGS, 'demo', self.root)
        capture('Deploy steps', 'Run install.py then the hook setup.', TAGS, 'demo', self.root)
        ref = 'demo:' + cache['id']
        _, row = self.hook('session-start')
        self.assertEqual(row['detail']['indexed'], True); self.assertIn('degrade', row['detail'])
        self.assertNotIn('turn', row)  # no prompt yet; None fields are left out
        _, row = self.hook('user-prompt', prompt=f'what is our cache eviction policy {SECRET_WORDS[0]}')
        detail = row['detail']
        self.assertEqual((detail['path'], detail['injected']), ('injected', [ref]))
        self.assertEqual(row['turn'], runtime.session_hash(json.loads(runtime._pointer(self.root, 's1').read_text())['turn']))
        kept = [c for c in detail['candidates'] if c['ref'] == ref][0]
        self.assertTrue(kept['gate'].startswith('kept:'), kept)
        self.assertEqual(kept['rank'], 1); self.assertIsInstance(kept['engine_score'], float)
        _, row = self.hook('user-prompt', prompt='cache eviction policy again')
        self.assertEqual((row['detail']['path'], row['detail']['suppressed_shown']), ('all_shown', [ref]))
        _, row = self.hook('user-prompt', prompt='please explain cache layout database migration rollout')
        self.assertIn('dropped:weak', [c['gate'] for c in row['detail']['candidates']])
        self.assertEqual(row['detail']['path'], 'overview_done')
        for word in (SECRET_WORDS[0], 'Cache eviction', 'LRU', 'migration'):
            self.assertNotIn(word, self.text())

    def test_no_detail_at_info(self):
        capture('Cache eviction policy', 'LRU.', TAGS, 'demo', self.root)
        with patch.dict(os.environ, {'LIVING_MEMORY_LOG_LEVEL': 'info'}):
            runtime.process('claude', 'user-prompt', self.payload(prompt='cache eviction policy'), self.root)
        row = self.rows(kind='hook')[0]
        self.assertNotIn('detail', row); self.assertNotIn('turn', row)

    def test_stop_reasons(self):
        reason = lambda **extra: self.hook('stop', **extra)[1]['detail']['reason']
        file = self.project / 'a.py'
        self.assertEqual(self.hook('stop', cwd=str(self.base / 'elsewhere'))[1]['detail']['reason'], 'unbound')
        self.assertEqual(runtime.process('claude', 'stop', {'cwd': str(self.project)}, self.root), {})
        self.assertEqual(self.rows(kind='hook')[0]['detail']['reason'], 'unknown_session')
        self.edit(file, session='early')   # a change before any prompt: nothing to remind about yet
        self.assertEqual(reason(session='early'), 'no_turns')
        self.hook('user-prompt', prompt='summarize the file')
        self.assertEqual(reason(), 'no_change')
        self.hook('user-prompt', prompt='remember that we fix bugs with a test'); self.edit(file)
        self.assertEqual(reason(stop_hook_active=True), 'stop_active')
        self.hook('user-prompt', prompt='fix the next bug'); self.edit(file)
        self.assertEqual(reason(), 'files_only')
        self.edit(file)
        self.assertEqual(reason(), 'files_only')
        self.hook('user-prompt', prompt='from now on, use tabs')
        self.assertEqual(reason(), 'nudged')   # raised in the prompt's own context, so the turn ends quietly
        self.hook('user-prompt', prompt='remember that we use tabs')
        self.assertEqual(reason(), 'reminded:remember')
        self.hook('user-prompt', prompt='edit again'); self.edit(file)
        self.assertEqual(self.cli('capture-finalize', '--session', 's1')[0], 0)
        self.assertEqual(reason(), 'receipt')

    def test_tool_decisions(self):
        note = capture('Cache eviction policy', 'LRU.', TAGS, 'demo', self.root)
        self.hook('user-prompt', prompt='work on it')
        detail = lambda result_row: result_row[1]['detail']
        result, row = self.edit(note['path'])
        self.assertIn('deny', result); self.assertEqual(row['detail']['decision'], 'deny:protected:domains')
        self.assertEqual(detail(self.edit(self.root / 'bindings.json'))['decision'], 'deny:protected:config')
        self.assertEqual(detail(self.hook('pre-tool-use', tool_name='Read', tool_input={'file_path': str(self.project / 'a.py')}))['mutation'], 'skipped:read_tool')
        shell = lambda command, **extra: detail(self.hook('pre-tool-use', tool_name='Bash', tool_input={'command': command}, **extra))
        found = shell(self.memory('search cache'))
        self.assertEqual((found['decision'], found['subcommand'], found['mutation']), ('allow', 'search', 'skipped:memory_cmd'))
        self.assertEqual(shell(self.memory("capture 'T' 'B'"), agent_id='sub')['decision'], 'deny:subagent')
        self.assertEqual(shell('git status')['mutation'], 'skipped:readonly_shell')
        self.assertEqual(detail(self.edit(self.project / 'a.py', permission_mode='plan'))['mutation'], 'skipped:plan_mode')
        self.assertEqual(detail(self.edit(self.project / 'a.py', agent_id='sub'))['mutation'], 'skipped:subagent')
        tracked = detail(self.edit(self.project / 'a.py'))
        self.assertEqual((tracked['decision'], tracked['mutation'], tracked['touched']), ('none', 'tracked', 1))

    def test_command_detail_and_reason(self):
        code, made = self.cli('capture', 'Cache eviction policy', 'LRU.', '--tag', 'subject/project', '--tag', 'concern/decision')
        self.assertEqual(code, 0, made)
        row = self.rows(kind='command')[0]
        self.assertEqual((row['status'], row['operation_id'], row['ref']), ('created', made['operation_id'], 'demo:' + made['id']))
        code, _ = self.cli('update', 'demo:' + made['id'], '--body', 'TTL.', '--expected-revision', '0' * 64)
        self.assertEqual(self.rows(kind='command')[0]['error']['reason'], 'stale_revision')
        with patch.dict(os.environ, {'LIVING_MEMORY_LOG_LEVEL': 'info'}):
            self.cli('read', 'demo:' + made['id'])
        self.assertNotIn('status', self.rows(kind='command')[0])


class Content(LogFixture):
    """Text in the log only for a store marked as a sandbox that is not the configured data root."""
    def setUp(self):
        super().setUp()
        on = patch.dict(os.environ, {'LIVING_MEMORY_LOG_CONTENT': '1'})
        on.start(); self.addCleanup(on.stop)
        capture('Cache eviction policy', 'The cache uses LRU eviction.', TAGS, 'demo', self.root)

    def prompt(self):
        return runtime.process('claude', 'user-prompt', self.payload(prompt='what is our cache eviction policy'), self.root)

    def mark(self):
        (self.root / 'sandbox.json').write_text('{"sandbox": true}', encoding='utf-8')

    def test_refused_without_the_marker(self):
        self.prompt()
        self.assertNotIn('content', self.rows(kind='hook')[0])
        self.assertEqual([r['event'] for r in self.rows(kind='internal')], ['content_logging_refused'])
        self.assertEqual(logs.content_state(self.root), 'refused')
        self.assertNotIn('LRU', self.text())

    def test_refused_on_the_configured_root(self):
        self.mark()
        with patch('core.runtime._configured_root', return_value=self.root.resolve()):
            self.prompt()
            self.assertEqual(logs.content_state(self.root), 'refused')
        self.assertNotIn('content', self.rows(kind='hook')[0])
        self.assertNotIn('LRU', self.text())

    def test_off_without_the_env_var(self):
        self.mark()
        with patch.dict(os.environ, {'LIVING_MEMORY_LOG_CONTENT': ''}):
            self.prompt()
            self.assertEqual(logs.content_state(self.root), 'off')
        self.assertNotIn('content', self.rows(kind='hook')[0]); self.assertFalse(self.rows(kind='internal'))

    def test_logged_in_a_sandbox(self):
        self.mark()
        with patch('core.runtime._configured_root', return_value=None):
            result = self.prompt()
            code, _ = self.cli('search', 'cache')
            self.assertEqual(self.cli('doctor')[1]['logs']['content'], 'on')
        hook = self.rows(kind='hook')[0]['content']
        self.assertEqual(hook['context'], result['context'])
        self.assertEqual(hook['prompt'], 'what is our cache eviction policy')
        command = [r for r in self.rows(kind='command') if r['event'] == 'search'][0]['content']
        self.assertIn('"search"', command['argv']); self.assertIn('Cache eviction policy', command['result'])

if __name__ == '__main__':
    unittest.main()
