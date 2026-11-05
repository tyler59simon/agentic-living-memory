"""Stop-hook reminder: only real file changes by the main agent arm it."""
from test_core import StoreFixture
from core import runtime


class StopHook(StoreFixture):
    def setUp(self):
        super().setUp()
        self.cwd = str(self.base / 'project')
        self.payload = {'cwd': self.cwd, 'session_id': 'session', 'prompt': 'work on it'}

    def turn(self, *tools, harness='claude'):
        runtime.process(harness, 'user-prompt', self.payload, self.root)
        for tool in tools:
            runtime.process(harness, 'pre-tool-use', {**self.payload, **tool}, self.root)
        return runtime.process(harness, 'stop', self.payload, self.root)

    @staticmethod
    def shell(command, tool='Bash'):
        return {'tool_name': tool, 'tool_input': {'command': command}}

    def test_readonly_shell_commands_do_not_arm(self):
        for command in ['ls -la', 'git status --short', 'git log --oneline -5 | head -3',
                        'grep -n x file.py 2>/dev/null', 'cat a.txt && wc -l a.txt']:
            with self.subTest(command=command):
                self.assertEqual(self.turn(self.shell(command)), {})
        for command in ['Get-ChildItem', 'Get-Content x.txt | Select-String foo']:
            with self.subTest(command=command):
                self.assertEqual(self.turn(self.shell(command, 'PowerShell')), {})

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
                self.assertEqual(self.turn(self.shell(command)), {})

    def test_redirection_substitution_and_unknown_commands_arm(self):
        for command in ['echo x > out.txt', 'cat $(ls)', 'touch x', 'git commit -m x', 'sed -i s/a/b/ f']:
            with self.subTest(command=command):
                self.assertIn('continue_reason', self.turn(self.shell(command)))

    def test_writing_forms_of_research_commands_arm(self):
        for command in ['sed -i.bak s/a/b/ f', 'sed -ni p f', "sed 's/a/b/w out' f", 'sed -f x.sed f',
                        "sed '1w out' f", "sed --in-place 's/a/b/' f",
                        "awk '{print > \"out\"}' f", "awk '{system(\"rm x\")}' f", "awk -i inplace '{print}' f",
                        'xargs rm', 'ls | xargs -I{} cp {} d',
                        'for f in *; do rm $f; done', 'uniq in out', '(rm x)', 'cd x && (touch y)',
                        'git stash', 'git stash pop', 'git reflog expire --all', 'git reflog delete HEAD@{1}']:
            with self.subTest(command=command):
                self.assertIn('continue_reason', self.turn(self.shell(command)))

    def test_plan_mode_writes_do_not_arm(self):
        plan_write = {'tool_name': 'Write', 'tool_input': {'file_path': self.cwd + '/plan.md'}, 'permission_mode': 'plan'}
        self.assertEqual(self.turn(plan_write), {})

    def test_subagent_tool_calls_do_not_arm(self):
        write = {'tool_name': 'Write', 'tool_input': {'file_path': self.cwd + '/x.py'},
                 'agent_id': 'agent-1', 'agent_type': 'Explore'}
        self.assertEqual(self.turn(write, self.shell('touch y') | {'agent_id': 'agent-1'}), {})

    def test_scratchpad_writes_do_not_arm(self):
        scratch = self.base / 'scratch'
        scratch.mkdir()
        payload = {'scratchpad_dir': str(scratch)}
        inside = {'tool_name': 'Write', 'tool_input': {'file_path': str(scratch / 'a.py')}, **payload}
        outside = {'tool_name': 'Write', 'tool_input': {'file_path': self.cwd + '/a.py'}, **payload}
        self.assertEqual(self.turn(inside), {})
        self.assertIn('continue_reason', self.turn(outside))

    def test_main_agent_write_arms_one_reminder_per_turn(self):
        write = {'tool_name': 'Edit', 'tool_input': {'file_path': self.cwd + '/x.py'}}
        runtime.process('claude', 'user-prompt', self.payload, self.root)
        runtime.process('claude', 'pre-tool-use', {**self.payload, **write}, self.root)
        first = runtime.process('claude', 'stop', self.payload, self.root)
        second = runtime.process('claude', 'stop', self.payload, self.root)
        self.assertIn('continue_reason', first)
        self.assertEqual(second, {})


if __name__ == '__main__':
    import unittest
    unittest.main()
