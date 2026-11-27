"""Regression tests for the 8 Codex review findings on Phases 1 to 4."""
from __future__ import annotations
import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from test_core import StoreFixture


class CodexReviewFixTests(StoreFixture):
    def _cli(self, *argv, cwd=None):
        from core.cli import main
        out, err = io.StringIO(), io.StringIO()
        args = ['--root', str(self.root), '--json']
        if cwd is not None:
            args += ['--cwd', str(cwd)]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args + list(argv))
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_sync_persists_conflict_copy_before_overwriting_vault(self):
        from core import sync as sync_mod
        from core.notes import change
        vault = self.base / 'vault'
        n = self.note('Conflict note', 'Original body.')
        sync_mod.sync('demo', vault, self.root)
        # Edit both vault copy and canonical note to create a conflict (without triggering an intermediate overlay sync during change()).
        state = json.loads((self.root / 'domains' / 'demo' / 'sync-state.json').read_text(encoding='utf-8'))
        overlay_rel = state['entries'][n['id']]['overlay']
        vault_file = vault / overlay_rel
        with patch('core.sync.sync'):
            change(n['id'], 'demo', 'update', self.root, body='Canonical edit body.')
        vault_file.write_text(vault_file.read_text(encoding='utf-8').replace('Original body.', 'Vault edit body.'), encoding='utf-8')

        real_tx = sync_mod.transaction
        def fail_overlay_tx(target_root, changes, op_name, *args, **kwargs):
            if op_name == 'overlay_sync':
                raise OSError('simulated crash before vault overwrite')
            return real_tx(target_root, changes, op_name, *args, **kwargs)

        with patch.object(sync_mod, 'transaction', side_effect=fail_overlay_tx):
            with self.assertRaises(OSError):
                sync_mod.sync('demo', vault, self.root)

        staged_dir = self.root / 'domains' / 'demo' / 'staged'
        staged_files = list(staged_dir.glob('*.md')) if staged_dir.is_dir() else []
        self.assertTrue(staged_files, 'conflict copy must be persisted before vault overwrite')
        self.assertIn('Vault edit body.', staged_files[0].read_text(encoding='utf-8'))
        # Vault still has user edit and sync-state has not claimed the conflict was published yet.
        self.assertIn('Vault edit body.', vault_file.read_text(encoding='utf-8'))

    def test_legacy_pending_migration_preserves_markers_on_conflict(self):
        from core import repository as repo
        ops = self.root / '.operations'
        pending_dir = ops / repo.PENDING_DIR
        if pending_dir.exists():
            for child in pending_dir.iterdir():
                child.unlink()
            pending_dir.rmdir()
        # Create a legacy pending operation whose journal fails recovery (corrupt).
        op_id = 'legacy-op-1'
        op_dir = ops / op_id
        op_dir.mkdir(parents=True, exist_ok=True)
        (op_dir / 'journal.json').write_text('{corrupt json', encoding='utf-8')
        (op_dir / repo.PENDING).write_text('', encoding='utf-8')

        first = repo.roll_forward_pending(self.root)
        self.assertEqual(len(first), 1)
        self.assertFalse(first[0]['ok'])
        self.assertTrue((pending_dir / op_id).is_file())

        # Second call must still discover the unrecovered legacy operation via .pending/.
        second = repo.roll_forward_pending(self.root)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]['operation_id'], op_id)
        self.assertFalse(second[0]['ok'])

    def test_cross_domain_capture_handles_stale_index_deleted_and_archived(self):
        from core.init_domain import init_domain
        from core.notes import capture, change
        from core.search import rebuild_index
        init_domain('other', [str(self.base / 'other')], self.root)
        # Capture two notes in `other` and index them.
        deleted = capture('Deleted cross note', 'Exact body text.', ['subject/project', 'concern/architecture'], 'other', self.root)
        archived = capture('Archived cross note', 'Archived body text.', ['subject/project', 'concern/architecture'], 'other', self.root)
        rebuild_index('other', self.root)
        # Delete one file directly and archive the other without updating search.sqlite3, and clear .derived-dirty to simulate stale index.
        Path(deleted['path']).unlink()
        change(archived['id'], 'other', 'archive', self.root)
        (self.root / 'domains' / 'other' / '.derived-dirty').unlink(missing_ok=True)

        # Capturing matching titles in `demo` must not crash on the deleted note or treat the archived note as an active duplicate.
        r1 = capture('Deleted cross note', 'Exact body text.', ['subject/project', 'concern/architecture'], 'demo', self.root)
        self.assertEqual(r1['status'], 'created')
        r2 = capture('Archived cross note', 'Archived body text.', ['subject/project', 'concern/architecture'], 'demo', self.root)
        self.assertEqual(r2['status'], 'created')

    def test_find_note_index_rejects_symlink_or_unconfined_path(self):
        from core.notes import _find_note_index
        from core.search import rebuild_index
        n = self.note('Confined note', 'Body text.')
        rebuild_index('demo', self.root)
        # Normal indexed lookup succeeds.
        hit = _find_note_index(n['id'], 'demo', self.root)
        self.assertIsNotNone(hit)
        # If any path component is a symlink/junction, the index fast path must refuse it.
        with patch('core.repository.is_link_or_junction', return_value=True):
            self.assertIsNone(_find_note_index(n['id'], 'demo', self.root))

    def test_large_store_backup_and_restore_with_cap_override(self):
        from core import backup as backup_mod
        self.note('Large note', 'x' * 4000)
        with patch.object(backup_mod, 'MAX_MEMBER_BYTES', 512), patch.object(backup_mod, 'MAX_TOTAL_BYTES', 2048):
            # backup() on a valid local store exceeding default caps still succeeds.
            res = backup_mod.backup(self.root)
            self.assertTrue(res['ok'])
            snap = Path(res['backup'])
            # Default untrusted restore refuses over-cap archive and tells user the env var.
            with self.assertRaisesRegex(ValueError, 'LIVING_MEMORY_BACKUP_MAX_MEMBER_BYTES'):
                backup_mod.restore_backup(self.root, snap)
            # With explicit env var override, restore succeeds.
            with patch.dict(os.environ, {'LIVING_MEMORY_BACKUP_MAX_MEMBER_BYTES': '1048576', 'LIVING_MEMORY_BACKUP_MAX_TOTAL_BYTES': '4194304'}):
                restored = backup_mod.restore_backup(self.root, snap)
                self.assertTrue(restored['ok'])

    def test_search_query_all_domains_from_unbound_dir_includes_unbound_domain(self):
        from core.init_domain import init_domain
        from core.notes import capture
        unbound_dir = self.base / 'nowhere'
        unbound_dir.mkdir()
        # Bind temporarily to capture a valid note, then drop from bindings.json so it is an unbound domain folder on disk.
        init_domain('orphan', [str(self.base / 'orphan_proj')], self.root)
        capture('Quasar telemetry note', 'Unique quasar telemetry payload.', ['subject/project', 'concern/architecture'], 'orphan', self.root)
        bindings_path = self.root / 'bindings.json'
        bindings = json.loads(bindings_path.read_text(encoding='utf-8'))
        bindings['domains'].pop('orphan', None)
        bindings_path.write_text(json.dumps(bindings), encoding='utf-8')

        code, res = self._cli('search', 'quasar telemetry', '--all-domains', cwd=unbound_dir)
        self.assertEqual(code, 0, res)
        self.assertIn('orphan', res['domains'])
        self.assertTrue(any(h['domain'] == 'orphan' for h in res['results']))

    def test_capture_finalize_resolves_bare_ref_via_env_session_domain(self):
        from core.runtime import _update
        n = self.note('Session ref note', 'Pinned session note body.')
        sid = 'env-session-42'
        _update(self.root, sid, lambda s: s.update(domain='demo', turn='turn-1'))
        unbound_dir = self.base / 'unbound_cwd'
        unbound_dir.mkdir()
        from core.capture import status as capture_status
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': sid}):
            code, res = self._cli('capture-finalize', '--note', n['id'][:8], cwd=unbound_dir)
        self.assertEqual(code, 0, res)
        self.assertEqual(res.get('status'), 'saved')
        self.assertIn(f"demo:{n['id']}", capture_status(sid, 'turn-1', self.root).get('notes', []))

    def test_semantic_ascii_normalizer_strips_formfeed_and_vtab(self):
        from core.semantic import Model
        m = Model.__new__(Model)
        m.clean = True
        m.lower = True
        m.strip = True
        m.chinese = False
        # Compare ASCII fast path with Unicode path (by appending a non-ASCII char and stripping it).
        sample = 'Alpha\fBeta\x0bGamma\tDelta\nEpsilon\rZeta'
        ascii_out = m._normalize(sample)
        unicode_out = m._normalize(sample + '\u00e9')[:-1]
        self.assertEqual(ascii_out, 'alphabetagamma delta epsilon zeta')
        self.assertEqual(ascii_out, unicode_out)


if __name__ == '__main__':
    unittest.main()
