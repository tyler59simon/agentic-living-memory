"""#9 reinforcement: confirm, review ordering and the strength score."""
import contextlib, io, json, unittest
from datetime import date, timedelta
from unittest.mock import patch
from test_core import StoreFixture
from core import runtime, usage, session_start
from core.cli import main
from core.notes import capture, confirm, find_note
from core.note_types import review_due, review_order, effective_date

TAGS = ['subject/project', 'concern/decision']


class Confirm(StoreFixture):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--domain', 'demo', *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_confirm_records_the_date_and_keeps_updated(self):
        n = capture('Rollout status', 'Half done.', TAGS, 'demo', self.root, type='status')
        before = find_note(n['id'], 'demo', self.root)[1]
        code, result = self.cli('confirm', 'demo:' + n['id'][:8], '--session', 's1', '--turn', 't1')
        self.assertEqual(code, 0, result)
        after = find_note(n['id'], 'demo', self.root)[1]
        today = date.today().isoformat()
        self.assertEqual(after['confirmed'], [today])
        self.assertEqual(after['updated'], before['updated'])  # content did not change
        self.assertEqual(after['body'], before['body'])
        self.assertEqual(after['review_by'], (date.today() + timedelta(days=30)).isoformat())
        self.assertEqual(result['confirmed'], [today])
        from core.capture import status
        self.assertEqual(status('s1', 't1', self.root)['notes'], ['demo:' + n['id']])
        self.assertIn(('confirm', 'demo:' + n['id']), [(r['e'], r['ref']) for r in usage.events(self.root)])

    def test_same_day_is_unchanged_and_fifo_keeps_eight(self):
        n = capture('Cache rule', 'LRU.', TAGS, 'demo', self.root)
        self.assertEqual(confirm(n['id'], 'demo', self.root)['status'], 'confirm')
        self.assertEqual(confirm(n['id'], 'demo', self.root)['status'], 'unchanged')
        older = [f'2026-01-{d:02d}' for d in range(1, 11)]
        from core.notes import change
        with patch('core.notes.date') as fake:
            fake.today.return_value = date(2026, 1, 1)
            fake.fromisoformat = date.fromisoformat
            for day in range(1, 11):
                fake.today.return_value = date(2026, 1, day)
                change(n['id'], 'demo', 'confirm', self.root)
        self.assertEqual(find_note(n['id'], 'demo', self.root)[1]['confirmed'], older[-8:])

    def test_confirm_is_journaled_and_undoable(self):
        from core.governance import undo, apply
        n = capture('Undo me', 'A.', TAGS, 'demo', self.root)
        path = find_note(n['id'], 'demo', self.root)[0]
        original = path.read_bytes()
        result = confirm(n['id'], 'demo', self.root)
        plan = undo(self.root, result['operation_id'])
        apply(self.root, plan['plan_id'], plan['digest'])
        self.assertEqual(path.read_bytes(), original)

    def test_confirmed_survives_a_round_trip_and_is_validated(self):
        from core.repository import validate
        n = capture('Round trip', 'A.', TAGS, 'demo', self.root)
        confirm(n['id'], 'demo', self.root)
        path, note = find_note(n['id'], 'demo', self.root)
        text = path.read_text(encoding='utf-8')
        self.assertLess(text.index('review_by') if 'review_by' in text else text.index('updated'), text.index('confirmed'))
        self.assertLess(text.index('confirmed'), text.index('schema_version'))
        bad = dict(note, confirmed=['yesterday'])
        with self.assertRaisesRegex(ValueError, 'confirmed'):
            validate(bad, 'demo', self.root)

    def test_retired_notes_cannot_be_confirmed(self):
        old = capture('Old fact', 'A.', TAGS, 'demo', self.root)
        capture('New fact entirely', 'B.', TAGS, 'demo', self.root, supersedes=['demo:' + old['id']])
        self.assertEqual(self.cli('confirm', 'demo:' + old['id'])[0], 2)

    def test_subagents_are_denied(self):
        self.assertIn('confirm', runtime.MUTATING_SUBCOMMANDS)
        self.assertIn('confirm', runtime.ALLOWED_SUBCOMMANDS)
        self.assertEqual(self.cli('--actor', 'subagent', 'confirm', 'x')[0], 2)


class Review(unittest.TestCase):
    def note(self, **kw):
        return {'id': kw.pop('id', 'x'), 'title': kw.pop('title', 'T'), 'status': 'active', 'type': 'knowledge',
                'updated': '2025-01-01T00:00:00+00:00', **kw}

    def test_a_recent_confirmation_resets_the_age(self):
        today = date(2026, 11, 6)
        stale = self.note()
        self.assertTrue(review_due(stale, today))
        self.assertFalse(review_due(self.note(confirmed=['2026-10-01']), today))
        self.assertEqual(effective_date(self.note(confirmed=['2026-10-01'])), date(2026, 10, 1))
        # A review date still decides for time-sensitive notes.
        self.assertTrue(review_due(self.note(type='status', review_by='2026-11-01', confirmed=['2026-10-01']), today))

    def test_neglected_notes_first_then_oldest(self):
        read = self.note(id='read', title='Read lately', updated='2024-01-01T00:00:00+00:00')
        confirmed = self.note(id='conf', title='Confirmed once', confirmed=['2025-02-01'], updated='2023-01-01T00:00:00+00:00')
        neglected_new = self.note(id='new', title='Neglected newer', updated='2025-03-01T00:00:00+00:00')
        neglected_old = self.note(id='old', title='Neglected older', updated='2024-06-01T00:00:00+00:00')
        status = self.note(id='st', title='Status', type='status', review_by='2026-10-01', updated='2026-09-01T00:00:00+00:00')
        ordered = review_order([read, confirmed, neglected_new, neglected_old, status], {'demo:read'}, 'demo')
        self.assertEqual([n['id'] for n in ordered], ['old', 'new', 'st', 'read', 'conf'])
        # ISO timestamps and review dates sort as dates (the old key compared '2026-10-01' with full timestamps).


class Strength(StoreFixture):
    def test_decay_and_repetition(self):
        today = date(2026, 11, 6)
        fresh = {'created': '2026-11-05T00:00:00+00:00', 'updated': '2026-11-05T00:00:00+00:00'}
        old = {'created': '2025-11-05T00:00:00+00:00', 'updated': '2025-11-05T00:00:00+00:00'}
        self.assertGreater(usage.strength(fresh, (), today), usage.strength(old, (), today))
        used = [('read', '2026-11-04T10:00:00+00:00'), ('confirm', '2026-11-03T10:00:00+00:00')]
        self.assertGreater(usage.strength(old, used, today), usage.strength(old, (), today))
        self.assertGreater(usage.strength(dict(old, confirmed=['2026-11-01']), (), today), usage.strength(old, (), today))
        self.assertEqual(usage.strength(old, [('inject', '2026-11-05T00:00:00+00:00')], today), usage.strength(old, (), today))

    def test_session_index_orders_by_strength_when_enabled(self):
        a = capture('Alpha topic note', 'A.', TAGS, 'demo', self.root)
        b = capture('Beta topic note', 'B.', TAGS, 'demo', self.root, allow_similar=True)
        for _ in range(5):
            usage.record(self.root, 'read', ['demo:' + a['id']])
        with patch.object(usage, 'USE_STRENGTH', False):
            text = session_start.build_context(str(self.base / 'project'), self.root)[0]
        self.assertLess(text.index('Beta topic'), text.index('Alpha topic'))  # off: newest first
        with patch.object(usage, 'USE_STRENGTH', True):
            text = session_start.build_context(str(self.base / 'project'), self.root)[0]
        self.assertLess(text.index('Alpha topic'), text.index('Beta topic'))

    def test_search_nudge_only_reorders_near_ties(self):
        from core.search import search_domains
        a = capture('Deploy checklist', 'Deploy steps for the package.', TAGS, 'demo', self.root)
        b = capture('Deploy rollback', 'Deploy rollback for the package.', TAGS, 'demo', self.root, allow_similar=True)
        far = capture('Webcam diagnosis', 'Unrelated camera issue, deploy mentioned once.', TAGS, 'demo', self.root)
        with patch.object(usage, 'USE_STRENGTH', False):
            base = [h['id'] for h in search_domains('deploy package', ['demo'], 3, self.root)]
        weaker = base[1]
        for _ in range(10):
            usage.record(self.root, 'confirm', ['demo:' + weaker])
        with patch.object(usage, 'USE_STRENGTH', True):
            nudged = [h['id'] for h in search_domains('deploy package', ['demo'], 3, self.root)]
        self.assertEqual(set(nudged), set(base))  # the same hits, so injection decisions are unchanged
        self.assertEqual(nudged[0], weaker)  # adjacent ranks differ by under 5%, so the used note moves up one


if __name__ == '__main__':
    unittest.main()
