"""PERF-3: a write updates only the changed notes' search rows and hands the Obsidian overlay to a background worker."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from test_core import StoreFixture
from core import search, sync
from core.common import file_lock
from core.notes import change
from core.repository import reconcile
from core.search import rebuild_index, update_index, search_domain


def snapshot(root, domain='demo'):
    """The index contents a reader can see, without rowids (which differ between a rebuild and an update)."""
    db = sqlite3.connect(Path(root) / 'domains' / domain / 'search.sqlite3')
    try:
        notes = sorted(db.execute('SELECT * FROM notes').fetchall())
        fts = sorted(db.execute('SELECT n.id,f.title,f.tags,f.body FROM notes_fts f JOIN notes n ON n.rowid=f.rowid').fetchall())
        vocab = dict(db.execute('SELECT stem,df FROM vocab'))
        meta = db.execute('SELECT fingerprint,vectors FROM metadata').fetchall()
        version = db.execute('PRAGMA user_version').fetchone()[0]
    finally:
        db.close()
    return {'notes': notes, 'fts': fts, 'vocab': vocab, 'meta': meta, 'version': version}


class IncrementalIndex(StoreFixture):
    def test_writes_leave_the_same_index_as_a_rebuild(self):
        a = self.note('Cache policy', 'The local cache is rebuildable from canonical notes.')
        b = self.note('Deploy steps', 'Deploys run from the release branch after the full suite.')
        c = self.note('Queue limits', 'The ingest queue holds at most fifty jobs.')
        change(a['id'], 'demo', 'update', self.root, body='The cache is rebuilt nightly from canonical notes.')
        change(b['id'], 'demo', 'archive', self.root)
        change(c['id'], 'demo', 'delete', self.root)
        incremental = snapshot(self.root)
        rebuild_index('demo', self.root)
        self.assertEqual(incremental, snapshot(self.root))
        self.assertEqual(incremental['version'], search.INDEX_VERSION)
        self.assertEqual([h['id'] for h in search_domain('rebuilt nightly', 'demo', root=self.root)], [a['id']])
        self.assertEqual(search_domain('fifty jobs', 'demo', root=self.root), [])

    def test_a_write_reindexes_only_the_changed_note(self):
        a = self.note('Cache policy', 'The local cache is rebuildable.')
        self.note('Deploy steps', 'Deploys run from the release branch.')
        with patch.object(search, '_index_note', wraps=search._index_note) as indexed:
            change(a['id'], 'demo', 'update', self.root, body='The cache is rebuilt nightly.')
        self.assertEqual([call.args[2]['id'] for call in indexed.call_args_list], [a['id']])

    def test_an_unchanged_store_writes_nothing(self):
        self.note()
        path = self.root / 'domains/demo/search.sqlite3'
        before = path.stat().st_mtime_ns
        with patch.object(search, 'rebuild_index') as rebuilt:
            update_index('demo', self.root)
        rebuilt.assert_not_called()
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_an_index_from_other_code_is_rebuilt(self):
        self.note()
        db = sqlite3.connect(self.root / 'domains/demo/search.sqlite3')
        db.execute('PRAGMA user_version=0'); db.commit(); db.close()
        with patch.object(search, 'rebuild_index', wraps=search.rebuild_index) as rebuilt:
            update_index('demo', self.root)
        rebuilt.assert_called_once()
        self.assertEqual(snapshot(self.root)['version'], search.INDEX_VERSION)

    def test_a_damaged_index_is_rebuilt_not_left_failing(self):
        # Codex review A1: a current-version index missing a table made every later write fail.
        a = self.note('Cache policy', 'The local cache is rebuildable.')
        db = sqlite3.connect(self.root / 'domains/demo/search.sqlite3')
        db.execute('DROP TABLE vocab'); db.commit(); db.close()
        receipt = change(a['id'], 'demo', 'update', self.root, body='The cache is rebuilt nightly.')
        self.assertNotIn('warning', receipt)
        incremental = snapshot(self.root)
        rebuild_index('demo', self.root)
        self.assertEqual(incremental, snapshot(self.root))

    def test_a_note_edited_outside_the_scripts_is_picked_up(self):
        a = self.note('Cache policy', 'The local cache is rebuildable.')
        path = Path(a['path'])
        path.write_text(path.read_text(encoding='utf-8').replace('rebuildable', 'zanzibarword'), encoding='utf-8')
        update_index('demo', self.root)
        self.assertEqual([h['id'] for h in search_domain('zanzibarword', 'demo', root=self.root)], [a['id']])
        incremental = snapshot(self.root)
        rebuild_index('demo', self.root)
        self.assertEqual(incremental, snapshot(self.root))


class BackgroundOverlay(StoreFixture):
    def managed_vault(self):
        self.note()
        vault = self.base / 'vault'
        sync.build_overlay('demo', vault, self.root)
        return vault

    def background(self):
        return patch.dict(os.environ, {'LIVING_MEMORY_NO_BACKGROUND': '', 'LIVING_MEMORY_MAINTENANCE_CHILD': ''})

    def test_a_write_starts_a_worker_instead_of_building_inline(self):
        self.managed_vault()
        with self.background(), patch.object(subprocess, 'Popen') as started, patch.object(sync, 'build_overlay') as built:
            self.note('Queue limits', 'The ingest queue holds at most fifty jobs.')
        built.assert_not_called()
        started.assert_called_once()
        argv = started.call_args.args[0]
        self.assertEqual(argv[1:5], ['-I', '-X', 'utf8', '-c'])   # as the hooks run: isolated, so the temp folder is no import path
        self.assertEqual(argv[-2:], [str(self.root), 'demo'])

    def test_no_worker_when_one_is_already_waiting(self):
        self.managed_vault()
        with self.background(), file_lock(sync._overlay_lock(self.root, 'demo', 'queued'), timeout=0), \
                patch.object(subprocess, 'Popen') as started, patch.object(sync, 'build_overlay') as built:
            self.assertTrue(sync.schedule_overlay('demo', self.root))
            reconcile('demo', self.root)
        started.assert_not_called()
        built.assert_not_called()

    def test_without_background_work_the_overlay_is_built_inline(self):
        self.managed_vault()
        with patch.object(subprocess, 'Popen') as started, patch.object(sync, 'build_overlay') as built:
            self.note('Queue limits', 'The ingest queue holds at most fifty jobs.')
        started.assert_not_called()
        built.assert_called_once()

    def test_a_worker_that_cannot_start_falls_back_to_inline(self):
        self.managed_vault()
        with self.background(), patch.object(subprocess, 'Popen', side_effect=OSError('no process')), \
                patch.object(sync, 'build_overlay') as built:
            self.note('Queue limits', 'The ingest queue holds at most fifty jobs.')
        built.assert_called_once()

    def test_the_worker_command_builds_the_overlay(self):
        vault = self.managed_vault()
        with self.background(), patch.object(subprocess, 'Popen') as started:
            n = self.note('Queue limits', 'The ingest queue holds at most fifty jobs.')
        copy = vault / 'Domains/demo' / Path(n['path']).name
        self.assertFalse(copy.exists())
        argv = started.call_args.args[0]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=started.call_args.kwargs['env'])
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertTrue(copy.exists())

    def test_the_worker_leaves_the_build_to_a_waiting_one(self):
        self.managed_vault()
        with file_lock(sync._overlay_lock(self.root, 'demo', 'queued'), timeout=0), patch.object(sync, 'build_overlay') as built:
            self.assertEqual(sync.refresh_overlay('demo', self.root), 'queued')
        built.assert_not_called()
        with patch.object(sync, 'build_overlay') as built, patch.object(sync, 'OVERLAY_QUIET', 0):
            self.assertEqual(sync.refresh_overlay('demo', self.root), 'built')
        built.assert_called_once()

    def test_a_writes_lock_probe_does_not_send_the_worker_away(self):
        # Codex review B1: a starting worker met another write's instant probe of the queued lock and exited.
        import threading
        self.managed_vault()
        held, release = threading.Event(), threading.Event()
        def probe():
            with file_lock(sync._overlay_lock(self.root, 'demo', 'queued'), timeout=0):
                held.set(); release.wait(5)
        prober = threading.Thread(target=probe); prober.start()
        held.wait(5)
        threading.Timer(0.2, release.set).start()
        with patch.object(sync, 'build_overlay') as built, patch.object(sync, 'OVERLAY_QUIET', 0):
            self.assertEqual(sync.refresh_overlay('demo', self.root), 'built')
        prober.join()
        built.assert_called_once()

    def test_no_temp_folder_falls_back_to_inline(self):
        # Codex review C4: tempfile.gettempdir() raised outside the launch fallback.
        self.managed_vault()
        with self.background(), patch('tempfile.gettempdir', side_effect=FileNotFoundError('no temp')), \
                patch.object(subprocess, 'Popen') as started, patch.object(sync, 'build_overlay') as built:
            self.note('Queue limits', 'The ingest queue holds at most fifty jobs.')
        started.assert_not_called()
        built.assert_called_once()

    def test_the_worker_waits_for_writes_to_pause(self):
        self.managed_vault()
        marker = self.root / 'domains/demo/.derived-dirty'
        slept = []
        def sleep(seconds):
            slept.append(seconds)
            os.utime(marker, (marker.stat().st_atime, marker.stat().st_mtime - 1))   # a second passes with no write
        with patch.object(sync.time, 'sleep', side_effect=sleep), patch.object(sync, 'OVERLAY_QUIET', 2.5):
            os.utime(marker)
            sync._quiet(self.root, 'demo')
        self.assertEqual(len(slept), 3)
