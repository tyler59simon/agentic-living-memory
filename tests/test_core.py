import contextlib,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from core import repository as repo
from core.notes import capture,change,find_note
from core.search import search_domain,search_domains,rebuild_index,readonly_query
from core.init_domain import init_domain
from core import installer_lifecycle as lifecycle

# Hooks start a detached background `source-sync` for bound git repos; no test run should leave one behind (test_sources turns it back on).
os.environ.setdefault('LIVING_MEMORY_NO_BACKGROUND','1')

class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='living-memory-test-');self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'store'
        # Tests edit vault files and sync immediately; production waits for Obsidian to go quiet.
        quiet=patch('core.sync.QUIET_SECONDS',0);quiet.start();self.addCleanup(quiet.stop)
        # SessionStart checks the harness's real instruction file; point every harness home at an empty folder.
        # ...and a test run from inside a headless `claude -p` must not make every Stop reminder end with the headless sentence.
        homes=patch.dict('os.environ',{**{h.upper()+'_HOME':str(self.base/('home-'+h)) for h in ('claude','codex','copilot','factory')},
                                       'LIVING_MEMORY_HEADLESS':'','CLAUDE_CODE_SESSION_ATTENDED':'1','CLAUDE_CODE_ENTRYPOINT':'cli'})
        homes.start();self.addCleanup(homes.stop)
        init_domain('demo',[str(self.base/'project')],self.root)
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','concern/architecture','concern/decision']}))
    def note(self,title='Cache',body='The local cache is rebuildable.',**kwargs):
        return capture(title,body,['subject/project','concern/architecture'],'demo',self.root,**kwargs)

class StoreCase(StoreFixture):
    def test_capture_retrieval_and_duplicate(self):
        a=self.note();b=self.note();self.assertEqual(a['id'],b['id']);self.assertEqual(b['status'],'duplicate')
        hits=search_domain('Please explain how our cache works','demo',root=self.root)
        self.assertEqual([h['id'] for h in hits],[a['id']]);self.assertNotIn('body',hits[0])
    def test_staged_content_is_not_canonical(self):
        n=self.note();source=Path(n['path']);stage=self.root/'domains/demo/staged/x.md';stage.parent.mkdir()
        stage.write_text(source.read_text().replace('rebuildable','stagedunique'))
        rebuild_index('demo',self.root);self.assertEqual(search_domain('stagedunique','demo',root=self.root),[])
    def test_score_prefers_stronger_match(self):
        a=self.note('cache cache cache','cache '*30);self.note('Other','cache and unrelated words '*50)
        self.assertEqual(search_domains('cache',['demo'],10,self.root)[0]['id'],a['id'])
    def test_revision_check_and_retag_catalog(self):
        n=self.note();path,note=find_note(n['id'],'demo',self.root)
        change(n['id'],'demo','retag',self.root,tags=['subject/project','concern/decision'])
        self.assertIn('concern/decision',(self.root/'domains/demo/INDEX.md').read_text())
        with self.assertRaisesRegex(ValueError,'stale'):change(n['id'],'demo','update',self.root,expected_revision=note['revision'],body='stale text')
    def test_configuration_corruption_preserved(self):
        p=self.root/'bindings.json';p.write_text('{broken')
        with self.assertRaises(ValueError):init_domain('another',[str(self.base)],self.root)
        self.assertEqual(p.read_text(),'{broken')
    def test_transaction_restart_and_preflight(self):
        real=repo._write;counter=[0]
        def crash(path,data):
            counter[0]+=1
            if counter[0]==2:raise OSError('synthetic process interruption')
            real(path,data)
        with patch.object(repo,'_write',crash):
            with self.assertRaises(OSError):repo.transaction(self.root,{'a.txt':'a','b.txt':'b'},'test',operation_id='restart')
        repo.recover(self.root,'restart');self.assertEqual((self.root/'b.txt').read_text(),'b')
        (self.root/'a.txt').write_text('user edit')
        with self.assertRaises(ValueError):repo.recover(self.root,'restart','backward')
        self.assertEqual((self.root/'b.txt').read_text(),'b')
    def test_readonly_sql(self):
        self.note();rebuild_index('demo',self.root)
        self.assertEqual(len(readonly_query('demo','select id from notes',self.root)['rows']),1)
        with self.assertRaises(Exception):readonly_query('demo','delete from notes',self.root)
        with self.assertRaises(Exception):readonly_query('demo','select id from notes; delete from notes',self.root)
    def test_installer_stale_rollback_has_no_effects(self):
        source=self.base/'source';(source/'core').mkdir(parents=True)
        for name in ('a','b'):(source/'core'/f'{name}.py').write_text(name+'1')
        target=self.base/'target';p=lifecycle.build_plan(target,source);lifecycle.apply_plan(p,p['planSha256'])
        for name in ('a','b'):(source/'core'/f'{name}.py').write_text(name+'2')
        p=lifecycle.build_plan(target,source);receipt=lifecycle.apply_plan(p,p['planSha256'])
        (target/'core/a.py').write_text('user edit')
        before={p.name:p.read_bytes() for p in (target/'core').glob('*.py')}
        with self.assertRaises(lifecycle.StalePlanError):lifecycle.rollback(receipt['receipt'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in (target/'core').glob('*.py')})
    def test_taxonomy_and_credentials(self):
        with self.assertRaises(ValueError):capture('Bad','body',[],'demo',self.root)
        with self.assertRaises(ValueError):self.note(body='-----BEGIN RSA PRIVATE KEY-----')
    def test_governed_plan_and_undo(self):
        from core.governance import apply,undo
        n=self.note();p=change(n['id'],'demo','update',self.root,plan_only=True,body='New body')
        with self.assertRaises(ValueError):apply(self.root,p['plan_id'],'bad')
        receipt=apply(self.root,p['plan_id'],p['digest']);self.assertEqual(find_note(n['id'],'demo',self.root)[1]['body'],'New body')
        p=undo(self.root,receipt['operation_id']);apply(self.root,p['plan_id'],p['digest'])
        self.assertIn('rebuildable',find_note(n['id'],'demo',self.root)[1]['body'])

if __name__=='__main__':unittest.main()
