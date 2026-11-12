"""#13 ingest-plan: Claude Code auto-memory files become reviewed notes."""
import contextlib, io, json, os, unittest
from pathlib import Path
from test_core import StoreFixture
from core.cli import main
from core.governance import apply, undo
from core.ingest import default_source, parse
from core.notes import capture, find_note
from core.repository import inventory

TAXONOMY = ['subject/project', 'subject/user', 'concern/architecture', 'concern/decision', 'concern/preference', 'concern/knowledge']


def memory_file(folder, name, kind, description, body, nested=True):
    meta = f'metadata:\n  type: {kind}\n' if nested else f'type: {kind}\n'
    (folder / f'{name}.md').write_text(f'---\nname: {name}\ndescription: {description}\n{meta}---\n\n{body}\n', encoding='utf-8')


class Ingest(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root / 'tag_taxonomy.json').write_text(json.dumps({'tags': TAXONOMY}))
        self.source = self.base / 'auto-memory'
        self.source.mkdir()
        memory_file(self.source, 'prefers-terse-replies', 'feedback', 'The user wants short answers.',
                    'Keep replies under ten lines.\n**Why:** they read on a phone.\n**How to apply:** summarise first.')
        memory_file(self.source, 'staging-box-is-slow', 'project', 'The staging box takes 20 minutes to deploy.',
                    '## Workaround\nDeploy at night. See [[deploy-notes]].', nested=False)
        memory_file(self.source, 'grafana-dashboard', 'reference', 'Latency dashboard lives in Grafana.', 'https://grafana.example/d/latency')
        memory_file(self.source, 'user-role', 'user', 'The user is a data scientist.', 'New to the frontend.')
        (self.source / 'MEMORY.md').write_text('- [Prefers terse](prefers-terse-replies.md)\n', encoding='utf-8')

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), '--domain', 'demo', *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def ingest(self, *extra):
        code, result = self.cli('ingest-plan', '--source', str(self.source), *extra)
        self.assertEqual(code, 0, result)
        return result

    def test_mapping_and_layout(self):
        result = self.ingest()
        types = {c['file']: c['type'] for c in result['created']}
        self.assertEqual(types, {'prefers-terse-replies.md': 'preference', 'staging-box-is-slow.md': 'context',
                                 'grafana-dashboard.md': 'knowledge', 'user-role.md': 'preference'})
        self.assertNotIn('MEMORY.md', json.dumps(result))
        self.assertEqual(inventory('demo', self.root), [])  # only a plan so far
        apply(self.root, result['plan_id'], result['digest'])
        notes = {n['title']: n for n in inventory('demo', self.root)}
        terse = notes['Prefers terse replies']
        self.assertTrue(terse['body'].startswith('The user wants short answers.\n\n## Details'))
        self.assertIn('- **Why:** they read on a phone.', terse['body'])
        self.assertEqual(terse['tags'], ['concern/preference', 'subject/user'])
        slow = notes['Staging box is slow']
        self.assertIn('- **Workaround**', slow['body'])
        self.assertIn('[[deploy-notes]]', slow['body'])
        self.assertEqual(slow['tags'], ['concern/knowledge', 'subject/project'])
        self.assertTrue(terse['capture_key'].startswith('auto-memory:'))

    def test_rerun_and_after_apply_are_idempotent(self):
        first = self.ingest()
        second = self.ingest()
        self.assertEqual(len(second['created']), 4)  # nothing applied yet: same proposal
        apply(self.root, first['plan_id'], first['digest'])
        third = self.ingest()
        self.assertEqual((third['created'], third['plan_id']), ([], None))
        self.assertEqual(len(third['unchanged']), 4)
        with self.assertRaises(ValueError):
            apply(self.root, second['plan_id'], second['digest'])  # the stale proposal can no longer apply cleanly
        self.assertEqual(len(inventory('demo', self.root)), 4)

    def test_skip_rules(self):
        (self.source / 'huge.md').write_text('---\nname: huge\nmetadata:\n  type: project\n---\n' + 'x' * 70000, encoding='utf-8')
        memory_file(self.source, 'odd', 'mystery', 'Unknown kind.', 'Body.')
        (self.source / 'binary.md').write_bytes(b'\xff\xfe\x00bad')
        try:
            os.symlink(self.source / 'user-role.md', self.source / 'link.md')
            linked = True
        except OSError:
            linked = False  # Windows without symlink rights
        reasons = {s['file']: s['reason'] for s in self.ingest()['skipped']}
        self.assertIn('larger than 64 KB', reasons['huge.md'])
        self.assertIn("unknown auto-memory type 'mystery'", reasons['odd.md'])
        self.assertEqual(reasons['binary.md'], 'not UTF-8 text')
        if linked:
            self.assertEqual(reasons['link.md'], 'symbolic link')

    def test_secrets_are_skipped_unless_redacted(self):
        memory_file(self.source, 'deploy-token', 'reference', 'The deploy token.', 'token = ghp_' + 'a1B2' * 9)
        skipped = {s['file']: s['reason'] for s in self.ingest()['skipped']}
        self.assertIn('credential', skipped['deploy-token.md'])
        result = self.ingest('--redact')
        made = next(c for c in result['created'] if c['file'] == 'deploy-token.md')
        self.assertTrue(made['redacted'])
        apply(self.root, result['plan_id'], result['digest'])
        body = next(n for n in inventory('demo', self.root) if n['title'] == 'Deploy token')['body']
        self.assertIn("<redacted type=", body)
        self.assertNotIn('ghp_', body)

    def test_similar_titles_are_skipped_unless_allowed(self):
        capture('Staging box is slow', 'Known.', ['subject/project', 'concern/knowledge'], 'demo', self.root)
        reasons = {s['file']: s['reason'] for s in self.ingest()['skipped']}
        self.assertIn('similar to demo:', reasons['staging-box-is-slow.md'])
        allowed = {s['file'] for s in self.ingest('--allow-similar')['skipped']}
        self.assertNotIn('staging-box-is-slow.md', allowed)

    def test_apply_and_undo(self):
        result = self.ingest()
        receipt = apply(self.root, result['plan_id'], result['digest'])
        plan = undo(self.root, receipt['operation_id'])
        apply(self.root, plan['plan_id'], plan['digest'])
        self.assertEqual(inventory('demo', self.root), [])

    def test_not_for_subagents_or_auto_allow(self):
        from core import runtime
        self.assertNotIn('ingest-plan', runtime.ALLOWED_SUBCOMMANDS)
        self.assertEqual(self.cli('--actor', 'subagent', 'ingest-plan', '--source', str(self.source))[0], 2)

    def test_default_source_and_parser(self):
        self.assertEqual(default_source(r'C:\Users\me\My Project') if os.name == 'nt' else default_source('/home/me/My Project'),
                         self.base / 'home-claude' / 'projects' / ('C--Users-me-My-Project' if os.name == 'nt' else '-home-me-My-Project') / 'memory')
        meta, body = parse('\ufeff---\r\nname: "quoted"\r\nmetadata:\r\n  type: user\r\n---\r\n\r\nBody text\r\n')
        self.assertEqual((meta, body), ({'name': 'quoted', 'metadata': {'type': 'user'}}, 'Body text'))
        self.assertEqual(parse('no frontmatter'), ({}, 'no frontmatter'))

    def test_missing_source_fails(self):
        self.assertEqual(self.cli('ingest-plan', '--source', str(self.base / 'nope'))[0], 2)


if __name__ == '__main__':
    unittest.main()
