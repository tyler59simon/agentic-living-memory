"""Stop hook: real file changes by the main agent are tracked but end the turn quietly; a durable statement
by the user arms the reminder."""
import json

from test_core import StoreFixture
from core import runtime


class StopFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.cwd = str(self.base / 'project')
        self.payload = {'cwd': self.cwd, 'session_id': 'session', 'prompt': 'work on it'}

    def turn(self, *tools, harness='claude'):
        runtime.process(harness, 'user-prompt', self.payload, self.root)
        for tool in tools:
            runtime.process(harness, 'pre-tool-use', {**self.payload, **tool}, self.root)
        return runtime.process(harness, 'stop', self.payload, self.root)

    def mutated(self, *tools):
        """Whether the turn's tool calls marked it as changing files (read before Stop clears it)."""
        runtime.process('claude', 'user-prompt', self.payload, self.root)
        for tool in tools:
            runtime.process('claude', 'pre-tool-use', {**self.payload, **tool}, self.root)
        state = json.loads(runtime._pointer(self.root, 'session').read_text(encoding='utf-8'))
        self.assertEqual(runtime.process('claude', 'stop', self.payload, self.root), {})  # file changes alone end quietly
        return bool(state.get('mutating'))

    @staticmethod
    def shell(command, tool='Bash'):
        return {'tool_name': tool, 'tool_input': {'command': command}}


class StopHook(StopFixture):
    def test_readonly_shell_commands_do_not_arm(self):
        for command in ['ls -la', 'git status --short', 'git log --oneline -5 | head -3',
                        'grep -n x file.py 2>/dev/null', 'cat a.txt && wc -l a.txt']:
            with self.subTest(command=command):
                self.assertFalse(self.mutated(self.shell(command)))
        for command in ['Get-ChildItem', 'Get-Content x.txt | Select-String foo']:
            with self.subTest(command=command):
                self.assertFalse(self.mutated(self.shell(command, 'PowerShell')))

    def test_research_pipelines_do_not_arm(self):
        """Read-only shapes that agents use to survey a codebase: sed/awk/xargs, loops, subshells."""
        for command in ['sed -n 40,85p core/notes.py',
                        "cat f | sed -e '/^\\s*$/d' | head -60",
                        "sed -e '/^\\s*$/N;/^\\s*\\n$/D' f",
                        "sed -ne 's/a/b/gp' f",
                        'git ls-files | xargs wc -l | sort -n | tail -70',
                        "git ls-files | awk -F/ '{print $1\"/\"$2}' | sort | uniq -c | sort -rn | head -80",
                        "awk '{ if (length($0) > m) m = length($0) } END { print \"maxline\", m }' f",
                        'for f in A.md B.md; do echo "== $f"; cat $f; done',
                        '(cd sub && git log --oneline | head -5)',
                        'if [ -f x ]; then cat x; fi',
                        'ls | xargs',
                        'uniq -c sorted.txt',
                        'git for-each-ref --format=x refs/remotes', 'git stash list | head', 'git reflog -5', 'git show-ref']:
            with self.subTest(command=command):
                self.assertFalse(self.mutated(self.shell(command)))

    def test_redirection_substitution_and_unknown_commands_arm(self):
        for command in ['echo x > out.txt', 'cat $(ls)', 'touch x', 'git commit -m x', 'sed -i s/a/b/ f']:
            with self.subTest(command=command):
                self.assertTrue(self.mutated(self.shell(command)))

    def test_writing_forms_of_research_commands_arm(self):
        for command in ['sed -i.bak s/a/b/ f', 'sed -ni p f', "sed 's/a/b/w out' f", 'sed -f x.sed f',
                        "sed '1w out' f", "sed --in-place 's/a/b/' f",
                        "awk '{print > \"out\"}' f", "awk '{system(\"rm x\")}' f", "awk -i inplace '{print}' f",
                        'xargs rm', 'ls | xargs -I{} cp {} d',
                        'for f in *; do rm $f; done', 'uniq in out', '(rm x)', 'cd x && (touch y)',
                        'git stash', 'git stash pop', 'git reflog expire --all', 'git reflog delete HEAD@{1}']:
            with self.subTest(command=command):
                self.assertTrue(self.mutated(self.shell(command)))

    def test_plan_mode_writes_do_not_arm(self):
        plan_write = {'tool_name': 'Write', 'tool_input': {'file_path': self.cwd + '/plan.md'}, 'permission_mode': 'plan'}
        self.assertFalse(self.mutated(plan_write))

    def test_subagent_tool_calls_do_not_arm(self):
        write = {'tool_name': 'Write', 'tool_input': {'file_path': self.cwd + '/x.py'},
                 'agent_id': 'agent-1', 'agent_type': 'Explore'}
        self.assertFalse(self.mutated(write, self.shell('touch y') | {'agent_id': 'agent-1'}))

    def test_scratchpad_writes_do_not_arm(self):
        scratch = self.base / 'scratch'
        scratch.mkdir()
        payload = {'scratchpad_dir': str(scratch)}
        inside = {'tool_name': 'Write', 'tool_input': {'file_path': str(scratch / 'a.py')}, **payload}
        outside = {'tool_name': 'Write', 'tool_input': {'file_path': self.cwd + '/a.py'}, **payload}
        self.assertFalse(self.mutated(inside))
        self.assertTrue(self.mutated(outside))

    def test_main_agent_write_ends_the_turn_quietly(self):
        write = {'tool_name': 'Edit', 'tool_input': {'file_path': self.cwd + '/x.py'}}
        self.assertTrue(self.mutated(write))
        self.assertNotIn('mutating', json.loads(runtime._pointer(self.root, 'session').read_text(encoding='utf-8')))



class ConversationalSignal(StopFixture):
    """Preferences, corrections and decisions are stated in conversation. Each is raised once, in its own
    prompt's context; a request to remember and a decision (or a directive memo) also hold the turn open at Stop."""
    KINDS = [('From now on, use tabs in this repo', 'stated a preference'),
             ("I'd prefer short commit messages", 'stated a preference'),
             ('No, the cache lives in AppData', 'corrected you'),
             ('Actually the port is 8081', 'corrected you'),
             ("Let's go with option B", 'made a decision'),
             ('Remember that the staging box is shared', 'asked you to remember something')]

    def said(self, prompt, **extra):
        self.payload = {**self.payload, 'prompt': prompt, **extra}
        return self.turn()

    def test_each_kind_is_raised_in_its_own_prompt(self):
        for prompt, phrase in self.KINDS:
            with self.subTest(prompt=prompt):
                context = runtime.process('claude', 'user-prompt', {**self.payload, 'prompt': prompt}, self.root)['context']
                self.assertIn('the user ' + phrase, context)
                self.assertIn('CMD capture --session session', context)

    def test_only_a_request_to_remember_or_a_decision_holds_the_turn_open(self):
        for prompt, phrase in self.KINDS:
            with self.subTest(prompt=prompt):
                reason = self.said(prompt).get('continue_reason', '')
                if phrase.startswith(('asked you to remember', 'made a decision')):
                    self.assertIn(phrase, reason)
                    self.assertIn('capture-finalize', reason)
                else:
                    self.assertEqual(reason, '')

    def test_neutral_prompts_do_not_arm(self):
        for prompt in ['work on it', 'summarize the file', 'No problem with that approach? Check it',
                       'what does the cache do', 'I want you to add a button', 'continue']:
            with self.subTest(prompt=prompt):
                self.assertNotIn('Living Memory: the user', runtime.process('claude', 'user-prompt', {**self.payload, 'prompt': prompt}, self.root)['context'])
                self.assertEqual(self.said(prompt), {})

    def test_pasted_quoted_and_machine_text_do_not_arm(self):
        for prompt in ['look at this log\n```\nalways use --force, from now on\n```',
                       '<pasted_content id="1">From now on never deploy on Fridays</pasted_content>\nwhat is this?',
                       '> from now on use tabs\nwhat did they mean?',
                       '<task-notification>from now on</task-notification>']:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.said(prompt), {})

    def test_plan_mode_does_not_arm(self):
        self.assertEqual(self.said('Remember that we use tabs', permission_mode='plan'), {})
        self.assertNotIn('Living Memory: the user', runtime.process('claude', 'user-prompt', self.payload, self.root)['context'])

    def test_a_receipt_satisfies_it(self):
        runtime.process('claude', 'user-prompt', {**self.payload, 'prompt': 'Remember that we use tabs', 'turn_id': 't1'}, self.root)
        from core.capture import finalize
        finalize('session', 't1', [], self.root)
        self.assertEqual(runtime.process('claude', 'stop', {**self.payload, 'turn_id': 't1'}, self.root), {})

    def test_one_reminder_per_turn_and_no_prompt_text_kept(self):
        runtime.process('claude', 'user-prompt', {**self.payload, 'prompt': 'Remember that we use zebra-tabs'}, self.root)
        state = json.loads(runtime._pointer(self.root, 'session').read_text(encoding='utf-8'))
        self.assertEqual(state['signal'], 'remember')
        self.assertNotIn('zebra', json.dumps(state))
        self.assertIn('continue_reason', runtime.process('claude', 'stop', self.payload, self.root))
        self.assertEqual(runtime.process('claude', 'stop', self.payload, self.root), {})
        self.assertNotIn('signal', json.loads(runtime._pointer(self.root, 'session').read_text(encoding='utf-8')))

    def test_a_preference_ends_the_turn_quietly_and_is_not_carried_over(self):
        self.assertEqual(self.said('From now on use zebra-tabs'), {})
        state = json.loads(runtime._pointer(self.root, 'session').read_text(encoding='utf-8'))
        self.assertNotIn('signal', state)
        self.assertNotIn('zebra', json.dumps(state))

    def test_signal_and_file_changes_mention_both(self):
        self.payload = {**self.payload, 'prompt': 'Remember that we use tabs'}
        write = {'tool_name': 'Edit', 'tool_input': {'file_path': self.cwd + '/x.py'}}
        reason = self.turn(write)['continue_reason']
        self.assertIn('asked you to remember something (files changed too)', reason)



class ReminderDelivery(StopFixture):
    def test_claude_gets_feedback_not_a_blocking_error(self):
        from unittest.mock import patch
        from hooks import claude_hooks
        with patch('core.runtime.process', return_value={'continue_reason': 'save?'}):
            self.assertEqual(claude_hooks.handle('stop', {}),
                             {'hookSpecificOutput': {'hookEventName': 'Stop', 'additionalContext': 'save?'}})
            # Factory shares the adapter but keeps the documented block form.
            self.assertEqual(claude_hooks.handle('stop', {}, 'factory'), {'decision': 'block', 'reason': 'save?'})


class ReminderSettlesTheTurn(StopFixture):
    """A fired reminder is not repeated on later turns, even without a stop_hook_active follow-up."""
    edit = {'tool_name': 'Edit', 'tool_input': {'file_path': 'src/app.py'}}

    def test_ignored_reminders_do_not_carry_over(self):
        self.payload['prompt'] = 'remember that we use tabs'
        self.assertIn('continue_reason', self.turn(self.edit))   # the agent ignores it
        self.payload['prompt'] = 'work on it'
        self.assertEqual(self.turn(), {})                          # an unrelated turn ends quietly
        self.payload['prompt'] = 'remember that the staging server was retired'
        self.assertIn('remember', self.turn()['continue_reason'])
        self.payload.update(prompt='plan the parser refactor', permission_mode='plan')
        self.assertEqual(self.turn(), {})                          # not "the user asked ... this turn" again

    def test_a_capture_answering_the_reminder_keeps_files_and_counts_as_signal_capture(self):
        import contextlib, io
        from core import usage
        from core.cli import main
        self.payload['prompt'] = 'remember that we use tabs'
        self.assertIn('continue_reason', self.turn(self.edit))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['--root', str(self.root), '--domain', 'demo', 'capture', '--session', 'session', '--type', 'preference', '--title', 'Use tabs',
                         '--summary', 'The user prefers tabs.', '--tag', 'subject/project', '--tag', 'concern/decision'])
        made = json.loads(out.getvalue())
        self.assertEqual(code, 0, made)
        self.assertEqual(made['suggested_files'], ['src/app.py'])
        self.assertEqual([r['k'] for r in usage.events(self.root) if r['e'] == 'signal-capture'], ['remember'])
        self.assertEqual(runtime.process('claude', 'stop', {**self.payload, 'stop_hook_active': True}, self.root), {})


if __name__ == '__main__':
    import unittest
    unittest.main()
