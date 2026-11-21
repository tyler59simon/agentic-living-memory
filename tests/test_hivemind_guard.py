"""Hivemind guard: with HIVEMIND_SESSION set, an agent's shell command may not apply or undo a plan or make a direct write."""
import contextlib, io, json, os, sys, unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core import logs, runtime
from core.cli import main

from core.runtime import HIVEMIND_DENY as REASON
MODES = ('default', 'acceptEdits', 'plan', 'auto', 'dontAsk', 'bypassPermissions')
APPLY = 'apply plan-1 --expected-plan-sha256 abc'


class HivemindGuard(StoreFixture):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {'HIVEMIND_SESSION': 'hm-1', 'LIVING_MEMORY_LOG_LEVEL': 'debug', 'LIVING_MEMORY_LOG_CONTENT': ''})
        env.start(); self.addCleanup(env.stop)

    def memory(self, rest):
        return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'

    def payload(self, harness, tool, command, **extra):
        args = {'command': ['bash', '-lc', command]} if (harness, tool) == ('codex', 'shell') else {'command': command}
        return {'cwd': str(self.base / 'project'), 'session_id': 's1', 'tool_name': tool, 'tool_input': args, **extra}

    def run_hook(self, harness, tool, command, **extra):
        return runtime.process(harness, 'pre-tool-use', self.payload(harness, tool, command, **extra), self.root)

    def denied(self, command, harness='claude', tool='Bash', **extra):
        return self.run_hook(harness, tool, command, **extra).get('deny') == REASON

    def test_governed_subcommands_are_denied_for_both_harnesses(self):
        for rest in (APPLY, 'undo op-1', 'recover op-1 --direction backward', 'setup --apply-plan p1', 'archive demo:x', 'rewrite --payload-file r.json', 'remap', 'promote demo:x', 'taxonomy add concern/x', 'source add', 'project create', 'system update', 'change close'):
            for harness, tool in (('claude', 'Bash'), ('codex', 'shell'), ('codex', 'exec_command')):
                with self.subTest(rest=rest, harness=harness, tool=tool):
                    result = self.run_hook(harness, tool, self.memory(rest))
                    self.assertEqual(result.get('deny'), REASON)
                    self.assertNotIn('allow', result)

    def test_decision_detail_is_deny_hivemind(self):
        logs.use(self.root)
        runtime.process('claude', 'user-prompt', {'cwd': str(self.base / 'project'), 'session_id': 's1', 'prompt': 'work on it'}, self.root)
        self.run_hook('claude', 'Bash', self.memory('undo op-1'))
        row = logs.entries(self.root, 'debug', limit=10, kind='hook')[0][0]
        self.assertEqual(row['detail']['decision'], 'deny:hivemind')

    def test_adapters_emit_the_deny(self):
        from hooks import claude_hooks, codex_hooks
        with patch.dict(os.environ, {'LIVING_MEMORY_ROOT': str(self.root)}):
            claude = claude_hooks.handle('pre-tool-use', self.payload('claude', 'Bash', self.memory(APPLY)))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                codex_hooks.handle('pre-tool-use', self.payload('codex', 'shell', self.memory(APPLY)))
        for result in (claude, json.loads(out.getvalue())):
            body = result['hookSpecificOutput']
            self.assertEqual((body['hookEventName'], body['permissionDecision'], body['permissionDecisionReason']), ('PreToolUse', 'deny', REASON))

    def test_setup_is_denied_only_with_apply_plan(self):
        for rest in ('setup --apply-plan p1', 'setup --apply-plan=p1', 'setup --apply p1', 'setup --non-interactive --apply-plan p1'):
            self.assertTrue(self.denied(self.memory(rest)), rest)
        for rest in ('setup', 'setup --inspect-plan p1', 'setup --non-interactive'):
            self.assertFalse(self.denied(self.memory(rest)), rest)

    def test_chained_commands_are_denied(self):
        for command in (f'cd x && {self.memory(APPLY)}',
                        f'{self.memory("undo op-1")} ; echo done',
                        f'{self.memory("recover op-1")} | cat',
                        'cd x && python /opt/living_memory.py setup --apply-plan p1',
                        'cd x && alm apply p1 --expected-plan-sha256 a',
                        'alm undo op-1 && echo ok'):
            with self.subTest(command=command):
                self.assertTrue(self.denied(command))
                self.assertTrue(self.denied(command, harness='codex', tool='shell'))

    def test_powershell_forms_are_denied(self):
        for command in (self.memory('undo op-1'),
                        '& ' + self.memory(APPLY),
                        'Set-Location x; & ' + self.memory('recover op-1'),
                        'cd x; python .\\living_memory.py apply p1'):
            with self.subTest(command=command):
                self.assertTrue(self.denied(command, tool='PowerShell'))

    def test_every_permission_mode_is_denied(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertTrue(self.denied(self.memory(APPLY), permission_mode=mode))
                self.assertTrue(self.denied(f'cd x && {self.memory("undo op-1")}', permission_mode=mode))
                self.assertTrue(self.denied(self.memory('undo op-1'), harness='codex', tool='shell', permission_mode=mode))

    def test_a_subagent_is_denied_too(self):
        self.assertTrue(self.denied(self.memory('undo op-1'), agent_id='sub'))

    def test_reading_and_plan_writing_commands_are_allowed(self):
        for rest in ('plan --list', 'plan delete demo:NOTE', 'inspect-plan plan-1', 'setup --inspect-plan p1', 'search apply', 'taxonomy', 'projects',
                     'read undo', 'show', 'diff op-1', 'history', 'retag-plan', 'merge-plan', 'restore-plan b.zip', 'import-plan x.zip',
                     'ingest-plan', 'doctor', "capture 'apply the plan' 'undo recover'"):
            with self.subTest(rest=rest):
                self.assertFalse(self.denied(self.memory(rest)))
                self.assertFalse(self.denied(self.memory(rest), harness='codex', tool='shell'))
        for command in ('git apply fix.patch', 'cd x && echo undo recover', 'python tool.py apply'):
            self.assertFalse(self.denied(command), command)
        # Fails closed: an unparsed command naming alm and a governed word is refused even when the word is only an argument.
        self.assertTrue(self.denied('cd x && alm search apply'))

    def test_no_refusal_without_the_variable(self):
        for value in (None, ''):
            with self.subTest(value=value):
                env = {k: v for k, v in os.environ.items() if k != 'HIVEMIND_SESSION'}
                if value is not None:
                    env['HIVEMIND_SESSION'] = value
                with patch.dict(os.environ, env, clear=True):
                    for command in (self.memory(APPLY), self.memory('setup --apply-plan p1'), f'cd x && {self.memory("undo op-1")}', 'alm recover op-1'):
                        self.assertFalse(self.denied(command), command)
                        self.assertFalse(self.denied(command, harness='codex', tool='shell'), command)

    def test_non_shell_tools_are_not_checked(self):
        payload = {'cwd': str(self.base / 'project'), 'session_id': 's1', 'tool_name': 'Read',
                   'tool_input': {'file_path': str(self.base / 'project' / 'undo.txt')}}
        self.assertNotIn('deny', runtime.process('claude', 'pre-tool-use', payload, self.root))


    def test_unbound_working_directory_is_denied(self):
        elsewhere = str(self.base / 'unbound')
        for harness, tool in (('claude', 'Bash'), ('codex', 'shell')):
            payload = self.payload(harness, tool, self.memory('undo op-1'), cwd=elsewhere)
            self.assertEqual(runtime.process(harness, 'pre-tool-use', payload, self.root).get('deny'), REASON)
            payload = self.payload(harness, tool, 'alm --domain demo undo op-1', cwd=elsewhere)
            self.assertEqual(runtime.process(harness, 'pre-tool-use', payload, self.root).get('deny'), REASON)

    def test_equals_and_abbreviated_global_options_are_denied(self):
        base = f'"{sys.executable}" "{runtime.SCRIPT}"'
        for rest in ('--domain=demo undo op-1', '--dom demo undo op-1', '--root=x --domain=demo apply p --expected-plan-sha256 a',
                     '--domain=demo setup --apply-plan p1', '--json --actor=main recover op-1'):
            with self.subTest(rest=rest):
                self.assertTrue(self.denied(f'{base} {rest}'))
                self.assertTrue(self.denied(f'{base} {rest}', tool='PowerShell'))
        self.assertFalse(self.denied(f'{base} --domain=demo search cache'))


class HivemindCli(StoreFixture):
    """The CLI refuses governed commands itself: batch and serve re-enter main(), so they pass the same check."""
    def run_main(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is None:
                code = main(['--root', str(self.root), '--domain', 'demo', *argv])
            else:
                with patch('sys.stdin', io.StringIO(stdin)):
                    code = main(['--root', str(self.root), '--domain', 'demo', *argv])
        return code, out.getvalue(), err.getvalue()

    def refused(self, err):
        error = json.loads(err)['error']
        return error['code'] == 'hivemind' and error['message'].endswith(REASON)

    def with_env(self, value):
        env = patch.dict(os.environ, {'HIVEMIND_SESSION': value} if value else {}, clear=False)
        env.start(); self.addCleanup(env.stop)
        if not value:
            os.environ.pop('HIVEMIND_SESSION', None)

    def test_governed_commands_are_refused_with_the_variable(self):
        self.with_env('hm-1')
        for argv in (['apply', 'p1', '--expected-plan-sha256', 'a'], ['undo', 'op-1'], ['recover', 'op-1'], ['setup', '--apply-plan', 'p1', '--expected-plan-sha256', 'a']):
            with self.subTest(argv=argv):
                code, out, err = self.run_main('--json', *argv)
                self.assertEqual(code, 2)
                self.assertTrue(self.refused(err), err)

    def test_other_commands_and_the_unset_variable_pass_the_check(self):
        self.with_env('hm-1')
        code, out, err = self.run_main('--json', 'version')
        self.assertEqual(code, 0, err)
        self.with_env('')
        code, out, err = self.run_main('--json', 'undo', 'missing-op')
        self.assertNotEqual(code, 0)
        self.assertFalse(self.refused(err), err)

    def test_batch_with_a_nested_undo_is_refused(self):
        self.with_env('hm-1')
        payload = self.base / 'ops.json'
        payload.write_text(json.dumps([{'argv': ['version']}, {'argv': ['undo', 'op-1']}]))
        code, out, err = self.run_main('--json', 'batch', '--payload-file', str(payload))
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertEqual(rows[0]['exit_code'], 0)
        self.assertEqual(rows[1]['exit_code'], 2)
        self.assertEqual(json.loads(rows[1]['error'])['error']['code'], 'hivemind')

    def test_serve_refuses_a_governed_request(self):
        self.with_env('hm-1')
        lines = ''.join(json.dumps(r) + chr(10) for r in ({'id': 1, 'argv': ['undo', 'op-1']}, {'id': 2, 'argv': ['version']}))
        code, out, err = self.run_main('serve', '--stdio', stdin=lines)
        replies = [json.loads(l) for l in out.splitlines()]
        self.assertEqual(replies[0]['exit_code'], 2)
        self.assertEqual(replies[0]['error']['code'], 'hivemind')
        self.assertEqual(replies[1]['exit_code'], 0)


if __name__ == '__main__':
    unittest.main()
