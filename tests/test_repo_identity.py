"""Repo identity (core/common.py repo_identity): clones and worktrees of one origin share one map and one brief, and
name themselves after the remote instead of the folder they were cloned into."""
import contextlib, io, json, os, sqlite3, unittest
from pathlib import Path

from test_core import StoreFixture
from core import runtime
from core.briefs import find_brief
from core.cli import main
from core.common import git_dir, head_oid, load_bindings, normalize_remote, repo_identity, repo_label
from core.maps import find_map, repo_name
from core.notes import change, find_note
from core.repository import validate
from core.search import rebuild_index

ORIGIN = 'git@github.com:Acme/ledger.git'
IDENTITY = 'github.com/acme/ledger'   # identities casefold the path: hosts treat Owner/Repo and owner/repo alike


def make_repo(folder, origin=None):
    """A work tree with a .git folder (and an origin remote when given), without running git."""
    git = Path(folder) / '.git'
    git.mkdir(parents=True, exist_ok=True)
    config = '[core]\n\trepositoryformatversion = 0\n'
    if origin:
        config += f'[remote "origin"]\n\turl = {origin}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    (git / 'config').write_text(config, encoding='utf-8')
    return Path(folder).resolve()


class Identity(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix='living-memory-identity-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_remote_urls_normalize_to_one_identity(self):
        same = ['https://github.com/Acme/ledger.git', 'https://GitHub.com/Acme/ledger/', 'https://user:token@github.com/Acme/ledger.git',
                'https://github.com:443/Acme/ledger', 'http://github.com/Acme/ledger.git/', 'ssh://git@github.com/Acme/ledger.git',
                'ssh://git@GITHUB.com:2222/Acme/ledger.git', 'git@github.com:Acme/ledger.git', 'git@GitHub.com:Acme/ledger',
                'github.com:Acme/ledger.git', 'git://github.com/Acme/ledger.git']
        for url in same:
            with self.subTest(url=url):
                self.assertEqual(normalize_remote(url), IDENTITY)
        self.assertEqual(normalize_remote('git@host.example:/srv/git/ledger.git'), 'host.example/srv/git/ledger')
        self.assertEqual(normalize_remote('https://dev.azure.com/org/proj/_git/ledger'), 'dev.azure.com/org/proj/_git/ledger')
        for empty in (None, '', '   '):
            self.assertIsNone(normalize_remote(empty))

    def test_a_local_remote_is_the_casefolded_resolved_path(self):
        origin = (self.base / 'Origins' / 'Ledger.git')
        origin.mkdir(parents=True)
        want = 'file:' + origin.resolve().as_posix().casefold()
        for url in (str(origin), origin.as_posix(), origin.as_uri(), str(origin).replace('/', '\\')):
            with self.subTest(url=url):
                self.assertEqual(normalize_remote(url), want)
        # A relative remote is relative to the clone.
        clone = self.base / 'clones' / 'ledger'
        clone.mkdir(parents=True)
        self.assertEqual(normalize_remote('../../Origins/Ledger.git', clone), want)
        # A Windows drive letter is a path, not an scp-style host.
        self.assertTrue(normalize_remote('C:/work/Ledger').startswith('file:'))
        self.assertTrue(normalize_remote('C:\\work\\Ledger').startswith('file:'))

    def test_identity_comes_from_the_origin_remote_only(self):
        repo = self.base / 'ledger'
        (repo / '.git').mkdir(parents=True)
        (repo / '.git' / 'config').write_text(
            '[remote "upstream"]\n\turl = https://example.org/other/thing.git\n# a comment\n[remote "origin"]\n\t; another\n'
            '\turl = https://github.com/Acme/ledger.git\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n[branch "main"]\n\tremote = origin\n', encoding='utf-8')
        self.assertEqual(repo_identity(repo), IDENTITY)
        self.assertEqual(repo_label(repo), 'ledger')
        self.assertEqual(repo_name(repo), 'ledger')

    def test_no_origin_or_not_a_work_tree_has_no_identity_and_the_folder_names_it(self):
        bare = make_repo(self.base / 'scratch-clone')
        only_upstream = make_repo(self.base / 'fork')
        (only_upstream / '.git' / 'config').write_text('[remote "upstream"]\n\turl = https://example.org/a/b.git\n', encoding='utf-8')
        plain = self.base / 'plain'
        plain.mkdir()
        for folder in (bare, only_upstream, plain, self.base / 'missing'):
            with self.subTest(folder=folder.name):
                self.assertIsNone(repo_identity(folder))
                self.assertEqual(repo_label(folder), folder.name)
                self.assertEqual(repo_name(str(folder)), folder.name)

    def test_a_linked_worktree_and_a_submodule_follow_their_git_files(self):
        main_repo = make_repo(self.base / 'main', ORIGIN)
        wt_git = main_repo / '.git' / 'worktrees' / 'wt'
        wt_git.mkdir(parents=True)
        (wt_git / 'commondir').write_text('../..\n', encoding='utf-8')
        (wt_git / 'HEAD').write_text('ref: refs/heads/feature\n', encoding='utf-8')
        worktree = self.base / 'wt'
        worktree.mkdir()
        (worktree / '.git').write_text(f'gitdir: {wt_git.as_posix()}\n', encoding='utf-8')
        self.assertEqual(git_dir(worktree), wt_git.resolve())
        self.assertEqual(repo_identity(worktree), IDENTITY)
        # A relative gitdir (what git writes for submodules) resolves against the .git file.
        module = main_repo / '.git' / 'modules' / 'sub'
        module.mkdir(parents=True)
        (module / 'config').write_text('[remote "origin"]\n\turl = https://github.com/Acme/sub.git\n', encoding='utf-8')
        sub = main_repo / 'sub'
        sub.mkdir()
        (sub / '.git').write_text('gitdir: ../.git/modules/sub\n', encoding='utf-8')
        self.assertEqual(git_dir(sub), module.resolve())
        self.assertEqual(repo_identity(sub), 'github.com/acme/sub')
        # A .git file that names nothing is no repository.
        (sub / '.git').write_text('gitdir: ../nowhere\n', encoding='utf-8')
        self.assertIsNone(repo_identity(sub))

    def test_head_oid_reads_loose_packed_detached_and_worktree_refs_without_git(self):
        repo = make_repo(self.base / 'ledger', ORIGIN)
        a, b, c = 'a' * 40, 'b' * 40, 'c' * 40
        (repo / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
        self.assertIsNone(head_oid(repo))
        (repo / '.git' / 'packed-refs').write_text(f'# pack-refs\n{b} refs/heads/other\n{a} refs/heads/main\n', encoding='utf-8')
        self.assertEqual(head_oid(repo), a)
        (repo / '.git' / 'refs' / 'heads').mkdir(parents=True)
        (repo / '.git' / 'refs' / 'heads' / 'main').write_text(c + '\n', encoding='utf-8')
        self.assertEqual(head_oid(repo), c)
        (repo / '.git' / 'HEAD').write_text(b + '\n', encoding='utf-8')
        self.assertEqual(head_oid(repo), b)
        # A linked worktree keeps HEAD in its own git directory and the branch ref in the common one.
        wt_git = repo / '.git' / 'worktrees' / 'wt'
        wt_git.mkdir(parents=True)
        (wt_git / 'commondir').write_text('../..\n', encoding='utf-8')
        (wt_git / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
        worktree = self.base / 'wt'
        worktree.mkdir()
        (worktree / '.git').write_text(f'gitdir: {wt_git.as_posix()}\n', encoding='utf-8')
        self.assertEqual(head_oid(worktree), c)
        self.assertIsNone(head_oid(self.base / 'missing'))


class SharedMaps(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root / 'tag_taxonomy.json').write_text(json.dumps({'tags': ['subject/project', 'concern/architecture', 'concern/decision', 'concern/knowledge']}))
        self.project = self.base / 'project'
        self.po = make_repo(self.project / 'ws' / 'po', ORIGIN)
        self.arch = make_repo(self.project / 'ws' / 'arch', 'https://github.com/acme/ledger.git'.replace('acme', 'Acme'))
        self.other = make_repo(self.project / 'ws' / 'other', 'https://github.com/Acme/hive.git')

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--domain', 'demo', *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def capture(self, kind, folder, title, *extra):
        what = ('--detail', '**Layout:** src/.') if kind == 'map' else ('--detail', '**Goals and scope:** Ledger rebuild.')
        return self.cli('capture', '--type', kind, '--file', str(folder), '--title', title, '--summary', 'A ledger service.', *what, *extra)

    def bindings(self):
        return load_bindings(self.root)

    def test_two_clones_of_one_origin_share_one_map(self):
        code, made = self.capture('map', self.po, 'ledger codebase map')
        self.assertEqual(code, 0, made)
        note = find_note(made['id'], 'demo', self.root)[1]
        self.assertEqual(note['repo'], IDENTITY)
        self.assertEqual(note['files'], ['ws/po'])
        for clone in (self.po, self.arch):
            with self.subTest(clone=clone.name):
                self.assertEqual(find_map(clone, self.root, self.bindings())['id'], made['id'])
        self.assertIsNone(find_map(self.other, self.root, self.bindings()))
        # A second capture from the other clone names the existing map.
        code, again = self.capture('map', self.arch, 'ledger structure overview', '--allow-similar')
        self.assertNotEqual(code, 0)
        text = json.dumps(again)
        self.assertIn('map_exists conflict', text)
        self.assertIn('demo:' + made['id'][:8], text)
        self.assertIn('ledger codebase map', text)
        self.assertIn(IDENTITY, text)
        # A different repo is not in the way.
        self.assertEqual(self.capture('map', self.other, 'hive codebase map')[0], 0)

    def test_repo_is_system_managed_stored_in_the_index_and_validated(self):
        code, made = self.capture('map', self.po, 'ledger codebase map')
        text = Path(find_note(made['id'], 'demo', self.root)[0]).read_text(encoding='utf-8')
        front = text.split('---')[1]
        self.assertIn(f'repo: "{IDENTITY}"', front)
        self.assertLess(front.index('origin') if 'origin' in front else front.index('capture_key'), front.index('repo:'))
        db = sqlite3.connect(self.root / 'domains' / 'demo' / 'search.sqlite3')
        try:
            self.assertEqual(db.execute('SELECT repo FROM notes WHERE id=?', (made['id'],)).fetchone()[0], IDENTITY)
        finally:
            db.close()
        note = find_note(made['id'], 'demo', self.root)[1]
        validate(note, 'demo', self.root)
        with self.assertRaisesRegex(ValueError, 'repo must be text'):
            validate({**note, 'repo': ['github.com/x/y']}, 'demo', self.root)
        # An update refreshes it (pointing the map at another repo changes it) and never makes a note without a remote carry one.
        change(made['id'], 'demo', 'update', self.root, files=['ws/other'])
        self.assertEqual(find_note(made['id'], 'demo', self.root)[1]['repo'], 'github.com/acme/hive')
        make_repo(self.project / 'ws' / 'local')
        change(made['id'], 'demo', 'update', self.root, files=['ws/local'])
        self.assertNotIn('repo', find_note(made['id'], 'demo', self.root)[1])

    def test_updating_a_shared_note_after_its_original_clone_is_gone_keeps_its_identity(self):
        import shutil
        made = {kind: self.capture(kind, self.po, title)[1] for kind, title in (('map', 'ledger codebase map'), ('brief', 'ledger project brief'))}
        shutil.rmtree(self.po)
        for kind, note in made.items():
            with self.subTest(kind=kind):
                change(note['id'], 'demo', 'update', self.root, body=find_note(note['id'], 'demo', self.root)[1]['body'] + ' (updated after the original clone was removed)')
                self.assertEqual(find_note(note['id'], 'demo', self.root)[1]['repo'], IDENTITY)
        self.assertEqual(find_map(self.arch, self.root, self.bindings())['id'], made['map']['id'])
        self.assertEqual(find_brief(str(self.arch), self.root, self.bindings())['id'], made['brief']['id'])
        # Pointing the note at a folder without a remote on purpose still drops the identity.
        make_repo(self.project / 'ws' / 'solo')
        change(made['map']['id'], 'demo', 'update', self.root, files=['ws/solo'])
        self.assertNotIn('repo', find_note(made['map']['id'], 'demo', self.root)[1])

    def test_ordinary_notes_and_the_vault_never_carry_repo(self):
        made = self.note('Cache', 'The local cache is rebuildable.', files=['ws/po'])
        self.assertNotIn('repo', find_note(made['id'], 'demo', self.root)[1])

    def test_a_map_written_before_the_property_existed_is_still_found_from_another_clone(self):
        code, made = self.capture('map', self.po, 'ledger codebase map')
        path, note = find_note(made['id'], 'demo', self.root)
        Path(path).write_text(''.join(l for l in Path(path).read_text(encoding='utf-8').splitlines(True) if not l.startswith('repo:')), encoding='utf-8')
        rebuild_index('demo', self.root)
        self.assertNotIn('repo', find_note(made['id'], 'demo', self.root)[1])
        self.assertEqual(find_map(self.arch, self.root, self.bindings())['id'], made['id'])

    def test_without_an_origin_maps_stay_keyed_by_folder(self):
        a = make_repo(self.project / 'ws' / 'solo-a')
        b = make_repo(self.project / 'ws' / 'solo-b')
        code, made = self.capture('map', a, 'solo a codebase map')
        self.assertEqual(code, 0, made)
        self.assertNotIn('repo', find_note(made['id'], 'demo', self.root)[1])
        self.assertEqual(find_map(a, self.root, self.bindings())['id'], made['id'])
        self.assertIsNone(find_map(b, self.root, self.bindings()))
        self.assertEqual(self.capture('map', b, 'solo b codebase map')[0], 0)

    def test_suggested_titles_use_the_remote_name_in_every_clone(self):
        payload = lambda clone: {'cwd': str(clone), 'session_id': 's-' + clone.name}
        for clone in (self.po, self.arch):
            with self.subTest(clone=clone.name):
                text = runtime.process('claude', 'user-prompt', {**payload(clone), 'prompt': 'work on the ledger service'}, self.root).get('context', '')
                self.assertIn(f"--title 'ledger codebase map'", text)
                self.assertNotIn(f"--title '{clone.name} codebase map'", text)
        self.assertEqual(self.capture('map', self.po, 'ledger codebase map')[0], 0)
        text = runtime.process('claude', 'user-prompt', {**payload(self.arch), 'prompt': 'work on the ledger service'}, self.root).get('context', '')
        self.assertIn('Codebase map for ledger', text)
        self.assertNotIn('no codebase map yet', text)

    def test_the_stop_reminder_after_exploring_an_unmapped_clone_suggests_the_remote_name(self):
        payload = {'cwd': str(self.arch), 'session_id': 's1'}
        runtime.process('claude', 'user-prompt', {**payload, 'prompt': 'look around'}, self.root)
        for name in ('a.py', 'b.py', 'c.py'):
            runtime.process('claude', 'pre-tool-use', {**payload, 'tool_name': 'Read', 'tool_input': {'file_path': str(self.arch / name)}}, self.root)
        runtime.process('claude', 'pre-tool-use', {**payload, 'tool_name': 'Write', 'tool_input': {'file_path': str(self.arch / 'notes.txt')}}, self.root)   # housekeeping is for sessions that edit
        reply = runtime.process('claude', 'stop', payload, self.root)
        self.assertIn("--title 'ledger codebase map'", reply.get('continue_reason', ''))

    def test_two_clones_of_one_origin_share_one_brief(self):
        code, made = self.capture('brief', self.po, 'ledger project brief')
        self.assertEqual(code, 0, made)
        self.assertEqual(find_note(made['id'], 'demo', self.root)[1]['repo'], IDENTITY)
        for clone in (self.po, self.arch, self.arch / 'src'):
            (clone).mkdir(exist_ok=True)
            with self.subTest(clone=str(clone.relative_to(self.project))):
                self.assertEqual(find_brief(str(clone), self.root, self.bindings())['id'], made['id'])
        self.assertIsNone(find_brief(str(self.other), self.root, self.bindings()))
        code, again = self.capture('brief', self.arch, 'ledger brief again', '--allow-similar')
        self.assertNotEqual(code, 0)
        self.assertIn('brief_exists conflict', json.dumps(again))
        self.assertIn(IDENTITY, json.dumps(again))
        # The map and the brief of one repo are different types and live side by side.
        self.assertEqual(self.capture('map', self.arch, 'ledger codebase map')[0], 0)


if __name__ == '__main__':
    unittest.main()
