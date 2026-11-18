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

    def test_the_auto_resolved_line_is_short_and_goes_with_the_tail_when_the_budget_is_tight(self):
        self.many(10)
        resolved = [(f'demo:{i:08x}', f'Finished plan number {i} with a rather long descriptive title ' * 3) for i in range(12)]
        text = session_start.build_context(str(self.base / 'project'), self.root, 100000, resolved=resolved)[0]
        line = next(l for l in text.splitlines() if l.startswith('Auto-resolved'))
        self.assertTrue(line.startswith('Auto-resolved 12 finished note(s): '), line)
        self.assertEqual(line.count('(reopen demo:'), 4)   # three named, then the rest by ref
        self.assertIn(' and 9 more (reopen demo:00000003, ', line)
        self.assertLess(len(line), 450)
        tight = session_start.build_context(str(self.base / 'project'), self.root, 400, resolved=resolved)[0]
        self.assertLessEqual(len(tight), 400)
        self.assertNotIn('Auto-resolved', tight)   # the tail is the first thing to go, after the index and open work
        one = session_start.build_context(str(self.base / 'project'), self.root, 100000, resolved=[('demo:0000000a', 'Finished plan')])[0]
        self.assertIn('Auto-resolved 1 finished note(s): Finished plan (reopen demo:0000000a)', one)

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

    def rule(self, title, summary, kind='policy', domain='demo'):
        return capture(title, summary, TAGS, domain, self.root, type=kind, rule=True, allow_similar=True)

    def test_rules_are_shown_in_full_and_survive_the_smallest_budget(self):
        summary = 'Pull request descriptions never carry a Claude session URL, because the link exposes the session to every reader of the pull request. ' * 2
        rule = self.rule('No session URL in pull requests', summary.strip())
        capture('Prefer small commits', 'Keep commits small. ' * 20, TAGS, 'demo', self.root, type='preference')
        self.many(60)
        for budget in (100000, session_start.BUDGET, session_start.MIN_BUDGET, 260):
            with self.subTest(budget=budget):
                text, shown, _ = self.build(budget=budget)
                self.assertIn(session_start.RULES_HEADER, text)
                self.assertIn(f"- demo:{rule['id'][:8]} No session URL in pull requests — {' '.join(summary.split())}", text)
                self.assertIn('demo:' + rule['id'], shown)   # shown in full, so a prompt does not inject it again
        tiny = self.build(budget=260)[0]
        self.assertNotIn('Other notes', tiny)   # everything else still degraded
        self.assertLess(tiny.index(session_start.RULES_HEADER), tiny.index('Prefer small commits'))

    def test_rules_do_not_take_guidance_slots(self):
        for i in range(3):
            capture(f'{TOPICS[i]} decision', 'Decided.', TAGS, 'demo', self.root, type='decision', allow_similar=True)
        rules = [self.rule(f'{TOPICS[i + 3]} rule', 'Always do it this way.') for i in range(2)]
        text, shown, _ = self.build(budget=100000)
        guidance = [l for l in text.splitlines() if l.startswith('- [')]
        self.assertEqual(len(guidance), 3)
        self.assertTrue(all('DECISION' in l for l in guidance), guidance)
        for r in rules:
            self.assertEqual(sum(1 for l in text.splitlines() if r['id'][:8] in l), 1)   # one line, in the Rules block, and none in the index
            self.assertIn('demo:' + r['id'], shown)

    def test_rules_past_the_reserve_show_as_titles_and_are_never_dropped(self):
        long = 'Every rule here has a long summary so that the reserved block fills up after a few of them. ' * 3
        made = [self.rule(f'Rule {c}', long.strip()) for c in 'ABCDEFG']
        text, shown, _ = self.build(budget=session_start.MIN_BUDGET)
        block = text.split(session_start.RULES_HEADER + '\n', 1)[1].splitlines()[:len(made)]
        self.assertEqual([l.split(' ', 2)[2].split(' — ')[0].removesuffix(session_start.RULE_TITLE_ONLY) for l in block], [f'Rule {c}' for c in 'ABCDEFG'])   # by title
        full = [l for l in block if ' — ' in l]
        self.assertTrue(full and len(full) < len(made), block)
        self.assertTrue(all(l.endswith(session_start.RULE_TITLE_ONLY) for l in block[len(full):]), block)
        self.assertLessEqual(len('\n'.join([session_start.RULES_HEADER] + full)), session_start.RULES_BUDGET)
        self.assertEqual({r for r in shown if r.startswith('demo:')}, {'demo:' + n['id'] for n in made[:len(full)]})

    def test_codex_rules_fit_the_hook_limit_with_the_fallback_protocol(self):
        import json
        long_text = ('Every word of this rule matters and the summary is long on purpose. ' * 9).strip()
        short = self.rule('A short rule', 'Keep commits small.')
        long = self.rule('B long rule', long_text)
        capture('Prefer small commits', 'Keep commits small. ' * 20, TAGS, 'demo', self.root, type='preference')
        self.many(60)
        payload = {'cwd': str(self.base / 'project'), 'session_id': 'cx'}
        kept = lambda context: context[:4000]   # Codex keeps this much of SessionStart (additionalContextLimit, integrations/setup.py)
        # The test store's CMD is a long python path; the fallback protocol then leaves room only for a count line.
        tight = runtime.process('codex', 'session-start', payload, self.root)['context']
        self.assertIn(session_start.RULES_MORE.format(2) + '\n', kept(tight))
        self.assertNotIn('demo:' + short['id'], json.loads(runtime._pointer(self.root, 'cx').read_text()).get('shown_snippet', {}))
        with patch('core.runtime.command', return_value='alm --domain demo'):   # an installed store's CMD
            context = runtime.process('codex', 'session-start', payload, self.root)['context']
        self.assertIn(session_start.RULES_HEADER, kept(context))
        shown = json.loads(runtime._pointer(self.root, 'cx').read_text()).get('shown_snippet', {})
        for note, text in ((short, 'Keep commits small.'), (long, long_text)):
            with self.subTest(title=note['id'][:8]):
                self.assertIn(note['id'][:8], kept(context))   # every rule is named inside the limit
                self.assertEqual('demo:' + note['id'] in shown, f" — {text}\n" in kept(context))   # shown only when its summary arrives whole
        self.assertIn('demo:' + short['id'], shown)

    def test_rule_summaries_keep_code_spans_and_the_header_defers_to_current_instructions(self):
        self.rule('Stage tests by glob', 'Run `git add src/*/test/*` and keep **bold** markup as written.')
        text = self.build()[0]
        self.assertIn('— Run `git add src/*/test/*` and keep **bold** markup as written.', text)
        self.assertIn("unless the user's current instructions or the source say otherwise", session_start.RULES_HEADER)

    def test_global_rules_come_first(self):
        local = self.rule('A local rule', 'Applies here.')
        g = self.rule('Z global rule', 'Applies everywhere.', kind='preference', domain='_global')
        text = self.build()[0]
        self.assertLess(text.index('Z global rule'), text.index('A local rule'))
        self.assertIn('_global:' + g['id'][:8], text)
        self.assertIn('demo:' + local['id'][:8], text)

    def test_no_title_fallback_after_the_index(self):
        capture('Webcam conclusion', 'Likely hardware.', TAGS, 'demo', self.root)
        self.start()
        self.assertNotIn('No lexical match', self.prompt('weather forecast tomorrow'))
        # Without a session start (e.g. a harness that skipped it), the fallback still works.
        self.assertIn('No lexical match', self.prompt('weather forecast tomorrow', 'no-start'))


if __name__ == '__main__':
    import unittest
    unittest.main()
