import contextlib
import io
import json
import re
import unittest
from pathlib import Path

from test_core import StoreFixture
from core import note_format as nf
from core.cli import main as cli
from core.notes import capture, change, find_note, quality_warnings
from core.repository import parse_text, serialize
from core.search import snippet
from core.sync import build_overlay, TEMPLATE_PROMPTS

TAGS = ['subject/project', 'concern/architecture']
# A note as it was written before the current layout.
LEGACY = ('## 📌 Executive Summary\nEdit the source repo.\n\n---\n\n'
          '## 📝 Key Details\n* **Workflow:** run the tests, then install.\n\n'
          '## 🗂️ Resource Links\n* [[Codebase map]]\n\n'
          '## 🟩 Action Items\n- [ ] Deploy\n\n'
          '> ℹ️ **Important Note:**\n> Auto mode blocks setup.py.')


class Layout(unittest.TestCase):
    def test_plain_text_becomes_the_summary(self):
        self.assertEqual(nf.format_body('The cache is disposable.'), 'The cache is disposable.')
        self.assertEqual(nf.sections('The cache is disposable.')['summary'], 'The cache is disposable.')

    def test_format_is_idempotent_and_keeps_the_reference_layout(self):
        body = nf.render('Short summary.', ['**Cause:** stale key', 'nested\n  - sub'], ['[[Other note]]'],
                         ['Fix it'], ['Found it'], 'Back up the key first.')
        self.assertEqual(nf.format_body(body), body)
        self.assertEqual(body, 'Short summary.\n\n'
                         '> **Important:** Back up the key first.\n\n'
                         '## Details\n- **Cause:** stale key\n- nested\n  - sub\n\n'
                         '## Links\n- [[Other note]]\n\n'
                         '## Action items\n- [ ] Fix it\n- [x] Found it')

    def test_a_note_in_the_earlier_layout_parses_and_converts(self):
        found = nf.sections(LEGACY)
        self.assertEqual(found['summary'], 'Edit the source repo.')
        self.assertEqual(found['important'], 'Auto mode blocks setup.py.')
        self.assertEqual(nf.action_items(LEGACY), [(False, 'Deploy')])
        self.assertEqual(nf.summary_text(LEGACY), 'Edit the source repo.')
        converted = nf.format_body(LEGACY)
        self.assertEqual(converted, 'Edit the source repo.\n\n> **Important:** Auto mode blocks setup.py.\n\n'
                         '## Details\n* **Workflow:** run the tests, then install.\n\n'
                         '## Links\n* [[Codebase map]]\n\n## Action items\n- [ ] Deploy')
        self.assertEqual(nf.format_body(converted), converted)
        self.assertEqual(nf.sections(converted), found)
        self.assertEqual(nf.summary_text(converted), 'Edit the source repo.')

    def test_the_title_heading_is_stripped_only_from_a_file(self):
        self.assertEqual(nf.strip_title('# 🎯 Old title\n\nBody.'), 'Body.')
        self.assertEqual(nf.strip_title('# Title\n\nBody.', heading=True), 'Body.')
        self.assertEqual(nf.strip_title('# Not a title\n\nBody.'), '# Not a title\n\nBody.')
        self.assertEqual(nf.strip_title('## Why\nBody.', heading=True), '## Why\nBody.')

    def test_empty_sections_are_left_out(self):
        body = 'Kept.\n\n> **Important:** \n\n## Why\n\n## Details\n- **Focus Point:** \n\n## Links\n- '
        self.assertEqual(nf.format_body(body), 'Kept.')
        legacy = '## 📌 Executive Summary\nKept.\n\n---\n\n## 📝 Key Details\n* **Focus Point:** \n\n> ℹ️ **Important Note:**\n> '
        self.assertEqual(nf.format_body(legacy), 'Kept.')

    def test_render_replaces_only_named_sections_and_keeps_custom_ones(self):
        body = nf.render('One.', ['old detail']) + '\n\n## Background\nWhy it exists.'
        updated = nf.render(details=['new detail'], links=['https://example.com'], body=body)
        self.assertIn('- new detail', updated)
        self.assertNotIn('old detail', updated)
        self.assertIn('## Background\nWhy it exists.', updated)
        self.assertLess(updated.index('## Details'), updated.index('## Links'))

    def test_the_callout_goes_right_after_the_summary(self):
        body = nf.render('One.', ['a fact'], ['[[Other]]'])
        added = nf.render(important='Mind the lock.', body=body)
        self.assertTrue(added.startswith('One.\n\n> **Important:** Mind the lock.\n\n## Details'), added)
        self.assertEqual(nf.sections(added)['important'], 'Mind the lock.')
        self.assertEqual(nf.sections('One.\n\n> Important things happen.')['summary'], 'One.\n\n> Important things happen.')

    def test_type_sections_keep_the_types_order_and_come_before_details(self):
        body = nf.render('We chose SQLite.', ['a fact'], named=[('Alternatives considered', 'Postgres.'), ('Why', 'No server.')],
                         note_type='decision')
        self.assertLess(body.index('## Why'), body.index('## Alternatives considered'))
        self.assertLess(body.index('## Alternatives considered'), body.index('## Details'))
        replaced = nf.set_section(body, 'why:', 'Zero operations.', 'decision')
        self.assertEqual(replaced.count('## Why'), 1)
        self.assertIn('## Why\nZero operations.', replaced)
        self.assertEqual(nf.named_sections(replaced), [('Why', 'Zero operations.'), ('Alternatives considered', 'Postgres.')])
        dropped = nf.drop_section(replaced, 'Alternatives considered')
        self.assertNotIn('Postgres', dropped)
        self.assertEqual(nf.set_section(dropped, 'Details', '- replaced'), nf.render(details=['replaced'], body=dropped))
        with self.assertRaisesRegex(ValueError, 'No section named'):
            nf.drop_section(dropped, 'Missing')
        with self.assertRaisesRegex(ValueError, 'cannot be dropped'):
            nf.drop_section(dropped, 'Summary')

    def test_text_after_a_leading_callout_is_the_summary(self):
        body = nf.format_body('> **Important:** Mind the lock.\n\nThe cache is disposable.\n\n## Details\n- a fact')
        self.assertEqual(body, 'The cache is disposable.\n\n> **Important:** Mind the lock.\n\n## Details\n- a fact')
        self.assertEqual(nf.sections(body)['summary'], 'The cache is disposable.')
        self.assertEqual(nf.format_body(body), body)

    def test_a_named_section_without_a_summary_keeps_its_heading(self):
        body = nf.format_body('## Why\nBecause.')
        self.assertEqual(body, '## Why\nBecause.')
        with self.assertRaisesRegex(ValueError, 'Note needs a summary'):
            nf.check(body, 'decision')

    def test_action_items_only_on_plan_bug_and_risk(self):
        body = nf.render('Summary.', actions=['Do it'])
        with self.assertRaisesRegex(ValueError, 'Action items are only'):
            nf.check(body, 'knowledge')
        for allowed in ('plan', 'bug', 'risk'):
            nf.check(body, allowed)

    def test_plain_text_for_snippets_has_no_markup(self):
        body = nf.render('Summary here.', ['**Label:** fact'], important='Careful')
        self.assertEqual(nf.plain(body), 'Summary here. Careful Label: fact')
        self.assertEqual(snippet(body, []), 'Summary here. Careful Label: fact')
        self.assertEqual(nf.plain(LEGACY), 'Edit the source repo. Workflow: run the tests, then install. [[Codebase map]] Deploy Auto mode blocks setup.py.')


class Files(StoreFixture):
    def test_file_has_ordered_frontmatter_and_derived_title_heading(self):
        n = capture('Cache layer', 'The cache is disposable.', TAGS, 'demo', self.root, context='ctx')
        text = Path(n['path']).read_text(encoding='utf-8')
        front = [line.split(':', 1)[0] for line in text.split('---\n')[1].splitlines()]
        self.assertEqual(front[:5], ['title', 'type', 'status', 'tags', 'context'])
        self.assertEqual(front[-1], 'sha256')
        self.assertIn('\n---\n\n# Cache layer\n\nThe cache is disposable.\n', text)
        note = parse_text(text, n['path'])
        self.assertEqual(note['body'], 'The cache is disposable.')
        self.assertEqual(serialize(note), text)

    def test_a_file_in_the_earlier_layout_still_reads_with_its_hash(self):
        n = capture('Cache layer', 'The cache is disposable.', TAGS, 'demo', self.root)
        path = Path(n['path'])
        from core.repository import digest
        # A file the earlier release wrote: an emoji title heading, and a stored hash of the body as written.
        front = path.read_text(encoding='utf-8').split('\n---\n\n', 1)[0]
        front = re.sub(r'sha256: "[0-9a-f]{64}"', f'sha256: "{digest(LEGACY.encode())}"', front)
        parsed = parse_text(front + '\n---\n\n# 🎯 Cache layer\n\n' + LEGACY + '\n', path)
        self.assertEqual(parsed['body'], LEGACY)
        self.assertEqual(parsed['sha256'], parsed['digest'])
        self.assertEqual(nf.sections(parsed['body'])['summary'], 'Edit the source repo.')

    def test_renamed_title_moves_the_heading_without_duplicating_it(self):
        n = capture('Cache layer', 'The cache is disposable.', TAGS, 'demo', self.root)
        result = change(n['id'], 'demo', 'update', self.root, title='Cache tier')
        text = Path(result['path']).read_text(encoding='utf-8')
        self.assertEqual(text.count('\n# '), 1)
        self.assertIn('\n# Cache tier\n', text)
        self.assertEqual(find_note(n['id'], 'demo', self.root)[1]['body'], 'The cache is disposable.')

    def test_capture_refuses_action_items_on_knowledge(self):
        with self.assertRaisesRegex(ValueError, 'Action items are only'):
            capture('Steps', nf.render('Summary.', actions=['x']), TAGS, 'demo', self.root)
        n = capture('Steps', nf.render('Summary.', actions=['x']), TAGS, 'demo', self.root, type='plan')
        with self.assertRaisesRegex(ValueError, 'Action items are only'):
            change(n['id'], 'demo', 'update', self.root, type='knowledge')

    def test_open_items_in_action_section_are_not_flagged_for_bugs(self):
        body = nf.render('Webcam fails with 0xA00F4244.', actions=['Reinstall the device; not yet run'])
        self.assertFalse([w for w in quality_warnings({'title': 'Webcam', 'body': body, 'type': 'bug'}) if 'unfinished' in w])
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
        self.assertTrue(body.startswith('Camera app fails.\n\n> **Important:** Export the key first.'))
        self.assertIn('- [ ] Reinstall device', body)
        self.run_cli('update', made['id'], '--done', 'Reinstall device', '--link', 'https://example.com')
        body = find_note(made['id'], 'demo', self.root)[1]['body']
        self.assertIn('- [x] Reinstall device', body)
        self.assertNotIn('- [ ]', body)
        self.assertIn('- **Code:** 0xA00F4244', body)
        self.assertIn('- https://example.com', body)

    def test_cli_named_sections_on_capture_and_update(self):
        made = self.run_cli('capture', '--subject', 'project', '--concern', 'architecture', '--type', 'bug',
                            '--title', 'Hook drops input', '--summary', 'The Stop hook drops the prompt.',
                            '--section', 'Fix', 'Read stdin as UTF-8.', '--section', 'Cause', 'stdin is read as cp1252.')
        body = find_note(made['id'], 'demo', self.root)[1]['body']
        self.assertLess(body.index('## Cause'), body.index('## Fix'))
        self.run_cli('update', made['id'], '--section', 'How to check', 'Send a prompt with an emoji.', '--drop-section', 'Fix')
        body = find_note(made['id'], 'demo', self.root)[1]['body']
        self.assertEqual([h for h, _ in nf.named_sections(body)], ['Cause', 'How to check'])


class Vault(StoreFixture):
    def test_vault_copy_matches_canonical_and_edits_round_trip(self):
        vault = self.base / 'vault'
        n = self.note()
        build_overlay('demo', vault, self.root)
        copy = vault / 'Domains/demo' / Path(n['path']).name
        self.assertEqual(copy.read_bytes(), Path(n['path']).read_bytes())
        # An edit may use an earlier heading; the note is saved in the current layout.
        text = copy.read_text(encoding='utf-8').replace('rebuildable.', 'rebuildable.\n\n## 📝 Key Details\n- **Where:** local disk')
        copy.write_text(text, encoding='utf-8')
        self.assertEqual(len(build_overlay('demo', vault, self.root)['applied']), 1)
        path = find_note(n['id'], 'demo', self.root)[0]
        self.assertEqual(copy.read_bytes(), path.read_bytes())
        self.assertIn('rebuildable.\n\n## Details\n- **Where:** local disk', path.read_text(encoding='utf-8'))

    def test_templates_use_the_layout_and_unfilled_sections_drop(self):
        vault = self.base / 'vault'
        build_overlay('demo', vault, self.root)
        template = (vault / 'Templates/Plan.md').read_text(encoding='utf-8')
        for part in ('# {{title}}', TEMPLATE_PROMPTS['plan'], '## Details', '## Action items', '> **Important:**'):
            self.assertIn(part, template)
        self.assertNotIn('🎯', template)
        knowledge = (vault / 'Templates/Knowledge.md').read_text(encoding='utf-8')
        self.assertNotIn('Action items', knowledge)
        self.assertNotIn('## Details', knowledge)
        decision = (vault / 'Templates/Decision.md').read_text(encoding='utf-8')
        self.assertLess(decision.index('## Why'), decision.index('## Alternatives considered'))
        draft = vault / 'Domains/demo/Rollout.md'
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(template.replace('{{title}}', 'Rollout').replace('concern/knowledge', 'concern/decision').replace(
            TEMPLATE_PROMPTS['plan'], 'Ship the cache in two steps.'), encoding='utf-8')
        build_overlay('demo', vault, self.root)
        self.assertFalse(draft.exists())
        made = next(p for p in (self.root / 'domains/demo/notes').glob('PLAN - rollout-*.md'))
        self.assertTrue(made.read_text(encoding='utf-8').endswith('\n# Rollout\n\nShip the cache in two steps.\n'))


if __name__ == '__main__':
    unittest.main()
