"""#13 export bundles and reviewed import."""
import contextlib, io, json, unittest, zipfile
from test_core import StoreFixture
from core.cli import main
from core.governance import apply
from core.init_domain import init_domain
from core.notes import capture, change, find_note
from core.repository import inventory

TAGS = ['subject/project', 'concern/decision']


class Bundles(StoreFixture):
    def setUp(self):
        super().setUp()
        self.a = capture('Cache eviction policy', 'LRU eviction.', TAGS, 'demo', self.root, type='decision')
        self.b = capture('Webcam conclusion', 'Hardware fault.', TAGS, 'demo', self.root)
        old = capture('Old deploy note', 'Use the old script.', TAGS, 'demo', self.root)
        self.c = capture('Deploy with install.py', 'Use install.py.', TAGS, 'demo', self.root, supersedes=['demo:' + old['id']])
        self.old = old
        self.other = self.base / 'store2'
        init_domain('demo', [str(self.base / 'project2')], self.other)
        (self.other / 'tag_taxonomy.json').write_text(json.dumps({'tags': TAGS + ['concern/architecture']}))

    def cli(self, root, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(root), '--domain', 'demo', *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def export(self, name='bundle.zip', *extra):
        target = self.base / name
        code, result = self.cli(self.root, 'export', '--out', str(target), *extra)
        self.assertEqual(code, 0, result)
        return target, result

    def test_round_trip_into_a_second_store(self):
        bundle, result = self.export('all.zip', '--all')
        self.assertEqual(result['notes'], 4)
        with zipfile.ZipFile(bundle) as z:
            names = set(z.namelist())
        self.assertIn('manifest.json', names)
        self.assertTrue(any(n.startswith('archive/') for n in names))
        code, plan = self.cli(self.other, 'import-plan', str(bundle))
        self.assertEqual(code, 0, plan)
        self.assertEqual(len(plan['created']), 4)
        apply(self.other, plan['plan_id'], plan['digest'])
        mine = {n['id']: find_note(n['id'], 'demo', self.root)[0].read_bytes() for n in inventory('demo', self.root, retired=True)}
        theirs = {n['id']: find_note(n['id'], 'demo', self.other)[0].read_bytes() for n in inventory('demo', self.other, retired=True)}
        self.assertEqual(mine.keys(), theirs.keys())
        for note_id in mine:
            self.assertEqual(mine[note_id].replace(b'\r\n', b'\n'), theirs[note_id].replace(b'\r\n', b'\n'))
        self.assertEqual(find_note(self.old['id'], 'demo', self.other)[1]['status'], 'superseded')
        # Re-import is a no-op.
        code, again = self.cli(self.other, 'import-plan', str(bundle))
        self.assertEqual((again['plan_id'], len(again['unchanged']), again['created']), (None, 4, []))

    def test_filters_and_refusals(self):
        _, result = self.export('decisions.zip', '--type', 'decision')
        self.assertEqual(result['notes'], 1)
        _, result = self.export('active.zip')
        self.assertEqual(result['notes'], 3)  # retired notes only with --all
        self.assertEqual(self.cli(self.root, 'export', '--out', str(self.base / 'active.zip'))[0], 2)  # exists
        self.assertEqual(self.cli(self.root, 'export', '--out', str(self.base / 'x.zip'), '--tag', 'concern/bug')[0], 2)  # none match
        self.assertEqual(self.cli(self.root, 'export', '--out', str(self.root / 'domains' / 'demo' / 'b.zip'))[0], 2)
        self.assertEqual(self.cli(self.root, '--actor', 'subagent', 'export', '--out', str(self.base / 's.zip'))[0], 2)

    def test_conflicts_are_reported_not_written(self):
        bundle, _ = self.export()
        code, plan = self.cli(self.other, 'import-plan', str(bundle))
        apply(self.other, plan['plan_id'], plan['digest'])
        change(self.a['id'], 'demo', 'update', self.root, body='LRU eviction with a TTL.')
        newer, _ = self.export('newer.zip')
        code, result = self.cli(self.other, 'import-plan', str(newer))
        self.assertEqual(code, 0, result)
        self.assertEqual([c['ref'] for c in result['conflicts']], ['demo:' + self.a['id']])
        self.assertEqual(len(result['unchanged']), 2)
        self.assertIsNone(result['plan_id'])
        self.assertIn('LRU eviction.', find_note(self.a['id'], 'demo', self.other)[1]['body'])

    def test_into_another_domain(self):
        init_domain('team', [str(self.base / 'team')], self.other)
        bundle, _ = self.export()
        code, plan = self.cli(self.other, '--domain', 'team', 'import-plan', str(bundle))
        self.assertEqual(code, 0, plan)
        apply(self.other, plan['plan_id'], plan['digest'])
        self.assertEqual({n['domain'] for n in inventory('team', self.other)}, {'team'})

    def test_taxonomy_gap_names_the_plan(self):
        capture('Architecture note', 'Layers.', ['subject/project', 'concern/architecture'], 'demo', self.root)
        (self.other / 'tag_taxonomy.json').write_text(json.dumps({'tags': TAGS}))
        bundle, _ = self.export()
        code, result = self.cli(self.other, 'import-plan', str(bundle))
        self.assertEqual(code, 2)
        self.assertIn('taxonomy-plan concern/architecture', result['error']['message'])

    def tampered(self, name, edit):
        bundle, _ = self.export('source-' + name)
        with zipfile.ZipFile(bundle) as z:
            members = {n: z.read(n) for n in z.namelist()}
        edit(members)
        target = self.base / name
        with zipfile.ZipFile(target, 'w') as z:
            for n, data in members.items():
                z.writestr(n, data)
        return self.cli(self.other, 'import-plan', str(target))

    def test_rejects_traversal_unknown_members_and_tampering(self):
        code, result = self.tampered('trav.zip', lambda m: m.update({'notes/../../bindings.md': b'x'}))
        self.assertEqual(code, 2)
        self.assertIn('outside the bundle layout', result['error']['message'])
        code, result = self.tampered('extra.zip', lambda m: m.update({'readme.txt': b'hi'}))
        self.assertIn('outside the bundle layout', result['error']['message'])
        def change_note(m):
            name = next(n for n in m if n.startswith('notes/'))
            m[name] = m[name].replace(b'LRU', b'MRU').replace(b'Hardware', b'Software')
        code, result = self.tampered('edit.zip', change_note)
        self.assertIn('changed after export', result['error']['message'])
        code, result = self.tampered('nomanifest.zip', lambda m: m.pop('manifest.json'))
        self.assertIn('no manifest', result['error']['message'])

    def test_unbound_target_domain_fails(self):
        bundle, _ = self.export()
        code, result = self.cli(self.other, '--domain', 'nowhere', 'import-plan', str(bundle))
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
