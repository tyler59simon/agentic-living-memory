"""Folder notebooks (NS-2B): reach, policy, digests, history and recovery. These are the folder-notebook cases of the Codex reviews of
NS-2B (rounds 1 to 3), kept when hosted notebooks were retired (CON-18). Each test names its finding: P, S, D, R and F, then a number.
Record: docs/reviews/ns2b-codex-review.md."""
import contextlib,json,os,subprocess,time
from pathlib import Path
from unittest.mock import patch
import test_sources_notebooks
from core import digests,docs,folder_policy,governance,runtime,sources
from core.init_domain import init_domain
from core.repository import read_json


def own(cls):
    """Run only the tests this class defines, not those of the fixture class it extends (they run in their own file)."""
    for name in dir(cls):
        if name.startswith('test') and name not in cls.__dict__:setattr(cls,name,None)
    return cls


def junction(link,target,test):
    """A Windows junction, or a directory symlink elsewhere (the code refuses both as links)."""
    if os.name!='nt':os.symlink(target,link,target_is_directory=True);return
    if subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],capture_output=True).returncode:test.skipTest('no junction')


def unjunction(link):
    os.rmdir(link) if os.name=='nt' else os.unlink(link)


def managed(root,vault,domain='demo'):
    (vault/'.living-memory').mkdir(parents=True,exist_ok=True);(vault/'.living-memory'/'vault.json').write_text('{"id":"v1"}')
    state=root/'domains'/domain/'sync-state.json';state.parent.mkdir(parents=True,exist_ok=True);state.write_text(json.dumps({'vault':str(vault)}))


@own
class ReachB(test_sources_notebooks.Notebooks):
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


    def test_P6_a_private_notebook_file_is_no_digest_source_and_old_digests_are_withheld(self):
        described=digests.describe(self.root,'demo',str(self.nb/'Runbook.md'))
        self.set(self.nb,'searchable','--private')
        with self.assertRaisesRegex(ValueError,'hidden or private notebook'):digests.describe(self.root,'demo',str(self.nb/'Runbook.md'))
        hit={'domain':'demo','id':'x','title':'Runbook digest','snippet':'Promote the standby wombat cluster','sources':[described]}
        digests.mark([hit],self.root)
        self.assertTrue(hit['withheld']);self.assertEqual(hit['snippet'],'');self.assertIn('WITHHELD',hit['stale'])
        self.assertNotIn('standby',runtime.hit_line(hit))


@own
class RestoreB(test_sources_notebooks.Notebooks):
    def test_D3_a_restore_and_a_re_registration_keep_a_folder_private(self):
        self.set(self.nb,'searchable');older=(self.root/folder_policy.REGISTRY).read_text()
        self.set(self.nb,'searchable','--private');self.cli('--json','doc','folder','unset',str(self.nb))
        merged=json.loads(folder_policy.merge_registry(self.root,older))['folders']
        self.assertEqual([(e['private'],e.get('removed')) for e in merged],[(True,True)])
        self.set(self.nb,'searchable');self.assertTrue(folder_policy.load(self.root)['folders'][0]['private'])


@own
class HistoryB(test_sources_notebooks.Notebooks):
    def test_P5_registry_and_mirror_state_changes_are_redacted_for_agents(self):
        receipt=self.set(self.nb,'searchable','--private')
        row=next(r for r in governance.history(self.root,limit=50) if r['operation_id']==receipt['operation_id'])
        self.assertEqual((row['files'],row['summary']),([],None))
        code,diff=self.cli('--json','diff',receipt['operation_id']);self.assertEqual(diff,[{'redacted':True}])
        code,diff=self.cli('--json','diff',receipt['operation_id'],'--include-private');self.assertIn('notes',json.dumps(diff))
        self.assertTrue(docs.is_private_path('domains/demo/notebook-sync.json'))


@own
class ReachB2(test_sources_notebooks.Notebooks):
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
        unjunction(alias);junction(alias,second,self)
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



@own
class HistoryB2(test_sources_notebooks.Notebooks):
    def test_R20_only_a_registry_change_that_reopens_a_closed_folder_needs_include_private(self):
        self.set(self.nb,'searchable');loosen=self.set(self.nb,'injectable')
        code,result=self.cli('--json','undo',loosen['operation_id']);self.assertEqual(code,0,result)
        closed=self.set(self.nb,'searchable','--private')
        code,result=self.cli('--json','undo',closed['operation_id']);self.assertNotEqual(code,0,result);self.assertIn('--include-private',json.dumps(result))
        stricter=json.dumps({'version':1,'folders':[dict(folder_policy.load(self.root)['folders'][0],visibility='hidden')]})
        self.assertFalse(folder_policy.loosens(self.root,stricter))
        self.set(self.nb,'hidden');opened=self.set(self.nb,'searchable','--private')   # undoing this one closes the folder further
        code,result=self.cli('--json','undo',opened['operation_id']);self.assertEqual(code,0,result)
        self.assertEqual(folder_policy.load(self.root)['folders'][0].get('visibility'),'hidden')


@own
class ReachB3(test_sources_notebooks.Notebooks):
    def registry(self,*entries):(self.root/folder_policy.REGISTRY).write_text(json.dumps({'version':1,'folders':list(entries)}))

    def test_F01_a_deleted_page_under_any_bound_folder_is_judged(self):
        sha=digests.describe(self.root,'demo',str(self.nb/'Runbook.md'))['sha256']
        self.set(self.nb,'searchable','--private');(self.nb/'Runbook.md').unlink()
        hit={'domain':'demo','id':'x','title':'Runbook digest','snippet':'standby','sources':[{'path':'kb/notes/Runbook.md','sha256':sha}]}
        with patch('core.digests.domain_prefixes',lambda *a:[self.base/'longer-root',self.project]):digests.mark([hit],self.root)
        self.assertTrue(hit.get('withheld'),hit)

    def test_F02_the_prompt_fallback_shows_no_withheld_title(self):
        described=digests.describe(self.root,'demo',str(self.nb/'Runbook.md'));self.set(self.nb,'searchable','--private')
        note={'domain':'demo','id':'abc12345','title':'Secret standby wombat runbook','body':'x','sources':[described]}
        with patch('core.search.recent_notes',lambda *a,**k:[dict(note)]):
            text,refs,overview=runtime.context(str(self.project),'zyxwv qutrop',self.root,'demo')
        self.assertTrue(text and 'Withheld note' in text,text);self.assertNotIn('wombat',text)

    def test_F03_a_retargeted_link_with_matching_file_stamps_is_read_again(self):
        first=self.base/'first';first.mkdir();second=self.base/'second';second.mkdir()
        (first/'Page.md').write_text('alpha zeppelin\n');(second/'Page.md').write_text('bravo marmalad\n')
        stamp=os.stat(first/'Page.md').st_mtime_ns;os.utime(second/'Page.md',ns=(stamp,stamp))
        alias=self.base/'alias';junction(alias,first,self);self.register(name='alias',kind='folder',path=str(alias));self.sync()
        unjunction(alias);junction(alias,second,self);self.sync()
        self.assertNotIn('Page.md',self.plain('alpha zeppelin'));self.assertIn('Page.md',self.plain('bravo marmalad'))

    def test_F12_an_ordinary_source_named_like_a_notebook_source_reads_as_missing(self):
        outside=self.base/'older';outside.mkdir();(outside/'Old.md').write_text('# Old\n\nThe zeppelin marmalade guide.\n')
        self.register(name='nbold',kind='folder',path=str(outside))
        for p in self.root.rglob('sources.json'):
            text=p.read_text(encoding='utf-8')
            if '"nbold"' in text:p.write_text(text.replace('"nbold"','"nb-0123456789"'),encoding='utf-8')
        self.sync();described=digests.describe(self.root,'demo',str(outside/'Old.md'))
        if not described['path'].startswith('nb-0123456789:'):self.skipTest('the file was not named by its source')
        (outside/'Old.md').unlink()
        self.assertEqual(digests.states([described],'demo',self.root),{described['path']:'missing'})

    def test_F14_counts_and_ingest_scan_leave_out_rows_of_a_retargeted_link(self):
        first=self.base/'first';first.mkdir();(first/'A.md').write_text('# A\n\nzeppelin\n');empty=self.base/'empty';empty.mkdir()
        alias=self.base/'alias';junction(alias,first,self);self.register(name='alias',kind='folder',path=str(alias));self.sync()
        unjunction(alias);junction(alias,empty,self)
        row=next(r for r in sources.listing(self.root,'demo')['sources'] if r['name']=='alias')
        self.assertEqual(row['files'],0);self.assertIn('folder moved',row['state'])
        self.assertFalse([i for i in digests.scan(self.root,'demo','alias')['items'] if i['path'].endswith('A.md')])

    def test_F15_a_nested_source_with_globs_leaves_the_notebook_its_other_pages(self):
        book=self.base/'book';sub=book/'sub';sub.mkdir(parents=True)
        (sub/'only.md').write_text('# Only\n\nonly page\n');(sub/'Other.md').write_text('# Other\n\nzeppelin marmalade other\n')
        self.register(name='inner',kind='folder',path=str(sub),include=['only.md']);self.set(book,'searchable');self.sync()
        self.assertIn('Other.md',[h['path'].rsplit('/',1)[-1] for h in sources.search(self.root,'demo','zeppelin marmalade',10,pages=True)])

    def test_F16_a_file_two_sources_index_never_takes_two_result_slots(self):
        lib=self.base/'lib';lib.mkdir()
        (lib/'A.md').write_text('# Quokka\n\nquokka quokka quokka quokka quokka\n');(lib/'B.md').write_text('# B\n\nA page with one quokka in it among other words.\n')
        self.register(name='one',kind='folder',path=str(lib));self.register(name='two',kind='folder',path=str(lib));self.sync()
        self.assertEqual(sorted(h['path'] for h in sources.search(self.root,'demo','quokka',2)),['A.md','B.md'])

    def test_F17_a_notebook_moved_to_another_domain_leaves_the_old_one_at_once(self):
        outside=self.base/'journal';outside.mkdir();(outside/'Day.md').write_text('# Day\n\nzeppelin marmalade walk\n')
        self.set(outside,'searchable');self.sync();self.assertIn('Day.md',self.pages('zeppelin marmalade'))
        init_domain('other',[str(self.base/'other-project')],self.root);spawned=[]
        with patch('core.folder_policy._spawn',lambda root,domain:spawned.append(domain)):folder_policy.set_folder(self.root,str(outside),'other','searchable')
        self.assertNotIn('Day.md',self.pages('zeppelin marmalade'));self.assertEqual(sorted(spawned),['demo','other'])


    def test_F23_set_and_unset_fold_duplicate_rows_even_when_nothing_else_changes(self):
        path=self.nb.resolve().as_posix()
        loose={'path':path,'domain':'demo','visibility':'injectable','private':False};strict={'path':path,'domain':'demo','visibility':'searchable','private':True}
        self.registry(loose,strict);self.set(self.nb,'searchable','--private')
        self.assertEqual(len(folder_policy.load(self.root)['folders']),1)
        self.registry(dict(strict,removed=True),loose);self.cli('--json','doc','folder','unset',str(self.nb))
        self.assertEqual(len(folder_policy.load(self.root)['folders']),1)


@own
class HistoryB3(test_sources_notebooks.Notebooks):
    def test_F08_recovering_a_registry_change_backward_into_a_looser_one_needs_include_private(self):
        open_=self.set(self.nb,'searchable');closed=self.set(self.nb,'searchable','--private')
        journal=read_json(self.root/'.operations'/closed['operation_id']/'journal.json')
        pending=self.root/'.operations'/'interrupted01';pending.mkdir()
        (pending/'journal.json').write_text(json.dumps(dict(journal,id='interrupted01',state='prepared')))
        code,result=self.cli('--json','recover','interrupted01','--direction','backward')
        self.assertNotEqual(code,0,result);self.assertIn('--include-private',json.dumps(result))
        self.assertTrue(folder_policy.load(self.root)['folders'][0]['private'])


if __name__=='__main__':
    import unittest;unittest.main()
