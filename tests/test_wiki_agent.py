"""The agent-written wiki edition: packets, validation, the build loop (with a fake `claude`), the saved edition's model, the page, the
command's guards and the hook rules. No test calls a real model: the provider is an argv prefix that runs FAKE below."""
import contextlib,io,json,os,re,signal,subprocess,sys,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
import test_wiki as tw
from core import maintenance_agent as agent,runtime,wiki,wiki_agent as wa
from revision_fill import main
from core.common import file_lock
from core.notes import change

# A stand-in for `claude -p`: it answers each kind of call from the packet it reads on stdin, so a test only chooses the mode.
# FAKE_PAGES: pages in the outline (default 2); FAKE_COST: dollars per call; FAKE_FAIL: a task ("page", "outline", "place") or
# "page:Title" that answers with a provider error (its subtype is FAKE_SUBTYPE); FAKE_BAD: a task whose answer breaks the contract; FAKE_PLACE: new | omit; FAKE_SLEEP: seconds a call waits before it answers.
FAKE='''import json,os,sys,time
args=sys.argv[1:]
if '--help' in args:
    print('Usage: claude [options]\\n'+'\\n'.join('  '+f+' <value>' for f in %r));sys.exit(0)
packet=json.loads(sys.stdin.read());task=packet['task'];env=os.environ.get
title=packet['page']['title'] if task=='page' else None
os.makedirs(env('FAKE_LOG'),exist_ok=True)   # one file per call: concurrent appends to one file can collide on Windows
open(os.path.join(env('FAKE_LOG'),'%%d-%%d.json'%%(time.time_ns(),os.getpid())),'w').write(json.dumps({'task':task,'title':title,'budget':args[args.index('--max-budget-usd')+1],'model':args[args.index('--model')+1],
    'policy':args[args.index('--system-prompt')+1],'sources':len(packet.get('sources',[])),'bodies':[len(s['body']) for s in packet.get('sources',[])]})+'\\n')
time.sleep(float(env('FAKE_SLEEP','0')))
def done(report):
    print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'','structured_output':report,'total_cost_usd':float(env('FAKE_COST','0.01'))}))
fail=env('FAKE_FAIL','')
if fail in (task,task+':'+str(title)):
    print(json.dumps({'type':'result','subtype':env('FAKE_SUBTYPE','error_during_execution'),'is_error':True,'total_cost_usd':float(env('FAKE_COST','0.01'))}));sys.exit(1)
bad=env('FAKE_BAD','')==task
if task=='outline':
    keys=[n['key'] for n in packet['notes']];count=min(int(env('FAKE_PAGES','2')),len(keys));groups=[keys[i::count] for i in range(count)]
    pages=[{'key':'P%%d'%%(i+1),'title':'Page %%d'%%(i+1),'purpose':'Explain part %%d.'%%(i+1),'sources':g} for i,g in enumerate(groups)]
    if bad:pages[0]['sources']=['N999']
    omit=[{'note':keys[-1],'reason':'A transient status.'}] if env('FAKE_OMIT') else []
    if omit:pages=[dict(p,sources=[k for k in p['sources'] if k!=keys[-1]]) for p in pages if [k for k in p['sources'] if k!=keys[-1]]]
    done({'summary':'A wiki made for a test.','sections':[{'key':'S1','title':'Core','blurb':'The core pages.','pages':pages[:2]},{'key':'S2','title':'More','blurb':'The rest.','pages':pages[2:]}] if len(pages)>2 else
          [{'key':'S1','title':'Core','blurb':'The core pages.','pages':pages}],'omit':omit})
elif task=='place':
    mode=env('FAKE_PLACE','page')
    notes=[n['key'] for n in packet['notes']]
    if mode=='new':done({'placements':[{'note':k,'page':'NP1'} for k in notes],'new_pages':[{'key':'NP1','section':'S1','title':'Fresh page','purpose':'Cover the new notes.'}]})
    elif mode=='omit':done({'placements':[{'note':k,'page':None,'reason':'Not useful to a reader.'} for k in notes],'new_pages':[]})
    else:done({'placements':[{'note':k,'page':'P1'} for k in notes],'new_pages':[]})
else:
    src=[s['key'] for s in packet['sources']];others=packet['other_pages']
    link=' See [[%%s]].'%%others[0]['key'] if others else ''
    body='## Details\\n'+' '.join('Claim %%d.[%%s]'%%(i,k) for i,k in enumerate(src))+link
    if env('FAKE_EVIL'):body+=' </script><img src=x onerror=alert(1)> and [[%%s]]'%%others[0]['key'] if others else ''
    done({'lead':'This page covers %%s.[%%s]'%%(title,src[0]),'body':'## Details\\n[N99] unknown.' if bad else body,'cited':src})
''' % (list(agent.REQUIRED_FLAGS),)

PAGE_RE=re.compile(r'\[\^(\d+)\]')

class Base(tw.Fixture):
    def setUp(self):
        super().setUp()
        self.script=self.base/'fake_claude.py';self.script.write_text(FAKE,encoding='utf-8');self.exe=[sys.executable,str(self.script)]
        self.log=self.base/'fake.log'
        env=patch.dict(os.environ,{'FAKE_LOG':str(self.log),'HIVEMIND_SESSION':'','LIVING_MEMORY_MAINTENANCE_CHILD':'','FAKE_PAGES':'2','FAKE_COST':'0.01'})
        env.start();self.addCleanup(env.stop)
        for name in ('HIVEMIND_SESSION','LIVING_MEMORY_MAINTENANCE_CHILD'):os.environ.pop(name,None)
    def calls(self):
        return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(self.log.glob('*.json'))] if self.log.exists() else []
    def kinds(self):
        c=self.calls();return {k:sum(1 for x in c if x['task']==k) for k in ('outline','place','page')}
    def build(self,**kw):
        with patch.dict(os.environ,{k:str(v) for k,v in kw.pop('env',{}).items()}):
            return wa.build(self.root,'demo',executable=self.exe,**kw)
    def edition(self,scope='domain'):
        return json.loads(wa.edition_path(self.root,'demo',scope).read_text(encoding='utf-8'))
    def run_cli(self,*argv,actor='main'):
        out,err=io.StringIO(),io.StringIO()
        real=agent.provider
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch.object(agent,'provider',lambda cfg=None,executable=None:real(cfg,self.exe)):
            try:code=main(['--root',str(self.root),'--actor',actor,'--domain','demo',*argv])
            except SystemExit as exc:code=exc.code
        return code,out.getvalue(),err.getvalue()
    def cli_json(self,*argv,**kw):
        code,out,err=self.run_cli(*argv,**kw);text=out or err
        return code,(json.loads(text) if text.strip()[:1] in '{[' else text)

class Validation(unittest.TestCase):
    outline={'notes':{'N1':'a','N2':'b','N3':'c'}}
    def good(self):
        return {'summary':'A wiki.','sections':[{'key':'S1','title':'Core','blurb':'Core pages.','pages':[
            {'key':'P1','title':'First','purpose':'Explain the first thing.','sources':['N1','N2']},{'key':'P2','title':'Second','purpose':'The second.','sources':['N2']}]}],'omit':[{'note':'N3','reason':'Obsolete.'}]}
    def test_a_good_outline_maps_keys_to_ids(self):
        out=wa.validate_outline(self.good(),self.outline)
        self.assertEqual([p['sources'] for p in out['sections'][0]['pages']],[['a','b'],['b']]);self.assertEqual(out['omit'],[{'id':'c','reason':'Obsolete.'}])
    def test_outline_failures(self):
        def breaks(edit,text):
            r=self.good();edit(r)
            with self.assertRaises(wa.WikiError,msg=text) as c:wa.validate_outline(r,self.outline)
            return str(c.exception)
        breaks(lambda r:r['sections'][0]['pages'][0]['sources'].append('N9'),'unknown note key')
        breaks(lambda r:r['sections'][0]['pages'][0]['sources'].append('N1'),'a source twice')
        breaks(lambda r:r['sections'][0]['pages'][1].update(key='P1'),'a page key twice')
        breaks(lambda r:r['sections'].append(dict(r['sections'][0],pages=[])),'a section with no pages')
        breaks(lambda r:r['sections'].append(dict(r['sections'][0],pages=[dict(r['sections'][0]['pages'][0],key='P3')])),'a section key twice')
        breaks(lambda r:r['sections'][0]['pages'][1].update(title='first'),'a page title twice, ignoring case')
        breaks(lambda r:r['omit'].append({'note':'N3','reason':'Again.'}),'an omit twice')
        breaks(lambda r:r['omit'].append({'note':'N1','reason':'Also used.'}),'an omitted source')
        breaks(lambda r:r.update(extra=1),'an unknown key')
        breaks(lambda r:r['sections'][0]['pages'][0].update(sources=[]),'a page with no sources')
        breaks(lambda r:r['sections'][0]['pages'][0].update(purpose='x'*(wa.PURPOSE_MAX+1)),'an over-long purpose')
        breaks(lambda r:r.update(sections=[]),'no sections')
        breaks(lambda r:r['sections'].extend({**r['sections'][0],'key':f'S{i}','pages':[dict(r['sections'][0]['pages'][0],key=f'P{i}',title=f'T{i}')]} for i in range(2,15)),'13 sections')
    def test_the_page_limit(self):
        notes={'notes':{f'N{i}':f'n{i}' for i in range(1,wa.PAGES_MAX+2)}}
        report={'summary':'x','sections':[{'key':'S1','title':'All','blurb':'b','pages':[{'key':f'P{i}','title':f'Page {i}','purpose':'p','sources':[f'N{i}']} for i in range(1,wa.PAGES_MAX+2)]}],'omit':[]}
        with self.assertRaises(wa.WikiError):wa.validate_outline(report,notes)
        report['sections'][0]['pages'].pop();self.assertEqual(len(wa.validate_outline(report,notes)['sections'][0]['pages']),wa.PAGES_MAX)
    def test_titles_lose_link_breaking_characters(self):
        r=self.good();r['sections'][0]['pages'][0]['title']='C# [and] a|bar'
        self.assertEqual(wa.validate_outline(r,self.outline)['sections'][0]['pages'][0]['title'],'C and a bar')
    def test_placement_rules(self):
        m={'notes':{'N1':'a','N2':'b'},'pages':{'P1':'pg1'},'sections':{'S1':'a1'},'capacity':{'P1':2},'titles':set()}
        ok=wa.validate_place({'placements':[{'note':'N1','page':'P1'},{'note':'N2','page':'NP1'}],'new_pages':[{'key':'NP1','section':'S1','title':'New','purpose':'p'}]},m,1)
        self.assertEqual([p['page'] for p in ok['placements']],['P1','NP1'])
        for bad in ({'placements':[{'note':'N3','page':'P1'}],'new_pages':[]},{'placements':[{'note':'N1','page':'P2'}],'new_pages':[]},
                    {'placements':[{'note':'N1','page':'P1'},{'note':'N1','page':'P1'}],'new_pages':[]},{'placements':[{'note':'N1','page':None}],'new_pages':[]},
                    {'placements':[],'new_pages':[{'key':'NP1','section':'S9','title':'New','purpose':'p'}]},
                    {'placements':[],'new_pages':[{'key':'NP1','section':'S1','title':'New','purpose':'p'}]*2}):
            with self.assertRaises(wa.WikiError,msg=bad):wa.validate_place(bad,m,1)
        with self.assertRaises(wa.WikiError):wa.validate_place({'placements':[],'new_pages':[{'key':'NP1','section':'S1','title':'New','purpose':'p'}]},m,wa.PAGES_MAX)
    def test_citations_become_numbered_footnotes_in_order_of_first_appearance(self):
        m={'notes':{'N1':'a','N2':'b','N3':'c'},'pages':{'P1':'pg1'}}
        out=wa.validate_page({'lead':'Lead fact.[N3]','body':'## Part\nOne.[N1] Two.[N3][N2] Three [N1, N2] and see [[P1]].','cited':['N1','N2','N3']},m,{'P1':'Other page'})
        self.assertEqual(out['lead'],'Lead fact.[^1]');self.assertEqual(out['body'],'## Part\nOne.[^2] Two.[^1][^3] Three [^2][^3] and see [[Other page]].')
        self.assertEqual(out['sources'],['c','a','b'])
    def test_page_failures(self):
        m={'notes':{'N1':'a','N2':'b'},'pages':{'P1':'pg1'}}
        def bad(**kw):
            r={'lead':'Lead.[N1]','body':'Body.[N1]','cited':['N1'],**kw}
            with self.assertRaises(wa.WikiError,msg=kw):wa.validate_page(r,m,{'P1':'Other'})
        bad(body='Unknown.[N7]');bad(lead='No marker.',body='Nor here.');bad(cited=['N1','N1']);bad(cited=['N5']);bad(body='A footnote.[^1][N1]')
        bad(body='A link [[P4]] to nowhere.[N1]');bad(lead='x'*(wa.LEAD_MAX+1));bad(body='x'*(wa.BODY_MAX+1));bad(body='  ');bad(extra=1)
    def test_the_place_packet_gives_page_capacity_and_validation_enforces_it(self):   # AW-5
        note=lambda i:{'id':i,'title':i,'type':'knowledge','tags':[],'files':[],'updated':'2026-01-01','lead':'','body':'b','project':'p'}
        edition={'sections':[{'key':'a1','title':'Core','blurb':'b','pages':['pg1','pg2']}],'pages':{
            'pg1':{'title':'Full','purpose':'p','plan':[f'x{i}' for i in range(wa.SOURCES_MAX)]},'pg2':{'title':'Roomy','purpose':'q','plan':['x0']}}}
        text,m=wa.place_packet('demo','domain',edition,[note('n1'),note('n2')]);pages=json.loads(text)['outline']['sections'][0]['pages']
        self.assertEqual([(p['key'],p['capacity']) for p in pages],[('P1',0),('P2',wa.SOURCES_MAX-1)]);self.assertEqual(m['capacity'],{'P1':0,'P2':wa.SOURCES_MAX-1})
        wa.validate_place({'placements':[{'note':'N1','page':'P2'},{'note':'N2','page':'P2'}],'new_pages':[]},m,2)
        for page in ('P1',):
            with self.assertRaises(wa.WikiError,msg='a full page'):wa.validate_place({'placements':[{'note':'N1','page':page},{'note':'N2','page':'P2'}],'new_pages':[]},m,2)
        m['capacity']['P2']=1
        with self.assertRaises(wa.WikiError,msg='more notes than the page has room for'):wa.validate_place({'placements':[{'note':'N1','page':'P2'},{'note':'N2','page':'P2'}],'new_pages':[]},m,2)
    def test_a_new_page_may_not_reuse_a_saved_title(self):   # AW-6
        note=lambda i:{'id':i,'title':i,'type':'knowledge','tags':[],'files':[],'updated':'2026-01-01','lead':'','body':'b','project':'p'}
        edition={'sections':[{'key':'a1','title':'Core','blurb':'b','pages':['pg1']}],'pages':{'pg1':{'title':'Hive Overview','purpose':'p','plan':['x']}}}
        _,m=wa.place_packet('demo','domain',edition,[note('n1')])
        report=lambda title:{'placements':[{'note':'N1','page':'NP1'}],'new_pages':[{'key':'NP1','section':'S1','title':title,'purpose':'p'}]}
        with self.assertRaises(wa.WikiError):wa.validate_place(report('hive overview'),m,1)
        self.assertEqual(wa.validate_place(report('Another page'),m,1)['new_pages'][0]['title'],'Another page')
    def test_a_placement_must_cover_every_new_note(self):   # AW-7
        m={'notes':{'N1':'a','N2':'b'},'pages':{'P1':'pg1'},'sections':{'S1':'a1'},'capacity':{'P1':5},'titles':set()}
        with self.assertRaises(wa.WikiError) as c:wa.validate_place({'placements':[{'note':'N1','page':'P1'}],'new_pages':[]},m,1)
        self.assertIn('N2',str(c.exception))
        self.assertEqual(len(wa.validate_place({'placements':[{'note':'N2','page':'P1'},{'note':'N1','page':'P1'}],'new_pages':[]},m,1)['placements']),2)
    def test_equal_bodies_are_cut_together_to_the_cap(self):   # AW-8
        edition={'sections':[{'key':'a1','title':'Core','blurb':'b','pages':['pg1']}],'pages':{'pg1':{'title':'One','purpose':'p','section':'a1','plan':['x','y']}}}
        note=lambda i,size:{'id':i,'title':i,'type':'knowledge','tags':[],'files':[],'updated':'2026-01-01','lead':'','body':'w'*size}
        for sizes in ((80_000,80_000),(80_000,80_000,3_000),(60_000,60_000,60_000)):
            plan=[f'n{i}' for i in range(len(sizes))];edition['pages']['pg1']['plan']=plan
            packet=json.loads(wa.page_packet('demo','domain',edition,'pg1',{p:note(p,n) for p,n in zip(plan,sizes)})[0])
            got=[len(s['body']) for s in packet['sources']];self.assertLessEqual(sum(got),wa.PAGE_SOURCES_MAX,sizes);self.assertTrue(all(g>=min(n,wa.SOURCE_BODY_MIN) for g,n in zip(got,sizes)))
            self.assertTrue(all(s.get('cut') for s,n in zip(packet['sources'],sizes) if n>=60_000),sizes)
        self.assertEqual(got,[50_000]*3)
    def test_code_is_left_alone_by_citations_and_page_links(self):   # AW-9
        m={'notes':{'N1':'a','N2':'b'},'pages':{'P1':'pg1'}}
        body='Use `[N9]` or `[[P7]]` here.[N1] See [[P1]].\n```js\nx[N2]; [[P9]]; re=/[^1]/\n```\nAfter.[N1]\n'
        out=wa.validate_page({'lead':'Fact `[N8]`.[N1]','body':body,'cited':['N1']},m,{'P1':'Other'})
        self.assertEqual(out['body'],'Use `[N9]` or `[[P7]]` here.[^1] See [[Other]].\n```js\nx[N2]; [[P9]]; re=/[^1]/\n```\nAfter.[^1]');self.assertEqual((out['lead'],out['sources']),('Fact `[N8]`.[^1]',['a']))
        with self.assertRaises(wa.WikiError):wa.validate_page({'lead':'Lead.[N1]','body':'Prose [N9] and `code`.','cited':['N1']},m,{'P1':'Other'})   # prose is still checked
        with self.assertRaises(wa.WikiError):wa.validate_page({'lead':'Lead.[N1]','body':'A footnote.[^1]\n```\ncode\n```\n','cited':['N1']},m,{'P1':'Other'})
    def test_project_editions_are_files_with_a_prefix(self):   # AW-11
        self.assertEqual(wa.edition_path('r','demo','domain').name,'domain.json')
        for slug in ('hive','con','nul','com1','aux','prn'):self.assertEqual(wa.edition_path('r','demo',slug).name,f'project-{slug}.json')
    def test_page_packets_cut_the_longest_bodies_first(self):
        edition={'sections':[{'key':'a1','title':'Core','blurb':'b','pages':['pg1','pg2']}],'pages':{'pg1':{'title':'One','purpose':'p','section':'a1','plan':['x','y','z']},'pg2':{'title':'Two','purpose':'q'}}}
        note=lambda i,size:{'id':i,'title':i,'type':'knowledge','tags':[],'files':[],'updated':'2026-01-01','lead':'','body':'w'*size}
        notes={'x':note('x',120_000),'y':note('y',60_000),'z':note('z',1_000)}
        text,manifest=wa.page_packet('demo','domain',edition,'pg1',notes);packet=json.loads(text)
        sizes={s['title']:len(s['body']) for s in packet['sources']}
        self.assertLessEqual(sum(sizes.values()),wa.PAGE_SOURCES_MAX);self.assertEqual(sizes['z'],1_000);self.assertTrue(sizes['x']>=wa.SOURCE_BODY_MIN)
        self.assertEqual({s['title'] for s in packet['sources'] if s.get('cut')},{'x','y'} if sizes['y']<60_000 else {'x'});self.assertEqual(manifest['pages'],{'P1':'pg2'})
        self.assertEqual(wa.page_packet('demo','domain',edition,'pg1',{}),(None,None))
    def test_credentials_are_redacted_from_packets(self):
        edition={'sections':[{'key':'a1','title':'Core','blurb':'b','pages':['pg1']}],'pages':{'pg1':{'title':'One','purpose':'p','section':'a1','plan':['x']}}}
        secret='sk-ant-'+'Ab1'*12
        note={'id':'x','title':'t','type':'knowledge','tags':[],'files':[],'updated':'2026-01-01','lead':'','body':'The key is '+secret}
        text,_=wa.page_packet('demo','domain',edition,'pg1',{'x':note});self.assertNotIn(secret,text);self.assertIn('redacted',text)
    def test_the_policies_carry_the_trust_paragraph_and_the_writing_rules(self):
        for kind,policy in wa.POLICIES.items():
            for phrase in ('untrusted data','You have no tools','em or en dashes','Lead with the fact','one name','delve, tapestry, seamless, showcase, vibrant, meticulous, additionally'):self.assertIn(phrase,policy,kind)
        self.assertIn('[N3]',wa.PAGE_POLICY);self.assertIn('Never invent',wa.PAGE_POLICY)
        for kind,policy in wa.POLICIES.items():self.assertIn('sentence case',policy,kind)   # AW-37
        self.assertNotIn('Overview, Architecture and Services',wa.OUTLINE_POLICY)
        for policy in (wa.OUTLINE_POLICY,wa.PLACE_POLICY):self.assertIn('removed or replaced',policy)   # AW-38
        self.assertIn('the date it was last updated',wa.OUTLINE_POLICY)
        # An obsolete note kept for its history is a source, not omitted: the outline refuses a note that is both.
        self.assertIn('keep it as a source (not in omit)',wa.OUTLINE_POLICY);self.assertIn('the updated date is only a hint',wa.OUTLINE_POLICY)
        for kind,schema in wa.SCHEMAS.items():self.assertEqual(json.loads(schema),json.loads(json.dumps(getattr(wa,kind.upper()+'_SCHEMA'))))

    def test_title_case_is_told_from_sentence_case(self):   # AW-37
        for title in ('Tauri Core, Settings and Watcher','Settings And Watcher Flow Details','The `x` Method And Flow Notes'):
            self.assertTrue(wa.title_case(title),title)
        # Product names often carry two capitalised words, so two are not enough.
        for title in ('Settings and watcher','Living Memory hooks','Claude Code','How the API uses OAuth tokens','Overview','`Build.Run Method` notes',
                      'Run Docker on Windows','Configure GitHub Actions','Visual Studio Code','Hivemind Executive Workspace',
                      'Compare Win32, Direct3D and OpenGL'):   # words with digits do not count (Codex re-review F7)
            self.assertFalse(wa.title_case(title),title)
        self.assertEqual(wa.case_warnings('Payments overview','## Fine heading\n## Core Pages And Services Today\n```\n## Not A Heading Here At All\n```'),
                         ['Title Case heading ("Core Pages And Services Today"); write titles and headings in sentence case'])
        section={'key':'a1','title':'Core Pages And Services Today','pages':['pg1','pg2']}
        self.assertEqual(wa.section_warnings(section,'pg1'),['Title Case heading ("Core Pages And Services Today"); write titles and headings in sentence case'])
        self.assertEqual(wa.section_warnings(section,'pg2'),[],'once per section')
        self.assertEqual(wa.section_warnings(dict(section,title='Core pages and services'),'pg1'),[])

class Build(Base):
    def test_a_first_build_plans_then_writes_every_page(self):
        self.sample();r=self.build(progress=(events:=[]).append)
        self.assertEqual((r['ok'],r['stopped'],r['calls'],r['failed'],r['pending'],r['dry_run'],r['scope'],r['model']),(True,None,{'outline':1,'place':0,'pages':2},[],[],False,'domain','sonnet'))
        self.assertEqual(len(r['written']),2);self.assertAlmostEqual(r['cost_usd'],0.03);self.assertEqual(r['budget_usd'],5.0);self.assertEqual(Path(r['path']),wa.edition_path(self.root,'demo','domain'))
        ed=self.edition();self.assertEqual(ed['cost_micro'],30_000);self.assertEqual([s['key'] for s in ed['sections']],['a1'])
        for pid,page in ed['pages'].items():
            self.assertRegex(pid,r'^pg[0-9a-f]{6}$');self.assertTrue(page['written'] and not page['dirty']);self.assertEqual(page['model'],'sonnet')
            self.assertEqual(len(page['sources']),len(page['plan']));self.assertTrue(all(re.fullmatch(r'\[\^\d+\]',m) for m in re.findall(r'\[\^\d+\]',page['lead']+page['body'])))
        self.assertEqual([e['event'] for e in events],['start','call','call','call','end']);self.assertEqual(events[0],{'event':'start','scope':'domain','calls':None})
        self.assertEqual([(e['kind'],e['done'],e['total']) for e in events[1:4]],[('outline',1,3),('page',2,3),('page',3,3)]);self.assertEqual(events[-1]['ok'],True)
        self.assertEqual({c['model'] for c in self.calls()},{'sonnet'});self.assertTrue(all(c['policy']==wa.POLICIES[c['task']] for c in self.calls()))
        self.assertEqual([c['budget'] for c in self.calls() if c['task']=='outline'],['2.00'])
    def test_a_cost_above_the_grant_stops_further_calls(self):   # AW-1
        self.sample()
        with patch.object(wa,'WORKERS',1):r=self.build(budget='20',env={'FAKE_PAGES':'3','FAKE_COST':'1.2'})
        self.assertEqual((r['ok'],r['stopped'],r['calls'],len(r['written']),len(r['pending'])),(True,'budget',{'outline':1,'place':0,'pages':1},1,2));self.assertAlmostEqual(r['overrun_usd'],0.2);self.assertAlmostEqual(r['cost_usd'],2.4)
        self.assertEqual(self.kinds(),{'outline':1,'place':0,'page':1});self.assertNotIn('overrun_usd',self.build(budget='20',env={'FAKE_PAGES':'3','FAKE_COST':'0.01'}))
    def test_the_edition_is_read_under_the_lock(self):   # AW-2
        self.sample();path=wa.edition_path(self.root,'demo','domain');seen=[];real=wa.load_edition
        def probe(p):
            try:
                with file_lock(path.parent/(path.name+'.lock'),timeout=0):seen.append('free')
            except TimeoutError:seen.append('locked')
            return real(p)
        with patch.object(wa,'load_edition',probe):self.build();self.build(dry_run=True)
        self.assertEqual(seen,['locked','free'])   # a real build reads it under the lock; a dry run may read it without
    def test_note_text_and_revision_come_from_one_read(self):   # AW-3
        from core import repository
        ids=self.sample();nid=ids['bug'];real=repository.inventory;state=[];old={n['id']:n for n in real('demo',self.root)}[nid]
        def once(*a,**k):
            out=real(*a,**k)
            if not state:state.append(1);change(nid,'demo','update',self.root,body='Changed after the first read.')
            return out
        with patch.object(repository,'inventory',once),patch.object(wa,'inventory',once):_,notes,revisions=wa.gather('demo',self.root)
        self.assertNotEqual(old['revision'],{n['id']:n for n in real('demo',self.root)}[nid]['revision'])
        self.assertEqual((revisions[nid],notes[nid]['lead']),(old['revision'],'The app crashed.'))
    def test_progress_counters_never_go_backward(self):   # AW-12
        events=[]
        def progress(e):
            if e['event']=='call' and e['done']==2:time.sleep(1.0)   # a slow listener must not let the next call overtake it
            events.append(e)
        self.sample();self.build(progress=progress);self.assertEqual([e['done'] for e in events if e['event']=='call'],[1,2,3])
    def test_a_signal_cancels_the_build_and_kills_the_running_calls(self):   # AW-14
        self.sample();self.build();ed=self.edition()
        for page in ed['pages'].values():change(page['plan'][0],'demo','update',self.root,body='A changed body for '+page['title'])
        for sig in (signal.SIGTERM,signal.SIGINT):
            before=len(self.calls());old=signal.getsignal(sig)
            def fire():
                end=time.monotonic()+30
                while time.monotonic()<end and len(self.calls())<before+2:time.sleep(0.05)
                signal.raise_signal(sig)
            thread=threading.Thread(target=fire);thread.start();start=time.monotonic()
            r=self.build(env={'FAKE_SLEEP':'120'});thread.join()
            self.assertLess(time.monotonic()-start,60,sig);self.assertEqual((r['ok'],r['stopped'],r['error'],r['written']),(False,'error','cancelled',[]),sig)
            self.assertIs(signal.getsignal(sig),old);self.assertEqual({p['id']:p['body'] for p in self.edition()['pages'].values()},{i:p['body'] for i,p in ed['pages'].items()})
    def cancel_after(self,*kinds):
        real=wa.Build.ask
        def ask(job,kind,packet,validate):
            out=real(job,kind,packet,validate)
            if kind in kinds:job.cancel()   # the signal lands after the answer came back
            return out
        return patch.object(wa.Build,'ask',ask)
    def test_an_answer_that_arrives_after_a_signal_is_not_applied(self):   # AW-24
        ids=self.sample()
        with self.cancel_after('outline'):r=self.build()
        self.assertEqual((r['ok'],r['stopped'],r['error'],r['written']),(False,'error','cancelled',[]));self.assertFalse(wa.edition_path(self.root,'demo','domain').exists())
        self.build();ed=self.edition();shape=lambda e:({i:(p['title'],p['body']) for i,p in e['pages'].items()},[s['key'] for s in e['sections']])
        with self.cancel_after('outline'):r=self.build(restructure=True,env={'FAKE_PAGES':'3'})   # a cancelled --restructure keeps the saved outline
        self.assertEqual((r['error'],r['written']),('cancelled',[]));self.assertEqual(shape(self.edition()),shape(ed))
        self.add('Fresh note','Something new.')
        with self.cancel_after('place'):r=self.build(env={'FAKE_PLACE':'new'})
        self.assertEqual((r['error'],r['written']),('cancelled',[]));self.assertEqual(shape(self.edition()),shape(ed))
        owner=next(p for p in ed['pages'].values() if ids['bug'] in [s['id'] for s in p['sources']]);change(ids['bug'],'demo','update',self.root,body='The app crashed on start; fixed in 1.2.')
        with patch.object(wa,'WORKERS',1),self.cancel_after('page'):r=self.build()
        self.assertEqual((r['error'],r['written']),('cancelled',[]));self.assertEqual(self.edition()['pages'][owner['id']]['body'],owner['body'])
    def test_a_build_off_the_main_thread_installs_no_handler(self):   # AW-14
        self.sample();box=[];thread=threading.Thread(target=lambda:box.append(self.build()));thread.start();thread.join();self.assertTrue(box[0]['ok'])
    def test_claude_is_taken_from_LIVING_MEMORY_CLAUDE_before_PATH(self):   # AW-18
        self.sample();seen=[];real=agent.provider
        def spy(cfg=None,executable=None):
            seen.append(executable);return real(cfg,self.exe)
        shim=self.base/'claude.cmd';shim.write_text('@echo off',encoding='utf-8')
        with patch.object(agent,'provider',spy):
            with patch.dict(os.environ,{'LIVING_MEMORY_CLAUDE':str(self.script)}):self.assertTrue(wa.build(self.root,'demo')['ok'])
            self.assertEqual(seen,[[str(self.script)]])
            with patch.dict(os.environ,{'LIVING_MEMORY_CLAUDE':str(self.script)}):wa.build(self.root,'demo',executable=self.exe)   # an explicit executable wins
            self.assertEqual(seen[1],self.exe)
            with patch.dict(os.environ,{'LIVING_MEMORY_CLAUDE':''}):wa.build(self.root,'demo');self.assertIsNone(seen[2])   # unset: PATH, as before
            for value,text in ((str(shim),'script shim'),('claude',"absolute path"),(str(self.base/'missing'/'claude'),'absolute path')):
                with patch.dict(os.environ,{'LIVING_MEMORY_CLAUDE':value}),self.assertRaises(ValueError,msg=value) as c:wa.build(self.root,'demo')
                self.assertIn('claude is not available',str(c.exception));self.assertIn(text,str(c.exception))
        self.assertEqual(len(seen),3)
    def test_an_untouched_wiki_makes_no_calls(self):
        self.sample();self.build();before=len(self.calls())
        r=self.build();self.assertEqual((r['calls'],r['written'],r['cost_usd'],r['stopped']),({'outline':0,'place':0,'pages':0},[],0,None));self.assertEqual(len(self.calls()),before)
    def test_a_changed_source_makes_only_its_page_stale(self):
        ids=self.sample();self.build();ed=self.edition()
        owner=next(p for p in ed['pages'].values() if ids['bug'] in [s['id'] for s in p['sources']]);other=next(p for p in ed['pages'].values() if p['id']!=owner['id'])
        self.assertIn(ids['bug'],[s['id'] for s in owner['sources']])
        change(ids['bug'],'demo','update',self.root,body='The app crashed on start; fixed in 1.2.')
        m=wa.read_model(self.root,'demo');pages={n['id']:n for n in m['notes'] if n['type']=='article'}
        self.assertEqual({i:n['page']['stale'] for i,n in pages.items()},{owner['id']:True,other['id']:False})
        self.assertEqual((m['build']['stale'],m['build']['pending'],m['build']['complete']),(1,1,False))
        self.assertEqual([(s['title'],s['current']) for s in pages[owner['id']]['page']['sources'] if s['id']==ids['bug']],[('Crash on start',False)])
        calls=len(self.calls());r=self.build();self.assertEqual((r['calls'],r['written']),({'outline':0,'place':0,'pages':1},[owner['id']]));self.assertEqual(self.calls()[calls]['title'],owner['title'])
        m=wa.read_model(self.root,'demo');self.assertEqual((m['build']['stale'],m['build']['pending'],m['build']['complete']),(0,0,True))
    def test_an_archived_source_is_missing_and_the_page_is_rewritten_without_it(self):
        ids=self.sample();self.build();change(ids['bug'],'demo','resolve',self.root)
        m=wa.read_model(self.root,'demo');flags=[s for n in m['notes'] if n['type']=='article' for s in n['page']['sources'] if s['id']==ids['bug']]
        self.assertEqual([(s['missing'],s['current']) for s in flags],[(True,False)]);self.assertNotIn(ids['bug'],[n['id'] for n in m['notes']])
        r=self.build();self.assertEqual(r['calls']['pages'],1);self.assertNotIn(ids['bug'],[s['id'] for p in self.edition()['pages'].values() for s in p['sources']])
        self.assertEqual(self.build()['calls'],{'outline':0,'place':0,'pages':0})
    def test_a_new_note_goes_to_one_place_call_and_its_page_is_rewritten(self):
        self.sample();self.build();extra=self.add('Fresh note','Something new.',['project/hive']);calls=len(self.calls())
        r=self.build();self.assertEqual(r['calls'],{'outline':0,'place':1,'pages':1});new=self.calls()[calls:];self.assertEqual([c['task'] for c in new],['place','page'])
        ed=self.edition();target=ed['pages'][r['written'][0]];self.assertIn(extra,target['plan']);self.assertIn(extra,[s['id'] for s in target['sources']])
        self.assertEqual(self.build()['calls'],{'outline':0,'place':0,'pages':0})
    def test_the_place_call_may_add_a_page_or_omit_a_note(self):
        self.sample();self.build();extra=self.add('Fresh note','Something new.')
        r=self.build(env={'FAKE_PLACE':'new'});ed=self.edition();fresh=next(p for p in ed['pages'].values() if p['title']=='Fresh page')
        self.assertEqual((r['calls'],r['written']),({'outline':0,'place':1,'pages':1},[fresh['id']]));self.assertEqual(fresh['plan'],[extra]);self.assertIn(fresh['id'],ed['sections'][0]['pages'])
        third=self.add('Third note','Another one.');r=self.build(env={'FAKE_PLACE':'omit'})
        self.assertEqual((r['calls'],r['written']),({'outline':0,'place':1,'pages':0},[]));self.assertEqual([o['reason'] for o in self.edition()['omit'] if o['id']==third],['Not useful to a reader.'])
        self.assertEqual(self.build()['calls'],{'outline':0,'place':0,'pages':0})
    def test_omitted_notes_are_listed_with_their_reason(self):
        ids=self.sample();r=self.build(env={'FAKE_OMIT':'1'});self.assertTrue(r['ok']);m=wa.read_model(self.root,'demo')
        self.assertEqual(len(m['build']['omitted']),1);self.assertEqual(m['build']['omitted'][0]['reason'],'A transient status.');self.assertIn(m['build']['omitted'][0]['id'],ids.values())
    def test_restructure_keeps_ids_and_unchanged_pages(self):
        self.sample();self.build();before=self.edition();calls=len(self.calls())
        r=self.build(restructure=True);self.assertEqual(r['calls'],{'outline':1,'place':0,'pages':0});self.assertEqual(len(self.calls()),calls+1)
        after=self.edition();self.assertEqual(set(after['pages']),set(before['pages']));self.assertEqual({i:p['body'] for i,p in after['pages'].items()},{i:p['body'] for i,p in before['pages'].items()})
        r=self.build(restructure=True,env={'FAKE_PAGES':'3'});self.assertEqual(r['calls']['outline'],1);ed=self.edition();self.assertEqual(len(ed['pages']),3)
        self.assertTrue(set(before['pages'])<=set(ed['pages']))   # the old pages keep their ids
        self.assertEqual(len(ed['sections']),2)
    def test_a_failed_outline_stops_with_an_error_and_saves_nothing(self):
        self.sample()
        for env in ({'FAKE_FAIL':'outline'},{'FAKE_BAD':'outline'}):
            r=self.build(env=env);self.assertEqual((r['ok'],r['stopped'],r['calls'],r['written']),(False,'error',{'outline':1,'place':0,'pages':0},[]));self.assertIn('outline call failed',r['error'])
            self.assertFalse(wa.edition_path(self.root,'demo','domain').exists())
        self.assertGreater(r['cost_usd'],0)
    def test_a_budget_error_with_an_overrun_is_a_budget_stop_for_outline_and_place(self):   # AW-26
        self.sample();over={'FAKE_SUBTYPE':'error_max_budget_usd','FAKE_COST':'2.5'}
        r=self.build(env={'FAKE_FAIL':'outline',**over});self.assertEqual((r['ok'],r['stopped'],r['calls']['outline'],r['written'],'error' in r),(True,'budget',1,[],False));self.assertAlmostEqual(r['overrun_usd'],0.5)
        self.assertFalse(wa.edition_path(self.root,'demo','domain').exists());self.build();self.add('Fresh note','Something new.')
        r=self.build(env={'FAKE_FAIL':'place',**over});self.assertEqual((r['ok'],r['stopped'],r['calls']['place']),(True,'budget',1));self.assertIn('overrun_usd',r)
        r=self.build(env={'FAKE_FAIL':'place','FAKE_SUBTYPE':'error_max_budget_usd','FAKE_COST':'0.5'});self.assertEqual((r['ok'],r['stopped']),(False,'error'))   # no overrun: still an error
    def test_a_failed_page_keeps_its_previous_version(self):
        ids=self.sample();self.build();ed=self.edition()
        owner=next(p for p in ed['pages'].values() if ids['bug'] in [s['id'] for s in p['sources']]);change(ids['bug'],'demo','update',self.root,body='The app crashed on start; fixed in 1.2.')
        for env in ({'FAKE_FAIL':'page'},{'FAKE_BAD':'page'}):
            r=self.build(env=env);self.assertEqual((r['ok'],r['written'],r['pending']),(True,[],[]));self.assertEqual([f['page'] for f in r['failed']],[owner['id']])
            self.assertEqual(self.edition()['pages'][owner['id']]['body'],owner['body']);self.assertTrue(r['failed'][0]['reason'])
        self.assertEqual(self.build()['written'],[owner['id']])
    def test_a_failed_place_call_stops_the_build(self):
        self.sample();self.build();self.add('Fresh note','Something new.');r=self.build(env={'FAKE_FAIL':'place'})
        self.assertEqual((r['ok'],r['stopped'],r['calls']['place'],r['calls']['pages']),(False,'error',1,0))
    def test_the_budget_stops_the_build_and_the_next_one_continues(self):
        for i in range(6):self.add(f'Note {i}',f'Body {i}.')
        r=self.build(budget='0.12',env={'FAKE_PAGES':'6','FAKE_COST':'0.03'})
        self.assertEqual((r['ok'],r['stopped'],r['calls'],len(r['written']),len(r['pending'])),(True,'budget',{'outline':1,'place':0,'pages':2},2,4));self.assertAlmostEqual(r['cost_usd'],0.09)
        self.assertLessEqual(r['cost_usd'],r['budget_usd']);self.assertEqual(self.edition()['cost_micro'],90_000)
        m=wa.read_model(self.root,'demo');self.assertEqual((m['build']['written'],m['build']['pending'],m['build']['complete']),(2,4,False))
        self.assertEqual(sorted(n['id'] for n in m['notes'] if n['type']=='article' and not n['page']['written']),sorted(r['pending']))
        r2=self.build(budget='1.00',env={'FAKE_PAGES':'6','FAKE_COST':'0.03'});self.assertEqual((r2['stopped'],r2['calls'],sorted(r2['written'])),(None,{'outline':0,'place':0,'pages':4},sorted(r['pending'])))
        self.assertEqual(self.edition()['cost_micro'],210_000)
    def test_each_call_is_capped_by_what_is_left(self):
        self.sample();self.build(budget='0.30');calls=self.calls()
        self.assertEqual([c['budget'] for c in calls if c['task']=='outline'],['0.30']);self.assertTrue(all(float(c['budget'])<=0.30 for c in calls))   # the outline cap is $2, so the whole budget is its cap
    def test_a_dry_run_spends_and_writes_nothing(self):
        self.sample();first=self.build(dry_run=True)
        self.assertEqual((first['ok'],first['dry_run'],first['notes'],first['calls'],first['budget_usd'],first['scope'],first['model']),(True,True,8,{'outline':1,'place':0,'pages':None},5.0,'domain','sonnet'))
        self.assertGreater(first['packet_chars']['outline'],500);self.assertEqual((first['packet_chars']['place'],first['packet_chars']['page_max']),(0,0))
        self.assertFalse(self.log.exists());self.assertFalse(wa.edition_path(self.root,'demo','domain').exists())
        self.build();calls=len(self.calls());self.add('Fresh note','Something new.')
        later=self.build(dry_run=True);self.assertEqual((later['calls'],later['packet_chars']['outline']>0),({'outline':0,'place':1,'pages':0},False));self.assertGreater(later['packet_chars']['place'],0)
        first=next(iter(self.edition()['pages'].values()))['plan'][0];change(first,'demo','update',self.root,body='A changed body.')
        again=self.build(dry_run=True);self.assertEqual(again['calls']['pages'],1);self.assertGreater(again['packet_chars']['page_max'],0);self.assertEqual(len(self.calls()),calls)
    def test_the_outline_packet_has_no_bodies_and_an_oversized_domain_is_refused(self):
        notes=[{'id':str(i),'ref':str(i)[:8],'title':'T'*150,'type':'knowledge','tags':['a'],'project':'p','files':['f'],'lead':'L'*300,'body':'B'*5000} for i in range(1,1500)]
        with self.assertRaises(ValueError) as c:wa.outline_packet('demo','domain',notes)
        self.assertIn('--project',str(c.exception))
        text,manifest=wa.outline_packet('demo','domain',notes[:3]);data=json.loads(text);self.assertEqual([n['key'] for n in data['notes']],['N1','N2','N3']);self.assertNotIn('B'*50,text)
        self.assertEqual(set(data['notes'][0]),{'key','title','type','tags','project','updated','files','lead','headings'});self.assertEqual(manifest['notes']['N1'],'1')
        # The date lets the outline see that a later note records a removal (AW-38).
        dated=json.loads(wa.outline_packet('demo','domain',[dict(notes[0],updated='2026-11-16')])[0]);self.assertEqual(dated['notes'][0]['updated'],'2026-11-16')
    def test_a_project_builds_its_own_edition(self):
        ids=self.sample();r=self.build(project='hive');self.assertEqual((r['scope'],Path(r['path']).name),('hive','project-hive.json'))
        used={i for p in self.edition('hive')['pages'].values() for i in p['plan']};self.assertTrue(used);self.assertNotIn(ids['general'],used);self.assertNotIn(ids['both'],used)
        m=wa.read_model(self.root,'demo','hive');self.assertEqual((m['scope'],m['projects'][0]['name']),('hive','Hive'))
        with self.assertRaises(ValueError):wa.read_model(self.root,'demo')   # the domain edition is separate
        for bad in ('nosuch','../x','domain'):
            with self.assertRaises(ValueError,msg=bad):wa.build(self.root,'demo',project=bad,executable=self.exe)
    def test_a_second_build_of_the_same_scope_is_refused_while_one_runs(self):
        self.sample();path=wa.edition_path(self.root,'demo','domain')
        with file_lock(path.parent/(path.name+'.lock'),timeout=0):
            with self.assertRaises(ValueError) as c:self.build()
        self.assertIn('another agent wiki build',str(c.exception));self.assertFalse(self.log.exists())
    def test_input_checks(self):
        self.sample()
        with self.assertRaises(ValueError):wa.build(self.root,'demo',model='Sonnet 4!',executable=self.exe)
        for budget in ('0','-1','abc','1e3'):
            with self.assertRaises(ValueError,msg=budget):wa.build(self.root,'demo',budget=budget,executable=self.exe)
        with patch.object(agent,'provider',return_value={'ok':False,'reason_code':'provider_unavailable'}),self.assertRaises(ValueError) as c:wa.build(self.root,'demo')
        self.assertIn('claude is not available',str(c.exception))
        from core.init_domain import init_domain
        init_domain('hollow',[str(self.base/'hollow')],self.root)
        with self.assertRaises(ValueError) as c:wa.build(self.root,'hollow',dry_run=True)
        self.assertIn('no active notes',str(c.exception))

class Model(Base):
    def test_the_model_matches_the_contract(self):
        ids=self.sample();self.build();m=wa.read_model(self.root,'demo')
        self.assertEqual(set(m),{'domain','generated','renderer','archived','edition','scope','build','notes','projects','folders','sections','external'})
        self.assertEqual((m['domain'],m['renderer'],m['archived'],m['edition'],m['scope'],m['folders']),('demo',wiki.WIKI_RENDERER,False,'agent','domain',[]))
        self.assertEqual(set(m['build']),{'built','model','cost_usd','pages','written','stale','pending','complete','omitted'})
        self.assertEqual((m['build']['pages'],m['build']['written'],m['build']['stale'],m['build']['pending'],m['build']['complete'],m['build']['model']),(2,2,0,0,True,'sonnet'))
        pages=[n for n in m['notes'] if n['type']=='article'];sources=[n for n in m['notes'] if n['section']=='sources']
        self.assertEqual(len(pages)+len(sources),len(m['notes']));self.assertEqual([n['id'] for n in m['notes']][:2],[n['id'] for n in pages])
        for n in pages:
            self.assertEqual(set(n),{'id','ref','title','type','status','retired','tags','files','updated','created','lead','body','out','backlinks','project','section','folder','filename','page'})
            self.assertEqual((n['ref'],n['status'],n['retired'],n['tags'],n['files'],n['project'],n['folder'],n['filename']),(n['id'],'active',False,[],[],'wiki',None,''));self.assertEqual(n['page']['cost_usd'],0.01)
            self.assertEqual(set(n['page']),{'purpose','written','stale','built','model','cost_usd','warnings','sources'});self.assertTrue(n['page']['written']);self.assertEqual(n['page']['model'],'sonnet')
            self.assertEqual(set(n['page']['sources'][0]),{'id','ref','title','type','current','missing'})
            cited={int(k) for k in PAGE_RE.findall(n['lead']+n['body'])};self.assertEqual(cited,set(range(1,len(n['page']['sources'])+1)))
            self.assertNotIn('Sources',n['body']);self.assertTrue(set(s['id'] for s in n['page']['sources'])<=set(n['out']))
        by_id={n['id']:n for n in m['notes']};cited_ids={s['id'] for n in pages for s in n['page']['sources']}
        self.assertEqual({n['id'] for n in sources},cited_ids)
        for n in sources:self.assertEqual((n['project'],n['section']),('wiki','sources'));self.assertEqual(n['ref'],n['id'][:8]);self.assertTrue(all(b in by_id for b in n['backlinks']))
        self.assertEqual(len(m['projects']),1);p=m['projects'][0]
        self.assertEqual((p['slug'],p['name'],p['description'],p['aliases'],p['repos'],p['status'],p['charter'],p['briefs'],p['maps'],p['count']),('wiki','demo','A wiki made for a test.',[],[],'active',None,[],[],2))
        self.assertEqual(p['sections'],{'a1':{'notes':[n['id'] for n in pages],'count':2}});self.assertEqual(m['sections'],[{'key':'a1','label':'Core','blurb':'The core pages.'}])
        links=[n for n in pages if n['page']['sources'] and '[[' in n['body']];self.assertTrue(links)   # page 1 links to page 2 by title and the other way round
        for n in links:
            target=re.search(r'\[\[([^\]]+)\]\]',n['body'])[1];other=[x for x in pages if x['id']!=n['id']][0];self.assertEqual(target,other['title']);self.assertIn(other['id'],n['out']);self.assertIn(n['id'],other['backlinks'])
    def test_an_unwritten_page_shows_its_purpose(self):
        for i in range(4):self.add(f'Note {i}',f'Body {i}.')
        self.build(budget='0.08',env={'FAKE_PAGES':'4','FAKE_COST':'0.03'});m=wa.read_model(self.root,'demo');idle=[n for n in m['notes'] if n['type']=='article' and not n['page']['written']]
        self.assertTrue(idle);n=idle[0];self.assertEqual((n['body'],n['lead'],n['page']['stale'],n['page']['built'],n['page']['model']),('',n['page']['purpose'],False,None,None));self.assertTrue(n['page']['sources'])
        self.assertTrue(all(s['current'] and not s['missing'] for s in n['page']['sources']))
    def test_style_warnings_are_stored_and_advisory(self):
        self.sample();self.build();ed=self.edition();pid=next(iter(ed['pages']))
        self.assertEqual(ed['pages'][pid]['warnings'],[])
        change(next(iter(ed['pages'][pid]['plan'])),'demo','update',self.root,body='Changed.')
        real=wa.validate_page
        def dashes(report,manifest,titles):
            out=real(report,manifest,titles);out['body']+=' A claim' + chr(0x2014) + 'with a dash, and a seamless fix.[^1]';return out
        with patch.object(wa,'validate_page',dashes):self.build()
        self.assertTrue(any('em or en dash' in w for p in self.edition()['pages'].values() for w in p['warnings']))
    def test_title_case_headings_are_warned_about_not_rewritten(self):   # AW-37
        self.sample();self.build();ed=self.edition();pid=next(iter(ed['pages']))
        change(next(iter(ed['pages'][pid]['plan'])),'demo','update',self.root,body='Changed again.')
        real=wa.validate_page
        def titled(report,manifest,titles):
            out=real(report,manifest,titles);out['body']+='\n\n## Settings And Watcher Flow Details\n\nText.[^1]\n\n## How the API uses tokens\n\nMore.[^1]';return out
        with patch.object(wa,'validate_page',titled):self.build()
        def warned():
            return [w for n in wa.read_model(self.root,'demo')['notes'] if n['type']=='article' for w in n['page']['warnings'] if 'sentence case' in w]
        self.assertEqual(warned(),['Title Case heading ("Settings And Watcher Flow Details"); write titles and headings in sentence case'])
        self.assertTrue(any('## Settings And Watcher Flow Details' in p['body'] for p in self.edition()['pages'].values()), 'the text is kept as written')
        # Worked out on read, so a fixed heading, or a warning an older build saved, leaves nothing behind (Codex re-review F8).
        ed=self.edition();page=next(p for p in ed['pages'].values() if '## Settings And' in p['body'])
        page['body']=page['body'].replace('Settings And Watcher Flow Details','Settings and watcher flow');page['warnings'].append(wa.case_warning('Old Saved Title Case Heading'))
        wa.edition_path(self.root,'demo','domain').write_text(json.dumps(ed),encoding='utf-8')
        self.assertEqual(warned(),[])
    def test_an_edition_saved_for_another_scope_is_not_this_scopes_edition(self):   # AW-23
        self.sample();self.build(project='hive');hive=wa.edition_path(self.root,'demo','hive');domain=wa.edition_path(self.root,'demo','domain');domain.write_bytes(hive.read_bytes())
        with self.assertRaises(ValueError) as c:wa.read_model(self.root,'demo')
        self.assertIn('no agent wiki for demo yet',str(c.exception));calls=len(self.calls())
        for kw in ({},{'dry_run':True}):
            with self.assertRaises(ValueError,msg=kw) as c:self.build(**kw)
            self.assertIn(str(domain),str(c.exception));self.assertIn("'hive', not 'domain'",str(c.exception))
        self.assertEqual(len(self.calls()),calls);self.assertEqual(domain.read_bytes(),hive.read_bytes());self.assertEqual(wa.read_model(self.root,'demo','hive')['scope'],'hive')
    def test_no_edition_yet_is_an_error_that_says_how_to_build(self):
        self.sample();code,err=self.cli_json('wiki','--agent','--format','json');self.assertNotEqual(code,0);self.assertIn('no agent wiki for demo yet; run wiki --agent --build',err['error']['message'])
        code,err=self.cli_json('wiki','--agent','--project','hive');self.assertNotEqual(code,0);self.assertIn('no agent wiki for hive yet',err['error']['message'])

class Page(Base):
    def html(self,**kw):
        return wiki.render_html(wa.read_model(self.root,'demo'),**kw)
    def test_notes_are_escaped_and_the_page_has_no_network_references(self):
        self.sample();self.add('Evil '+tw.EVIL,'Body '+tw.EVIL,['project/hive']);self.build(env={'FAKE_EVIL':'1'});page=self.html()
        self.assertEqual(page.count('<script'),2);self.assertEqual(page.count('</script>'),2)
        for raw in ('<img','onerror=alert(1)>'):self.assertNotIn(raw,page)
        data=json.loads(re.search(r'<script type="application/json" id="wiki-data">(.*?)</script>',page,re.S)[1]);self.assertEqual(data['edition'],'agent')
        self.assertTrue(any(tw.EVIL in n['title'] for n in data['notes']));self.assertEqual(page.count('.innerHTML='),1)
        self.assertFalse(re.findall(r'(?:src|href|action|data)\s*=\s*["\']?\s*(?:https?:)?//',page,re.I));self.assertFalse(re.findall(r'url\(\s*(?!["\']?\s*data:)',page,re.I))
        for word in ('fetch(','XMLHttpRequest','@import','<link','<img','WebSocket','sendBeacon'):self.assertNotIn(word,page)
    def test_the_page_renders_citations_sources_and_the_stale_flag(self):
        self.sample();self.build();page=self.html();js=wiki.JS
        for needle in ("D.edition==='agent'","function cite(k)","INLINE=/(`[^`]+`|\\*\\*[^*]+\\*\\*|\\*[^*\\s][^*]*\\*|\\[\\[[^\\]]+\\]\\]|\\[\\^\\d+\\]|","function sourcesBlock(n)","'Planned sources'","archived or deleted","changed since","out of date","S.sources="):
            self.assertIn(needle,js)
        self.assertIn('.type-article{color:var(--accent)}',page);self.assertIn('Demo agent wiki</title>',page);self.assertIn('agent wiki</span>',page);self.assertNotIn('[^1]</li>',page)
        self.assertEqual(wiki.render_html(wa.read_model(self.root,'demo'),document=False),self.html(document=False));self.assertIn('<noscript><ul><li>Page 1',page)
    @unittest.skipUnless(__import__('shutil').which('node'),'needs node')
    def test_the_script_parses(self):
        self.sample();self.build();page=self.html(theme='purple');script=re.findall(r'<script>(.*?)</script>',page,re.S)[0]
        probe=self.base/'page.js';probe.write_text(script,encoding='utf-8')
        done=subprocess.run(['node','--check',str(probe)],capture_output=True,text=True,timeout=30);self.assertEqual(done.returncode,0,done.stderr)
    @unittest.skipUnless(__import__('shutil').which('node'),'needs node')
    def test_citations_render_as_superscript_links_in_a_stub_dom(self):
        # Run the page's inline() and cite() against a tiny DOM: [^2] on a page with two sources is a link to the second source's page.
        js=wiki.JS;start=js.index('var INLINE=');end=js.index('function markdown(')
        stub=("var N={a:{ref:'aaaa1111'},b:{ref:'bbbb2222'}},CITE={page:{sources:[{id:'a',title:'A',current:true,missing:false},{id:'b',title:'B',current:false,missing:false},{id:'c',title:'C',current:true,missing:true}]}},AG=true;"
              "function mk(t){var e={tag:t,kids:[],attrs:{},appendChild:function(k){this.kids.push(k);return k},setAttribute:function(k,v){this.attrs[k]=v}};return e}"
              "var document={createTextNode:function(t){return {text:t}},createElement:mk};"
              "function el(t,c,x){var e=mk(t);e.className=c;if(x!=null)e.textContent=x;return e}function link(h,c,x){var a=el('a',c,x);a.href=h;return a}"
              "function nh(n){return '#n-'+n.ref}function wikilink(r){return {text:r}}")
        run=stub+js[start:end]+"var p=mk('p');inline(p,'Fact.[^1] More[^2] gone[^3] none[^9] `x`');console.log(JSON.stringify(p.kids.map(function(k){var c=k.kids&&k.kids[0]||{};return k.tag==='sup'?[k.tag,k.className,c.tag,c.href||'',c.title||'']:k.tag?[k.tag,k.className,k.textContent]:k.text})))"
        done=subprocess.run(['node','-e',run],capture_output=True,text=True,timeout=30);self.assertEqual(done.returncode,0,done.stderr)
        got=json.loads(done.stdout)
        self.assertEqual(got[0],'Fact.');self.assertEqual(got[1],['sup','cite','a','#n-aaaa1111','A']);self.assertEqual(got[2],' More')
        self.assertEqual(got[3],['sup','cite','a','#n-bbbb2222','B (changed since this page was written)']);self.assertEqual(got[5][3:],['','C (archived or deleted)']);self.assertEqual(got[7],'[^9]')
    @unittest.skipUnless(__import__('shutil').which('node'),'needs node')
    def test_code_nested_under_a_list_item_is_code_for_the_validator_and_the_renderer(self):   # AW-25
        body='- Item.[N1]\n  ```\n  x[N1] [[P1]] [^1]\n  ```\n- Next.[N1]\n'
        out=wa.validate_page({'lead':'Lead.[N1]','body':body,'cited':['N1']},{'notes':{'N1':'a'},'pages':{'P1':'pg1'}},{'P1':'Other'})
        self.assertEqual(out['body'],'- Item.[^1]\n  ```\n  x[N1] [[P1]] [^1]\n  ```\n- Next.[^1]')
        ul=self.render(out['body'])[2][0];self.assertEqual(ul[0],'ul');first,second=ul[2]
        # One citation link, outside the code; the code loses the list's indentation (AW-36).
        self.assertEqual([k[0] for k in first[2]].count('sup'),1);pre=[k for k in first[2] if k[0]=='pre'];self.assertEqual(len(pre),1);self.assertEqual(pre[0][2][0][1],'x[N1] [[P1]] [^1]')

    def render(self,body):
        """markdown() from the page's script run under node on `body`, as a [tag, text, kids] tree."""
        if not __import__('shutil').which('node'):self.skipTest('needs node')
        js=wiki.JS;start=js.index('var INLINE=');end=js.index('// ---- building blocks')
        stub=("var N={},CITE={page:{sources:[{id:'a',title:'A',current:true,missing:false}]}},AG=true;"
              "function mk(t){var e={tag:t,kids:[],lastChild:null,appendChild:function(k){this.kids.push(k);this.lastChild=k;return k},setAttribute:function(){},querySelector:function(){return null}};return e}"
              "var document={createTextNode:function(t){return {text:t}},createElement:mk,createDocumentFragment:function(){return mk('frag')}};"
              "function el(t,c,x){var e=mk(t);e.className=c;if(x!=null)e.textContent=x;return e}function link(h,c,x){return el('a',c,x)}function nh(n){return '#'}")
        run=stub+js[start:end]+("var f=markdown("+json.dumps(body)+");"
              "function show(n){return n.tag?[n.tag,n.textContent||null,n.kids.map(show)]:n.text}console.log(JSON.stringify(show(f)))")
        done=subprocess.run(['node','-e',run],capture_output=True,text=True,timeout=30);self.assertEqual(done.returncode,0,done.stderr)
        return json.loads(done.stdout)

    def test_a_longer_fence_holds_a_shorter_one_for_the_validator_and_the_renderer(self):   # AW-35
        body='Intro.[N1]\n\n- Item\n  ````md\n  ```python\n  x[N9]\n  ```\n  ````\n\nAfter.[N1]\n'
        out=wa.validate_page({'lead':'Lead.[N1]','body':body,'cited':['N1']},{'notes':{'N1':'a'},'pages':{}},{})
        self.assertIn('x[N9]',out['body']);self.assertTrue(out['body'].endswith('After.[^1]'))
        frag=self.render(out['body']);tags=[k[0] for k in frag[2]]
        self.assertEqual(tags,['p','ul','p'],frag)   # the prose after the real closer is prose again
        pre=[k for k in frag[2][1][2][0][2] if k[0]=='pre'];self.assertEqual(pre[0][2][0][1],'```python\nx[N9]\n```')
        # A closer needs only backticks on its line, and a run at least as long as the opener.
        self.assertEqual([k[0] for k in self.render('````\na\n```\n`````  \nb')[2]],['pre','p'])

    def test_a_fence_in_a_quote_is_code_for_the_validator_and_the_renderer(self):   # AW-34
        body='> Quoted.[N1]\n> ```\n> x[N1] [N9]\n> ```\n\nAfter.[N1]\n'
        out=wa.validate_page({'lead':'Lead.[N1]','body':body,'cited':['N1']},{'notes':{'N1':'a'},'pages':{}},{})
        self.assertIn('> x[N1] [N9]',out['body']);self.assertIn('> Quoted.[^1]',out['body'])
        quote=self.render(out['body'])[2][0];self.assertEqual(quote[0],'blockquote')
        self.assertEqual([k[0] for k in quote[2][0][2]],['p','pre']);self.assertEqual(quote[2][0][2][1][2][0][1],'x[N1] [N9]')
        # An unclosed fence in a quote ends with the quote.
        out=wa.validate_page({'lead':'Lead.[N1]','body':'> ```\n> x[N9]\n\nAfter.[N1]','cited':['N1']},{'notes':{'N1':'a'},'pages':{}},{})
        self.assertTrue(out['body'].endswith('After.[^1]'))

    def test_a_fence_belongs_to_the_item_its_indentation_reaches(self):   # AW-36
        frag=self.render('- Outer\n  - Inner\n  ```py\n  print(1)\n  ```\n- Next\n')
        outer=frag[2][0][2][0];self.assertEqual(outer[0],'li')
        kids=[k for k in outer[2] if isinstance(k,list)]
        self.assertEqual([k[0] for k in kids],['ul','pre'],outer);self.assertEqual(kids[1][2][0][1],'print(1)')
        inner=kids[0][2][0];self.assertFalse([k for k in inner[2] if isinstance(k,list)])
        # Deeper indentation goes to the inner item.
        outer=self.render('- Outer\n  - Inner\n    ```\n    x\n    ```\n')[2][0][2][0]
        inner=[k for k in outer[2] if isinstance(k,list)][0][2][0]
        self.assertEqual([k for k in inner[2] if isinstance(k,list) and k[0]=='pre'][0][2][0][1],'x')

    def check(self,body,lead='Lead.[N1]'):
        return wa.validate_page({'lead':lead,'body':body,'cited':['N1']},{'notes':{'N1':'a'},'pages':{}},{})

    def test_the_check_reads_line_breaks_and_spaces_as_the_page_does(self):   # Codex R1 on 680f5f6
        out=self.check('```\r\nx[N1]\r\n```\r\nAfter.[N1]')   # a CRLF closer still closes, so the prose after it cites
        self.assertEqual(out['body'],'```\nx[N1]\n```\nAfter.[^1]')
        out=self.check(' ```\nx[N1]\n```\nAfter.[N1]')   # a no-break space before the opener is whitespace to both
        self.assertIn('x[N1]',out['body']);self.assertEqual([k[0] for k in self.render(out['body'])[2]],['pre','p'])

    def test_a_quote_inside_a_list_item_is_a_quote_to_both(self):   # Codex R2 on 680f5f6
        out=self.check('- Item.[N1]\n  > ```\n  > x[N1]\n  > ```\n')
        self.assertIn('> x[N1]',out['body'])
        frag=self.render(out['body']);self.assertEqual([k[0] for k in frag[2]],['ul'],frag)
        li=frag[2][0][2][0];bq=[k for k in li[2] if isinstance(k,list) and k[0]=='blockquote']
        self.assertEqual([k[0] for k in bq[0][2][0][2]],['pre'])
        # The quote stays in its item, so the list goes on after it (Codex re-review F2).
        frag=self.render('1. First\n   > Quote\n2. Second');self.assertEqual([k[0] for k in frag[2]],['ol'])
        self.assertEqual([k[0] for k in frag[2][0][2]],['li','li'])

    def test_the_check_and_the_page_share_one_whitespace_set(self):   # Codex re-review F1
        # U+0085 is whitespace to Python but not to JavaScript, so the page's block does not close; U+FEFF is the reverse.
        self.assertIn('After.[N1]',self.check('Text.[N1]\n```\nx\n```\u0085\nAfter.[N1]')['body'])
        self.assertIn('After.[^1]',self.check('Text.[N1]\n```\nx\n```﻿\nAfter.[N1]')['body'])

    def test_a_lead_code_span_may_cross_a_line(self):   # Codex R3 on 680f5f6
        out=self.check('Body.[N1]',lead='Run `x\ny[N1]` first.[N1]')
        self.assertEqual(out['lead'],'Run `x\ny[N1]` first.[^1]')

    def test_odd_lists_end_and_keep_code_columns(self):   # Codex R4, R5, R6 on 680f5f6
        self.assertEqual(self.render('- one two')[2][0][0],'ul')   # it used to loop forever
        pre=self.render('- Item\n  ```\n  if True:\n\tpass\n  ```')[2][0][2][0][2][1]
        self.assertEqual(pre[2][0][1],'if True:\n  pass')   # a tab past the item's column keeps the rest of its width
        frag=self.render('10. Item\n  ```\n  x\n  ```')   # the fence does not reach the item's text column
        self.assertEqual([k[0] for k in frag[2]],['ol','pre']);self.assertEqual(frag[2][1][2][0][1],'x')

    def test_a_code_span_ends_at_its_table_cell(self):   # AW-39
        with self.assertRaisesRegex(wa.WikiError,'footnotes of its own'):self.check('| a `x | [^1] y` |\n|---|---|\nText.[N1]')
        out=self.check('| a `x | y [N1] z` |\n|---|---|')
        self.assertEqual(out['body'],'| a `x | y [^1] z` |\n|---|---|')
        cell=self.render(out['body'])[2][0][2][0][2][0][2][0][2][1]   # div > table > thead > tr > second th
        self.assertEqual([k[0] if isinstance(k,list) else k for k in cell[2]],['y ','sup',' z`'])

    def test_a_backtick_inside_a_link_or_bold_text_opens_no_code(self):   # AW-41
        for body in ('[a`b](https://x) [^1] c`','**a `x** [^1] y`'):
            with self.assertRaisesRegex(wa.WikiError,'footnotes of its own'):self.check(body+'\nText.[N1]')
        self.assertIn('`[^1]`',self.check('**a `[^1]` b** [N1]')['body'])   # a code span inside bold text is still code

    def test_tabs_move_to_the_next_tab_stop(self):   # AW-42
        pre=self.render('- Item\n  ```\n \tx\n  ```')[2][0][2][0][2][1]
        self.assertEqual(pre[2][0][1],'  x')   # the tab after one space ends at column 4; two columns come off
        frag=self.render('-\tItem\n    ```\n    x\n    ```')   # the item's text starts at column 4, so the fence is its own
        self.assertEqual([k[0] for k in frag[2]],['ul']);self.assertEqual([k[0] for k in frag[2][0][2][0][2] if isinstance(k,list)],['pre'])

    def test_quotes_nested_too_deep_are_refused(self):
        with self.assertRaisesRegex(wa.WikiError,'nests quotes'):self.check('>'*(wa.QUOTE_DEPTH_MAX+2)+' Deep.[N1]')
        # A style warning never stops a wiki from opening (Codex review of 868c987).
        deep='>'*(wa.QUOTE_DEPTH_MAX+2)
        self.assertEqual(wa.case_warnings('Title',deep+' x\n## '+deep+' plain heading'),[]);self.assertFalse(wa.title_case(deep))

    def test_review_fixes_for_tables_links_and_long_spaces(self):   # Codex review of 868c987
        # The separator row is never drawn, so a marker in it is not a citation.
        with self.assertRaisesRegex(wa.WikiError,'cites no source'):self.check('| a |\n|---|`[N1]`|',lead='Lead.')
        # A [N1] link label that becomes [^1] would let a URL backtick pair with a later one and show a raw [^1].
        with self.assertRaisesRegex(wa.WikiError,'numbering the citations'):self.check('[N1](https://x`y) and `z [^1]` w')
        import time
        for body in ('a'+' '*19500+'b [N1]','| x |\n'+' '*19500+'y [N1]'):
            began=time.perf_counter();self.check(body);self.assertLess(time.perf_counter()-began,2,body[:8])

    @unittest.skipUnless(__import__('shutil').which('node'),'needs node')
    def test_tabs_left_after_dedent_keep_their_columns(self):   # Codex review of 868c987
        pre=self.render(' \t```\n    \tx\n        y\n```')[2][0]
        self.assertEqual(pre[2][0][1],'    x\n    y')
        self.assertEqual(self.render('```make\nall:\n\tmake x\n```')[2][0][2][0][1],'all:\n\tmake x')   # Codex re-review: a recipe keeps its tab

    def test_numbering_must_leave_the_citations_made_where_they_were_made(self):   # Codex re-review of b020798
        # Joining "[N1,\nN2]" pulls a table into the list, so two hidden [^9] show while both new citations land in code.
        body='- `x [N1,\nN2] y`\n | H |\n |---|[^9][^9]|\n\nText.[N1]'
        with self.assertRaisesRegex(wa.WikiError,'numbering the citations'):
            wa.validate_page({'lead':'Lead.[N1]','body':body,'cited':['N1','N2']},{'notes':{'N1':'a','N2':'b'},'pages':{}},{})
        out=wa.validate_page({'lead':'Lead.[N1]','body':'A [N1, N2] and [[P1]] `[N2]`.','cited':['N1','N2']},{'notes':{'N1':'a','N2':'b'},'pages':{}},{'P1':'Other'})
        self.assertEqual(out['body'],'A [^1][^2] and [[Other]] `[N2]`.')

    @unittest.skipUnless(__import__('shutil').which('node'),'needs node')
    def test_the_check_and_the_page_agree_on_what_is_code(self):
        def drawn(node,inside_pre=False):
            if not isinstance(node,list):return []
            tag,text,kids=node
            if tag=='code' and not inside_pre:return [text]
            return [c for k in kids for c in drawn(k,tag=='pre')]
        for body in ('| a `x | y` |\n|---|---|\n| `p | q` z |','- item\n  | `a | b` |\n  |---|','> | `a | b` |\n> |---|\n> | c `d` |',
                     '**a `x** y`','[a`b](https://x) c `d`','- [ ] `t` x','## `h` x','1. a\n\n   `b` c\n\n- d `e`','Text `a\nb` c'):
            spans=wa._code_spans(body);inline=[body[s+1:e-1] for s,e in spans if body[s]=='`' and '\n' not in body[s:e] and not wa._OPEN.match(body[s:e])]
            self.assertEqual(inline,drawn(self.render(body)),body)

    def test_a_theme_picker_serves_both_editions(self):
        self.sample();self.build();agent_page=self.html();plain=wiki.render_html(wiki.wiki_model('demo',self.root))
        for page in (agent_page,plain):
            for key,label in (('purple','Purple'),('emerald','Emerald Dark'),('emerald-light','Emerald Light')):self.assertIn(f'data-t="{key}" aria-label="{label}"',page)
            self.assertNotIn('id="theme"',page);self.assertIn('START=null',page);self.assertNotIn('data-hm=',page.split('<head>')[0])
        for key in ('purple','emerald','emerald-light'):
            page=self.html(theme=key);self.assertIn(f'<html lang="en" data-hm="{key}">',page);self.assertIn(f'START="{key}"',page)
            self.assertIn(f'<html lang="en" data-hm="{key}">',wiki.render_html(wiki.wiki_model('demo',self.root),theme=key))
        self.assertNotIn('data-hm=',self.html(document=False,theme='purple').split('<style>')[0]);self.assertIn('START="purple"',self.html(document=False,theme='purple'))
        for bad in ('blue','Purple','dark',''):
            with self.assertRaises(ValueError):wiki.render_html(wiki.wiki_model('demo',self.root),theme=bad)
    def test_the_three_theme_token_sets_carry_the_same_keys(self):
        names=lambda block:sorted(re.findall(r'(--[a-z0-9-]+):',block))
        self.assertEqual(names(wiki.PURPLE),names(wiki.DARK));self.assertEqual(names(wiki.PURPLE),names(wiki.LIGHT));self.assertIn('--bg:#1b0626;',wiki.PURPLE);self.assertIn('--selected:#01dca4;',wiki.PURPLE)
        page=wiki.render_html(wiki.wiki_model('demo',self.root))
        self.assertIn(':root[data-hm="purple"],:root[data-hm="purple"] .sidebar{'+wiki.PURPLE,page);self.assertIn(':root[data-hm="emerald"]{'+wiki.DARK,page)
        self.assertIn(':root[data-hm="emerald-light"]{'+wiki.LIGHT,page);self.assertIn(':root[data-hm="emerald-light"] .sidebar{'+wiki.DARK+wiki.SIDEBAR_LIGHT,page)
        css=page[page.index('<style>'):];self.assertLess(css.index(':root[data-theme="dark"]{'),css.index(':root[data-hm="emerald"]{'))   # a picked theme comes last, so it wins
        used=set(re.findall(r'var\((--[a-z0-9-]+)',wiki.CSS))-{'--font-sans','--font-mono'};self.assertFalse(used-set(names(wiki.PURPLE)))
    def test_the_theme_follows_a_saved_choice_unless_one_is_given(self):
        js=wiki.JS;self.assertIn("localStorage.setItem('wiki-theme',b.dataset.t)",js);self.assertIn("if(START)root.setAttribute('data-hm',START)",js)
        self.assertLess(js.index("localStorage.getItem('wiki-theme')"),js.index("if(START)root.setAttribute"));self.assertIn("host==='dark'?'emerald':'emerald-light'",js)
        self.assertIn("matchMedia('(prefers-color-scheme: dark)').matches?'emerald':'emerald-light'",js)

class Command(Base):
    def test_build_and_read_through_the_command(self):
        self.sample();code,r=self.cli_json('wiki','--agent','--build','--dry-run');self.assertEqual(code,0);self.assertEqual((r['dry_run'],r['calls']['outline'],r['calls']['pages']),(True,1,None))
        self.assertFalse(self.log.exists())
        code,out,err=self.run_cli('wiki','--agent','--build','--progress','--budget','1.50','--model','haiku');self.assertEqual(code,0,err);r=json.loads(out)
        self.assertEqual((r['ok'],r['model'],r['budget_usd'],r['calls']['pages']),(True,'haiku',1.5,2));events=[json.loads(l) for l in err.splitlines() if l.startswith('{')]
        self.assertEqual([e['event'] for e in events],['start','call','call','call','end']);self.assertEqual({c['model'] for c in self.calls()},{'haiku'})
        model=self.ok('wiki','--agent','--format','json')['model'];self.assertEqual((model['edition'],model['build']['pages']),('agent',2))
        self.assertEqual(self.ok('wiki','--agent','--format','json',actor='viewer')['model']['edition'],'agent');self.ok('wiki','--agent','--format','json',actor='subagent')
        r=self.ok('wiki','--agent');self.assertEqual((Path(r['path']),r['pages'],r['format']),((self.root/'.views'/'demo'/'agent-wiki.html').resolve(),2,'html'))
        page=Path(r['path']).read_text(encoding='utf-8');self.assertIn('"edition":"agent"',page.replace(' ',''));self.assertIn('<html lang="en">',page)
        r=self.ok('wiki','--agent','--theme','emerald-light','--out',str(self.base/'out'/'a.html'));self.assertIn('<html lang="en" data-hm="emerald-light">',Path(r['path']).read_text(encoding='utf-8'))
        r=self.ok('wiki','--agent','--format','json','--out',str(self.base/'out'/'a.json'));self.assertEqual(json.loads(Path(r['path']).read_text(encoding='utf-8'))['edition'],'agent')
        self.assertEqual(self.ok('wiki','--theme','purple')['format'],'html');self.assertIn('data-hm="purple"',(self.root/'.views'/'demo'/'wiki.html').read_text(encoding='utf-8'))
        self.add('Fresh note','New.',['project/hive']);code,out,err=self.run_cli('wiki','--agent','--build','--project','hive');self.assertEqual(code,0,err)
        self.assertEqual(self.ok('wiki','--agent','--project','hive')['path'].endswith('agent-wiki-hive.html'),True)
    def test_an_empty_budget_is_refused_not_replaced_by_the_default(self):   # AW-4
        self.sample()
        for extra in ((),('--dry-run',)):
            code,err=self.cli_json('wiki','--agent','--build','--budget','',*extra);self.assertNotEqual(code,0,extra);self.assertIn('Invalid --budget',err['error']['message'])
        self.assertFalse(self.log.exists());code,r=self.cli_json('wiki','--agent','--build','--dry-run');self.assertEqual((code,r['budget_usd']),(0,5.0))   # absent: the default
    def test_the_hints_name_the_project_when_the_scope_is_one(self):   # AW-10
        self.sample()
        for argv,text in ((('wiki','--agent'),'run wiki --agent --build'),(('wiki','--agent','--project','hive'),'run wiki --agent --build --project hive')):
            code,err=self.cli_json(*argv,'--format','json');self.assertNotEqual(code,0);self.assertTrue(err['error']['message'].startswith('no agent wiki for '),err);self.assertTrue(err['error']['message'].endswith(text),err)
        js=wiki.JS;self.assertNotIn('Run alm wiki --agent --build',js);self.assertEqual(js.count('buildCmd()'),4)
        if __import__('shutil').which('node'):
            start=js.index('function buildCmd()');end=js.index('\n',start)
            for scope,want in (('domain','alm wiki --agent --build'),('hive','alm wiki --agent --build --project hive')):
                done=subprocess.run(['node','-e','var D={scope:'+json.dumps(scope)+'};'+js[start:end]+';console.log(buildCmd())'],capture_output=True,text=True,timeout=30)
                self.assertEqual((done.returncode,done.stdout.strip()),(0,want),done.stderr)
    def test_a_stopped_build_exits_nonzero_with_its_result(self):
        self.sample();code,out,err=self.run_cli('wiki','--agent','--build');self.assertEqual(code,0)
        self.add('Fresh note','New.');code,out,err=self.run_cli('wiki','--agent','--build');self.assertEqual(code,0)
        with patch.dict(os.environ,{'FAKE_FAIL':'place'}):self.add('Another note','New.');code,out,err=self.run_cli('wiki','--agent','--build')
        r=json.loads(out);self.assertEqual((code,r['ok'],r['stopped']),(1,False,'error'))
    def test_guards(self):
        self.sample()
        for actor,word in (('subagent','Subagents'),('viewer','Viewers')):
            for extra in ((),('--format','json'),('--dry-run',)):
                code,err=self.cli_json('wiki','--agent','--build',*extra,actor=actor);self.assertNotEqual(code,0);self.assertIn(word+' cannot build',err['error']['message'])
        for name,text in (('HIVEMIND_SESSION','hivemind'),('LIVING_MEMORY_MAINTENANCE_CHILD','inside a model call')):
            with patch.dict(os.environ,{name:'1'}):
                code,err=self.cli_json('wiki','--agent','--build');self.assertNotEqual(code,0);self.assertIn(text,err['error']['message'])
                code,err=self.cli_json('wiki','--agent','--build','--dry-run');self.assertNotEqual(code,0)   # even a dry run: the guard is about who may start it
            self.assertFalse(self.log.exists())
        for argv,text in ((('wiki','--build'),'--build needs --agent'),(('wiki','--project','hive'),'--project needs --agent'),(('wiki','--agent','--restructure'),'--restructure only applies'),
                          (('wiki','--agent','--budget','1'),'--budget only applies'),(('wiki','--agent','--model','sonnet','--dry-run'),'--model, --dry-run only apply'),(('wiki','--agent','--progress'),'--progress only applies')):
            code,err=self.cli_json(*argv);self.assertNotEqual(code,0,argv);self.assertIn(text,err['error']['message'])
        for argv in (('wiki','--agent','--build','--budget','0'),('wiki','--agent','--build','--budget','x'),('wiki','--agent','--build','--model','No Way!'),('wiki','--agent','--build','--project','nosuch'),
                     ('wiki','--agent','--theme','blue'),('wiki','--theme','Purple')):
            code,err=self.cli_json(*argv);self.assertNotEqual(code,0,argv)
        self.assertFalse(self.log.exists())
    def test_version_reports_the_capability(self):
        r=self.ok('version');self.assertEqual(r['capabilities']['wiki_agent'],{'version':1});self.assertIn('wiki',r['commands']);self.assertEqual(r['capabilities']['maintenance']['version'],1)
    def test_hook_rules_for_a_build(self):
        base=f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo wiki'
        for extra,writes in ((' --agent --build',True),(' --agent --build --format json',True),(' --agent --build --dry-run --format json',True),(' --agent --bui',True),(' --agent --build=1',True),
                             (' --agent --format json',False),(' --agent --format json --project hive',False),(' --agent --format json --out x.json',True),(' --agent',True),(' --agent --theme purple',True)):
            self.assertEqual(runtime.wiki_write(base+extra,'posix'),writes,extra);self.assertEqual(runtime.wiki_build(base+extra,'posix'),'--bui' in extra)
            payload={'cwd':str(self.base/'project'),'session_id':'h','tool_name':'Bash','tool_input':{'command':base+extra}}
            result=runtime.process('claude','pre-tool-use',payload,self.root);sub=runtime.process('claude','pre-tool-use',{**payload,'agent_id':'sub-1'},self.root)
            self.assertEqual('deny' in sub,writes,extra)
            if '--bui' in extra:self.assertNotIn('allow',result,extra);self.assertFalse(runtime.auto_allowed(base+extra,'posix',self.root,self.base/'project'),extra)   # left to the harness's own prompt
        self.assertTrue(runtime.auto_allowed(base+' --agent --format json','posix',self.root,self.base/'project'))
        self.assertTrue(runtime.auto_allowed(base,'posix',self.root,self.base/'project'))

class Docs(unittest.TestCase):
    def test_maintenance_keeps_its_policy_and_limits(self):
        self.assertEqual(agent.STDOUT_MAX,64*1024);import inspect
        self.assertEqual(inspect.signature(agent._spawn).parameters['stdout_max'].default,agent.STDOUT_MAX)
        argv=agent.build_argv(['claude'],{'model':'sonnet'},{'budget_passed_micro':250000});self.assertEqual((argv[argv.index('--system-prompt')+1],argv[argv.index('--json-schema')+1]),(agent.POLICY,agent.SCHEMA_JSON))
        argv=agent.build_argv(['claude'],{'model':'haiku'},{'budget_passed_micro':1500000},'P','{"type":"object"}');self.assertEqual((argv[argv.index('--system-prompt')+1],argv[argv.index('--json-schema')+1],argv[-1]),('P','{"type":"object"}','1.50'))

if __name__=='__main__':unittest.main()
