"""Regression tests for the 2026-11-09 production review (fix IDs as in the review's fix report)."""
import contextlib, io, json, os, sys, time, unittest, zipfile
from pathlib import Path

from test_core import StoreFixture
from core import backup as snap
from core import note_format, runtime, usage
from core.capture import status as receipt
from core.cli import main as cli
from core.governance import apply, undo
from core.notes import capture, change, find_note
from core.repository import inventory

TAGS = ['subject/project', 'concern/architecture']


class Cli(StoreFixture):
    def run_cli(self, *argv, stdin=None):
        """(exit code, stdout JSON or None, stderr JSON or None) for one CLI call against the test store."""
        out, err = io.StringIO(), io.StringIO()
        real = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli(['--root', str(self.root), '--domain', 'demo', *argv])
        finally:
            sys.stdin = real
        load = lambda text: json.loads(text) if text.strip() else None
        return code, load(out.getvalue()), load(err.getvalue())

    def ok(self, *argv, stdin=None):
        code, out, err = self.run_cli(*argv, stdin=stdin)
        self.assertEqual(code, 0, err)
        return out

    def body(self, note_id):
        return find_note(note_id, 'demo', self.root)[1]['body']

    def prompt(self, session, text='hello', turn='t-1'):
        project = self.base / 'project'
        project.mkdir(exist_ok=True)
        runtime.process('claude', 'session-start', {'cwd': str(project), 'session_id': session}, self.root)
        runtime.process('claude', 'user-prompt', {'cwd': str(project), 'session_id': session, 'turn_id': turn, 'prompt': text}, self.root)


class AutoAllow(Cli):
    """SEC-1: the PreToolUse auto-approval must not cover a command that can run anything else."""

    def cmd(self, rest):
        return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'

    def decision(self, tool, command, **extra):
        project = self.base / 'project'
        project.mkdir(exist_ok=True)
        payload = {'cwd': str(project), 'session_id': 'allow', 'tool_name': tool, 'tool_input': {'command': command}, **extra}
        return runtime.process('claude', 'pre-tool-use', payload, self.root)

    def test_bash_backslash_quote_cannot_hide_a_second_command(self):
        hidden = self.cmd("search x\\' ; rm -rf ~ ; echo \\'")
        self.assertIsNone(runtime.memory_subcommand(hidden, 'posix'))
        self.assertIsNone(runtime.memory_subcommand(hidden))
        self.assertNotIn('allow', self.decision('Bash', hidden))

    def test_powershell_expressions_and_curly_quotes_are_not_inert(self):
        for rest in ('search (Remove-Item -Recurse C:\\important)', 'search @(Remove-Item C:\\important)', 'search {Remove-Item x}',
                     "search 'abc\u2019 ; Remove-Item x ; \u2018def'", "search --% 'a | calc.exe'"):
            with self.subTest(rest=rest):
                command = '& ' + self.cmd(rest)
                self.assertIsNone(runtime.memory_subcommand(command, 'powershell'))
                self.assertIsNone(runtime.memory_subcommand(command))
                self.assertNotIn('allow', self.decision('PowerShell', command))

    def test_each_shells_own_quote_escape_is_recognised(self):
        bash = self.cmd("capture --title 'it'\\''s fine' --summary 'x'")
        self.assertEqual(runtime.memory_subcommand(bash, 'posix'), 'capture')
        self.assertIsNone(runtime.memory_subcommand(bash, 'powershell'))
        self.assertIn('allow', self.decision('Bash', bash))
        doubled = '& ' + self.cmd("capture --title 'it''s fine' --summary 'x'")
        self.assertEqual(runtime.memory_subcommand(doubled, 'powershell'), 'capture')
        self.assertIn('allow', self.decision('PowerShell', doubled))

    def test_a_curly_apostrophe_is_ordinary_text_in_bash(self):
        self.assertEqual(runtime.memory_subcommand(self.cmd("search 'don\u2019t repeat'"), 'posix'), 'search')

    def test_another_store_is_not_auto_approved(self):
        elsewhere = f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.base / "other-store"}" capture t b'
        self.assertEqual(runtime.memory_subcommand(elsewhere), 'capture')
        self.assertNotIn('allow', self.decision('Bash', elsewhere))
        self.assertNotIn('deny', self.decision('Bash', elsewhere))

    def test_input_files_are_auto_approved_only_inside_the_project_or_scratchpad(self):
        project = self.base / 'project'
        scratch = self.base / 'scratch'
        outside = self.base / 'outside'
        for folder in (project, scratch, outside):
            folder.mkdir(exist_ok=True)
        capture = lambda path, flag='--content-file': self.cmd(f"capture --title t {flag} '{Path(path).as_posix()}'")
        self.assertIn('allow', self.decision('Bash', capture(project / 'body.md')))
        self.assertIn('allow', self.decision('Bash', self.cmd("capture --title t --content-file body.md")))
        self.assertIn('allow', self.decision('Bash', capture(scratch / 'body.md'), scratchpad_dir=str(scratch)))
        self.assertNotIn('allow', self.decision('Bash', capture(scratch / 'body.md')))
        for flag in ('--content-file', '--content', '--payload-file'):
            with self.subTest(flag=flag):
                self.assertNotIn('allow', self.decision('Bash', capture(outside / 'secret.txt', flag)))
        self.assertNotIn('allow', self.decision('Bash', self.cmd(f"update demo:x --body-file='{(outside / 'secret.txt').as_posix()}'")))
        # Bash would expand the braces into two arguments, the last one outside the project.
        self.assertNotIn('allow', self.decision('Bash', self.cmd('capture --title t --content-file={body.md,' + (outside / 'secret.txt').as_posix() + '}')))
        self.assertNotIn('allow', self.decision('Bash', self.cmd('capture --title t --content-file ~nosuchuser/secret.txt')))


class BoldContent(unittest.TestCase):
    """DATA-1: text written entirely in bold is content, not empty markup."""

    def test_bold_only_bullets_and_callouts_survive(self):
        body = note_format.render(summary='Plan summary.', details=['**Postgres 16 required**', '**No ORM**'],
                                  important='**Never deploy on Fridays**')
        found = note_format.sections(body)
        self.assertIn('**Postgres 16 required**', found['details'])
        self.assertIn('**No ORM**', found['details'])
        self.assertIn('**Never deploy on Fridays**', found['important'])
        self.assertEqual(note_format.format_body(body), body)

    def test_a_bold_summary_is_a_summary(self):
        note_format.check(note_format.render(summary='**Use uv, not pip.**'), 'knowledge')

    def test_a_bare_label_is_still_empty(self):
        for empty in ('- **Cause:**', '* **Cause**:', '- ****', '- [ ]'):
            with self.subTest(empty=empty):
                self.assertNotIn('details', note_format.sections('Summary.\n\n## \U0001f4dd Key Details\n' + empty))


class SectionUpdates(Cli):
    """DATA-2: adding to a section must not replace it, and a replacement must be visible."""

    def plan(self):
        return self.ok('capture', '--type', 'plan', '--concern', 'architecture', '--title', 'Ship the importer', '--summary', 'Importer rollout.',
                       '--detail', '**Cause:** legacy rows', '--detail', '**Owner:** data team',
                       '--action', 'Write the importer', '--done', 'Agree the schema')['id']

    def test_add_flags_keep_what_the_section_already_holds(self):
        note = self.plan()
        self.ok('update', note, '--add-action', 'Run it on staging', '--add-detail', '**Risk:** long locks', '--add-link', 'https://example.com/runbook')
        items = note_format.action_items(self.body(note))
        self.assertEqual(items, [(False, 'Write the importer'), (True, 'Agree the schema'), (False, 'Run it on staging')])
        details = note_format.sections(self.body(note))['details']
        for kept in ('**Cause:** legacy rows', '**Owner:** data team', '**Risk:** long locks'):
            self.assertIn(kept, details)
        self.assertIn('https://example.com/runbook', note_format.sections(self.body(note))['links'])

    def test_set_detail_replaces_only_the_bullet_with_that_label(self):
        note = self.plan()
        self.ok('update', note, '--set-detail', '**Cause:** nulls in the legacy rows', '--set-detail', '**Deadline:** end of month')
        details = note_format.sections(self.body(note))['details'].split('\n')
        self.assertEqual(details, ['* **Cause:** nulls in the legacy rows', '* **Owner:** data team', '* **Deadline:** end of month'])

    def test_replacing_action_items_is_refused_unless_asked_for(self):
        note = self.plan()
        before = self.body(note)
        code, _, err = self.run_cli('update', note, '--action', 'A different step')
        self.assertEqual(code, 2)
        self.assertTrue(err['error']['message'].startswith('replace_actions'), err)
        self.assertIn('Write the importer', err['error']['message'])
        self.assertEqual(self.body(note), before)
        # Restating every item (here: ticking one) drops nothing, so it needs no extra flag.
        self.ok('update', note, '--done', 'Write the importer', '--done', 'Agree the schema')
        result = self.ok('update', note, '--action', 'A different step', '--replace-actions')
        self.assertEqual(result['replaced'], {'actions': {'before': 2, 'after': 1}})

    def test_a_replaced_section_is_reported(self):
        note = self.plan()
        result = self.ok('update', note, '--detail', '**Cause:** rewritten')
        self.assertEqual(result['replaced'], {'details': {'before': 2, 'after': 1}})
        result = self.ok('update', note, '--body', 'Only a summary now.')
        self.assertEqual(result['replaced'], {'details': {'before': 1, 'after': 0}, 'actions': {'before': 2, 'after': 0}})
        self.assertNotIn('replaced', self.ok('update', note, '--add-detail', '**Risk:** none'))

    def test_replace_and_add_for_one_section_are_refused_together(self):
        note = self.plan()
        code, _, err = self.run_cli('update', note, '--detail', 'x', '--add-detail', 'y')
        self.assertEqual(code, 2)
        self.assertIn('--add-detail', err['error']['message'])


class PayloadCapture(Cli):
    """DATA-3: flags beside a JSON payload are honoured or refused, never dropped."""

    def payload(self, **fields):
        path = self.base / 'note.json'
        path.write_text(json.dumps({'title': 'Queue choice', 'body': 'We use the durable queue.', 'tags': TAGS, **fields}), encoding='utf-8')
        return str(path)

    def test_session_beside_a_payload_gives_the_capture_its_receipt_and_origin(self):
        self.prompt('pay')
        created = self.ok('capture', '--session', 'pay', '--payload-file', self.payload())
        self.assertEqual(receipt('pay', 't-1', self.root)['outcome'], 'saved')
        self.assertTrue(find_note(created['id'], 'demo', self.root)[1]['origin'].startswith('claude:'))

    def test_session_beside_a_payload_applies_the_private_text_check(self):
        secret = 'the launch codename is periwinkle falcon seventeen and nobody outside may hear it'
        self.prompt('priv', f'Context only: <private>{secret}</private>')
        code, _, err = self.run_cli('capture', '--session', 'priv', '--payload-file', self.payload(body=secret.capitalize() + '.'))
        self.assertNotEqual(code, 0)
        self.assertIn('private', err['error']['message'])
        self.assertEqual(inventory('demo', self.root), [])

    def test_other_flags_beside_a_payload_are_refused_by_name(self):
        code, _, err = self.run_cli('capture', '--type', 'decision', '--allow-similar', '--payload-file', self.payload())
        self.assertEqual(code, 2)
        self.assertIn('--type', err['error']['message'])
        self.assertIn('--allow-similar', err['error']['message'])
        self.assertEqual(inventory('demo', self.root), [])


class CaseOnlyRename(StoreFixture):
    """DATA-4: on a case-insensitive filesystem a rename that only changes letter case must keep the note."""

    def test_a_file_whose_name_differs_only_in_case_survives_an_update(self):
        created = self.note()
        path = Path(created['path'])
        if not Path(str(path.parent / path.name.swapcase())).exists():
            self.skipTest('case-sensitive filesystem')
        os.rename(path, path.with_name(path.name.lower()))
        change(created['id'], 'demo', 'update', self.root, body='The cache is rebuilt on start.')
        self.assertIn('rebuilt on start', find_note(created['id'], 'demo', self.root)[1]['body'])
        self.assertEqual([p.name for p in path.parent.glob('*.md')], [path.name])


class FinishedWork(Cli):
    """LIFE-1: a plan, bug or risk note whose work is done leaves the active set in one undoable step."""

    def plan(self):
        return self.ok('capture', '--type', 'plan', '--concern', 'architecture', '--title', 'Ship the importer', '--summary', 'Importer rollout.',
                       '--action', 'Write the importer', '--done', 'Agree the schema')['id']

    def start(self):
        project = self.base / 'project'
        project.mkdir(exist_ok=True)
        return runtime.process('claude', 'session-start', {'cwd': str(project), 'session_id': 'fin'}, self.root)['context']

    def test_ticking_the_last_item_points_at_resolve_and_resolve_clears_the_note_from_context(self):
        note = self.plan()
        self.assertNotIn('every Action Item done', self.start())
        ticked = self.ok('check', note, 'Write the importer')
        self.assertEqual(ticked['open'], 0)
        self.assertIn('resolve demo:' + note[:8], ticked['hint'])
        self.assertIn('1 note(s) have every Action Item done', self.start())
        resolved = self.ok('resolve', note)
        path, stored = find_note(note, 'demo', self.root)
        self.assertEqual((stored['status'], path.parent.name), ('resolved', 'archive'))
        self.assertEqual(inventory('demo', self.root), [])
        self.assertNotIn('Ship the importer', self.start())
        # Journaled like any update: undo brings it back, and so does reopen.
        proposed = undo(self.root, resolved['operation_id'])
        apply(self.root, proposed['plan_id'], proposed['digest'])
        self.assertEqual(find_note(note, 'demo', self.root)[1]['status'], 'active')
        self.ok('resolve', note)
        self.ok('reopen', note)
        self.assertEqual(find_note(note, 'demo', self.root)[0].parent.name, 'notes')

    def test_only_notes_that_track_work_can_be_resolved(self):
        for kind in ('bug', 'risk'):
            with self.subTest(kind=kind):
                created = self.ok('capture', '--type', kind, '--concern', 'architecture', '--title', f'A {kind} to close', '--summary', 'Tracked.')
                self.ok('resolve', created['id'])
        fact = self.note('A lasting fact', 'Still true.')
        code, _, err = self.run_cli('resolve', fact['id'])
        self.assertEqual(code, 2)
        self.assertIn('plan, bug and risk', err['error']['message'])

    def test_resolve_is_auto_allowed_for_the_main_agent_and_denied_to_subagents(self):
        project = self.base / 'project'
        project.mkdir(exist_ok=True)
        command = f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo resolve demo:abcdef12'
        payload = {'cwd': str(project), 'session_id': 'fin', 'tool_name': 'Bash', 'tool_input': {'command': command}}
        self.assertIn('allow', runtime.process('claude', 'pre-tool-use', payload, self.root))
        self.assertIn('deny', runtime.process('claude', 'pre-tool-use', {**payload, 'agent_id': 'sub-1'}, self.root))


class MapsPerRepo(Cli):
    """LIFE-2: maps are one per repo, whatever their titles look like."""

    def setUp(self):
        super().setUp()
        self.repos = [self.base / 'project' / name for name in ('billing-api-service', 'billing-api-gateway')]
        for repo in self.repos:
            (repo / '.git').mkdir(parents=True)

    def map(self, repo, title=None):
        return self.run_cli('capture', '--type', 'map', '--file', str(repo), '--title', title or f'{repo.name} codebase map',
                            '--summary', f'What {repo.name} is.', '--detail', '**Layout:** src/ and tests/.')

    def test_a_second_repo_gets_its_map_although_the_titles_are_alike(self):
        self.assertEqual(self.map(self.repos[0])[0], 0)
        code, _, err = self.map(self.repos[1])
        self.assertEqual(code, 0, err)
        code, _, err = self.map(self.repos[1], 'Gateway internals')
        self.assertNotEqual(code, 0)
        self.assertIn('map_exists', err['error']['message'])

    def test_an_update_cannot_make_a_second_map_for_a_repo(self):
        self.assertEqual(self.map(self.repos[0])[0], 0)
        other = self.note('Service layout notes', 'Loose notes about the service.')
        code, _, err = self.run_cli('update', other['id'], '--type', 'map', '--file', str(self.repos[0]))
        self.assertNotEqual(code, 0)
        self.assertIn('map_exists', err['error']['message'])
        self.assertEqual(find_note(other['id'], 'demo', self.root)[1]['type'], 'knowledge')


class NoisyNotes(StoreFixture):
    """LIFE-3: standing context that nobody reads is not noise."""

    def test_session_guidance_and_maps_do_not_count_as_noisy(self):
        standing = capture('Prefer small commits', 'Keep commits small.', TAGS, 'demo', self.root, type='preference')
        retrieved = capture('Webcam conclusion', 'Hardware fault.', TAGS, 'demo', self.root)
        for i in range(3):
            usage.record(self.root, 'inject', ['demo:' + standing['id']], f's{i}', 'session')
            usage.record(self.root, 'inject', ['demo:' + standing['id']], f's{i}', 'prompt-map')
            usage.record(self.root, 'inject', ['demo:' + retrieved['id']], f's{i}', 'prompt')
        stats = usage.stats(self.root, ['demo'])
        self.assertEqual([n['ref'] for n in stats['noisy']], ['demo:' + retrieved['id']])
        self.assertEqual(stats['noisy'][0]['counts'], {'inject': 3})


class Backups(StoreFixture):
    """DATA-5: one damaged file must not stop backups, and rotation must not discard the last good copy."""

    def stray(self):
        path = self.root / 'domains/demo/notes/Untitled.md'
        path.write_text('no frontmatter here', encoding='utf-8')
        return path.relative_to(self.root).as_posix()

    def test_an_unreadable_note_is_saved_as_it_is_and_reported(self):
        self.note()
        stray = self.stray()
        result = snap.backup(self.root)
        self.assertEqual([p['member'] for p in result['unreadable']], [stray])
        self.assertIn('not readable', result['warning'])
        with zipfile.ZipFile(result['backup']) as archive:
            self.assertEqual(archive.read(stray), b'no frontmatter here')

    def test_the_daily_hook_backup_survives_an_unreadable_note(self):
        self.note()
        self.stray()
        self.assertIsNotNone(snap.maybe_backup(self.root))

    def test_restore_neither_writes_nor_deletes_for_an_unreadable_member(self):
        kept = self.note()
        stray = self.stray()
        saved = snap.backup(self.root)['backup']
        (self.root / stray).write_text('edited since the snapshot', encoding='utf-8')
        change(kept['id'], 'demo', 'update', self.root, body='Changed after the snapshot.')
        proposed = snap.restore_plan(self.root, saved)
        self.assertEqual(proposed['summary']['skipped_unreadable'], [stray])
        apply(self.root, proposed['plan_id'], proposed['digest'])
        self.assertIn('rebuildable', find_note(kept['id'], 'demo', self.root)[1]['body'])
        self.assertEqual((self.root / stray).read_text(encoding='utf-8'), 'edited since the snapshot')

    def test_rotation_keeps_snapshots_from_before_the_store_shrank(self):
        notes = [self.note(f'Fact number {i} about system {i}', f'Body {i}.', allow_similar=True) for i in range(12)]
        for _ in range(2):
            snap.backup(self.root, keep=2)
        for note in notes[2:]:
            Path(note['path']).unlink()          # a sync tool removing files behind the store's back
        results = [snap.backup(self.root, keep=2) for _ in range(3)]
        self.assertTrue(results[-1]['retained'])
        self.assertIn('far fewer', results[-1]['warning'])
        counts = sorted(snap._note_count(p) for p in snap.snapshots(self.root))
        self.assertEqual(counts, [2, 2, 12, 12])

    def test_a_corrupt_marker_does_not_stop_the_daily_backup(self):
        self.note()
        marker = self.root / 'hooks/last-backup.json'
        marker.parent.mkdir(exist_ok=True)
        marker.write_text('{broken', encoding='utf-8')
        self.assertIsNotNone(snap.maybe_backup(self.root))

    def test_session_start_says_when_the_newest_backup_is_stale(self):
        self.note()
        project = self.base / 'project'
        project.mkdir(exist_ok=True)
        start = lambda: runtime.process('claude', 'session-start', {'cwd': str(project), 'session_id': 'bk'}, self.root)['context']
        newest = Path(snap.backup(self.root)['backup'])
        self.assertNotIn('Living Memory backup:', start())
        old = time.time() - 72 * 3600
        os.utime(newest, (old, old))
        self.assertIn('Living Memory backup: newest backup', start())


if __name__ == '__main__':
    unittest.main()
