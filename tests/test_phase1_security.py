"""Regression tests for ALM Phase 1 security and governance fixes (REL-S1 through REL-S6)."""
import io
import json
import os
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from test_core import StoreFixture
from core import backup, bundle, connectors, paths, repository, runtime, semantic, storage, views
from core.cli import main as cli_main


class Phase1SecurityTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.project = self.base / 'project'
        self.project.mkdir(exist_ok=True)
        self.unbound = self.base / 'unbound-dir'
        self.unbound.mkdir()
        self.cmd_prefix = f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo'

    def _pre(self, tool_name, tool_input, cwd=None, session='s1', **extra):
        payload = {'cwd': str(cwd or self.project), 'session_id': session, 'tool_name': tool_name, 'tool_input': tool_input, **extra}
        return runtime.process('claude', 'pre-tool-use', payload, self.root)

    # ---- REL-S1: PreToolUse guards before off and unbound returns ----

    def test_rel_s1_protected_rule_denies_write_in_unbound_directory(self):
        target = str(self.root / 'bindings.json')
        res = self._pre('Write', {'file_path': target}, cwd=self.unbound)
        self.assertIn('deny', res)
        note_target = str(self.root / 'domains' / 'demo' / 'notes' / 'hack.md')
        res2 = self._pre('Edit', {'file_path': note_target}, cwd=self.unbound)
        self.assertIn('deny', res2)

    def test_rel_s1_off_mode_still_enforces_hivemind_and_subagent_guards(self):
        with patch.dict(os.environ, {'LIVING_MEMORY_OFF': '1', 'HIVEMIND_SESSION': '1'}):
            res = self._pre('Bash', {'command': f'{self.cmd_prefix} delete demo:12345678'})
            self.assertIn('deny', res)
        with patch.dict(os.environ, {'LIVING_MEMORY_OFF': '1'}):
            res2 = self._pre('Bash', {'command': f'{self.cmd_prefix} capture --title x --summary y'}, agent_id='sub-1')
            self.assertIn('deny', res2)

    def test_rel_s1_unbound_subagent_denied_remote_sync(self):
        res = self._pre('Bash', {'command': f'{self.cmd_prefix} source-sync --all-remote'}, cwd=self.unbound, agent_id='sub-1')
        self.assertIn('deny', res)

    # ---- REL-S2: Shell parser, mid-word @, # comments, G1, chained subagent, config set, maintenance paid ----

    def test_rel_s2_mid_word_at_in_ref_revision_parses_and_blocks_subagent(self):
        cmd = f'{self.cmd_prefix} supersede demo:12345678 --expected-revision aaaaaaaaaaaa --by demo:87654321@bbbbbbbbbbbb'
        for tool in ('Bash', 'PowerShell', 'run_shell_command'):
            self.assertEqual(runtime.memory_subcommand(cmd, runtime.shell_grammar(tool.lower())), 'supersede', tool)
            main_res = self._pre(tool, {'command': cmd})
            self.assertIn('allow', main_res, tool)
            sub_res = self._pre(tool, {'command': cmd}, agent_id='sub-1')
            self.assertIn('deny', sub_res, tool)
        # PowerShell splatting at word start is still rejected
        splat = f'{self.cmd_prefix} search @params'
        self.assertIsNone(runtime.memory_subcommand(splat, 'powershell'))
        self.assertIsNone(runtime.memory_subcommand(splat, None))

    def test_rel_s2_hash_comment_cannot_spoof_readonly_flags(self):
        for cmd in (f'{self.cmd_prefix} rebaseline # --dry-run', f'{self.cmd_prefix} wiki # --format json'):
            self.assertIsNone(runtime.memory_subcommand(cmd, 'posix'))
            self.assertIsNone(runtime.memory_subcommand(cmd, 'powershell'))
            self.assertIn('deny', self._pre('Bash', {'command': cmd}, agent_id='sub-1'))
            self.assertNotIn('allow', self._pre('Bash', {'command': cmd}))

    def test_rel_s2_abbreviated_global_options_do_not_bypass_g1_checkers(self):
        base = f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}"'
        self.assertTrue(runtime.views_write(f'{base} --dom views views add demo:12345678', 'posix'))
        self.assertTrue(runtime.wiki_build(f'{base} --dom wiki wiki --agent --build', 'posix'))
        self.assertTrue(runtime.maintenance_write(f'{base} --dom maintenance maintenance enable', 'posix'))
        self.assertTrue(runtime.maintenance_paid(f'{base} --dom maintenance maintenance run', 'posix'))
        self.assertTrue(runtime.rebaseline_write(f'{base} --dom rebaseline rebaseline', 'posix'))
        self.assertTrue(runtime.proposals_write(f'{base} --dom proposals proposals accept abcdef12', 'posix'))
        self.assertTrue(runtime.taxonomy_write(f'{base} --dom taxonomy taxonomy add subject/x', 'posix'))

    def test_rel_s2_chained_subagent_write_is_denied(self):
        cmd = f'echo ok && {self.cmd_prefix} capture --title x --summary y'
        self.assertIn('deny', self._pre('Bash', {'command': cmd}, agent_id='sub-1'))

    def test_rel_s2_config_set_denied_to_subagent_and_hivemind(self):
        cmd = f'{self.cmd_prefix} config set auto_resolve false'
        self.assertIn('deny', self._pre('Bash', {'command': cmd}, agent_id='sub-1'))
        with patch.dict(os.environ, {'HIVEMIND_SESSION': '1'}):
            self.assertIn('deny', self._pre('Bash', {'command': cmd}))
        get_cmd = f'{self.cmd_prefix} config get'
        self.assertNotIn('deny', self._pre('Bash', {'command': get_cmd}, agent_id='sub-1'))

    def test_rel_s2_spec_file_confined_in_auto_allowed(self):
        outside = self.base / 'outside.json'
        outside.write_text('{}', encoding='utf-8')
        cmd = f'{self.cmd_prefix} views add --title q --spec-file "{outside}"'
        self.assertFalse(runtime.auto_allowed(cmd, 'posix', self.root, self.project))

    def test_rel_s2_maintenance_enable_and_run_require_prompt_not_auto_allowed(self):
        for action in ('enable', 'run'):
            res = self._pre('Bash', {'command': f'{self.cmd_prefix} maintenance {action}'})
            self.assertNotIn('allow', res, action)
            self.assertNotIn('deny', res, action)
        for action in ('status', 'preview', 'history', 'disable'):
            res = self._pre('Bash', {'command': f'{self.cmd_prefix} maintenance {action}'})
            self.assertIn('allow', res, action)

    # ---- REL-S3: Protected store internals + macOS is_within ----

    def test_rel_s3_operations_backups_and_turns_are_protected(self):
        for rel in ('.operations/op1/journal.json', 'backups/snap.zip', '.turns/abc.json'):
            res = self._pre('Write', {'file_path': str(self.root / rel)})
            self.assertIn('deny', res, rel)

    def test_rel_s3_views_and_bundle_use_is_within_on_macos(self):
        with patch.object(paths.sys, 'platform', 'darwin'):
            mixed_view = Path(str(self.root).upper()) / 'domains' / 'demo' / 'out.html'
            with self.assertRaises(ValueError):
                views._out_path(self.root, mixed_view)
            mixed_bundle = Path(str(self.root).upper()) / 'domains' / 'demo' / 'b.zip'
            with self.assertRaises(ValueError):
                bundle.export(self.root, 'demo', mixed_bundle)

    # ---- REL-S4: P1 ZIP case collision, decompression caps, P2 NTFS junction, _PARSED deepcopy ----

    def test_rel_s4_backup_rejects_case_colliding_members_and_oversized_member(self):
        snap = self.base / 'collide.zip'
        with zipfile.ZipFile(snap, 'w') as zf:
            zf.writestr('domains/demo/notes/A.md', '---\nid: "1"\n---\nhi\n')
            zf.writestr('domains/demo/notes/a.md', '---\nid: "2"\n---\nhi\n')
        with self.assertRaisesRegex(ValueError, 'name the same file'):
            backup.verify(snap)

        bomb = self.base / 'bomb.zip'
        with zipfile.ZipFile(bomb, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('bindings.json', b' ' * 2048)
        with patch.object(backup, 'MAX_MEMBER_BYTES', 512):
            with self.assertRaisesRegex(ValueError, 'size cap'):
                backup.verify(bomb)

    def test_rel_s4_bundle_rejects_case_collision_and_actual_decompressed_overflow(self):
        bpath = self.base / 'b.zip'
        with zipfile.ZipFile(bpath, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('notes/A.md', 'a')
            zf.writestr('notes/a.md', 'b')
        with self.assertRaisesRegex(ValueError, 'name the same file'):
            bundle._read(bpath)

        big = self.base / 'big.zip'
        with zipfile.ZipFile(big, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('manifest.json', '{}')
            zf.writestr('notes/a.md', b'x' * 2048)
        with patch.object(bundle, 'MAX_MEMBER', 512):
            with self.assertRaisesRegex(ValueError, 'too large'):
                bundle._read(big)

    def test_rel_s4_repository_uses_is_link_or_junction_and_deepcopies_parsed(self):
        note_dir = self.root / 'domains' / 'demo' / 'notes'
        note_dir.mkdir(parents=True, exist_ok=True)
        with patch.object(repository, 'is_link_or_junction', return_value=True):
            with self.assertRaisesRegex(ValueError, 'Symbolic links'):
                repository.confined(self.root, 'domains/demo/notes/x.md')
        # Deepcopy test: mutating nested list/dict on returned note must not pollute cache
        p = note_dir / 'KNOWLEDGE - test-11111111.md'
        p.write_text('---\nid: "11111111-1111-1111-1111-111111111111"\ntitle: "Test"\ntype: "knowledge"\nstatus: "active"\ntags: ["subject/project", "concern/knowledge"]\nsources: [{"path": "a.py", "sha256": "' + '0'*64 + '"}]\n---\nBody\n', encoding='utf-8')
        # Backdate mtime past SETTLE_NS so _PARSED caches it
        old_ts = os.path.getmtime(p) - 10
        os.utime(p, (old_ts, old_ts))
        first = repository.inventory('demo', self.root)[0]
        first['sources'][0]['path'] = 'MUTATED'
        second = repository.inventory('demo', self.root)[0]
        self.assertEqual(second['sources'][0]['path'], 'a.py')

    # ---- REL-S5: ingest_run.py hardening and digest_refresh disabled on restore/relocate ----

    def test_rel_s5_ingest_run_flags_trust_boundary_and_stderr_redaction(self):
        import importlib.util
        script = Path(runtime.__file__).resolve().parents[1] / 'scripts' / 'ingest_run.py'
        spec = importlib.util.spec_from_file_location('ingest_run_s5', script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn('untrusted data', mod.PROMPT)
        ns = unittest.mock.MagicMock(command_json=None, model='haiku', allowed_tools=['Read'], budget_usd=1.5)
        argv, _ = mod.agent_argv(ns, 'hello', remaining_budget=0.75)
        for flag in ('--strict-mcp-config', '--no-session-persistence', '--disable-slash-commands', '--max-budget-usd'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--max-budget-usd') + 1], '0.7500')

    def test_rel_s5_restore_backup_disables_digest_refresh(self):
        bpath = self.root / 'bindings.json'
        cfg = json.loads(bpath.read_text(encoding='utf-8'))
        cfg['digest_refresh'] = {'enabled': True, 'daily_usd': 1.0, 'max_batches': 2, 'model': 'haiku'}
        bpath.write_text(json.dumps(cfg), encoding='utf-8')
        snap = backup.backup(self.root)['backup']
        backup.restore_backup(self.root, snap)
        restored = json.loads(bpath.read_text(encoding='utf-8'))
        self.assertFalse(restored['digest_refresh']['enabled'])

    def test_rel_s5_relocate_disables_digest_refresh(self):
        bpath = self.root / 'bindings.json'
        cfg = json.loads(bpath.read_text(encoding='utf-8'))
        cfg['digest_refresh'] = {'enabled': True, 'daily_usd': 1.0, 'max_batches': 2, 'model': 'haiku'}
        bpath.write_text(json.dumps(cfg), encoding='utf-8')
        cfg_file = self.base / 'cfg' / 'config.json'
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(json.dumps({'version': 1, 'data_root': str(self.root)}), encoding='utf-8')
        dest = self.base / 'relocated-store'
        with patch.object(storage, 'config_path', return_value=cfg_file):
            res = storage.setup(str(dest), non_interactive=True)
        self.assertEqual(res['status'], 'committed')
        relocated_cfg = json.loads((dest / 'bindings.json').read_text(encoding='utf-8'))
        self.assertFalse(relocated_cfg['digest_refresh']['enabled'])

    # ---- REL-S6: connectors, views domain validation, semantic HTTPS redirect, batch recursion ----

    def test_rel_s6_connectors_http_localhost_opt_in_and_safe_unlink(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LIVING_MEMORY_ALLOW_HTTP_LOCALHOST', None)
            with self.assertRaisesRegex(ValueError, 'must use https'):
                connectors._base_url('http://127.0.0.1:8000')
        outside = self.base / 'keep.txt'
        outside.write_text('keep', encoding='utf-8')
        folder = self.base / 'cache'
        folder.mkdir()
        connectors._safe_cache_unlink(folder, '../keep.txt')
        self.assertTrue(outside.exists())

    def test_rel_s6_views_rejects_traversal_domain(self):
        (self.root / 'views.json').write_text(json.dumps({'version': 2, 'views': [{'kind': 'note', 'note': '12345678', 'domain': '../escape'}]}), encoding='utf-8')
        with self.assertRaises(ValueError):
            views.load(self.root)

    def test_rel_s6_semantic_refuses_http_url(self):
        with self.assertRaisesRegex(ValueError, 'https'):
            semantic._fetch('http://example.com/model.bin', self.base / 'm.bin')

    def test_rel_s6_batch_rejects_nested_batch_with_leading_flags(self):
        payload = json.dumps([{'argv': ['--json', 'batch']}])
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdin', io.StringIO(payload)), patch('sys.stdout', out), patch('sys.stderr', err):
            code = cli_main(['--json', '--root', str(self.root), '--domain', 'demo', 'batch'])
        self.assertNotEqual(code, 0)
        self.assertIn('Invalid batch command', err.getvalue())


if __name__ == '__main__':
    unittest.main()
