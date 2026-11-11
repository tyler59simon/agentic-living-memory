"""Index schema v3: commands, repo and vocab, vectors that survive a schema bump, and a stale index at hook time."""
import json
import sqlite3
from unittest.mock import patch

from test_core import StoreFixture
from test_semantic import write_model
from core import runtime, semantic, search
from core.notes import capture, change
from core.repository import inventory

TAGS = ['subject/project', 'concern/decision']


def table(root, sql, *args):
    db = sqlite3.connect(root / 'domains' / 'demo' / 'search.sqlite3')
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


class IndexV3(StoreFixture):
    def setUp(self):
        super().setUp()
        self.cwd = str(self.base / 'project')

    def test_schema_has_commands_repo_and_a_vocabulary_of_active_notes(self):
        self.assertEqual(search.INDEX_VERSION, 3)
        a = self.note('Cache rebuild', 'The cache rebuilds from canonical notes.')
        gone = self.note('Webcam driver', 'Reinstall the webcam driver.', type='knowledge')
        change(gone['id'], 'demo', 'archive', self.root)
        search.rebuild_index('demo', self.root)
        columns = [r[1] for r in table(self.root, 'PRAGMA table_info(notes)')]
        self.assertIn('commands', columns)
        self.assertIn('repo', columns)
        self.assertEqual(table(self.root, 'SELECT commands, repo FROM notes WHERE id=?', a['id']), [('[]', '')])
        vocab = dict(table(self.root, 'SELECT stem, df FROM vocab'))
        self.assertEqual(vocab['cache'], 1)
        self.assertNotIn('webcam', vocab)   # archived notes add nothing
        self.assertEqual(search.vocabulary(['demo'], self.root), (1, vocab))

    def test_vocabulary_counts_notes_not_occurrences_and_can_be_limited(self):
        self.note('Cache rebuild', 'cache cache cache and the cache again')
        self.note('Cache eviction', 'The cache uses LRU.', allow_similar=True)
        search.rebuild_index('demo', self.root)
        total, df = search.vocabulary(['demo'], self.root)
        self.assertEqual((total, df['cache'], df['lru']), (2, 2, 1))
        self.assertEqual(search.vocabulary(['demo'], self.root, ['caches', 'unknown']), (2, {'cache': 2}))   # looked up by stem
        self.assertEqual(search.vocabulary(['demo', 'nowhere-at-all'], self.root)[0], 2)

    def test_an_index_without_the_table_adds_nothing(self):
        self.note()
        search.rebuild_index('demo', self.root)
        db = sqlite3.connect(self.root / 'domains' / 'demo' / 'search.sqlite3')
        db.execute('DROP TABLE vocab')
        db.commit()
        db.close()
        self.assertEqual(search.vocabulary(['demo'], self.root), (0, {}))

    def test_a_schema_bump_makes_the_index_stale_and_a_rebuild_makes_it_fresh(self):
        self.note()
        search.rebuild_index('demo', self.root)
        notes = inventory('demo', self.root, retired=True)
        self.assertTrue(search._fresh('demo', self.root, notes))
        with patch.object(search, 'INDEX_VERSION', 4):
            self.assertFalse(search._fresh('demo', self.root, notes))

    def test_session_start_rebuilds_a_stale_index_without_a_model(self):
        self.note('Cache rebuild', 'The cache rebuilds from canonical notes.')
        with patch.object(search, 'INDEX_VERSION', 2):
            search.rebuild_index('demo', self.root)   # as an older release left it
        notes = inventory('demo', self.root, retired=True)
        self.assertFalse(search._fresh('demo', self.root, notes))
        info = {}
        runtime._process('claude', 'session-start', {'cwd': self.cwd, 'session_id': 's1'}, self.root, info)
        self.assertTrue(info['detail']['reindexed'])
        self.assertTrue(search._fresh('demo', self.root, notes))
        self.assertEqual(search.vocabulary(['demo'], self.root)[0], 1)
        info = {}
        runtime._process('claude', 'session-start', {'cwd': self.cwd, 'session_id': 's2'}, self.root, info)
        self.assertFalse(info['detail']['reindexed'])   # a fresh index is left alone

    def test_session_start_skips_a_very_large_domain_and_survives_a_failure(self):
        self.note()
        with patch.object(search, 'INDEX_VERSION', 2):
            search.rebuild_index('demo', self.root)
        with patch.object(search, 'REFRESH_MAX', 0):
            self.assertFalse(search.refresh_stale('demo', self.root))
        with patch.object(search, 'rebuild_index', side_effect=OSError('disk full')):
            self.assertIn('Living Memory', runtime.process('claude', 'session-start', {'cwd': self.cwd, 'session_id': 's3'}, self.root)['context'])

    def test_an_up_to_date_index_is_left_alone(self):
        self.assertFalse(search.refresh_stale('demo', self.root))
        self.note()
        search.rebuild_index('demo', self.root)
        with patch.object(search, 'rebuild_index', side_effect=AssertionError('fresh index rebuilt')):
            self.assertFalse(search.refresh_stale('demo', self.root))

    def test_a_stale_index_gives_no_hint_and_no_error_at_tool_time(self):
        project = self.base / 'project'
        capture('Parser notes', 'The parser lives in src/parser.py.', TAGS, 'demo', self.root, files=['src/parser.py'])
        search.rebuild_index('demo', self.root)
        target = str(project / 'src' / 'parser.py')
        payload = {'cwd': self.cwd, 'session_id': 's1', 'tool_name': 'Edit', 'tool_input': {'file_path': target}}
        runtime.process('claude', 'session-start', {'cwd': self.cwd, 'session_id': 's1'}, self.root)
        fresh = runtime.process('claude', 'pre-tool-use', {**payload, 'session_id': 's0'}, self.root)
        self.assertIn('Parser notes', fresh.get('context') or '')   # with a fresh index the hint is there
        note_path = next((self.root / 'domains' / 'demo' / 'notes').glob('*.md'))
        note_path.write_text(note_path.read_text(encoding='utf-8').replace('src/parser.py', 'src/parser.py and more'), encoding='utf-8')   # outside the scripts: the index is stale
        result = runtime.process('claude', 'pre-tool-use', payload, self.root)
        self.assertNotIn('Parser notes', result.get('context') or '')
        # The same with the index gone altogether.
        (self.root / 'domains' / 'demo' / 'search.sqlite3').unlink()
        again = runtime.process('claude', 'pre-tool-use', {**payload, 'session_id': 's2', 'tool_input': {'file_path': target}}, self.root)
        self.assertNotIn('Parser notes', again.get('context') or '')

    def test_an_old_schema_index_still_serves_prompts_and_hints(self):
        capture('Parser notes', 'The parser lives in src/parser.py.', TAGS, 'demo', self.root, files=['src/parser.py'])
        with patch.object(search, 'INDEX_VERSION', 2):
            search.rebuild_index('demo', self.root)
        db = sqlite3.connect(self.root / 'domains' / 'demo' / 'search.sqlite3')
        db.execute('DROP TABLE vocab')
        db.execute('ALTER TABLE notes DROP COLUMN commands')
        db.execute('ALTER TABLE notes DROP COLUMN repo')
        db.commit()
        db.close()
        self.assertEqual(len(search.file_notes('src/parser.py', ['demo'], self.root, ensure_fresh=False)), 1)
        self.assertEqual(runtime.context(self.cwd, 'where is the parser', self.root, 'demo')[1] != [], True)


class VectorsSurviveABump(StoreFixture):
    def setUp(self):
        super().setUp()
        self.source = self.base / 'tiny'
        hashes = write_model(self.source)
        models = patch.dict(semantic.MODELS, {'tiny': {'repo': 'none/tiny', 'revision': '0', 'min_similarity': 0.45, 'files': hashes}}, clear=True)
        models.start()
        self.addCleanup(models.stop)
        self.addCleanup(semantic._unload)

    def vectors(self):
        return dict(table(self.root, 'SELECT n.id, v.vec FROM vectors v JOIN notes n ON n.rowid = v.rowid'))

    def test_the_embedding_identity_does_not_move_with_the_schema_version(self):
        semantic.install(self.root, 'tiny', self.source)
        before = search._vector_identity(self.root)
        with patch.object(search, 'INDEX_VERSION', 99):
            self.assertEqual(search._vector_identity(self.root), before)
        with patch.object(search, 'EMBED_VERSION', 99):
            self.assertNotEqual(search._vector_identity(self.root), before)
        self.assertTrue(before.startswith(f'{search.EMBED_VERSION}:tiny@'))

    def test_a_schema_bump_reuses_every_stored_vector(self):
        self.note('Layout rules', 'Sections follow a fixed order.')
        self.note('Cache', 'The local cache is rebuildable.', allow_similar=True)
        semantic.install(self.root, 'tiny', self.source)
        before = self.vectors()
        self.assertEqual(len(before), 2)
        model = semantic.load(self.root)
        with patch.object(search, 'INDEX_VERSION', 4), patch.object(model, 'embed', wraps=model.embed) as embed:
            notes = inventory('demo', self.root, retired=True)
            self.assertFalse(search._fresh('demo', self.root, notes))   # the bump made the index stale ...
            self.assertTrue(search.refresh_stale('demo', self.root))     # ... and SessionStart's rebuild is lexical
            embed.assert_not_called()                                    # ... with no re-embedding
            self.assertTrue(search._fresh('demo', self.root, notes))
        self.assertEqual(self.vectors(), before)

    def test_a_changed_embedding_recipe_does_re_embed(self):
        self.note('Layout rules', 'Sections follow a fixed order.')
        semantic.install(self.root, 'tiny', self.source)
        model = semantic.load(self.root)
        with patch.object(search, 'EMBED_VERSION', 99), patch.object(model, 'embed', wraps=model.embed) as embed:
            search.rebuild_index('demo', self.root)
            self.assertEqual(embed.call_count, 1)

    def test_only_a_changed_note_is_embedded_when_the_index_is_refreshed(self):
        a = self.note('Layout rules', 'Sections follow a fixed order.')
        self.note('Cache', 'The local cache is rebuildable.', allow_similar=True)
        semantic.install(self.root, 'tiny', self.source)
        path = next((self.root / 'domains' / 'demo' / 'notes').glob('*Layout*'))
        path.write_text(path.read_text(encoding='utf-8').replace('fixed order', 'fixed order, always'), encoding='utf-8')   # edited outside the scripts
        model = semantic.load(self.root)
        with patch.object(model, 'embed', wraps=model.embed) as embed:
            self.assertTrue(search.refresh_stale('demo', self.root))
            self.assertEqual(embed.call_count, 1)
        self.assertEqual(len(self.vectors()), 2)
        self.assertIn(a['id'], self.vectors())


if __name__ == '__main__':
    import unittest
    unittest.main()
