"""SessionStart gotcha tier, small-store index summaries and the budget's degrade order."""
import json
from unittest.mock import patch

from test_core import StoreFixture
from core import runtime, session_start
from core.notes import capture

TAGS = ['subject/project', 'concern/decision']


class SessionGotchas(StoreFixture):
    def build(self, budget=session_start.BUDGET):
        trace = {}
        text, shown, indexed = session_start.build_context(str(self.base / 'project'), self.root, budget, trace=trace)
        return text, shown, trace['degrade']

    def prompt(self, text, session='sess'):
        payload = {'cwd': str(self.base / 'project'), 'session_id': session, 'prompt': text}
        return runtime.process('claude', 'user-prompt', payload, self.root)['context']

    def start(self, session='sess'):
        return runtime.process('claude', 'session-start', {'cwd': str(self.base / 'project'), 'session_id': session}, self.root)['context']

    def gotchas(self):
        capture('cmd /c mvnw.cmd from Git Bash builds nothing', 'Run ./mvnw instead; cmd /c exits 0 without building.', TAGS, 'demo', self.root, type='bug')
        capture('Never edit generated sources', 'Generated sources are overwritten by the build. Change the template instead.', TAGS, 'demo', self.root, type='runbook')
        capture('Release runbook', 'Tag, build, publish. Then announce it.', TAGS, 'demo', self.root, type='runbook')
        capture('Webcam conclusion', 'Likely hardware.', TAGS, 'demo', self.root)

    def test_gotchas_follow_guidance_with_snippets_and_leave_the_index(self):
        capture('Prefer small commits', 'Keep commits small and focused.', TAGS, 'demo', self.root, type='preference')
        self.gotchas()
        text, shown, _ = self.build()
        lines = text.splitlines()
        heading = next(i for i, l in enumerate(lines) if l.startswith('Known gotchas'))
        self.assertTrue(lines[heading - 1].startswith('- [PREFERENCE'))
        gotcha_lines = lines[heading + 1:heading + 3]
        self.assertTrue(any(l.startswith('- [BUG') and 'Run ./mvnw instead' in l for l in gotcha_lines), gotcha_lines)
        self.assertTrue(any(l.startswith('- [RUNBOOK') and 'overwritten by the build' in l for l in gotcha_lines), gotcha_lines)
        self.assertTrue(lines[heading + 3].startswith('Other notes'))
        self.assertEqual(sum('mvnw' in l for l in lines), 1)  # not listed again in the index
        self.assertEqual(sum('Release runbook' in l for l in lines), 1)  # a plain runbook stays an index line
        self.assertEqual(len(shown), 3)  # the guidance note and both gotchas were shown with a snippet

    def test_gotcha_selection(self):
        for note, expected in (({'type': 'bug', 'title': 'Webcam', 'tags': []}, False),
                               ({'type': 'bug', 'title': 'Webcam', 'tags': ['concern/bug']}, True),
                               ({'type': 'knowledge', 'title': 'Notes', 'tags': ['concern/correction']}, True),
                               ({'type': 'knowledge', 'title': 'Notes', 'tags': ['concern/constraint']}, True),
                               ({'type': 'runbook', 'title': 'Deploy', 'tags': [], 'commands': ['mvnw verify']}, True),
                               ({'type': 'knowledge', 'title': 'Builds break on case', 'tags': []}, True),
                               ({'type': 'knowledge', 'title': 'Layout', 'body': 'Do not rely on the order. It differs.', 'tags': []}, True),
                               ({'type': 'knowledge', 'title': 'Layout', 'body': 'Sections follow a fixed order.', 'tags': []}, False)):
            self.assertEqual(session_start.is_gotcha(note), expected, note)
        # Only bug, runbook and knowledge notes are eligible.
        self.assertFalse(session_start.is_gotcha({'type': 'decision', 'title': 'Never push', 'tags': ['concern/constraint']}))
        self.assertFalse(session_start.is_gotcha({'type': 'status', 'title': 'Sync fails often', 'tags': []}))

    def test_at_most_three_gotchas(self):
        for i in range(5):
            capture(f'Never do thing {i}', f'Do not do thing {i}.', TAGS, 'demo', self.root, type='bug', allow_similar=True)
        text, shown, _ = self.build()
        self.assertEqual(sum(l.startswith('- [BUG') for l in text.splitlines()), session_start.GOTCHAS)
        self.assertEqual(len(shown), session_start.GOTCHAS)
        self.assertEqual(sum(l.startswith('- BUG') for l in text.splitlines()), 2)  # the rest stay in the index

    def test_small_store_index_lines_carry_a_summary_sentence(self):
        capture('Webcam conclusion', 'Likely hardware. A BIOS reset did not bring it back.', TAGS, 'demo', self.root)
        capture('Long summary', 'x' * 300, TAGS, 'demo', self.root)
        text, _, degrade = self.build()
        line = next(l for l in text.splitlines() if 'Webcam conclusion' in l)
        self.assertRegex(line, r'Webcam conclusion — Likely hardware\. \(~\d+t\)$')
        cut = next(l for l in text.splitlines() if 'Long summary' in l).split(' — ', 1)[1].rsplit(' (~', 1)[0]
        self.assertLessEqual(len(cut), session_start.SUMMARY_MAX)
        self.assertTrue(cut.endswith(' …') or len(cut) < 100, cut)
        self.assertTrue(degrade['summaries'])

    def test_a_store_over_the_small_limit_lists_titles_only(self):
        for i in range(session_start.SMALL_STORE + 1):
            capture(f'Topic {i} note', f'Plain fact {i}. More.', TAGS, 'demo', self.root, allow_similar=True)
        text, _, degrade = self.build()
        self.assertFalse(degrade['summaries'])
        self.assertFalse(any(' — ' in l for l in text.splitlines() if l.startswith('- KNOWLEDGE')))

    def test_budget_drops_summaries_then_gotcha_snippets_then_index_then_gotchas(self):
        capture('Prefer small commits', 'Keep commits small. ' * 20, TAGS, 'demo', self.root, type='preference')
        self.gotchas()
        capture('Plugin tests', 'A different status entirely: forty-two pass. ' * 5, TAGS, 'demo', self.root, type='status')
        full, _, top = self.build(100000)
        self.assertEqual((top['summaries'], top['gotcha_snippets'], top['gotchas']), (True, True, 2))
        steps = []
        for budget in range(len(full), 150, -1):
            _, shown, d = self.build(budget)
            state = (d['summaries'], d['gotcha_snippets'], d['listed'], d['gotchas'], d['titles_only'])
            if not steps or steps[-1][0] != state:
                steps.append((state, len(shown)))
        states = [s for s, _ in steps]
        first = lambda test: next(i for i, s in enumerate(states) if test(s))
        summaries_gone = first(lambda s: not s[0])
        snippets_gone = first(lambda s: not s[1])
        index_cut = first(lambda s: s[2] < states[0][2])
        gotcha_cut = first(lambda s: s[3] < states[0][3])
        self.assertLess(summaries_gone, snippets_gone)
        self.assertLessEqual(snippets_gone, index_cut)
        self.assertLess(index_cut, gotcha_cut)
        self.assertEqual(states[gotcha_cut][2], 0)  # gotcha lines go only once the index is empty
        # Snippet-shown gotchas count as shown; with the snippets dropped only the guidance note (if it still has its snippet) does.
        self.assertEqual(steps[0][1], 3)
        self.assertEqual(steps[snippets_gone][1], 1)

    def test_a_gotcha_shown_with_a_snippet_is_injected_in_full_once_a_prompt_finds_it_relevant(self):
        self.gotchas()
        self.start()
        state = json.loads(runtime._pointer(self.root, 'sess').read_text(encoding='utf-8'))
        self.assertEqual(len(state['shown_snippet']), 2)   # the two gotchas that fit with a snippet
        self.assertNotIn('shown', state)
        first = self.prompt('cmd mvnw builds nothing from git bash')
        self.assertIn('mvnw', first)        # the session-start snippet was not the note's delivery
        state = json.loads(runtime._pointer(self.root, 'sess').read_text(encoding='utf-8'))
        self.assertEqual(len(state['shown']), 1)             # the full injection is what counts as shown
        self.assertNotIn('mvnw', self.prompt('cmd mvnw builds nothing from git bash'))   # only the full injection is deduplicated
        self.assertIn('Release runbook', self.prompt('release runbook tag build publish'))


if __name__ == '__main__':
    import unittest
    unittest.main()
