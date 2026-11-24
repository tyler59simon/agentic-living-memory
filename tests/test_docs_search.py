"""Notebook page search (NS-2B): the derived page index, `search --pages`, `doc tasks`, `doc query`, and the `pages` array of
search and ask. Design: docs/notebooks-design.md sections 8.1 and 12b."""
import json,sqlite3,sys,time
from unittest.mock import patch
from test_docs import Fixture
from test_docs_review import junction
from core import backup,docs,docs_index,governance,render,runtime
from core.repository import digest

PAGE=b'---\ntitle: Retry policy\nstatus: draft\npriority: 2\ntags: [ops, net]\n---\n# Retry policy\n\nBackoff doubles each attempt up to a ceiling.\n\n- [ ] tune the jitter \xf0\x9f\x93\x85 2026-12-01\n- [x] write the first draft\n\n```\n- [ ] not a task inside a fence\n```\n'


class Search(Fixture):
    def setUp(self):
        super().setUp();self.notebook();self.page('Retry policy.md',PAGE)

    def test_search_and_ask_list_pages_apart_from_notes(self):
        found=self.ok('search','backoff ceiling')
        self.assertNotIn('Retry policy',json.dumps(found['results']))
        page=found['pages'][0]
        self.assertEqual((page['origin'],page['domain'],page['notebook'],page['path'],page['title']),('lm','demo','nb1','Retry policy.md','Retry policy'))
        self.assertEqual((page['revision'],page['label']),(digest(PAGE),'notebook page'))
        self.assertEqual(page['line_start'],7);self.assertNotIn('status: draft',page['snippet'])   # the heading's line; frontmatter is not prose
        asked=self.ok('ask','how does the backoff ceiling work')
        self.assertEqual([p['path'] for p in asked['pages']],['Retry policy.md']);self.assertNotIn('Retry policy',json.dumps(asked['sources']))

    def test_each_question_gets_its_own_pages(self):
        self.page('Cache.md',b'# Cache\n\nThe cache is rebuilt nightly from the warehouse.\n')
        asked=self.ok('ask','What is the backoff ceiling? When is the cache rebuilt?')
        self.assertEqual([[p['path'] for p in q['pages']] for q in asked['questions']],[['Retry policy.md'],['Cache.md']])

    def test_hidden_only_when_named_and_private_only_with_the_flag(self):
        self.notebook('quiet',visibility='hidden');self.page('q.md',b'backoff in a hidden notebook\n','quiet')
        self.notebook('secret',private=True);docs.create_page(self.root,'demo','secret','s.md',b'backoff in a private notebook\n',include_private=True)
        paths=lambda r:sorted(p['path'] for p in r['pages'])
        self.assertEqual(paths(self.ok('search','--pages','backoff')),['Retry policy.md'])
        self.assertEqual(paths(self.ok('search','backoff')),['Retry policy.md']);self.assertEqual(paths(self.ok('ask','backoff')),['Retry policy.md'])
        self.assertEqual(self.cli('search','--pages','backoff','--notebook','quiet')[0],2)   # doc search --notebook is gone: a hidden notebook is never searched
        self.assertEqual(paths(self.ok('search','--pages','backoff','--include-private')),['Retry policy.md','s.md'])
        self.assertEqual(paths(self.ok('search','backoff','--include-private')),['Retry policy.md','s.md'])
        code,text=self.fails('search','backoff','--include-private',env={'HIVEMIND_SESSION':'x'});self.assertNotIn('private notebook',text.replace('private notebooks are',''))
        docs.update_notebook(self.root,'demo','secret',private=False,include_private=True)   # private now decides, not ever-private
        self.assertEqual(paths(self.ok('search','backoff')),['Retry policy.md','s.md'])

    def test_agents_may_search_but_not_reach_private_pages(self):
        command=lambda rest:f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" {rest}'
        for rest,writes in (('search backoff --pages',False),('doc tasks --open',False),('doc query --payload-file q.json',False),('doc write demo nb1 a.md --expected-revision x',True)):
            with self.subTest(rest=rest):self.assertEqual(runtime.doc_write(command(rest),None),writes)
        self.assertTrue(runtime.include_private(command('search backoff --include-private'),None))
        self.ok('search','--pages','backoff',actor='subagent');self.ok('doc','tasks',actor='viewer')

    def test_a_write_is_searchable_at_once_and_undo_and_restore_follow(self):
        written=docs.write_page(self.root,'demo','nb1','Retry policy.md',b'# Retry policy\n\nQuokka marmalade.\n',digest(PAGE))
        db=docs_index._readonly(self.root,'demo')
        try:self.assertEqual(db.execute("SELECT sha256 FROM file WHERE path='Retry policy.md'").fetchone()[0],written['revision'])   # reindexed by the write itself
        finally:db.close()
        self.assertEqual(len(self.ok('search','--pages','quokka marmalade')['pages']),1)
        self.ok('doc','undo',written['operation_id'])
        self.assertEqual(self.ok('search','--pages','quokka marmalade')['pages'],[]);self.assertEqual(len(self.ok('search','--pages','backoff ceiling')['pages']),1)
        snapshot=backup.backup(self.root)['backup']
        docs.delete_page(self.root,'demo','nb1','Retry policy.md',digest(PAGE))
        self.assertEqual(self.ok('search','--pages','backoff ceiling')['pages'],[])
        plan=backup.restore_plan(self.root,snapshot);governance.apply(self.root,plan['plan_id'],plan['digest'])
        self.assertEqual(len(self.ok('search','--pages','backoff ceiling')['pages']),1)   # no hook on restore: the walk found it

    def test_removed_notebooks_leave_the_index(self):
        planned=docs.delete_notebook(self.root,'demo','nb1',with_pages=True);governance.apply(self.root,planned['plan_id'],planned['digest'])
        self.assertEqual(self.ok('search','--pages','backoff')['pages'],[])
        db=docs_index._readonly(self.root,'demo')
        try:self.assertEqual(db.execute('SELECT count(*) FROM file').fetchone()[0],0)
        finally:db.close()

    def test_tasks_and_properties(self):
        tasks=self.ok('doc','tasks')['tasks']
        self.assertEqual([(t['line'],t['done'],t['due']) for t in tasks],[(11,False,'2026-12-01'),(12,True,None)])
        self.assertEqual(len(self.ok('doc','tasks','--open')['tasks']),1)
        self.page('Other.md',b'---\nstatus: done\npriority: 10\n---\n# Other\n')
        query=self.base/'q.json'
        query.write_text(json.dumps({'where':[{'key':'priority','op':'>=','value':2}],'sort':[{'key':'priority','order':'desc'}]}))
        rows=self.ok('doc','query','--payload-file',str(query))['pages']
        self.assertEqual([r['path'] for r in rows],['Other.md','Retry policy.md'])   # 10 > 2 as numbers, not as text
        self.assertEqual(rows[1]['properties'],{'title':'Retry policy','status':'draft','priority':'2','tags':['ops','net']})
        query.write_text(json.dumps({'where':[{'key':'tags','op':'=','value':'net'}]}))
        self.assertEqual([r['path'] for r in self.ok('doc','query','--payload-file',str(query))['pages']],['Retry policy.md'])
        query.write_text(json.dumps({'where':[{'key':'status','op':'like'}]}))
        self.fails('doc','query','--payload-file',str(query))

    def test_a_small_budget_leaves_pages_stale_and_the_next_calls_finish(self):
        for n in range(6):self.notebook(f'nb{n+2}');self.page('p.md',f'# P\n\nzephyr {n}\n'.encode(),f'nb{n+2}')
        docs_index.db_path(self.root,'demo').unlink()
        with patch('core.docs_index.WALK_SECONDS',0),patch('core.docs_index.INDEX_SECONDS',0):
            first=self.ok('search','--pages','zephyr')
            self.assertTrue(first.get('pages_stale'))
            for _ in range(30):
                if not self.ok('search','--pages','zephyr').get('pages_stale'):break
        self.assertEqual(len(self.ok('search','--pages','zephyr','--limit','20')['pages']),6)

    def test_a_damaged_index_is_rebuilt(self):
        docs_index.db_path(self.root,'demo').write_bytes(b'not a database')
        self.assertEqual(len(self.ok('search','--pages','backoff')['pages']),1)
        self.assertEqual(len(self.ok('search','--pages','backoff','--rebuild')['pages']),1)

    def test_credentials_are_redacted_in_snippets(self):
        self.page('Keys.md',b'# Keys\n\nThe deploy key is AKIAIOSFODNN7EXAMPLE for the zephyr bucket.\n')
        snippet=self.ok('search','--pages','zephyr bucket')['pages'][0]['snippet']
        self.assertNotIn('AKIAIOSFODNN7EXAMPLE',snippet);self.assertIn('<redacted',snippet)

    def test_a_linked_notebook_folder_is_not_indexed(self):
        outside=self.base/'outside';outside.mkdir();(outside/'notebook.json').write_text('{"title":"x"}');(outside/'x.md').write_text('zephyr outside\n')
        if not junction(self.root/'domains/demo/notebooks/linked',outside):self.skipTest('no symlink or junction')
        self.assertEqual(self.ok('search','--pages','zephyr')['pages'],[])

    def test_terminal_output_lists_pages(self):
        text=render.render(self.ok('search','backoff ceiling'))
        self.assertIn('Notebook pages:',text);self.assertIn('demo/nb1/Retry policy.md:7',text)


if __name__=='__main__':
    import unittest;unittest.main()
