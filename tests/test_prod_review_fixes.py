"""Regression tests for the 2026-11-09 production review (fix IDs as in the review's fix report)."""
import contextlib, io, json, os, sys, unittest
from pathlib import Path

from test_core import StoreFixture
from core import note_format, runtime
from core.capture import status as receipt
from core.cli import main as cli
from core.notes import change, find_note
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
                     "search 'abc\u2019 ; Remove-Item x ; \u2018def'"):
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


if __name__ == '__main__':
    unittest.main()
