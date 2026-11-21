"""Regression tests for the second Codex review of NS-2B (fix commit 762e082): one or more per finding, named by its ID.
Record: docs/reviews/ns2b-codex-review.md."""
import json,os,shutil,sqlite3,time
from pathlib import Path
from unittest.mock import patch
import test_docs,test_docs_sync,test_sources_notebooks
from test_docs_review2b import own,junction
from test_docs_sync import PAGE
from core import digests,docs,docs_index,folder_policy,governance,notebook_sync,runtime,sources
from core.repository import read_json
from core.sync import teardown_plan


@own
class Reach(test_sources_notebooks.Notebooks):
    def registry(self,*entries):(self.root/folder_policy.REGISTRY).write_text(json.dumps({'version':1,'folders':list(entries)}))

    def counts(self,name='kb'):return next((r['files'],r['chunks']) for r in sources.listing(self.root,'demo')['sources'] if r['name']==name)

    def test_R01_a_digest_of_a_deleted_page_or_a_dropped_notebook_source_is_withheld(self):
        described=digests.describe(self.root,'demo',str(self.nb/'Runbook.md'))
        self.set(self.nb,'searchable','--private');(self.nb/'Runbook.md').unlink()
        hit={'domain':'demo','id':'x','title':'Runbook digest','snippet':'Promote the standby wombat cluster','sources':[described]}
        digests.mark([hit],self.root);self.assertTrue(hit.get('withheld'),hit)
        outside=self.base/'journal';outside.mkdir();(outside/'Day.md').write_text('# Day\n\nwombat cluster\n')
        self.set(outside,'searchable');self.sync()
        named=digests.describe(self.root,'demo',str(outside/'Day.md'))
        if not sources.NOTEBOOK_SOURCE.fullmatch(named['path'].split(':',1)[0]):self.skipTest('the page was not named by its notebook source')
        self.set(outside,'searchable','--private')   # the folder's own source is no longer resolved
        hit={'domain':'demo','id':'y','title':'Day digest','snippet':'wombat cluster','sources':[named]}
        digests.mark([hit],self.root);self.assertTrue(hit.get('withheld'),hit)

    def test_R02_search_by_file_withholds_and_a_withheld_hit_shows_no_title(self):
        page=str(self.nb/'Runbook.md')
        code,made=self.cli('--json','capture','--title','Standby wombat runbook digest','--summary','How the standby is promoted.','--detail','**Rule:** promote the standby first (Runbook.md:3).',
                           '--subject','project','--concern','architecture','--source',page,'--file',page)
        self.assertEqual(code,0,made)
        self.set(self.nb,'searchable','--private')
        code,result=self.cli('--json','search','--file',page);self.assertEqual(code,0,result)
        hit=next(h for h in result['results'] if h['id']==made['id'])
        self.assertTrue(hit.get('withheld'),hit);self.assertNotIn('wombat',json.dumps(hit).lower())

    def test_R03_a_retargeted_source_link_serves_nothing_until_the_next_sync(self):
        first=self.base/'first';first.mkdir();(first/'A.md').write_text('# A\n\nThe zeppelin marmalade checklist.\n')
        second=self.base/'second';second.mkdir()
        alias=self.base/'alias';junction(alias,first,self);self.register(name='alias',kind='folder',path=str(alias));self.sync()
        self.assertIn('A.md',self.plain('zeppelin marmalade'))
        os.rmdir(alias);junction(alias,second,self)
        self.assertNotIn('A.md',self.plain('zeppelin marmalade'));self.assertTrue(sources.stale(self.root,'demo'))

    def test_R04_set_unset_and_restore_take_the_strictest_of_duplicate_rows(self):
        path=self.nb.resolve().as_posix()
        loose={'path':path,'domain':'demo','visibility':'injectable','private':False};strict={'path':path,'domain':'demo','visibility':'searchable','private':True}
        self.registry(loose,strict);self.set(self.nb,'injectable')
        rows=folder_policy.load(self.root)['folders'];self.assertEqual([r['private'] for r in rows],[True])
        self.registry(loose,strict);self.cli('--json','doc','folder','unset',str(self.nb))
        rows=folder_policy.load(self.root)['folders'];self.assertEqual([(r['private'],r.get('removed')) for r in rows],[(True,True)])
        self.registry()
        merged=json.loads(folder_policy.merge_registry(self.root,json.dumps({'version':1,'folders':[strict,loose]})))['folders']
        self.assertEqual([r['private'] for r in merged],[True])

    def test_R12_a_new_private_sidecar_drops_its_files_from_the_counts_at_once(self):
        self.sync();before=self.counts()
        self.sidecar(self.nb,visibility='searchable',private=True);folder_policy._SIDECARS.clear()
        after=self.counts();self.assertLess(after[0],before[0]);self.assertLess(after[1],before[1])

    def test_R13_include_globs_never_count_as_covering_a_notebook_and_a_page_is_served_once(self):
        code=self.base/'code';book=code/'book';book.mkdir(parents=True);(book/'B.md').write_text('# B\n\nzeppelin marmalade\n')
        self.register(name='code',kind='folder',path=str(code),include=['**/p*.md']);self.set(book,'searchable')
        self.assertEqual([Path(s['path']).name for s in sources.resolve(self.root,'demo') if s.get('notebook')],['book'])
        self.sync();hits=sources.search(self.root,'demo','zeppelin marmalade',10,pages=True)
        self.assertEqual([h['path'] for h in hits],['B.md'])
        self.register(name='code2',kind='folder',path=str(code),include=['**/*.md']);self.sync()
        hits=sources.search(self.root,'demo','zeppelin marmalade',10,pages=True)
        self.assertEqual(len([h for h in hits if h['path'].endswith('B.md')]),1,hits)

    def test_R14_an_older_source_named_like_a_notebook_source_keeps_its_hits(self):
        outside=self.base/'older';outside.mkdir();(outside/'Old.md').write_text('# Old\n\nThe zeppelin marmalade guide.\n')
        registry=read_json(self.root/'sources.json',{'version':1,'sources':[]}) if (self.root/'sources.json').exists() else None
        self.register(name='nbold',kind='folder',path=str(outside))
        for p in self.root.rglob('sources.json'):
            text=p.read_text(encoding='utf-8')
            if '"nbold"' in text:p.write_text(text.replace('"nbold"','"nb-0123456789"'),encoding='utf-8')
        self.sync();self.assertIn('Old.md',self.plain('zeppelin marmalade'))

    def test_R14_a_notebook_source_left_after_unset_serves_nothing_under_a_searchable_sidecar(self):
        parent=self.base/'parent';outside=parent/'journal';outside.mkdir(parents=True);(outside/'Day.md').write_text('# Day\n\nzeppelin marmalade walk\n')
        self.set(outside,'searchable');self.sync();self.assertIn('Day.md',self.pages('zeppelin marmalade'))
        self.sidecar(parent,visibility='searchable');folder_policy._SIDECARS.clear()
        self.cli('--json','doc','folder','unset',str(outside))
        self.assertNotIn('Day.md',self.pages('zeppelin marmalade'))

    def test_R15_the_time_cap_is_checked_before_every_index_search(self):
        self.set(self.nb,'injectable');self.sync();calls=[]
        real=sources.search
        def slow(*a,**k):
            calls.append(k.get('pages',False));time.sleep(0.55);return real(*a,**k)
        with patch('core.sources.search',slow),patch('core.runtime.SOURCE_SECONDS',0.5):
            runtime.prompt_sources('wombat cluster standby',self.root,'demo',{},{})
        self.assertEqual(len(calls),1,calls)

    def test_R16_a_long_prompt_still_asks_the_sources_index_with_its_own_vocabulary(self):
        seen=[];real=runtime.long_queries
        def record(text,vocab):seen.append(vocab);return real(text,vocab)
        docs.create_notebook(self.root,'demo','inj','Inj',visibility='injectable')
        docs.create_page(self.root,'demo','inj','Z.md',b'# Z\n\nzeppelin\n');docs_index.refresh(self.root,'demo');self.sync()
        prompt='Please read the wombat cluster sizing guide. '+'Some unrelated filler words about nothing in particular. '*12
        with patch('core.runtime.long_queries',record):runtime.prompt_sources(prompt,self.root,'demo',{},{})
        self.assertGreaterEqual(len(seen),2,'only the merged vocabulary picked the queries')


@own
class History(test_sources_notebooks.Notebooks):
    def test_R20_only_a_registry_change_that_reopens_a_closed_folder_needs_include_private(self):
        self.set(self.nb,'searchable');loosen=self.set(self.nb,'injectable')
        code,result=self.cli('--json','undo',loosen['operation_id']);self.assertEqual(code,0,result)
        closed=self.set(self.nb,'searchable','--private')
        code,result=self.cli('--json','undo',closed['operation_id']);self.assertNotEqual(code,0,result);self.assertIn('--include-private',json.dumps(result))
        stricter=json.dumps({'version':1,'folders':[dict(folder_policy.load(self.root)['folders'][0],visibility='hidden')]})
        made=governance.plan(self.root,{folder_policy.REGISTRY:stricter},'restore')
        self.assertTrue(governance.apply(self.root,made['plan_id'],made['digest'])['ok'])


@own
class Index(test_docs.Fixture):
    def setUp(self):
        super().setUp();self.notebook();self.page('Good.md',b'# Good\n\nquokka good\n')

    def raw(self,domain,nb,rel,data):
        path=self.root/'domains'/domain/'notebooks'/nb/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);return path

    def meta(self,key,domain='demo'):
        db=sqlite3.connect(docs_index.db_path(self.root,domain))
        try:return sources._meta(db,key)
        finally:db.close()

    def rows(self,domain):
        db=sqlite3.connect(docs_index.db_path(self.root,domain))
        try:return {p for (p,) in db.execute('SELECT path FROM file')}
        finally:db.close()

    def test_R17_a_page_indexed_past_the_deadline_is_queued_for_vectors(self):
        docs_index.refresh(self.root,'demo')
        db=sqlite3.connect(docs_index.db_path(self.root,'demo'))
        try:sources._set_meta(db,'vectors_pending','0');db.commit()
        finally:db.close()
        self.raw('demo','nb1','Late.md',b'# Late\n\nquokka late\n');past=time.monotonic()-1
        result=docs_index.refresh(self.root,'demo',past,past,embed=True)
        self.assertEqual(result['indexed'],1);self.assertEqual(self.meta('vectors_pending'),'1')

    def test_R18_a_folder_that_cannot_be_listed_keeps_its_pages(self):
        self.raw('demo','nb1','sub/Deep.md',b'# Deep\n\nquokka deep\n');docs_index.refresh(self.root,'demo')
        self.assertIn('sub/Deep.md',self.rows('demo'))
        real=os.scandir
        def scandir(path='.'):
            if Path(path).name=='sub':raise PermissionError('denied')
            return real(path)
        with patch('core.docs_index.os.scandir',scandir):result=docs_index.refresh(self.root,'demo')
        self.assertTrue(result['stale']);self.assertEqual(result['removed'],0);self.assertIn('sub/Deep.md',self.rows('demo'))

    def test_R19_the_progress_allowance_goes_round_the_domains(self):
        for n in range(6):self.raw('demo','nb1',f'p{n}.md',f'# P{n}\n\nquokka {n}\n'.encode())
        docs.init_notebook_domain('other',self.root);docs.create_notebook(self.root,'other','nb3','Nb3')
        self.raw('other','nb3','O.md',b'# O\n\nquokka other\n')   # demo has seven pages left to index; other has one
        with patch('core.docs_index.WALK_SECONDS',0),patch('core.docs_index.INDEX_SECONDS',0):
            for _ in range(3):docs_index.refresh_all(self.root,['demo','other'])
        self.assertIn('O.md',self.rows('other'))


@own
class Vault(test_docs_sync.Mirror):
    def tick(self):
        status=self.vault/'Dashboards'/'Sync Status - demo.md';status.write_text(self.status().replace('- [ ] Delete page','- [x] Delete page'),encoding='utf-8')

    def test_R05_an_attachment_saved_in_the_vault_during_the_sync_is_never_overwritten(self):
        image=self.base/'pic.png';image.write_bytes(b'\x89PNG pic');docs.attach(self.root,'demo','nb1',image,'pic.png');self.build()
        target=next((self.vault/'Notebooks'/'demo'/'nb1'/'assets').iterdir());target.unlink()
        real=notebook_sync._bytes;done=[False]
        def bytes_(path):
            found=real(path)
            if Path(path)==target and not done[0]:done[0]=True;target.write_bytes(b'user image')   # saved right after the sync looked
            return found
        with patch('core.notebook_sync._bytes',bytes_):self.build()
        self.assertEqual(target.read_bytes(),b'user image')

    def test_R05_put_never_replaces_a_save_made_after_its_check(self):
        self.build();copy=self.copy('Plan.md');real=notebook_sync._bytes;done=[False]
        def bytes_(path):
            found=real(path)
            if Path(path)==copy and not done[0]:done[0]=True;copy.write_bytes(b'saved meanwhile\n')
            return found
        with patch('core.notebook_sync._bytes',bytes_):ok=notebook_sync._put(self.vault,'Notebooks/demo/nb1/Plan.md',b'new\n',notebook_sync._sha(PAGE))
        self.assertFalse(ok);self.assertEqual(copy.read_bytes(),b'saved meanwhile\n')

    def test_R06_R07_teardown_never_follows_a_junction_and_journals_no_page(self):
        image=self.base/'pic.png';image.write_bytes(b'\x89PNG pic');docs.attach(self.root,'demo','nb1',image,'pic.png');self.build()
        assets=self.vault/'Notebooks'/'demo'/'nb1'/'assets';name=next(assets.iterdir()).name
        outside=self.base/'outside';outside.mkdir();(outside/name).write_bytes(b'\x89PNG pic')
        shutil.rmtree(assets);junction(assets,outside,self)
        made=teardown_plan(self.vault,self.root);receipt=governance.apply(self.vault,made['plan_id'],made['digest'])
        self.assertTrue((outside/name).exists());self.assertFalse(self.copy('Plan.md').exists())
        journal=read_json(self.vault/'.operations'/receipt['operation_id']/'journal.json')
        self.assertFalse([e['path'] for e in journal['effects'] if e['path'].startswith('Notebooks/')])

    def test_R08_a_state_without_a_vault_id_never_proposes_deletions_in_a_new_vault(self):
        self.build();path=self.root/'domains'/'demo'/'notebook-sync.json';state=read_json(path)
        state.pop('vault_id',None);state.pop('vault',None);path.write_text(json.dumps(state))
        self.vault=self.base/'vault2';result=self.build()
        self.assertEqual(result['planned'],0);self.assertEqual(self.copy('Plan.md').read_bytes(),PAGE)

    def test_R09_a_copy_recreated_after_the_read_lapses_the_approval(self):
        self.build();copy=self.copy('Plan.md');copy.unlink();self.build();self.tick()
        real=notebook_sync.read_json
        def read(path,*a):
            if Path(path).parent.name=='.plans':copy.parent.mkdir(parents=True,exist_ok=True);copy.write_bytes(PAGE)   # Obsidian restores it after the page loop read it as gone
            return real(path,*a)
        with patch('core.notebook_sync.read_json',read):result=self.build()
        self.assertEqual(result['approved'],0);self.assertTrue(self.file('Plan.md').exists())

    def test_R10_alm_sync_rewrites_the_status_page(self):
        self.build();self.copy('Plan.md').unlink();self.ok('sync',str(self.vault))
        self.assertIn('lm-page:',self.status())

    def test_R11_a_locked_copy_of_a_closing_notebook_is_kept_once(self):
        self.build();self.copy('Draft.md').write_bytes(b'new draft\n');docs.update_notebook(self.root,'demo','nb1',private=True)
        real=notebook_sync._put
        def locked(base,rel,data,expect):
            if rel.endswith('Draft.md'):raise PermissionError('locked')
            return real(base,rel,data,expect)
        with patch('core.notebook_sync._put',locked):self.build();self.build()
        drafts=lambda:[p for p in docs.proposals(self.root,['demo'],'nb1',include_private=True)['proposals'] if p['path']=='Draft.md']
        self.assertEqual(len(drafts()),1);self.build()
        self.assertFalse(self.copy('Draft.md').exists());self.assertEqual(len(drafts()),1)

    def test_R18_a_folder_that_cannot_be_listed_keeps_its_vault_copies(self):
        docs.create_page(self.root,'demo','nb1','sub/Deep.md',b'# Deep\n\ndeep\n');self.build();self.assertTrue(self.copy('sub/Deep.md').exists())
        real=os.scandir;memory=self.file('sub')
        def scandir(path='.'):
            if Path(path)==memory:raise PermissionError('denied')
            return real(path)
        with patch('core.docs_index.os.scandir',scandir):self.build()
        self.assertTrue(self.copy('sub/Deep.md').exists());self.assertTrue(self.file('sub/Deep.md').exists())

    def test_R21_a_malformed_deletion_plan_is_made_again(self):
        self.build();self.copy('Plan.md').unlink();self.build()
        first=read_json(self.root/'domains'/'demo'/'notebook-sync.json')['pending']['nb1/Plan.md']['plan_id']
        (self.root/'.plans'/(first+'.json')).write_text('{}');self.tick();self.build()
        again=read_json(self.root/'domains'/'demo'/'notebook-sync.json')['pending']['nb1/Plan.md']['plan_id']
        self.assertNotEqual(again,first);self.assertTrue(self.file('Plan.md').exists())


if __name__=='__main__':
    import unittest;unittest.main()
