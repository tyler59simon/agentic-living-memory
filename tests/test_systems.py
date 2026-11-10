"""System facet, registry, system-plan, systems, capture attribution and the runbook note type."""
import contextlib,io,json,unittest
from unittest.mock import patch
from test_core import StoreFixture
from core.cli import main
from core.note_format import render
from core.notes import find_note
from core.note_types import NOTE_TYPES,REVIEW_DAYS
from core import backup
from core.runtime import protected_rule,_pointer

BODY=render('Billing takes payments and issues invoices.',['**Boundary:** the billing API and its ledger'],(),(),(),'','')
SLASH=lambda p:str(p).replace(chr(92),'/')

class Fixture(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','concern/architecture','concern/decision','concern/knowledge','concern/workflow']}))
        self.repo=self.base/'project'/'billing';self.repo.mkdir(parents=True)
    def cli(self,*argv,stdin=None,cwd=None):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch('sys.stdin',io.StringIO(stdin or '')):
            try:code=main(['--root',str(self.root),*(['--cwd',str(cwd)] if cwd else []),*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,(json.loads(text) if text.strip()[:1] in '{[' else text)
    def payload(self,**kw):
        value={'slug':'billing','name':'Billing','repos':[str(self.repo)],'owners':['Payments team'],'capabilities':['Take payments','Issue invoices'],'description':'Takes payments.','passport':{'body':BODY}}
        value.update(kw);return value
    def plan(self,action,value,actor='main'):
        return self.cli('--actor',actor,'system-plan',action,stdin=json.dumps(value))
    def apply(self,plan):
        code,result=self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest']);self.assertEqual(code,0,result);return result
    def create(self,**kw):
        code,plan=self.plan('create',self.payload(**kw));self.assertEqual(code,0,plan);return self.apply(plan),plan
    def registry(self):return json.loads((self.root/'systems.json').read_text(encoding='utf-8'))
    def entry(self,slug='billing'):return next(s for s in self.registry()['systems'] if s['slug']==slug)

class Systems(Fixture):
    def test_create_inspect_apply(self):
        code,plan=self.plan('create',self.payload(architecture=str(self.base/'arch'/'billing')+'/'));self.assertEqual(code,0,plan)
        self.assertEqual(plan['kind'],'system_create');s=plan['summary']
        self.assertEqual((s['slug'],s['tag'],s['added_tag'],s['files']),('billing','system/billing',True,3))
        _,view=self.cli('inspect-plan',plan['plan_id'])
        paths=sorted(e['path'] for e in view['effects']);self.assertEqual(len(paths),3)
        self.assertEqual(paths[-2:],['systems.json','tag_taxonomy.json']);self.assertTrue(paths[0].startswith('domains/demo/notes/'),paths)
        self.apply(plan)
        self.assertIn('system/billing',json.loads((self.root/'tag_taxonomy.json').read_text())['tags'])
        entry=self.entry();self.assertEqual((entry['status'],entry['repos'],entry['depends_on']),('active',[SLASH(self.repo)],[]))
        self.assertEqual((entry['owners'],entry['capabilities']),(['Payments team'],['Take payments','Issue invoices']))
        self.assertEqual(entry['architecture'],SLASH(self.base/'arch'/'billing'));self.assertEqual(entry['passport'],s['passport'])
        code,note=self.cli('--domain','demo','read',entry['passport'].split(':')[1]);self.assertEqual(code,0,note)
        n=note.get('note',note);self.assertEqual((n['type'],n['title']),('context','Billing passport'))
        self.assertEqual(sorted(n['tags']),['concern/architecture','subject/project','system/billing'])
        code,listed=self.cli('systems');row=listed['systems'][0]
        self.assertEqual((code,row['tag'],row['tag_registered'],row['dependents']),(0,'system/billing',True,[]))
        self.assertEqual(self.cli('systems','--status','retired')[1]['systems'],[])

    def test_system_without_repos_lands_in_the_global_domain(self):
        _,plan=self.create(slug='stripe',name='Stripe',repos=[],owners=[],capabilities=[])
        self.assertTrue(plan['summary']['passport'].startswith('_global:'),plan['summary'])
        self.assertEqual(self.entry('stripe')['repos'],[])

    def test_dependencies_and_dependents(self):
        self.create(slug='ledger',name='Ledger',repos=[]);self.create(depends_on=['ledger','ledger'])
        self.assertEqual(self.entry()['depends_on'],['ledger'])
        rows={s['slug']:s for s in self.cli('systems')[1]['systems']};self.assertEqual(rows['ledger']['dependents'],['billing'])
        for value,message in (({'slug':'billing','depends_on':['billing']},'itself'),({'slug':'billing','depends_on':['ghost']},'unknown systems'),({'slug':'billing','depends_on':'ledger'},'list of system slugs')):
            code,result=self.plan('update',value);self.assertNotEqual(code,0,value);self.assertIn(message,json.dumps(result))
        code,plan=self.plan('update',{'slug':'billing','depends_on':[]});self.assertEqual(code,0,plan);self.apply(plan)
        self.assertEqual(self.cli('systems')[1]['systems'][1]['dependents'],[])

    def test_undo_then_create_again(self):
        receipt,_=self.create()
        code,undo=self.cli('undo',receipt['operation_id']);self.assertEqual(code,0,undo);self.apply(undo)
        self.assertFalse((self.root/'systems.json').exists())
        self.assertNotIn('system/billing',json.loads((self.root/'tag_taxonomy.json').read_text())['tags'])
        self.assertEqual(list((self.root/'domains/demo/notes').glob('CONTEXT*')),[])
        self.create();self.assertEqual(len(self.registry()['systems']),1)

    def test_undo_refused_while_notes_use_the_tag(self):
        receipt,_=self.create()
        code,made=self.cli('--domain','demo','capture','--title','Use the ledger','--summary','Billing writes to the ledger.','--type','decision','--concern','decision','--system','billing')
        self.assertEqual(code,0,made)
        code,undo=self.cli('undo',receipt['operation_id']);self.assertNotEqual(code,0,undo);self.assertIn('retag those notes first',json.dumps(undo))

    def test_refusals(self):
        self.create()
        for value,message in ((self.payload(),'already exists'),(self.payload(slug='Bad Slug'),'slug'),(self.payload(slug='x',repos=['rel/path']),'absolute'),
                              (self.payload(slug='y',architecture='rel/arch'),'architecture'),(self.payload(slug='z',passport={'body':'## Key Details'+chr(10)+'* only details'}),'Executive Summary'),
                              (self.payload(slug='w',status='active'),'Unknown payload'),(self.payload(slug='v',owners=['x']*11),'at most 10'),({'slug':'u','repos':[]},'requires name')):
            code,result=self.plan('create',value);self.assertNotEqual(code,0,value['slug']);self.assertIn(message,json.dumps(result),value['slug'])
        code,result=self.cli('taxonomy-plan','system/x');self.assertNotEqual(code,0);self.assertIn('system-plan',json.dumps(result))
        code,result=self.cli('retag-plan',stdin=json.dumps({'concern/decision':'system/ghost'}));self.assertNotEqual(code,0);self.assertIn('Unregistered',json.dumps(result))
        code,result=self.cli('retag-plan',stdin=json.dumps({'concern/decision':'system/billing'}));self.assertEqual(code,0,result)

    def test_update_and_retire_touch_only_registry(self):
        self.create();tags=(self.root/'tag_taxonomy.json').read_text()
        code,plan=self.plan('update',{'slug':'billing','name':'Billing v2','status':'deprecated','owners':['Platform'],'architecture':None})
        self.assertEqual((code,plan['kind']),(0,'system_update'))
        _,view=self.cli('inspect-plan',plan['plan_id']);self.assertEqual([e['path'] for e in view['effects']],['systems.json']);self.apply(plan)
        entry=self.entry();self.assertEqual((entry['name'],entry['status'],entry['owners'],entry['architecture']),('Billing v2','deprecated',['Platform'],None))
        code,plan=self.plan('retire',{'slug':'billing'});self.assertEqual(plan['kind'],'system_retire');self.apply(plan)
        self.assertEqual(self.entry()['status'],'retired');self.assertEqual((self.root/'tag_taxonomy.json').read_text(),tags)
        self.assertNotEqual(self.plan('update',{'slug':'nope','name':'x'})[0],0)

    def test_viewer(self):
        self.assertEqual(self.cli('--actor','viewer','systems')[0],0)
        code,result=self.plan('create',self.payload(),actor='viewer');self.assertNotEqual(code,0);self.assertIn('Viewers',json.dumps(result))
        self.assertEqual(self.cli('--actor','subagent','systems')[0],0)
        self.assertNotEqual(self.plan('create',self.payload(),actor='subagent')[0],0)

    def test_warnings(self):
        self.create(repos=[SLASH(self.base/'missing-dir')],architecture=SLASH(self.base/'missing-arch'))
        warnings=' '.join(self.cli('systems')[1]['warnings'])
        self.assertIn('repo folder does not exist',warnings);self.assertIn('architecture folder does not exist',warnings)
        reg=self.registry();reg['systems'][0].update(passport='demo:00000000-0000-0000-0000-000000000000',depends_on=['ghost']);(self.root/'systems.json').write_text(json.dumps(reg))
        warnings=' '.join(self.cli('systems')[1]['warnings']);self.assertIn('passport',warnings);self.assertIn('unknown system ghost',warnings)
        _,doc=self.cli('--domain','demo','doctor');self.assertTrue(any(w.startswith('systems: ') for w in doc['warnings']))
        (self.root/'systems.json').write_text('{broken')
        _,doc=self.cli('--domain','demo','doctor');self.assertTrue(any(w.startswith('systems.json: ') for w in doc['warnings']))

    def test_backup_and_protection(self):
        self.create();self.assertIn('systems.json',backup.canonical_files(self.root))
        self.assertEqual(protected_rule(str(self.root/'systems.json'),str(self.base),self.root),'config')

class Attribution(Fixture):
    COMMON=['--subject','project','--concern','architecture']
    def tagged(self,result,facet='system/'):return [t for t in find_note(result['id'],result.get('domain') or 'demo',self.root)[1]['tags'] if t.startswith(facet)]
    def cap(self,title,*extra,**kw):
        code,r=self.cli('--domain','demo','capture',title,'--summary',f'{title} text.',*self.COMMON,*extra,**kw);self.assertEqual(code,0,r);return r
    def test_file_in_the_repo(self):
        self.create();r=self.cap('One','--file','billing/src/a.py')
        self.assertEqual(self.tagged(r),['system/billing']);self.assertIn('system/billing',r['inferred_tags'])
    def test_explicit_system_wins_and_unknown_is_refused(self):
        self.create();self.create(slug='ledger',name='Ledger',repos=[])
        r=self.cap('Two','--system','ledger','--file','billing/a.py');self.assertEqual(self.tagged(r),['system/ledger'])
        self.assertNotEqual(self.cli('--domain','demo','capture','Three','--summary','Text.','--system','missing',*self.COMMON)[0],0)
    def test_cwd_counts_only_for_a_session(self):
        self.create();r=self.cap('Four',cwd=self.repo);self.assertEqual(self.tagged(r),[])
        p=_pointer(self.root,'s1');p.parent.mkdir(exist_ok=True);p.write_text(json.dumps({'turn':'t1','touched':[],'harness':'claude'}))
        r=self.cap('Five','--session','s1',cwd=self.repo);self.assertEqual(self.tagged(r),['system/billing'])
    def test_deprecated_and_retired_are_not_inferred(self):
        self.create();self.apply(self.plan('update',{'slug':'billing','status':'deprecated'})[1])
        self.assertEqual(self.tagged(self.cap('Six','--file','billing/a.py')),[])
    def test_project_and_system_in_one_repo_both_tag(self):
        self.create()
        code,plan=self.cli('project-plan','create',stdin=json.dumps({'slug':'billing-revamp','name':'Billing revamp','repos':[str(self.repo)],'charter':{'body':BODY}}));self.assertEqual(code,0,plan);self.apply(plan)
        r=self.cap('Seven','--file','billing/a.py')
        self.assertEqual((self.tagged(r),self.tagged(r,'project/')),(['system/billing'],['project/billing-revamp']))
    def test_broken_registry_does_not_break_capture(self):
        self.create();(self.root/'systems.json').write_text('{broken');self.assertEqual(self.tagged(self.cap('Eight','--file','billing/a.py')),[])

class Runbook(Fixture):
    def test_runbook_type(self):
        self.assertIn('runbook',NOTE_TYPES);self.assertEqual(REVIEW_DAYS['runbook'],90)
        self.create()
        code,r=self.cli('--domain','demo','capture','--type','runbook','--title','Restart the billing worker','--summary','Drain the queue, then restart.','--system','billing')
        self.assertEqual(code,0,r);self.assertIn('concern/workflow',r['inferred_tags'])
        path,note=find_note(r['id'],'demo',self.root)
        self.assertTrue(path.name.startswith('RUNBOOK - '));self.assertTrue(note.get('review_by'));self.assertIn('system/billing',note['tags'])

if __name__=='__main__':unittest.main()
