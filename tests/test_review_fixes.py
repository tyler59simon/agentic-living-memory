"""Regression tests for the 2026-11-06 full-repo review (Codex, gpt-6-astra)."""
import json, subprocess, sys, unittest, zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from test_core import StoreFixture
from core.paths import fold
from core import runtime, session_start
from core.backup import snapshots
from core import governance
from core.governance import retag
from core.notes import capture, change, confirm, find_note
from core.note_types import review_due
from core.repository import inventory, parse

TAGS = ['subject/project', 'concern/decision']
REPO = Path(__file__).resolve().parents[1]


class AllowList(StoreFixture):
    def cmd(self, rest):
        return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" {rest}'

    def test_single_quotes_inside_double_quotes_do_not_hide_expansion(self):
        for unsafe in ('search "\'$(whoami)\'"', 'search "\'`whoami`\'"', 'search "a\\"; rm x; \\""', "search 'open"):
            with self.subTest(unsafe=unsafe):
                self.assertIsNone(runtime.memory_subcommand(self.cmd(unsafe)))
        for safe in ("search 'it''s $HOME'", 'search "plain words"', 'search "C:\\Users\\me"', "capture 'a \"quoted\" word' 'b'"):
            with self.subTest(safe=safe):
                self.assertIsNotNone(runtime.memory_subcommand(self.cmd(safe)))


class LinkedSupersede(StoreFixture):
    def test_superseding_two_linked_notes_keeps_one_copy_each_and_working_links(self):
        b = capture('Bravo predecessor', 'The second fact.', TAGS, 'demo', self.root)
        b_name = Path(b['path']).name
        a = capture('Alpha predecessor', f'See [bravo]({quote(b_name)}).', TAGS, 'demo', self.root)
        third = capture('Index of facts', f'Read [alpha]({quote(Path(a["path"]).name)}) and [bravo]({quote(b_name)}).', TAGS, 'demo', self.root)
        capture('Combined replacement', 'Both facts, merged.', TAGS, 'demo', self.root,
                supersedes=['demo:' + a['id'], 'demo:' + b['id']])
        everything = inventory('demo', self.root, retired=True)
        self.assertEqual(sorted(n['id'] for n in everything if n['id'] in (a['id'], b['id'])), sorted([a['id'], b['id']]))
        for note in everything:
            if note['id'] in (a['id'], b['id']):
                self.assertEqual(Path(note['path']).parent.name, 'archive', note['title'])
        alpha = next(n for n in everything if n['id'] == a['id'])
        self.assertIn(f'({quote(b_name)})', alpha['body'])  # both in archive/: a sibling link again
        index = next(n for n in everything if n['id'] == third['id'])
        for link in ('../archive/' + quote(Path(alpha['path']).name), '../archive/' + quote(b_name)):
            self.assertIn(link, index['body'])

    def test_archiving_rebases_the_notes_own_links(self):
        b = capture('Target note', 'Stays active.', TAGS, 'demo', self.root)
        a = capture('Linking note', f'See [target]({quote(Path(b["path"]).name)}).', TAGS, 'demo', self.root)
        change(a['id'], 'demo', 'archive', self.root)
        path, note = find_note(a['id'], 'demo', self.root)
        self.assertIn('(../notes/' + quote(Path(b['path']).name) + ')', note['body'])
        self.assertTrue((path.parent / '../notes' / Path(b['path']).name).resolve().exists())


class Rehearsal(StoreFixture):
    def test_rehearsal_never_touches_the_real_vault(self):
        self.note()
        vault = self.base / 'vault'
        from core.sync import build_overlay
        build_overlay('demo', vault, self.root)
        before = {p: p.read_bytes() for p in vault.rglob('*') if p.is_file()}
        result = subprocess.run([sys.executable, str(REPO / 'scripts' / 'rehearse_store.py'), '--root', str(self.root)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        after = {p: p.read_bytes() for p in vault.rglob('*') if p.is_file()}
        self.assertEqual(before, after)


class WriteRaces(StoreFixture):
    def test_taxonomy_add_reads_under_the_store_lock(self):
        from core import tag_taxonomy, repository
        held = []
        real = tag_taxonomy.read_json
        def spy(*a, **k):
            held.append(fold(self.root.resolve()) in getattr(repository._locks, 'held', set()))
            return real(*a, **k)
        with patch.object(tag_taxonomy, 'read_json', spy):
            tag_taxonomy.add_tags(['concern/bug'], self.root)
        self.assertEqual(held, [True])

    def test_retag_goes_stale_when_a_new_note_uses_the_old_tag(self):
        self.note('Uses architecture', 'A.')
        (self.root / 'tag_taxonomy.json').write_text(json.dumps({'tags': ['subject/project', 'concern/architecture', 'concern/decision', 'concern/design']}))
        real = governance.commit
        def racing(*a, **k):
            self.note('Captured meanwhile', 'B.')  # still uses concern/architecture, written after the retag read the notes
            return real(*a, **k)
        with patch.object(governance, 'commit', racing), self.assertRaisesRegex(ValueError, 'stale_write'):
            retag(self.root, {'concern/architecture': 'concern/design'})
        self.assertEqual({n['title'] for n in inventory('demo', self.root) if 'concern/architecture' in n['tags']}, {'Uses architecture', 'Captured meanwhile'})


class BundleIdentity(StoreFixture):
    def setUp(self):
        super().setUp()
        self.other = self.base / 'store2'
        from core.init_domain import init_domain
        init_domain('demo', [str(self.base / 'p2')], self.other)
        (self.other / 'tag_taxonomy.json').write_text(json.dumps({'tags': TAGS + ['concern/architecture']}))

    def bundle(self, name, notes):
        from core.bundle import export
        target = self.base / name
        export(self.root, 'demo', target)
        return target

    def test_two_notes_with_one_id_are_rejected(self):
        from core.bundle import import_bundle
        a = capture('First title', 'A.', TAGS, 'demo', self.root)
        b = capture('Second title', 'B.', TAGS, 'demo', self.root)
        source = self.bundle('dup.zip', None)
        with zipfile.ZipFile(source) as z:
            members = {n: z.read(n) for n in z.namelist()}
        manifest = json.loads(members['manifest.json'])
        b_name = next(e['path'] for e in manifest['notes'] if e['id'] == b['id'])
        members[b_name] = members[b_name].replace(b['id'].encode(), a['id'].encode())
        for entry in manifest['notes']:
            if entry['path'] == b_name:
                import hashlib
                entry['id'] = a['id']; entry['sha256'] = hashlib.sha256(members[b_name]).hexdigest()
        members['manifest.json'] = json.dumps(manifest).encode()
        tampered = self.base / 'tampered.zip'
        with zipfile.ZipFile(tampered, 'w') as z:
            for n, data in members.items():
                z.writestr(n, data)
        with self.assertRaisesRegex(ValueError, 'two notes with ID'):
            import_bundle(self.other, tampered)

    def test_an_import_adding_an_id_that_arrived_meanwhile_goes_stale(self):
        from core.bundle import import_bundle
        a = capture('Original title', 'A.', TAGS, 'demo', self.root)
        one = self.bundle('one.zip', None)
        change(a['id'], 'demo', 'update', self.root, title='Renamed title')
        two = self.bundle('two.zip', None)  # same ID, other filename
        real, raced = governance.commit, []
        def racing(*a, **k):
            if not raced:raced.append(1);import_bundle(self.other, one)   # lands after the second import read the store
            return real(*a, **k)
        with patch.object(governance, 'commit', racing), self.assertRaisesRegex(ValueError, 'stale_write'):
            import_bundle(self.other, two)
        self.assertEqual(len(inventory('demo', self.other)), 1)


class VaultCaptureReplay(StoreFixture):
    def test_an_interrupted_vault_capture_is_not_captured_twice(self):
        from core import sync
        self.note()
        vault = self.base / 'vault'
        sync.build_overlay('demo', vault, self.root)
        draft = vault / 'Domains/demo/Vault idea.md'
        draft.write_text('---\ntitle: "Vault idea"\ntype: "knowledge"\ntags: ["subject/project", "concern/architecture"]\n---\n\n'
                         '## 📌 Executive Summary\nA note written in Obsidian.\n', encoding='utf-8')
        real = sync.transaction
        def crash(root, changes, kind, *rest, **options):
            # The vault write follows the canonical capture; stop there, as a crash would.
            if kind == 'overlay_sync':raise RuntimeError('interrupted after the canonical write')
            return real(root, changes, kind, *rest, **options)
        with patch.object(sync, 'transaction', crash):
            with self.assertRaises(RuntimeError):
                sync.build_overlay('demo', vault, self.root)
        self.assertEqual([n['title'] for n in inventory('demo', self.root)].count('Vault idea'), 1)
        sync.build_overlay('demo', vault, self.root)
        self.assertEqual([n['title'] for n in inventory('demo', self.root)].count('Vault idea'), 1)
        self.assertFalse(draft.exists())
        page = (vault / 'Dashboards/Sync Status - demo.md').read_text(encoding='utf-8')
        self.assertNotIn('already saved', page)
        self.assertNotIn('undo None', page)
        self.assertEqual(page.count('Captured: Vault idea'), 1)   # the row saved with the capture, not a second one from the replay

    def test_undo_after_an_interrupted_vault_capture_is_not_captured_again(self):
        from core import sync
        from core.governance import undo
        self.note()
        vault = self.base / 'vault'
        sync.build_overlay('demo', vault, self.root)
        draft = vault / 'Domains/demo/Vault idea.md'
        draft.write_text('---\ntitle: "Vault idea"\ntype: "knowledge"\ntags: ["subject/project", "concern/architecture"]\n---\n\n'
                         '## 📌 Executive Summary\nA note written in Obsidian.\n', encoding='utf-8')
        real, ops = sync.transaction, []
        def crash(root, changes, kind, *rest, **options):
            if kind == 'overlay_sync':raise RuntimeError('interrupted after the canonical write')
            receipt = real(root, changes, kind, *rest, **options)
            if kind == 'overlay_capture':ops.append(receipt['operation_id'])
            return receipt
        with patch.object(sync, 'transaction', crash), self.assertRaises(RuntimeError):
            sync.build_overlay('demo', vault, self.root)
        undo(self.root, ops[0])
        self.assertEqual(sync.build_overlay('demo', vault, self.root)['applied'], [])
        self.assertNotIn('Vault idea', [n['title'] for n in inventory('demo', self.root)])   # the undo stands


class Backups(StoreFixture):
    def test_same_second_snapshots_sort_newest_first(self):
        folder = self.root / 'backups'
        folder.mkdir()
        for name in ('lm-20261106-120000.zip', 'lm-20261106-120000-1.zip', 'lm-20261106-120000-2.zip',
                     'lm-20261106-120000-10.zip', 'lm-20261105-235959-3.zip'):
            (folder / name).write_bytes(b'')
        self.assertEqual([p.name for p in snapshots(self.root)],
                         ['lm-20261106-120000-10.zip', 'lm-20261106-120000-2.zip', 'lm-20261106-120000-1.zip',
                          'lm-20261106-120000.zip', 'lm-20261105-235959-3.zip'])


    def test_new_same_second_backups_survive_retention(self):
        from core.backup import backup
        self.note()
        with patch('core.backup.datetime') as clock:
            from datetime import datetime, timezone
            clock.now.return_value = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
            made = [backup(self.root, keep=1)['backup'] for _ in range(4)]
        for path in made[-1:]:
            self.assertTrue(Path(path).exists(), path)
        self.assertEqual([p.name for p in snapshots(self.root)], [Path(made[-1]).name])
        self.assertEqual(Path(made[-1]).name, 'lm-20261106-120000-3.zip')


class SelfLinks(StoreFixture):
    def test_a_self_link_follows_the_note_into_the_archive(self):
        a = capture('Self linking note', 'Intro.', TAGS, 'demo', self.root)
        name = Path(a['path']).name
        change(a['id'], 'demo', 'update', self.root, body=f'Intro. See [details]({quote(name)}#details).')
        change(a['id'], 'demo', 'archive', self.root)
        _, note = find_note(a['id'], 'demo', self.root)
        self.assertIn(f'({quote(name)}#details)', note['body'])


class ConfirmExplicitDate(StoreFixture):
    def test_confirm_moves_an_explicit_past_review_date(self):
        n = capture('Knowledge with a date', 'A.', TAGS, 'demo', self.root, review_by=(date.today() - timedelta(days=3)).isoformat())
        self.assertTrue(review_due(find_note(n['id'], 'demo', self.root)[1]))
        confirm(n['id'], 'demo', self.root)
        self.assertFalse(review_due(find_note(n['id'], 'demo', self.root)[1]))


class SessionBudget(StoreFixture):
    def test_long_due_titles_stay_within_the_budget(self):
        for i in range(3):
            capture(f'Overdue {i} ' + ' '.join(f'word{j}' for j in range(200)), 'A.', TAGS, 'demo', self.root,
                    type='status', review_by='2020-01-01', allow_similar=True)
        for budget in (300, 800):
            text = session_start.build_context(str(self.base / 'project'), self.root, budget)[0]
            self.assertLessEqual(len(text), budget, budget)


class Blockquote(unittest.TestCase):
    def test_a_quote_does_not_hide_the_rest_of_the_prompt(self):
        self.assertEqual(runtime.durable_signal('> Earlier suggestion\n\nRemember that we always use tabs.'), 'remember')
        self.assertIsNone(runtime.durable_signal('> remember that this is quoted\nwhat now?'))


if __name__ == '__main__':
    unittest.main()
