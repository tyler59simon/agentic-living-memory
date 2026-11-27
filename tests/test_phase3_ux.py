"""Phase 3 UX, ergonomics, and standalone ALM verification (REL-U1 to REL-U8)."""
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from core import cli, notes, render, runtime, upgrade, views, wiki, wiring
from core.instructions import TOPICS, instruction_block, static_protocol
from core.note_types import NOTE_TYPES
from hooks import antigravity_hooks
from revision_fill import main
from test_core import StoreFixture


class Phase3Fixture(StoreFixture):
    def cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        ctx = patch.dict(os.environ, env) if env else contextlib.nullcontext()
        with ctx, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(['--root', str(self.root), '--domain', 'demo', '--json', *argv])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
        text = out.getvalue() or err.getvalue() or 'null'
        try:
            data = json.loads(text)
        except ValueError:
            data = text
        return code, data

    def ok(self, *argv, env=None):
        code, data = self.cli(*argv, env=env)
        self.assertEqual(code, 0, data)
        return data


class TestRenderAndFormatting(Phase3Fixture):
    def test_u1_render_includes_revision_and_dedicated_formatters(self):
        created = self.note('Cache layout', 'The cache lives in SQLite.')
        read_res = self.ok('read', f"demo:{created['id']}")
        text = render.render(read_res)
        self.assertIn('rev ' + read_res['revision'][:12], text)

        search_res = self.ok('search', 'cache')
        self.assertIn('rev ' + read_res['revision'][:12], render.render(search_res))

        domains_text = render.render(self.ok('domains'))
        self.assertIn('Domains', domains_text)
        self.assertIn('demo', domains_text)

        doc_text = render.render(self.ok('doctor'))
        self.assertIn('Living Memory doctor:', doc_text)

        rev_text = render.render(self.ok('review'))
        self.assertIn('Review:', rev_text)

        maint_text = render.render(self.ok('maintenance', 'status'))
        self.assertIn('Maintenance:', maint_text)

        logs_text = render.render(self.ok('logs'))
        self.assertIn('Logs (', logs_text)


class TestCliErgonomics(Phase3Fixture):
    def test_u2_repeated_filters_update_tags_and_body_file_aliases(self):
        body_file = self.base / 'body.md'
        body_file.write_text('Summary line for body file test.\n', encoding='utf-8')
        cap = self.ok('capture', '--title', 'Body file capture', '--body-file', str(body_file),
                      '--tag', 'subject/project', '--tag', 'concern/architecture')
        ref = f"demo:{cap['id']}"
        rev = self.ok('read', ref)['revision']

        # --add-tag preserves existing tags; --remove-tag removes only the named tag
        self.ok('update', ref, '--expected-revision', rev, '--add-tag', 'concern/decision')
        got = self.ok('read', ref)
        self.assertIn('subject/project', got['tags'])
        self.assertIn('concern/architecture', got['tags'])
        self.assertIn('concern/decision', got['tags'])

        self.ok('update', ref, '--expected-revision', got['revision'], '--remove-tag', 'concern/decision')
        got2 = self.ok('read', ref)
        self.assertNotIn('concern/decision', got2['tags'])
        self.assertIn('concern/architecture', got2['tags'])

        # --content-file works on update
        body_file.write_text('Updated via content-file alias.\n', encoding='utf-8')
        self.ok('update', ref, '--expected-revision', got2['revision'], '--content-file', str(body_file))
        self.assertIn('Updated via content-file alias.', self.ok('read', ref)['body'])

        # Repeated --filter tag=a --filter tag=b requires BOTH tags
        both = self.ok('search', 'content-file', '--filter', 'tag=subject/project', '--filter', 'tag=concern/architecture')
        self.assertEqual([r['id'] for r in both['results']], [cap['id']])
        miss = self.ok('search', 'content-file', '--filter', 'tag=subject/project', '--filter', 'tag=concern/decision')
        self.assertEqual(miss['results'], [])

    def test_u2_bare_id_resolves_global_notes(self):
        g = self.ok('capture', '--global', '--concern', 'decision', '--title', 'Global user tool', '--summary', 'Always use ripgrep.')
        got = self.ok('read', g['id'][:8])
        self.assertEqual(got['id'], g['id'])
        self.assertEqual(got['domain'], '_global')

    def test_u2_subagent_read_only_commands_and_status_without_session(self):
        self.note('Reviewable note', 'Some fact.')
        self.assertEqual(self.cli('review', '--actor', 'subagent')[0], 0)
        self.assertEqual(self.cli('taxonomy', '--actor', 'subagent')[0], 0)
        code, err = self.cli('taxonomy', 'add', 'concern/x', '--actor', 'subagent')
        self.assertEqual(code, 2)
        self.assertIn('read-only', err['error']['message'])

        # Bare status without session returns store summary instead of failing
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': ''}):
            st = self.ok('status')
        self.assertEqual(st['mode'], 'store')
        self.assertEqual(st['domain'], 'demo')
        self.assertIn('wiring', st)

    def test_u2_capture_finalize_env_fallback_and_wiring_permission_error(self):
        # Set up a turn so capture-finalize succeeds using CLAUDE_CODE_SESSION_ID
        runtime.process('claude', 'session-start', {'cwd': str(self.base / 'project'), 'session_id': 'env-s1'}, self.root)
        runtime.process('claude', 'user-prompt', {'cwd': str(self.base / 'project'), 'session_id': 'env-s1', 'prompt': 'hello'}, self.root)
        res = self.ok('capture-finalize', env={'CLAUDE_CODE_SESSION_ID': 'env-s1'})
        self.assertTrue(res['ok'])

        # wiring.check distinguishes PermissionError from invalid JSON
        from integrations import setup as setup_mod
        with patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'read_text', side_effect=PermissionError('denied')):
            row = wiring.check('claude', setup_mod)
        self.assertTrue(any('cannot be read' in p for p in row['problems']), row)
        self.assertFalse(any('not valid JSON' in p for p in row['problems']), row)


class TestQualityAndProtocol(Phase3Fixture):
    def test_u3_quality_checks_reduce_false_positives(self):
        clean_pending = {'title': 'Operations store', 'body': 'Pending operations live in .operations/.pending.', 'type': 'knowledge'}
        self.assertEqual(notes.quality_checks(clean_pending), [])

        clean_python = {'title': 'CI setup', 'body': 'The Python 3.13 test job runs on every PR.', 'type': 'knowledge'}
        self.assertEqual(notes.quality_checks(clean_python), [])

        settled_bug = {'title': 'Timeout bug', 'body': 'Probably caused by lock.\n\n**Root cause:** missing index.\n**Fix:** added index.', 'type': 'bug'}
        self.assertEqual(notes.quality_checks(settled_bug), [])

        # Real issues still warn
        real_pending = {'title': 'Task', 'body': 'Pending: wire the adapter.', 'type': 'knowledge'}
        self.assertTrue(any('unfinished state' in w for w in notes.quality_checks(real_pending)))
        real_count = {'title': 'Run', 'body': '42 tests passed on main.', 'type': 'knowledge'}
        self.assertTrue(any('test counts' in w for w in notes.quality_checks(real_count)))

    def test_u4_protocol_and_hook_hints(self):
        proto = static_protocol('claude')
        self.assertIn('When to save:', proto)
        self.assertIn('Updates and safety:', proto)

        factory_block = instruction_block('factory', enable_hooks=False, script='/bin/alm')
        self.assertIn("--title 'Title' --summary '...'", factory_block)
        self.assertNotIn('"Title" "Body"', factory_block)

        sub = runtime.process('claude', 'subagent-start', {'cwd': str(self.base / 'project'), 'session_id': 's-sub'}, self.root)
        for t in NOTE_TYPES:
            self.assertIn(t, sub['context'])

        rev_res = self.ok('review')
        self.assertIn('--expected-revision REV', rev_res['hint'])


class TestCrossHarnessAndLifecycle(Phase3Fixture):
    def test_u5_non_claude_harnesses_schedule_housekeeping_and_antigravity_passes_scratchpad(self):
        project = str(self.base / 'project')
        for harness in ('codex', 'copilot', 'factory', 'antigravity'):
            with patch.object(runtime, 'schedule_housekeeping', return_value='scheduled') as hk, \
                 patch.object(runtime, '_sync_vaults', return_value={}):
                runtime.process(harness, 'session-start', {'cwd': project, 'session_id': f's-{harness}'}, self.root)
                hk.assert_called_once()

        # Antigravity forwards artifactDirectoryPath as scratchpad_dir
        captured = {}
        def fake_process(harness, event, payload, root):
            captured.update(payload)
            return {}
        artifact_dir = self.base / 'antigravity-cli' / 'brain' / 'ag-1'
        with patch.object(runtime, 'process', side_effect=fake_process), \
             patch('core.storage.resolve_root', return_value=(self.root, 'test')):
            antigravity_hooks.handle('PreToolUse', {
                'hookEventName': 'PreToolUse',
                'conversationId': 'ag-1',
                'workspacePaths': [project],
                'artifactDirectoryPath': str(artifact_dir),
                'toolCall': {'name': 'write_to_file', 'args': {'TargetFile': str(artifact_dir / 'plan.md')}},
            })
        self.assertEqual(captured.get('scratchpad_dir'), str(artifact_dir))

    def test_u6_upgrade_rewire_executes_steps(self):
        with patch.object(upgrade, 'installed_stamp', return_value={'source': str(self.base), 'commit': 'a' * 40, 'interpreter': sys.executable}), \
             patch.object(upgrade, '_git', side_effect=['', 'a' * 40]), \
             patch.object(upgrade, 'head_branch', return_value='main'), \
             patch.object(upgrade, 'default_branch', return_value='main'), \
             patch.object(upgrade, '_run', return_value=(0, '', '')), \
             patch('install.next_steps', return_value=(['"py" "setup.py" claude --hooks'], ['codex'])), \
             patch.object(upgrade, '_rewire', return_value=(['claude'], [])) as rw:
            res = upgrade.run(rewire=True)
        rw.assert_called_once()
        self.assertEqual(res['rewired'], ['claude'])
        self.assertEqual(res['next_steps'], [])
        self.assertEqual(res['wiring_current'], ['claude', 'codex'])


class TestSubparsersAndStandalone(Phase3Fixture):
    def test_u7_subparsers_and_bare_defaults(self):
        # Bare defaults still work
        self.assertTrue(self.ok('proposals')['ok'])
        self.assertTrue(self.ok('proposals', '--status', 'all')['ok'])
        self.assertTrue(self.ok('views')['ok'])
        self.assertTrue(self.ok('views', '--all-domains')['ok'])
        self.assertIn('tags', self.ok('taxonomy'))
        self.assertTrue(self.ok('maintenance', 'status')['ok'])
        self.assertIn('enabled', self.ok('semantic', 'status'))
        self.assertTrue(self.ok('config', 'get')['ok'])

        # Stray flags rejected by argparse subparsers
        self.assertEqual(self.cli('maintenance', 'status', '--dry-run')[0], 2)
        self.assertEqual(self.cli('semantic', 'status', '--model', 'x')[0], 2)
        self.assertEqual(self.cli('proposals', 'list', '--reason', 'x')[0], 2)
        self.assertEqual(self.cli('views', 'list', '--url', 'https://x')[0], 2)

    def test_u8_standalone_branding_and_wording(self):
        n = self.note('Standalone test', 'Works without Hivemind.')
        read_n = self.ok('read', f"demo:{n['id']}")
        view_html = views.render_html(views.view_model(read_n, self.root), 'local')
        self.assertIn('<span>Living Memory</span>', view_html)
        self.assertIn('data:image/svg+xml', view_html)
        self.assertNotIn('<span>Hivemind</span>', view_html)

        wiki_html = wiki.render_html(wiki.wiki_model('demo', self.root))
        self.assertIn('<span>Living Memory</span>', wiki_html)
        self.assertIn('data:image/svg+xml', wiki_html)
        self.assertNotIn('<span>Hivemind</span>', wiki_html)

        self.assertNotIn('Hivemind', TOPICS['pages'][1])
        self.assertNotIn('Hivemind', runtime.PRIVATE_DENY)
        self.assertNotIn('Hivemind', runtime.folder_deny('a.md', {'path': '/nb', 'visibility': 'searchable'}, 'alm'))


if __name__ == '__main__':
    unittest.main()
