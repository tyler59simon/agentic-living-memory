"""Regression tests for the pre-existing findings of the 2026-11-06 Astra whole-repo review."""
import contextlib, io, json, re, threading, uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, unquote

from test_core import StoreFixture
from core import runtime, sync
from core.cli import main as cli
from core.governance import apply, merge_plan
from core.notes import change, find_note
from core.repository import inventory, read_json
from core.storage import validate_store

DRAFT = ('---\n{id}title: "{title}"\ntype: "knowledge"\ntags: ["subject/project", "concern/architecture"]\n---\n\n'
         '## 📌 Executive Summary\n{summary}\n')


def links(body):
    return [unquote(m) for m in re.findall(r'\]\(([^)]+)\)', body)]


class VaultSync(StoreFixture):
    def setUp(self):
        super().setUp()
        self.vault = self.base / 'vault'

    def draft(self, name, title, summary, note_id=None):
        path = self.vault / 'Domains/demo' / name
        path.write_text(DRAFT.format(id=f'id: "{note_id}"\n' if note_id else '', title=title, summary=summary), encoding='utf-8')
        return path

    def test_rename_does_not_overwrite_an_unmanaged_file(self):
        a = self.note()
        sync.build_overlay('demo', self.vault, self.root)
        old_copy = self.vault / 'Domains/demo' / Path(a['path']).name
        foreign = self.vault / 'Domains/demo' / old_copy.name.replace('cache-', 'cache-layer-')
        foreign.write_text('My own notes, not memory.\n', encoding='utf-8')
        change(a['id'], 'demo', 'rename', self.root, title='Cache layer')  # agent writes sync the vault too
        self.assertEqual(find_note(a['id'], 'demo', self.root)[0].name, foreign.name)
        result = sync.build_overlay('demo', self.vault, self.root)
        self.assertEqual(foreign.read_text(encoding='utf-8'), 'My own notes, not memory.\n')
        self.assertIn('Cache layer', old_copy.read_text(encoding='utf-8'))
        self.assertGreaterEqual(result['conflicts'], 1)
        self.assertIn('Not renamed to', (self.vault / 'Dashboards/Sync Status - demo.md').read_text(encoding='utf-8'))
        foreign.unlink()
        sync.build_overlay('demo', self.vault, self.root)
        self.assertIn('Cache layer', foreign.read_text(encoding='utf-8'))
        self.assertFalse(old_copy.exists())

    def test_draft_saved_during_sync_is_kept_and_captured_next_time(self):
        self.note()
        sync.build_overlay('demo', self.vault, self.root)
        draft = self.draft('Vault idea.md', 'Vault idea', 'First wording.')
        real = sync.validate
        def save_again(note, domain, root, new=False):
            real(note, domain, root, new=new)
            if new and draft.read_text(encoding='utf-8').count('First wording.'):
                draft.write_text(draft.read_text(encoding='utf-8').replace('First wording.', 'Second wording.'), encoding='utf-8')
        with patch.object(sync, 'validate', save_again):
            sync.build_overlay('demo', self.vault, self.root)
        self.assertTrue(draft.exists())
        self.assertIn('Second wording.', draft.read_text(encoding='utf-8'))
        self.assertNotIn('Vault idea', [n['title'] for n in inventory('demo', self.root)])
        sync.build_overlay('demo', self.vault, self.root)
        ideas = [n for n in inventory('demo', self.root) if n['title'] == 'Vault idea']
        self.assertEqual(len(ideas), 1)
        self.assertIn('Second wording.', ideas[0]['body'])
        self.assertFalse(draft.exists())

    def test_two_new_drafts_with_one_id_capture_once(self):
        self.note()
        sync.build_overlay('demo', self.vault, self.root)
        shared = str(uuid.uuid4())
        self.draft('Queue.md', 'Queue retry policy', 'Retries back off exponentially.', shared)
        self.draft('Logo.md', 'Logo color choice', 'The logo stays dark blue.', shared)
        sync.build_overlay('demo', self.vault, self.root)
        self.assertEqual([n['id'] for n in inventory('demo', self.root)].count(shared), 1)
        errors = read_json(self.root / 'domains/demo/sync-state.json')['capture_errors']
        self.assertEqual(list(errors.values()), ['Duplicate ID'])
        self.assertTrue((self.vault / next(iter(errors))).exists())


class SectionUpdates(StoreFixture):
    def test_concurrent_section_updates_both_survive(self):
        n = self.note()
        from core import cli as cli_module
        real = cli_module._sectioned
        other = threading.Thread(target=cli, args=(['--root', str(self.root), '--domain', 'demo', 'update', n['id'],
                                                    '--detail', '**Second:** from the other writer'],))
        def racing(args, base):
            if not other.ident:
                # Another writer arrives after this update read the note and before it writes.
                other.start();other.join(0.5)
            return real(args, base)
        with patch.object(cli_module, '_sectioned', racing), contextlib.redirect_stdout(io.StringIO()):
            cli(['--root', str(self.root), '--domain', 'demo', 'update', n['id'], '--summary', 'The cache is disposable.'])
            other.join()
        body = find_note(n['id'], 'demo', self.root)[1]['body']
        self.assertIn('The cache is disposable.', body)
        self.assertIn('from the other writer', body)


class Merge(StoreFixture):
    def test_a_note_cannot_be_merged_into_itself_by_prefix(self):
        a = self.note();b = self.note('Queue', 'Retries back off exponentially.')
        for sources in (['demo:' + a['id'][:8]], ['demo:' + b['id'], 'demo:' + b['id'][:8]]):
            with self.subTest(sources=sources), self.assertRaisesRegex(ValueError, 'distinct notes'):
                merge_plan(self.root, {'target': 'demo:' + a['id'], 'sources': sources, 'body': 'Combined.'})

    def test_merge_keeps_links_to_and_from_archived_sources(self):
        target = self.note()
        other = self.note('Queue', 'Retries back off exponentially.')
        source = self.note('Cache warmup', f'Warmup is described in [queue]({quote(Path(other["path"]).name)}).')
        self.note('Pointer', f'See [warmup]({quote(Path(source["path"]).name)}).')
        proposal = merge_plan(self.root, {'target': 'demo:' + target['id'][:8], 'sources': ['demo:' + source['id']], 'body': 'Combined.'})
        apply(self.root, proposal['plan_id'], proposal['digest'])
        archived, merged = find_note(source['id'], 'demo', self.root)
        self.assertEqual((archived.parent.name, merged['superseded_by']), ('archive', target['id']))
        for note in (merged, next(n for n in inventory('demo', self.root) if n['title'] == 'Pointer')):
            with self.subTest(note=note['title']):
                for link in links(note['body']):
                    self.assertTrue((Path(note['path']).parent / link).resolve().is_file(), link)


class CopilotPrompt(StoreFixture):
    def test_transformed_prompt_alone_drives_retrieval(self):
        n = self.note()
        payload = {'cwd': str(self.base / 'project'), 'sessionId': 's', 'timestamp': 1,
                   'transformedPrompt': 'How does our local cache get rebuilt?'}
        result = runtime.process('copilot', 'user-prompt', payload, self.root)
        self.assertIn(n['id'][:8], result['context'])


class StoreValidation(StoreFixture):
    def test_abandoned_operation_does_not_block_validation(self):
        self.note()
        journal = self.root / '.operations' / 'gone' / 'journal.json'
        journal.parent.mkdir(parents=True)
        journal.write_text(json.dumps({'schema': 1, 'id': 'gone', 'kind': 'update', 'state': 'abandoned', 'effects': []}))
        validate_store(self.root)
        journal.write_text(json.dumps({'schema': 1, 'id': 'gone', 'kind': 'update', 'state': 'prepared', 'effects': []}))
        with self.assertRaisesRegex(ValueError, 'Pending operation'):
            validate_store(self.root)
