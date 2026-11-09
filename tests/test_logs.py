"""Operational log (logs/log-*.jsonl): hook runs, command runs and swallowed exceptions; metadata only."""
import contextlib, io, json, os, unittest
from unittest.mock import patch
from test_core import StoreFixture
from core import logs, runtime
from core.cli import main
from core.notes import capture

TAGS = ['subject/project', 'concern/decision']
SECRET_WORDS = ('zebra', 'quokka')


class Log(StoreFixture):
    def setUp(self):
        super().setUp()
        level = patch.dict(os.environ, {'LIVING_MEMORY_LOG_LEVEL': ''})
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


if __name__ == '__main__':
    unittest.main()
