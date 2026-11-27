"""Regression tests for Phase 2 efficiency and indexing fixes (REL-P1 through REL-P7)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from test_core import StoreFixture
from core import activity, commands, common, digests, maps, notes, repository, runtime, search, session_start, sync, type_registry, usage, worklist


TAGS = ['subject/project', 'concern/architecture']


class Phase2Efficiency(StoreFixture):
    def test_p1_sync_writes_no_overlay_state_journal_and_prunes_legacy(self):
        vault = self.base / 'vault'
        self.note('Vault note', 'Synced to Obsidian.')
        sync.build_overlay('demo', vault, self.root)
        # No overlay_state journal should be created by sync().
        ops_dir = self.root / '.operations'
        kinds = []
        if ops_dir.is_dir():
            for d in ops_dir.iterdir():
                jp = d / 'journal.json'
                if jp.is_file():
                    kinds.append(repository.read_json(jp, {}).get('kind'))
        self.assertNotIn('overlay_state', kinds)
        self.assertTrue((self.root / 'domains' / 'demo' / 'sync-state.json').is_file())
        # Legacy overlay_state journals are pruned immediately regardless of age.
        legacy_op = repository.transaction(self.root, {'domains/demo/.dummy': 'x\n'}, 'overlay_state')['operation_id']
        self.assertTrue((ops_dir / legacy_op / 'journal.json').is_file())
        removed = repository.prune_journals(self.root)
        self.assertGreaterEqual(removed, 1)
        self.assertFalse((ops_dir / legacy_op).exists())

    def test_p1_pending_markers_live_in_dot_pending_with_legacy_fallback(self):
        n = self.note('Pending test', 'Body text.')
        self.assertTrue((self.root / '.operations' / '.pending').is_dir())
        self.assertEqual(list((self.root / '.operations' / '.pending').iterdir()), [])
        # Legacy .operations/<id>/pending migration works when .pending dir is absent.
        sub = self.base / 'legacy_store'
        sub.mkdir()
        op = repository.transaction(sub, {'a.txt': 'hello\n'}, 'test_write')['operation_id']
        # Remove .pending dir and plant a legacy marker for an unfinished journal.
        import shutil
        shutil.rmtree(sub / '.operations' / '.pending', ignore_errors=True)
        jp = sub / '.operations' / op / 'journal.json'
        data = repository.read_json(jp)
        data['state'] = 'prepared'
        jp.write_text(repository.canonical_json(data), encoding='utf-8')
        (sub / '.operations' / op / 'pending').write_text('pending\n', encoding='utf-8')
        results = repository.roll_forward_pending(sub)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['ok'])
        self.assertFalse((sub / '.operations' / op / 'pending').exists())
        self.assertTrue((sub / '.operations' / '.pending').is_dir())

    def test_p2_search_domain_readonly_skips_inventory_and_store_lock(self):
        self.note('Cache eviction policy', 'Use LRU eviction for bounded memory caches.')
        with patch.object(search, 'inventory', side_effect=AssertionError('inventory must not run on ensure_fresh=False')), \
             patch.object(search, 'store_lock', side_effect=AssertionError('store_lock must not run on ensure_fresh=False')):
            hits = search.search_domain('LRU eviction cache', 'demo', root=self.root, ensure_fresh=False)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['title'], 'Cache eviction policy')

    def test_p2_index_v5_tables_and_wal_inplace_update(self):
        a = self.note('Build script', 'Run `pytest -q` for tests.', files=['src/app.py'])
        db_path = self.root / 'domains' / 'demo' / 'search.sqlite3'
        db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 5)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'note_files', 'note_commands'} <= tables)
            self.assertEqual(db.execute('SELECT path_cf FROM note_files WHERE note_id=?', (a['id'],)).fetchall(), [('src/app.py',)])
            self.assertEqual(db.execute('SELECT program FROM note_commands WHERE note_id=?', (a['id'],)).fetchall(), [('pytest',)])
        finally:
            db.close()
        # Ignored commands skip opening search.sqlite3 entirely.
        with patch.object(commands.sqlite3, 'connect', side_effect=AssertionError('should not open sqlite for ls')):
            self.assertEqual(commands.command_notes('ls -la', ['demo'], self.root), [])
        # Matching command finds the note via note_commands.
        found = commands.command_notes('pytest -v', ['demo'], self.root)
        self.assertEqual([h['id'] for h in found], [a['id']])

    def test_p2_find_note_fast_path_skips_inventory(self):
        a = self.note('Fast lookup', 'Body for fast lookup.')
        with patch.object(notes, 'inventory', side_effect=AssertionError('find_note should hit search.sqlite3')):
            path, found = notes.find_note(a['id'], 'demo', self.root)
            self.assertEqual(found['id'], a['id'])
            path2, found2 = notes.find_note(a['id'][:8], 'demo', self.root)
            self.assertEqual(found2['id'], a['id'])

    def test_p3_lazy_imports_keep_semantic_and_runtime_lean(self):
        # Importing core.semantic must not eagerly import urllib.request or shutil.
        code = (
            "import sys; import core.semantic; "
            "assert 'urllib.request' not in sys.modules, 'urllib.request imported eagerly'; "
            "assert 'shutil' not in sys.modules, 'shutil imported eagerly'; "
            "import core.runtime; "
            "assert 'core.search' not in sys.modules, 'core.search imported eagerly by runtime'; "
            "assert 'sqlite3' not in sys.modules, 'sqlite3 imported eagerly by runtime'"
        )
        res = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_p3_antigravity_short_circuits_without_loading_runtime(self):
        code = (
            "import sys; from hooks import antigravity_hooks as ag; "
            "base = {'conversationId': 'c1', 'artifactDirectoryPath': '/x/antigravity-cli/brain/c1', 'cwd': '/tmp'}; "
            "assert ag.handle('PreInvocation', {**base, 'hookEventName': 'PreInvocation', 'invocationNum': 2, 'initialNumSteps': 5}) == {}; "
            "assert ag.handle('PreToolUse', {**base, 'hookEventName': 'PreToolUse', 'toolCall': {'name': 'update_steps', 'args': {}}}) == {'decision': 'allow'}; "
            "assert 'core.runtime' not in sys.modules, 'core.runtime was imported on short-circuit'"
        )
        res = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_p4_coverage_line_caches_healthy_check_daily(self):
        marker = self.root / '.cache' / 'coverage-told'
        self.assertFalse(marker.exists())
        lines = session_start._coverage_line('demo', self.root, 'sess-1')
        self.assertEqual(lines, [])
        self.assertTrue(marker.exists())
        with patch('core.usage.capture_coverage', side_effect=AssertionError('should not re-read ledger same day')):
            self.assertEqual(session_start._coverage_line('demo', self.root, 'sess-2'), [])

    def test_p4_worklist_preslices_before_maintenance(self):
        for i in range(5):
            self.note(f'Unconfirmed belief number {i}', 'Root cause unconfirmed.', type='bug', allow_similar=True)
        calls = []
        real_maint = worklist._maintenance
        def spy(*a, **k):
            calls.append(a[0]['id'])
            return real_maint(*a, **k)
        with patch.object(worklist, '_maintenance', spy):
            res = worklist.worklist(self.root, ['demo'], kinds=['warned'], limit=2)
        self.assertLessEqual(len(calls), 2)
        # Cheap mode skips _maintenance entirely.
        calls.clear()
        with patch.object(worklist, '_maintenance', spy):
            worklist.worklist(self.root, ['demo'], kinds=['warned'], limit=2, cheap=True)
        self.assertEqual(calls, [])

    def test_p5_record_many_and_settle_signals_batch_writes(self):
        usage.record_many(self.root, [('inject', ['demo:a'], 'prompt', None), ('surface', ['demo:b'], 'prompt', None)], 'sess-1')
        rows = usage.events(self.root, days=1)
        self.assertEqual([r['e'] for r in rows], ['inject', 'surface'])
        # settle_signals writes all hashes in one read/write.
        _, hashed = runtime.signals_seen(self.root, 'demo')
        h1, h2 = hashed('always use tabs'), hashed('never force push')
        runtime.settle_signals(self.root, 'demo', [h1, h2])
        raised, _ = runtime.signals_seen(self.root, 'demo')
        self.assertTrue(raised('always use tabs'))
        self.assertTrue(raised('never force push'))

    def test_p7_search_trims_debug_keys_without_explain(self):
        self.note('Postgres pool size', 'Set pool size to 20 connections.')
        script = Path(__file__).resolve().parents[1] / 'living_memory.py'
        r1 = subprocess.run([sys.executable, str(script), '--json', '--root', str(self.root), '--domain', 'demo', 'search', 'Postgres pool size'], capture_output=True, text=True)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        hit = json.loads(r1.stdout)['results'][0]
        for kept in ('id', 'domain', 'title', 'type', 'updated', 'tags', 'status', 'snippet', 'score', 'rank', 'engine', 'revision', 'sourceField', 'sourceScope', 'relevance'):
            self.assertIn(kept, hit)
        for trimmed in ('coverage', 'titleOverlap', 'titleTerms', 'queryTerms', 'backend', 'gate'):
            self.assertNotIn(trimmed, hit)
        r2 = subprocess.run([sys.executable, str(script), '--json', '--root', str(self.root), '--domain', 'demo', 'search', 'Postgres pool size', '--explain'], capture_output=True, text=True)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        hit2 = json.loads(r2.stdout)['results'][0]
        self.assertIn('coverage', hit2)
        self.assertIn('backend', hit2)


if __name__ == '__main__':
    unittest.main()
