"""Project facet, registry, project-plan and projects."""
import contextlib,io,json,os,unittest
from unittest.mock import patch
from test_core import StoreFixture
from core.cli import main
from core.note_format import render
from core import projects,backup
from core.runtime import protected_rule

BODY=render('Hivemind is the desktop app for browsing memory.',['**Scope:** the app and its bridge'],(),(),(),'','')
SLASH=lambda p:str(p).replace(chr(92),'/')

class Projects(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','concern/architecture','concern/decision','concern/knowledge']}))
        self.repo=self.base/'project'/'hive';self.repo.mkdir(parents=True)
    def cli(self,*argv,stdin=None):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch('sys.stdin',io.StringIO(stdin or '')):
            try:code=main(['--root',str(self.root),*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,(json.loads(text) if text.strip().startswith('{') else text)
    def payload(self,**kw):
        value={'slug':'demo-app','name':'Demo App','repos':[str(self.repo)],'aliases':['DA'],'description':'A demo.','charter':{'body':BODY}}
        value.update(kw);return value
    def plan(self,action,value,actor='main'):
        return self.cli('--actor',actor,'project-plan',action,stdin=json.dumps(value))
    def apply(self,plan):
        code,result=self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest']);self.assertEqual(code,0,result);return result
    def create(self,**kw):
        code,plan=self.plan('create',self.payload(**kw));self.assertEqual(code,0,plan);return self.apply(plan),plan
    def registry(self):return json.loads((self.root/'projects.json').read_text(encoding='utf-8'))

    def test_create_inspect_apply(self):
        code,plan=self.plan('create',self.payload());self.assertEqual(code,0,plan)
        self.assertEqual(plan['kind'],'project_create');s=plan['summary']
        self.assertEqual((s['slug'],s['tag'],s['added_tag'],s['files']),('demo-app','project/demo-app',True,3))
        _,view=self.cli('inspect-plan',plan['plan_id'])
        paths=[e['path'] for e in view['effects']];self.assertEqual(len(paths),3)
        self.assertEqual(sorted(paths)[-2:],['projects.json','tag_taxonomy.json']);self.assertTrue(any(p.startswith('domains/demo/notes/') for p in paths),paths)
        self.apply(plan)
        self.assertIn('project/demo-app',json.loads((self.root/'tag_taxonomy.json').read_text())['tags'])
        entry=self.registry()['projects'][0];self.assertEqual(entry['slug'],'demo-app');self.assertEqual(entry['status'],'active')
        self.assertEqual(entry['repos'],[SLASH(self.repo)]);self.assertEqual(entry['charter'],s['charter'])
        code,note=self.cli('--domain','demo','read',entry['charter'].split(':')[1]);self.assertEqual(code,0,note)
        n=note.get('note',note);self.assertEqual(n['type'],'context');self.assertIn('project/demo-app',n['tags']);self.assertEqual(n['title'],'Demo App charter')
        code,listed=self.cli('projects');row=listed['projects'][0]
        self.assertEqual((code,row['tag_registered'],listed['warnings']),(0,True,[]));self.assertEqual(row['tag'],'project/demo-app')
        self.assertEqual(self.cli('projects','--status','archived')[1]['projects'],[])

    def test_undo_then_create_again(self):
        receipt,_=self.create()
        code,undo=self.cli('undo',receipt['operation_id']);self.assertEqual(code,0,undo);self.apply(undo)
        self.assertFalse((self.root/'projects.json').exists())
        self.assertNotIn('project/demo-app',json.loads((self.root/'tag_taxonomy.json').read_text())['tags'])
        self.assertEqual(list((self.root/'domains/demo/notes').glob('CONTEXT*')),[])
        self.create()
        self.assertEqual(len(self.registry()['projects']),1)

    def test_refusals(self):
        self.create()
        for value,message in ((self.payload(),'already exists'),(self.payload(slug='Bad Slug'),'slug'),(self.payload(slug='x',repos=['rel/path']),'absolute'),
                              (self.payload(slug='y',repos=[SLASH(self.base/f'r{i}') for i in range(11)]),'at most 10'),
                              (self.payload(slug='z',charter={'body':'## Key Details'+chr(10)+'* only details'}),'Executive Summary'),(self.payload(slug='w',status='active'),'Unknown payload')):
            code,result=self.plan('create',value);self.assertNotEqual(code,0,value['slug']);self.assertIn(message,json.dumps(result),value['slug'])
        code,result=self.cli('taxonomy-plan','project/x');self.assertNotEqual(code,0);self.assertIn('project-plan',json.dumps(result))
        code,result=self.cli('retag-plan',stdin=json.dumps({'concern/decision':'project/ghost'}));self.assertNotEqual(code,0);self.assertIn('Unregistered',json.dumps(result))
        code,result=self.cli('retag-plan',stdin=json.dumps({'concern/decision':'project/demo-app'}));self.assertEqual(code,0,result)

    def test_update_and_archive_touch_only_registry(self):
        self.create();tags=(self.root/'tag_taxonomy.json').read_text()
        code,plan=self.plan('update',{'slug':'demo-app','name':'Renamed','status':'paused'});self.assertEqual((code,plan['kind']),(0,'project_update'))
        _,view=self.cli('inspect-plan',plan['plan_id']);self.assertEqual([e['path'] for e in view['effects']],['projects.json']);self.apply(plan)
        entry=self.registry()['projects'][0];self.assertEqual((entry['name'],entry['status']),('Renamed','paused'))
        code,plan=self.plan('archive',{'slug':'demo-app'});self.assertEqual(plan['kind'],'project_archive')
        _,view=self.cli('inspect-plan',plan['plan_id']);self.assertEqual([e['path'] for e in view['effects']],['projects.json']);self.apply(plan)
        self.assertEqual(self.registry()['projects'][0]['status'],'archived');self.assertEqual((self.root/'tag_taxonomy.json').read_text(),tags)
        self.assertNotEqual(self.plan('update',{'slug':'nope','name':'x'})[0],0)

    def test_capture_project_flag(self):
        self.create()
        code,result=self.cli('--domain','demo','capture','Cache layout','--summary','The cache is rebuildable.','--project','demo-app','--subject','project','--concern','architecture')
        self.assertEqual(code,0,result)
        _,got=self.cli('--domain','demo','read',result['id']);self.assertIn('project/demo-app',json.dumps(got))
        code,result=self.cli('--domain','demo','update','abcdef12','--project','demo-app');self.assertEqual(code,2)
        code,result=self.cli('--domain','demo','capture','Other','--summary','Text.','--project','missing','--subject','project','--concern','architecture');self.assertNotEqual(code,0)

    def test_viewer(self):
        self.assertEqual(self.cli('--actor','viewer','projects')[0],0)
        code,result=self.plan('create',self.payload(),actor='viewer');self.assertNotEqual(code,0);self.assertIn('Viewers',json.dumps(result))
        self.assertEqual(self.cli('--actor','subagent','projects')[0],0)
        self.assertNotEqual(self.plan('create',self.payload(),actor='subagent')[0],0)

    def test_warnings(self):
        self.create(repos=[SLASH(self.base/'missing-dir')])
        _,listed=self.cli('projects');self.assertIn('repo folder does not exist',' '.join(listed['warnings']))
        reg=self.registry();reg['projects'][0]['charter']='demo:00000000-0000-0000-0000-000000000000';(self.root/'projects.json').write_text(json.dumps(reg))
        _,listed=self.cli('projects');self.assertIn('charter',' '.join(listed['warnings']))
        _,doc=self.cli('--domain','demo','doctor');self.assertTrue(any(w.startswith('projects: ') for w in doc['warnings']))

    def test_backup_and_protection(self):
        self.create();self.assertIn('projects.json',backup.canonical_files(self.root))
        self.assertEqual(protected_rule(str(self.root/'projects.json'),str(self.base),self.root),'config')

class MatchPath(unittest.TestCase):
    def test_match(self):
        base='C:/w' if os.name=='nt' else '/w'
        P=lambda slug,*repos,status='active':{'slug':slug,'status':status,'repos':[f'{base}/{r}' for r in repos]}
        ps=[P('outer','a'),P('inner','a/b'),P('old','z',status='archived')]
        self.assertEqual(projects.match_path(f'{base}/a/x.py',ps)['slug'],'outer')
        self.assertEqual(projects.match_path(f'{base}/a/b/x.py',ps)['slug'],'inner')
        self.assertEqual(projects.match_path(f'{base}/a/b',ps)['slug'],'inner')
        self.assertIsNone(projects.match_path(f'{base}/z/x',ps));self.assertIsNone(projects.match_path(f'{base}/ab/x',ps));self.assertIsNone(projects.match_path(f'{base}/other',ps))
        self.assertIsNone(projects.match_path('relative/a',ps))
    @unittest.skipUnless(os.name=='nt','case folding is Windows-only')
    def test_case_insensitive(self):
        ps=[{'slug':'p','status':'active','repos':['C:/Work/App']}]
        self.assertEqual(projects.match_path('c:\\work\\app\\src\\x.py',ps)['slug'],'p')

if __name__=='__main__':unittest.main()
