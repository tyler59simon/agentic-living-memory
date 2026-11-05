"""Canonical data stays visible and recoverable under real-world Windows conditions."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from test_core import StoreFixture
from core import common, repository as repo
from core.governance import apply, retag_plan
from core.init_domain import init_domain
from core.notes import capture, change, find_note
from core.search import search_domain
from core.validate import validate_domain, reconcile as rebuild


class Integrity(StoreFixture):
    def rewrite(self, note, transform):
        path = Path(note['path'])
        path.write_bytes(transform(path.read_bytes()))
        return path

    def test_bom_note_stays_visible_and_valid(self):
        n = self.note()
        self.rewrite(n, lambda raw: b'\xef\xbb\xbf' + raw)
        self.assertEqual([x['id'] for x in repo.inventory('demo', self.root)], [n['id']])
        self.assertEqual(validate_domain('demo', self.root), [])
        hits = search_domain('cache', 'demo', root=self.root, rebuild=True)
        self.assertEqual([h['id'] for h in hits], [n['id']])

    def test_crlf_note_validates_and_rebuilds(self):
        n = self.note(body='Line one about the cache.\nLine two.')
        self.rewrite(n, lambda raw: raw.replace(b'\n', b'\r\n'))
        self.assertEqual(validate_domain('demo', self.root), [])
        self.assertTrue(rebuild('demo', self.root)['ok'])
        # The revision is still the exact file bytes, so transactions detect later edits.
        _, note = find_note(n['id'], 'demo', self.root)
        self.assertEqual(note['revision'], repo.digest(Path(n['path']).read_bytes()))
        change(n['id'], 'demo', 'update', self.root, expected_revision=note['revision'], body='Updated body.')

    def test_yaml_scalars_from_obsidian(self):
        note = repo.parse_text("---\nid: \"x\"\ntitle: 2024\ncontext: 'It''s fine'\ntags:\n---\n\nBody\n")
        self.assertEqual(note['title'], '2024')
        self.assertEqual(note['context'], "It's fine")
        self.assertEqual(note['tags'], [])

    def test_unreadable_and_id_less_files_are_reported_not_fatal(self):
        n = self.note()
        notes = self.root / 'domains/demo/notes'
        (notes / 'template-a.md').write_text('---\ntitle: "Template"\n---\n\nPrompt\n', encoding='utf-8')
        (notes / 'template-b.md').write_text('---\ntitle: "Template"\n---\n\nPrompt\n', encoding='utf-8')
        (notes / 'broken.md').write_text('no frontmatter at all', encoding='utf-8')
        self.assertEqual([x['id'] for x in repo.inventory('demo', self.root)], [n['id']])
        found = {Path(p['path']).name: p['problem'] for p in repo.problems('demo', self.root)}
        self.assertEqual(found, {'broken.md': 'unreadable frontmatter', 'template-a.md': 'missing id', 'template-b.md': 'missing id'})
        self.assertEqual(len(validate_domain('demo', self.root)), 3)

    def test_duplicate_id_keeps_domain_usable(self):
        n = self.note()
        source = Path(n['path'])
        (source.parent / ('copy-' + source.name)).write_bytes(source.read_bytes())
        self.assertEqual(len(repo.inventory('demo', self.root)), 1)
        self.assertIn('duplicate id', repo.problems('demo', self.root)[0]['problem'])
        self.assertTrue(capture('Other', 'A separate note.', ['subject/project', 'concern/decision'], 'demo', self.root))

    def test_retag_covers_every_domain(self):
        init_domain('other', [str(self.base / 'other')], self.root)
        a = self.note()
        b = capture('Elsewhere', 'Other domain note.', ['subject/project', 'concern/architecture'], 'other', self.root)
        proposed = retag_plan(self.root, {'concern/architecture': 'concern/decision'})
        apply(self.root, proposed['plan_id'], proposed['digest'])
        for domain, note in (('demo', a), ('other', b)):
            self.assertEqual(find_note(note['id'], domain, self.root)[1]['tags'], ['concern/decision', 'subject/project'])
        with self.assertRaisesRegex(ValueError, 'other domains'):
            capture('Again', 'Uses the tag again.', ['subject/project', 'concern/decision'], 'other', self.root)
            retag_plan(self.root, {'concern/decision': 'concern/architecture'}, ['demo'])

    def test_sharing_violation_is_retried(self):
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) < 3:
                raise PermissionError(13, 'The process cannot access the file')
            return real(src, dst)

        with patch.object(common.os, 'replace', flaky):
            n = self.note()
        self.assertTrue(Path(n['path']).exists())
        self.assertGreaterEqual(len(calls), 3)

    def test_interrupted_rename_rolls_forward_on_next_write(self):
        n = self.note()
        real = repo._write
        state = {'calls': 0}

        def crash(path, data):
            state['calls'] += 1
            if state['calls'] == 3:  # dirty marker, new file, then die before removing the old file
                raise KeyboardInterrupt  # simulates the process dying mid-transaction
            return real(path, data)

        with patch.object(repo, '_write', crash), self.assertRaises(KeyboardInterrupt):
            change(n['id'], 'demo', 'rename', self.root, title='Renamed cache')
        # New file written first, so the note is never missing; the duplicate is reported.
        self.assertEqual(len(repo.inventory('demo', self.root)), 1)
        self.assertIn('duplicate id', repo.problems('demo', self.root)[0]['problem'])
        self.assertTrue(list((self.root / '.operations').glob('*/pending')))
        self.note('Unrelated', 'Next write finishes the rename first.')
        self.assertFalse(list((self.root / '.operations').glob('*/pending')))
        self.assertEqual(find_note(n['id'], 'demo', self.root)[1]['title'], 'Renamed cache')
        self.assertEqual(repo.problems('demo', self.root), [])

    def test_conflicted_pending_operation_can_be_abandoned(self):
        n = self.note()
        real = repo._write
        state = {'calls': 0}

        def crash(path, data):
            state['calls'] += 1
            if state['calls'] == 2:
                raise KeyboardInterrupt
            return real(path, data)

        with patch.object(repo, '_write', crash), self.assertRaises(KeyboardInterrupt):
            change(n['id'], 'demo', 'rename', self.root, title='Renamed cache')
        operation = next((self.root / '.operations').glob('*/pending')).parent.name
        Path(n['path']).write_text(Path(n['path']).read_text(encoding='utf-8') + '\nUser edit.\n', encoding='utf-8')
        self.assertFalse(repo.roll_forward_pending(self.root)[0]['ok'])
        result = repo.abandon(self.root, operation)
        self.assertEqual(result['state'], 'abandoned')
        self.assertEqual(repo.roll_forward_pending(self.root), [])
        with self.assertRaisesRegex(ValueError, 'abandoned'):
            repo.recover(self.root, operation)


if __name__ == '__main__':
    import unittest
    unittest.main()
