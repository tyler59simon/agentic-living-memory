"""Change registry: change-plan create|update|close, changes, the brief note, numbering, adoption, and wiring."""
import contextlib,io,json,unittest
from unittest.mock import patch
from test_core import StoreFixture
from core.cli import main
from core.note_format import render
from core.init_domain import init_domain

CHARTER=render('PayStream turns payment events into alerts.',(),(),(),(),'','')
BRIEF=render('Process repeated payment events once.',(),(),['Define a durable event key','Verify replay and restart'],(),'','')+'\n\n## Acceptance evidence\n- [ ] Replay produces no duplicate alert\n- [x] Suppression survives a restart'
SLASH=lambda p:str(p).replace(chr(92),'/')
GHOST='demo:00000000-0000-0000-0000-000000000000'

class Fixture(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','concern/architecture','concern/decision','concern/knowledge','concern/workflow']}))
        self.repo=self.base/'project'/'billing';self.repo.mkdir(parents=True)
        for command,value in (('project-plan',{'slug':'paystream','name':'PayStream','repos':[str(self.repo)],'charter':{'body':CHARTER}}),('system-plan',{'slug':'billing','name':'Billing','repos':[str(self.repo)],'passport':{'body':CHARTER}})):
            code,plan=self.cli(command,'create',stdin=json.dumps(value));self.assertEqual(code,0,plan);self.apply(plan)
    def cli(self,*argv,stdin=None,cwd=None):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch('sys.stdin',io.StringIO(stdin or '')):
            try:code=main(['--root',str(self.root),*(['--cwd',str(cwd)] if cwd else []),*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,(json.loads(text) if text.strip()[:1] in '{[' else text)
    def payload(self,**kw):
        value={'project':'paystream','title':'Prevent duplicate payment alerts','repo':str(self.repo),'branch':'feature/idempotency','system':'billing','element':'Ingestion','brief':{'body':BRIEF}}
        value.update(kw);return value
    def plan(self,action,value,actor='main'):
        return self.cli('--actor',actor,'change-plan',action,stdin=json.dumps(value))
    def apply(self,plan):
        code,result=self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest']);self.assertEqual(code,0,result);return result
    def create(self,**kw):
        code,plan=self.plan('create',self.payload(**kw));self.assertEqual(code,0,plan);return self.apply(plan),plan
    def registry(self):return json.loads((self.root/'changes.json').read_text(encoding='utf-8'))
    def entry(self,number=1):return next(c for c in self.registry()['changes'] if c['id']==number)
    def note(self,ref):
        code,result=self.cli('--domain','demo','read',ref.split(':')[1]);self.assertEqual(code,0,result);return result.get('note',result)
    def capture(self,kind,title):
        code,cap=self.cli('--domain','demo','capture','--type',kind,'--title',title,'--summary','Summary text.','--subject','project','--concern','workflow',*(('--action','Publish the contract') if kind=='plan' else ()));self.assertEqual(code,0,cap)
        return cap

class Changes(Fixture):
    def test_create_inspect_apply(self):
        taxonomy=(self.root/'tag_taxonomy.json').read_bytes()
        code,plan=self.plan('create',self.payload());self.assertEqual(code,0,plan)
        self.assertEqual(plan['kind'],'change_create');s=plan['summary']
        self.assertEqual((s['id'],s['display'],s['adopted'],s['files']),(1,'CHG-001',False,2))
        _,view=self.cli('inspect-plan',plan['plan_id']);paths=sorted(e['path'] for e in view['effects']);self.assertEqual(len(paths),2)
        self.assertEqual(paths[0],'changes.json');self.assertTrue(paths[1].startswith('domains/demo/notes/PLAN - chg-001-'),paths)
        self.apply(plan);self.assertEqual((self.root/'tag_taxonomy.json').read_bytes(),taxonomy)
        entry=self.entry();self.assertEqual((entry['stage'],entry['status'],entry['repo'],entry['branch'],entry['system'],entry['element']),('explore','active',SLASH(self.repo),'feature/idempotency','billing','Ingestion'))
        self.assertEqual(entry['brief'],s['brief']);n=self.note(entry['brief'])
        self.assertEqual((n['type'],n['title']),('plan','CHG-001 Prevent duplicate payment alerts'))
        self.assertEqual(sorted(n['tags']),['concern/workflow','project/paystream','subject/project','system/billing'])
        code,listed=self.cli('changes');row=listed['changes'][0]
        self.assertEqual((code,len(listed['changes']),row['display'],row['brief_status'],row['actions']),(0,1,'CHG-001','active',{'open':2,'done':0}))
        self.assertEqual(self.cli('changes','--status','done')[1]['changes'],[]);self.assertEqual(self.cli('changes','--project','other')[1]['changes'],[])
        self.assertEqual(len(self.cli('changes','--project','paystream')[1]['changes']),1)

    def test_numbers_follow_and_a_second_plan_goes_stale(self):
        _,first=self.plan('create',self.payload());_,second=self.plan('create',self.payload(title='Other'))
        self.assertEqual((first['summary']['id'],second['summary']['id']),(1,1));self.apply(first)
        code,result=self.cli('apply',second['plan_id'],'--expected-plan-sha256',second['digest']);self.assertEqual(code,6);self.assertIn('Stale revision',json.dumps(result))
        code,again=self.plan('create',self.payload(title='Other'));self.assertEqual(again['summary']['id'],2);self.apply(again)
        self.assertEqual([c['id'] for c in self.registry()['changes']],[1,2])

    def test_undo_then_create_again_reuses_the_number(self):
        receipt,_=self.create()
        code,undo=self.cli('undo',receipt['operation_id']);self.assertEqual(code,0,undo);self.apply(undo)
        self.assertFalse((self.root/'changes.json').exists());self.assertEqual(list((self.root/'domains/demo/notes').glob('PLAN*')),[])
        self.create();self.assertEqual(self.entry()['id'],1)

    def test_adopt_an_existing_plan_note(self):
        cap=self.capture('plan','Extract delivery');short=str(cap.get('ref') or cap.get('id') or cap.get('note_id')).split(':')[-1]
        full=self.note('demo:'+short)['id']
        code,plan=self.plan('create',{'project':'paystream','title':'Extract delivery','stage':'design','brief':'demo:'+short});self.assertEqual(code,0,plan)
        self.assertEqual((plan['summary']['adopted'],plan['summary']['files']),(True,2));self.apply(plan)
        n=self.note('demo:'+short);self.assertEqual(n['title'],'Extract delivery');self.assertIn('project/paystream',n['tags'])
        entry=self.entry();self.assertEqual((entry['brief'],entry['stage']),('demo:'+full,'design'))
        code,result=self.plan('create',{'project':'paystream','title':'Again','brief':'demo:'+short[:8]});self.assertNotEqual(code,0);self.assertIn('already the brief of CHG-001',json.dumps(result))
        dec=self.capture('decision','A choice');dshort=str(dec.get('ref') or dec.get('id') or dec.get('note_id')).split(':')[-1]
        code,result=self.plan('create',{'project':'paystream','title':'Bad','brief':'demo:'+dshort});self.assertNotEqual(code,0);self.assertIn('active plan note',json.dumps(result))
        code,result=self.plan('create',{'project':'paystream','title':'Bad','brief':GHOST});self.assertNotEqual(code,0);self.assertIn('does not resolve',json.dumps(result))

    def test_update_touches_only_the_registry(self):
        self.create();code,plan=self.plan('update',{'id':'CHG-001','stage':'design','branch':'feature/x','title':'New title'});self.assertEqual(code,0,plan)
        self.assertEqual(plan['kind'],'change_update');_,view=self.cli('inspect-plan',plan['plan_id']);self.assertEqual([e['path'] for e in view['effects']],['changes.json'])
        s=plan['summary'];self.assertEqual((s['fields'],s['brief_action'],s['files']),(['branch','stage','title'],None,1));self.apply(plan)
        e=self.entry();self.assertEqual((e['stage'],e['branch'],e['title']),('design','feature/x','New title'));self.assertGreater(e['updated'],e['created'])
        self.assertEqual(self.note(e['brief'])['title'],'CHG-001 Prevent duplicate payment alerts')
        code,plan=self.plan('update',{'id':1,'element':None});self.assertEqual(code,0,plan);self.apply(plan);self.assertIsNone(self.entry()['element'])
        code,result=self.plan('update',{'id':9,'stage':'design'});self.assertNotEqual(code,0);self.assertIn('Unknown change CHG-009',json.dumps(result))
        code,result=self.plan('update',{'id':1});self.assertNotEqual(code,0);self.assertIn('at least one field',json.dumps(result))

    def test_close_resolves_the_brief_and_reopen_brings_it_back(self):
        self.create();ref=self.entry()['brief'];code,plan=self.plan('close',{'id':1});self.assertEqual(code,0,plan)
        self.assertEqual((plan['kind'],plan['summary']['brief_action']),('change_close','resolve'))
        _,view=self.cli('inspect-plan',plan['plan_id']);effects=view['effects'];paths=[e['path'] for e in effects]
        self.assertEqual(len(paths),3,effects);self.assertIn('changes.json',paths)
        self.assertTrue(any(p.startswith('domains/demo/notes/PLAN') for p in paths) and any(p.startswith('domains/demo/archive/') for p in paths),paths)
        self.assertIn('delete',[e.get('action') for e in effects if e['path'].startswith('domains/demo/notes/')]);self.apply(plan)
        self.assertEqual(self.entry()['status'],'done');self.assertEqual(self.note(ref)['status'],'resolved')
        code,opened=self.cli('open','--all-domains');self.assertNotIn('Define a durable event key',json.dumps(opened))
        row=self.cli('changes')[1]['changes'][0];self.assertEqual((row['brief_status'],row['brief']),('resolved',ref))
        code,result=self.plan('close',{'id':1});self.assertNotEqual(code,0);self.assertIn('already done',json.dumps(result))
        code,plan=self.plan('update',{'id':1,'status':'active'});self.assertEqual(code,0,plan);self.assertEqual(plan['summary']['brief_action'],'reopen');self.apply(plan)
        self.assertEqual((self.note(ref)['status'],self.entry()['status']),('active','active'))
        code,plan=self.plan('close',{'id':1,'status':'cancelled'});self.assertEqual(code,0,plan);self.apply(plan);self.assertEqual(self.entry()['status'],'cancelled')
        self.create(title='Second');code,result=self.plan('close',{'id':2,'status':'active'});self.assertNotEqual(code,0);self.assertIn('close status must be done or cancelled',json.dumps(result))

    def test_close_leaves_a_brief_that_is_already_resolved(self):
        self.create();ref=self.entry()['brief'];code,res=self.cli('--domain','demo','resolve',ref.split(':')[1]);self.assertEqual(code,0,res)
        if isinstance(res,dict) and res.get('plan_id'):self.apply(res)
        code,plan=self.plan('close',{'id':1});self.assertEqual(code,0,plan);_,view=self.cli('inspect-plan',plan['plan_id'])
        self.assertEqual(([e['path'] for e in view['effects']],plan['summary']['brief_action']),(['changes.json'],None));self.apply(plan)

    def test_refusals(self):
        cases=[({'project':'ghost'},'unknown project'),({'system':'ghost'},'unknown system'),({'title':None},'requires title'),({'brief':None},'requires brief'),
               ({'brief':{'body':'## Key Details\n* only details'}},'Executive Summary'),({'brief':{'body':BRIEF,'extra':1}},'brief accepts'),({'repo':'rel/path'},'absolute'),
               ({'stage':'ship'},'stage must be one of'),({'status':'active'},'Unknown payload'),({'title':'Fix [x]'},'cannot contain'),({'title':'x'*121},'1..120'),
               ({'system':None},'element needs a system'),({'title':'Rotate '+'sk-'+'a'*24},'credential')]
        for kw,message in cases:
            value=self.payload(**kw)
            for key in ('title','brief'):
                if kw.get(key,0) is None:del value[key]
            code,result=self.plan('create',value);self.assertNotEqual(code,0,kw);self.assertIn(message,json.dumps(result),kw)
        self.assertFalse((self.root/'changes.json').exists())
        for value,message in (({'id':'CHG-x','stage':'design'},'change number'),({'id':1,'project':'other'},'Unknown payload')):
            code,result=self.plan('update',value);self.assertNotEqual(code,0,value);self.assertIn(message,json.dumps(result))
        self.assertFalse((self.root/'changes.json').exists())

    def test_viewer(self):
        for actor in ('viewer','subagent'):self.assertEqual(self.cli('--actor',actor,'changes')[0],0)
        code,result=self.plan('create',self.payload(),'viewer');self.assertNotEqual(code,0);self.assertIn('Viewers',json.dumps(result))
        self.assertNotEqual(self.plan('create',self.payload(),'subagent')[0],0)

    def test_warnings(self):
        self.create();e=dict(self.entry(),project='ghost',system='gone',brief=GHOST)
        (self.root/'changes.json').write_text(json.dumps({'version':1,'changes':[e]}))
        code,listed=self.cli('changes');text=json.dumps(listed['warnings']);self.assertEqual(code,0)
        for part in ('unknown project ghost','unknown system gone','does not resolve'):self.assertIn(part,text)
        row=listed['changes'][0];self.assertEqual((row['brief_status'],row['actions']),(None,{'open':0,'done':0}))
        self.assertTrue(any(w.startswith('changes: ') for w in self.cli('doctor')[1]['warnings']))
        (self.root/'changes.json').write_text('{broken');self.assertEqual(self.cli('changes')[0],2)
        self.assertTrue(any(w.startswith('changes.json: ') for w in self.cli('doctor')[1]['warnings']))

    def test_an_archived_brief_still_resolves(self):
        self.create();ref=self.entry()['brief'];code,plan=self.cli('--domain','demo','plan','archive',ref);self.assertEqual(code,0,plan);self.apply(plan)
        code,listed=self.cli('changes');row=listed['changes'][0]
        self.assertEqual((row['brief'],row['brief_status'],row['actions']),(ref,'archived',{'open':2,'done':0}));self.assertFalse(any('does not resolve' in w for w in listed['warnings']))
        code,plan=self.plan('close',{'id':1});self.assertEqual(code,0,plan);_,view=self.cli('inspect-plan',plan['plan_id'])
        self.assertEqual(([e['path'] for e in view['effects']],plan['summary']['brief_action']),(['changes.json'],None));self.apply(plan);self.assertEqual(self.note(ref)['status'],'archived')

    def test_a_brief_that_changed_domain_is_still_found(self):
        init_domain('other',[str(self.base/'other')],self.root);self.create();ref=self.entry()['brief']
        e=dict(self.entry(),brief='other:'+ref.split(':')[1]);(self.root/'changes.json').write_text(json.dumps({'version':1,'changes':[e]}))
        code,listed=self.cli('changes');row=listed['changes'][0];self.assertEqual((row['brief'],row['brief_status'],listed['warnings']),(ref,'active',[]))

    def test_brief_keeps_custom_sections(self):
        self.create();ref=self.entry()['brief']
        code,res=self.cli('--domain','demo','check',ref,'--index','2');self.assertEqual(code,0,res)
        body=self.note(ref)['body']
        for part in ('## Acceptance evidence','- [ ] Replay produces no duplicate alert','- [x] Suppression survives a restart'):self.assertIn(part,body)
        code,res=self.cli('--domain','demo','check',ref,'--index','3');self.assertNotEqual(code,0);self.assertIn('No Action Item 3; the note has 2',json.dumps(res))
        code,res=self.cli('--domain','demo','update',ref,'--add-action','Ship it');self.assertEqual(code,0,res);self.assertIn('## Acceptance evidence',self.note(ref)['body'])
        n=self.note(ref);new=n['body'].replace('- [ ] Replay produces no duplicate alert','- [x] Replay produces no duplicate alert')
        code,res=self.cli('--domain','demo','update',ref,stdin=json.dumps({'body':new,'expected_revision':n['revision']}));self.assertEqual(code,0,res)
        self.assertIn('- [x] Replay produces no duplicate alert',self.note(ref)['body'])
        row=self.cli('changes')[1]['changes'][0];self.assertEqual(row['actions']['open']+row['actions']['done'],3)

    def test_version_lists_the_commands(self):
        code,v=self.cli('version');self.assertEqual((code,v['api']),(0,2));self.assertIn('changes',v['commands']);self.assertIn('change-plan',v['commands'])

if __name__=='__main__':unittest.main()
