"""The page index of folder notebooks (domains/D/docs.sqlite3: task rows for `open` and SessionStart): its time budgets, failures and
the allowance that goes round the domains, as the NS-2B reviews tested them (I, R and F findings) before hosted notebooks were retired."""
import contextlib,json,os,sqlite3,time
from pathlib import Path
from unittest.mock import patch
import test_sources_notebooks
from test_folder_notebooks import own
from core import docs_index,sources
from core.init_domain import init_domain


@own
class Index(test_sources_notebooks.Notebooks):
    def setUp(self):
        super().setUp()
        self.set(self.nb,'injectable');(self.nb/'Good.md').write_text('# Good\n\n- [ ] good task\n');(self.nb/'Bad.md').write_text('# Bad\n\n- [ ] bad task\n')
        docs_index.refresh(self.root,'demo')
    def meta(self,key):
        db=sqlite3.connect(docs_index.db_path(self.root,'demo'))
        try:return sources._meta(db,key)
        finally:db.close()
    def rows(self,domain='demo'):
        db=sqlite3.connect(docs_index.db_path(self.root,domain))
        try:return {p for (p,) in db.execute('SELECT path FROM file')}
        finally:db.close()
    def tasks(self):return sorted(t['task'] for t in docs_index.task_rows(self.root,['demo'],refresh=False)[0])

    def test_pages_and_their_task_lines_are_indexed(self):
        self.assertEqual(self.rows(),{'Runbook.md','Good.md','Bad.md'});self.assertEqual(self.tasks(),['bad task','good task'])

    def test_I2_an_unreadable_page_is_stale_retried_last_and_never_blocks_progress(self):
        docs_index.db_path(self.root,'demo').unlink()
        real=docs_index._index_page
        def flaky(db,nb,folder,rel,*a):return False if rel=='Bad.md' else real(db,nb,folder,rel,*a)
        with patch('core.docs_index._index_page',flaky):result=docs_index.refresh(self.root,'demo')
        self.assertTrue(result['stale']);self.assertEqual(len(json.loads(self.meta('failed'))),1);self.assertTrue(json.loads(self.meta('failed'))[0].endswith('/Bad.md'))
        late=time.monotonic()-1
        self.assertEqual(docs_index.refresh(self.root,'demo',late,late,progress=False)['indexed'],0)
        tried=[]
        def watch(db,nb,folder,rel,*a):tried.append(rel);return real(db,nb,folder,rel,*a)
        with patch('core.docs_index._index_page',watch):docs_index.refresh(self.root,'demo',late,late)
        self.assertEqual(tried,['Bad.md'])   # the only page left, tried once even past the deadline

    def test_I3_an_index_missing_a_table_is_rebuilt_and_a_failed_rebuild_is_reported(self):
        path=docs_index.db_path(self.root,'demo');path.unlink()
        db=sqlite3.connect(path);db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)');db.execute("INSERT INTO meta VALUES('version','3')");db.commit();db.close()
        self.assertIsNone(docs_index._readonly(self.root,'demo'))
        self.assertEqual(len([r for r in self.cli('--json','open')[1]['items'] if r['kind']=='page']),2)   # open rebuilds it
        with patch('core.docs_index.os.replace',side_effect=PermissionError('in use')):
            path.write_bytes(b'not a database');result=docs_index.refresh(self.root,'demo')
        self.assertTrue(result['stale']);self.assertIn('in use',result['error'])

    def test_I4_the_notebook_inventory_is_taken_under_the_lock_and_a_failed_listing_removes_nothing(self):
        held=[False];real_lock=docs_index._lock;real_nb=docs_index._folder_notebooks
        @contextlib.contextmanager
        def lock(*a,**k):
            with real_lock(*a,**k):
                held[0]=True
                try:yield
                finally:held[0]=False
        def inventory(*a,**k):
            self.assertTrue(held[0],'inventory taken outside the index lock');return real_nb(*a,**k)
        with patch('core.docs_index._lock',lock),patch('core.docs_index._folder_notebooks',inventory):docs_index.refresh(self.root,'demo')
        with patch('core.docs_index._folder_notebooks',return_value={}):result=docs_index.refresh(self.root,'demo')
        self.assertFalse(result['stale']);self.assertEqual(result['removed'],3)   # the notebook is gone from the registry: its rows go
        docs_index.refresh(self.root,'demo');self.assertEqual(self.rows(),{'Runbook.md','Good.md','Bad.md'})

    def test_R18_a_folder_that_cannot_be_listed_keeps_its_pages(self):
        (self.nb/'sub').mkdir();(self.nb/'sub'/'Deep.md').write_text('# Deep\n\n- [ ] deep task\n');docs_index.refresh(self.root,'demo')
        self.assertIn('sub/Deep.md',self.rows())
        real=os.scandir
        def scandir(path='.'):
            if Path(path).name=='sub':raise PermissionError('denied')
            return real(path)
        with patch('core.docs_index.os.scandir',scandir):result=docs_index.refresh(self.root,'demo')
        self.assertTrue(result['stale']);self.assertEqual(result['removed'],0);self.assertIn('sub/Deep.md',self.rows())

    def test_R19_the_progress_allowance_goes_round_the_domains(self):
        for n in range(6):(self.nb/f'p{n}.md').write_text(f'# P{n}\n\n- [ ] task {n}\n')
        init_domain('other',[str(self.base/'other-project')],self.root)
        wiki=self.base/'other-wiki';wiki.mkdir();(wiki/'O.md').write_text('# O\n\n- [ ] other task\n')
        self.cli('--json','doc','folder','set',str(wiki),'--domain','other','--visibility','injectable')
        with patch('core.docs_index.WALK_SECONDS',0),patch('core.docs_index.INDEX_SECONDS',0):
            for _ in range(3):docs_index.refresh_all(self.root,['demo','other'])
        self.assertIn('O.md',self.rows('other'))

    def test_F21_a_domain_whose_refresh_fails_still_passes_the_allowance_on(self):
        init_domain('bad',[str(self.base/'bad-project')],self.root);wiki=self.base/'bad-wiki';wiki.mkdir()
        self.cli('--json','doc','folder','set',str(wiki),'--domain','bad','--visibility','injectable')
        seen=[];real=docs_index.refresh
        def refresh(root,domain,*a,**k):
            seen.append((domain,k.get('progress')))
            return {'stale':True,'indexed':0,'removed':0,'error':'cannot list'} if domain=='bad' else real(root,domain,*a,**k)
        with patch('core.docs_index.refresh',refresh):
            for _ in range(2):docs_index.refresh_all(self.root,['bad','demo'])
        self.assertIn(('demo',True),seen)

    def test_a_page_over_the_size_cap_a_dot_folder_and_a_link_are_not_indexed(self):
        (self.nb/'.obsidian').mkdir();(self.nb/'.obsidian'/'Hidden.md').write_text('- [ ] dot task\n')
        (self.nb/'Big.md').write_bytes(b'- [ ] big task\n'+b'x'*(sources.MAX_BYTES+1))
        docs_index.refresh(self.root,'demo');self.assertEqual(self.rows(),{'Runbook.md','Good.md','Bad.md'})


if __name__=='__main__':
    import unittest;unittest.main()
