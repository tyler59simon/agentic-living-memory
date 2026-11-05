"""Memory stays current: direct update/supersede, review dates, and their surfacing."""
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from test_core import StoreFixture
from core import runtime
from core.cli import main as cli
from core.governance import undo, apply
from core.notes import capture, change, find_note
from core.note_types import default_review_by, review_due
from core.repository import inventory
from core.session_start import render_context

TAGS = ['subject/project', 'concern/decision']


class Currency(StoreFixture):
    def run_cli(self, *argv, stdin=None):
        out = io.StringIO()
        if stdin is not None:
            import unittest.mock
            with unittest.mock.patch('sys.stdin', io.StringIO(stdin)), redirect_stdout(out):
                code = cli(['--root', str(self.root), '--domain', 'demo', *argv])
        else:
            with redirect_stdout(out):
                code = cli(['--root', str(self.root), '--domain', 'demo', *argv])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_update_changes_title_body_and_tags_in_one_undoable_transaction(self):
        a = self.note()
        b = self.note('Pointer', f'See [cache]({quote(Path(a["path"]).name)}).')
        result = self.run_cli('update', a['id'], '--title', 'Cache layer', '--body', 'The cache is disposable.', '--concern', 'decision', '--subject', 'project')
        path, note = find_note(a['id'], 'demo', self.root)
        self.assertEqual((note['title'], note['body'], note['tags']), ('Cache layer', '## 📌 Executive Summary\nThe cache is disposable.', sorted(TAGS)))
        self.assertIn('cache-layer', path.name)
        self.assertIn(quote(path.name), find_note(b['id'], 'demo', self.root)[1]['body'])
        reverse = undo(self.root, result['operation_id'])
        apply(self.root, reverse['plan_id'], reverse['digest'])
        note = find_note(a['id'], 'demo', self.root)[1]
        self.assertEqual((note['title'], note['body']), ('Cache', '## 📌 Executive Summary\nThe local cache is rebuildable.'))

    def test_update_from_json_stdin(self):
        a = self.note()
        self.run_cli('update', 'demo:' + a['id'], stdin=json.dumps({'body': 'Line one -> two | $three'}))
        self.assertEqual(find_note(a['id'], 'demo', self.root)[1]['body'], '## 📌 Executive Summary\nLine one -> two | $three')

    def test_update_rejects_unknown_fields_and_stale_revisions(self):
        a = self.note()
        with self.assertRaisesRegex(ValueError, 'Cannot update: status'):
            change(a['id'], 'demo', 'update', self.root, status='archived')
        with self.assertRaisesRegex(ValueError, 'stale'):
            change(a['id'], 'demo', 'update', self.root, expected_revision='0' * 64, body='x')

    def test_capture_supersedes_in_one_transaction(self):
        old = capture('Plugin has 144 tests', '144 pass.', TAGS, 'demo', self.root, type='status')
        new = capture('Plugin has 155 tests', '155 pass after c4-gcp.', TAGS, 'demo', self.root, type='status', supersedes=['demo:' + old['id']])
        self.assertEqual(new['superseded'], ['demo:' + old['id']])
        retired = find_note(old['id'], 'demo', self.root)[1]
        self.assertEqual((retired['status'], retired['superseded_by']), ('superseded', new['id']))
        self.assertEqual(find_note(new['id'], 'demo', self.root)[1]['supersedes'], [old['id']])
        self.assertEqual([n['id'] for n in inventory('demo', self.root)], [new['id']])
        reverse = undo(self.root, new['operation_id'])
        apply(self.root, reverse['plan_id'], reverse['digest'])
        self.assertEqual([n['id'] for n in inventory('demo', self.root)], [old['id']])

    def test_supersede_command_and_duplicate_hint(self):
        old = self.note('Deploy steps', 'Old steps.')
        with self.assertRaisesRegex(ValueError, 'update demo:' + old['id']):
            self.note('Deploy steps!', 'New steps.')
        new = self.note('Deploy steps (2026)', 'New steps.', allow_similar=True)
        self.run_cli('supersede', old['id'], '--by', new['id'])
        self.assertEqual(find_note(old['id'], 'demo', self.root)[1]['status'], 'superseded')

    def test_review_dates_by_type(self):
        status = capture('Build status', 'Green.', TAGS, 'demo', self.root, type='status')
        decision = capture('Use SQLite', 'Chosen.', TAGS, 'demo', self.root, type='decision')
        self.assertEqual(find_note(status['id'], 'demo', self.root)[1]['review_by'], default_review_by('status'))
        self.assertNotIn('review_by', find_note(decision['id'], 'demo', self.root)[1])
        later = date.today() + timedelta(days=31)
        self.assertTrue(review_due(find_note(status['id'], 'demo', self.root)[1], later))
        self.assertFalse(review_due(find_note(decision['id'], 'demo', self.root)[1], later))
        self.assertTrue(review_due(find_note(decision['id'], 'demo', self.root)[1], date.today() + timedelta(days=181)))
        with self.assertRaisesRegex(ValueError, 'YYYY-MM-DD'):
            capture('Bad date', 'x', TAGS, 'demo', self.root, review_by='next week')

    def test_overdue_notes_surface_in_review_and_session_start(self):
        old = capture('Build status', 'Green.', TAGS, 'demo', self.root, type='status', review_by='2020-01-01')
        self.note('Unrelated', 'Not due.')
        listed = self.run_cli('review')
        self.assertEqual((listed['due'], listed['notes'][0]['ref']), (1, 'demo:' + old['id']))
        context = render_context(str(self.base / 'project'), self.root)
        self.assertIn('1 note(s) due for review', context)
        self.assertIn('demo:' + old['id'], context)
        # Updating the body makes it current again.
        change(old['id'], 'demo', 'update', self.root, body='Still green.')
        self.assertEqual(self.run_cli('review')['due'], 0)

    def test_update_and_supersede_are_auto_allowed_and_count_as_decisions(self):
        a = self.note()
        cmd = f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo update {a["id"]} --body "Fresh"'
        result = runtime.process('claude', 'pre-tool-use', {'cwd': str(self.base / 'project'), 'session_id': 's', 'tool_name': 'Bash', 'tool_input': {'command': cmd}}, self.root)
        self.assertIn('allow', result)
        self.run_cli('update', a['id'], '--body', 'Fresh', '--session', 's', '--turn', 't')
        from core.capture import status
        self.assertTrue(status('s', 't', self.root))

    def test_subagents_cannot_update(self):
        a = self.note()
        script = Path(__file__).resolve().parents[1] / 'living_memory.py'
        denied = subprocess.run([sys.executable, str(script), '--root', str(self.root), '--domain', 'demo', '--actor', 'subagent', 'update', a['id'], '--body', 'x'], text=True, capture_output=True)
        self.assertNotEqual(denied.returncode, 0)


if __name__ == '__main__':
    import unittest
    unittest.main()
