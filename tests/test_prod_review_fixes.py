"""Regression tests for the 2026-11-09 production review (fix IDs as in the review's fix report)."""
import contextlib, io, json, os, sys, time, unittest, unittest.mock, zipfile
from pathlib import Path

from test_core import StoreFixture
from core import backup as snap
from core import note_format, repository, runtime, semantic, usage
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
        self.assertEqual(details, ['- **Cause:** nulls in the legacy rows', '- **Owner:** data team', '- **Deadline:** end of month'])

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


class Map1Truncation(Cli):
    """MAP-1: a map cut to the budget says so and names the note to read."""

    def setUp(self):
        super().setUp()
        self.repo = self.base / 'project' / 'billing-api-service'
        (self.repo / '.git').mkdir(parents=True)

    def inject(self, lines):
        details = [x for line in lines for x in ('--detail', line)]
        note = self.ok('capture', '--type', 'map', '--file', str(self.repo), '--title', 'billing-api-service codebase map',
                       '--summary', 'What billing is.', *details)
        runtime.process('claude', 'session-start', {'cwd': str(self.base / 'project'), 'session_id': 's1'}, self.root)
        out = runtime.process('claude', 'user-prompt', {'cwd': str(self.base / 'project'), 'session_id': 's1', 'prompt': 'fix billing-api-service'}, self.root)
        return note['id'], out['context']

    def lines(self, count):
        return [f'**Module {i:02d}:** ' + 'x' * 60 for i in range(count)]

    def test_a_long_map_gets_a_marker_with_its_short_ref_within_the_bound(self):
        from core.maps import MAP_BUDGET, map_block
        NL = chr(10)
        hit = {'domain': 'demo', 'id': 'abcdef1234567890', 'title': 'T', 'body': NL.join(f'- line {i} ' + 'y' * 60 for i in range(80))}
        block = map_block(hit, self.repo, entry={})
        head, body = block.split(NL, 1)
        self.assertTrue(body.endswith('(truncated; read the rest: read demo:abcdef12)'), body[-80:])
        self.assertIn('demo:abcdef12', head)
        self.assertLessEqual(len(body), MAP_BUDGET)

    def test_a_map_between_2500_and_3000_characters_is_injected_whole(self):
        nid, text = self.inject(self.lines(36))
        body = self.body(nid)
        self.assertTrue(2500 < len(body.strip()) < 3000, len(body))
        self.assertIn('Module 35', text)
        self.assertNotIn('truncated; read the rest', text)

    def test_a_short_map_is_unchanged(self):
        nid, text = self.inject(self.lines(2))
        self.assertIn('Module 01', text)
        self.assertNotIn('truncated; read the rest', text)

    def test_the_injected_long_map_carries_the_marker(self):
        nid, text = self.inject(self.lines(60))
        self.assertIn('(truncated; read the rest: read demo:' + nid[:8] + ')', text)


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


class DurableSignals(Cli):
    """HOOK-1: ordinary sentences are not durable statements, and a real one costs no extra turn."""

    def test_questions_descriptions_and_task_remarks_are_not_signals(self):
        for prompt in ('The API should always use HTTPS for requests', 'Actually can you check the logs?',
                       'why does the code never use the cache?', "don't forget to run the tests",
                       "please don't touch the tests for now", 'do you remember that bug we fixed?',
                       'Will we always use this cache?', 'See config.no, it is unused'):
            with self.subTest(prompt=prompt):
                self.assertIsNone(runtime.durable_signal(prompt))

    def test_statements_addressed_to_the_agent_still_are(self):
        for prompt, kind in (('Always use uv, not pip.', 'preference'), ('Sure. Never push to main.', 'preference'),
                             ('we always squash-merge', 'preference'), ('You should never edit the lockfile by hand', 'preference'),
                             ("please don't touch the generated files", 'preference'), ('Actually, the tests live in tests/', 'correction'),
                             ('Thanks. No, the other file.', 'correction'), ("don't forget that staging is shared", 'remember'),
                             ('Remember that we deploy on Tuesdays.', 'remember'), ("let's go with option B", 'decision')):
            with self.subTest(prompt=prompt):
                self.assertEqual(runtime.durable_signal(prompt), kind)

    def stop(self, session):
        return runtime.process('claude', 'stop', {'cwd': str(self.base / 'project'), 'session_id': session}, self.root)

    def context(self, session, text):
        project = self.base / 'project'
        project.mkdir(exist_ok=True)
        payload = {'cwd': str(project), 'session_id': session, 'turn_id': 't-1', 'prompt': text}
        return runtime.process('claude', 'user-prompt', payload, self.root)['context']

    def test_a_preference_is_raised_in_its_prompt_and_does_not_hold_the_turn(self):
        context = self.context('nudge', 'From now on, use tabs')
        self.assertIn('the user stated a preference in this prompt', context)
        self.assertIn('CMD capture --session nudge --type preference', context)
        self.assertEqual(self.stop('nudge'), {})
        self.assertNotIn('Living Memory: the user', self.context('nudge', 'now fix the parser'))

    def test_a_capture_made_after_the_nudge_counts_as_a_signal_capture(self):
        self.context('caught', 'From now on, use tabs')
        self.ok('capture', '--session', 'caught', '--type', 'preference', '--subject', 'project', '--concern', 'decision', '--title', 'Use tabs', '--summary', 'The user prefers tabs.')
        self.assertEqual([r['k'] for r in usage.events(self.root) if r['e'] == 'signal-capture'], ['preference'])
        self.assertEqual(self.stop('caught'), {})

    def test_a_request_to_remember_still_holds_the_turn_until_it_is_answered(self):
        self.assertIn('asked you to remember something', self.context('keep', 'Remember that staging is shared'))
        self.assertIn('capture-finalize', self.stop('keep')['continue_reason'])


class HookHints(Cli):
    """HOOK-2: hints the agent can act on, in a form that works in its shell."""

    def setUp(self):
        super().setUp()
        self.project = self.base / 'project'
        self.repo = self.project / 'billing-api-service'
        (self.repo / '.git').mkdir(parents=True)
        (self.repo / 'src').mkdir()
        self.payload = {'cwd': str(self.project), 'session_id': 'hint'}
        self.map = self.ok('capture', '--type', 'map', '--file', str(self.repo), '--title', 'billing-api-service codebase map',
                           '--summary', 'A billing API.', '--detail', '**Layout:** src/ and tests/.')['id']

    def hook(self, event, **extra):
        return runtime.process('claude', event, {**self.payload, **extra}, self.root)

    def test_a_spawn_gets_no_hint_and_the_subagent_gets_the_map(self):
        self.hook('session-start')
        self.hook('user-prompt', prompt='look around')
        spawn = self.hook('pre-tool-use', tool_name='Agent', tool_input={'description': 'Explore', 'prompt': 'Explore the billing api service repo'})
        self.assertNotIn('context', spawn)
        started = self.hook('subagent-start', agent_id='sub-1')['context']
        self.assertIn('demo:' + self.map[:8], started)
        self.assertIn(f'"{sys.executable}"', started)          # a subagent has no CMD: it needs the whole command

    def test_hints_to_the_main_agent_use_cmd_and_a_subagents_use_the_whole_command(self):
        self.hook('session-start')
        self.hook('user-prompt', prompt='look around')
        target = {'file_path': str(self.repo / 'src' / 'app.py')}
        main = self.hook('pre-tool-use', tool_name='Read', tool_input=target)['context']
        self.assertIn('Read it (CMD read demo:' + self.map[:8], main)
        self.assertNotIn('living_memory.py', main)
        sub = self.hook('pre-tool-use', tool_name='Read', tool_input=target, agent_id='sub-1')['context']
        self.assertIn(f'"{sys.executable}" "{runtime.SCRIPT}"', sub)


class Doctor(Cli):
    """ENV-1: doctor says whether each harness's hooks still reach an interpreter and this package."""

    def wire(self, interpreter, script=None):
        home = Path(os.environ['CLAUDE_HOME'])
        home.mkdir(parents=True, exist_ok=True)
        script = script or (runtime.SCRIPT.parent / 'hooks' / 'claude_hooks.py')
        handler = lambda action: {'type': 'command', 'command': str(interpreter), 'args': [str(script), action], 'timeout': 15}
        config = {'statusLine': {'type': 'command', 'command': 'echo status'},
                  'hooks': {'SessionStart': [{'hooks': [handler('session-start')]}], 'Stop': [{'hooks': [handler('stop')]}]}}
        (home / 'settings.json').write_text(json.dumps(config), encoding='utf-8')

    def test_an_unwired_harness_is_not_reported(self):
        report = self.ok('doctor')
        self.assertEqual(report['hooks']['wiring'], {})
        self.assertTrue(report['ok'])

    def test_a_missing_interpreter_is_named_with_the_command_that_fixes_it(self):
        gone = self.base / 'Python399' / 'python.exe'
        self.wire(gone)
        report = self.ok('doctor')
        row = report['hooks']['wiring']['claude']
        self.assertEqual((row['wired'], row['handlers'], row['interpreter_ok'], row['script_exists']), (True, 2, False, True))
        self.assertFalse(report['ok'])
        warning = next(w for w in report['warnings'] if w.startswith('hooks: claude:'))
        self.assertIn(str(gone), warning)
        self.assertIn('integrations/setup.py claude --hooks', warning)

    def test_a_working_interpreter_passes_and_a_stale_instruction_block_is_a_warning_only(self):
        self.wire(sys.executable)
        report = self.ok('doctor')
        row = report['hooks']['wiring']['claude']
        self.assertEqual((row['interpreter_ok'], row['this_copy'], row['instructions']), (True, True, 'stale_or_missing'))
        self.assertTrue(report['ok'])
        self.assertTrue(any('instruction block' in w for w in report['warnings']))

    def test_hooks_that_run_another_copy_are_checked_for_existence_only(self):
        other = self.base / 'elsewhere' / 'hooks' / 'claude_hooks.py'
        other.parent.mkdir(parents=True)
        other.write_text('', encoding='utf-8')
        self.wire(sys.executable, other)
        row = self.ok('doctor')['hooks']['wiring']['claude']
        self.assertEqual((row['this_copy'], row['script_exists'], row['problems']), (False, True, []))

    def test_unfinished_and_unreadable_journals_are_found_without_parsing_every_journal(self):
        from core import repository
        self.note()
        operations = self.root / '.operations'
        (operations / 'stuck').mkdir()
        (operations / 'stuck' / 'journal.json').write_text(repository.canonical_json(
            {'schema': 1, 'id': 'stuck', 'kind': 'test', 'created': repository.now(), 'state': 'prepared', 'effects': []}), encoding='utf-8')
        with unittest.mock.patch('core.repository.read_json', side_effect=AssertionError('a readable journal was parsed in full')):
            self.assertEqual(repository.unfinished_operations(self.root), (['stuck'], []))
        (operations / 'broken').mkdir()
        (operations / 'broken' / 'journal.json').write_text('{not json', encoding='utf-8')
        self.assertEqual(repository.unfinished_operations(self.root), (['stuck'], ['broken']))
        report = self.ok('doctor')
        self.assertEqual(report['pending_recovery'], ['stuck'])
        self.assertFalse(report['ok'])
        self.assertTrue(any('recover ID' in w for w in report['warnings']))
        self.assertTrue(any('cannot be read' in w for w in report['warnings']))


class ScanOnce(StoreFixture):
    """PERF-1: a process reads each unchanged note once, and never serves a stale one."""

    def setUp(self):
        super().setUp()
        repository._PARSED.clear()
        self.addCleanup(repository._PARSED.clear)

    def rewrite(self, note, old, new):
        path = Path(note['path'])
        path.write_text(path.read_text(encoding='utf-8').replace(old, new), encoding='utf-8')

    def test_unchanged_notes_are_parsed_once_and_a_changed_one_again(self):
        with unittest.mock.patch.object(repository, 'SETTLE_NS', 0):
            first = self.note('Alpha fact', 'One.')
            self.note('Bravo fact', 'Two.')
            repository._PARSED.clear()
            with unittest.mock.patch.object(repository, 'parse', wraps=repository.parse) as parse:
                inventory('demo', self.root)
                self.assertEqual(parse.call_count, 2)
                inventory('demo', self.root)
                self.assertEqual(parse.call_count, 2)
                self.rewrite(first, 'One.', 'One changed.')
                again = inventory('demo', self.root)
                self.assertEqual(parse.call_count, 3)
        self.assertIn('One changed.', next(n for n in again if n['id'] == first['id'])['body'])

    def test_a_caller_cannot_change_what_the_next_scan_returns(self):
        with unittest.mock.patch.object(repository, 'SETTLE_NS', 0):
            self.note('Alpha fact', 'One.')
            mine = inventory('demo', self.root)[0]
            mine['title'] = 'Scribbled over'
            mine['tags'].append('concern/decision')
            fresh = inventory('demo', self.root)[0]
        self.assertEqual((fresh['title'], fresh['tags']), ('Alpha fact', sorted(TAGS)))

    def test_a_file_changed_a_moment_ago_is_read_again_even_with_the_same_size_and_time(self):
        note = self.note('Alpha fact', 'One.')
        path = Path(note['path'])
        before = path.stat()
        inventory('demo', self.root)
        self.rewrite(note, 'One.', 'Two.')                       # same length
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))   # a filesystem with coarse timestamps
        self.assertIn('Two.', inventory('demo', self.root)[0]['body'])

    def test_a_symbolic_link_among_the_notes_is_still_refused(self):
        note = self.note('Alpha fact', 'One.')
        outside = self.base / 'outside.md'
        outside.write_text(Path(note['path']).read_text(encoding='utf-8'), encoding='utf-8')
        try:
            os.symlink(outside, Path(note['path']).with_name('KNOWLEDGE - linked-00000000.md'))
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links need a privilege this account lacks')
        with self.assertRaisesRegex(ValueError, 'Symbolic links'):
            inventory('demo', self.root)

    def test_a_listing_entry_that_is_a_link_is_refused(self):
        # The same rule without needing the privilege to create a link: the listing itself says what each entry is.
        self.note('Alpha fact', 'One.')

        class Link:
            name = 'KNOWLEDGE - linked-00000000.md'
            path = str(self.root / 'domains/demo/notes' / name)
            def is_symlink(self): return True

        class Listing(list):
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        with unittest.mock.patch.object(repository.os, 'scandir', return_value=Listing([Link()])):
            with self.assertRaisesRegex(ValueError, 'Symbolic links'):
                inventory('demo', self.root)


class VectorReuse(StoreFixture):
    """PERF-1: a write embeds the note it changed, not every note in the domain."""

    def setUp(self):
        super().setUp()
        from test_semantic import write_model
        source = self.base / 'tiny'
        hashes = write_model(source)
        models = unittest.mock.patch.dict(semantic.MODELS, {'tiny': {'repo': 'none/tiny', 'revision': '0', 'min_similarity': 0.45, 'files': hashes}}, clear=True)
        models.start()
        self.addCleanup(models.stop)
        self.addCleanup(semantic._unload)
        semantic.install(self.root, 'tiny', source)
        for title, body in (('Layout note', 'The layout.'), ('Cache note', 'The cache.'), ('Weather note', 'The weather.')):
            self.note(title, body, allow_similar=True)

    def embedded(self, action):
        """The texts embedded while `action` runs."""
        real, texts = semantic.Model.embed, []
        def counting(model, text):
            texts.append(text)
            return real(model, text)
        with unittest.mock.patch.object(semantic.Model, 'embed', counting):
            action()
        return texts

    def vectors(self):
        import sqlite3
        db = sqlite3.connect(self.root / 'domains/demo/search.sqlite3')
        try:
            return db.execute('SELECT count(*) FROM vectors').fetchone()[0]
        finally:
            db.close()

    def test_a_capture_embeds_only_the_new_note(self):
        texts = self.embedded(lambda: self.note('Rebuild note', 'Rebuild the cache.', allow_similar=True))
        self.assertEqual(len(texts), 1, texts)
        self.assertTrue(texts[0].startswith('Rebuild note'))
        self.assertEqual(self.vectors(), 4)

    def test_another_model_revision_embeds_everything_again(self):
        from core.search import rebuild_index
        self.assertEqual(self.embedded(lambda: rebuild_index('demo', self.root)), [])
        with unittest.mock.patch.dict(semantic.MODELS['tiny'], {'revision': '1'}):
            self.assertEqual(len(self.embedded(lambda: rebuild_index('demo', self.root))), 3)
        self.assertEqual(self.vectors(), 3)


class Cfg1BomBindings(Cli):
    """CFG-1: a UTF-8 BOM (Windows PowerShell 5.1 hand edits) must not make bindings.json invalid."""

    def test_bom_prefixed_bindings_load_and_resolve_the_domain(self):
        from core.common import load_bindings
        path = self.root / 'bindings.json'
        path.write_bytes(b'\xef\xbb\xbf' + path.read_bytes())
        self.assertIn('demo', load_bindings(self.root)['domains'])
        code, out, err = self.run_cli('search', 'anything')
        self.assertEqual(code, 0, err)


class Cli5DefaultConcern(Cli):
    """CLI-5: plan/context/status/risk/research default their concern; a missing concern names the flag and options."""

    def test_plan_without_concern_infers_workflow(self):
        (self.root / 'tag_taxonomy.json').write_text(json.dumps({'tags': TAGS + ['concern/workflow']}))
        out = self.ok('capture', '--type', 'plan', '--title', 'Ship the batch', '--summary', 'Order of the fixes.')
        self.assertIn('concern/workflow', out['inferred_tags'])

    def test_missing_concern_error_names_flag_and_registered_tags(self):
        code, out, err = self.run_cli('capture', '--type', 'plan', '--title', 'Ship the batch', '--summary', 'Order of the fixes.')
        self.assertEqual(code, 2)
        message = err['error']['message']
        self.assertIn('Capture requires a registered concern/ tag', message)
        self.assertIn('--concern', message)
        self.assertIn('concern/architecture', message)


class Ops1HistoryAndPlans(Cli):
    """OPS-1: history survives a corrupt journal, undo refuses one, and old plan files are pruned."""

    def corrupt(self):
        self.ok('capture', '--type', 'knowledge', '--concern', 'architecture', '--title', 'First fact', '--summary', 'One.')
        self.ok('capture', '--type', 'knowledge', '--concern', 'architecture', '--title', 'Second fact', '--summary', 'Two.', '--allow-similar')
        folder = self.root / '.operations' / 'badjournal1'
        folder.mkdir(parents=True)
        (folder / 'journal.json').write_text('{not json')
        return 'badjournal1'

    def test_history_skips_corrupt_journal_and_doctor_names_it(self):
        bad = self.corrupt()
        rows = self.ok('history')
        self.assertIsInstance(rows, list)  # the Hivemind app reads history as a list
        self.assertGreaterEqual(len(rows), 2)
        self.assertNotIn(bad, [r['operation_id'] for r in rows])
        code, doctor, _ = self.run_cli('doctor')
        self.assertTrue(any(bad in w and 'history skips them' in w for w in doctor['warnings']), doctor['warnings'])

    def test_undo_corrupt_journal_raises(self):
        bad = self.corrupt()
        with self.assertRaisesRegex(ValueError, f'corrupt_journal {bad}: this operation cannot be undone; see RECOVERY.md'):
            undo(self.root, bad)
        with self.assertRaisesRegex(ValueError, 'Operation unavailable'):
            undo(self.root, 'nosuchop')

    def test_prune_plans(self):
        from datetime import datetime, timedelta, timezone
        plans = self.root / '.plans'
        plans.mkdir(exist_ok=True)
        def make(name, days, state=None):
            plan_id = name * 32
            when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            (plans / (plan_id + '.json')).write_text(json.dumps({'id': plan_id, 'created': when}))
            if state:
                folder = self.root / '.operations' / plan_id
                folder.mkdir(parents=True)
                (folder / 'journal.json').write_text(json.dumps({'id': plan_id, 'state': state, 'created': when, 'kind': 'x', 'effects': []}))
            return plans / (plan_id + '.json')
        old_done, old_unapplied = make('a', 5, 'committed'), make('b', 40)
        new_done, new_unapplied, old_pending = make('c', 0, 'committed'), make('d', 5), make('e', 40, 'pending')
        self.assertEqual(repository.prune_plans(self.root), 2)
        self.assertFalse(old_done.exists() or old_unapplied.exists())
        self.assertTrue(new_done.exists() and new_unapplied.exists() and old_pending.exists())

    def test_prune_plans_keeps_a_plan_a_vault_approval_waits_on(self):
        from datetime import datetime, timedelta, timezone
        plans = self.root / '.plans'
        plans.mkdir(exist_ok=True)
        plan_id = 'f' * 32
        when = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        (plans / (plan_id + '.json')).write_text(json.dumps({'id': plan_id, 'created': when}))
        state = self.root / 'domains' / 'demo' / 'sync-state.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({'entries': {'n1': {'pending_plan': plan_id, 'pending_action': 'Delete'}}}))
        self.assertEqual(repository.prune_plans(self.root), 0)
        self.assertTrue((plans / (plan_id + '.json')).exists())


class Priv1RecallMasking(unittest.TestCase):
    SECRET = 'zebra quartz meridian lantern'
    def test_span_closing_past_the_cut_is_masked(self):
        from core import recall
        text = 'start ' + '<private>' + self.SECRET + ' ' + 'filler ' * 700 + '</private> tail'
        clean = recall._clean(text)
        self.assertIn('[private]', clean)
        for word in ('zebra', 'quartz', 'meridian', 'lantern'):
            self.assertNotIn(word, clean)
    def test_records_mask_long_span(self):
        from core import recall
        import tempfile
        text = 'plan <private>' + self.SECRET + ' ' + 'filler ' * 700 + '</private> done'
        entry = {'type': 'user', 'message': {'content': text}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 't.jsonl'
            path.write_text(json.dumps(entry) + '\n', encoding='utf-8')
            rows, _ = recall.records(path)
        self.assertNotIn('zebra', rows[0]['text'])
        self.assertIn('[private]', rows[0]['text'])
    def test_unclosed_span_is_masked(self):
        from core import recall
        clean = recall._clean('before <private>' + self.SECRET + ' never closed')
        self.assertEqual(clean, 'before [private]')
        self.assertNotIn('zebra', recall._safe('x <private>zebra'))


class Priv1UnclosedTag(unittest.TestCase):
    PLAN = 'the acquisition of northwind closes on march third pending board approval'
    def test_unclosed_tag_is_remembered_and_refused(self):
        from core import private
        state = {}
        self.assertTrue(private.remember(state, 'use <private>' + self.PLAN))
        with self.assertRaisesRegex(ValueError, 'private_content'):
            private.check(state, {'title': 'Note: The acquisition of Northwind closes on March third'})
    def test_empty_unclosed_tag_is_nothing(self):
        from core import private
        state = {}
        self.assertFalse(private.remember(state, 'hello <private>'))
        self.assertFalse(private.remember(state, 'hello <private>   '))
        self.assertEqual(state, {})
    def test_only_the_last_span_unclosed(self):
        from core import private
        found = private.spans('a <private>one two</private> b <private>three four</private> c <private>five six')
        self.assertEqual([x.strip() for x in found], ['one two', 'three four', 'five six'])


class Priv1ShingleCap(unittest.TestCase):
    def words(self, prefix, n):
        return ' '.join(f'{prefix}{i}' for i in range(n))
    def test_long_later_spans_do_not_evict_earlier_protection(self):
        from core import private
        state = {}
        first = self.words('alpha', 40)
        private.remember(state, f'<private>{first}</private>')
        for k in range(4):
            private.remember(state, f'<private>{self.words("later" + str(k) + "x", 200)}</private>')
        spans = state['private']['spans']
        total = sum(map(len, spans))
        self.assertLessEqual(total, max(private.MAX_SHINGLES, private.MIN_PER_SPAN * len(spans)))
        self.assertGreaterEqual(len(spans[0]), private.MIN_PER_SPAN)
        with self.assertRaisesRegex(ValueError, 'private_content'):
            private.check(state, {'title': first})
    def test_dedupes_within_a_span(self):
        from core import private
        state = {}
        private.remember(state, '<private>' + 'ping pong ' * 30 + '</private>')
        spans = state['private']['spans']
        self.assertEqual(len(spans[0]), len({tuple(x) for x in spans[0]}))
        self.assertLessEqual(len(spans[0]), 2)
    def test_legacy_flat_state_still_blocks_and_upgrades(self):
        from core import private
        salt = '00' * 16
        words = ['red', 'fox', 'jumps']
        state = {'private': {'salt': salt, 'shingles': [[3, private._hash(salt, words)]]}}
        with self.assertRaisesRegex(ValueError, 'private_content'):
            private.check(state, {'title': 'the red fox jumps high'})
        self.assertTrue(private.remember(state, '<private>blue harbor</private>'))
        self.assertNotIn('shingles', state['private'])
        self.assertEqual(len(state['private']['spans']), 2)
        with self.assertRaisesRegex(ValueError, 'private_content'):
            private.check(state, {'title': 'red fox jumps'})
        with self.assertRaisesRegex(ValueError, 'private_content'):
            private.check(state, {'title': 'Blue Harbor'})


class Gov4RetagSourceTypo(Cli):
    def test_unknown_source_tag_fails(self):
        self.note()
        code, out, err = self.run_cli('retag-plan', stdin=json.dumps({'concern/archtecture': 'concern/decision'}))
        self.assertNotEqual(code, 0, out)
        self.assertEqual(err['error']['message'], 'Unknown source tags: concern/archtecture; check the spelling with taxonomy')

    def test_real_source_tag_still_plans(self):
        self.note()
        out = self.ok('retag-plan', stdin=json.dumps({'concern/architecture': 'concern/decision'}))
        self.assertTrue(out['plan_id'])


class Sto1RelocationLogs(unittest.TestCase):
    def test_log_written_between_plan_and_apply(self):
        from test_storage import SharedStorage

        class Case(SharedStorage):
            def runTest(self):
                plan = self.plan()
                logs = self.root / 'logs'
                logs.mkdir()
                (logs / 'log-2026-01-01.jsonl').write_text('{"event":"x"}\n')
                result = self.apply(plan)
                self.assertEqual(result['status'], 'committed')
                self.assertTrue((Path(plan['destination']) / 'logs' / 'log-2026-01-01.jsonl').exists())
        case = Case()
        res = unittest.TestResult()
        case.run(res)
        self.assertTrue(res.wasSuccessful(), res.errors + res.failures)


class Sto3GlobalBundle(StoreFixture):
    def test_global_round_trip(self):
        note = capture('Global habit', 'Always verify.', TAGS, '_global', self.root)
        bundle = self.base / 'g.zip'
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli(['--root', str(self.root), '--domain', '_global', 'export', '--out', str(bundle)]), 0, err.getvalue())
        find_note(note['id'], '_global', self.root)[0].unlink()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli(['--root', str(self.root), 'import-plan', str(bundle)]), 0, err.getvalue())
        plan = json.loads(out.getvalue())
        apply(self.root, plan['plan_id'], plan['digest'])
        self.assertTrue(find_note(note['id'], '_global', self.root)[1])


class Cli3RefResolution(Cli):
    def cap(self, title, *extra):
        return self.ok('capture', '--title', title, '--summary', 'Body of ' + title, '--type', 'knowledge', '--tag', 'subject/project', '--tag', 'concern/architecture', *extra)

    def test_supersedes_accepts_title_and_short_ref(self):
        a = self.cap('Alpha cache rule')
        self.cap('Beta cache rule', '--supersedes', 'alpha CACHE rule')
        self.assertEqual(find_note(a['id'], 'demo', self.root)[1]['status'], 'superseded')
        c = self.cap('Gamma note')
        self.cap('Delta note', '--supersedes', 'demo:' + c['id'][:8])
        self.assertEqual(find_note(c['id'], 'demo', self.root)[1]['status'], 'superseded')

    def test_supersedes_payload_file_title(self):
        a = self.cap('Epsilon rule')
        payload = self.base / 'p.json'
        payload.write_text(json.dumps({'title': 'Zeta rule', 'body': 'Zeta body.', 'tags': TAGS, 'supersedes': ['Epsilon rule']}))
        self.ok('capture', '--payload-file', str(payload))
        self.assertEqual(find_note(a['id'], 'demo', self.root)[1]['status'], 'superseded')

    def test_supersedes_other_domain_is_rejected(self):
        g = capture('Global habit', 'Always verify.', TAGS, '_global', self.root)
        code, _, err = self.run_cli('capture', '--title', 'Local habit', '--summary', 'Local.', '--type', 'knowledge', '--tag', 'subject/project', '--tag', 'concern/architecture', '--supersedes', 'Global habit')
        self.assertNotEqual(code, 0)
        code, _, err = self.run_cli('capture', '--title', 'Local habit', '--summary', 'Local.', '--type', 'knowledge', '--tag', 'subject/project', '--tag', 'concern/architecture', '--supersedes', '_global:' + g['id'][:8])
        self.assertNotEqual(code, 0)
        self.assertIn('--supersedes must name a note in demo', json.dumps(err))
        self.assertEqual(find_note(g['id'], '_global', self.root)[1].get('status', 'active'), 'active')

    def test_capture_finalize_note_forms(self):
        a = self.note('Finalize alpha')
        b = self.note('Finalize beta')
        self.prompt('sf', turn='t1')
        self.ok('capture-finalize', '--session', 'sf', '--turn', 't1', '--note', a['id'][:8])
        self.assertEqual(receipt('sf', 't1', self.root)['notes'], ['demo:' + a['id']])
        self.ok('capture-finalize', '--session', 'sf', '--turn', 't1', '--note', 'finalize BETA')
        self.assertEqual(receipt('sf', 't1', self.root)['notes'], ['demo:' + a['id'], 'demo:' + b['id']])

    def test_history_note_forms(self):
        a = self.note('History subject')
        self.ok('update', a['id'], '--body', 'Changed body.')
        for ref in ('History subject', 'demo:' + a['id'][:8], 'demo:' + a['id']):
            rows = self.ok('history', '--note', ref)
            self.assertIn('update', [r['kind'] for r in rows], ref)
        code, out, err = self.run_cli('history', '--note', 'nosuchnote')
        self.assertNotEqual(code, 0)
        self.assertIsNone(out)
        self.assertTrue(err)

    def test_history_of_a_deleted_note_by_full_id(self):
        a = self.note('Deleted subject')
        plan = self.ok('plan', 'delete', a['id'])
        self.ok('apply', plan['plan_id'], '--expected-plan-sha256', plan['digest'])
        for ref in (a['id'], 'demo:' + a['id']):
            self.assertTrue(self.ok('history', '--note', ref), ref)


if __name__ == '__main__':
    unittest.main()
