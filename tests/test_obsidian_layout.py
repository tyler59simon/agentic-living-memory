import json
from pathlib import Path

from test_core import StoreFixture
from core.governance import apply
from core.notes import find_note
from core.note_types import NOTE_TYPES
from core.repository import parse_text, read_json
from core.sync import sync, build_overlay, teardown_plan


class ObsidianLayout(StoreFixture):
    def vault(self):
        return self.base / 'vault'

    def test_top_level_layout(self):
        a = self.note()
        vault = self.vault()
        result = build_overlay('demo', vault, self.root)
        self.assertEqual(result['templates'], len(NOTE_TYPES))
        top = {p.name for p in vault.iterdir() if not p.name.startswith('.')}
        self.assertEqual(top, {'Dashboards', 'Domains', 'Canvas', 'Templates'})
        self.assertTrue((vault / 'Domains/demo' / Path(a['path']).name).exists())
        self.assertEqual({p.stem for p in (vault / 'Dashboards').iterdir()},
                         {'Home', 'Taxonomy', 'Stale Notes', 'Timeline', 'Domain Overview', 'Archive', 'Sync Status - demo'})
        overview = (vault / 'Dashboards/Domain Overview.md').read_text(encoding='utf-8')
        self.assertIn('[[Domains/demo/' + Path(a['path']).stem + '|', overview)
        canvas = json.loads((vault / 'Canvas/demo.canvas').read_text(encoding='utf-8'))
        self.assertEqual(canvas['nodes'][0]['file'], 'Domains/demo/' + Path(a['path']).name)
        self.assertTrue((vault / '.living-memory/generated.json').exists())
        self.assertTrue((vault / '.living-memory/copies.json').exists())

    def test_templates_are_not_scanned_and_customizations_survive(self):
        self.note()
        vault = self.vault()
        build_overlay('demo', vault, self.root)
        custom = vault / 'Templates/Decision.md'
        custom.write_text(custom.read_text(encoding='utf-8') + '\nMy extra prompt\n',encoding='utf-8')
        result = build_overlay('demo', vault, self.root)
        self.assertEqual(result['plans'], [])
        self.assertIn('My extra prompt', custom.read_text(encoding='utf-8'))

    def test_template_note_without_id_is_captured(self):
        self.note()
        vault = self.vault()
        build_overlay('demo', vault, self.root)
        text = (vault / 'Templates/Decision.md').read_text(encoding='utf-8').replace('{{title}}', 'Chose top-level folders')
        text = text.replace('"concern/knowledge"', '"concern/decision"')
        text = text.replace('The choice, who made it and when.', 'Top-level folders keep the vault easy to browse.')
        incoming = vault / 'Domains/demo/Chose top-level folders.md'
        incoming.write_text(text,encoding='utf-8')
        result = sync('demo', vault, self.root)
        self.assertEqual(result['plans'], [])
        self.assertEqual(len(result['applied']), 1)
        self.assertFalse(incoming.exists())
        notes_dir = self.root / 'domains/demo/notes'
        created = [p for p in notes_dir.iterdir() if p.name.startswith('DECISION - chose-top-level-folders')]
        self.assertEqual(len(created), 1)
        self.assertTrue((vault / 'Domains/demo' / created[0].name).exists())
        again = sync('demo', vault, self.root)
        self.assertEqual((again['plans'], again['applied'], again['conflicts']), ([], [], 0))

    def test_yaml_block_lists_from_property_editor(self):
        raw = '---\ntitle: "T"\ntags:\n  - subject/project\n  - concern/decision\ncontext:\ntype: "decision"\n---\n\nBody\n'
        note = parse_text(raw)
        self.assertEqual(note['tags'], ['subject/project', 'concern/decision'])
        self.assertEqual(note['context'], '')
        self.assertEqual(note['type'], 'decision')

    def test_legacy_layout_migrates_without_canonical_deletes(self):
        a = self.note()
        vault = self.vault()
        name = Path(a['path']).name
        # Recreate what the previous release built.
        legacy_note = vault / 'Living Memory/domains/demo/notes' / name
        legacy_note.parent.mkdir(parents=True)
        legacy_note.write_text(Path(a['path']).read_text(encoding='utf-8'),encoding='utf-8')
        first = sync('demo', vault, self.root)
        self.assertEqual(first['plans'], [])
        state_path = self.root / 'domains/demo/sync-state.json'
        state = read_json(state_path)
        for entry in state['entries'].values():
            entry['overlay'] = 'Living Memory/domains/demo/notes/' + name
        state_path.write_text(json.dumps(state),encoding='utf-8')
        copies = read_json(vault / '.living-memory/copies.json')
        (vault / 'Living Memory/.copies.json').write_text(json.dumps({'files': {'Living Memory/domains/demo/notes/' + name: copies['files']['Domains/demo/' + name]}}),encoding='utf-8')
        (vault / '.living-memory/copies.json').unlink()
        (vault / 'Domains/demo' / name).unlink()
        legacy_home = vault / 'Living Memory/Home.md'
        legacy_home.write_bytes(b'# old\n')
        edited = vault / 'Living Memory/Timeline.md'
        edited.write_bytes(b'# user edit\n')
        from core.repository import digest
        (vault / 'Living Memory/.generated.json').write_text(json.dumps({'files': {
            'Living Memory/Home.md': digest(b'# old\n'), 'Living Memory/Timeline.md': digest(b'# original\n')}}),encoding='utf-8')

        result = build_overlay('demo', vault, self.root)
        self.assertEqual(result['plans'], [])
        self.assertEqual(result['conflicts'], 0)
        self.assertEqual(result['legacy_conflicts'], ['Living Memory/Timeline.md'])
        self.assertTrue((vault / 'Domains/demo' / name).exists())
        self.assertFalse(legacy_note.exists())
        self.assertFalse(legacy_home.exists())
        self.assertTrue(edited.exists())
        self.assertFalse((vault / 'Living Memory/.generated.json').exists())
        self.assertFalse((vault / 'Living Memory/.copies.json').exists())
        self.assertFalse((vault / 'Living Memory/domains').exists())
        self.assertTrue(find_note(a['id'], 'demo', self.root))

    def obsidian_reformat(self, path, move=False):
        data = json.loads(path.read_text(encoding='utf-8'))
        for e in data['edges']:
            e.update(fromSide='right', toSide='left')
        if move:
            data['nodes'][0]['x'] += 37
        path.write_text(json.dumps(data, indent='\t'),encoding='utf-8')

    def test_canvas_reformatted_by_obsidian_still_regenerates(self):
        self.note()
        vault = self.vault()
        build_overlay('demo', vault, self.root)
        canvas = vault / 'Canvas/demo.canvas'
        self.obsidian_reformat(canvas)
        self.note('Second', 'Another architecture note')
        result = build_overlay('demo', vault, self.root)
        self.assertEqual(result['view_conflicts'], [])
        self.assertEqual(len(json.loads(canvas.read_text(encoding='utf-8'))['nodes']), 2)

    def test_canvas_with_user_layout_is_preserved_without_failing(self):
        self.note()
        vault = self.vault()
        build_overlay('demo', vault, self.root)
        canvas = vault / 'Canvas/demo.canvas'
        self.obsidian_reformat(canvas, move=True)
        before = canvas.read_text(encoding='utf-8')
        self.note('Second', 'Another architecture note')
        result = build_overlay('demo', vault, self.root)
        self.assertEqual(result['view_conflicts'], ['Canvas/demo.canvas'])
        self.assertEqual(canvas.read_text(encoding='utf-8'), before)
        self.assertIn('Second', (vault / 'Dashboards/Domain Overview.md').read_text(encoding='utf-8'))

    def test_legacy_canvas_reformatted_by_obsidian_is_removed(self):
        self.note()
        vault = self.vault()
        build_overlay('demo', vault, self.root)
        legacy = vault / 'Living Memory/demo.canvas'
        legacy.parent.mkdir()
        legacy.write_bytes((vault / 'Canvas/demo.canvas').read_bytes())
        from core.repository import digest
        (vault / 'Living Memory/.generated.json').write_text(json.dumps({'files': {'Living Memory/demo.canvas': digest(legacy.read_bytes())}}),encoding='utf-8')
        self.obsidian_reformat(legacy)
        result = build_overlay('demo', vault, self.root)
        self.assertEqual(result['legacy_conflicts'], [])
        self.assertFalse((vault / 'Living Memory').exists())

    def test_teardown_accepts_obsidian_reformatted_canvas(self):
        self.note()
        vault = self.vault()
        build_overlay('demo', vault, self.root)
        self.obsidian_reformat(vault / 'Canvas/demo.canvas')
        proposal = teardown_plan(vault)
        apply(vault, proposal['plan_id'], proposal['digest'])
        self.assertFalse((vault / 'Canvas/demo.canvas').exists())

    def test_teardown_removes_everything_managed(self):
        self.note()
        vault = self.vault()
        build_overlay('demo', vault, self.root)
        proposal = teardown_plan(vault)
        apply(vault, proposal['plan_id'], proposal['digest'])
        leftover = [p for p in vault.rglob('*') if p.is_file() and not p.relative_to(vault).parts[0].startswith('.')]
        self.assertEqual(leftover, [])
