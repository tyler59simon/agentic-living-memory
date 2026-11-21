"""Regression tests for the Codex review of NS-2B (commit 5723a24): one or more per finding, named by its ID.
Record: docs/reviews/ns2b-codex-review.md."""
import base64,contextlib,io,json,os,shutil,sqlite3,subprocess,time
from pathlib import Path
from unittest.mock import patch
import test_docs,test_docs_sync,test_sources_notebooks
from test_docs_sync import PAGE
from core import digests,docs,docs_index,folder_policy,governance,notebook_sync,render,runtime,sources
from core.repository import digest,prune_plans,read_json
from core.sync import teardown_plan


def own(cls):
    """Run only the tests this class defines, not those of the fixture class it extends (they run in their own file)."""
    for name in dir(cls):
        if name.startswith('test') and name not in cls.__dict__:setattr(cls,name,None)
    return cls


def junction(link,target,test):
    if subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],capture_output=True).returncode:test.skipTest('no junction')


def managed(root,vault,domain='demo'):
    (vault/'.living-memory').mkdir(parents=True,exist_ok=True);(vault/'.living-memory'/'vault.json').write_text('{"id":"v1"}')
    state=root/'domains'/domain/'sync-state.json';state.parent.mkdir(parents=True,exist_ok=True);state.write_text(json.dumps({'vault':str(vault)}))


@own
class Reach(test_sources_notebooks.Notebooks):
    def registry(self,*entries):(self.root/folder_policy.REGISTRY).write_text(json.dumps({'version':1,'folders':list(entries)}))

    def test_P1_a_source_rooted_at_a_junction_is_judged_as_the_folder_it_reaches(self):
        self.set(self.nb,'searchable','--private');alias=self.base/'alias';junction(alias,self.repo,self)
        self.register(name='alias',kind='folder',path=str(alias));self.sync()
        hits=sources.search(self.root,'demo','failover promote standby',10)
        self.assertNotIn('notes/Runbook.md',[h['path'] for h in hits])
        with contextlib.closing(self.db()) as db:
            self.assertFalse(db.execute("SELECT 1 FROM file WHERE source='alias' AND path='notes/Runbook.md'").fetchone())

    def test_P2_the_vault_mirror_stays_hidden_and_cannot_be_registered(self):
        vault=self.base/'vault';managed(self.root,vault);mirror=vault/'Notebooks';mirror.mkdir()
        with self.assertRaisesRegex(ValueError,'Obsidian vault'):folder_policy.set_folder(self.root,str(mirror),'demo','injectable')
        with self.assertRaisesRegex(ValueError,'Obsidian vault'):folder_policy.set_folder(self.root,str(vault),'demo','searchable')
        self.registry({'path':mirror.resolve().as_posix(),'domain':'demo','visibility':'injectable','private':False})
        policy=folder_policy.Policy(self.root)
        self.assertEqual(policy.folder(folder_policy.real(mirror))[0],'hidden');self.assertEqual(policy.registered_sources('demo'),[])

    def test_P3_duplicate_rows_keep_the_strictest_and_bad_rows_are_damage(self):
        path=self.nb.resolve().as_posix()
        self.registry({'path':path,'domain':'demo','visibility':'searchable','private':True},{'path':path,'domain':'demo','visibility':'injectable','private':False})
        self.assertEqual(folder_policy.Policy(self.root).folder(path)[0],'private')
        for bad in ({'path':'relative/folder','visibility':'searchable'},{'path':path,'visibility':'public'},{'path':path,'private':'yes'}):
            with self.subTest(bad=bad):
                self.registry(bad);self.assertTrue(folder_policy.Policy(self.root).broken)

    def test_P3_an_unreadable_sidecar_is_hidden_and_a_damaged_registry_stops_indexing(self):
        self.sidecar(self.nb,visibility='searchable');folder_policy._SIDECARS.clear()
        real_stat=os.stat
        def stat(path,*a,**k):
            if str(path).endswith('notebook.json'):raise PermissionError('denied')
            return real_stat(path,*a,**k)
        with patch('core.folder_policy.os.stat',stat):self.assertEqual(folder_policy.sidecar(self.nb),('hidden',None))
        (self.root/folder_policy.REGISTRY).write_text('{not json')
        with self.assertRaisesRegex(ValueError,'damaged'):self.sync()
        self.assertFalse(sources.stale(self.root,'demo'))

    def test_P7_a_child_of_a_private_folder_gets_no_source_and_listing_hides_old_counts(self):
        outside=self.base/'journal';child=outside/'child';child.mkdir(parents=True);(child/'Day.md').write_text('# Day\n\nwombat cluster\n')
        self.set(outside,'searchable','--private');self.set(child,'searchable')
        self.assertFalse([s for s in sources.resolve(self.root,'demo') if s.get('notebook')])
        self.set(self.nb,'searchable');self.sync()
        count=lambda:next((r['files'],r['chunks']) for r in sources.listing(self.root,'demo')['sources'] if r['name']=='kb')
        before=count();self.set(self.nb,'searchable','--private');after=count()
        self.assertLess(after[0],before[0]);self.assertLess(after[1],before[1])   # judged as the registry says now, not as the last sync saw

    def test_S1_refused_rows_never_push_a_visible_page_out_of_the_candidates(self):
        crowd=self.base/'crowd';crowd.mkdir();lone=self.base/'lone';lone.mkdir()
        for n in range(70):(crowd/f'p{n}.md').write_text(f'# Crowd {n}\n\nquokka quokka quokka quokka quokka\n')
        (lone/'Lone.md').write_text('# Lone\n\nA long page about many things, and one quokka somewhere in the middle of it all.\n')
        self.set(crowd,'searchable');self.set(lone,'searchable');self.sync()
        self.set(crowd,'searchable','--private')   # its rows stay until the next sync
        self.assertIn('Lone.md',[h['path'] for h in sources.search(self.root,'demo','quokka',5,pages=True)])

    def test_S2_coverage_respects_include_globs_and_nested_notebooks_get_one_source(self):
        code=self.base/'code';book=code/'book';book.mkdir(parents=True);(book/'B.md').write_text('# B\n\nwombat cluster\n')
        self.register(name='code',kind='folder',path=str(code),include=['**/*.py']);self.set(book,'searchable')
        self.assertEqual([Path(s['path']).name for s in sources.resolve(self.root,'demo') if s.get('notebook')],['book'])
        outer=self.base/'outer';inner=outer/'inner';inner.mkdir(parents=True)
        self.set(outer,'searchable');self.set(inner,'searchable')
        self.assertEqual(sorted(Path(s['path']).name for s in sources.resolve(self.root,'demo') if s.get('notebook')),['book','outer'])

    def test_S3_a_removed_sidecar_and_a_sidecar_above_the_source_make_it_stale(self):
        self.sidecar(self.nb,visibility='searchable');self.sync();self.assertFalse(sources.stale(self.root,'demo'))
        time.sleep(0.05);(self.nb/'.hivemind'/'notebook.json').unlink()
        self.assertTrue(sources.stale(self.root,'demo'))
        inner=self.nb/'inner';inner.mkdir();folder_policy._SIDECARS.clear()
        before=folder_policy.Policy(self.root).revision(inner)
        self.sidecar(self.nb,visibility='hidden');folder_policy._SIDECARS.clear()
        self.assertNotEqual(folder_policy.Policy(self.root).revision(inner),before)

    def test_S4_an_unset_folder_source_serves_nothing_before_the_next_sync(self):
        outside=self.base/'journal';outside.mkdir();(outside/'Day.md').write_text('# Day\n\nWalked the wombat cluster dog.\n')
        self.set(outside,'searchable');self.sync()
        self.cli('--json','doc','folder','unset',str(outside))
        self.assertNotIn('Day.md',self.plain('walked wombat dog'));self.assertNotIn('Day.md',self.pages('walked wombat dog'))
        with self.assertRaisesRegex(ValueError,'kept for folder notebooks'):
            sources.manage(self.root,'demo','add',{'name':'nb-0123456789','kind':'folder','path':str(outside)}) if hasattr(sources,'manage') else self._add('nb-0123456789',outside)

    def _add(self,name,path):
        code,result=self.cli('source','add',stdin=json.dumps({'name':name,'kind':'folder','path':str(path)}))
        if code:raise ValueError(json.dumps(result))

    def test_S5_a_long_prompt_finds_an_injectable_page_whose_words_no_source_has(self):
        docs.create_notebook(self.root,'demo','inj','Inj',visibility='injectable')
        docs.create_page(self.root,'demo','inj','Zep.md',b'# Zeppelin\n\nThe zeppelin turbine marmalade quokka checklist.\n')
        docs_index.refresh(self.root,'demo');self.sync()
        prompt='Please look at the zeppelin turbine marmalade quokka checklist again. '+'Some unrelated filler words about nothing in particular. '*12
        text,keys=runtime.prompt_sources(prompt,self.root,'demo',{},{})
        self.assertTrue(text and '[notebook Inj]' in text,text);self.assertTrue(any(k.startswith('doc:demo/inj/Zep.md') for k in keys))

    def test_P6_a_private_notebook_file_is_no_digest_source_and_old_digests_are_withheld(self):
        described=digests.describe(self.root,'demo',str(self.nb/'Runbook.md'))
        self.set(self.nb,'searchable','--private')
        with self.assertRaisesRegex(ValueError,'hidden or private notebook'):digests.describe(self.root,'demo',str(self.nb/'Runbook.md'))
        hit={'domain':'demo','id':'x','title':'Runbook digest','snippet':'Promote the standby wombat cluster','sources':[described]}
        digests.mark([hit],self.root)
        self.assertTrue(hit['withheld']);self.assertEqual(hit['snippet'],'');self.assertIn('WITHHELD',hit['stale'])
        self.assertNotIn('standby',runtime.hit_line(hit))


@own
class Restore(test_sources_notebooks.Notebooks):
    def test_D3_a_restore_and_a_re_registration_keep_a_folder_private(self):
        self.set(self.nb,'searchable');older=(self.root/folder_policy.REGISTRY).read_text()
        self.set(self.nb,'searchable','--private');self.cli('--json','doc','folder','unset',str(self.nb))
        merged=json.loads(folder_policy.merge_registry(self.root,older))['folders']
        self.assertEqual([(e['private'],e.get('removed')) for e in merged],[(True,True)])
        self.set(self.nb,'searchable');self.assertTrue(folder_policy.load(self.root)['folders'][0]['private'])


@own
class History(test_sources_notebooks.Notebooks):
    def test_P5_registry_and_mirror_state_changes_are_redacted_for_agents(self):
        receipt=self.set(self.nb,'searchable','--private')
        row=next(r for r in governance.history(self.root,limit=50) if r['operation_id']==receipt['operation_id'])
        self.assertEqual((row['files'],row['summary']),([],None))
        code,diff=self.cli('--json','diff',receipt['operation_id']);self.assertEqual(diff,[{'redacted':True}])
        code,diff=self.cli('--json','diff',receipt['operation_id'],'--include-private');self.assertIn('notes',json.dumps(diff))
        self.assertTrue(docs.is_private_path('domains/demo/notebook-sync.json',docs.private_scope(self.root)))


@own
class Index(test_docs.Fixture):
    def setUp(self):
        super().setUp();self.notebook();self.page('Good.md',b'# Good\n\nquokka good\n');self.page('Bad.md',b'# Bad\n\nquokka bad\n')

    def meta(self,key):
        db=sqlite3.connect(docs_index.db_path(self.root,'demo'))
        try:return sources._meta(db,key)
        finally:db.close()

    def test_I1_a_page_written_through_doc_is_queued_for_vectors(self):
        self.assertEqual(self.meta('vectors_pending'),'1')

    def test_I2_an_unreadable_page_is_stale_retried_last_and_never_blocks_progress(self):
        shutil.rmtree(docs_index.db_path(self.root,'demo').parent/'docs.sqlite3',ignore_errors=True);docs_index.db_path(self.root,'demo').unlink(missing_ok=True)
        real=docs_index._index_page
        def flaky(db,nb,folder,rel,*a):return False if rel=='Bad.md' else real(db,nb,folder,rel,*a)
        with patch('core.docs_index._index_page',flaky):result=docs_index.refresh(self.root,'demo')
        self.assertTrue(result['stale']);self.assertEqual(json.loads(self.meta('failed')),['nb1/Bad.md'])
        late=time.monotonic()-1
        self.assertEqual(docs_index.refresh(self.root,'demo',late,late,progress=False)['indexed'],0)
        tried=[]
        def watch(db,nb,folder,rel,*a):tried.append(rel);return real(db,nb,folder,rel,*a)
        with patch('core.docs_index._index_page',watch):docs_index.refresh(self.root,'demo',late,late)
        self.assertEqual(tried,['Bad.md'])   # the only page left, tried once even past the deadline

    def test_I3_an_index_missing_a_table_is_rebuilt_and_a_failed_rebuild_is_reported(self):
        path=docs_index.db_path(self.root,'demo');path.unlink()
        db=sqlite3.connect(path);db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)');db.execute("INSERT INTO meta VALUES('version','1')");db.commit();db.close()
        self.assertIsNone(docs_index._readonly(self.root,'demo'))
        self.assertEqual(len(self.ok('doc','tasks')['tasks']),0)
        with patch('core.docs_index.os.replace',side_effect=PermissionError('in use')):
            code,text=self.fails('doc','search','quokka','--rebuild')
        self.assertIn('was not rebuilt',text)

    def test_I4_the_notebook_inventory_is_taken_under_the_lock_and_a_failed_listing_removes_nothing(self):
        held=[False];real_lock=docs_index._lock;real_nb=docs_index._notebooks
        @contextlib.contextmanager
        def lock(*a,**k):
            with real_lock(*a,**k):
                held[0]=True
                try:yield
                finally:held[0]=False
        def inventory(*a):
            self.assertTrue(held[0],'inventory taken outside the index lock');return real_nb(*a)
        with patch('core.docs_index._lock',lock),patch('core.docs_index._notebooks',inventory):docs_index.refresh(self.root,'demo')
        with patch('core.docs_index._notebooks',return_value=None):result=docs_index.refresh(self.root,'demo')
        self.assertTrue(result['stale']);self.assertEqual(len(self.ok('doc','search','quokka')['pages']),2)

    def test_P4_a_linked_notebook_json_is_never_trusted(self):
        docs.create_notebook(self.root,'demo','secret','Secret',private=True);docs.create_page(self.root,'demo','secret','S.md',b'quokka secret\n',include_private=True)
        meta=self.file('notebook.json','secret');meta.write_text(json.dumps({'title':'Secret','visibility':'searchable','uid':'u'}))   # what a link to another notebook's file reads
        from core import docs as docs_module
        real=docs_module._is_link
        with patch('core.docs._is_link',lambda p:Path(p).name=='notebook.json' and Path(p).parent.name=='secret' or real(p)):
            self.assertNotIn('secret',docs_index.visible(self.root,'demo'));self.assertNotIn('secret',docs_index._notebooks(self.root,'demo'))

    def test_C2_wrong_types_in_a_query_are_refused(self):
        for payload in ({'where':False},{'sort':{}},{'where':0}):
            with self.subTest(payload=payload),self.assertRaises(ValueError):docs_index.check_query(payload)

    def test_C1_a_subagent_may_not_ask_for_private_notebooks(self):
        for argv in (('search','quokka','--include-private'),('ask','quokka','--include-private')):
            with self.subTest(argv=argv):
                code,text=self.fails(*argv,actor='subagent');self.assertIn('not shown to subagents',text)

    def test_C3_the_terminal_shows_task_state_place_due_date_and_stale_pages(self):
        text=render.render({'ok':True,'tasks':[{'domain':'demo','notebook':'nb1','path':'Plan.md','title':'Plan','line':4,'done':False,'text':'Call the vet','due':'2026-12-01'}],'pages_stale':True})
        self.assertIn('[ ] Call the vet',text);self.assertIn('demo/nb1/Plan.md:4 due 2026-12-01',text);self.assertIn('not fully brought up to date',text)


@own
class Vault(test_docs_sync.Mirror):
    def tick(self):
        status=self.vault/'Dashboards'/'Sync Status - demo.md';status.write_text(self.status().replace('- [ ] Delete page','- [x] Delete page'),encoding='utf-8')

    def test_D1_alm_sync_mirrors_notebooks_too(self):
        self.ok('sync',str(self.vault));self.assertEqual(self.copy('Plan.md').read_bytes(),PAGE)

    def test_V1_a_tick_does_nothing_once_the_copy_is_back(self):
        self.build();self.copy('Plan.md').unlink();self.assertEqual(self.build()['planned'],1)
        self.tick();self.copy('Plan.md').parent.mkdir(parents=True,exist_ok=True);self.copy('Plan.md').write_bytes(PAGE)
        result=self.build();self.assertEqual(result['approved'],0);self.assertTrue(self.file('Plan.md').exists())

    def test_V1_a_pending_deletion_counts_toward_a_vault_reset(self):
        for n in range(6):self.page(f'p{n}.md',f'page {n}\n'.encode())
        self.build();self.copy('Plan.md').unlink();self.build();self.tick()
        shutil.rmtree(self.vault/'Notebooks')
        result=self.build();self.assertEqual(result['approved'],0);self.assertTrue(self.file('Plan.md').exists());self.assertTrue(self.copy('Plan.md').exists())

    def test_V2_a_save_during_sync_keeps_the_baseline_so_memory_is_never_overwritten(self):
        self.build();self.copy('Plan.md').write_bytes(b'vault text\n');self.age(self.copy('Plan.md'))
        docs.write_page(self.root,'demo','nb1','Plan.md',b'memory text\n',digest(PAGE))
        real=notebook_sync._put
        def obsidian_saves_first(base,rel,data,expect):
            if rel.endswith('Plan.md'):(Path(base)/rel).write_bytes(b'saved again\n')
            return real(base,rel,data,expect)
        with patch('core.notebook_sync._put',obsidian_saves_first):self.build()
        self.age(self.copy('Plan.md'));self.build()
        self.assertEqual(self.file('Plan.md').read_bytes(),b'memory text\n')

    def test_V3_every_file_of_a_notebook_that_turns_private_leaves_the_vault(self):
        self.notebook('nb2');self.page('Keep.md',b'keep\n','nb2');self.build()
        self.copy('Plan.md').write_bytes(b'# Plan\r\n\r\nEdited.\r\n');self.age(self.copy('Plan.md'));self.build()   # logged as Saved: nb1/Plan.md
        draft=self.copy('Draft.md');draft.write_bytes(b'new draft\n');(self.copy('notes.txt')).write_bytes(b'loose file\n')
        docs.update_notebook(self.root,'demo','nb1',private=True);self.build()
        self.assertFalse((self.vault/'Notebooks'/'demo'/'nb1').exists())
        self.assertTrue(any(p['path']=='Draft.md' for p in docs.proposals(self.root,['demo'],'nb1',include_private=True)['proposals']))
        self.assertTrue(any(f.name.endswith('from-obsidian.txt') for f in self.file('assets').iterdir()))
        self.assertNotIn('nb1',self.status().split('## Notebook pages',1)[-1])

    def test_V4_the_vault_keeps_no_journal_of_notebook_pages(self):
        self.build();docs.update_notebook(self.root,'demo','nb1',private=True);self.build()
        for journal in (self.vault/'.operations').glob('*/journal.json') if (self.vault/'.operations').is_dir() else ():
            paths=[e.get('path','') for e in read_json(journal).get('effects',[])]
            self.assertFalse([p for p in paths if p.startswith('Notebooks/')],journal)

    def test_V5_a_junction_in_the_vault_mirror_is_never_followed(self):
        self.build();outside=self.base/'outside';outside.mkdir()
        assets=self.vault/'Notebooks'/'demo'/'nb1'/'assets';shutil.rmtree(assets,ignore_errors=True);junction(assets,outside,self)
        image=self.base/'pic.png';image.write_bytes(b'\x89PNG pic');docs.attach(self.root,'demo','nb1',image,'pic.png')
        self.build();self.assertEqual(list(outside.iterdir()),[])
        self.assertIn('link or junction',json.dumps(read_json(self.root/'domains'/'demo'/'notebook-sync.json')['errors']))

    def test_V6_a_new_vault_starts_afresh_and_teardown_removes_the_copies(self):
        self.build();old=self.vault;self.vault=self.base/'vault2'
        result=self.build();self.assertEqual(result['planned'],0);self.assertEqual(self.copy('Plan.md').read_bytes(),PAGE)
        self.assertFalse((old/'Notebooks'/'demo'/'nb1'/'Plan.md').exists())
        made=read_json(self.vault/'.plans'/(teardown_plan(self.vault,self.root)['plan_id']+'.json'))
        self.assertIn('Notebooks/demo/nb1/Plan.md',made['mirror'])   # removed at apply, outside the vault journal (R07)

    def test_V7_a_waiting_deletion_plan_is_never_pruned_and_a_lost_one_is_made_again(self):
        self.build();self.copy('Plan.md').unlink();self.build()
        first=read_json(self.root/'domains'/'demo'/'notebook-sync.json')['pending']['nb1/Plan.md']['plan_id']
        prune_plans(self.root,applied_days=-1,unapplied_days=-1);self.assertTrue((self.root/'.plans'/(first+'.json')).exists())
        (self.root/'.plans'/(first+'.json')).unlink();self.build()
        again=read_json(self.root/'domains'/'demo'/'notebook-sync.json')['pending']['nb1/Plan.md']['plan_id']
        self.assertNotEqual(again,first);self.assertIn(f'lm-page:{again}',self.status())


if __name__=='__main__':
    import unittest;unittest.main()
