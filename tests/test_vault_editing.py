import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from test_core import StoreFixture
from core.governance import apply, undo, plan
from core.notes import capture, find_note
from core.repository import read_json
from core.sync import sync, build_overlay
from core import runtime


class VaultEditing(StoreFixture):
    def setUp(self):
        super().setUp()
        self.vault = self.base / 'vault'

    def built(self, *notes):
        build_overlay('demo', self.vault, self.root)
        return [self.vault / 'Domains/demo' / Path(n['path']).name for n in notes]

    def edit(self, path, old, new):
        text = path.read_text(encoding='utf-8')
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding='utf-8')

    def status_page(self):
        return self.vault / 'Dashboards/Sync Status - demo.md'

    def tick_all(self):
        page = self.status_page()
        page.write_text(page.read_text(encoding='utf-8').replace('- [ ]', '- [x]'), encoding='utf-8')

    def body(self, note_id):
        return find_note(note_id, 'demo', self.root)[1]['body']

    def test_body_edit_is_applied_and_can_be_undone(self):
        a = self.note()
        copy, = self.built(a)
        self.edit(copy, 'rebuildable', 'disposable')
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual((result['plans'], result['conflicts']), ([], 0))
        self.assertEqual(len(result['applied']), 1)
        self.assertIn('disposable', self.body(a['id']))
        self.assertIn('Edited: Cache', self.status_page().read_text(encoding='utf-8'))
        reverse = undo(self.root, result['applied'][0])
        apply(self.root, reverse['plan_id'], reverse['digest'])
        self.assertIn('rebuildable', self.body(a['id']))
        self.assertIn('rebuildable', copy.read_text(encoding='utf-8'))

    def test_title_edit_renames_and_rewrites_links(self):
        a = self.note()
        name = Path(a['path']).name
        b = self.note('Pointer', f'See [the cache note]({quote(name)}) for details.')
        copy, = self.built(a)
        self.edit(copy, '"title": "Cache"' if '"title": "Cache"' in copy.read_text() else 'title: "Cache"', 'title: "Cache layer"')
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual(len(result['applied']), 1)
        path, note = find_note(a['id'], 'demo', self.root)
        self.assertEqual(note['title'], 'Cache layer')
        self.assertIn('cache-layer', path.name)
        self.assertFalse((self.root / 'domains/demo/notes' / name).exists())
        self.assertIn(quote(path.name), self.body(b['id']))
        self.assertTrue((self.vault / 'Domains/demo' / path.name).exists())
        self.assertFalse(copy.exists())

    def test_unknown_tag_is_reported_and_canonical_untouched(self):
        a = self.note()
        copy, = self.built(a)
        before = Path(a['path']).read_bytes()
        self.edit(copy, '"concern/architecture"', '"concern/made-up"')
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual(result['applied'], [])
        self.assertEqual(Path(a['path']).read_bytes(), before)
        self.assertIn('concern/made-up', self.status_page().read_text(encoding='utf-8'))
        again = build_overlay('demo', self.vault, self.root)
        self.assertEqual((again['applied'], again['plans']), ([], []))
        self.assertIn('made-up', copy.read_text(encoding='utf-8'))

    def test_status_change_waits_for_checkbox_then_archives(self):
        a = self.note()
        copy, = self.built(a)
        self.edit(copy, 'status: "active"', 'status: "archived"')
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual(len(result['plans']), 1)
        self.assertEqual(find_note(a['id'], 'demo', self.root)[1]['status'], 'active')
        self.assertIn('- [ ] Archive: Cache', self.status_page().read_text(encoding='utf-8'))
        untouched = build_overlay('demo', self.vault, self.root)
        self.assertEqual((untouched['plans'], untouched['approved']), ([], []))
        self.tick_all()
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual(len(result['approved']), 1)
        self.assertEqual(result['conflicts'], 0)
        self.assertEqual(find_note(a['id'], 'demo', self.root)[1]['status'], 'archived')
        self.assertFalse(copy.exists())
        self.assertTrue((self.vault / 'Archive/demo' / copy.name).exists())
        self.assertNotIn('- [ ]', self.status_page().read_text(encoding='utf-8'))
        self.assertEqual(build_overlay('demo', self.vault, self.root)['conflicts'], 0)

    def test_body_and_status_edit_together(self):
        a = self.note()
        copy, = self.built(a)
        self.edit(copy, 'rebuildable', 'disposable')
        self.edit(copy, 'status: "active"', 'status: "archived"')
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual((len(result['applied']), len(result['plans'])), (1, 1))
        self.assertIn('disposable', self.body(a['id']))
        self.tick_all()
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual((len(result['approved']), result['conflicts']), (1, 0))
        path, note = find_note(a['id'], 'demo', self.root)
        self.assertEqual(note['status'], 'archived')
        self.assertIn('disposable', note['body'])

    def test_vault_deletion_waits_for_checkbox(self):
        a = self.note()
        copy, = self.built(a)
        copy.unlink()
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual(len(result['plans']), 1)
        self.assertTrue(Path(a['path']).exists())
        self.assertIn('- [ ] Delete: Cache', self.status_page().read_text(encoding='utf-8'))
        self.assertEqual(build_overlay('demo', self.vault, self.root)['plans'], [])
        self.tick_all()
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual(len(result['approved']), 1)
        self.assertFalse(Path(a['path']).exists())
        state = read_json(self.root / 'domains/demo/sync-state.json')
        self.assertNotIn(a['id'], state['entries'])

    def test_restoring_deleted_file_cancels(self):
        a = self.note()
        copy, = self.built(a)
        saved = copy.read_bytes()
        copy.unlink()
        build_overlay('demo', self.vault, self.root)
        copy.write_bytes(saved)
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual((result['plans'], result['conflicts']), ([], 0))
        self.assertNotIn('- [ ]', self.status_page().read_text(encoding='utf-8'))
        self.assertTrue(Path(a['path']).exists())

    def test_ticked_foreign_plan_is_ignored(self):
        a = self.note()
        self.built(a)
        foreign = plan(self.root, {Path(a['path']).relative_to(self.root).as_posix(): None}, 'delete', ['demo'])
        page = self.status_page()
        page.write_text(page.read_text(encoding='utf-8') + f"\n- [x] Delete: Cache <!-- lm:{foreign['plan_id']} -->\n", encoding='utf-8')
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual(result['approved'], [])
        self.assertTrue(Path(a['path']).exists())

    def test_two_sided_change_clears_and_keep_restores_vault_version(self):
        a = self.note()
        copy, = self.built(a)
        self.edit(copy, 'rebuildable', 'vault-side')
        from core.notes import change
        change(a['id'], 'demo', 'update', self.root, body='Memory-side edit')
        # The agent's write synced the vault: memory wins there, the vault version is kept aside.
        self.assertIn('Memory-side', copy.read_text(encoding='utf-8'))
        self.assertIn('Memory-side', self.body(a['id']))
        page = self.status_page().read_text(encoding='utf-8')
        self.assertIn('- [ ] Keep my vault version: Cache', page)
        saved = list((self.root / 'domains/demo/staged').glob('*.conflict.md'))
        self.assertEqual(len(saved), 1)
        self.assertIn('vault-side', saved[0].read_text(encoding='utf-8'))
        # The conflict does not repeat, and later vault edits apply normally.
        quiet = build_overlay('demo', self.vault, self.root)
        self.assertEqual((quiet['conflicts'], quiet['applied']), (0, []))
        self.tick_all()
        kept = build_overlay('demo', self.vault, self.root)
        self.assertEqual(len(kept['applied']), 1)
        self.assertIn('vault-side', self.body(a['id']))
        self.assertNotIn('Keep my vault version', self.status_page().read_text(encoding='utf-8'))
        self.edit(copy, 'vault-side', 'later edit')
        self.assertEqual(len(build_overlay('demo', self.vault, self.root)['applied']), 1)
        self.assertIn('later edit', self.body(a['id']))

    def test_edited_dashboard_is_preserved_not_fatal(self):
        a = self.note()
        self.built(a)
        home = self.vault / 'Dashboards/Home.md'
        home.write_text(home.read_text(encoding='utf-8') + '\nMy own notes here.\n', encoding='utf-8')
        copy = self.vault / 'Domains/demo' / Path(a['path']).name
        self.edit(copy, 'rebuildable', 'disposable')
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual(result['view_conflicts'], ['Dashboards/Home.md'])
        self.assertIn('My own notes here.', home.read_text(encoding='utf-8'))
        self.assertIn('disposable', self.body(a['id']))
        page = self.status_page().read_text(encoding='utf-8')
        self.assertIn('Dashboards/Home.md', page)
        self.assertIn('Edited: Cache', page)
        home.unlink()
        self.assertEqual(build_overlay('demo', self.vault, self.root)['view_conflicts'], [])
        self.assertTrue(home.exists())

    def test_new_vault_copies_instead_of_proposing_deletes(self):
        notes = [self.note(f'Note {i}', f'Body number {i}.', allow_similar=True) for i in range(6)]
        self.built(*notes)
        fresh = self.base / 'fresh-vault'
        result = build_overlay('demo', fresh, self.root)
        self.assertEqual(result['plans'], [])
        self.assertTrue(result['new_vault'])
        self.assertEqual(len(list((fresh / 'Domains/demo').glob('*.md'))), 6)

    def test_deleting_the_domain_folder_restores_instead_of_deleting(self):
        notes = [self.note(f'Note {i}', f'Body number {i}.', allow_similar=True) for i in range(6)]
        self.built(*notes)
        import shutil
        shutil.rmtree(self.vault / 'Domains/demo')
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual((result['plans'], result['restored']), ([], 6))
        self.assertEqual(len(list((self.vault / 'Domains/demo').glob('*.md'))), 6)

    def test_teardown_stops_automatic_rebuilds(self):
        a = self.note()
        self.built(a)
        from core.sync import teardown_plan
        proposal = teardown_plan(self.vault)
        apply(self.vault, proposal['plan_id'], proposal['digest'])
        self.note('Later', 'Captured after teardown.')
        folder = self.vault / 'Domains/demo'
        self.assertFalse(folder.exists() and any(folder.iterdir()))
        rebuilt = build_overlay('demo', self.vault, self.root)
        self.assertEqual(rebuilt['plans'], [])

    def test_renamed_file_is_followed_not_deleted(self):
        a = self.note()
        copy, = self.built(a)
        renamed = copy.with_name('My cache notes.md')
        copy.rename(renamed)
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual((result['plans'], result['renamed']), ([], 1))
        self.assertTrue(copy.exists())
        self.assertFalse(renamed.exists())
        self.assertTrue(Path(a['path']).exists())
        self.assertNotIn('Duplicate', self.status_page().read_text(encoding='utf-8'))

    def test_renamed_and_edited_file_applies_the_edit(self):
        a = self.note()
        copy, = self.built(a)
        renamed = copy.with_name('Elsewhere.md')
        copy.rename(renamed)
        self.edit(renamed, 'rebuildable', 'disposable')
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual((result['plans'], len(result['applied'])), ([], 1))
        self.assertIn('disposable', self.body(a['id']))

    def test_recent_edits_wait_for_quiet(self):
        a = self.note()
        copy, = self.built(a)
        self.edit(copy, 'rebuildable', 'disposable')
        with patch('core.sync.QUIET_SECONDS', 60):
            result = build_overlay('demo', self.vault, self.root)
        self.assertEqual((result['applied'], result['deferred']), ([], 1))
        self.assertIn('disposable', copy.read_text(encoding='utf-8'))
        self.assertIn('rebuildable', self.body(a['id']))
        self.assertEqual(len(build_overlay('demo', self.vault, self.root)['applied']), 1)

    def test_unfinished_template_is_not_captured(self):
        self.built(self.note())
        text = (self.vault / 'Templates/Status.md').read_text(encoding='utf-8').replace('{{title}}', 'Untitled')
        draft = self.vault / 'Domains/demo/Untitled.md'
        draft.write_text(text, encoding='utf-8')
        result = build_overlay('demo', self.vault, self.root)
        self.assertEqual(result['applied'], [])
        self.assertTrue(draft.exists())
        self.assertIn('Still the template text', self.status_page().read_text(encoding='utf-8'))

    def test_vault_only_properties_survive(self):
        a = self.note()
        copy, = self.built(a)
        text = copy.read_text(encoding='utf-8').replace('\n---\n', '\naliases:\n  - cache\ncssclasses: wide\n---\n', 1)
        copy.write_text(text, encoding='utf-8')
        self.assertEqual(build_overlay('demo', self.vault, self.root)['applied'], [])
        self.assertIn('aliases:', copy.read_text(encoding='utf-8'))
        from core.notes import change
        change(a['id'], 'demo', 'update', self.root, body='Memory changed the body.')
        after = copy.read_text(encoding='utf-8')
        self.assertIn('Memory changed the body.', after)
        self.assertIn('aliases:\n  - cache', after)
        self.assertIn('cssclasses: wide', after)
        self.edit(copy, 'Memory changed the body.', 'Vault changed it back.')
        self.assertEqual(len(build_overlay('demo', self.vault, self.root)['applied']), 1)
        self.assertIn('cssclasses: wide', copy.read_text(encoding='utf-8'))
        self.assertNotIn('aliases', Path(find_note(a['id'], 'demo', self.root)[0]).read_text(encoding='utf-8'))

    def test_ticked_delete_is_logged_with_runnable_undo(self):
        a = self.note()
        copy, = self.built(a)
        copy.unlink()
        build_overlay('demo', self.vault, self.root)
        self.tick_all()
        build_overlay('demo', self.vault, self.root)
        page = self.status_page().read_text(encoding='utf-8')
        self.assertIn('Deleted: Cache', page)
        self.assertIn('living_memory.py" --domain demo undo ', page)

    def test_sync_all_continues_past_a_failing_domain(self):
        from core.init_domain import init_domain
        from core.sync import sync_all
        init_domain('other', [str(self.base / 'other')], self.root)
        capture('Other note', 'In the other domain.', ['subject/project', 'concern/decision'], 'other', self.root)
        self.built(self.note())
        build_overlay('other', self.vault, self.root)
        real = sync

        def flaky(domain, vault, root=None):
            if domain == 'other':
                raise ValueError('boom')
            return real(domain, vault, root)

        with patch('core.sync.sync', flaky):
            summary = sync_all(self.root, trigger='test')
        self.assertFalse(summary['ok'])
        self.assertEqual({r['domain']: r['ok'] for r in summary['results']}, {'demo': True, 'other': False})
        self.assertIn('Last sync failed', (self.vault / 'Dashboards/Sync Status - other.md').read_text(encoding='utf-8'))
        self.assertTrue((self.root / 'hooks/last-sync.json').exists())

    def test_session_end_hook_pulls_vault_edits(self):
        a = self.note()
        copy, = self.built(a)
        self.edit(copy, 'rebuildable', 'disposable')
        payload = {'cwd': str(self.base / 'elsewhere'), 'session_id': 's'}
        self.assertEqual(runtime.process('claude', 'session-end', payload, self.root), {})
        self.assertIn('disposable', self.body(a['id']))
        self.assertEqual(read_json(self.root / 'hooks/last-sync.json')['trigger'], 'session end')

    def test_codex_session_start_sync_is_throttled(self):
        a = self.note()
        copy, = self.built(a)
        payload = {'cwd': str(self.base / 'project'), 'session_id': 's'}
        runtime.process('codex', 'session-start', payload, self.root)
        self.edit(copy, 'rebuildable', 'disposable')
        runtime.process('codex', 'session-start', payload, self.root)
        self.assertIn('rebuildable', self.body(a['id']))  # synced moments ago: skipped
        with patch('core.runtime.SESSION_START_SYNC_SECONDS', 0):
            runtime.process('codex', 'session-start', payload, self.root)
        self.assertIn('disposable', self.body(a['id']))

    def test_note_history_ignores_sync_bookkeeping(self):
        from core.governance import history
        a = self.note()
        copy, = self.built(a)
        for word in ('one', 'two', 'three'):
            body = copy.read_text(encoding='utf-8').split('\n---\n', 1)[1].strip()
            self.edit(copy, body, 'Body ' + word)
            build_overlay('demo', self.vault, self.root)
        kinds = [row['kind'] for row in history(self.root, a['id'])]
        self.assertNotIn('overlay_state', kinds)
        self.assertEqual(kinds.count('overlay_auto_update'), 3)

    def test_invalid_new_note_is_reported_and_left_in_place(self):
        self.built(self.note())
        stray = self.vault / 'Domains/demo/Scratch.md'
        stray.write_text('just some text, no frontmatter\n', encoding='utf-8')
        build_overlay('demo', self.vault, self.root)
        self.assertTrue(stray.exists())
        self.assertIn('Scratch.md', self.status_page().read_text(encoding='utf-8'))

    def test_guard_covers_vault_note_and_dashboard_folders(self):
        self.built(self.note())
        cwd = str(self.base / 'project')
        self.assertTrue(runtime.protected(str(self.vault / 'Domains/demo/x.md'), cwd, self.root))
        self.assertTrue(runtime.protected(str(self.vault / 'Dashboards/Sync Status - demo.md'), cwd, self.root))
        self.assertTrue(runtime.protected(str(self.vault / 'Archive/demo/x.md'), cwd, self.root))
        self.assertFalse(runtime.protected(str(self.vault / 'Templates/Decision.md'), cwd, self.root))
        self.assertFalse(runtime.protected(str(self.base / 'elsewhere.md'), cwd, self.root))
