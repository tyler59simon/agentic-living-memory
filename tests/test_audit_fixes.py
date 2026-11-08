"""Regressions for the defects the mem-audit review reproduced at d32971a; each test fails on that code."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_core import StoreFixture
from core import runtime, session_start
from core.init_domain import init_domain
from core.note_format import format_body, sections
from core.notes import capture, change
from core.repository import transaction, prune_journals, read_json, canonical_json
from core.search import search_domain, file_notes
from core.sync import build_overlay

TAGS = ['subject/project', 'concern/decision']


def journals(root):
    return sorted(p.parent.name for p in Path(root).glob('.operations/*/journal.json'))


class FencedCode(StoreFixture):
    EXAMPLE = ('Example of Markdown syntax:\n```markdown\n## Executive Summary\nThis is example text.\n```\n'
               'Actual concluding explanation.')

    def test_a_heading_inside_a_fence_is_kept(self):
        out = format_body(self.EXAMPLE)
        self.assertIn('```markdown\n## Executive Summary\nThis is example text.\n```', out)
        self.assertEqual(format_body(out), out)

    def test_tilde_and_longer_fences_and_sections_after_them(self):
        body = ('## 📌 Executive Summary\nSummary.\n\n````\n```\n## Key Details\n```\n````\n\n'
                '~~~\n## 🗂️ Resource Links\n~~~\n\n## 📝 Key Details\n* Real details.')
        found = sections(body)
        self.assertIn('## Key Details', found['summary'])
        self.assertIn('## 🗂️ Resource Links', found['summary'])
        self.assertEqual(found['details'], '* Real details.')
        self.assertNotIn('links', found)


class Lifecycle(StoreFixture):
    def test_filters_narrow_active_notes_unless_status_is_named(self):
        old = capture('Retired cache rule', 'The zebracache is rebuilt weekly.', TAGS, 'demo', self.root)
        capture('Current cache rule', 'The zebracache is rebuilt daily.', TAGS, 'demo', self.root, supersedes=['demo:' + old['id']])
        filtered = search_domain('zebracache', 'demo', 10, self.root, filters={'type': 'knowledge'})
        self.assertEqual([h['status'] for h in filtered], ['active'])
        history = search_domain('zebracache', 'demo', 10, self.root, filters={'status': 'superseded'})
        self.assertEqual([h['id'] for h in history], [old['id']])


class Identity(StoreFixture):
    def test_distinct_plans_sharing_title_words_both_stay_listed(self):
        capture('Payment API authentication', 'Rotate signing tokens.', TAGS, 'demo', self.root, type='plan')
        capture('Payment API pagination', 'Cursor pagination.', TAGS, 'demo', self.root, type='plan')
        text = session_start.build_context(str(self.base / 'project'), self.root)[0]
        self.assertIn('Payment API authentication', text)
        self.assertIn('Payment API pagination', text)


class CrossDomain(StoreFixture):
    def test_weak_local_hits_do_not_crowd_out_a_strong_parent_match(self):
        init_domain('child', [str(self.base / 'project' / 'child')], self.root)
        words = ['amber', 'basil', 'cedar', 'delta', 'ember', 'fjord', 'garnet', 'harbor', 'indigo', 'juniper',
                 'kelp', 'lumen', 'maple', 'nectar', 'onyx', 'pebble', 'quartz', 'raven', 'saffron', 'tundra']
        for i, w in enumerate(words):
            capture(f'{w.title()} {words[(i + 7) % 20]}ish summary', f'Notes about {w}. One line mentions deployment in passing.', TAGS, 'child', self.root)
        strong = capture('Deployment rollback checklist procedure', 'Drain, revert, verify.', TAGS, 'demo', self.root)
        text, ids, _ = runtime.context(str(self.base / 'project' / 'child'), 'what is our deployment rollback checklist procedure', self.root, 'child')
        self.assertEqual(ids, ['demo:' + strong['id']])
        self.assertNotIn('No lexical match', text)


class Delivery(StoreFixture):
    def prompt(self, text):
        return runtime.process('claude', 'user-prompt', {'cwd': str(self.base / 'project'), 'session_id': 's', 'prompt': text}, self.root)['context']

    def test_a_corrected_note_is_delivered_again_and_an_unchanged_one_is_not(self):
        n = capture('Deployment rollback checklist', 'Drain, revert, verify.', TAGS, 'demo', self.root)
        self.assertIn('Deployment rollback checklist', self.prompt('deployment rollback checklist steps'))
        self.assertNotIn('Deployment rollback checklist', self.prompt('deployment rollback checklist again'))
        change(n['id'], 'demo', 'update', self.root, body='Drain, snapshot the database, revert, verify.')
        self.assertIn('snapshot the database', self.prompt('deployment rollback checklist steps'))

    def test_legacy_shown_lists_still_suppress(self):
        n = capture('Deployment rollback checklist', 'Drain, revert, verify.', TAGS, 'demo', self.root)
        self.assertEqual(runtime.context(str(self.base / 'project'), 'deployment rollback checklist', self.root, 'demo', ['demo:' + n['id']])[1], [])

    def test_file_hint_skips_a_row_whose_canonical_file_changed(self):
        n = capture('Parser module note', 'The parser in src/parser.py is hand written.', TAGS, 'demo', self.root, files=['src/parser.py'])
        self.assertEqual([h['id'] for h in file_notes('src/parser.py', ['demo'], self.root, ensure_fresh=False)], [n['id']])
        path = Path(n['path'])
        path.write_text(path.read_text(encoding='utf-8').replace('["src/parser.py"]', '[]').replace('src/parser.py', 'the parser'), encoding='utf-8')
        self.assertEqual(file_notes('src/parser.py', ['demo'], self.root, ensure_fresh=False), [])

    def test_session_start_never_passes_the_hard_cap(self):
        capture('Prefer small commits', 'Keep commits small.', TAGS, 'demo', self.root, type='preference')
        with patch.object(runtime, 'protocol', lambda *a, **k: 'protocol line\n' * 1000):
            text = runtime.process('claude', 'session-start', {'cwd': str(self.base / 'project'), 'session_id': 'cap'}, self.root)['context']
        self.assertLessEqual(len(text), session_start.HARD_CAP)


class QuietSync(StoreFixture):
    def setUp(self):
        super().setUp();self.vault = self.base / 'vault'

    def test_a_sync_that_changes_nothing_writes_no_journal(self):
        self.note();build_overlay('demo', self.vault, self.root)
        store, vault = journals(self.root), journals(self.vault)
        status = (self.vault / 'Dashboards/Sync Status - demo.md').read_bytes()
        build_overlay('demo', self.vault, self.root, trigger='session end')
        self.assertEqual((journals(self.root), journals(self.vault)), (store, vault))
        self.assertEqual((self.vault / 'Dashboards/Sync Status - demo.md').read_bytes(), status)
        self.note('Second note', 'Something new to copy.')   # a real change still syncs, and is journaled
        self.assertGreater(len(journals(self.vault)), len(vault))
        self.assertTrue(any('second' in p.name.casefold() for p in (self.vault / 'Domains/demo').rglob('*.md')))

    def test_canvas_edges_come_from_links_not_shared_tags(self):
        a = self.note('Cache policy', 'The cache is rebuildable.')
        b = self.note('Index layout', 'Built from the notes; see [[Cache policy]].')
        self.note('Unrelated', 'Shares tags with both, links to nothing.')
        result = build_overlay('demo', self.vault, self.root)
        canvas = json.loads((self.vault / 'Canvas/demo.canvas').read_text(encoding='utf-8'))
        self.assertEqual([(e['fromNode'], e['toNode'], e['label']) for e in canvas['edges']], [(b['id'], a['id'], 'link')])
        self.assertEqual(result['canvas_edges'], 1)


class Pruning(StoreFixture):
    def age(self, root, operation_id, days):
        path = Path(root) / '.operations' / operation_id / 'journal.json'
        value = read_json(path);value['created'] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        path.write_text(canonical_json(value), encoding='utf-8')

    def test_only_old_committed_bookkeeping_is_pruned(self):
        place = self.base / 'place';place.mkdir()
        old_views = transaction(place, {'a.md': 'one'}, 'overlay_views')['operation_id']
        old_pending = transaction(place, {'b.md': 'two'}, 'overlay_state')['operation_id']
        old_edit = transaction(place, {'c.md': 'three'}, 'overlay_auto_update')['operation_id']
        recent = transaction(place, {'d.md': 'four'}, 'overlay_sync')['operation_id']
        for op in (old_views, old_pending, old_edit):self.age(place, op, 30)
        (place / '.operations' / old_pending / 'pending').write_text('pending\n')
        self.assertEqual(prune_journals(place), 1)
        self.assertEqual(set(journals(place)), {old_pending, old_edit, recent})

    def test_housekeeping_prunes_at_most_daily(self):
        with patch('core.repository.prune_journals', return_value=0) as prune:
            runtime._prune_journals(self.root);runtime._prune_journals(self.root)
        self.assertEqual(prune.call_count, 1)

    def test_skip_unchanged_writes_no_journal(self):
        place = self.base / 'place';place.mkdir()
        transaction(place, {'a.md': 'same'}, 'overlay_views')
        before = journals(place)
        self.assertEqual(transaction(place, {'a.md': 'same', 'gone.md': None}, 'overlay_views', skip_unchanged=True)['state'], 'unchanged')
        self.assertEqual(journals(place), before)
