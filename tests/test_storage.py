import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from test_core import StoreFixture
from core import storage
from core.repository import read_json, store_lock


class SharedStorage(StoreFixture):
    def setUp(self):
        super().setUp()
        self.config = self.base / 'user config' / 'config.json'
        self.addCleanup(patch.stopall)
        patch.object(storage, 'config_path', return_value=self.config).start()
        patch.object(storage, 'legacy_roots', return_value=[]).start()
        patch.dict(os.environ, {}, clear=False).start()
        os.environ.pop('LIVING_MEMORY_ROOT', None)
        os.environ.pop('LIVING_MEMORY_HOME', None)

    def configure(self, root=None):
        return storage.setup(root or self.root, True)

    def plan(self):
        self.configure()
        self.note()
        return storage.relocation_plan(self.root, self.base / 'new shared store')

    def apply(self, plan):
        return storage.apply_relocation(plan['plan_id'], plan['digest'])

    def test_fresh_setup_initializes_and_repeats(self):
        target = self.base / 'new empty store'
        self.configure(target)
        self.assertTrue((target / 'tag_taxonomy.json').exists())
        self.assertEqual(storage.resolve_root(), (target, 'shared_config'))
        self.configure(target)
        self.assertEqual(read_json(self.config)['data_root'], str(target))

    def test_no_store_requires_setup_without_creating_one(self):
        with self.assertRaisesRegex(ValueError, 'setup required'):
            storage.resolve_root()
        self.assertFalse(self.config.exists())
        with self.assertRaisesRegex(ValueError, '--location'):
            storage.setup(non_interactive=True)

    def test_prompt_accepts_suggested_location(self):
        with patch.object(Path, 'home', return_value=self.base), patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value=''):
            result = storage.setup()
        self.assertEqual(result['root'], str(self.base / 'LivingMemory'))

    def test_precedence_and_diagnostics(self):
        self.configure()
        a, b = self.base / 'override', self.base / 'old override'
        with patch.dict(os.environ, {'LIVING_MEMORY_ROOT': str(a), 'LIVING_MEMORY_HOME': str(b)}):
            self.assertEqual(storage.resolve_root()[0], a)
            self.assertEqual(storage.resolve_root(self.root)[1], 'argument')
            info = storage.diagnostics()
            self.assertEqual(len(info['conflicting_overrides']), 2)
        self.assertEqual(storage.resolve_root()[0], self.root)

    def test_invalid_or_missing_configured_store_never_falls_back(self):
        self.configure()
        self.config.write_text('{invalid')
        with patch.object(storage, 'legacy_roots', return_value=[self.root]):
            with self.assertRaisesRegex(ValueError, 'Invalid configuration'):
                storage.resolve_root()
        self.config.write_text(json.dumps({'version': 1, 'data_root': str(self.base / 'missing')}))
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            storage.resolve_root()

    def test_legacy_read_is_nonmutating_and_setup_adopts(self):
        with patch.object(storage, 'legacy_roots', return_value=[self.root]):
            self.assertEqual(storage.resolve_root()[1], 'legacy')
            self.assertFalse(self.config.exists())
            self.configure()
            self.assertEqual(storage.resolve_root()[1], 'shared_config')

    def test_bad_destinations_and_unrelated_files_are_preserved(self):
        for value in ('relative/folder', '//server/share/store'):
            with self.assertRaises(ValueError):
                self.configure(value)
        directory = self.base / 'unrelated'
        directory.mkdir()
        (directory / 'precious.txt').write_text('preserve')
        with self.assertRaises(ValueError):
            self.configure(directory)
        self.assertEqual((directory / 'precious.txt').read_text(), 'preserve')

    def test_relocation_is_preview_then_verified_copy(self):
        plan = self.plan()
        before = storage._manifest(self.root)
        self.assertEqual(storage.resolve_root()[0], self.root)
        self.assertFalse(Path(plan['destination']).exists())
        self.apply(plan)
        target = Path(plan['destination'])
        self.assertEqual(storage.resolve_root()[0], target)
        self.assertEqual(storage._manifest(target), before)
        self.assertTrue((target / 'domains/demo/search.sqlite3').exists())
        self.assertTrue((self.root / 'bindings.json').exists())
        with self.assertRaisesRegex(ValueError, 'retired'):
            with store_lock(self.root):
                pass
        self.assertEqual(self.apply(plan)['status'], 'committed')

    def test_relocation_stale_source_and_digest_do_not_switch(self):
        plan = self.plan()
        initial = storage.inspect_relocation(plan['plan_id'])
        self.assertEqual(initial['changed'] + initial['added'] + initial['removed'], [])
        with self.assertRaisesRegex(ValueError, 'digest'):
            storage.apply_relocation(plan['plan_id'], 'wrong')
        self.note('Later', 'New knowledge after preview')
        self.assertTrue(storage.inspect_relocation(plan['plan_id'])['added'])
        with self.assertRaisesRegex(ValueError, 'Stale source'):
            self.apply(plan)
        self.assertEqual(storage.resolve_root()[0], self.root)

    def test_relocation_destination_conflict_is_preserved(self):
        plan = self.plan()
        dest = Path(plan['destination'])
        dest.mkdir()
        (dest / 'unrelated').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.apply(plan)
        self.assertEqual((dest / 'unrelated').read_text(), 'keep')
        self.assertEqual(storage.resolve_root()[0], self.root)

    def test_interrupted_copy_resumes(self):
        plan = self.plan()
        real = storage.shutil.copyfile
        count = [0]
        def fail(source, target):
            count[0] += 1
            if count[0] == 2:
                Path(target).write_bytes(b'partial')
                raise OSError('interrupted copy')
            return real(source, target)
        with patch.object(storage.shutil, 'copyfile', fail):
            with self.assertRaises(OSError):
                self.apply(plan)
        self.assertEqual(storage.resolve_root()[0], self.root)
        self.assertEqual(self.apply(plan)['status'], 'committed')

    def test_pointer_failure_after_retirement_resumes(self):
        plan = self.plan()
        real = storage._write
        def fail(path, value):
            if path == self.config:
                raise OSError('pointer failure')
            return real(path, value)
        with patch.object(storage, '_write', fail):
            with self.assertRaises(OSError):
                self.apply(plan)
        self.assertTrue((self.root / '.relocated.json').exists())
        self.assertEqual(self.apply(plan)['status'], 'committed')

    def test_empty_domain_directories_survive(self):
        self.configure()
        plan = storage.relocation_plan(self.root, self.base / 'empty relocated')
        self.apply(plan)
        self.assertTrue((Path(plan['destination']) / 'domains/demo/notes').is_dir())

    def test_shared_adapters_capture_and_search(self):
        from hooks import claude_hooks, copilot_hooks, codex_hooks
        from core.cli import main
        from core.notes import capture
        from core.search import search_domain
        from core.runtime import process
        self.configure()
        item = capture('Shared', 'Shared harness knowledge', ['subject/project', 'concern/architecture'], 'demo')
        payload = {'cwd': str(self.base / 'project'), 'session_id': 'a', 'turn_id': 'b', 'prompt': 'Shared'}
        for harness in ('codex', 'claude', 'factory', 'copilot'):
            self.assertIn('Shared', process(harness, 'user-prompt', {**payload, 'session_id': harness})['context'])
        # Shown once per session, not on every prompt about the same thing.
        self.assertNotIn('Shared', process('claude', 'user-prompt', {**payload, 'session_id': 'claude'})['context'])
        self.assertIn('hookSpecificOutput', claude_hooks.handle('session-start', payload))
        self.assertIn('additionalContext', copilot_hooks.handle('sessionStart', {'cwd': payload['cwd'], 'sessionId': 'copilot'}))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            codex_hooks.handle('session-start', payload)
        # The shared store needs no --root: the command is the bare script invocation.
        self.assertIn('CMD = ', output.getvalue())
        self.assertNotIn('--root', output.getvalue())
        self.assertEqual(search_domain('Shared', 'demo')[0]['id'], item['id'])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['doctor', '--cwd', payload['cwd']]), 0)
        self.assertEqual(json.loads(output.getvalue())['location']['selection_source'], 'shared_config')

    def test_subagent_cannot_setup(self):
        from core.cli import main
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['setup', '--location', str(self.base / 'forbidden'), '--actor', 'subagent']), 2)
        self.assertFalse(self.config.exists())

    def test_verification_failure_does_not_publish_pointer(self):
        plan = self.plan()
        real = storage.shutil.copyfile
        def corrupt(source, target):
            real(source, target)
            Path(target).write_bytes(b'corrupted copy')
        with patch.object(storage.shutil, 'copyfile', corrupt):
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                self.apply(plan)
        self.assertEqual(storage.resolve_root()[0], self.root)
        self.assertFalse((self.root / '.relocated.json').exists())

    def test_stale_configuration_blocks_relocation(self):
        plan = self.plan()
        self.config.write_text(json.dumps({'version': 1, 'data_root': str(self.base / 'another')}))
        with self.assertRaisesRegex(ValueError, 'Stale shared configuration'):
            self.apply(plan)

    def test_refresh_after_stale_source_preserves_notes(self):
        self.configure()
        self.note()
        plan = storage.relocation_plan(self.root, self.base / 'refreshed destination')
        (self.root / 'tag_taxonomy.json').write_text(json.dumps({'tags': ['subject/project', 'concern/architecture', 'concern/decision', 'concern/workflow']}))
        self.assertEqual(storage.inspect_relocation(plan['plan_id'])['changed'], ['tag_taxonomy.json'])
        with self.assertRaisesRegex(ValueError, 'Stale source'):
            self.apply(plan)
        refreshed = storage.setup(plan['destination'], True)
        self.assertEqual(refreshed['notes'], plan['notes'])
        self.assertEqual(refreshed['source'], plan['source'])
        self.assertEqual(refreshed['destination'], plan['destination'])
        self.assertEqual(self.apply(refreshed)['status'], 'committed')

    def test_installer_reuses_shared_location_and_requires_unattended_selection(self):
        self.configure()
        self.assertEqual(storage.ensure_setup(), self.root)
        self.config.unlink()
        with patch.object(storage, 'legacy_roots', return_value=[self.root]), patch.object(sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, '--location'):
                storage.ensure_setup()
        self.assertEqual(storage.ensure_setup(self.root), self.root)

    def test_cli_setup_and_parallel_writers_discover_shared_config(self):
        script = Path(__file__).resolve().parents[1] / 'living_memory.py'
        user_home = self.base / 'fresh user'
        user_home.mkdir()
        env = {**os.environ, 'USERPROFILE': str(user_home), 'HOME': str(user_home), 'CODEX_HOME': str(user_home / '.codex')}
        target = self.base / 'cli shared store'
        def run(*args):
            result = subprocess.run([sys.executable, str(script), *args], env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        run('setup', '--location', str(target), '--non-interactive')
        run('bind', 'demo', str(self.base / 'project'))
        processes = []
        for i in range(3):
            cmd = [sys.executable, str(script), 'capture', '--domain', 'demo', '--subject', 'project', '--concern', 'knowledge', f'Parallel {i}', f'Shared store writer {i}']
            processes.append(subprocess.Popen(cmd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for process in processes:
            out, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, error)
        self.assertEqual(len(run('search', 'Shared store', '--domain', 'demo')['results']), 3)
        result = run('doctor', '--cwd', str(self.base / 'project'))
        self.assertEqual(result['location']['selection_source'], 'shared_config')
        self.assertEqual(result['root'], str(target))
