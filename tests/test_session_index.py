"""SessionStart: standing guidance in full, a budgeted title index, and how it interacts with prompt retrieval."""
from unittest.mock import patch

from test_core import StoreFixture
from core import runtime, session_start
from core.init_domain import init_domain
from core.notes import capture

TAGS = ['subject/project', 'concern/decision']
TOPICS = ['webcam', 'deploy', 'cache', 'hooks', 'obsidian', 'taxonomy', 'backup', 'install', 'sync', 'search',
          'budget', 'secrets', 'refs', 'review', 'journal', 'vault', 'codex', 'claude', 'plugin', 'diagram']


class SessionIndex(StoreFixture):
    def build(self, budget=session_start.BUDGET, cwd=None):
        return session_start.build_context(cwd or str(self.base / 'project'), self.root, budget)

    def start(self, session='sess'):
        payload = {'cwd': str(self.base / 'project'), 'session_id': session}
        return runtime.process('claude', 'session-start', payload, self.root)['context']

    def prompt(self, text, session='sess'):
        payload = {'cwd': str(self.base / 'project'), 'session_id': session, 'prompt': text}
        return runtime.process('claude', 'user-prompt', payload, self.root)['context']

    def large_store(self):
        """Index lines carry summaries only in a small store; these tests are about the titles-only layout."""
        patcher = patch.object(session_start, 'SMALL_STORE', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def many(self, count, body='x' * 300, kind='knowledge'):
        return [capture(f'{TOPICS[i % len(TOPICS)]} note {i}', body + f' item{i}', TAGS, 'demo', self.root, type=kind, allow_similar=True)
                for i in range(count)]

    def test_guidance_in_full_then_titles_with_costs(self):
        self.large_store()
        pref = capture('Prefer small commits', 'Keep commits small and focused.', TAGS, 'demo', self.root, type='preference')
        other = capture('Webcam conclusion', 'Likely hardware. ' * 40, TAGS, 'demo', self.root)
        text, shown, indexed = self.build()
        lines = text.splitlines()
        self.assertTrue(lines[1].startswith('- [PREFERENCE'), lines)
        self.assertIn('— Keep commits small', lines[1])
        index_line = next(l for l in lines if 'Webcam conclusion' in l)
        self.assertTrue(index_line.startswith('- KNOWLEDGE '), index_line)
        self.assertRegex(index_line, r'\(~\d+t\)$')
        self.assertNotIn('Likely hardware', text)  # index lines carry no snippet
        self.assertIn('Read: CMD read REF', text)
        self.assertEqual(list(shown), ['demo:' + pref['id']])
        self.assertTrue(indexed)
        self.assertIn('demo:' + other['id'][:8], index_line)

    def test_at_most_three_guidance_notes_and_twenty_index_lines(self):
        self.many(30, body='short')
        for i in range(5):
            capture(f'{TOPICS[i]} decision', 'Decided.', TAGS, 'demo', self.root, type='decision', allow_similar=True)
        text, shown, _ = self.build(budget=100000)
        self.assertEqual(len(shown), 3)
        self.assertEqual(sum(1 for l in text.splitlines() if l.startswith('- [')), 3)
        index = [l for l in text.splitlines() if l.startswith('- ') and not l.startswith('- [')]
        self.assertEqual(len(index), 20)
        self.assertEqual(sum(1 for l in index if l.startswith('- DECISION')), 2)  # extra guidance falls into the index

    def test_newest_first_and_similar_titles_stay_listed(self):
        # Shared title words are not proof that one note replaced another; supersede does that explicitly.
        capture('Plugin tests: 144 pass', 'Older.', TAGS, 'demo', self.root, type='status')
        capture('Plugin tests: 155 pass', 'Newer.', TAGS, 'demo', self.root, type='status', allow_similar=True)
        capture('Webcam conclusion', 'Hardware.', TAGS, 'demo', self.root)
        text = self.build()[0]
        self.assertIn('Plugin tests: 144 pass', text)
        self.assertLess(text.index('Webcam conclusion'), text.index('Plugin tests: 155'))
        self.assertLess(text.index('Plugin tests: 155'), text.index('Plugin tests: 144'))

    def test_budget_degrades_cheapest_first(self):
        capture('Prefer small commits', 'Keep commits small. ' * 20, TAGS, 'demo', self.root, type='preference')
        self.many(60)
        full = self.build(budget=100000)[0]
        self.assertGreater(len(full), 1500)
        trimmed, _, indexed = self.build(budget=1000)
        self.assertLessEqual(len(trimmed), 1000)
        self.assertTrue(indexed)
        self.assertIn('— Keep commits small', trimmed)  # index lines go before guidance snippets
        tiny, _, indexed = self.build(budget=260)
        self.assertFalse(indexed)
        self.assertNotIn('Other notes', tiny)
        self.assertNotIn('Search: CMD', tiny)
        self.assertNotIn('— Keep', tiny)  # last resort: guidance titles only
        self.assertIn('Prefer small commits', tiny)
        self.assertEqual(self.build(budget=260)[1], {})  # title-only guidance stays eligible for prompts

    def test_session_start_stays_within_the_whole_budget(self):
        capture('Prefer small commits', 'Keep commits small. ' * 20, TAGS, 'demo', self.root, type='preference')
        self.many(60)
        # With the static protocol installed in the instruction file, SessionStart carries only per-session lines.
        with patch('core.runtime.installed', return_value=True):
            self.assertLessEqual(len(self.start()), session_start.BUDGET)
        # Without it the full protocol is injected, and the notes still get a minimum share.
        self.assertIn('Other notes', self.start('other'))

    def test_long_titles_are_cut_at_a_word(self):
        self.large_store()
        capture('Very long title ' + ' '.join(f'word{i}' for i in range(40)), 'Body.', TAGS, 'demo', self.root)
        line = next(l for l in self.build()[0].splitlines() if 'Very long title' in l)
        title = line.split(' ', 4)[4].rsplit(' (~', 1)[0]
        self.assertTrue(title.endswith(' …'), title)
        self.assertLessEqual(len(title), session_start.TITLE_MAX + 2)
        self.assertTrue(all(w.startswith(('word', 'Very', 'long', 'title', '…')) for w in title.split()))

    def test_parent_domain_notes_are_listed(self):
        init_domain('child', [str(self.base / 'project' / 'child')], self.root)
        capture('Parent workspace rule', 'Applies below.', TAGS, 'demo', self.root)
        text = self.build(cwd=str(self.base / 'project' / 'child'))[0]
        self.assertIn('Parent workspace rule', text)
        self.assertIn("domain 'child'", text)

    def test_unbound_and_empty(self):
        self.assertEqual(self.build(cwd=str(self.base / 'elsewhere')), (None, [], False))
        text, shown, indexed = self.build()
        self.assertIn("Living Memory is active for domain 'demo'", text)
        self.assertEqual((shown, indexed), ([], False))

    def test_guidance_is_injected_in_full_once_and_index_notes_are_too(self):
        capture('Prefer small commits', 'Keep commits small.', TAGS, 'demo', self.root, type='preference')
        capture('Webcam conclusion', 'Likely hardware.', TAGS, 'demo', self.root)
        self.start()
        self.assertIn('Prefer small commits', self.prompt('should commits be small'))   # SessionStart gave a snippet; the prompt delivers the note in full, once
        self.assertNotIn('Prefer small commits', self.prompt('should commits be small'))
        self.assertIn('Webcam conclusion', self.prompt('webcam conclusion'))

    def test_no_title_fallback_after_the_index(self):
        capture('Webcam conclusion', 'Likely hardware.', TAGS, 'demo', self.root)
        self.start()
        self.assertNotIn('No lexical match', self.prompt('weather forecast tomorrow'))
        # Without a session start (e.g. a harness that skipped it), the fallback still works.
        self.assertIn('No lexical match', self.prompt('weather forecast tomorrow', 'no-start'))


if __name__ == '__main__':
    import unittest
    unittest.main()
