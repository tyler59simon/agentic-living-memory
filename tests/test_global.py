"""The global domain: --global capture, duplicates across domains, automatic and reviewed promotion."""
import contextlib
import io
import json
from pathlib import Path

from test_core import StoreFixture
from core import runtime, session_start
from core.cli import doctor
from revision_fill import main
from core.common import canonical_domain, load_bindings
from core.governance import undo, promote, promote_suggest
from core.init_domain import init_domain
from core.migration import migrate_schema
from core.notes import capture, near_identical
from core.repository import inventory, read_json

TAGS = ['subject/project', 'concern/decision']



def at(root, domain, ref_id):
    """DOMAIN:REF_ID@REVISION, the revision read from the note now (merge, remap and promote require one)."""
    from core.notes import find_note
    return f'{domain}:{ref_id}@' + find_note(ref_id, domain, root)[1]['revision']

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
        return undo(self.root, operation_id)   # these operations are direct kinds: the undo runs at once


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
        self.assertIn('promote demo:' + out['id'], out['scope_hint']['next'])
        self.assertNotIn('scope_hint', capture('Prefer tabs', 'Tabs.', TAGS, '_global', self.root, type='preference'))


class Bindings(GlobalFixture):
    def test_migration_points_a_folderless_global_at__global(self):
        config = load_bindings(self.root);config['search_scope']['global_domains'] = ['global']
        (self.root / 'bindings.json').write_text(json.dumps(config), encoding='utf-8')
        self.assertTrue(any("names 'global'" in w for w in doctor(self.root, str(self.base / 'project'))['warnings']))
        migrate_schema(self.root)
        config = load_bindings(self.root)
        self.assertEqual(config['search_scope']['global_domains'], ['_global'])
        self.assertEqual(canonical_domain('global', self.root), '_global')
        self.assertFalse(any('global_domains' in w for w in doctor(self.root, str(self.base / 'project'))['warnings']))


    def test_doctor_warns_about_a_bound_folder_that_no_longer_exists(self):
        project = str(self.base / 'project');(self.base / 'project').mkdir(exist_ok=True)
        self.assertFalse([w for w in doctor(self.root, project)['warnings'] if w.startswith('bindings:')])
        init_domain('moved', [str(self.base / 'gone')], self.root)
        warned = [w for w in doctor(self.root, project)['warnings'] if w.startswith('bindings:')]
        self.assertEqual(len(warned), 1);self.assertIn("domain 'moved'", warned[0]);self.assertIn('bind moved NEW_PATH', warned[0])   # BIND-RENAME
        old = str((self.base / 'gone').resolve());self.assertIn(f'unbind moved --prefix {old}', warned[0])
        (self.base / 'moved-here').mkdir();self.assertEqual(self.cli('bind', 'moved', str(self.base / 'moved-here'))[0], 0)
        code, out = self.cli('unbind', 'moved', '--prefix', old);self.assertEqual((code, out['status'], out['prefix']), (0, 'prefix_removed', old))
        self.assertFalse([w for w in doctor(self.root, project)['warnings'] if w.startswith('bindings:')])
        self.assertEqual(load_bindings(self.root)['domains']['moved']['prefixes'], [str((self.base / 'moved-here').resolve())])

    def test_unbind_prefix_matches_expanded_case_folded_and_legacy_folders(self):
        import os, sys
        (self.base / 'keep').mkdir();os.environ['ALM_TEST_BASE'] = str(self.base);self.addCleanup(os.environ.pop, 'ALM_TEST_BASE', None)
        config = load_bindings(self.root)
        config['domains']['legacy'] = {'prefixes': ['$ALM_TEST_BASE/old-a', str(self.base / 'keep')], 'root': str(self.base / 'old-b')}
        (self.root / 'bindings.json').write_text(json.dumps(config), encoding='utf-8')
        warned = [w for w in doctor(self.root, str(self.base))['warnings'] if "domain 'legacy'" in w];self.assertEqual(len(warned), 2)
        for old in ('old-a', 'old-b'):
            folder = str((self.base / old).resolve())
            spelled = folder.upper() if sys.platform in ('win32', 'darwin') else folder   # Windows and macOS paths are case-insensitive
            code, out = self.cli('unbind', 'legacy', '--prefix', spelled);self.assertEqual((code, out['status']), (0, 'prefix_removed'), out)
        entry = load_bindings(self.root)['domains']['legacy']
        self.assertEqual(entry, {'prefixes': [str(self.base / 'keep')]})   # the legacy root field is gone, not left behind
        self.assertFalse([w for w in doctor(self.root, str(self.base))['warnings'] if "domain 'legacy'" in w])

    def test_rebinding_keeps_legacy_binding_keys_and_the_old_folders_still_resolve(self):   # STO-4
        from core.common import resolve_domain
        config = load_bindings(self.root)
        config['domains']['old-paths'] = {'paths': [str(self.base / 'paths-a'), str(self.base / 'paths-b')], 'note': 'kept'}
        config['domains']['old-root'] = {'root': str(self.base / 'root-a')}
        config['domains']['old-path'] = {'path': str(self.base / 'path-a')}
        (self.root / 'bindings.json').write_text(json.dumps(config), encoding='utf-8')
        for name, old in (('old-paths', 'paths-a'), ('old-paths', 'paths-b'), ('old-root', 'root-a'), ('old-path', 'path-a')):
            self.assertEqual(resolve_domain(self.base / old, load_bindings(self.root)), name)
        for name, new in (('old-paths', 'paths-new'), ('old-root', 'root-new'), ('old-path', 'path-new')):
            self.assertEqual(self.cli('bind', name, str(self.base / new))[0], 0)
        domains = load_bindings(self.root)['domains']
        self.assertEqual(domains['old-paths']['paths'], [str(self.base / 'paths-a'), str(self.base / 'paths-b')]);self.assertEqual(domains['old-paths']['note'], 'kept')
        self.assertEqual(domains['old-paths']['prefixes'], sorted(str(self.base / n) for n in ('paths-a', 'paths-b', 'paths-new')))   # the old paths folders are carried into prefixes
        self.assertEqual(domains['old-root']['root'], str(self.base / 'root-a'));self.assertEqual(domains['old-path']['path'], str(self.base / 'path-a'))
        for name, old, new in (('old-paths', 'paths-a', 'paths-new'), ('old-paths', 'paths-b', 'paths-new'), ('old-root', 'root-a', 'root-new'), ('old-path', 'path-a', 'path-new')):
            self.assertEqual(resolve_domain(self.base / old, domains), name, f'{old} stopped resolving after re-bind')
            self.assertEqual(resolve_domain(self.base / new, domains), name)

    def test_rebinding_an_entry_with_both_keys_keeps_the_shadowed_paths_folder_inactive(self):   # STO-4.1
        from core.common import resolve_domain
        config = load_bindings(self.root)
        config['domains']['both'] = {'prefixes': [str(self.base / 'new')], 'paths': [str(self.base / 'old')]}   # `prefixes` shadows `paths`
        (self.root / 'bindings.json').write_text(json.dumps(config), encoding='utf-8')
        self.assertIsNone(resolve_domain(self.base / 'old', load_bindings(self.root)))
        self.assertEqual(self.cli('bind', 'both', str(self.base / 'extra'))[0], 0)
        domains = load_bindings(self.root)['domains']
        self.assertEqual(domains['both']['prefixes'], sorted(str(self.base / n) for n in ('new', 'extra')));self.assertEqual(domains['both']['paths'], [str(self.base / 'old')])
        self.assertIsNone(resolve_domain(self.base / 'old', domains), 'the shadowed paths folder resolves again after re-bind')
        self.assertEqual(resolve_domain(self.base / 'extra', domains), 'both')

    def test_unbind_prefix_refuses_an_unknown_folder_and_the_last_folder(self):
        capture('Keep me', 'A note in the domain.', TAGS, 'demo', self.root)
        only = load_bindings(self.root)['domains']['demo']['prefixes']
        code, out = self.cli('unbind', 'demo', '--prefix', str(self.base / 'elsewhere'));self.assertEqual(code, 2);self.assertIn('is not bound to', out['error']['message'])
        if len(only) == 1:
            code, out = self.cli('unbind', 'demo', '--prefix', only[0]);self.assertEqual(code, 2);self.assertIn('last folder', out['error']['message'])
        self.assertEqual(load_bindings(self.root)['domains']['demo']['prefixes'], only)
        for actor in ('subagent', 'viewer'):self.assertEqual(self.cli('--actor', actor, 'unbind', 'demo', '--prefix', only[0])[0], 2)


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

    def allow_project_tag(self):
        path = self.root / 'tag_taxonomy.json';taxonomy = json.loads(path.read_text())
        path.write_text(json.dumps({**taxonomy, 'tags': taxonomy['tags'] + ['project/other']}))

    def seeded(self, title='Codex review needs a full-repo brief', body='Codex review covers only the diff.', **extra):
        self.other();(self.base / 'other').mkdir(exist_ok=True);self.allow_project_tag();(self.base / 'other' / 'x.py').write_text('x=1', encoding='utf-8')
        return capture(title, body, TAGS + ['project/other'], 'other', self.root, **extra)

    def assertCleaned(self, note, original):
        self.assertFalse([t for t in note['tags'] if t.startswith('project/')])
        for key in ('files', 'repo', 'file_hashes'):self.assertNotIn(key, note)
        self.assertEqual(set(note['tags']), set(original['tags']) - {'project/other'});self.assertEqual(note['body'], original['body'])

    def test_a_promoted_copy_drops_project_tags_and_files_and_undo_restores_them(self):
        first = self.seeded(files=['x.py']);original = self.ids('other')[first['id']]
        self.assertTrue(original['files'] and original['file_hashes'] and 'project/other' in original['tags'])
        out = capture('Codex review needs a full-repo brief', 'Codex review covers only the diff.', TAGS, 'demo', self.root)
        self.assertEqual(out['status'], 'promoted');self.assertIn('project tags and files were dropped', out['hint'])
        self.assertCleaned(self.ids('_global')[first['id']], original)
        self.undo(out['operation_id'])
        restored = self.ids('other')[first['id']]
        self.assertEqual((restored['tags'], restored['files'], restored['file_hashes']), (original['tags'], original['files'], original['file_hashes']))

    def test_a_near_identical_promoted_copy_is_cleaned_too(self):
        body = 'Codex review only covers the diff, so a whole-repo review needs codex exec with a full-repo brief and read-only sandbox.'
        first = self.seeded(body=body, files=['x.py']);original = self.ids('other')[first['id']]
        out = capture('Codex review needs a full repo brief', body.replace('read-only sandbox', 'a read-only sandbox'), TAGS, 'demo', self.root)
        self.assertEqual(out['status'], 'promoted');self.assertCleaned(self.ids('_global')[first['id']], original)

    def test_a_note_naming_a_project_path_stays_put(self):
        body = f"Run the script at {(self.base / 'other' / 'x.py').as_posix().upper()} first."
        first = self.seeded(body=body)
        with self.assertRaisesRegex(ValueError, 'other:' + first['id'] + '.*names a project path'):
            capture('Codex review needs a full-repo brief', body, TAGS, 'demo', self.root)
        self.assertIn(first['id'], self.ids('other'));self.assertEqual(self.ids('demo'), {});self.assertFalse((self.root / 'domains/_global/notes').is_dir())

    def test_a_project_path_ending_a_sentence_counts_but_a_sibling_folder_does_not(self):
        from core.notes import promotion_blocker
        self.other();folder = (self.base / 'other').resolve().as_posix()
        for body, blocked in ((f'The checkout is {folder}.', True), (f'The checkout is {folder}', True), (f'Run {folder}/x.py, then stop.', True),
                              (f'See `{folder}`.', True), (f'Use {folder}-old/x.py.', False), (f'Use {folder}.old/x.py.', False)):
            with self.subTest(body=body):self.assertEqual(promotion_blocker({'body': body}, self.root) is not None, blocked)

    def test_digest_and_map_notes_stay_put(self):
        from core.digests import describe
        self.other();(self.base / 'other').mkdir(exist_ok=True);(self.base / 'other' / 'doc.md').write_text('# Doc', encoding='utf-8')
        d = capture('Doc digest', 'Summary of the doc.', TAGS, 'other', self.root, sources=[describe(self.root, 'other', 'doc.md')])
        with self.assertRaisesRegex(ValueError, 'other:' + d['id'] + '.*digest'):
            capture('Doc digest', 'Summary of the doc.', TAGS, 'demo', self.root, allow_similar=True)
        m = capture('Layout', 'Where things live.', TAGS, 'other', self.root, type='map', files=['.'])
        with self.assertRaisesRegex(ValueError, 'other:' + m['id'] + '.*map'):
            capture('Layout', 'Where things live.', TAGS, 'demo', self.root, type='map', files=['.'])
        self.assertEqual(self.ids('demo'), {})

    def test_promote_cleans_the_same_way_and_refuses_the_same_notes(self):
        first = self.seeded(files=['x.py']);original = self.ids('other')[first['id']]
        promote(self.root, [at(self.root, 'other', first['id'])])
        self.assertCleaned(self.ids('_global')[first['id']], original)
        pathy = capture('Run the script', f"Run {self.base / 'other' / 'x.py'}.", TAGS, 'other', self.root)
        with self.assertRaisesRegex(ValueError, 'names a project path'):
            promote(self.root, [at(self.root, 'other', pathy['id'])])
        m = capture('Layout', 'Where things live.', TAGS, 'other', self.root, type='map', files=['.'])
        with self.assertRaisesRegex(ValueError, 'map'):
            promote(self.root, [at(self.root, 'other', m['id'])])

    def test_a_merely_similar_note_in_an_unrelated_project_is_refused_with_promote(self):
        self.other()
        first = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'other', self.root)
        with self.assertRaisesRegex(ValueError, 'promote other:' + first['id']):
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


class GlobalEverywhere(GlobalFixture):
    def test_open_work_includes_global_notes(self):
        code, out = self.cli('--domain', 'demo', 'capture', '--global', '--type', 'plan', '--concern', 'decision', '--title', 'Laptop webcam follow-up',
                             '--summary', 'Check the camera registry keys.', '--action', 'Export the Device Parameters key')
        self.assertEqual(code, 0)
        code, listed = self.cli('--domain', 'demo', 'open')
        self.assertEqual([i['item'] for i in listed['items']], ['Export the Device Parameters key'])
        self.assertIn('Export the Device Parameters key', session_start.build_context(str(self.base / 'project'), self.root)[0])

    def test_a_global_rule_is_shown_in_full_in_every_bound_domain(self):
        self.other()
        code, out = self.cli('--domain', 'demo', 'capture', '--global', '--type', 'preference', '--subject', 'project', '--concern', 'decision', '--rule',
                             '--title', 'No session URL in pull requests', '--summary', 'Pull request descriptions never carry a Claude session URL.')
        self.assertEqual((code, out['domain']), (0, '_global'), out)
        for cwd in (self.base / 'project', self.base / 'other'):
            text, shown, _ = session_start.build_context(str(cwd), self.root, session_start.MIN_BUDGET)
            self.assertIn(f"_global:{out['id'][:8]} No session URL in pull requests — Pull request descriptions never carry a Claude session URL.", text)
            self.assertIn('_global:' + out['id'], shown)

    def test_search_does_not_bury_a_global_match_under_weak_local_hits(self):
        for word in ('amber', 'basil', 'cedar', 'delta', 'ember', 'fjord', 'garnet', 'harbor'):
            capture(f'{word.title()} notes', f'About {word}; mentions a webcam once.', TAGS, 'demo', self.root)
        g = capture('Webcam diagnosis conclusion', 'The webcam diagnosis points to hardware.', TAGS, '_global', self.root)
        code, out = self.cli('--domain', 'demo', 'search', 'webcam diagnosis', '--limit', '3')
        self.assertEqual((code, out['relevant']), (0, True))
        self.assertEqual(out['results'][0]['id'], g['id'])
        self.assertEqual(out['results'][0]['sourceScope'], 'global')

    def test_a_global_note_ranks_like_a_local_one_among_relevant_hits(self):
        # Three relevant local notes must not push the best global match out of the three injected.
        for topic in ('branch', 'pull request', 'diff'):
            capture(f'Codex review of a {topic}', f'Codex can review a {topic} when asked.', TAGS, 'demo', self.root, allow_similar=True)
        g = capture('Claude calling Codex for code review', 'Run codex exec for whole-repo code review with a brief.', TAGS, '_global', self.root)
        _, ids, _ = runtime.context(str(self.base / 'project'), 'can codex do a code review of this?', self.root, 'demo')
        self.assertIn('_global:' + g['id'], ids)

    def test_search_answers_even_when_nothing_is_relevant_and_all_scope_includes_global(self):
        capture('Amber notes', 'About amber; mentions a webcam once.', TAGS, 'demo', self.root)
        g = capture('Prefer forward slashes', 'Use forward slashes in shell paths.', TAGS, '_global', self.root)
        code, out = self.cli('--domain', 'demo', 'search', 'webcam color calibration profile', '--limit', '3')
        self.assertEqual((code, out['relevant']), (0, False))
        self.assertTrue(out['results']);self.assertIn('hint', out)
        code, out = self.cli('--domain', 'demo', 'search', 'forward slashes', '--scope', 'all')
        self.assertIn('_global', out['domains']);self.assertEqual(out['results'][0]['id'], g['id'])


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
        return len(list((self.root / folder).glob('*/journal.json')))

    def test_suggest_lists_candidates_and_writes_nothing(self):
        self.other()
        a = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'other', self.root)
        b = capture('Deploy using install.py', 'Run install.py --plan and then --apply-plan.', TAGS, 'demo', self.root, allow_similar=True)
        p = capture('Prefer short commit messages', 'Keep subjects short.', TAGS, 'demo', self.root, type='preference')
        before = self.count('.operations')
        rows = {r['ref']: r for r in promote_suggest(self.root)['candidates']}
        self.assertEqual(self.count('.operations'), before)
        self.assertIn('also in project demo', rows['other:' + a['id'][:8]]['reasons'][0])
        self.assertIn('also in project other', rows['demo:' + b['id'][:8]]['reasons'][0])
        self.assertIn('preference', rows['demo:' + p['id'][:8]]['reasons'][0])
        self.assertEqual({r['domain'] for r in promote_suggest(self.root, 'other')['candidates']}, {'other'})

    def test_promote_moves_notes_merges_twins_and_follows_links(self):
        self.other()
        a = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'other', self.root)
        b = capture('Deploy using install.py', 'Run install.py --plan and then --apply-plan.', TAGS, 'demo', self.root, allow_similar=True)
        name = Path(b['path']).name
        c = capture('Release checklist', f'Deploy as in [the deploy note](<{name}>).', TAGS, 'demo', self.root)
        done = promote(self.root, [at(self.root, 'other', a['id']), at(self.root, 'demo', b['id'])]);self.assertEqual((done['kind'], done['operation_id'][:2]), ('promote', 'd-'))
        active, everything = self.ids('_global'), self.ids('_global', retired=True)
        self.assertEqual(set(active), {b['id']});self.assertEqual(everything[a['id']]['superseded_by'], b['id'])
        self.assertEqual(active[b['id']]['supersedes'], [a['id']])
        self.assertIn(f'(<../../_global/notes/{name}>)', self.ids('demo')[c['id']]['body'])

    def test_a_promotion_is_undone_directly_unless_a_later_edit_conflicts(self):
        a = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'demo', self.root)
        done = promote(self.root, [at(self.root, 'demo', a['id'])])
        code, _ = self.cli('--domain', '_global', 'update', '_global:' + a['id'], '--body', 'Changed since the promotion.')
        self.assertEqual(code, 0)
        with self.assertRaisesRegex(ValueError, 'Undo conflicts with later edits'):
            undo(self.root, done['operation_id'])

    def test_obsidian_copies_follow_a_promotion(self):
        from core.sync import build_overlay
        vault = self.base / 'vault'
        a = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'demo', self.root)
        build_overlay('demo', vault, self.root);build_overlay('_global', vault, self.root)
        copies = lambda domain: sorted(p.name for p in (vault / 'Domains' / domain).glob('*.md')) if (vault / 'Domains' / domain).is_dir() else []
        self.assertEqual(copies('demo'), [Path(a['path']).name])
        promote(self.root, [at(self.root, 'demo', a['id'])])
        build_overlay('demo', vault, self.root);build_overlay('_global', vault, self.root)
        self.assertEqual((copies('demo'), copies('_global')), ([], [Path(a['path']).name]))

    def test_an_old_ref_still_finds_a_promoted_note(self):
        from core.notes import resolve_ref
        a = capture('Deploy with install.py', 'Use install.py to deploy.', TAGS, 'demo', self.root)
        promote(self.root, [at(self.root, 'demo', a['id'])])
        self.assertEqual(resolve_ref('demo:' + a['id'][:8], 'demo', self.root), ('_global', a['id']))
        code, out = self.cli('--domain', 'demo', 'read', 'demo:' + a['id'][:8])
        self.assertEqual((code, out['domain']), (0, '_global'))

    def test_global_notes_cannot_be_promoted_again(self):
        g = capture('Prefer forward slashes', 'Use forward slashes.', TAGS, '_global', self.root)
        with self.assertRaisesRegex(ValueError, 'already in the global domain'):
            promote(self.root, [at(self.root, '_global', g['id'])])
