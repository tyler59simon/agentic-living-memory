"""#17 timeline: notes around an anchor note or date."""
import contextlib, io, json, unittest
from unittest.mock import patch
from test_core import StoreFixture
from core import runtime
from core.cli import main
from core.notes import capture

TAGS = ['subject/project', 'concern/decision']
TOPICS = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel']


class Timeline(StoreFixture):
    def setUp(self):
        super().setUp()
        self.ids = []
        for day, topic in enumerate(TOPICS, start=1):
            stamp = f'2026-10-{day:02d}T12:00:00.000000+00:00'
            with patch('core.notes.now', return_value=stamp):
                kind = 'decision' if day % 2 else 'knowledge'
                self.ids.append(capture(f'{topic} subject note', f'About {topic}.', TAGS, 'demo', self.root, type=kind)['id'])

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--domain', 'demo', *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def titles(self, result):
        return [n['title'].split()[0] for n in result['notes']]

    def test_default_anchor_is_the_newest(self):
        code, result = self.cli('timeline', '--before', '2')
        self.assertEqual(code, 0, result)
        self.assertEqual(self.titles(result), ['foxtrot', 'golf', 'hotel'])
        self.assertTrue(result['notes'][-1]['anchor'])
        self.assertEqual(result['anchor'], 'demo:' + self.ids[-1])

    def test_anchor_on_a_ref_with_a_window(self):
        code, result = self.cli('timeline', 'demo:' + self.ids[3][:8], '--before', '1', '--after', '2')
        self.assertEqual(code, 0, result)
        self.assertEqual(self.titles(result), ['charlie', 'delta', 'echo', 'foxtrot'])
        self.assertEqual([n.get('anchor', False) for n in result['notes']], [False, True, False, False])
        self.assertEqual(result['notes'][1]['date'], '2026-10-04')

    def test_anchor_on_a_date_and_window_bounds(self):
        code, result = self.cli('timeline', '--date', '2026-10-03', '--before', '5', '--after', '0')
        self.assertEqual(self.titles(result), ['alpha', 'bravo', 'charlie'])
        code, result = self.cli('timeline', '--date', '2030-01-01', '--before', '0')
        self.assertEqual(self.titles(result), ['hotel'])  # past the end: the newest
        self.assertEqual(self.cli('timeline', '--date', 'soon')[0], 2)
        self.assertEqual(self.cli('timeline', 'demo:' + self.ids[0], '--date', '2026-10-01')[0], 2)

    def test_type_filter_keeps_the_anchor(self):
        code, result = self.cli('timeline', 'demo:' + self.ids[1], '--type', 'decision', '--before', '1', '--after', '1')
        self.assertEqual(self.titles(result), ['alpha', 'bravo', 'charlie'])  # bravo is knowledge but is the anchor
        code, result = self.cli('timeline', '--type', 'knowledge')
        self.assertEqual(set(n['type'] for n in result['notes']), {'knowledge'})

    def test_retired_notes_only_with_all(self):
        capture('India replaces alpha', 'Newer.', TAGS, 'demo', self.root, supersedes=['demo:' + self.ids[0]])
        code, result = self.cli('timeline', '--date', '2026-10-01', '--after', '1')
        self.assertNotIn('alpha', self.titles(result))
        # Retiring a note sets its updated time, so place it by creation.
        code, result = self.cli('timeline', '--all', '--by', 'created', '--date', '2026-10-01', '--after', '1')
        self.assertEqual(self.titles(result)[0], 'alpha')
        self.assertEqual(result['notes'][0]['status'], 'superseded')
        code, result = self.cli('timeline', 'demo:' + self.ids[0], '--before', '0', '--after', '0')  # a retired anchor is shown
        self.assertEqual((self.titles(result), result['notes'][0]['status']), (['alpha'], 'superseded'))

    def test_by_created(self):
        with patch('core.notes.now', return_value='2026-10-20T00:00:00.000000+00:00'):
            self.cli('update', 'demo:' + self.ids[0], '--summary', 'Edited later.')
        self.assertEqual(self.titles(self.cli('timeline', '--before', '0')[1]), ['alpha'])
        self.assertEqual(self.titles(self.cli('timeline', '--by', 'created', '--before', '0')[1]), ['hotel'])

    def test_read_only_and_auto_allowed(self):
        self.assertIn('timeline', runtime.ALLOWED_SUBCOMMANDS)
        self.assertNotIn('timeline', runtime.MUTATING_SUBCOMMANDS)
        self.assertEqual(self.cli('--actor', 'subagent', 'timeline')[0], 0)


if __name__ == '__main__':
    unittest.main()
