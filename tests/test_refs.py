"""Short references and title lookup: humans and injected lines use short forms, storage keeps full IDs."""
import io
import json
import uuid
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from test_core import StoreFixture
from core import runtime
from core.cli import main as cli
from core.capture import status
from core.init_domain import init_domain
from core.notes import capture, find_note, resolve_ref, short_ref

TAGS = ['subject/project', 'concern/decision']


def fixed_ids(*values):
    """Note IDs from the given values in order; transactions keep their own uuid module."""
    from unittest.mock import Mock
    return patch('core.notes.uuid', Mock(uuid4=Mock(side_effect=[uuid.UUID(v) for v in values])))


class ShortRefs(StoreFixture):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli(['--root', str(self.root), '--domain', 'demo', *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_prefix_resolves_to_the_full_id(self):
        n = self.note()
        self.assertEqual(short_ref('demo', n['id']), 'demo:' + n['id'][:8])
        self.assertEqual(find_note(n['id'][:8], 'demo', self.root)[1]['id'], n['id'])
        self.assertEqual(resolve_ref('demo:' + n['id'][:8], None, self.root), ('demo', n['id']))
        self.assertEqual(resolve_ref(n['id'][:10], 'demo', self.root), ('demo', n['id']))

    def test_prefixes_shorter_than_eight_characters_are_rejected(self):
        n = self.note()
        with self.assertRaises(ValueError):
            find_note(n['id'][:7], 'demo', self.root)

    def test_ambiguous_prefix_lists_the_candidates(self):
        with fixed_ids('abcdef12-0000-4000-8000-000000000001', 'abcdef12-0000-4000-8000-000000000002'):
            a = capture('First topic', 'One.', TAGS, 'demo', self.root)
            b = capture('Second subject', 'Two.', TAGS, 'demo', self.root)
        with self.assertRaisesRegex(ValueError, 'ambiguous_ref') as caught:
            find_note('abcdef12', 'demo', self.root)
        self.assertIn(a['id'], str(caught.exception))
        self.assertIn(b['id'], str(caught.exception))
        self.assertEqual(find_note('abcdef12-0000-4000-8000-000000000002', 'demo', self.root)[1]['id'], b['id'])

    def test_cli_commands_accept_short_refs_and_store_full_refs(self):
        runtime.process('claude', 'user-prompt', {'cwd': str(self.base / 'project'), 'session_id': 'sx', 'turn_id': 't1', 'prompt': 'hi'}, self.root)
        old = self.note('Old cache rule', 'Rebuild nightly.')
        code, read = self.run_cli('read', 'demo:' + old['id'][:8])
        self.assertEqual((code, read['id']), (0, old['id']))
        code, _ = self.run_cli('update', old['id'][:8], '--body', 'Rebuild hourly.', '--session', 'sx', '--turn', 't1')
        self.assertEqual(code, 0)
        self.assertEqual(status('sx', 't1', self.root)['notes'], ['demo:' + old['id']])
        new = capture('New cache policy', 'Rebuild on change.', TAGS, 'demo', self.root)
        code, _ = self.run_cli('supersede', old['id'][:8], '--by', 'demo:' + new['id'][:8], '--session', 'sx', '--turn', 't2')
        self.assertEqual(code, 0)
        self.assertEqual(find_note(old['id'], 'demo', self.root)[1]['superseded_by'], new['id'])
        code, _ = self.run_cli('capture-finalize', '--session', 'sx', '--turn', 't3', '--note', 'demo:' + new['id'][:8])
        self.assertEqual(status('sx', 't3', self.root)['notes'], ['demo:' + new['id']])

    def test_capture_supersedes_accepts_a_short_ref(self):
        old = self.note('Deploy target', 'Staging box.')
        new = capture('Production deploy host', 'Prod box.', TAGS, 'demo', self.root, supersedes=['demo:' + old['id'][:8]])
        self.assertEqual(new['superseded'], ['demo:' + old['id']])
        self.assertEqual(find_note(old['id'], 'demo', self.root)[1]['status'], 'superseded')

    def test_injected_lines_use_short_refs(self):
        n = capture('Build pipeline status', 'Green on main.', TAGS, 'demo', self.root, type='status')
        payload = {'cwd': str(self.base / 'project'), 'session_id': 's', 'prompt': 'build pipeline'}
        context = runtime.process('claude', 'user-prompt', payload, self.root)['context']
        self.assertIn('demo:' + n['id'][:8] + ' ', context)
        self.assertNotIn(n['id'], context)


class TitleLookup(StoreFixture):
    def test_exact_title_resolves_case_insensitively(self):
        n = self.note('Living Memory: edit the source repo', 'Deploy with install.py.')
        self.assertEqual(resolve_ref('living memory: EDIT the source repo', 'demo', self.root), ('demo', n['id']))

    def test_partial_titles_do_not_resolve(self):
        self.note('Living Memory: edit the source repo', 'Deploy with install.py.')
        with self.assertRaisesRegex(ValueError, 'No note with ID'):
            resolve_ref('Living Memory: edit', 'demo', self.root)

    def test_duplicate_titles_are_ambiguous(self):
        init_domain('child', [str(self.base / 'project' / 'child')], self.root)
        self.note('Shared title', 'In the parent.')
        # The child already sees the parent's note, so a second one needs allow_similar.
        capture('Shared title', 'In the child.', TAGS, 'child', self.root, allow_similar=True)
        with self.assertRaisesRegex(ValueError, 'ambiguous_ref'):
            resolve_ref('Shared title', 'child', self.root, ['demo'])

    def test_parent_domain_titles_resolve(self):
        init_domain('child', [str(self.base / 'project' / 'child')], self.root)
        n = self.note('Parent rule', 'Applies below.')
        self.assertEqual(resolve_ref('Parent rule', 'child', self.root, ['demo']), ('demo', n['id']))

    def test_retired_notes_are_not_found_by_title(self):
        old = self.note('Retired rule', 'Gone.')
        capture('Replacement rule', 'Current.', TAGS, 'demo', self.root, supersedes=[old['id']])
        with self.assertRaises(ValueError):
            resolve_ref('Retired rule', 'demo', self.root)
        self.assertEqual(resolve_ref(old['id'][:8], 'demo', self.root), ('demo', old['id']))  # IDs still reach retired notes

    def test_cli_read_by_title(self):
        n = self.note('Webcam conclusion', 'Likely hardware.')
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli(['--root', str(self.root), '--cwd', str(self.base / 'project'), 'read', 'Webcam conclusion']), 0)
        self.assertEqual(json.loads(out.getvalue())['id'], n['id'])

    def test_protocol_asks_for_titles_with_the_user(self):
        self.assertIn('Name notes to the user by title', runtime.PROTOCOL)


if __name__ == '__main__':
    import unittest
    unittest.main()
