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


class StorageBase(StoreFixture):
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

    def prepared(self):
        self.configure()
        self.note()
        return self.base / 'new shared store'

    def relocate(self, destination=None):
        return storage.relocate(self.root, destination or self.base / 'new shared store')

    def interrupted(self, destination=None):
        """A relocation stopped by a failed copy of its second file."""
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
                self.relocate(destination)
        self.assertEqual(storage.resolve_root()[0], self.root)


class SharedStorage(StorageBase):
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

    def test_prompt_suggests_the_configured_folder_when_it_is_missing(self):
        gone = self.base / 'moved away'
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({'version': 1, 'data_root': str(gone)}), encoding='utf-8')
        err = io.StringIO()
        with patch.object(Path, 'home', return_value=self.base), patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value=''), contextlib.redirect_stderr(err):
            result = storage.setup()
        self.assertEqual(result['root'], str(gone))
        self.assertIn(f'Suggested location: {gone}', err.getvalue())
        self.assertIn('is missing', err.getvalue())

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

    def test_relocation_is_a_verified_copy(self):
        target = self.prepared()
        before = storage._manifest(self.root)
        self.assertEqual(storage.resolve_root()[0], self.root)
        self.assertFalse(target.exists())
        result = self.relocate()
        self.assertEqual((result['status'], result['root'], result['notes'], result['files']), ('committed', str(target), 1, len(before)))
        self.assertIn('Restart', result['next'])
        self.assertEqual(storage.resolve_root()[0], target)
        self.assertEqual(storage._manifest(target), before)
        self.assertTrue((target / 'domains/demo/search.sqlite3').exists())
        self.assertTrue((self.root / 'bindings.json').exists())
        with self.assertRaisesRegex(ValueError, 'retired'):
            with store_lock(self.root):
                pass
        identity = read_json(self.root / '.relocated.json')['relocation_id']
        self.assertEqual(read_json(self.config.parent / 'relocations' / (identity + '.journal.json'))['state'], 'committed')
        self.assertEqual(read_json(self.config.parent / 'relocations' / (identity + '.relocation.json'))['destination'], str(target))
        with self.assertRaisesRegex(ValueError, 'empty destination'):   # a finished relocation is not run again
            self.relocate()

    def test_a_source_change_during_an_interrupted_relocation_does_not_switch(self):
        self.prepared()
        self.interrupted()
        self.note('Later', 'New knowledge after the copy started')
        with self.assertRaisesRegex(ValueError, 'source store changed'):
            self.relocate()
        self.assertEqual(storage.resolve_root()[0], self.root)
        self.assertFalse((self.root / '.relocated.json').exists())

    def test_relocation_destination_conflict_is_preserved(self):
        dest = self.prepared()
        dest.mkdir()
        (dest / 'unrelated').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'empty destination'):
            self.relocate()
        self.assertEqual((dest / 'unrelated').read_text(), 'keep')
        self.assertEqual(storage.resolve_root()[0], self.root)
        (dest / 'unrelated').unlink()
        self.interrupted()
        (dest / 'unrelated').write_text('keep')   # a file the relocation did not copy, found on resume
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.relocate()
        self.assertEqual((dest / 'unrelated').read_text(), 'keep')
        self.assertEqual(storage.resolve_root()[0], self.root)

    def test_interrupted_copy_resumes(self):
        self.prepared()
        self.interrupted()
        self.assertEqual(self.relocate()['status'], 'committed')
        self.assertEqual(len(list((self.config.parent / 'relocations').glob('*.relocation.json'))), 1)   # resumed, not started again

    def test_setup_location_resumes_an_interrupted_relocation(self):
        dest = self.prepared()
        self.interrupted()
        self.assertEqual(storage.setup(dest, True)['status'], 'committed')
        self.assertEqual(storage.resolve_root()[0], dest)
        self.assertTrue((self.root / '.relocated.json').exists())   # the source was retired, so the copy was verified first
        self.assertEqual(len(list((self.config.parent / 'relocations').glob('*.relocation.json'))), 1)

    def test_setup_location_resumes_after_the_source_was_retired(self):
        dest = self.prepared()
        real = storage._write
        def fail(path, value):
            if path == self.config:
                raise OSError('pointer failure')
            return real(path, value)
        with patch.object(storage, '_write', fail), self.assertRaises(OSError):
            storage.setup(dest, True)
        self.assertEqual(storage.setup(dest, True)['status'], 'committed')
        self.assertEqual(read_json(self.config)['data_root'], str(dest))

    def test_setup_does_not_overwrite_a_configuration_changed_after_it_chose_the_store(self):
        dest = self.prepared()
        other = self.base / 'other store'
        from core.migration import migrate_schema
        migrate_schema(other)
        real = storage.resolve_root
        def racing(*args, **kwargs):
            result = real(*args, **kwargs)
            self.config.write_text(json.dumps({'version': 1, 'data_root': str(other)}), encoding='utf-8')   # another setup wins meanwhile
            return result
        with patch.object(storage, 'resolve_root', racing), self.assertRaisesRegex(ValueError, 'configuration changed'):
            storage.setup(dest, True)
        self.assertEqual(read_json(self.config)['data_root'], str(other))

    def test_pointer_failure_after_retirement_resumes(self):
        self.prepared()
        real = storage._write
        def fail(path, value):
            if path == self.config:
                raise OSError('pointer failure')
            return real(path, value)
        with patch.object(storage, '_write', fail):
            with self.assertRaises(OSError):
                self.relocate()
        self.assertTrue((self.root / '.relocated.json').exists())
        self.assertEqual(self.relocate()['status'], 'committed')

    def test_empty_domain_directories_survive(self):
        self.configure()
        result = self.relocate(self.base / 'empty relocated')
        self.assertTrue((Path(result['root']) / 'domains/demo/notes').is_dir())

    def test_shared_adapters_capture_and_search(self):
        from hooks import claude_hooks, copilot_hooks, codex_hooks
        from revision_fill import main
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
        from revision_fill import main
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['setup', '--location', str(self.base / 'forbidden'), '--actor', 'subagent']), 2)
        self.assertFalse(self.config.exists())

    def test_verification_failure_does_not_publish_pointer(self):
        self.prepared()
        real = storage.shutil.copyfile
        def corrupt(source, target):
            real(source, target)
            Path(target).write_bytes(b'corrupted copy')
        with patch.object(storage.shutil, 'copyfile', corrupt):
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                self.relocate()
        self.assertEqual(storage.resolve_root()[0], self.root)
        self.assertFalse((self.root / '.relocated.json').exists())

    def test_stale_configuration_blocks_relocation(self):
        self.prepared()
        self.interrupted()
        self.config.write_text(json.dumps({'version': 1, 'data_root': str(self.base / 'another')}))
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            self.relocate()
        self.assertFalse((self.root / '.relocated.json').exists())

    def test_setup_relocates_directly_when_the_configured_store_differs(self):
        dest = self.prepared()
        result = storage.setup(dest, True)
        self.assertEqual((result['status'], result['root'], result['notes']), ('committed', str(dest), 1))
        self.assertEqual(storage.resolve_root()[0], dest)

    def test_refresh_after_stale_source_preserves_notes(self):
        import shutil
        dest = self.base / 'refreshed destination'
        self.prepared()
        self.interrupted(dest)
        (self.root / 'tag_taxonomy.json').write_text(json.dumps({'tags': ['subject/project', 'concern/architecture', 'concern/decision', 'concern/workflow']}))
        with self.assertRaisesRegex(ValueError, 'source store changed'):
            self.relocate(dest)
        shutil.rmtree(dest)   # what the error says to do
        refreshed = storage.setup(dest, True)
        self.assertEqual((refreshed['status'], refreshed['notes'], refreshed['root']), ('committed', 1, str(dest)))
        self.assertIn('concern/workflow', (dest / 'tag_taxonomy.json').read_text())

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


class MaintenanceRelocation(StorageBase):
    def setUp(self):
        from datetime import datetime, timezone
        super().setUp()
        from core import maintenance_state as ms
        self.ms, self.T = ms, datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
        self.cfg = ms.validate({'enabled': True, 'domains': ['demo']})
        ms.set_config(self.root, self.cfg)
        self.configure()
        self.note()

    def finished_run(self):
        run = self.ms.reserve(self.root, self.cfg, 3, self.T)
        self.ms.launch(self.root, run, self.T)
        return self.ms.finish(self.root, run, 1000, 'completed', self.T)

    def test_the_lock_is_not_copied_and_the_records_are(self):
        run = self.finished_run()
        self.ms.running(self.root)
        self.assertTrue((self.root / 'maintenance/worker.lock').exists())
        self.assertFalse([k for k in storage._manifest(self.root) if k.endswith('worker.lock')])
        dest = Path(self.relocate()['root'])
        self.assertFalse([k for k in storage._manifest(dest) if k.endswith('worker.lock')])
        self.assertTrue((dest / f"maintenance/runs/{run['day']}/{run['id']}.json").is_file())
        self.assertEqual(self.ms.accounting(dest, self.T)[0]['reviewed'], 3)

    def test_the_destination_is_disabled_and_keeps_its_settings(self):
        self.ms.set_config(self.root, dict(self.cfg, model='haiku'))
        result = self.relocate()
        cfg = self.ms.config(Path(result['root']))
        self.assertEqual((cfg['enabled'], cfg['model'], cfg['domains']), (False, 'haiku', ['demo']))
        self.assertTrue(self.ms.config(self.root)['enabled'])   # the retired source is untouched
        self.assertEqual(result['status'], 'committed')

    def test_a_bom_prefixed_enabled_config_is_disabled_at_the_destination(self):
        path = self.root / 'maintenance.json'
        path.write_bytes(bytes([0xef, 0xbb, 0xbf]) + path.read_bytes())
        self.assertTrue(self.ms.config(self.root)['enabled'])
        self.assertFalse(self.ms.config(Path(self.relocate()['root']))['enabled'])

    def test_the_destination_config_must_be_the_source_one_disabled(self):
        from core.repository import canonical_json
        dest = self.base / 'dest-check'
        (dest).mkdir()
        planned = storage._hash(self.root / 'maintenance.json')
        (dest / 'maintenance.json').write_text(canonical_json(dict(self.cfg, enabled=False)))
        self.assertTrue(storage._maintenance_matches(self.root, dest, planned))
        (dest / 'maintenance.json').write_text(canonical_json(dict(self.cfg, enabled=False, daily_budget_usd='9.00')))
        self.assertFalse(storage._maintenance_matches(self.root, dest, planned))
        (dest / 'maintenance.json').write_text('{broken')
        self.assertFalse(storage._maintenance_matches(self.root, dest, planned))

    def test_an_unfinished_run_refuses_the_relocation(self):
        self.ms.reserve(self.root, self.cfg, 3, self.T)
        with self.assertRaisesRegex(ValueError, 'maintenance'):
            self.relocate()
        self.assertEqual(storage.resolve_root()[0], self.root)

    def test_a_held_worker_lock_refuses_the_relocation(self):
        with self.ms.worker_lock(self.root):
            with self.assertRaisesRegex(ValueError, 'maintenance'):
                self.relocate()
        self.assertEqual(self.relocate()['status'], 'committed')

    def test_an_old_worker_is_refused_after_relocation(self):
        from core import maintenance
        self.relocate()
        with self.assertRaisesRegex(ValueError, 'Store retired'):
            self.ms.reserve(self.root, self.cfg, 3, self.T)
        with self.assertRaisesRegex(ValueError, 'Store retired'):
            self.ms.set_retry(self.root, 'demo:a', 'r', 'f', 'kept', self.T)
        with self.assertRaisesRegex(ValueError, 'Store retired'):
            maintenance.run(self.root, dry_run=True, now=self.T)
        self.assertEqual(list((self.root / 'maintenance').glob('runs/*/*.json')), [])
