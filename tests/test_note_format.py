import contextlib
import io
import json
import unittest
from pathlib import Path

from test_core import StoreFixture
from core import note_format as nf
from core.cli import main as cli
from core.notes import capture, change, find_note, quality_warnings
from core.repository import parse_text, serialize
from core.search import snippet
from core.sync import build_overlay

SUMMARY = '## 📌 Executive Summary\n'
TAGS = ['subject/project', 'concern/architecture']


class Layout(unittest.TestCase):
    def test_plain_text_becomes_the_summary(self):
        self.assertEqual(nf.format_body('The cache is disposable.'), SUMMARY + 'The cache is disposable.')

    def test_format_is_idempotent_and_keeps_the_reference_layout(self):
        body = nf.render('Short summary.', ['**Cause:** stale key', 'nested\n  * sub'], ['[[Other note]]'],
                         ['Fix it'], ['Found it'], 'Back up the key first.')
        self.assertEqual(nf.format_body(body), body)
        self.assertEqual(body, SUMMARY + 'Short summary.\n\n---\n\n'
                         '## 📝 Key Details\n* **Cause:** stale key\n* nested\n  * sub\n\n'
                         '## 🗂️ Resource Links\n* [[Other note]]\n\n'
                         '## 🟩 Action Items\n- [ ] Fix it\n- [x] Found it\n\n'
                         '> ℹ️ **Important Note:**\n> Back up the key first.')

    def test_empty_sections_are_left_out(self):
        body = SUMMARY + 'Kept.\n\n---\n\n## 📝 Key Details\n* **Focus Point:** \n\n## 🗂️ Resource Links\n* \n\n> ℹ️ **Important Note:**\n> '
        self.assertEqual(nf.format_body(body), SUMMARY + 'Kept.')

    def test_render_replaces_only_named_sections_and_keeps_custom_ones(self):
        body = nf.render('One.', ['old detail']) + '\n\n## Background\nWhy it exists.'
        updated = nf.render(details=['new detail'], links=['https://example.com'], body=body)
        self.assertIn('* new detail', updated)
        self.assertNotIn('old detail', updated)
        self.assertIn('## Background\nWhy it exists.', updated)
        self.assertLess(updated.index('Key Details'), updated.index('Resource Links'))

    def test_action_items_only_on_plan_bug_and_risk(self):
        body = nf.render('Summary.', actions=['Do it'])
        with self.assertRaisesRegex(ValueError, 'Action Items are only'):
            nf.check(body, 'knowledge')
        for allowed in ('plan', 'bug', 'risk'):
            nf.check(body, allowed)

    def test_plain_text_for_snippets_has_no_markup(self):
        body = nf.render('Summary here.', ['**Label:** fact'], important='Careful')
        self.assertEqual(nf.plain(body), 'Summary here. Label: fact Careful')
        self.assertEqual(snippet(body, []), 'Summary here. Label: fact Careful')


class Files(StoreFixture):
    def test_file_has_ordered_frontmatter_and_derived_title_heading(self):
        n = capture('Cache layer', 'The cache is disposable.', TAGS, 'demo', self.root, context='ctx')
        text = Path(n['path']).read_text(encoding='utf-8')
        front = [line.split(':', 1)[0] for line in text.split('---\n')[1].splitlines()]
        self.assertEqual(front[:5], ['title', 'type', 'status', 'tags', 'context'])
        self.assertEqual(front[-1], 'sha256')
        self.assertIn('\n---\n\n# 🎯 Cache layer\n\n## 📌 Executive Summary\nThe cache is disposable.\n', text)
        note = parse_text(text, n['path'])
        self.assertEqual(note['body'], SUMMARY + 'The cache is disposable.')
        self.assertEqual(serialize(note), text)

    def test_renamed_title_moves_the_heading_without_duplicating_it(self):
        n = capture('Cache layer', 'The cache is disposable.', TAGS, 'demo', self.root)
        result = change(n['id'], 'demo', 'update', self.root, title='Cache tier')
        text = Path(result['path']).read_text(encoding='utf-8')
        self.assertEqual(text.count('# 🎯'), 1)
        self.assertIn('# 🎯 Cache tier', text)

    def test_capture_refuses_action_items_on_knowledge(self):
        with self.assertRaisesRegex(ValueError, 'Action Items are only'):
            capture('Steps', nf.render('Summary.', actions=['x']), TAGS, 'demo', self.root)
        n = capture('Steps', nf.render('Summary.', actions=['x']), TAGS, 'demo', self.root, type='plan')
        with self.assertRaisesRegex(ValueError, 'Action Items are only'):
            change(n['id'], 'demo', 'update', self.root, type='knowledge')

    def test_open_items_in_action_section_are_not_flagged_for_bugs(self):
        body = nf.render('Webcam fails with 0xA00F4244.', actions=['Reinstall the device; not yet run'])
        self.assertEqual(quality_warnings({'title': 'Webcam', 'body': body, 'type': 'bug'}), [])
        flagged = nf.render('Webcam fix not yet run.')
        self.assertTrue(quality_warnings({'title': 'Webcam', 'body': flagged, 'type': 'bug'}))

    def test_duplicate_capture_of_plain_text_is_recognized(self):
        a = capture('Cache layer', 'The cache is disposable.', TAGS, 'demo', self.root)
        b = capture('Cache layer', 'The cache is disposable.', TAGS, 'demo', self.root)
        self.assertEqual((b['id'], b['status']), (a['id'], 'duplicate'))

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli(['--root', str(self.root), '--domain', 'demo', *args])
        return json.loads(out.getvalue())

    def test_cli_sections_on_capture_and_update(self):
        made = self.run_cli('capture', '--subject', 'project', '--concern', 'architecture', '--type', 'bug',
                            '--title', 'Webcam error', '--summary', 'Camera app fails.', '--detail', '**Code:** 0xA00F4244',
                            '--action', 'Reinstall device', '--important', 'Export the key first.')
        body = find_note(made['id'], 'demo', self.root)[1]['body']
        self.assertTrue(body.startswith(SUMMARY + 'Camera app fails.'))
        self.assertIn('- [ ] Reinstall device', body)
        self.run_cli('update', made['id'], '--done', 'Reinstall device', '--link', 'https://example.com')
        body = find_note(made['id'], 'demo', self.root)[1]['body']
        self.assertIn('- [x] Reinstall device', body)
        self.assertNotIn('- [ ]', body)
        self.assertIn('* **Code:** 0xA00F4244', body)
        self.assertIn('* https://example.com', body)


class Vault(StoreFixture):
    def test_vault_copy_matches_canonical_and_edits_round_trip(self):
        vault = self.base / 'vault'
        n = self.note()
        build_overlay('demo', vault, self.root)
        copy = vault / 'Domains/demo' / Path(n['path']).name
        self.assertEqual(copy.read_bytes(), Path(n['path']).read_bytes())
        text = copy.read_text(encoding='utf-8').replace('rebuildable.', 'rebuildable.\n\n## 📝 Key Details\n* **Where:** local disk')
        copy.write_text(text, encoding='utf-8')
        self.assertEqual(len(build_overlay('demo', vault, self.root)['applied']), 1)
        path = find_note(n['id'], 'demo', self.root)[0]
        self.assertEqual(copy.read_bytes(), path.read_bytes())
        self.assertIn('---\n\n## 📝 Key Details\n* **Where:** local disk', path.read_text(encoding='utf-8'))

    def test_templates_use_the_layout_and_unfilled_sections_drop(self):
        vault = self.base / 'vault'
        build_overlay('demo', vault, self.root)
        template = (vault / 'Templates/Plan.md').read_text(encoding='utf-8')
        for part in ('# 🎯 {{title}}', '## 📌 Executive Summary', '## 🟩 Action Items', '> ℹ️ **Important Note:**'):
            self.assertIn(part, template)
        self.assertNotIn('Action Items', (vault / 'Templates/Knowledge.md').read_text(encoding='utf-8'))
        draft = vault / 'Domains/demo/Rollout.md'
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(template.replace('{{title}}', 'Rollout').replace('concern/knowledge', 'concern/decision').replace(
            'Intended steps, dependencies, how success is checked; proposed or approved.', 'Ship the cache in two steps.'), encoding='utf-8')
        build_overlay('demo', vault, self.root)
        self.assertFalse(draft.exists())
        made = next(p for p in (self.root / 'domains/demo/notes').glob('PLAN - rollout-*.md'))
        self.assertTrue(made.read_text(encoding='utf-8').endswith('# 🎯 Rollout\n\n' + SUMMARY + 'Ship the cache in two steps.\n'))


if __name__ == '__main__':
    unittest.main()
