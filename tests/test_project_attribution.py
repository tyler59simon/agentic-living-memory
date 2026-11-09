"""Project auto-attribution on capture, cross-domain reads, metadata-only updates and SessionStart project context."""
import contextlib,io,json,os,time,unittest
from datetime import date,timedelta
from unittest.mock import patch
from test_core import StoreFixture
from core.cli import main
from core.init_domain import init_domain
from core.notes import capture,change,find_note
from core.note_format import render
from core.runtime import _pointer,_tag_summary
from core.session_start import build_context

SLASH=lambda p:str(p).replace(chr(92),'/')
COMMON=['--subject','project','--concern','architecture']
BASE=['subject/project','concern/architecture']

class Fixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.tags=['subject/project','concern/architecture','concern/decision','project/hive','project/lm','project/a','project/b','project/c']
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':self.tags}))
        self.project=self.base/'project'
        self.write([self.entry('hive'),self.entry('lm')])
    def entry(self,slug,status='active',repo=None,**kw):
        return dict({'slug':slug,'name':slug.upper(),'status':status,'repos':[SLASH(repo or self.project/slug)]},**kw)
    def write(self,entries):(self.root/'projects.json').write_text(json.dumps({'version':1,'projects':entries}))
    def cli(self,*argv,domain='demo',cwd=None,stdin=None):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch('sys.stdin',io.StringIO(stdin or '')):
            try:code=main(['--root',str(self.root),*(['--domain',domain] if domain else []),*(['--cwd',str(cwd)] if cwd else []),*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,(json.loads(text) if text.strip()[:1] in '{[' else text)
    def session(self,touched=()):
        p=_pointer(self.root,'s1');p.parent.mkdir(exist_ok=True);p.write_text(json.dumps({'turn':'t1','touched':list(touched),'harness':'claude'}))
    def cap(self,title,*extra,**kw):
        code,r=self.cli('capture',title,'--summary',f'{title} text.',*COMMON,*extra,**kw);self.assertEqual(code,0,r);return r
    def tagged(self,r,domain='demo'):return [t for t in find_note(r['id'],r.get('domain') or domain,self.root)[1]['tags'] if t.startswith('project/')]

class Attribution(Fixture):
    def test_relative_file_in_bound_domain(self):
        r=self.cap('One','--file','hive/src/a.py');self.assertEqual(self.tagged(r),['project/hive']);self.assertIn('project/hive',r['inferred_tags'])
    def test_child_domain_prefix(self):
        init_domain('child',[str(self.project/'lm')],self.root)
        r=self.cap('Two','--file','src/a.py',domain='child');self.assertEqual(self.tagged(r,'child'),['project/lm'])
    def test_cwd_ignored_without_session(self):
        r=self.cap('Three',cwd=self.project/'hive');self.assertEqual(self.tagged(r),[])
    def test_session_touched_files_with_parent_cwd(self):
        self.session(['hive/x.py']);r=self.cap('Four','--session','s1',cwd=self.project);self.assertEqual(self.tagged(r),['project/hive'])
    def test_session_cwd_without_files(self):
        self.session();r=self.cap('Five','--session','s1',cwd=self.project/'lm');self.assertEqual(self.tagged(r),['project/lm'])
    def test_files_beat_touched_and_cwd(self):
        self.session(['lm/x.py']);r=self.cap('Six','--session','s1','--file','hive/a.py',cwd=self.project/'lm');self.assertEqual(self.tagged(r),['project/hive'])
    def test_explicit_project_wins(self):
        r=self.cap('Seven','--project','lm','--file','hive/a.py');self.assertEqual(self.tagged(r),['project/lm']);self.assertNotIn('inferred_tags',r)
    def test_many_matches_give_candidates(self):
        self.write([self.entry(s) for s in 'abc'])
        r=self.cap('Eight','--file','a/x','--file','b/x','--file','c/x');self.assertEqual(self.tagged(r),[]);self.assertEqual(r['project_candidates'],['a','b','c'])
        r=self.cap('Nine','--file','a/x','--file','b/x');self.assertEqual(self.tagged(r),['project/a','project/b']);self.assertNotIn('project_candidates',r)
    @unittest.skipUnless(os.name=='nt','case folding is Windows-only')
    def test_mixed_case_windows_paths(self):
        r=self.cap('Ten','--file',SLASH(self.project/'HIVE'/'Src'/'A.py').swapcase());self.assertEqual(self.tagged(r),['project/hive'])
    def test_archived_paused_and_unregistered_ignored(self):
        self.write([self.entry('hive','archived'),self.entry('lm','paused'),self.entry('ghost')])
        r=self.cap('Eleven','--file','hive/a','--file','lm/b','--file','ghost/c');self.assertEqual(self.tagged(r),[])
    def test_broken_registry_does_not_break_capture(self):
        (self.root/'projects.json').write_text('{broken');r=self.cap('Twelve','--file','hive/a');self.assertEqual(self.tagged(r),[])
    def test_batch_payload_and_update_not_attributed(self):
        code,r=self.cli('capture',stdin=json.dumps([{'title':'Batch','body':'Batch text here.','tags':BASE,'files':['hive/a.py']}]));self.assertEqual(code,0,r)
        self.assertEqual(self.tagged(r[0]),['project/hive'])
        r=self.cap('Plain');code,u=self.cli('update',r['id'],'--file','hive/a.py');self.assertEqual(code,0,u);self.assertEqual(self.tagged(r),[])

class Reads(Fixture):
    def test_open_all_domains_and_tag(self):
        init_domain('other',[str(self.base/'elsewhere')],self.root)
        def plan(title,domain,*tags):return capture(title,render('Plan.',actions=['Do it']),BASE+list(tags),domain,self.root,type='plan')
        plan('Hive plan','demo','project/hive');plan('Hive other plan','other','project/hive');plan('Lm plan','demo','project/lm')
        code,r=self.cli('open','--all-domains','--tag','project/hive');self.assertEqual(code,0,r)
        self.assertIsNone(r['domain']);self.assertEqual(sorted(i['title'] for i in r['items']),['Hive other plan','Hive plan'])
        self.assertEqual(self.cli('open','--tag','project/hive')[1]['count'],1)
        self.assertEqual(self.cli('open')[1]['count'],2)
    def test_review_all_domains_has_revision_and_domain(self):
        init_domain('other',[str(self.base/'elsewhere')],self.root)
        past=(date.today()-timedelta(days=3)).isoformat()
        a=capture('Due one',render('S.'),BASE+['project/hive'],'demo',self.root,review_by=past)
        capture('Due two',render('S.'),BASE+['project/hive'],'other',self.root,review_by=past)
        capture('Due three',render('S.'),BASE+['project/lm'],'demo',self.root,review_by=past)
        code,r=self.cli('review','--all-domains','--tag','project/hive');self.assertEqual(code,0,r)
        self.assertIsNone(r['domain']);self.assertEqual((r['due'],sorted(n['domain'] for n in r['notes'])),(2,['demo','other']))
        row=next(n for n in r['notes'] if n['ref']=='demo:'+a['id']);self.assertEqual(row['revision'],find_note(a['id'],'demo',self.root)[1]['revision'])
        self.assertEqual(self.cli('review')[1]['due'],2)
    def test_list_with_summary(self):
        p=capture('Roadmap',render('Ship the **thing** soon.',['Detail'],actions=['A','B'],done=['C']),BASE,'demo',self.root,type='plan',files=['hive/a.py'])
        k=self.note('Fact','A plain fact.')
        code,r=self.cli('list','--with-summary');self.assertEqual(code,0,r)
        rows={n['id']:n for n in r['notes']}
        self.assertEqual((rows[p['id']]['summary'],rows[p['id']]['files'],rows[p['id']]['actions']),('Ship the thing soon.',['hive/a.py'],{'open':2,'done':1}))
        self.assertEqual((rows[k['id']]['summary'],rows[k['id']]['files']),('A plain fact.',[]));self.assertNotIn('actions',rows[k['id']])
        self.assertNotIn('summary',self.cli('list')[1]['notes'][0])
        long=capture('Long','x'*500,BASE,'demo',self.root)
        self.assertEqual(len(next(n for n in self.cli('list','--with-summary')[1]['notes'] if n['id']==long['id'])['summary']),300)

class Updated(Fixture):
    def test_metadata_only_updates_keep_updated(self):
        n=self.note(type='plan',review_by=(date.today()-timedelta(days=2)).isoformat());_,before=find_note(n['id'],'demo',self.root)
        self.assertEqual(self.cli('review')[1]['due'],1);first=self.cli('review')[1]['notes']
        for kw in ({'tags':['subject/project','concern/decision']},{'files':['a.py']}):
            change(n['id'],'demo','update',self.root,**kw);self.assertEqual(find_note(n['id'],'demo',self.root)[1]['updated'],before['updated'],kw)
        change(n['id'],'demo','retag',self.root,tags=BASE);self.assertEqual(find_note(n['id'],'demo',self.root)[1]['updated'],before['updated'])
        self.assertEqual(self.cli('list')[1]['notes'][0]['updated'],before['updated'])
        self.assertEqual(self.cli('review')[1]['notes'],[dict(first[0],revision=self.cli('review')[1]['notes'][0]['revision'])])
        change(n['id'],'demo','update',self.root,review_by='2099-01-01');self.assertEqual(find_note(n['id'],'demo',self.root)[1]['updated'],before['updated'])
        self.assertEqual(self.cli('review')[1]['due'],0)
    def test_content_updates_bump_updated(self):
        n=self.note();updated=find_note(n['id'],'demo',self.root)[1]['updated']
        for kw in ({'body':'New body text.'},{'title':'Renamed cache'},{'context':'why'},{'type':'decision'},{'body':'Again text','tags':['subject/project','concern/decision']}):
            time.sleep(0.01);change(n['id'],'demo','update',self.root,**kw)
            now=find_note(n['id'],'demo',self.root)[1]['updated'];self.assertNotEqual(now,updated,kw);updated=now

class SessionContext(Fixture):
    def setUp(self):
        super().setUp()
        (self.project/'hive').mkdir(parents=True,exist_ok=True)
        self.charter=capture('Hive charter',render('Hive is the **desktop** app for memory.'),BASE+['project/hive'],'demo',self.root,type='context')
        self.write([self.entry('hive',charter='demo:'+self.charter['id']),self.entry('lm')])
    def ctx(self,cwd,**kw):return build_context(str(cwd),self.root,**kw)[0]
    def test_project_line(self):
        text=self.ctx(self.project/'hive'/'src');line=next(l for l in text.splitlines() if l.startswith('Project: '))
        self.assertIn('HIVE (project/hive; capture with --project hive) — Hive is the desktop app for memory.',line);self.assertIn('· charter demo:'+self.charter['id'][:8],line)
        self.assertEqual(text.splitlines()[1],line);self.assertNotIn('Projects here',text)
    def test_charter_fallback_and_missing_summary(self):
        self.write([self.entry('hive',charter='demo:00000000-0000-0000-0000-000000000000')])
        self.assertIn('Hive is the desktop app',self.ctx(self.project/'hive'))
        self.write([self.entry('lm',charter='demo:00000000-0000-0000-0000-000000000000')])
        line=next(l for l in self.ctx(self.project/'lm').splitlines() if l.startswith('Project: '));self.assertEqual(line,'Project: LM (project/lm; capture with --project lm)')
    def test_tight_budget_keeps_short_line(self):
        for i in range(6):self.note(f'Filler note number {i}',f'Filler body {i}.')
        trace={};text,_,_=build_context(str(self.project/'hive'),self.root,budget=250,trace=trace)
        line=next(l for l in text.splitlines() if l.startswith('Project: '));self.assertIn('capture with --project hive',line)
        self.assertFalse(trace['degrade']['project_summary']);self.assertNotIn('desktop app',line)
    def test_open_items_of_the_project_first(self):
        capture('Other plan',render('S.',actions=['Elsewhere']),BASE+['project/lm'],'demo',self.root,type='plan');time.sleep(0.01)
        capture('Hive plan',render('S.',actions=['Here']),BASE+['project/hive'],'demo',self.root,type='plan');time.sleep(0.01)
        capture('Newest other',render('S.',actions=['Elsewhere too']),BASE,'demo',self.root,type='plan')
        lines=self.ctx(self.project/'hive').splitlines();first=next(i for i,l in enumerate(lines) if l.startswith('Open work'))
        self.assertIn('Hive plan',lines[first+1]);self.assertIn('3 items in 3 notes',lines[first])
    def test_projects_here_at_parent(self):
        text=self.ctx(self.project);self.assertIn('Projects here: hive, lm — tag captures with --project SLUG',text);self.assertNotIn('Project: ',text)
        self.write([self.entry(f'p{i}'*8) for i in range(30)]);line=next(l for l in self.ctx(self.project).splitlines() if l.startswith('Projects here'));self.assertLessEqual(len(line),160)
    def test_no_registry_or_broken(self):
        (self.root/'projects.json').unlink();text=self.ctx(self.project/'hive');self.assertNotIn('Project: ',text);self.assertNotIn('Projects here',text)
        (self.root/'projects.json').write_text('{broken');self.assertNotIn('Projects here',self.ctx(self.project));self.assertNotIn('Project: ',self.ctx(self.project/'hive'))
    def test_tag_summary_lists_project_facet(self):self.assertIn('project/{',_tag_summary(self.root))

class Version(Fixture):
    def test_api(self):self.assertEqual(self.cli('version')[1]['api'],2)

if __name__=='__main__':unittest.main()
