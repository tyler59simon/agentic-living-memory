"""Direct journaled writes (archive, rewrite, remap, promote, taxonomy add, source, project, system, change): each journals and undoes
without a plan; the old *-plan names are gone; undo still returns a plan for the kept plan kinds; the permission lists know the new verbs."""
import contextlib,io,json,os,sys,unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core import runtime,governance,render
from core.cli import main,DIRECT_WRITES
from core.notes import find_note
from core.repository import read_json
from core.init_domain import init_domain

NEW_VERBS=('archive','rewrite','remap','promote','source','project','system','change','type','domain')
CHARTER='A project.\n\n## Key Details\n\n- Direct writes.\n'

class Fixture(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','subject/user','concern/architecture','concern/decision','concern/knowledge','concern/workflow']}))
        (self.base/'project').mkdir(exist_ok=True)
    def cli(self,*argv,stdin=None,actor='main',domain='demo'):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch('sys.stdin',io.StringIO(stdin or '')):
            try:code=main(['--root',str(self.root),'--actor',actor,*(['--domain',domain] if domain else []),*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,(json.loads(text) if text.strip()[:1] in '{[' else text)
    def ok(self,*argv,**kw):
        code,result=self.cli(*argv,**kw);self.assertEqual(code,0,result);return result
    def journal(self,written):return read_json(self.root/'.operations'/written['operation_id']/'journal.json')

class EachVerbJournalsAndUndoes(Fixture):
    def check(self,written,kind):
        self.assertEqual((written['kind'],self.journal(written)['kind']),(kind,kind));self.assertTrue(written['operation_id'].startswith('d-'))
        self.assertNotIn('plan_id',written);self.assertEqual(written['undo'],'undo '+written['operation_id'])
    def undo(self,written):
        undone=self.ok('undo',written['operation_id']);self.assertNotIn('plan_id',undone);self.assertEqual(self.journal(undone)['kind'],'undo:'+written['operation_id']);return undone

    def test_archive(self):
        n=self.note();written=self.ok('archive','demo:'+n['id'])
        self.assertEqual((written['status'],self.journal(written)['kind']),('archive','archive'));self.assertEqual(written['undo'],'undo '+written['operation_id'])
        self.assertEqual(find_note(n['id'],'demo',self.root)[1]['status'],'archived')
        undone=self.ok('undo',written['operation_id']);self.assertNotIn('plan_id',undone);self.assertEqual(find_note(n['id'],'demo',self.root)[1]['status'],'active')
    def test_rewrite(self):
        n=self.note();written=self.ok('rewrite',stdin=json.dumps([{'ref':'demo:'+n['id'],'revision':find_note(n['id'],'demo',self.root)[1]['revision'],'body':'The cache can be deleted and rebuilt.'}]))
        self.check(written,'rewrite');self.assertIn('deleted and rebuilt',find_note(n['id'],'demo',self.root)[1]['body'])
        self.undo(written);self.assertIn('rebuildable',find_note(n['id'],'demo',self.root)[1]['body'])
    def test_remap(self):
        init_domain('other',[str(self.base/'other')],self.root);n=self.note()
        written=self.ok('remap',stdin=json.dumps([{'before':{'domain':'demo','id':n['id']},'after':{'domain':'other','id':n['id']}}]))
        self.check(written,'reference_remap');self.assertEqual(find_note(n['id'],'other',self.root)[1]['domain'],'other')
        self.undo(written);self.assertEqual(find_note(n['id'],'demo',self.root)[1]['domain'],'demo')
    def test_promote(self):
        n=self.note();written=self.ok('promote','demo:'+n['id'])
        self.check(written,'promote');self.assertEqual(find_note(n['id'],'_global',self.root)[1]['domain'],'_global')
        self.undo(written);self.assertEqual(find_note(n['id'],'demo',self.root)[1]['domain'],'demo')
    def test_taxonomy_add(self):
        written=self.ok('taxonomy','add','concern/security');self.check(written,'taxonomy_expansion')
        self.assertIn('concern/security',json.loads((self.root/'tag_taxonomy.json').read_text())['tags'])
        self.undo(written);self.assertNotIn('concern/security',json.loads((self.root/'tag_taxonomy.json').read_text())['tags'])
        self.assertIn('tags',self.ok('taxonomy'))   # without `add` it still lists
    def test_source(self):
        folder=self.base/'docs';folder.mkdir()
        written=self.ok('source','add',stdin=json.dumps({'name':'wiki','kind':'folder','path':str(folder)}));self.check(written,'source_add')
        self.assertTrue((self.root/'sources.json').exists());self.undo(written);self.assertFalse((self.root/'sources.json').exists())
    def test_project_system_and_change(self):
        project=self.ok('project','create',stdin=json.dumps({'slug':'pay','name':'Pay','repos':[str(self.base/'project')],'charter':{'body':CHARTER}}));self.check(project,'project_create')
        system=self.ok('system','create',stdin=json.dumps({'slug':'billing','name':'Billing','repos':[str(self.base/'project')],'passport':{'body':CHARTER}}));self.check(system,'system_create')
        change=self.ok('change','create',stdin=json.dumps({'project':'pay','title':'Add alerts','brief':{'body':CHARTER}}));self.check(change,'change_create')
        self.assertTrue((self.root/'changes.json').exists())
        closed=self.ok('change','close',stdin=json.dumps({'id':1}));self.check(closed,'change_close')
        self.undo(closed);self.undo(change);self.assertFalse((self.root/'changes.json').exists())
        self.undo(system);self.undo(project);self.assertFalse((self.root/'projects.json').exists())

    def test_a_direct_operation_is_not_labelled_a_plan_but_an_applied_plan_is(self):
        n=self.note();written=self.ok('rewrite',stdin=json.dumps([{'ref':'demo:'+n['id'],'revision':find_note(n['id'],'demo',self.root)[1]['revision'],'body':'The cache can be deleted.'}]))
        rows={r['operation_id']:r for r in self.ok('history')}
        self.assertEqual(rows[written['operation_id']]['actor'],'command')
        a,b=self.note('One','Alpha body.',allow_similar=True),self.note('Two','Beta body.',allow_similar=True)
        plan=self.ok('merge-plan',stdin=json.dumps({'target':'demo:'+a['id'],'sources':['demo:'+b['id']],'body':'Merged body.'}))
        applied=self.ok('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest'])
        rows={r['operation_id']:r for r in self.ok('history')};self.assertEqual(rows[applied['operation_id']]['actor'],'plan')
    def test_a_direct_change_is_logged_as_a_command(self):
        self.ok('project','create',stdin=json.dumps({'slug':'pay','name':'Pay','repos':[str(self.base/'project')],'charter':{'body':CHARTER}}))
        change=self.ok('change','create',stdin=json.dumps({'project':'pay','title':'Add alerts','brief':{'body':CHARTER}}))
        rows=[r for r in self.ok('log','--type','change')['rows'] if r.get('op')==change['operation_id']]
        self.assertTrue(rows);self.assertEqual({r['actor'] for r in rows},{'command'})

class RemovedNames(Fixture):
    def test_old_commands_fail_at_argparse(self):
        for old in ('rewrite-plan','taxonomy-plan','remap-plan','promote-plan','source-plan','project-plan','system-plan','change-plan'):
            with self.subTest(old=old):
                code,error=self.cli(old);self.assertEqual(code,2,old);self.assertIn('invalid',json.dumps(error))
        for action in ('update','rename','retag','resolve','reopen','restore','archive','supersede'):
            with self.subTest(action=action):self.assertEqual(self.cli('plan',action,'demo:x')[0],2)
        self.assertIn('plan_id',self.ok('plan','delete','demo:'+self.note()['id']))   # plan delete stays
        code,version=self.cli('version');commands=set(version['commands'])
        self.assertTrue(set(NEW_VERBS)<=commands);self.assertFalse(commands&{'rewrite-plan','taxonomy-plan','remap-plan','promote-plan','source-plan','project-plan','system-plan','change-plan'})

class UndoOfKeptKinds(Fixture):
    def test_undo_of_a_merge_returns_a_plan(self):
        a,b=self.note('One','Alpha body.',allow_similar=True),self.note('Two','Beta body.',allow_similar=True)
        plan=self.ok('merge-plan',stdin=json.dumps({'target':'demo:'+a['id'],'sources':['demo:'+b['id']],'body':'Merged body.'}))
        applied=self.ok('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest'])
        undo=self.ok('undo',applied['operation_id']);self.assertTrue(undo.get('plan_id') and undo.get('digest'));self.assertEqual(undo['kind'],'undo:'+applied['operation_id'])
        self.ok('apply',undo['plan_id'],'--expected-plan-sha256',undo['digest']);self.assertEqual(find_note(b['id'],'demo',self.root)[1]['status'],'active')
    def test_undo_of_a_plan_delete_returns_a_plan_and_of_an_archive_does_not(self):
        a=self.note('One','Alpha body.');plan=self.ok('plan','delete','demo:'+a['id'])
        deleted=self.ok('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest']);undo=self.ok('undo',deleted['operation_id']);self.assertIn('plan_id',undo)
        self.ok('apply',undo['plan_id'],'--expected-plan-sha256',undo['digest'])
        archived=self.ok('archive','demo:'+a['id']);self.assertNotIn('plan_id',self.ok('undo',archived['operation_id']))
    def test_an_undo_of_an_undo_follows_the_kind_of_the_original(self):
        a=self.note('One','Alpha body.');archived=self.ok('archive','demo:'+a['id']);undone=self.ok('undo',archived['operation_id'])
        again=self.ok('undo',undone['operation_id']);self.assertNotIn('plan_id',again);self.assertEqual(find_note(a['id'],'demo',self.root)[1]['status'],'archived')
    def test_undo_of_an_unlisted_kind_such_as_a_harness_setup_returns_a_plan(self):
        # Only the direct kinds are undone at once; anything else (install and setup operations outside the store, maintenance, a kind added later) needs approval.
        a=self.note('One','Alpha body.');written=self.ok('update','demo:'+a['id'],'--body','Changed body.')
        journal=self.root/'.operations'/written['operation_id']/'journal.json';value=read_json(journal);value['kind']='claude_setup';journal.write_text(json.dumps(value))
        self.assertIn('plan_id',self.ok('undo',written['operation_id']))
        self.assertTrue(governance._plan_kind('package_install'));self.assertTrue(governance._plan_kind('maintenance_decision'));self.assertFalse(governance._plan_kind('change_update'))
    def test_the_conflict_check_stays(self):
        a=self.note('One','Alpha body.');archived=self.ok('archive','demo:'+a['id'])
        self.ok('restore','demo:'+a['id'])
        code,error=self.cli('undo',archived['operation_id']);self.assertNotEqual(code,0);self.assertIn('Undo conflicts with later edits',json.dumps(error))

class Guards(Fixture):
    def test_rewrite_refuses_a_stale_revision(self):
        n=self.note();stale=find_note(n['id'],'demo',self.root)[1]['revision']
        self.ok('update','demo:'+n['id'],'--body','The cache is now disposable.')
        code,error=self.cli('rewrite',stdin=json.dumps([{'ref':'demo:'+n['id'],'revision':stale,'body':'Other text.'}]));self.assertNotEqual(code,0);self.assertIn('stale_revision',json.dumps(error))
    def test_archive_keeps_the_link_update_cap_and_writes_nothing(self):
        a=self.note('Cache policy','The cache is rebuildable.')
        for i in range(3):self.note(f'Pointer number {i}',f'Unique words {i} about [the cache](<{Path(a["path"]).name}>).',allow_similar=True)
        before={p:p.read_bytes() for p in (self.root/'domains').rglob('*.md')}
        with patch('core.notes.LINK_UPDATE_MAX',2):code,error=self.cli('archive','demo:'+a['id'])
        self.assertNotEqual(code,0);self.assertIn('too_many_link_updates',json.dumps(error));self.assertEqual({p:p.read_bytes() for p in (self.root/'domains').rglob('*.md')},before)
    def test_a_registry_write_fails_as_stale_when_a_file_it_read_changed(self):
        (self.root/'projects.json').write_text('{"version":1,"projects":[]}')
        with self.assertRaisesRegex(ValueError,'stale plan'):
            governance.commit(self.root,{'changes.json':'{}'},'change_create',[],guards={'read_set':{'projects.json':'0'*64}})
        self.assertFalse((self.root/'changes.json').exists())
    def test_unused_tags_guard_is_checked_at_commit(self):
        self.note()
        with self.assertRaisesRegex(ValueError,'stale plan'):
            governance.commit(self.root,{'tag_taxonomy.json':'{"tags":[]}'},'taxonomy_expansion',['demo'],guards={'unused_tags':['subject/project']})

class Permissions(Fixture):
    def command(self,rest):return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'
    def decision(self,rest,**extra):
        payload={'cwd':str(self.base/'project'),'session_id':'s1','tool_name':'Bash','tool_input':{'command':self.command(rest)},**extra}
        return runtime.process('claude','pre-tool-use',payload,self.root)
    def test_the_lists(self):
        for allowed in ('archive','rewrite','promote'):self.assertIn(allowed,runtime.ALLOWED_SUBCOMMANDS)
        for ask in ('promote-plan','remap','source','project','system','change','rewrite-plan'):self.assertNotIn(ask,runtime.ALLOWED_SUBCOMMANDS)
        self.assertTrue(set(NEW_VERBS)<=runtime.MUTATING_SUBCOMMANDS);self.assertTrue(set(NEW_VERBS)<=runtime.HIVEMIND_GOVERNED);self.assertEqual(set(NEW_VERBS),DIRECT_WRITES)
    def test_what_is_auto_allowed(self):
        for rest in ("archive demo:abc","promote demo:abc",'taxonomy','taxonomy --json'):
            with self.subTest(rest=rest):self.assertIn('allow',self.decision(rest))
        for rest in ('remap','source add','project create','system create','change create','taxonomy add concern/x','taxonomy --json add concern/x','remap-plan'):
            with self.subTest(rest=rest):self.assertNotIn('allow',self.decision(rest))
    def test_subagents_are_denied_every_new_write_including_taxonomy_add(self):
        for rest in ('archive demo:abc','rewrite','remap','promote demo:abc','source add','project create','system create','change create','taxonomy add concern/x'):
            with self.subTest(rest=rest):self.assertIn('deny',self.decision(rest,agent_id='sub1'))
        self.assertNotIn('deny',self.decision('taxonomy',agent_id='sub1'))
        self.assertIn('deny',self.decision('undo d-abc',agent_id='sub1'))   # undo writes directly now
    def test_the_cli_refuses_subagents_and_viewers(self):
        n=self.note()
        for actor in ('subagent','viewer'):
            for argv in (('archive','demo:'+n['id']),('rewrite',),('remap',),('promote','demo:'+n['id']),('source','add'),('project','create'),('system','create'),('change','create'),('taxonomy','add','concern/x')):
                with self.subTest(actor=actor,argv=argv):
                    code,error=self.cli(*argv,actor=actor);self.assertEqual(code,2);self.assertIn('read-only',json.dumps(error))
        self.assertEqual(find_note(n['id'],'demo',self.root)[1]['status'],'active')
        self.assertEqual(self.cli('taxonomy',actor='viewer')[0],0)

class HivemindSession(Fixture):
    def test_every_new_write_is_refused_in_a_hivemind_session_and_reads_are_not(self):
        n=self.note()
        with patch.dict(os.environ,{'HIVEMIND_SESSION':'hm-1'}):
            for argv in (('archive','demo:'+n['id']),('rewrite',),('remap',),('promote','demo:'+n['id']),('source','add'),('project','create'),('system','create'),('change','create'),('taxonomy','add','concern/x'),('undo','op-1')):
                with self.subTest(argv=argv):
                    code,error=self.cli(*argv);self.assertEqual(code,2);self.assertIn('hivemind:',json.dumps(error))
            self.assertEqual(self.cli('taxonomy')[0],0);self.assertEqual(self.cli('projects')[0],0)
        self.assertEqual(find_note(n['id'],'demo',self.root)[1]['status'],'active')
    def test_the_hook_refuses_them_too(self):
        cmd=lambda rest:f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'
        def denied(rest):
            payload={'cwd':str(self.base/'project'),'session_id':'s1','tool_name':'Bash','tool_input':{'command':cmd(rest)}}
            return 'Hivemind' in str(runtime.process('claude','pre-tool-use',payload,self.root).get('deny',''))
        with patch.dict(os.environ,{'HIVEMIND_SESSION':'hm-1'}):
            for rest in ('archive demo:abc','rewrite','remap','promote demo:abc','source add','project create','system create','change create','taxonomy add concern/x'):
                with self.subTest(rest=rest):self.assertTrue(denied(rest))
            for rest in ('taxonomy','projects','search cache'):
                with self.subTest(rest=rest):self.assertFalse(denied(rest))

class Capabilities(Fixture):
    def test_version_reports_direct_writes(self):
        self.assertEqual(self.ok('version')['capabilities']['direct_writes'],1)

class Receipt(Fixture):
    def test_a_direct_receipt_prints_the_operation_and_the_undo_hint(self):
        written=self.ok('taxonomy','add','concern/security');text=render.render(written)
        self.assertIn(written['operation_id'],text);self.assertIn('alm undo '+written['operation_id'],text);self.assertNotIn('Apply it with',text)
        n=self.note();archived=self.ok('archive','demo:'+n['id']);text=render.render(archived);self.assertIn('alm undo '+archived['operation_id'],text)
    def test_a_plan_still_prints_as_a_plan(self):
        n=self.note();plan=self.ok('plan','delete','demo:'+n['id']);self.assertIn('Apply it with',render.render(plan))

if __name__=='__main__':unittest.main()
