"""Stop hook: real file changes by the main agent are tracked but end the turn quietly; a durable statement
by the user arms the reminder."""
import contextlib
import io
import json

from test_core import StoreFixture
from core import runtime
from core.cli import main


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

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['--root', str(self.root), '--domain', 'demo', *argv])
        self.assertEqual(code, 0, out.getvalue())
        return json.loads(out.getvalue())

    def capture(self, title, summary):
        return self.cli('capture', '--session', 'session', '--type', 'knowledge', '--tag', 'subject/project', '--tag', 'concern/architecture', '--title', title, '--summary', summary)

    def test_an_unrelated_capture_does_not_settle_a_request_to_remember(self):
        self.payload = {**self.payload, 'prompt': 'Remember that the staging box is shared'}
        runtime.process('claude', 'user-prompt', self.payload, self.root)
        self.capture('Retries use jittered backoff', 'Clients retry with a random delay.')
        self.assertIn('asked you to remember something', runtime.process('claude', 'stop', self.payload, self.root)['continue_reason'])
        self.assertEqual(runtime.process('claude', 'stop', self.payload, self.root), {})   # still one reminder per reply

    def test_a_capture_that_paraphrases_the_statement_settles_it(self):
        self.payload = {**self.payload, 'prompt': 'Remember that the staging box is shared'}
        runtime.process('claude', 'user-prompt', self.payload, self.root)
        self.capture('Shared staging box', 'Everyone deploys to one staging box, so coordinate before you use it.')
        self.assertEqual(runtime.process('claude', 'stop', self.payload, self.root), {})

    def test_an_update_of_a_note_that_covers_the_statement_settles_it(self):
        old = self.note('Staging environment', 'Staging hosts the nightly build.')
        self.payload = {**self.payload, 'prompt': 'Remember that the staging box is shared'}
        runtime.process('claude', 'user-prompt', self.payload, self.root)
        self.cli('update', 'demo:' + old['id'], '--session', 'session', '--add-detail', '**Access:** the box is shared between teams')
        self.assertEqual(runtime.process('claude', 'stop', self.payload, self.root), {})

    def test_capture_finalize_settles_it(self):
        self.payload = {**self.payload, 'prompt': 'Remember that the staging box is shared'}
        runtime.process('claude', 'user-prompt', {**self.payload, 'turn_id': 't1'}, self.root)
        from core.capture import finalize
        finalize('session', 't1', [], self.root)
        self.assertEqual(runtime.process('claude', 'stop', {**self.payload, 'turn_id': 't1'}, self.root), {})

    def test_only_salted_word_hashes_of_the_statement_are_kept(self):
        runtime.process('claude', 'user-prompt', {**self.payload, 'prompt': 'Remember that we use zebra-tabs'}, self.root)
        state = json.loads(runtime._pointer(self.root, 'session').read_text(encoding='utf-8'))
        self.assertEqual(len(state['signal_words']), 3)   # use, zebra, tabs: the request wording is not content
        self.assertNotIn('zebra', json.dumps(state))
        runtime.process('claude', 'stop', self.payload, self.root)
        state = json.loads(runtime._pointer(self.root, 'session').read_text(encoding='utf-8'))
        self.assertNotIn('signal_words', state)

    def test_capture_finalize_after_an_unrelated_capture_settles_it(self):
        self.payload = {**self.payload, 'prompt': 'Remember that the staging box is shared'}
        runtime.process('claude', 'user-prompt', self.payload, self.root)
        self.capture('Retries use jittered backoff', 'Clients retry with a random delay.')
        self.assertEqual(self.cli('capture-finalize', '--session', 'session')['status'], 'dismissed')
        self.assertEqual(runtime.process('claude', 'stop', self.payload, self.root), {})

    def test_a_verbatim_save_of_a_short_technical_statement_settles_it(self):
        self.payload = {**self.payload, 'prompt': 'Remember that CI uses Go.'}
        runtime.process('claude', 'user-prompt', self.payload, self.root)
        self.capture('CI uses Go', 'CI uses Go.')
        self.assertEqual(runtime.process('claude', 'stop', self.payload, self.root), {})

    def test_every_statement_the_nudge_names_is_raised_once(self):
        self.payload = {**self.payload, 'prompt': 'I prefer short runbooks. We decided to standardize on the SDK.'}
        first = runtime.process('claude', 'user-prompt', self.payload, self.root)
        self.assertIn('stated a preference', json.dumps(first))
        runtime.process('claude', 'stop', self.payload, self.root)
        again = runtime.process('claude', 'user-prompt', self.payload, self.root)
        self.assertNotIn('Living Memory: the user', json.dumps(again))

    def test_state_without_stored_words_is_settled_by_any_receipt(self):
        self.payload = {**self.payload, 'prompt': 'Remember that the staging box is shared'}
        runtime.process('claude', 'user-prompt', self.payload, self.root)
        runtime._update(self.root, 'session', lambda s: s.pop('signal_words'))
        self.capture('Retries use jittered backoff', 'Clients retry with a random delay.')
        self.assertEqual(runtime.process('claude', 'stop', self.payload, self.root), {})

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

    def test_a_headless_run_is_told_to_end_with_its_answer(self):
        from unittest.mock import patch
        sentence = 'This is a non-interactive run and only your last message is returned: after saving, end with your answer to the task repeated in full.'
        for i, env in enumerate(({'LIVING_MEMORY_HEADLESS': '1'}, {'CLAUDE_CODE_SESSION_ATTENDED': '0'}, {'CLAUDE_CODE_ENTRYPOINT': 'sdk-cli'})):
            with self.subTest(env=env), patch.dict('os.environ', env):
                self.payload['prompt'] = f'remember that we use {i} tabs'   # a statement is raised once per domain
                self.payload['session_id'] = f'headless{i}'
                text = self.turn(self.edit)['continue_reason']
                self.assertTrue(text.endswith(sentence), text)
                self.assertIn('Nothing durable: CMD capture-finalize', text)
        self.payload['session_id'] = 'attended';self.payload['prompt'] = 'remember that we use attended tabs'
        text = self.turn(self.edit)['continue_reason']
        self.assertNotIn('non-interactive', text)
        self.assertTrue(text.endswith('capture-finalize --session attended.'))
        self.assertFalse(runtime.headless())

    def test_the_headless_sentence_counts_toward_the_text_cap(self):
        from unittest.mock import patch
        pending = [('remember', {'files': False}), ('doc', {'path': 'docs/adr/ADR-004-money.md', 'notes': []}),
                   ('unbriefed', {'name': 'project', 'dir': '.'})]
        plain = runtime.stop_text(pending, 's')
        with patch.dict('os.environ', {'LIVING_MEMORY_HEADLESS': '1'}):
            text = runtime.stop_text(pending, 's')
        self.assertTrue(text.endswith(runtime.HEADLESS_TEXT))
        self.assertLessEqual(len(text) - len(runtime.HEADLESS_TEXT) - 1, runtime.STOP_TEXT_CAP)   # room was reserved for the sentence
        self.assertGreaterEqual(len(plain), len(text) - len(runtime.HEADLESS_TEXT) - 1)
        self.assertIn('Nothing durable', text)

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
