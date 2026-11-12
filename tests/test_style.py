"""Advisory writing checks (WRITING.md) and the reviewed rewrite of several notes' prose."""
import contextlib
import io
import json
from pathlib import Path

from test_core import StoreFixture
from core import governance
from core.cli import main as cli
from core.notes import capture, find_note, quality_warnings, rewrite_plan
from core.style import style_warnings

TAGS = ['subject/project', 'concern/decision']


def kinds(warnings):
    return [w.split('(')[0].strip() for w in warnings]


class Style(StoreFixture):
    def test_plain_notes_have_no_style_warnings(self):
        for note in ({'type': 'knowledge', 'title': 'Use the venv interpreter',
                      'body': 'System Python lacks pytest, so run tests with `.venv/Scripts/python.exe -m pytest`.'},
                     {'type': 'decision', 'title': 'SQLite for the index', 'body': 'On 2026-11-12 the user chose SQLite.\n\n## Why\nIt needs no server.'},
                     {'type': 'bug', 'title': 'Hook drops input', 'body': 'The hook drops the prompt.\n\n## Cause\nstdin is read as cp1252.'}):
            with self.subTest(note['title']):
                self.assertEqual(style_warnings(note), [])

    def test_ai_habits_are_flagged_once_each(self):
        body = ('The index serves as a pivotal layer \u2014 it\u2019s \U0001f680 fast. Let me know.\n\n'
                '## Details\n- It uses **bold** for emphasis.\n- Additionally, it is crucial.')
        found = ' | '.join(style_warnings({'type': 'context', 'title': 'Index', 'body': body}))
        for part in ('em or en dash', 'curly quotation marks', 'emoji', 'chat phrasing', 'stock AI word', 'inflated or staged', 'bold used for emphasis'):
            self.assertIn(part, found)
        self.assertEqual(found.count('stock AI word'), 1)

    def test_code_paths_links_and_labels_are_not_prose(self):
        body = ('Run `git log --format=%h \u2014 x` from C:/Users/me/repo/delve.py or /tmp/crucial.\n\n'
                '> **Important:** keep the lock.\n\n## Details\n- **Cause:** a stale key.\n- See [[A pivotal note]] and https://example.com/showcase.')
        self.assertEqual(style_warnings({'type': 'context', 'title': 'Paths', 'body': body}), [])

    def test_long_and_cut_off_text(self):
        long = ' '.join(['word'] * 70) + '.'
        found = ' | '.join(style_warnings({'type': 'context', 'title': 'T', 'body': long + '\n\n## Details\n- ' + long}))
        self.assertIn('the summary has 70 words', found)
        self.assertIn('a bullet has 70 words', found)
        cut = style_warnings({'type': 'knowledge', 'title': 'T', 'body': 'After pytest passes, CI runs the check. It fails with public'})
        self.assertTrue(any('stop mid-sentence' in w for w in cut))
        self.assertEqual(style_warnings({'type': 'map', 'title': 'T', 'body': long + '\n\n## Details\n- **Layout:** ' + long}), [])

    def test_shape_follows_the_type(self):
        self.assertIn("shape: a decision note says why", ' '.join(style_warnings({'type': 'decision', 'title': 'T', 'body': 'We chose SQLite.'})))
        self.assertEqual(style_warnings({'type': 'decision', 'title': 'T', 'body': 'We chose SQLite.\n\n## Details\n- **Why:** no server.'}), [])
        self.assertIn('shape: a bug note', ' '.join(style_warnings({'type': 'bug', 'title': 'T', 'body': 'It crashes.'})))
        self.assertIn('shape: a research note', ' '.join(style_warnings({'type': 'research', 'title': 'T', 'body': 'Which store?'})))
        labelled = 'The cache is disposable.\n\n## Details\n- **Where:** disk.\n- **Rebuild:** run search --rebuild.'
        self.assertIn('shape: a knowledge note', ' '.join(style_warnings({'type': 'knowledge', 'title': 'T', 'body': labelled})))
        article = 'The cache is disposable.\n\n## Rebuilding it\nRun `search --rebuild`.'
        self.assertEqual(style_warnings({'type': 'knowledge', 'title': 'T', 'body': article}), [])

    def test_capture_returns_style_warnings(self):
        result = capture('Index choice', 'The index serves as a pivotal layer.', TAGS, 'demo', self.root, type='decision')
        self.assertEqual(result['status'], 'created')
        self.assertTrue(any(w.startswith('style: stock AI word') for w in result['quality_warnings']))
        self.assertTrue(any(w.startswith('shape: a decision') for w in result['quality_warnings']))
        self.assertEqual(quality_warnings({'type': 'plan', 'body': 'Pending approval: next steps are A then B.'}), [])


class Rewrite(StoreFixture):
    def run_cli(self, *args, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli(['--root', str(self.root), '--domain', 'demo', *args])
        return json.loads(out.getvalue())

    def made(self, title, body, **kw):
        n = capture(title, body, TAGS, 'demo', self.root, **kw)
        return find_note(n['id'], 'demo', self.root)[1]

    def test_one_plan_rewrites_several_notes_and_keeps_dates(self):
        a = self.made('Index choice', 'The index serves as a pivotal layer.', type='decision')
        b = self.made('Cache layer', 'The cache is disposable.', type='status')
        payload = [{'ref': f"demo:{a['id']}", 'revision': a['revision'], 'body': 'On 2026-11-12 the user chose SQLite.\n\n## Why\nNo server.'},
                   {'ref': b['id'], 'revision': b['revision'], 'body': 'The cache can be deleted and rebuilt.'}]
        result = rewrite_plan(payload, 'demo', self.root)
        self.assertEqual(result['summary'], {'notes': 2, 'titles_unchanged': True})
        self.assertEqual([n['ref'] for n in result['notes']], [f"demo:{a['id']}", f"demo:{b['id']}"])
        self.assertNotIn('quality_warnings', result['notes'][0])
        self.assertEqual(find_note(a['id'], 'demo', self.root)[1]['body'], a['body'])   # nothing changes before apply
        inspected = governance.inspect_plan(self.root, result['plan_id'], full=True)
        self.assertEqual(len(inspected['effects']), 2)
        self.assertFalse(any(e['truncated'] for e in inspected['effects']))
        governance.apply(self.root, result['plan_id'], result['digest'])
        after_a = find_note(a['id'], 'demo', self.root)[1]
        after_b = find_note(b['id'], 'demo', self.root)[1]
        self.assertEqual(after_a['body'], 'On 2026-11-12 the user chose SQLite.\n\n## Why\nNo server.')
        self.assertEqual((after_a['title'], after_a['updated']), (a['title'], a['updated']))
        self.assertEqual((after_b['updated'], after_b.get('review_by')), (b['updated'], b.get('review_by')))

    def test_refusals(self):
        a = self.made('Index choice', 'We chose SQLite.', type='decision')
        cases = (([], 'JSON list'),
                 ([{'ref': a['id'], 'revision': a['revision'], 'body': 'x.', 'title': 'New'}], 'never changes titles'),
                 ([{'ref': a['id'], 'revision': 'f' * 64, 'body': 'New text.'}], 'stale_revision'),
                 ([{'ref': a['id'], 'revision': a['revision']}], 'non-empty strings'),
                 ([{'ref': a['id'], 'revision': a['revision'], 'body': 'One.'}] * 2, 'appears twice'),
                 ([{'ref': a['id'], 'revision': a['revision'], 'body': '## Why\nNo summary.'}], 'Note needs a summary'))
        for payload, message in cases:
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                rewrite_plan(payload, 'demo', self.root)
        m = self.made('Repo map', 'What the repo is for.', type='map', files=[str(self.base)])
        with self.assertRaisesRegex(ValueError, 'labelled layout'):
            rewrite_plan([{'ref': m['id'], 'revision': m['revision'], 'body': 'New.'}], 'demo', self.root)

    def test_cli_rewrite_plan_from_a_payload_file(self):
        a = self.made('Index choice', 'We chose SQLite.', type='decision')
        payload = self.base / 'rewrite.json'
        payload.write_text(json.dumps([{'ref': a['id'], 'revision': a['revision'], 'body': 'We chose SQLite.\n\n## Why\nNo server.'}]), encoding='utf-8')
        result = self.run_cli('rewrite-plan', '--payload-file', str(payload))
        self.assertIn('inspect-plan PLAN_ID --full', result['hint'])
        unchanged = self.base / 'same.json'
        unchanged.write_text(json.dumps([{'ref': a['id'], 'revision': a['revision'], 'body': 'We chose SQLite.'}]), encoding='utf-8')
        self.assertEqual(self.run_cli('rewrite-plan', '--payload-file', str(unchanged))['unchanged'], [f"demo:{a['id']}"])
