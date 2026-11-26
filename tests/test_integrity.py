"""Canonical data stays visible and recoverable under real-world Windows conditions."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from test_core import StoreFixture
from core import common, repository as repo
from core.governance import retag, undo
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

    def test_rebuild_indexes_every_note_and_lists_issues_as_warnings(self):
        good = self.note()
        bad = self.note('Upload pipeline', 'The upload pipeline retries on 307.')
        self.rewrite(bad, lambda raw: raw.replace(b'concern/architecture', b'concern/not-a-tag'))
        (self.root / 'domains/demo/notes/broken.md').write_text('no frontmatter at all', encoding='utf-8')
        self.assertEqual(len(validate_domain('demo', self.root)), 2)
        result = rebuild('demo', self.root)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['indexed'], 2)
        self.assertEqual(len(result['warnings']), 2)
        self.assertEqual([h['id'] for h in search_domain('cache', 'demo', root=self.root)], [good['id']])
        self.assertEqual([h['id'] for h in search_domain('upload pipeline', 'demo', root=self.root)], [bad['id']])

    def test_rebuild_survives_metadata_the_index_cannot_hold(self):
        for old, new in ((b'tags: ["concern/architecture", "subject/project"]', b'tags: null'),
                         (b'tags: ["concern/architecture", "subject/project"]', b'tags: [123]'),
                         (b'status: "active"', b'status: [1]')):
            with self.subTest(new=new):
                good = self.note()
                bad = self.note('Upload pipeline', 'The upload pipeline retries on 307.')
                raw = Path(bad['path']).read_bytes()
                self.assertIn(old, raw)
                Path(bad['path']).write_bytes(raw.replace(old, new, 1))
                result = rebuild('demo', self.root)
                self.assertTrue(result['ok'], result)
                self.assertTrue(result['warnings'] and all(w.endswith(bad['path']) for w in result['warnings']), result)
                self.assertEqual([h['id'] for h in search_domain('cache', 'demo', root=self.root)], [good['id']])
                Path(bad['path']).unlink()
                rebuild('demo', self.root)

    def test_rebuild_names_a_note_the_index_left_out_even_when_it_validates(self):
        good = self.note()
        bad = self.note('Upload pipeline', 'The upload pipeline retries on 307.')
        raw = Path(bad['path']).read_bytes()
        Path(bad['path']).write_bytes(raw.replace(b'created: "', b'created: [1]\nx_created: "', 1))
        result = rebuild('demo', self.root)
        self.assertEqual(result['indexed'], 1, result)
        self.assertTrue(any(w.startswith('Left out of the search index') and w.endswith(bad['path']) for w in result['warnings']), result)
        self.assertEqual([h['id'] for h in search_domain('cache', 'demo', root=self.root)], [good['id']])

    def test_duplicate_id_keeps_domain_usable(self):
        n = self.note()
        source = Path(n['path'])
        (source.parent / ('copy-' + source.name)).write_bytes(source.read_bytes())
        self.assertEqual(len(repo.inventory('demo', self.root)), 1)
        self.assertIn('duplicate id', repo.problems('demo', self.root)[0]['problem'])
        self.assertTrue(capture('Other', 'A separate note.', ['subject/project', 'concern/decision'], 'demo', self.root))

    @__import__('unittest').skipIf(os.name == 'nt', 'POSIX file modes')
    def test_undo_brings_a_removed_file_back_with_its_mode(self):
        from core.governance import commit
        commit(self.root, {'extra/tool.sh': 'echo hi\n'}, 'package_install')
        (self.root / 'extra/tool.sh').chmod(0o755)
        removed = commit(self.root, {'extra/tool.sh': None}, 'package_uninstall')
        undo(self.root, removed['operation_id'])
        self.assertEqual((self.root / 'extra/tool.sh').stat().st_mode & 0o777, 0o755)

    def test_retag_undo_with_a_bom_taxonomy(self):
        a = self.note()
        taxonomy = self.root / 'tag_taxonomy.json'
        taxonomy.write_bytes(b'\xef\xbb\xbf' + taxonomy.read_bytes())
        before = taxonomy.read_bytes(), Path(a['path']).read_bytes()
        done = retag(self.root, {'concern/architecture': 'concern/decision'})
        undo(self.root, done['operation_id'])
        self.assertEqual((taxonomy.read_bytes(), Path(a['path']).read_bytes()), before)

    def test_retag_covers_every_domain(self):
        init_domain('other', [str(self.base / 'other')], self.root)
        a = self.note()
        b = capture('Elsewhere', 'Other domain note.', ['subject/project', 'concern/architecture'], 'other', self.root)
        before = {n['id']: Path(n['path']).read_bytes() for n in (a, b)}
        done = retag(self.root, {'concern/architecture': 'concern/decision'})
        for domain, note in (('demo', a), ('other', b)):
            self.assertEqual(find_note(note['id'], domain, self.root)[1]['tags'], ['concern/decision', 'subject/project'])
        undo(self.root, done['operation_id'])
        self.assertEqual({n['id']: Path(n['path']).read_bytes() for n in (a, b)}, before)
        with self.assertRaisesRegex(ValueError, 'other domains'):
            capture('Again', 'Uses the tag again.', ['subject/project', 'concern/decision'], 'other', self.root)
            retag(self.root, {'concern/decision': 'concern/architecture'}, ['demo'])

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


class Governance(StoreFixture):
    def crash_once(self, n, calls):
        real, state = repo._write, {'calls': 0}

        def crash(path, data):
            state['calls'] += 1
            if state['calls'] == calls:
                raise KeyboardInterrupt
            return real(path, data)
        with patch.object(repo, '_write', crash), self.assertRaises(KeyboardInterrupt):
            change(n['id'], 'demo', 'rename', self.root, title='Renamed cache')
        return next((self.root / '.operations').glob('*/pending')).parent.name

    def test_marker_is_written_before_the_journal_and_a_failed_journal_write_leaves_a_clearable_marker(self):
        real = repo.atomic_write

        def failing(path, data, *a, **k):
            if Path(path).name == 'journal.json':
                raise OSError('journal write failed')
            return real(path, data, *a, **k)
        with patch('core.repository.atomic_write', failing), self.assertRaises(OSError):
            self.note('Never journaled', 'No journal exists for this note.')
        marker = next((self.root / '.operations').glob('*/pending'))
        self.assertFalse(marker.with_name('journal.json').exists())
        self.note('Next write', 'The next write clears the orphan marker.')
        self.assertFalse(marker.exists())
        self.assertEqual(validate_domain('demo', self.root), [])

    def test_transaction_reports_rolled_forward_operations(self):
        n = self.note()
        op = self.crash_once(n, 3)
        result = repo.transaction(self.root, {'x.txt': 'x'}, 'test')
        self.assertEqual(result['recovered'], [op])
        self.assertNotIn('warnings', result)

    def test_transaction_warns_about_a_pending_operation_it_cannot_roll_forward(self):
        n = self.note()
        op = self.crash_once(n, 2)
        Path(n['path']).write_text(Path(n['path']).read_text(encoding='utf-8') + '\nUser edit.\n', encoding='utf-8')
        result = repo.transaction(self.root, {'x.txt': 'x'}, 'test')
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['warnings']), 1)
        self.assertIn(f'operation {op} could not be rolled forward', result['warnings'][0])
        self.assertIn(f'run recover {op} or recover {op} --abandon', result['warnings'][0])
        self.assertNotIn('recovered', result)

    def test_a_damaged_pending_journal_is_a_warning_and_does_not_block_writes(self):
        n = self.note()
        op = self.crash_once(n, 2)
        (self.root / '.operations' / op / 'journal.json').write_text('{not json', encoding='utf-8')
        result = repo.transaction(self.root, {'x.txt': 'x'}, 'test')
        self.assertTrue(result['ok'])
        self.assertIn(f'operation {op} could not be rolled forward', result['warnings'][0])

    def test_recover_refuses_to_reverse_a_final_state(self):
        repo.transaction(self.root, {'a.txt': 'a'}, 'test', operation_id='done')
        with self.assertRaisesRegex(ValueError, 'is committed; reverse it with undo done'):
            repo.recover(self.root, 'done', 'backward')
        self.assertEqual((self.root / 'a.txt').read_text(), 'a')
        self.assertTrue(repo.recover(self.root, 'done', 'forward')['idempotent'])
        real, state = repo._write, {'n': 0}

        def crash(path, data):
            state['n'] += 1
            if state['n'] == 1:
                raise KeyboardInterrupt
            return real(path, data)
        with patch.object(repo, '_write', crash), self.assertRaises(KeyboardInterrupt):
            repo.transaction(self.root, {'b.txt': 'b'}, 'test', operation_id='half')
        repo.recover(self.root, 'half', 'backward')
        with self.assertRaisesRegex(ValueError, 'Operation half was rolled back'):
            repo.recover(self.root, 'half', 'forward')
        self.assertTrue(repo.recover(self.root, 'half', 'backward')['idempotent'])
