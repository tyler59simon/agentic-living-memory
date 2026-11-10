"""Snapshots of the canonical store and restore through an approved, undoable plan."""
import io
import json
import os
import time
import zipfile
from contextlib import redirect_stdout
from unittest.mock import patch

from test_core import StoreFixture
from core import backup as snap
from core import runtime
from core.cli import main as cli
from core.governance import apply, inspect_plan, undo
from core.notes import find_note
from core.repository import inventory


class Backups(StoreFixture):
    def test_snapshot_holds_the_canonical_set_only(self):
        n = self.note()
        (self.root / 'domains/demo/staged').mkdir(exist_ok=True)
        (self.root / 'domains/demo/staged/draft.md').write_text('---\nid: x\n---\nstaged')
        (self.root / '.turns').mkdir(exist_ok=True)
        (self.root / '.turns/t.json').write_text('{}')
        result = snap.backup(self.root)
        names = zipfile.ZipFile(result['backup']).namelist()
        self.assertIn('bindings.json', names)
        self.assertIn('tag_taxonomy.json', names)
        self.assertTrue(any(name.endswith(n['id'][:8] + '.md') for name in names), names)
        self.assertFalse(any('staged' in name or '.turns' in name or name.endswith('.sqlite3') for name in names), names)
        self.assertEqual(result['notes'], 1)

    def test_a_snapshot_that_fails_verification_is_not_kept(self):
        self.note()
        with patch('core.backup._allowed', return_value=False):
            with self.assertRaisesRegex(ValueError, 'outside the canonical set'):
                snap.backup(self.root)
        self.assertEqual(list((self.root / 'backups').iterdir()), [])

    def test_retention_keeps_the_newest(self):
        self.note()
        for _ in range(4):
            snap.backup(self.root, keep=2)
        self.assertEqual(len(snap.snapshots(self.root)), 2)

    def test_hook_backups_run_at_most_daily(self):
        self.note()
        self.assertIsNotNone(snap.maybe_backup(self.root))
        self.assertIsNone(snap.maybe_backup(self.root))
        marker = self.root / 'hooks/last-backup.json'
        marker.write_text(json.dumps({'at': '2000-01-01T00:00:00+00:00'}))
        self.assertIsNotNone(snap.maybe_backup(self.root))

    def test_session_end_takes_the_daily_backup_and_never_fails(self):
        self.note()
        payload = {'cwd': str(self.base / 'project'), 'session_id': 's'}
        self.assertEqual(runtime.process('claude', 'session-end', payload, self.root), {})
        self.assertEqual(len(snap.snapshots(self.root)), 1)
        with patch('core.backup.backup', side_effect=OSError('disk full')):
            (self.root / 'hooks/last-backup.json').unlink()
            self.assertEqual(runtime.process('claude', 'session-end', payload, self.root), {})

    def test_doctor_warns_when_backups_are_missing_or_stale(self):
        def warnings():
            out = io.StringIO()
            with redirect_stdout(out):
                cli(['--root', str(self.root), 'doctor', '--cwd', str(self.base / 'project')])
            return json.loads(out.getvalue())['warnings']
        self.assertTrue(any('no backup yet' in w for w in warnings()))
        path = snap.backup(self.root)['backup']
        self.assertFalse(any('backup' in w for w in warnings()))
        old = time.time() - 72 * 3600
        os.utime(path, (old, old))
        self.assertTrue(any('hours old' in w for w in warnings()))

    def test_restore_plan_round_trip(self):
        keep = self.note('Keep me', 'Original body.')
        source = snap.backup(self.root)['backup']
        path, _ = find_note(keep['id'], 'demo', self.root)
        path.unlink()
        later = self.note('Added after the backup', 'New.')
        proposed = snap.restore_plan(self.root, source)
        self.assertEqual((proposed['summary']['write'], proposed['summary']['delete']), (1, 1))
        effects = {e['action'] for e in inspect_plan(self.root, proposed['plan_id'])['effects']}
        self.assertEqual(effects, {'write', 'delete'})
        receipt = apply(self.root, proposed['plan_id'], proposed['digest'])
        ids = {n['id'] for n in inventory('demo', self.root)}
        self.assertEqual(ids, {keep['id']})
        reverse = undo(self.root, receipt['operation_id'])
        apply(self.root, reverse['plan_id'], reverse['digest'])
        self.assertEqual({n['id'] for n in inventory('demo', self.root)}, {later['id']})

    def test_restore_of_an_identical_store_changes_nothing(self):
        self.note()
        source = snap.backup(self.root)['backup']
        self.assertEqual(snap.restore_plan(self.root, source)['status'], 'unchanged')

    def test_restore_rejects_members_outside_the_canonical_set(self):
        for name in ('../escape.md', 'domains/demo/notes/../../x.md', 'C:/abs.md', 'domains/demo/search.sqlite3', 'hooks/x.json'):
            with self.subTest(name=name):
                bad = self.base / 'bad.zip'
                with zipfile.ZipFile(bad, 'w') as archive:
                    archive.writestr(name, '---\nid: x\n---\nbody')
                with self.assertRaisesRegex(ValueError, 'outside the canonical set'):
                    snap.restore_plan(self.root, bad)

    def test_cli_backup_records_the_marker(self):
        self.note()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli(['--root', str(self.root), 'backup', '--keep', '3']), 0)
        self.assertTrue(json.loads(out.getvalue())['ok'])
        self.assertIsNone(snap.maybe_backup(self.root))  # a manual backup counts for the day

    def test_subagents_may_not_run_backup(self):
        cmd = f'"{runtime.sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" backup'
        payload = {'cwd': str(self.base / 'project'), 'session_id': 's', 'tool_name': 'Bash',
                   'tool_input': {'command': cmd}, 'agent_id': 'a1'}
        self.assertIn('deny', runtime.process('claude', 'pre-tool-use', payload, self.root))


    def test_restore_repairs_damaged_bindings_through_the_cli(self):
        self.note()
        source = snap.backup(self.root)['backup']
        (self.root / 'bindings.json').write_text('{not json')
        def run(*argv):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli(['--root', str(self.root), '--cwd', str(self.base / 'project'), *argv]), 0)
            return json.loads(out.getvalue())
        proposed = run('restore-plan', source)
        run('inspect-plan', proposed['plan_id'])
        run('apply', proposed['plan_id'], '--expected-plan-sha256', proposed['digest'])
        self.assertIn('demo', json.loads((self.root / 'bindings.json').read_text())['domains'])

    def test_restore_covers_domains_that_were_empty(self):
        from core.init_domain import init_domain
        from core.notes import capture
        init_domain('empty', [str(self.base / 'other')], self.root)
        source = snap.backup(self.root)['backup']
        capture('Added later', 'New.', ['subject/project', 'concern/decision'], 'empty', self.root)
        proposed = snap.restore_plan(self.root, source)
        self.assertEqual(proposed['summary']['delete'], 1)

    def test_same_second_backups_get_distinct_names(self):
        self.note()
        with patch('core.backup.datetime') as clock:
            from datetime import datetime, timezone
            clock.now.return_value = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
            first = snap.backup(self.root)['backup']
            second = snap.backup(self.root)['backup']
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith('lm-20260102-030405-1.zip'), second)


if __name__ == '__main__':
    import unittest
    unittest.main()
