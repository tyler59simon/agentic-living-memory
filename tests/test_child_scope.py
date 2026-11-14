"""Child domains (folders bound inside the active domain's folder) are in scope beside parents, at weight 1."""
import json
import unittest

from test_core import StoreFixture
from core import runtime, session_start
from core.init_domain import init_domain
from core.notes import capture

TAGS = ['subject/project', 'concern/decision']


class ChildScope(StoreFixture):
    def setUp(self):
        super().setUp()
        self.child = self.base / 'project' / 'pkg'
        (self.child / 'src').mkdir(parents=True)
        init_domain('pkg', [str(self.child)], self.root)
        self.bindings = json.loads((self.root / 'bindings.json').read_text())
        self.cwd = str(self.base / 'project')

    def prompt(self, text, session='s'):
        return runtime.process('claude', 'user-prompt', {'cwd': self.cwd, 'session_id': session, 'prompt': text}, self.root)['context']

    def test_child_domains_are_nearest_first_and_exclude_parents(self):
        deep = self.child / 'deep'
        deep.mkdir()
        init_domain('deep', [str(deep)], self.root)
        bindings = json.loads((self.root / 'bindings.json').read_text())
        self.assertEqual(runtime.child_domains('demo', bindings), ['pkg', 'deep'])
        self.assertEqual(runtime.child_domains('pkg', bindings), ['deep'])
        self.assertEqual(runtime.child_domains('deep', bindings), [])
        self.assertEqual(runtime.parent_domains('pkg', bindings), ['demo'])
        self.assertEqual(runtime.scope_domains('pkg', bindings), ['demo', 'deep'])

    def test_a_prompt_in_the_parent_injects_a_child_note(self):
        capture('Webcam conclusion', 'Likely hardware.', TAGS, 'pkg', self.root)
        self.assertIn('Webcam conclusion', self.prompt('webcam conclusion'))

    def test_a_sibling_folder_is_not_a_child(self):
        other = self.base / 'project-two'
        other.mkdir()
        init_domain('two', [str(other)], self.root)
        bindings = json.loads((self.root / 'bindings.json').read_text())
        self.assertEqual(runtime.child_domains('demo', bindings), ['pkg'])

    def test_session_start_lists_the_child_note_and_its_open_work(self):
        capture('Webcam conclusion', 'Likely hardware.', TAGS, 'pkg', self.root)
        from core.note_format import render
        capture('Pkg rollout plan', render('A plan.', actions=['Ship pkg']), TAGS, 'pkg', self.root, type='plan')
        text = session_start.build_context(self.cwd, self.root, session_start.BUDGET)[0]
        self.assertIn('Webcam conclusion', text)
        self.assertIn('Pkg rollout plan', text)

    def test_child_path_is_relative_to_the_child_folder(self):
        b = self.bindings
        self.assertEqual(runtime.child_path('pkg/src/a.py', 'demo', 'pkg', b), 'src/a.py')
        self.assertIsNone(runtime.child_path('core/a.py', 'demo', 'pkg', b))
        self.assertIsNone(runtime.child_path('pkg', 'demo', 'pkg', b))
        self.assertEqual(runtime.child_path((self.child / 'src' / 'a.py').resolve().as_posix(), 'demo', 'pkg', b), 'src/a.py')

    def test_file_hints_find_a_child_note_by_its_own_relative_path(self):
        n = capture('Parser module note', 'The parser is hand written.', TAGS, 'pkg', self.root, files=['src/parser.py'])
        state = {}
        follow = {}
        text, looked = runtime.file_hints(['pkg/src/parser.py', 'src/parser.py'], state, self.root, 'demo', self.bindings, follow=follow)
        self.assertIn('Parser module note', text)
        self.assertEqual(follow, {'pkg/src/parser.py': ['pkg:' + n['id']]})   # the unrelated project-level src/parser.py matched nothing

    def test_stop_follow_asks_about_a_child_note(self):
        n = capture('Parser module note', 'The parser is hand written.', TAGS, 'pkg', self.root, files=['src/parser.py'])
        state = {'unlooked': ['pkg/src/parser.py']}
        found = runtime.follow_pending(self.root, state, self.bindings, 'demo')
        self.assertEqual([(k, d['ref'], d['path']) for k, d in found], [('follow', 'pkg:' + n['id'], 'pkg/src/parser.py')])

    def test_signal_notes_include_children(self):
        capture('Prefer small commits', 'Keep commits small and focused.', TAGS, 'pkg', self.root, type='preference')
        found = runtime.signal_notes('Always keep commits small and focused', 'demo', self.root, self.bindings)
        self.assertEqual([h['title'] for h in found], ['Prefer small commits'])


if __name__ == '__main__':
    unittest.main()
