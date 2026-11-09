"""Usage diary (.turns/usage-*.jsonl) and the stats command: refs and dates only, local, pruned with .turns."""
import contextlib, io, json, os, time, unittest
from unittest.mock import patch
from test_core import StoreFixture
from core import runtime, usage
from core.capture import prune
from core.cli import main
from core.notes import capture

TAGS = ['subject/project', 'concern/decision']
SECRET_WORDS = ('zebra', 'quokka')


class Diary(StoreFixture):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--domain', 'demo', *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def payload(self, session='s1', **extra):
        return {'cwd': str(self.base / 'project'), 'session_id': session, **extra}

    def rows(self):
        return usage.events(self.root)

    def diary_text(self):
        return ''.join(p.read_text(encoding='utf-8') for p in (self.root / '.turns').glob('usage-*.jsonl'))

    def test_hooks_and_commands_write_events(self):
        pref = capture('Prefer small commits', 'Keep commits small.', TAGS, 'demo', self.root, type='preference')
        cache = capture('Cache eviction policy', 'The cache uses LRU eviction.', TAGS, 'demo', self.root)
        runtime.process('claude', 'session-start', self.payload(), self.root)
        runtime.process('claude', 'user-prompt', self.payload(prompt=f'cache eviction policy {SECRET_WORDS[0]} {SECRET_WORDS[1]}'), self.root)
        self.assertEqual(self.cli('read', 'demo:' + cache['id'][:8])[0], 0)
        self.assertEqual(self.cli('update', 'demo:' + cache['id'], '--summary', 'LRU, now with a TTL.')[0], 0)
        code, dup = self.cli('capture', 'Prefer small commits', 'Keep commits small.', '--type', 'preference',
                             '--tag', 'subject/project', '--tag', 'concern/decision')
        self.assertEqual((code, dup['status']), (0, 'duplicate'), dup)
        events = [(r['e'], r['ref'], r['k']) for r in self.rows()]
        self.assertIn(('inject', 'demo:' + pref['id'], 'session'), events)
        self.assertIn(('inject', 'demo:' + cache['id'], 'prompt'), events)
        self.assertIn(('read', 'demo:' + cache['id'], None), events)
        self.assertIn(('update', 'demo:' + cache['id'], None), events)
        self.assertIn(('duplicate', 'demo:' + pref['id'], None), events)
        text = self.diary_text()
        for word in SECRET_WORDS + ('Cache eviction', 'Keep commits'):
            self.assertNotIn(word, text)  # no prompt text, titles or bodies
        session_hashes = {r['s'] for r in self.rows() if r['s']}
        self.assertEqual(session_hashes, {runtime.session_hash('s1')})

    def test_reminder_and_signal_capture(self):
        runtime.process('claude', 'user-prompt', self.payload(prompt='from now on, use tabs'), self.root)
        self.assertEqual([(r['e'], r['k']) for r in self.rows()], [('reminder', 'nudge-preference')])
        code, made = self.cli('capture', 'Use tabs', 'The user prefers tabs.', '--session', 's1', '--type', 'preference',
                              '--tag', 'subject/project', '--tag', 'concern/decision')
        self.assertEqual(code, 0, made)
        signal = [r for r in self.rows() if r['e'] == 'signal-capture']
        self.assertEqual([(r['ref'], r['k']) for r in signal], [('demo:' + made['id'], 'preference')])
        code, stats = self.cli('stats')
        self.assertEqual(code, 0, stats)
        self.assertEqual(stats['reminders'], {'nudge-preference': 1})
        self.assertEqual(sum(stats['signal_captures'].values()), 1)
        self.assertEqual(runtime.process('claude', 'stop', self.payload(), self.root), {})

    def test_a_turn_without_a_signal_records_no_signal_capture(self):
        runtime.process('claude', 'user-prompt', self.payload(prompt='summarize the file'), self.root)
        self.cli('capture', 'Plain fact', 'A fact.', '--session', 's1', '--tag', 'subject/project', '--tag', 'concern/decision')
        self.assertFalse([r for r in self.rows() if r['e'] == 'signal-capture'])

    def test_stats_noisy_and_used(self):
        noisy = capture('Webcam conclusion', 'Hardware.', TAGS, 'demo', self.root)
        used = capture('Deploy steps', 'Run install.py.', TAGS, 'demo', self.root)
        for i in range(3):
            usage.record(self.root, 'inject', ['demo:' + noisy['id'], 'demo:' + used['id']], f's{i}', 'prompt')
        usage.record(self.root, 'read', ['demo:' + used['id']])
        usage.record(self.root, 'inject', ['other:abc'], 's9', 'prompt')  # outside the domain scope
        code, stats = self.cli('stats')
        self.assertEqual(code, 0, stats)
        self.assertEqual([n['ref'] for n in stats['noisy']], ['demo:' + noisy['id']])
        self.assertEqual(stats['noisy'][0]['title'], 'Webcam conclusion')
        self.assertEqual(stats['noisy'][0]['counts'], {'inject': 3})
        self.assertEqual([n['ref'] for n in stats['used']], ['demo:' + used['id']])
        self.assertEqual(stats['notes'], 2)
        self.assertEqual(stats['sessions_with_injections'], 3)
        self.assertEqual(self.cli('--actor', 'subagent', 'stats')[0], 0)  # read-only

    def test_missing_notes_are_marked(self):
        usage.record(self.root, 'read', ['demo:deadbeef-0000'])
        stats = usage.stats(self.root, ['demo'])
        self.assertEqual(stats['used'][0]['status'], 'missing')

    def test_old_days_are_pruned_and_ignored(self):
        usage.record(self.root, 'read', ['demo:x'])
        old = self.root / '.turns' / 'usage-2020-01-01.jsonl'
        old.write_text(json.dumps({'t': '2020-01-01T00:00:00+00:00', 'e': 'read', 'ref': 'demo:y', 's': None, 'k': None}) + '\n', encoding='utf-8')
        self.assertEqual([r['ref'] for r in self.rows()], ['demo:x'])
        stamp = time.time() - 40 * 86400
        os.utime(old, (stamp, stamp))
        prune(self.root)
        self.assertFalse(old.exists())
        self.assertTrue(list((self.root / '.turns').glob('usage-*.jsonl')))

    def test_bad_lines_are_skipped(self):
        usage.record(self.root, 'read', ['demo:x'])
        path = next((self.root / '.turns').glob('usage-*.jsonl'))
        with path.open('a', encoding='utf-8') as out:
            out.write('{not json\n["list"]\n{"e":"unknown"}\n')
        self.assertEqual(len(self.rows()), 1)

    def test_a_failing_diary_never_breaks_a_hook(self):
        capture('Cache eviction policy', 'LRU.', TAGS, 'demo', self.root)
        with patch('core.common.file_lock', side_effect=OSError('locked')):
            result = runtime.process('claude', 'user-prompt', self.payload(prompt='cache eviction policy'), self.root)
        self.assertIn('Cache eviction policy', result['context'])
        self.assertEqual(self.rows(), [])

    def test_stats_is_auto_allowed(self):
        self.assertIn('stats', runtime.ALLOWED_SUBCOMMANDS)
        self.assertNotIn('stats', runtime.MUTATING_SUBCOMMANDS)


if __name__ == '__main__':
    unittest.main()
