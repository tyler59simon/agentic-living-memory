"""Long prompts: IDF term selection, segmenting, and a sim-style task brief whose assignment names a stored gotcha."""
import math
import sqlite3
from unittest.mock import patch

from test_core import StoreFixture
from core import runtime, search
from core.notes import capture

TAGS = ['subject/project', 'concern/decision']
HEADER = ('You are the backend engineer on the harbor team. Team rules for this sprint: never push, work in your own worktree, '
          'the reviewer reads every diff. Report format: three bullets, what changed, what you verified, what is left. ')
CHECKLIST = ('Closing checklist: update the ticket board, list blockers, attach screenshots, confirm formatting, summarize risks, note follow-ups, '
             'mention dependencies, tag reviewers, include timestamps, record versions, archive scratch files, then say so in one line when done. ')


def brief(assignment, sections=True):
    filler = ' '.join(['Background: the team keeps a shared runbook and a ticket board; each item names an owner and a reviewer.'] * 12)
    if not sections:
        return HEADER + filler + ' Your assignment: ' + assignment + ' ' + CHECKLIST
    return '\n\n'.join(['# Team brief', HEADER + filler, '## Your assignment', assignment, '## Checklist', CHECKLIST + filler])


class QueryTerms(StoreFixture):
    WORDS = [f'word{i}' for i in range(40)]

    def test_without_a_vocabulary_a_long_query_keeps_its_start_and_end(self):
        text = ' '.join(self.WORDS)
        self.assertEqual(search.query_terms(text), self.WORDS[:16] + self.WORDS[-16:])
        self.assertEqual(search.query_terms(text, (0, {})), self.WORDS[:16] + self.WORDS[-16:])   # no notes: nothing to select by

    def test_a_query_within_the_term_limit_is_never_reduced(self):
        text = ' '.join(self.WORDS[:30])
        self.assertEqual(search.query_terms(text, (20, {'word3': 1})), self.WORDS[:30])

    def test_df_zero_terms_are_dropped_and_rare_terms_rank_first(self):
        df = {f'word{i}': 1 for i in range(40)}
        df.update({f'word{i}': 0 for i in range(0, 40, 2)})   # even words match no note
        df.update({'word1': 5, 'word3': 5})                    # two odd words are more common than the rest
        picked = search.query_terms(' '.join(self.WORDS), (30, {k: v for k, v in df.items() if v}))
        self.assertEqual(len(picked), 16)
        self.assertTrue(all(int(w[4:]) % 2 for w in picked), picked)
        self.assertNotIn('word1', picked)
        self.assertNotIn('word3', picked)
        self.assertEqual(picked, sorted(picked, key=lambda w: int(w[4:])))   # prompt order is kept

    def test_ties_go_to_later_terms(self):
        picked = search.query_terms(' '.join(self.WORDS), (30, {w: 2 for w in self.WORDS}))
        self.assertEqual(picked, self.WORDS[-16:])

    def test_common_terms_are_dropped_once_there_are_eight_notes(self):
        df = {w: 1 for w in self.WORDS}
        df.update({w: 10 for w in self.WORDS[:30]})   # in 10 of 20 notes: more than COMMON_SHARE
        picked = search.query_terms(' '.join(self.WORDS), (20, df))
        self.assertEqual(picked, self.WORDS[30:])
        small = search.query_terms(' '.join(self.WORDS), (6, {w: 3 for w in self.WORDS}))   # N < 8: nothing counts as common
        self.assertEqual(len(small), 16)

    def test_selection_prefers_the_rarer_term(self):
        df = {w: 4 for w in self.WORDS}
        df['word7'] = 1
        picked = search.query_terms(' '.join(self.WORDS), (20, df))
        self.assertIn('word7', picked)
        self.assertEqual(search.idf_sum('word7 word8', (20, df)), math.log(20) + math.log(5))

    def test_a_final_e_does_not_hide_a_word_the_notes_carry(self):
        self.assertEqual(search.df_of({'rotate': 3}, 'rotates'), 3)
        self.assertEqual(search.df_of({'rotat': 2}, 'rotate'), 2)


class Segments(StoreFixture):
    def test_headings_and_blank_lines_split_a_prompt(self):
        text = '# Team\nLine one\nline two\n\n## Task\nDo it.\n### Sub\n\n\nLast words'
        self.assertEqual(runtime.prompt_segments(text), ['# Team\nLine one\nline two', '## Task\nDo it.', '### Sub', 'Last words'])

    def test_one_paragraph_is_one_segment(self):
        self.assertEqual(runtime.prompt_segments('one line only, with # inside'), ['one line only, with # inside'])
        self.assertEqual(runtime.prompt_segments('  \n\n'), [])

    def test_at_most_four_segments_are_queried_and_the_last_always_is(self):
        vocab = (20, {f'rare{i}': 1 for i in range(8)})
        text = '\n\n'.join(f'rare{i} filler' for i in range(8))
        queries = [q for q, _ in runtime.long_queries(text, vocab)]
        self.assertEqual(len(queries), 5)   # the last segment, the three richest others, the whole prompt
        self.assertEqual(queries[0], 'rare7')
        self.assertIn('rare0 rare1 rare2 rare3 rare4 rare5 rare6 rare7', queries)

    def test_the_whole_prompt_query_is_judged_by_titles_only(self):
        vocab = (20, {'alpha': 1, 'beta': 1, 'gamma': 1})
        self.assertEqual(runtime.long_queries('alpha beta\n\ngamma', vocab), [('gamma', True), ('alpha beta', True), ('alpha beta gamma', False)])
        self.assertEqual(runtime.long_queries('alpha beta gamma', vocab), [('alpha beta gamma', False)])   # a single paragraph


class LongBriefs(StoreFixture):
    def setUp(self):
        super().setUp()
        self.cwd = str(self.base / 'project')
        capture('cmd /c mvnw.cmd from Git Bash builds nothing', 'Run ./mvnw directly: cmd /c exits 0 and the build never starts.', TAGS, 'demo', self.root, type='bug')
        capture('Webcam is not detected after a BIOS update', 'Reinstall the driver from the vendor site.', TAGS, 'demo', self.root, type='bug')
        capture('Prefer small commits', 'Keep each commit small and focused.', TAGS, 'demo', self.root, type='preference')
        for i, topic in enumerate(['cache', 'queue', 'ledger', 'invoice', 'export', 'billing', 'audit', 'report']):
            capture(f'{topic.capitalize()} module notes', f'The {topic} module lives in src/{topic}. Detail {i}.', TAGS, 'demo', self.root, allow_similar=True)

    def ask(self, prompt, **kwargs):
        trace = {}
        text, ids, _ = runtime.context(self.cwd, prompt, self.root, 'demo', trace=trace, **kwargs)
        return text, ids, trace

    def titles(self, ids):
        return {t for t, _ in search.active_notes(ids, self.root).values()}

    def test_a_gotcha_in_the_middle_of_a_3kb_brief_is_injected(self):
        prompt = brief('Build the service with mvnw.cmd from Git Bash and confirm the build runs.')
        self.assertGreater(len(prompt), 2500)
        text, ids, trace = self.ask(prompt)
        self.assertEqual(self.titles(ids), {'cmd /c mvnw.cmd from Git Bash builds nothing'})
        self.assertEqual(trace['path'], 'injected')
        self.assertGreater(trace['queries'], 1)
        # The old head-and-tail terms were all team boilerplate, so the assignment never reached the search.
        self.assertNotIn('mvnw', ' '.join(search.query_terms(prompt)))

    def test_the_same_gotcha_in_a_single_paragraph_brief_is_injected_by_its_title(self):
        prompt = brief('Build the service with mvnw.cmd from Git Bash and confirm the build runs.', sections=False)
        text, ids, trace = self.ask(prompt)
        self.assertEqual(self.titles(ids), {'cmd /c mvnw.cmd from Git Bash builds nothing'})
        self.assertEqual(trace['queries'], 1)

    def test_a_brief_with_no_stored_subject_injects_nothing(self):
        for sections in (True, False):
            with self.subTest(sections=sections):
                text, ids, trace = self.ask(brief('Add a dark mode toggle to the settings page and persist the choice.', sections))
                self.assertEqual(ids, [])
                self.assertNotEqual(trace['path'], 'injected')

    def test_generic_words_spread_over_a_brief_do_not_match_a_note_by_chance(self):
        # Words like "each", "keep", "small" and "focused" occur in the preference note; a brief that merely uses them is not about it.
        text, ids, _ = self.ask(brief('Keep each change small and focused, then check that every page still renders.', sections=False))
        self.assertEqual(ids, [])

    def test_short_prompts_take_the_unchanged_path(self):
        text, ids, trace = self.ask('why does mvnw.cmd from git bash build nothing')
        self.assertEqual(self.titles(ids), {'cmd /c mvnw.cmd from Git Bash builds nothing'})
        self.assertNotIn('queries', trace)
        with patch.object(search, 'vocabulary', side_effect=AssertionError('short prompts never read the vocabulary')):
            self.ask('webcam not detected after bios update')

    def test_a_long_prompt_falls_back_when_the_index_has_no_vocabulary(self):
        db = sqlite3.connect(self.root / 'domains' / 'demo' / 'search.sqlite3')
        db.execute('DROP TABLE vocab');db.commit();db.close()
        text, ids, trace = self.ask(brief('Build the service with mvnw.cmd from Git Bash and confirm the build runs.'))
        self.assertNotIn('queries', trace)   # the old head-and-tail path, no error
        self.assertEqual(ids, [])

    def test_notes_shown_before_are_not_injected_again(self):
        prompt = brief('Build the service with mvnw.cmd from Git Bash and confirm the build runs.')
        _, ids, _ = self.ask(prompt)
        shown = {ids[0]: runtime.revision_key(search.search_domain('mvnw', 'demo', 1, self.root)[0])}
        text, again, trace = self.ask(prompt, shown=shown)
        self.assertEqual(again, [])
        self.assertEqual(trace['suppressed_shown'], ids)

    def test_the_cap_of_three_holds_across_queries(self):
        for i in range(4):
            capture(f'Mvnw gotcha {i} on Git Bash', f'Build failure {i} with mvnw.cmd under Git Bash.', TAGS, 'demo', self.root, type='bug', allow_similar=True)
        _, ids, trace = self.ask(brief('Build the service with mvnw.cmd from Git Bash and confirm the build runs.'))
        self.assertEqual(len(ids), 3)
        self.assertTrue(trace['cut_by_cap'])


if __name__ == '__main__':
    import unittest
    unittest.main()
