"""The global domain: --global capture, duplicates across domains, automatic and reviewed promotion."""
import contextlib
import io
import json
from pathlib import Path

from test_core import StoreFixture
from core import runtime, session_start
from core.cli import main, doctor
from core.common import canonical_domain, load_bindings
from core.governance import apply, undo, promote_plan, promote_suggest
from core.init_domain import init_domain
from core.migration import migration_plan
from core.notes import capture, near_identical
from core.repository import inventory, read_json

TAGS = ['subject/project', 'concern/decision']


class GlobalFixture(StoreFixture):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def other(self):
        init_domain('other', [str(self.base / 'other')], self.root)

    def ids(self, domain, retired=False):
        return {n['id']: n for n in inventory(domain, self.root, retired=retired)}

    def undo(self, operation_id):
        proposed = undo(self.root, operation_id)
        return apply(self.root, proposed['plan_id'], proposed['digest'])


class Capture(GlobalFixture):
    def test_global_flag_overrides_the_pinned_domain_and_keeps_the_receipt(self):
        runtime.process('claude', 'user-prompt', {'cwd': str(self.base / 'project'), 'session_id': 's1', 'prompt': 'hello'}, self.root)
        code, out = self.cli('--domain', 'demo', 'capture', '--global', '--session', 's1', '--type', 'decision', '--subject', 'project',
                             '--concern', 'decision', '--title', 'Use forward slashes in shell paths', '--summary', 'Bash strips backslashes.')
        self.assertEqual((code, out['domain'], out['status']), (0, '_global', 'created'))
        self.assertIn(out['id'], self.ids('_global'))
        state = read_json(runtime._pointer(self.root, 's1'))
        self.assertIn('_global:' + out['id'], state.get('saved', []))

    def test_global_notes_reach_every_project(self):
        self.other()
        g = capture('Prefer forward slashes', 'Use forward slashes in shell paths on Windows.', TAGS, '_global', self.root)
        for cwd, domain in ((self.base / 'project', 'demo'), (self.base / 'other', 'other')):
            _, ids, _ = runtime.context(str(cwd), 'forward slashes shell paths', self.root, domain)
            self.assertIn('_global:' + g['id'], ids)
            self.assertIn('Prefer forward slashes', session_start.build_context(str(cwd), self.root)[0])

    def test_scope_hint_on_a_general_looking_project_note(self):
        out = capture('Prefer short commit messages', 'Keep commit subjects short.', TAGS, 'demo', self.root, type='preference')
        self.assertIn('promote-plan demo:' + out['id'], out['scope_hint']['next'])
        self.assertNotIn('scope_hint', capture('Prefer tabs', 'Tabs.', TAGS, '_global', self.root, type='preference'))


class Bindings(GlobalFixture):
    def test_migration_points_a_folderless_global_at__global(self):
        config = load_bindings(self.root);config['search_scope']['global_domains'] = ['global']
        (self.root / 'bindings.json').write_text(json.dumps(config), encoding='utf-8')
        self.assertTrue(any("names 'global'" in w for w in doctor(self.root, str(self.base / 'project'))['warnings']))
        proposed = migration_plan(self.root);apply(self.root, proposed['plan_id'], proposed['digest'])
        config = load_bindings(self.root)
        self.assertEqual(config['search_scope']['global_domains'], ['_global'])
        self.assertEqual(canonical_domain('global', self.root), '_global')
        self.assertFalse(any('global_domains' in w for w in doctor(self.root, str(self.base / 'project'))['warnings']))


class AcrossDomains(GlobalFixture):
    def test_an_exact_copy_of_a_global_note_is_a_duplicate(self):
        g = capture('Prefer forward slashes', 'Use forward slashes.', TAGS, '_global', self.root)
        out = capture('Prefer forward slashes', 'Use forward slashes.', TAGS, 'demo', self.root)
        self.assertEqual((out['status'], out['domain'], out['id']), ('duplicate', '_global', g['id']))
        self.assertEqual(self.ids('demo'), {})

    def test_a_similar_global_note_is_refused_with_its_ref(self):
        g = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, '_global', self.root)
        with self.assertRaisesRegex(ValueError, '_global:' + g['id']):
            capture('Deploy using install.py', 'Run install.py for deploys.', TAGS, 'demo', self.root)

    def test_parent_and_child_duplicates_are_refused_not_promoted(self):
        init_domain('child', [str(self.base / 'project' / 'child')], self.root)
        parent = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'demo', self.root)
        with self.assertRaisesRegex(ValueError, 'demo:' + parent['id'] + '.*already sees'):
            capture('Deploy using install.py', 'Run install.py for deploys.', TAGS, 'child', self.root)
        self.assertEqual(capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'child', self.root)['status'], 'duplicate')
        self.assertFalse((self.root / 'domains/_global/notes').is_dir())

    def test_an_exact_copy_in_an_unrelated_project_is_promoted_and_undoable(self):
        self.other()
        first = capture('Codex review needs a full-repo brief', 'Codex review covers only the diff.', TAGS, 'other', self.root)
        out = capture('Codex review needs a full-repo brief', 'Codex review covers only the diff.', TAGS, 'demo', self.root)
        self.assertEqual((out['status'], out['domain'], out['id']), ('promoted', '_global', first['id']))
        self.assertIn(first['id'], self.ids('_global'));self.assertEqual(self.ids('other'), {});self.assertEqual(self.ids('demo'), {})
        self.assertEqual(read_json(self.root / '.operations' / out['operation_id'] / 'journal.json')['kind'], 'promote')
        self.undo(out['operation_id'])
        self.assertIn(first['id'], self.ids('other'));self.assertEqual(self.ids('_global'), {})

    def test_a_near_identical_note_in_an_unrelated_project_is_promoted(self):
        self.other()
        body = 'Codex review only covers the diff, so a whole-repo review needs codex exec with a full-repo brief and read-only sandbox.'
        first = capture('Codex review needs a full-repo brief', body, TAGS, 'other', self.root)
        out = capture('Codex review needs a full repo brief', body.replace('read-only sandbox', 'a read-only sandbox'), TAGS, 'demo', self.root)
        self.assertEqual((out['status'], out['id']), ('promoted', first['id']))
        self.assertIn('update _global:' + first['id'], out['hint'])

    def test_a_merely_similar_note_in_an_unrelated_project_is_refused_with_promote_plan(self):
        self.other()
        first = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'other', self.root)
        with self.assertRaisesRegex(ValueError, 'promote-plan other:' + first['id']):
            capture('Deploy using install.py', 'Run install.py --plan and then --apply-plan.', TAGS, 'demo', self.root)
        self.assertEqual(capture('Deploy using install.py', 'Run install.py --plan and then --apply-plan.', TAGS, 'demo', self.root,
                                 allow_similar=True)['status'], 'created')

    def test_capturing_to_global_supersedes_a_similar_project_note(self):
        old = capture('Deploy with install.py', 'Use install.py.', TAGS, 'demo', self.root)
        new = capture('Deploy using install.py', 'Run install.py --plan, then --apply-plan with the digest.', TAGS, '_global', self.root)
        self.assertEqual(new['promoted'], ['demo:' + old['id']])
        retired = self.ids('_global', retired=True)[old['id']]
        self.assertEqual((retired['status'], retired['superseded_by'], retired['domain']), ('superseded', new['id'], '_global'))
        self.assertIn('/archive/', Path(retired['path']).as_posix())
        self.assertEqual(self.ids('_global')[new['id']]['supersedes'], [old['id']])
        self.assertEqual(self.ids('demo', retired=True), {})
        self.undo(new['operation_id'])
        self.assertIn(old['id'], self.ids('demo'));self.assertEqual(self.ids('_global', retired=True), {})


class NearIdentical(GlobalFixture):
    def test_rules(self):
        note = lambda title, body, kind='plan': {'title': title, 'body': body, 'type': kind}
        body = 'Rotate the signing tokens for payment authentication every quarter.'
        self.assertTrue(near_identical('Payment API authentication', body, 'plan', note('Payment API authentication', body)))
        self.assertFalse(near_identical('Payment API authentication', body, 'plan', note('Payment API pagination', body)))
        self.assertFalse(near_identical('Payment API authentication', body, 'plan', note('Payment API authentication', 'Cursor pagination for the list endpoint.')))
        self.assertFalse(near_identical('Payment API authentication', body, 'plan', note('Payment API authentication', body, 'decision')))


class Promotion(GlobalFixture):
    def count(self, folder):
        return len(list((self.root / folder).glob('*/journal.json' if folder == '.operations' else '*.json')))

    def test_suggest_lists_candidates_and_writes_nothing(self):
        self.other()
        a = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'other', self.root)
        b = capture('Deploy using install.py', 'Run install.py --plan and then --apply-plan.', TAGS, 'demo', self.root, allow_similar=True)
        p = capture('Prefer short commit messages', 'Keep subjects short.', TAGS, 'demo', self.root, type='preference')
        before = (self.count('.operations'), self.count('.plans'))
        rows = {r['ref']: r for r in promote_suggest(self.root)['candidates']}
        self.assertEqual((self.count('.operations'), self.count('.plans')), before)
        self.assertIn('also in project demo', rows['other:' + a['id'][:8]]['reasons'][0])
        self.assertIn('also in project other', rows['demo:' + b['id'][:8]]['reasons'][0])
        self.assertIn('preference', rows['demo:' + p['id'][:8]]['reasons'][0])
        self.assertEqual({r['domain'] for r in promote_suggest(self.root, 'other')['candidates']}, {'other'})

    def test_promote_plan_moves_notes_merges_twins_and_follows_links(self):
        self.other()
        a = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'other', self.root)
        b = capture('Deploy using install.py', 'Run install.py --plan and then --apply-plan.', TAGS, 'demo', self.root, allow_similar=True)
        name = Path(b['path']).name
        c = capture('Release checklist', f'Deploy as in [the deploy note](<{name}>).', TAGS, 'demo', self.root)
        proposed = promote_plan(self.root, ['other:' + a['id'], 'demo:' + b['id']])
        self.assertEqual(self.ids('_global'), {})            # a plan writes nothing until applied
        apply(self.root, proposed['plan_id'], proposed['digest'])
        active, everything = self.ids('_global'), self.ids('_global', retired=True)
        self.assertEqual(set(active), {b['id']});self.assertEqual(everything[a['id']]['superseded_by'], b['id'])
        self.assertEqual(active[b['id']]['supersedes'], [a['id']])
        self.assertIn(f'(<../../_global/notes/{name}>)', self.ids('demo')[c['id']]['body'])

    def test_a_stale_promote_plan_is_refused(self):
        a = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'demo', self.root)
        proposed = promote_plan(self.root, ['demo:' + a['id']])
        code, _ = self.cli('--domain', 'demo', 'update', 'demo:' + a['id'], '--body', 'Changed since the plan.')
        self.assertEqual(code, 0)
        with self.assertRaisesRegex(ValueError, '(?i)stale'):
            apply(self.root, proposed['plan_id'], proposed['digest'])

    def test_obsidian_copies_follow_a_promotion(self):
        from core.sync import build_overlay
        vault = self.base / 'vault'
        a = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'demo', self.root)
        build_overlay('demo', vault, self.root);build_overlay('_global', vault, self.root)
        copies = lambda domain: sorted(p.name for p in (vault / 'Domains' / domain).glob('*.md')) if (vault / 'Domains' / domain).is_dir() else []
        self.assertEqual(copies('demo'), [Path(a['path']).name])
        proposed = promote_plan(self.root, ['demo:' + a['id']]);apply(self.root, proposed['plan_id'], proposed['digest'])
        build_overlay('demo', vault, self.root);build_overlay('_global', vault, self.root)
        self.assertEqual((copies('demo'), copies('_global')), ([], [Path(a['path']).name]))

    def test_global_notes_cannot_be_promoted_again(self):
        g = capture('Prefer forward slashes', 'Use forward slashes.', TAGS, '_global', self.root)
        with self.assertRaisesRegex(ValueError, 'already in the global domain'):
            promote_plan(self.root, ['_global:' + g['id']])
