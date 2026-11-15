"""scripts/eval_related.py: the related check over a synthetic store, read-only, with pairs, labels and a copy."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from test_core import StoreFixture
from core.notes import capture, find_note

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('eval_related', REPO / 'scripts' / 'eval_related.py')
EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVAL)
TAGS = ['subject/project', 'concern/decision']


class EvalRelated(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags': TAGS}), encoding='utf-8')

    def note(self, title, body, **kw):
        return capture(title, body, TAGS, 'demo', self.root, type=kw.pop('type', 'decision'), allow_similar=True, **kw)

    def test_scan_counts_pairs_and_the_sample_and_never_writes(self):
        old = self.note('Index storage engine', 'We keep the search index in SQLite FTS5 files, one per domain.')
        new = self.note('Search index moves to one shared file', 'The search index moves from per-domain SQLite FTS5 files to one shared SQLite FTS5 file.')
        self.note('Unrelated billing rounding', 'Invoices round half up to cents.')
        self.note('Index search tuning', 'Query weights for the SQLite FTS5 search index.', type='knowledge')
        before = sorted(p.read_bytes() for p in (self.root/'domains'/'demo'/'notes').glob('*.md'))
        labels = {f"demo:{new['id']}|demo:{old['id']}": True}
        report = EVAL.evaluate(self.root, [(new['id'][:8], old['id'][:8])], labels, 5, 30)
        self.assertEqual(sorted(p.read_bytes() for p in (self.root/'domains'/'demo'/'notes').glob('*.md')), before)
        listing = report['listing']
        self.assertEqual(listing['notes_scanned'], 3)   # the knowledge note is not scanned
        self.assertGreaterEqual(listing['unique_pairs'], 1);self.assertEqual(listing['cross_domain_pairs'], 0)
        self.assertEqual(listing['recent']['notes_scanned'], 3)
        pair = report['pairs'][0]
        self.assertTrue(pair['found_listing']);self.assertIn('owed_bar', pair['signals'])
        self.assertEqual(listing['labelled'], 1 if pair['found_listing'] else 0)
        self.assertTrue(report['sample']);self.assertIn('|', report['sample'][0]['key'])
        self.assertLessEqual(len(report['sample']), 5)
        self.assertEqual(report['owed']['notes_scanned'], 3)

    def test_copy_restores_a_retired_note_and_leaves_the_store_alone(self):
        old = self.note('Index storage engine', 'We keep the search index in SQLite FTS5 files, one per domain.')
        new = self.note('Search index moves to one shared file', 'The search index moves from per-domain SQLite FTS5 files to one shared SQLite FTS5 file.',
                        supersedes=[old['id']])
        self.assertEqual(find_note(old['id'], 'demo', self.root)[1]['status'], 'superseded')
        report = EVAL.run(self.root, [(new['id'], 'Index storage engine')], copy=True)
        self.assertTrue(report['pairs'][0]['found_listing'])
        self.assertEqual(find_note(old['id'], 'demo', self.root)[1]['status'], 'superseded')   # the store itself is untouched
        with self.assertRaises(ValueError):EVAL.run(self.root, [(new['id'], old['id'])])


if __name__ == '__main__':
    unittest.main()
