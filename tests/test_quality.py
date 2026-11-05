"""Advisory note-quality warnings and the capture guidance that prevents low-value notes."""
from test_core import StoreFixture
from core import runtime
from core.notes import capture, change, quality_warnings

TAGS = ['subject/project', 'concern/decision']


class Quality(StoreFixture):
    def test_moment_notes_are_saved_with_warnings(self):
        result = capture('Webcam attempt 2', 'Awaiting user retest. Scripts live in the session scratchpad.', TAGS, 'demo', self.root, type='status')
        self.assertEqual(result['status'], 'created')
        warnings = result['quality_warnings']
        self.assertTrue(any('scratchpad' in w for w in warnings))
        self.assertTrue(any('unfinished' in w for w in warnings))

    def test_test_counts_and_commit_hashes_are_flagged(self):
        self.assertTrue(any('test counts' in w for w in quality_warnings({'body': 'Full suite: 144 tests passed.'})))
        self.assertTrue(any('git' in w for w in quality_warnings({'body': 'Shipped in commit 0a56b9f.'})))

    def test_lasting_knowledge_has_no_warnings(self):
        result = capture('Use the venv interpreter', 'System Python lacks pytest; run tests with .venv/Scripts/python.exe.', TAGS, 'demo', self.root)
        self.assertNotIn('quality_warnings', result)

    def test_plans_may_describe_unfinished_work(self):
        self.assertEqual(quality_warnings({'type': 'plan', 'body': 'Pending approval: next steps are A then B.'}), [])

    def test_update_reports_warnings_too(self):
        n = self.note()
        self.assertIn('quality_warnings', change(n['id'], 'demo', 'update', self.root, body='Awaiting the rebuild.'))

    def test_protocol_and_reminder_steer_away_from_moment_notes(self):
        self.assertIn('most turns do not', runtime.PROTOCOL)
        self.assertIn('Skip progress snapshots, test counts', runtime.PROTOCOL)
        payload = {'cwd': str(self.base / 'project'), 'session_id': 'q', 'prompt': 'edit'}
        runtime.process('claude', 'user-prompt', payload, self.root)
        runtime.process('claude', 'pre-tool-use', {**payload, 'tool_name': 'Edit', 'tool_input': {'file_path': str(self.base / 'project/x.py')}}, self.root)
        reminder = runtime.process('claude', 'stop', payload, self.root)['continue_reason']
        self.assertTrue(reminder.index('capture-finalize') < reminder.index(' capture --session'))
        self.assertIn('Usually nothing here is durable', reminder)


if __name__ == '__main__':
    import unittest
    unittest.main()
