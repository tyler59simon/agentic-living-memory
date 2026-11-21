"""Folder notebooks in the knowledge base (NS-2B): the notebook-folders.json registry (`doc folder set|unset`), sidecar policy,
the walk that never enters hidden or private notebooks, the serve-time check, folder pages in search and ask, and ingest-scan.
Design: docs/notebooks-design.md sections 8.2 and 12b."""
import contextlib,json,os,sqlite3,sys,time
from pathlib import Path
from unittest.mock import patch
from test_sources import Kb
from test_semantic import write_model
from core import digests,docs,folder_policy,governance,runtime,semantic,sources


class Notebooks(Kb):
    def setUp(self):
        super().setUp()
        self.nb=self.repo/'notes';self.nb.mkdir()
        (self.nb/'Runbook.md').write_text('# Failover runbook\n\nPromote the standby wombat cluster before draining traffic.\n')
        (self.repo/'docs'/'plain.md').write_text('# Plain doc\n\nThe wombat cluster sizing guide.\n')
    def sidecar(self,folder,**value):
        (folder/'.hivemind').mkdir(exist_ok=True);(folder/'.hivemind'/'notebook.json').write_text(json.dumps(value))
    def set(self,folder,visibility,*extra):
        code,result=self.cli('--json','doc','folder','set',str(folder),'--visibility',visibility,*extra);self.assertEqual(code,0,result);return result
    def pages(self,query='wombat cluster'):return sorted(h['path'] for h in sources.search(self.root,'demo',query,10,pages=True))
    def plain(self,query='wombat cluster'):return sorted(h['path'] for h in sources.search(self.root,'demo',query,10))

    def test_a_searchable_notebook_is_served_as_pages_never_as_sources(self):
        self.set(self.nb,'searchable');self.sync()
        self.assertEqual(self.pages(),['notes/Runbook.md']);self.assertEqual(self.plain(),['docs/plain.md'])
        hit=sources.search(self.root,'demo','wombat standby',5,pages=True)[0]
        self.assertEqual((hit['notebook'],hit['notebook_title'],hit['reach']),(self.nb.resolve().as_posix(),'notes','searchable'))
        code,result=self.cli('--json','search','--sources','wombat cluster');self.assertEqual([h['path'] for h in result['results']],['docs/plain.md'])
        code,result=self.cli('--json','ask','wombat cluster');self.assertEqual(code,0,result)
        page=next(p for p in result['pages'] if p['origin']=='folder')
        self.assertEqual((page['path'],page['notebook'],page['label']),('Runbook.md',self.nb.resolve().as_posix(),'notebook page'))
        self.assertEqual(page['revision'],__import__('hashlib').sha256((self.nb/'Runbook.md').read_bytes()).hexdigest())
        self.assertNotIn('Runbook',json.dumps(result['sources']))

    def test_hidden_and_private_are_never_indexed_or_served(self):
        for level,extra in (('hidden',()),('searchable',('--private',))):
            with self.subTest(level=level,extra=extra):
                self.set(self.nb,level,*extra);self.sync()
                self.assertNotIn('notes/Runbook.md',self.indexed());self.assertEqual(self.pages(),[]);self.assertEqual(self.plain(),['docs/plain.md'])

    def test_the_stricter_of_registry_and_sidecar_wins(self):
        self.set(self.nb,'injectable');self.sidecar(self.nb,visibility='hidden');self.sync()
        self.assertEqual(self.pages(),[])
        self.sidecar(self.nb,visibility='injectable',private=True);self.sync();self.assertEqual(self.pages(),[])
        self.sidecar(self.nb,visibility='searchable',title='Ops book');self.sync()
        self.assertEqual(sources.search(self.root,'demo','wombat standby',5,pages=True)[0]['notebook_title'],'Ops book')

    def test_a_sidecar_alone_makes_a_notebook_and_a_damaged_one_hides_it(self):
        self.sidecar(self.nb,visibility='searchable');self.sync();self.assertEqual(self.pages(),['notes/Runbook.md'])
        (self.nb/'.hivemind'/'notebook.json').write_text('{not json');self.assertEqual(self.pages(),[])   # at once, before a sync
        self.sync();self.assertNotIn('notes/Runbook.md',self.indexed())

    def test_a_nested_notebook_inherits_the_private_one_above_it(self):
        inner=self.nb/'inner';inner.mkdir();(inner/'Deep.md').write_text('# Deep\n\nThe wombat cluster in depth.\n')
        self.set(inner,'injectable');self.sidecar(self.nb,private=True);self.sync()
        self.assertEqual(self.pages(),[]);self.assertNotIn('notes/inner/Deep.md',self.indexed())

    def test_made_private_disappears_before_any_sync_and_its_rows_go_at_the_next(self):
        source=self.base/'tiny';hashes=write_model(source)
        models=patch.dict(semantic.MODELS,{'tiny':{'repo':'none/tiny','revision':'0','min_similarity':0.45,'files':hashes}},clear=True);models.start();self.addCleanup(models.stop)
        self.addCleanup(semantic._unload);semantic.install(self.root,'tiny',source)
        self.set(self.nb,'searchable');self.assertTrue(self.sync()['vectors']['embedded']);self.assertIn('notes/Runbook.md',self.indexed())
        self.set(self.nb,'searchable','--private')
        self.assertEqual(self.pages(),[]);self.assertEqual(self.pages('failover promote standby'),[])   # lexical and semantic hits alike
        self.assertTrue(sources.stale(self.root,'demo'))   # the policy revision changed
        self.sync();self.assertNotIn('notes/Runbook.md',self.indexed())
        self.set(self.nb,'searchable','--no-private')
        self.assertTrue(sources.stale(self.root,'demo'));self.sync();self.assertIn('notes/Runbook.md',self.indexed())

    def test_a_sidecar_edit_makes_the_index_stale(self):
        self.sidecar(self.nb,visibility='searchable');self.sync();self.assertFalse(sources.stale(self.root,'demo'))
        time.sleep(0.05);self.sidecar(self.nb,visibility='hidden')
        self.assertTrue(sources.stale(self.root,'demo'))

    def test_a_registered_folder_outside_every_source_is_a_source_of_its_own(self):
        outside=self.base/'journal';outside.mkdir();(outside/'Day.md').write_text('# Day\n\nWalked the wombat cluster dog.\n')
        hidden_repo=self.repo/'.private-stuff';hidden_repo.mkdir();(hidden_repo/'In dot.md').write_text('# Dot\n\nwombat cluster inside a dot folder\n')
        self.set(outside,'searchable');self.set(hidden_repo,'searchable');self.set(self.nb,'searchable')
        names={s['name']:s for s in sources.resolve(self.root,'demo')}
        own=[s for s in names.values() if s.get('notebook')]
        self.assertEqual(sorted(Path(s['path']).name for s in own),['.private-stuff','journal'])   # notes/ is walked by the repo source
        self.sync();self.assertEqual(self.pages(),['Day.md','In dot.md','notes/Runbook.md'])
        code,result=self.cli('--json','doc','folder','unset',str(outside));self.assertEqual(code,0,result)
        self.assertNotIn('journal',json.dumps(sources.resolve(self.root,'demo')));self.sync();self.assertNotIn('Day.md',self.pages())

    def test_set_and_unset_are_idempotent_journaled_and_undoable(self):
        first=self.set(self.nb,'searchable');self.assertEqual(first['status'],'set')
        self.assertEqual(self.set(self.nb,'searchable')['status'],'unchanged')
        changed=self.set(self.nb,'hidden');entry=folder_policy.load(self.root)['folders'][0]
        self.assertEqual((entry['visibility'],entry['private'],entry['domain']),('hidden',False,'demo'))
        code,undone=self.cli('--json','undo',changed['operation_id'],'--include-private');self.assertEqual(code,0,undone)
        self.assertEqual(folder_policy.load(self.root)['folders'][0]['visibility'],'searchable')
        code,gone=self.cli('--json','doc','folder','unset',str(self.nb));self.assertEqual(gone['status'],'removed');self.assertEqual(folder_policy.load(self.root)['folders'],[])

    def test_unset_of_a_hidden_or_private_folder_leaves_a_tombstone_until_forget(self):
        self.set(self.nb,'searchable','--private');self.sync()
        code,result=self.cli('--json','doc','folder','unset',str(self.nb));self.assertEqual(result['status'],'tombstone')
        self.assertTrue(folder_policy.load(self.root)['folders'][0]['removed']);self.sync()
        self.assertNotIn('notes/Runbook.md',self.indexed())   # still excluded: the sidecar may never have been written
        code,result=self.cli('--json','doc','folder','unset',str(self.nb),'--forget');self.assertEqual(result['status'],'removed')
        self.sync();self.assertIn('notes/Runbook.md',self.indexed());self.assertEqual(self.pages(),[]);self.assertEqual(self.plain(),['docs/plain.md','notes/Runbook.md'])

    def test_set_without_visibility_keeps_what_the_registry_holds(self):
        # NS-2C review, P1: Hivemind registers a folder again without naming a visibility, so a hidden tombstone stays hidden.
        code,result=self.cli('--json','doc','folder','set',str(self.nb));self.assertEqual(code,0,result)
        self.assertEqual(result['folder']['visibility'],'searchable')
        self.set(self.nb,'hidden')
        code,result=self.cli('--json','doc','folder','unset',str(self.nb));self.assertEqual(result['status'],'tombstone')
        code,result=self.cli('--json','doc','folder','set',str(self.nb));self.assertEqual(code,0,result)
        entry=folder_policy.load(self.root)['folders'][0];self.assertEqual((entry['visibility'],entry.get('removed')),('hidden',None))
        self.set(self.nb,'injectable','--private')
        code,result=self.cli('--json','doc','folder','set',str(self.nb),'--no-private');self.assertEqual(code,0,result)
        self.assertEqual((result['folder']['visibility'],result['folder']['private']),('injectable',False))

    def test_refusals(self):
        code,result=self.cli('--json','doc','folder','set',str(self.root/'domains'),'--visibility','searchable');self.assertNotEqual(code,0);self.assertIn('inside the memory store',json.dumps(result))
        code,result=self.cli('--json','doc','folder','set','relative/path','--visibility','searchable');self.assertNotEqual(code,0)
        code,result=self.cli('--json','--actor','subagent','doc','folder','set',str(self.nb),'--visibility','searchable');self.assertNotEqual(code,0)
        self.assertTrue(runtime.doc_write(f'"{sys.executable}" "{runtime.SCRIPT}" doc folder set "{self.nb}" --visibility injectable',None))

    def test_a_damaged_registry_serves_nothing_and_doctor_says_why(self):
        self.set(self.nb,'searchable');self.sync()
        (self.root/'notebook-folders.json').write_text('{broken')
        self.assertEqual(self.plain(),[]);self.assertEqual(self.pages(),[])
        with self.assertRaisesRegex(ValueError,'damaged'):digests.scan(self.root,'demo')
        self.assertTrue(any('notebook-folders.json is damaged' in w for w in docs.health(self.root)))

    def test_ingest_scan_skips_notebook_pages(self):
        self.set(self.nb,'injectable');self.sync()
        paths=[i['path'] for i in digests.scan(self.root,'demo',limit=500)['items']]
        self.assertIn('kb/docs/plain.md',paths);self.assertFalse([p for p in paths if 'Runbook' in p])

    def test_version_reports_folder_policy(self):
        code,result=self.cli('--json','version');self.assertEqual(result['capabilities']['docs']['folder_policy'],2)   # 2: set keeps what it is not given


if __name__=='__main__':
    import unittest;unittest.main()
