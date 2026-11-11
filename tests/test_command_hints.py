"""Command hints: a note that warns about a shell command is shown when an agent is about to run it."""
import contextlib
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from test_core import StoreFixture
from core import commands, runtime, search
from core.commands import Sig, extract, match, signature
from core.repository import inventory, validate


def one(text):
    found = signature(text)
    assert len(found) == 1, found
    return found[0]


TAGS = ('--tag', 'subject/project', '--tag', 'concern/architecture')


class Normalization(unittest.TestCase):
    def test_shell_command_forms_name_one_program(self):
        table = [
            (r'cmd /c mvnw.cmd -q -B verify', 'cmd', 'mvnw', ('verify',)),
            (r'cmd //c mvnw.cmd -q -B verify', 'cmd', 'mvnw', ('verify',)),   # Git Bash doubles the slash
            (r'cmd.exe /d /s /c "mvnw.cmd verify"', 'cmd', 'mvnw', ('verify',)),
            (r'./mvnw verify', None, 'mvnw', ('verify',)),
            (r'.\mvnw.cmd verify', None, 'mvnw', ('verify',)),
            (r'& .\mvnw.cmd verify', None, 'mvnw', ('verify',)),
            (r'& ".\mvnw.cmd" verify', None, 'mvnw', ('verify',)),
            (r'C:\x\mvnw.cmd verify', None, 'mvnw', ('verify',)),
            (r'bash -c "./mvnw test"', 'bash', 'mvnw', ('test',)),
            (r'bash -lc "./mvnw test"', 'bash', 'mvnw', ('test',)),
            (r'powershell -NoProfile -Command "& .\mvnw.cmd verify"', 'powershell', 'mvnw', ('verify',)),
            (r'wsl -d Ubuntu ./mvnw test', 'wsl', 'mvnw', ('test',)),
            (r'FOO=1 mvn -q verify', None, 'mvn', ('verify',)),
            (r'sudo nohup mvn verify', None, 'mvn', ('verify',)),
            (r'time mvn verify', None, 'mvn', ('verify',)),
            (r'python -m pytest -k x', None, 'pytest', ('x',)),
            (r'py -3 -m pytest', None, 'pytest', ()),
            (r'python3 build.py', None, 'python', ('build.py',)),
            (r'npx prettier --write .', None, 'prettier', ()),
            (r'git push --force', None, 'git', ('push',)),
            (r'git status', None, 'git', ('status',)),
        ]
        for text, wrapper, program, subs in table:
            with self.subTest(text):
                sig = one(text)
                self.assertEqual((sig.wrapper, sig.program, sig.subs), (wrapper, program, subs))

    def test_flags_are_kept_and_chains_split(self):
        self.assertEqual(one('git push --force origin main').flags, frozenset({'--force'}))
        self.assertEqual(one('cmd /c mvnw.cmd -q -B verify').flags, frozenset({'-q', '-b'}))
        chain = signature('cd app && ./mvnw -q verify | tee out.log; echo done')
        self.assertEqual([(s.program, s.subs) for s in chain], [('cd', ('app',)), ('mvnw', ('verify',)), ('tee', ('out.log',)), ('echo', ('done',))])
        quoted = signature('bash -c "cd app && ./mvnw test"')
        self.assertEqual({s.wrapper for s in quoted}, {'bash'})   # the && inside quotes stays inside the wrapper's command
        self.assertEqual([s.program for s in quoted], ['cd', 'mvnw'])

    def test_wrappers_with_nothing_to_run_give_no_signature(self):
        for text in ('cmd', 'bash', 'bash script.sh', 'powershell', 'wsl', '', '   '):
            self.assertEqual(signature(text), [], text)


class Extraction(unittest.TestCase):
    def test_commands_come_from_code_spans_and_shell_fences(self):
        body = ('## 📌 Executive Summary\nRunning `cmd /c mvnw.cmd` from Git Bash builds nothing and exits 0; use ./mvnw.\n\n---\n\n## 📝 Key Details\n'
                '* **Fix:** `./mvnw -q verify`, or `py -3.15 scripts/simulate.py`\n'
                '```bash\n$ git push --force origin main\n# a comment\nmvn -q \\\n  verify\n```\n'
                '```python\ngit push nothing\n```\n')
        self.assertEqual(extract('Build with `go test ./...`', body),
                         ['go test ./...', 'cmd /c mvnw.cmd', 'git push --force origin main', 'mvn -q verify', './mvnw -q verify', 'py -3.15 scripts/simulate.py'])

    def test_identifiers_paths_and_flags_are_not_commands(self):
        body = ('## 📌 Executive Summary\nUse `Money` and `BigDecimal` as in `ADR-004`, see `docs/adr/x.md`, `src/main/App.java`, `--flag`, `-q`, `node`, `git`, `go`, '
                '`java.util.List`, `Math.max(a, b)`, `./src/notes.md`, `core/search.py`, `make`.')
        self.assertEqual(extract('Money in `BigDecimal`', body), [])

    def test_links_and_actions_are_not_read_and_limits_hold(self):
        body = ('## 📌 Executive Summary\nSee below.\n\n---\n\n## 🗂️ Resource Links\n* `git push --force`\n\n## 🟩 Action Items\n- [ ] run `mvn test`\n')
        self.assertEqual(extract('x', body), [])
        many = '## 📌 Executive Summary\n' + ' '.join(f'`mvn goal{i}`' for i in range(15))
        self.assertEqual(len(extract('x', many)), 10)
        self.assertEqual(extract('x', '## 📌 Executive Summary\n`mvn ' + 'a' * 300 + '`'), [])   # too long to be one command

    def test_explicit_commands_come_first_and_merge_without_repeats(self):
        body = '## 📌 Executive Summary\nSee `mvn verify` and `./mvnw verify`.'
        self.assertEqual(extract('x', body, ['deploy-tool run', 'MVN verify']), ['deploy-tool run', 'MVN verify', './mvnw verify'])


class Matching(unittest.TestCase):
    def test_exact_needs_the_same_wrapper_and_related_is_another_wrapper(self):
        note = one(r'cmd /c mvnw.cmd')
        self.assertEqual(match(note, one(r'cmd //c mvnw.cmd -q -B verify')), 'exact')
        self.assertEqual(match(note, one('./mvnw -q verify')), 'related')
        self.assertEqual(match(note, one('bash -c "./mvnw test"')), 'related')
        self.assertEqual(match(one('./mvnw'), one('./mvnw verify')), 'exact')   # both unwrapped
        self.assertIsNone(match(note, one('mvn verify')))

    def test_the_notes_subcommands_must_lead_the_commands(self):
        self.assertEqual(match(one('mvn verify'), one('mvn verify -pl core')), 'exact')
        self.assertIsNone(match(one('mvn verify'), one('mvn clean')))
        self.assertIsNone(match(one('./mvnw verify'), one('./mvnw')))

    def test_generic_programs_need_a_matching_subcommand(self):
        force = one('git push --force')
        self.assertEqual(match(force, one('git push --force origin main')), 'exact')
        self.assertIsNone(match(force, one('git push origin main')))      # the note is about the flag
        self.assertEqual(match(one('git push'), one('git push origin main')), 'exact')
        self.assertIsNone(match(force, one('git status')))
        for bare in ('git', 'npm', 'python', 'pip', 'curl', 'docker'):
            self.assertIsNone(match(Sig(None, bare, (), frozenset()), Sig(None, bare, ('x',), frozenset())), bare)
        self.assertEqual(match(one('mvn'), one('mvn verify')), 'exact')   # a specific tool alone is enough

    def test_read_only_programs_and_the_memory_script_never_match(self):
        self.assertIsNone(match(Sig(None, 'grep', (), frozenset()), Sig(None, 'grep', (), frozenset())))
        self.assertIsNone(match(Sig(None, 'cat', ('x',), frozenset()), Sig(None, 'cat', ('x',), frozenset())))
        self.assertIsNone(match(Sig(None, 'living_memory', (), frozenset()), Sig(None, 'living_memory', ('search',), frozenset())))


class Hints(StoreFixture):
    def setUp(self):
        super().setUp()
        self.project = self.base / 'project'
        (self.project / 'core').mkdir(parents=True)
        self.gotcha = self.note('Maven wrapper from Git Bash', 'Running `cmd /c mvnw.cmd` from Git Bash builds nothing and exits 0; use ./mvnw.', type='bug')

    def payload(self, session='s1', **extra):
        return {'cwd': str(self.project), 'session_id': session, 'prompt': 'go', **extra}

    def run_command(self, command, harness='claude', session='s1', tool='Bash', **extra):
        p = self.payload(session, **extra)
        runtime.process(harness, 'user-prompt', p, self.root)
        return runtime.process(harness, 'pre-tool-use', {**p, 'tool_name': tool, 'tool_input': {'command': command}}, self.root)

    def state(self, session='s1'):
        return json.loads(runtime._pointer(self.root, session).read_text(encoding='utf-8'))

    def test_the_simulations_gotcha_is_shown_for_both_spellings_and_for_the_fix_as_related(self):
        first = self.run_command('cmd //c mvnw.cmd -q -B verify')
        self.assertIn('a previous session recorded about this command', first['context'])
        self.assertIn('Maven wrapper from Git Bash', first['context'])
        self.assertIn('demo:' + self.gotcha['id'][:8], first['context'])
        self.assertIn('[BUG', first['context'])
        self.assertIn('builds nothing and exits 0', first['context'])
        self.assertIn('context', self.run_command('cmd /c mvnw.cmd verify', session='s2'))
        related = self.run_command('./mvnw -q verify', session='s3')
        self.assertIn('Maven wrapper from Git Bash', related['context'])
        self.assertNotIn('context', self.run_command('mvn -q verify', session='s4'))
        self.assertNotIn('context', self.run_command('git status', session='s5'))

    def test_a_note_is_shown_once_per_session_and_a_command_shape_is_not_looked_up_again(self):
        self.run_command('cmd /c mvnw.cmd verify')
        self.assertEqual(self.state()['command_hints'], ['demo:' + self.gotcha['id']])
        with patch('core.commands.command_notes') as lookup:
            self.assertNotIn('context', self.run_command('cmd /c mvnw.cmd verify -q'))   # same shape: no lookup at all
            lookup.assert_not_called()
        self.assertNotIn('context', self.run_command('./mvnw verify'))                    # another shape, same note: already shown
        self.assertIn('context', self.run_command('cmd /c mvnw.cmd verify', session='s2'))

    def test_at_most_two_notes_and_the_exact_one_first(self):
        self.note('Wrapper script notes', 'The `./mvnw` script needs the exec bit.', type='decision')
        self.note('Maven via the batch file', 'Do not call `cmd /c mvnw.cmd`: it ignores the JDK.', type='decision')
        self.note('Another wrapper fact', 'Prefer `./mvnw` over mvn.', type='knowledge')
        text = self.run_command('cmd /c mvnw.cmd verify')['context']
        lines = [line for line in text.splitlines() if line.startswith('- [')]
        self.assertEqual(len(lines), 2)
        self.assertIn('Maven wrapper from Git Bash', lines[0])       # exact, bug
        self.assertIn('Maven via the batch file', lines[1])          # exact, decision before any related note
        # The related notes are still there for the next call: only the shown ones are remembered.
        self.assertEqual(len(self.state()['command_hints']), 2)
        more = self.run_command('./mvnw verify')['context']
        self.assertEqual(len([line for line in more.splitlines() if line.startswith('- [')]), 2)

    def test_ranking_prefers_exact_then_type_then_newest(self):
        decision = self.note('Wrapper decision', 'Always use `cmd /c mvnw.cmd` in CI.', type='decision')
        related = self.note('Wrapper tip', 'Use `./mvnw` locally.', type='knowledge')
        hits = commands.command_notes('cmd /c mvnw.cmd verify', ['demo'], self.root, limit=5)
        self.assertEqual([h['id'] for h in hits], [self.gotcha['id'], decision['id'], related['id']])
        self.assertEqual([h['match'] for h in hits], ['exact', 'exact', 'related'])
        self.assertTrue(all(h['snippet'] and h['updated'] for h in hits))

    def test_harness_gating(self):
        self.assertNotIn('context', self.run_command('cmd /c mvnw.cmd verify', harness='copilot', session='s5'))
        self.assertIn('context', self.run_command('cmd /c mvnw.cmd verify', harness='codex', session='s6'))

    def test_bindings_opt_out(self):
        bindings = self.root / 'bindings.json'
        data = json.loads(bindings.read_text(encoding='utf-8'))
        data['command_hints'] = False
        bindings.write_text(json.dumps(data), encoding='utf-8')
        self.assertNotIn('context', self.run_command('cmd /c mvnw.cmd verify'))
        self.assertNotIn('command_hints', self.state())

    def test_not_for_memory_scripts_read_only_programs_unknown_sessions_or_unbound_folders(self):
        self.note('Search flags', 'Use `grep -rn foo` to look.', type='knowledge', allow_similar=True)
        self.assertNotIn('context', self.run_command('grep -rn foo src'))
        self.assertNotIn('context', self.run_command('cmd /c mvnw.cmd verify', session='unknown'))
        other = self.base / 'elsewhere'
        other.mkdir()
        p = {'cwd': str(other), 'session_id': 's8', 'tool_name': 'Bash', 'tool_input': {'command': 'cmd /c mvnw.cmd verify'}}
        self.assertNotIn('context', runtime.process('claude', 'pre-tool-use', p, self.root))
        self.assertNotIn('context', self.run_command('python living_memory.py --domain demo search mvnw.cmd'))

    def test_a_read_only_command_is_still_looked_up(self):
        self.note('Diff gotcha', 'On this repo `git diff --stat` hangs on the generated folder.', type='bug')
        result = self.run_command('git diff --stat')
        self.assertIn('Diff gotcha', result['context'])
        self.assertEqual(self.run_command('git diff --stat', session='s2')['context'].count('Diff gotcha'), 1)
        self.assertNotIn('context', self.run_command('git log --oneline', session='s3'))

    def test_a_subagent_gets_the_hint(self):
        self.assertIn('Maven wrapper from Git Bash', self.run_command('cmd /c mvnw.cmd verify', agent_id='sub')['context'])

    def test_a_missing_or_old_index_gives_no_hint_and_no_error(self):
        db = self.root / 'domains/demo/search.sqlite3'
        conn = sqlite3.connect(db)
        conn.execute('ALTER TABLE notes DROP COLUMN commands')
        conn.commit()
        conn.close()
        result = self.run_command('cmd /c mvnw.cmd verify')
        self.assertNotIn('context', result)
        self.assertNotIn('deny', result)
        db.unlink()
        result = self.run_command('cmd /c mvnw.cmd verify', session='s2')
        self.assertNotIn('context', result)
        self.assertNotIn('deny', result)

    def test_a_note_changed_since_the_index_was_built_is_not_trusted(self):
        path = next(p for p in (self.root / 'domains/demo/notes').glob('*.md') if 'Maven' in p.read_text(encoding='utf-8'))
        path.write_text(path.read_text(encoding='utf-8') + '\nedited outside the scripts\n', encoding='utf-8')
        self.assertNotIn('context', self.run_command('cmd /c mvnw.cmd verify'))

    def test_command_hints_join_file_hints_instead_of_replacing_them(self):
        self.note('Notes parser gotcha', 'Frontmatter keys keep their order.', files=['core/notes.py'], allow_similar=True)
        target = str(self.project / 'core' / 'notes.py')
        text = self.run_command(f'cmd /c mvnw.cmd verify {target}')['context']
        self.assertIn('a previous session recorded about this command', text)
        self.assertIn('Living Memory notes about core/notes.py', text)
        self.assertIn('Notes parser gotcha', text)

    def test_the_diary_and_the_log_detail_name_the_hint(self):
        from core import usage
        self.run_command('cmd /c mvnw.cmd verify')
        rows = [r for r in usage.events(self.root) if r['e'] == 'inject' and r['k'] == 'command-hint']
        self.assertEqual([r['ref'] for r in rows], ['demo:' + self.gotcha['id']])

    def test_the_seen_and_hinted_lists_are_capped(self):
        for i in range(105):
            self.note(f'Gotcha {i} widget{i}', f'Running `./tool{i} go` fails on widget{i}.', allow_similar=True)
        for i in range(105):
            self.run_command(f'./tool{i} go')
        state = self.state()
        self.assertEqual(len(state['command_hints']), 100)
        self.assertLessEqual(len(state['command_seen']), 200)


class ExplicitCommands(StoreFixture):
    def cli(self, *argv):
        from core.cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--domain', 'demo', *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def commands_of(self, note_id):
        return next(n for n in inventory('demo', self.root) if n['id'] == note_id).get('commands')

    def indexed(self, note_id):
        db = sqlite3.connect(self.root / 'domains/demo/search.sqlite3')
        try:
            return json.loads(db.execute('SELECT commands FROM notes WHERE id=?', (note_id,)).fetchone()[0])
        finally:
            db.close()

    def test_capture_and_update_round_trip(self):
        code, made = self.cli('capture', *TAGS, '--title', 'Deploy gotcha', '--summary', 'Deploys skip the lint step.', '--command', 'deploy-tool apply', '--command', 'deploy-tool rollback')
        self.assertEqual(code, 0)
        self.assertEqual(self.commands_of(made['id']), ['deploy-tool apply', 'deploy-tool rollback'])
        self.assertEqual(self.indexed(made['id']), ['deploy-tool apply', 'deploy-tool rollback'])
        code, _ = self.cli('update', made['id'], '--command', 'deploy-tool status')
        self.assertEqual(code, 0)
        self.assertEqual(self.commands_of(made['id']), ['deploy-tool apply', 'deploy-tool rollback', 'deploy-tool status'])
        self.assertEqual(self.indexed(made['id']), ['deploy-tool apply', 'deploy-tool rollback', 'deploy-tool status'])
        hits = commands.command_notes('deploy-tool apply --env prod', ['demo'], self.root)
        self.assertEqual([h['id'] for h in hits], [made['id']])
        code, result = self.cli('read', made['id'], '--metadata-only')
        self.assertEqual(result['result']['commands'] if 'result' in result else result['commands'], ['deploy-tool apply', 'deploy-tool rollback', 'deploy-tool status'])

    def test_a_payload_may_carry_commands_but_not_beside_a_flag(self):
        payload = self.base / 'payload.json'
        payload.write_text(json.dumps({'title': 'Release gotcha', 'body': 'The release tool skips tags.', 'tags': ['subject/project', 'concern/architecture'], 'commands': ['release-tool cut']}), encoding='utf-8')
        code, made = self.cli('capture', '--payload-file', str(payload))
        self.assertEqual(code, 0)
        self.assertEqual(self.commands_of(made['id']), ['release-tool cut'])
        from core.cli import main
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--domain', 'demo', 'capture', '--payload-file', str(payload), '--command', 'x y'])
        self.assertNotEqual(code, 0)

    def test_limits_are_enforced_on_write_and_on_validate(self):
        code, _ = self.cli('capture', *TAGS, '--title', 'Too many', '--summary', 'Eleven commands.', *[a for i in range(11) for a in ('--command', f'tool{i} run')])
        self.assertNotEqual(code, 0)
        code, _ = self.cli('capture', *TAGS, '--title', 'Too long', '--summary', 'One long command.', '--command', 'tool ' + 'a' * 300)
        self.assertNotEqual(code, 0)
        note = {'id': 'x', 'title': 't', 'body': 'b', 'tags': [], 'commands': ['ok'] * 11}
        for bad in (['a'] * 11, ['x' * 201], [''], 'run it', [1]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate({**note, 'commands': bad}, 'demo', self.root)

    def test_removing_the_list_clears_it(self):
        code, made = self.cli('capture', *TAGS, '--title', 'Deploy gotcha', '--summary', 'Deploys skip lint.', '--command', 'deploy-tool apply')
        from core.notes import change
        change(made['id'], 'demo', 'update', self.root, commands=[])
        self.assertIsNone(self.commands_of(made['id']))
        self.assertEqual(self.indexed(made['id']), [])


if __name__ == '__main__':
    unittest.main()
