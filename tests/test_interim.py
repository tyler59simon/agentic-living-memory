"""Unconfirmed beliefs: notes.is_interim, its exclusion from the gotcha tier, and the UNCONFIRMED (as of date) mark on injected lines."""
import json
import re

from test_core import StoreFixture
from core import runtime, session_start
from core.maps import find_map, repo_notes
from core.common import load_bindings
from core.notes import capture, is_interim, unconfirmed
from core.search import search_domain

TAGS = ['subject/project', 'concern/decision']
MARK = re.compile(r'UNCONFIRMED \(as of \d{4}-\d{2}-\d{2}\): ')


class IsInterim(StoreFixture):
    def test_interim_wordings(self):
        said = ['Root cause unconfirmed.', 'This is unconfirmed, do not build on it.', 'Treat as true until confirmed.', 'Hold until it is confirmed by legal.',
                'Pending approval from the owner.', 'Pending confirmation.', 'Pending sign-off.', 'Not yet approved.', 'The contract is not signed.',
                'We suspect the proxy.', 'It is probably the DNS cache.', 'Not reproduced locally.']
        for kind in ('bug', 'knowledge', 'decision'):
            for body in said:
                with self.subTest(kind=kind, body=body):
                    self.assertTrue(is_interim({'type': kind, 'title': 'Slow start', 'body': body}))

    def test_a_hypothesis_label_makes_any_type_interim(self):
        for kind in ('bug', 'runbook', 'plan'):
            self.assertTrue(is_interim({'type': kind, 'title': 'x', 'body': '- **Hypothesis:** the license check.'}))

    def test_settled_or_other_notes_are_not_interim(self):
        quiet = [({'type': 'bug', 'body': 'The cause is the license check. Fixed by a timeout.'}),
                 ({'type': 'plan', 'body': 'Pending approval: next steps are A then B.'}),
                 ({'type': 'bug', 'body': 'It was unconfirmed.\n- **Root cause:** a race in the writer.'}),
                 ({'type': 'bug', 'body': 'Root cause unconfirmed at first.\n**Fix:** pin the lock.'}),
                 ({'type': 'decision', 'body': 'Not yet approved.\n**Outcome:** approved by legal on Monday.'}),
                 ({'type': 'bug', 'status': 'resolved', 'body': 'Root cause unconfirmed.'}),
                 ({'type': 'bug', 'body': 'Improbable and unsuspecting.'})]
        for note in quiet:
            with self.subTest(note=note):
                self.assertFalse(is_interim(dict(note, title='Slow start')))

    def test_a_resolution_that_is_itself_hedged_does_not_settle_the_note(self):
        self.assertTrue(is_interim({'type': 'bug', 'title': 'x', 'body': 'Slow.\n- **Root cause:** unconfirmed, likely the proxy.'}))

    def test_the_prefix_carries_the_updated_date(self):
        self.assertEqual(unconfirmed({'type': 'bug', 'title': 'x', 'body': 'Unconfirmed.', 'updated': '2026-11-02T10:00:00+00:00'}), 'UNCONFIRMED (as of 2026-11-02): ')
        self.assertEqual(unconfirmed({'type': 'bug', 'title': 'x', 'body': 'Fixed.', 'updated': '2026-11-02'}), '')

    def test_search_hits_carry_the_flag(self):
        self.note('Nightly export stalls', 'The export hangs on Fridays. Cause unconfirmed until the logs are read.', type='bug')
        self.note('Nightly export retention', 'The export keeps seven files.')
        hits = {h['title']: h for h in search_domain('nightly export', 'demo', 5, self.root)}
        self.assertTrue(hits['Nightly export stalls'].get('interim'))
        self.assertNotIn('interim', hits['Nightly export retention'])


class Marking(StoreFixture):
    def setUp(self):
        super().setUp()
        self.cwd = str(self.base / 'project')

    def test_a_prompt_hit_for_an_interim_note_is_marked_with_its_date_and_a_settled_one_is_not(self):
        self.note('Nightly export stalls on Fridays', 'The export hangs on Fridays. Cause unconfirmed until the scheduler logs are read.', type='bug')
        self.note('Nightly export keeps seven files', 'The nightly export keeps the newest seven files.', type='decision')
        text, ids, _ = runtime.context(self.cwd, 'why does the nightly export stall on fridays', self.root, 'demo')
        line = next(l for l in text.splitlines() if 'stalls on Fridays' in l)
        self.assertRegex(line, r'UNCONFIRMED \(as of \d{4}-\d{2}-\d{2}\): Nightly export stalls')
        text, ids, _ = runtime.context(self.cwd, 'how many files does the nightly export keep', self.root, 'demo')
        settled = next(l for l in text.splitlines() if 'keeps seven files' in l)
        self.assertNotIn('UNCONFIRMED', settled)

    def test_an_interim_note_is_not_a_known_gotcha_and_its_index_line_is_marked(self):
        guard = self.note('Never edit generated sources', 'Generated sources are overwritten; never edit them. Change the template instead.', type='runbook')
        maybe = self.note('Caching layer is unconfirmed, do not build on it', 'The caching layer is unconfirmed; do not build on it, it fails under load.', type='knowledge')
        trace = {}
        text, shown, _ = session_start.build_context(self.cwd, self.root, trace=trace)
        gotcha_block = text.split('Known gotchas', 1)[1].split('Other notes', 1)[0]
        self.assertIn('Never edit generated sources', gotcha_block)
        self.assertNotIn('Caching layer', gotcha_block)
        self.assertEqual(trace['degrade']['gotcha_of'], 1)
        self.assertIn('demo:' + guard['id'], shown)
        self.assertNotIn('demo:' + maybe['id'], shown)
        self.assertRegex(text, r'(?s)Other notes.*UNCONFIRMED \(as of \d{4}-\d{2}-\d{2}\): Caching layer')

    def test_repo_notes_lines_are_marked(self):
        project = self.base / 'project'
        (project / 'suite' / '.git').mkdir(parents=True)
        suite = (project / 'suite').resolve()
        (self.root / 'tag_taxonomy.json').write_text(json.dumps({'tags': TAGS + ['concern/knowledge']}))
        capture('Parser may drop a newline', 'Unconfirmed: the parser drops a trailing newline.', TAGS, 'demo', self.root, type='bug', files=['suite'])
        capture('Money is BigDecimal', 'Amounts use BigDecimal.', TAGS, 'demo', self.root, type='decision', files=['suite'])
        capture('suite map', 'A suite.', TAGS, 'demo', self.root, type='map', files=[str(suite)])
        text, refs = repo_notes(find_map(suite, self.root, load_bindings(self.root)), suite, self.root, load_bindings(self.root), {})
        self.assertEqual(len(refs), 2)
        self.assertTrue(any(MARK.search(l) and 'Parser may drop' in l for l in text.splitlines()))
        self.assertFalse(any(MARK.search(l) for l in text.splitlines() if 'BigDecimal' in l))
