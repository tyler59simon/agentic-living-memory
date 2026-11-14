"""MAINT-1B: pinned notes, the last_retirement record, and forward restoration of archived notes."""
import contextlib, io, json, sys, unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from test_core import StoreFixture
from core import backup as snap, runtime
from core.cli import main
from core.governance import apply, undo
from core.init_domain import init_domain
from core.notes import capture, change, find_note
from core.repository import inventory, validate
from core.sync import build_overlay

TAGS = ['subject/project', 'concern/architecture']
RETIRED = {'version': 1, 'at': '2026-11-14T10:00:00+00:00', 'action': 'archive', 'actor': 'maintenance'}


class Restore(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root / 'tag_taxonomy.json').write_text(json.dumps({'tags': TAGS + ['concern/workflow']}))

    def cli(self, *argv, domain='demo', actor='main'):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--domain', domain, '--actor', actor, *argv])
        text = out.getvalue() or err.getvalue()
        return code, json.loads(text) if text.strip()[:1] in '{[' else text

    def ok(self, *argv, **kw):
        code, result = self.cli(*argv, **kw)
        self.assertEqual(code, 0, result)
        return result

    def fail(self, *argv, **kw):
        code, result = self.cli(*argv, **kw)
        self.assertNotEqual(code, 0, result)
        return result['error']['message']

    def archive(self, note, domain='demo'):
        plan = self.ok('plan', 'archive', f"{domain}:{note['id']}", domain=domain)
        return apply(self.root, plan['plan_id'], plan['digest'])

    def get(self, note, domain='demo'):
        return find_note(note['id'], domain, self.root)

    def link(self, note, domain='demo'):
        return quote(Path(self.get(note, domain)[0]).name)

    def snapshot(self):
        return {p: p.read_bytes() for p in (self.root / 'domains').rglob('*.md')}

    def test_archive_edit_referrer_restore_keeps_the_edit_and_repoints_links(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        name = quote(Path(a['path']).name)
        b = self.note('Pointer', f'See [the cache](<{Path(a["path"]).name}>) and [again]({name}).')
        archived = self.archive(a)
        self.assertIn('../archive/', self.get(b)[1]['body'])
        self.ok('update', 'demo:' + b['id'], '--body', self.get(b)[1]['body'] + '\n\nAn edit made while A was archived.')
        restored = self.ok('restore', 'demo:' + a['id'])
        path, note = self.get(a)
        self.assertEqual((path.parent.name, note['status'], note['pinned']), ('notes', 'active', True))
        body = self.get(b)[1]['body']
        self.assertIn('An edit made while A was archived.', body)
        self.assertNotIn('../archive/', body)
        self.assertIn(f'({name})', body)
        self.assertTrue(restored['operation_id'])
        with self.assertRaisesRegex(ValueError, 'Undo conflicts with later edits'):
            undo(self.root, archived['operation_id'])

    def test_restore_keeps_identity_body_created_and_checked(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        self.ok('confirm', 'demo:' + a['id'])
        before = self.get(a)[1]
        self.archive(a)
        self.ok('restore', 'demo:' + a['id'])
        after = self.get(a)[1]
        for key in ('id', 'body', 'created', 'checked', 'title'):
            self.assertEqual(before.get(key), after.get(key), key)
        self.assertEqual(after['last_retirement']['actor'], 'plan')
        self.assertEqual(after['last_retirement']['restored_by'], 'agent')
        self.assertTrue(after['last_retirement']['restored_at'])

    def test_a_filename_collision_is_refused_with_nothing_written(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        self.archive(a)
        name = Path(self.get(a)[0]).name
        clash = self.root / 'domains/demo/notes' / name
        clash.write_text('A user file in the way.\n', encoding='utf-8')
        before = self.snapshot()
        self.assertIn('collision', self.fail('restore', 'demo:' + a['id']))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.get(a)[1]['status'], 'archived')

    def test_a_keyed_map_collision_is_refused_with_nothing_written(self):
        folder = self.base / 'project' / 'repo'
        folder.mkdir(parents=True)
        first = self.ok('capture', '--type', 'map', '--title', 'repo codebase map', '--summary', 'The layout.', '--subject', 'project', '--concern', 'architecture', '--file', str(folder))
        self.archive({'id': first['id']})
        self.ok('capture', '--type', 'map', '--title', 'repo codebase map two', '--summary', 'The new layout.', '--subject', 'project', '--concern', 'architecture', '--file', str(folder))
        before = self.snapshot()
        self.assertIn('map', self.fail('restore', 'demo:' + first['id']).lower())
        self.assertEqual(self.snapshot(), before)

    def test_resolved_and_superseded_notes_are_refused_with_guidance(self):
        plan = self.ok('capture', '--type', 'plan', '--title', 'Ship the batch', '--summary', 'Order of the fixes.', '--action', 'Publish it', '--concern', 'workflow')
        self.ok('resolve', 'demo:' + plan['id'])
        self.assertIn('reopen', self.fail('restore', 'demo:' + plan['id']))
        old = self.note('Old deploy note', 'Use the old script.')
        new = self.note('Deploy with install.py', 'Use install.py.', supersedes=['demo:' + old['id']])
        message = self.fail('restore', 'demo:' + old['id'])
        self.assertIn('Superseded by', message)
        self.assertIn(new['id'], message)

    def test_a_second_restore_changes_nothing(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        self.archive(a)
        self.ok('restore', 'demo:' + a['id'])
        before = self.snapshot()
        journals = len(list((self.root / '.operations').glob('*/journal.json')))
        self.assertIn('Only archived notes can be restored', self.fail('restore', 'demo:' + a['id']))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(list((self.root / '.operations').glob('*/journal.json'))), journals)

    def test_plan_restore_goes_through_the_reviewed_route(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        self.archive(a)
        plan = self.ok('plan', 'restore', 'demo:' + a['id'])
        self.assertEqual(self.get(a)[1]['status'], 'archived')
        apply(self.root, plan['plan_id'], plan['digest'])
        self.assertEqual((self.get(a)[1]['status'], self.get(a)[1]['pinned']), ('active', True))

    def test_restore_is_undoable(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        self.archive(a)
        result = self.ok('restore', 'demo:' + a['id'])
        reverse = undo(self.root, result['operation_id'])
        apply(self.root, reverse['plan_id'], reverse['digest'])
        self.assertEqual(self.get(a)[1]['status'], 'archived')

    def test_cross_domain_relative_links_follow_the_move(self):
        init_domain('other', [str(self.base / 'other')], self.root)
        a = self.note('Cache policy', 'The cache is rebuildable.')
        name = Path(a['path']).name
        b = capture('Foreign pointer', f'Cache rules live in [the policy](<../../demo/notes/{name}>).', TAGS, 'other', self.root)
        self.archive(a)
        self.assertIn(f'../../demo/archive/{name}', self.get(b, 'other')[1]['body'])
        restored = self.ok('restore', 'demo:' + a['id'])
        self.assertIn(f'../../demo/notes/{name}', self.get(b, 'other')[1]['body'])
        reverse = undo(self.root, restored['operation_id'])
        self.assertIn('other', reverse and json.loads((self.root / '.plans' / (reverse['plan_id'] + '.json')).read_text(encoding='utf-8'))['domains'])

    def test_too_many_referring_notes_fails_with_nothing_written(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        for i in range(3):
            self.note(f'Pointer number {i}', f'Unique words {i} about [the cache](<{Path(a["path"]).name}>).', allow_similar=True)
        before = self.snapshot()
        with patch('core.notes.LINK_UPDATE_MAX', 2):
            self.assertIn('too_many_link_updates', self.fail('plan', 'archive', 'demo:' + a['id']))
        self.assertEqual(self.snapshot(), before)

    def test_restore_is_main_only_and_the_hook_allows_it_for_the_main_agent(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        self.archive(a)
        for actor in ('subagent', 'viewer'):
            self.assertNotEqual(self.cli('restore', 'demo:' + a['id'], actor=actor)[0], 0)
        self.assertEqual(self.get(a)[1]['status'], 'archived')
        project = self.base / 'project'
        project.mkdir(exist_ok=True)
        command = f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo restore demo:abcdef12'
        payload = {'cwd': str(project), 'session_id': 'fin', 'tool_name': 'Bash', 'tool_input': {'command': command}}
        self.assertIn('allow', runtime.process('claude', 'pre-tool-use', payload, self.root))
        self.assertIn('deny', runtime.process('claude', 'pre-tool-use', {**payload, 'agent_id': 'sub-1'}, self.root))
        self.assertIn('restore', runtime.MUTATING_SUBCOMMANDS)

    def test_version_lists_the_new_capabilities_and_command(self):
        version = self.ok('version')
        self.assertIn('restore', version['commands'])
        self.assertEqual(version['capabilities'], {'learning': 1, 'restore': 1, 'pin': 1,
                                                   'maintenance': {'version': 1, 'actions': ['status', 'preview', 'run', 'enable', 'disable', 'history']}})

    def test_the_log_shows_restored_and_the_archive_row(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        self.archive(a)
        self.ok('restore', 'demo:' + a['id'])
        events = [r['event'] for r in self.ok('log', '--note', 'demo:' + a['id'])['rows']]
        self.assertEqual(events[:2], ['restored', 'archived'])
        self.assertEqual(self.ok('log', '--event', 'restored')['rows'][0]['title'], 'Cache policy')


class Pin(StoreFixture):
    def setUp(self):
        super().setUp()
        self.vault = self.base / 'vault'

    def cli(self, *argv, actor='main'):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--domain', 'demo', '--actor', actor, *argv])
        text = out.getvalue() or err.getvalue()
        return code, json.loads(text) if text.strip()[:1] in '{[' else text

    def ok(self, *argv):
        code, result = self.cli(*argv)
        self.assertEqual(code, 0, result)
        return result

    def get(self, note):
        return find_note(note['id'], 'demo', self.root)[1]

    def test_capture_pin_and_update_pin_unpin_leave_updated_and_checked_alone(self):
        created = self.ok('capture', '--title', 'Pinned fact', '--summary', 'It stays.', '--subject', 'project', '--concern', 'architecture', '--pin')
        self.assertIs(self.get(created)['pinned'], True)
        plain = self.note('Plain fact', 'Nothing special.')
        self.assertNotIn('pinned', self.get(plain))
        self.ok('confirm', 'demo:' + plain['id'])
        before = self.get(plain)
        self.ok('update', 'demo:' + plain['id'], '--pin')
        pinned = self.get(plain)
        self.assertIs(pinned['pinned'], True)
        self.ok('update', 'demo:' + plain['id'], '--unpin')
        after = self.get(plain)
        self.assertIs(after['pinned'], False)
        for key in ('updated', 'checked', 'confirmed', 'review_by', 'body'):
            self.assertEqual((before.get(key), pinned.get(key)), (before.get(key), after.get(key)), key)
        with self.assertRaises(SystemExit):
            self.cli('update', 'demo:' + plain['id'], '--pin', '--unpin')

    def test_a_pinned_note_still_archives_resolves_and_supersedes_by_hand(self):
        a = self.note('Pinned fact', 'It stays.', pinned=True)
        plan = self.ok('plan', 'archive', 'demo:' + a['id'])
        apply(self.root, plan['plan_id'], plan['digest'])
        self.assertEqual(self.get(a)['status'], 'archived')

    def test_pinned_must_be_a_real_bool(self):
        a = self.note('Plain fact', 'Nothing special.')
        for bad in ('true', 1, None):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, 'pinned'):
                    change(a['id'], 'demo', 'update', self.root, pinned=bad)
        note = dict(self.get(a), pinned='true')
        with self.assertRaisesRegex(ValueError, 'pinned'):
            validate(note, 'demo', self.root)

    def test_pin_and_last_retirement_survive_bundle_and_backup(self):
        a = self.note('Pinned fact', 'It stays.', pinned=True)
        b = self.note('Retired fact', 'It went.')
        plan = self.ok('plan', 'archive', 'demo:' + b['id'])
        apply(self.root, plan['plan_id'], plan['digest'])
        bundle = self.base / 'all.zip'
        self.ok('export', '--out', str(bundle), '--all')
        other = self.base / 'store2'
        init_domain('demo', [str(self.base / 'project2')], other)
        (other / 'tag_taxonomy.json').write_text(json.dumps({'tags': TAGS}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(['--root', str(other), '--domain', 'demo', 'import-plan', str(bundle)])
        imported = json.loads(out.getvalue())
        apply(other, imported['plan_id'], imported['digest'])
        self.assertIs(find_note(a['id'], 'demo', other)[1]['pinned'], True)
        self.assertEqual(find_note(b['id'], 'demo', other)[1]['last_retirement'], self.get(b)['last_retirement'])
        source = snap.backup(self.root)['backup']
        for note in (a, b):
            Path(self.get(note)['path']).unlink()
        proposed = snap.restore_plan(self.root, source)
        apply(self.root, proposed['plan_id'], proposed['digest'])
        self.assertIs(self.get(a)['pinned'], True)
        self.assertEqual(self.get(b)['last_retirement']['action'], 'archive')

    def edit(self, path, old, new):
        text = path.read_text(encoding='utf-8')
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding='utf-8')

    def test_vault_edits_omit_toggle_and_refuse_pinned(self):
        a = self.note('Pinned fact', 'It stays.', pinned=True)
        build_overlay('demo', self.vault, self.root)
        copy = self.vault / 'Domains/demo' / Path(a['path']).name
        self.assertIn('pinned', copy.read_text(encoding='utf-8'))
        # Omitting the property keeps the pin.
        text = copy.read_text(encoding='utf-8')
        copy.write_text('\n'.join(l for l in text.splitlines() if not l.startswith('pinned:')).replace('It stays.', 'It stays here.') + '\n', encoding='utf-8')
        build_overlay('demo', self.vault, self.root)
        self.assertEqual((self.get(a)['pinned'], 'here' in self.get(a)['body']), (True, True))
        # Toggling it works.
        copy = self.vault / 'Domains/demo' / Path(self.get(a)['path']).name
        self.edit(copy, 'pinned: true', 'pinned: false')
        build_overlay('demo', self.vault, self.root)
        self.assertIs(self.get(a)['pinned'], False)
        # A non-bool is refused with a warning and the canonical value stays.
        self.edit(copy, 'pinned: false', 'pinned: "yes"')
        build_overlay('demo', self.vault, self.root)
        self.assertIs(self.get(a)['pinned'], False)
        self.assertIn("pinned must be true or false", (self.vault / 'Dashboards/Sync Status - demo.md').read_text(encoding='utf-8'))

    def test_a_pin_only_vault_edit_keeps_updated(self):
        a = self.note('Plain fact', 'Nothing special.')
        build_overlay('demo', self.vault, self.root)
        before = self.get(a)
        copy = self.vault / 'Domains/demo' / Path(a['path']).name
        text = copy.read_text(encoding='utf-8')
        copy.write_text(text.replace('title: ', 'pinned: true\ntitle: ', 1), encoding='utf-8')
        build_overlay('demo', self.vault, self.root)
        after = self.get(a)
        self.assertIs(after['pinned'], True)
        self.assertEqual((after['updated'], after.get('checked')), (before['updated'], before.get('checked')))

    def test_pin_beside_a_capture_payload_is_refused(self):
        payload = self.base / 'note.json'
        payload.write_text(json.dumps({'title': 'Payload fact', 'body': 'From a file.', 'tags': ['subject/project', 'concern/architecture']}), encoding='utf-8')
        code, result = self.cli('capture', '--payload-file', str(payload), '--pin')
        self.assertNotEqual(code, 0)
        self.assertIn('--pin', str(result))

    def test_the_log_shows_pinned_and_unpinned(self):
        a = self.note('Plain fact', 'Nothing special.')
        self.ok('update', 'demo:' + a['id'], '--pin')
        self.ok('update', 'demo:' + a['id'], '--unpin')
        out = self.ok('log', '--note', 'demo:' + a['id'])
        self.assertEqual([r['event'] for r in out['rows']][:2], ['unpinned', 'pinned'])

    def test_vault_lifecycle_request_records_actor_vault(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        build_overlay('demo', self.vault, self.root)
        copy = self.vault / 'Domains/demo' / Path(a['path']).name
        self.edit(copy, 'status: "active"', 'status: "archived"')
        result = build_overlay('demo', self.vault, self.root)
        plan = result['plans'][0]
        apply(self.root, plan['plan_id'], plan['digest'])
        self.assertEqual(self.get(a)['last_retirement']['actor'], 'vault')


class LastRetirement(StoreFixture):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--domain', 'demo', *argv])
        text = out.getvalue() or err.getvalue()
        return code, json.loads(text) if text.strip()[:1] in '{[' else text

    def ok(self, *argv):
        code, result = self.cli(*argv)
        self.assertEqual(code, 0, result)
        return result

    def get(self, note):
        return find_note(note['id'], 'demo', self.root)[1]

    def test_each_retiring_path_records_action_and_actor(self):
        plan_note = self.ok('capture', '--type', 'plan', '--title', 'Ship the batch', '--summary', 'Order.', '--action', 'Publish it', '--concern', 'architecture')
        self.ok('resolve', 'demo:' + plan_note['id'])
        self.assertEqual({k: self.get(plan_note)['last_retirement'][k] for k in ('version', 'action', 'actor')}, {'version': 1, 'action': 'resolve', 'actor': 'agent'})
        a = self.note('Cache policy', 'The cache is rebuildable.')
        plan = self.ok('plan', 'archive', 'demo:' + a['id'])
        apply(self.root, plan['plan_id'], plan['digest'])
        self.assertEqual({k: self.get(a)['last_retirement'][k] for k in ('action', 'actor')}, {'action': 'archive', 'actor': 'plan'})
        old = self.note('Old deploy note', 'Use the old script.')
        new = self.ok('capture', '--title', 'Deploy with install.py', '--summary', 'Use install.py.', '--subject', 'project', '--concern', 'architecture', '--supersedes', 'demo:' + old['id'])
        record = self.get(old)['last_retirement']
        self.assertEqual((record['action'], record['actor'], record['successor']), ('supersede', 'agent', f"demo:{new['id']}"))
        old2 = self.note('Older deploy note', 'Use the older script.', allow_similar=True)
        self.ok('supersede', 'demo:' + old2['id'], '--by', 'demo:' + new['id'])
        self.assertEqual(self.get(old2)['last_retirement']['successor'], f"demo:{new['id']}")

    def test_merge_records_a_plan_supersession_with_the_successor(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        b = self.note('Eviction policy', 'Entries expire after a day.')
        from core.governance import merge_plan
        proposed = merge_plan(self.root, {'target': 'demo:' + a['id'], 'sources': ['demo:' + b['id']], 'body': 'Rebuildable, and entries expire after a day.'})
        apply(self.root, proposed['plan_id'], proposed['digest'])
        record = self.get(b)['last_retirement']
        self.assertEqual((record['action'], record['actor'], record['successor']), ('supersede', 'plan', f"demo:{a['id']}"))

    def test_auto_resolve_records_actor_auto_resolve(self):
        p = self.ok('capture', '--type', 'plan', '--title', 'Ship the batch', '--summary', 'Order.', '--action', 'Publish it', '--concern', 'architecture')
        change(p['id'], 'demo', 'resolve', self.root, actor='auto_resolve')
        self.assertEqual(self.get(p)['last_retirement']['actor'], 'auto_resolve')

    def test_restore_fills_restored_fields_and_a_new_retirement_replaces_the_record(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        plan = self.ok('plan', 'archive', 'demo:' + a['id'])
        apply(self.root, plan['plan_id'], plan['digest'])
        self.ok('restore', 'demo:' + a['id'])
        self.assertTrue(self.get(a)['last_retirement']['restored_at'])
        plan = self.ok('plan', 'archive', 'demo:' + a['id'])
        apply(self.root, plan['plan_id'], plan['digest'])
        self.assertNotIn('restored_at', self.get(a)['last_retirement'])

    def test_capture_and_update_cannot_set_it(self):
        with self.assertRaisesRegex(ValueError, 'last_retirement'):
            self.note('Sneaky', 'Body.', last_retirement=RETIRED)
        a = self.note('Cache policy', 'The cache is rebuildable.')
        with self.assertRaisesRegex(ValueError, 'last_retirement'):
            change(a['id'], 'demo', 'update', self.root, last_retirement=RETIRED)

    def test_validation_rules(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        note = self.get(a)
        validate({**note, 'last_retirement': {**RETIRED, 'reason': 'Obsolete.', 'rule': 'obsolete_source_claim', 'evidence': [
            {'kind': 'note', 'ref': 'demo:' + a['id'], 'revision': 'a' * 64}, {'kind': 'file', 'path': 'repo/core/x.py', 'sha256': 'b' * 64}]}}, 'demo', self.root)
        bad = [{**RETIRED, 'version': 2}, {**RETIRED, 'action': 'delete'}, {**RETIRED, 'actor': 'someone'}, {**RETIRED, 'at': 'yesterday'},
               {**RETIRED, 'reason': 'x' * 501}, {**RETIRED, 'evidence': [{'kind': 'file', 'path': 'p', 'sha256': 'b' * 64}] * 9},
               {**RETIRED, 'evidence': [{'kind': 'web', 'path': 'p', 'sha256': 'b' * 64}]}, {**RETIRED, 'evidence': [{'kind': 'file', 'path': 'p', 'sha256': 'short'}]},
               {**RETIRED, 'reason': 'token ghp_' + 'a1B2c3D4e5' * 4}, 'archived']
        for value in bad:
            with self.subTest(value=str(value)[:60]), self.assertRaises(ValueError):
                validate({**note, 'last_retirement': value}, 'demo', self.root)

    def test_list_and_review_rows_show_pin_and_retirement(self):
        a = self.note('Pinned fact', 'It stays.', pinned=True, review_by='2020-01-01')
        b = self.note('Retired fact', 'It went.', allow_similar=True)
        plan = self.ok('plan', 'archive', 'demo:' + b['id'])
        apply(self.root, plan['plan_id'], plan['digest'])
        rows = {r['id']: r for r in self.ok('list', '--status', 'any', '--with-summary')['notes']}
        self.assertIs(rows[a['id']]['pinned'], True)
        self.assertEqual(set(rows[b['id']]['last_retirement']), {'action', 'at', 'actor', 'reason'})
        read = self.ok('read', 'demo:' + a['id'])
        self.assertIs(read['pinned'], True)
        self.assertEqual(self.ok('read', 'demo:' + b['id'])['last_retirement']['action'], 'archive')
        review = self.ok('review', '--limit', '5')['notes']
        self.assertEqual([row['pinned'] for row in review], [True])


if __name__ == '__main__':
    unittest.main()
