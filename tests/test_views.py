"""Note views: the item format, the view model, the page, refresh on reconcile, the registry, the commands and the artifact hint."""
import contextlib,io,json,re,unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core.cli import main
from core import backup,installer_lifecycle,runtime,storage,views
from core.notes import find_note
from core.repository import reconcile
from core.runtime import protected_rule

ACTIONS=['[P2] [ALM] HOOK-4 Cap the total context below the limit','[P1] [ALM] [approved] CUR-1 Fix `cursor` parsing','[P3] [HM] UX-Q2 Polish spacing',
         '[ALM, HM] [next] DASH-1 Build the page','No tags, no id','[P2] [ALM] MAP-REKEY Rekey after a move']
DETAILS=['**HOOK-4:** the prompt path in `core/runtime.py` builds the context.\n  A second line of the same detail.','**DASH-1:** render the model:\n  - bullet one\n  - bullet two','A loose bullet that belongs to no item.']

def body(*,actions=ACTIONS,details=DETAILS):
    from core.note_format import render
    return render('Close the open gaps before release.',details,['[[Other note]]'],actions,('[P3] [ALM] PRIV-1 Close the gaps (PR #38)',),'Mind the order.','')

def fake(text=None,**kw):
    return {'id':'3a562a09-2b34-4a43-8901-c7a1801168e6','domain':'demo','title':'Hook plan','type':'plan','status':'active','updated':'2026-11-13T10:00:00+00:00','revision':'abc','body':text if text is not None else body(),**kw}

class Fixture(StoreFixture):
    def setUp(self):
        super().setUp();(self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','concern/architecture','concern/workflow']}))
    def cli(self,*argv,actor='main',domain='demo'):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            try:code=main(['--root',str(self.root),'--actor',actor,*(['--domain',domain] if domain else []),*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,(json.loads(text) if text.strip()[:1] in '{[' else text)
    def ok(self,*argv,**kw):
        code,result=self.cli(*argv,**kw);self.assertEqual(code,0,result);return result
    def plan_note(self,title='Hook plan',actions=ACTIONS):
        args=['capture','--type','plan','--title',title,'--subject','project','--concern','workflow','--summary','Close the open gaps before release.']
        for a in actions:args+=['--action',a]
        for d in DETAILS:args+=['--detail',d]
        return self.ok(*args)['id']
    def note_of(self,note_id):
        path,note=find_note(note_id,'demo',self.root);return dict(note,domain='demo')
    def add(self,note_id,title='Hook plan'):return self.ok('views','add',note_id,'--title',title)
    def html(self,note_id):
        row=self.ok('views')['views'][0];self.assertEqual(row['note'],note_id);return Path(row['path']).read_text(encoding='utf-8')

class Parsing(unittest.TestCase):
    def test_tags_and_ids(self):
        self.assertEqual(views.parse_item('[P2] [ALM] CUR-1 Title text'),([['P2'],['ALM']],'CUR-1','Title text'))
        self.assertEqual(views.parse_item('[ALM, HM] [approved] DASH-1 Title'),([['ALM','HM'],['approved']],'DASH-1','Title'))
        self.assertEqual(views.parse_item('HM-P4 Retry'),([],'HM-P4','Retry'))
        self.assertEqual(views.parse_item('MAP-REKEY: Rekey'),([],'MAP-REKEY','Rekey'))
        self.assertEqual(views.parse_item('UX-Q2')[1],'UX-Q2')

    def test_what_is_not_an_id_or_a_tag(self):
        self.assertEqual(views.parse_item('API gateway work'),([],None,'API gateway work'))      # capitals alone
        self.assertEqual(views.parse_item('[P2] fix the thing'),([['P2']],None,'fix the thing'))
        self.assertEqual(views.parse_item('[[Some Note]] needs work'),([],None,'[[Some Note]] needs work'))
        self.assertEqual(views.parse_item('[a link](http://x) first'),([],None,'[a link](http://x) first'))
        self.assertEqual(views.parse_item('[] [P1] A-1 x'),([['P1']],'A-1','x'))

class Model(Fixture):
    def test_items_details_and_sections(self):
        note=fake();model=views.view_model(note)
        self.assertEqual((model['counts'],model['status'],model['summary'],model['important']),({'open':6,'done':1,'total':7},'active','Close the open gaps before release.','Mind the order.'))
        item=model['items'][0];self.assertEqual((item['index'],item['done'],item['id'],item['tags'],item['text']),(1,False,'HOOK-4',['P2','ALM'],'Cap the total context below the limit'))
        self.assertEqual(item['detail'],'the prompt path in `core/runtime.py` builds the context.\nA second line of the same detail.')   # the continuation line comes along
        self.assertEqual(model['items'][3]['detail'],'render the model:\n- bullet one\n- bullet two')
        self.assertIsNone(model['items'][2]['detail']);self.assertIsNone(model['items'][4]['id'])
        last=model['items'][-1];self.assertEqual((last['done'],last['id'],last['text']),(True,'PRIV-1','Close the gaps (PR #38)'))
        by={s['heading']:s['text'] for s in model['sections']}
        self.assertEqual(by['Details'],'- A loose bullet that belongs to no item.');self.assertNotIn('HOOK-4',by['Details']);self.assertIn('Other note',by['Links'])
        json.dumps(model)

    def test_index_matches_check_numbering(self):
        note_id=self.plan_note();model=views.view_model(self.note_of(note_id))
        for item in model['items']:
            self.ok('check',note_id,'--index',str(item['index']))
            after=views.view_model(self.note_of(note_id))['items'][item['index']-1]
            self.assertEqual((after['done'],after['id'],after['text']),(True,item['id'],item['text']))

    def test_a_note_without_action_items_shows_only_sections(self):
        from core.note_format import render
        note=fake(render('A decision.',['**Why:** because'],('[[X]]',),(),(),'',''),type='decision')
        model=views.view_model(note);self.assertEqual((model['items'],model['counts']),([],{'open':0,'done':0,'total':0}))
        self.assertEqual([s['heading'] for s in model['sections']],['Details','Links']);self.assertIn('Notes',views.render_html(model))
        named=fake('A finding.\n\n## Findings\nIt is slow.\n\n## Open questions\nWhy?',type='research')
        self.assertEqual([s['heading'] for s in views.view_model(named)['sections']],['Findings','Open questions'])

    def test_duplicate_detail_label_goes_to_the_first_item_only(self):
        text=body(actions=['[P1] AA-1 one','[P1] AA-1 two'],details=['**AA-1:** the detail'])
        items=views.view_model(fake(text))['items'];self.assertEqual((items[0]['detail'],items[1]['detail']),('the detail',None))

class Page(Fixture):
    def tricky(self):
        evil='</script><script>alert(1)</script><img src=x onerror=alert(2)>'
        return fake(body(actions=[f'[P1] [<b>t</b>] XSS-1 {evil}'],details=[f'**XSS-1:** {evil} <iframe src="http://evil.example/">'])+'\n\n## Section <u>x</u>\nbody '+evil,title='Plan '+evil)

    def test_notes_are_escaped(self):
        model=views.view_model(self.tricky())
        for target in views.TARGETS:
            page=views.render_html(dict(model,view_title='Short <i>title</i>'),target)
            self.assertEqual(page.count('<script'),2);self.assertEqual(page.count('</script>'),2)
            for raw in ('<img','<iframe','<b>t','<u>x','<i>title'):self.assertNotIn(raw,page)
            data=re.search(r'<script type="application/json" id="view-data">(.*?)</script>',page,re.S)[1]
            self.assertEqual(json.loads(data)['items'][0]['text'],model['items'][0]['text'])
            self.assertIn('<title>Short &lt;i&gt;title&lt;/i&gt;</title>',page)

    def test_no_network_references(self):
        for model in (views.view_model(fake()),views.view_model(self.tricky()),views.missing_model({'note':'n','domain':'d','title':'t'})):
            for target in views.TARGETS:
                page=views.render_html(model,target)
                self.assertFalse(re.findall(r'(?:src|href|action|data)\s*=\s*["\']?\s*(?:https?:)?//',page,re.I))
                self.assertFalse(re.findall(r'url\(\s*(?!["\']?\s*data:)',page,re.I))
                for word in ('fetch(','XMLHttpRequest','@import','<link','<img','WebSocket','sendBeacon','http://www.w3.org'):self.assertNotIn(word,page)
        self.assertIn('https://example.com/a',views.render_html(views.view_model(fake(body(actions=['[P1] A-1 see https://example.com/a']))),'local'))   # note text may hold a URL

    def test_theme_contract(self):
        model=dict(views.view_model(fake()),view_title='Hook plan');local=views.render_html(model,'local');artifact=views.render_html(model,'artifact')
        self.assertIn('<title>Hook plan</title>',artifact);self.assertIn('<meta name="viewport"',artifact)
        self.assertRegex(artifact,r':root\{color-scheme:light;--bg:#faf7fd')
        self.assertIn('@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#1b0626',artifact)
        self.assertIn(':root[data-theme="dark"]{color-scheme:dark;--bg:#1b0626',artifact);self.assertIn('body{margin:0;background:var(--bg)',artifact)
        self.assertIn('data-target="artifact"',artifact);self.assertIn('data-target="local"',local)
        self.assertNotIn('prefers-color-scheme',local);self.assertRegex(local,r':root\{color-scheme:dark;--bg:#1b0626')
        # Both carry the reload code (one script), but only a local page arms it.
        self.assertIn("getAttribute('data-target')==='local'",artifact);self.assertIn('15000',local)
        self.assertIn('--accent-strong:#8048b0',local);self.assertIn('--teal:#01dca4',local);self.assertIn('--teal:#018a68',artifact)

    def test_status_banners(self):
        base=views.view_model(fake(status='superseded',superseded_by='deadbeef-0000'))
        self.assertIn('This note was superseded by deadbeef.',views.render_html(base,'local'))
        self.assertIn('This note is resolved.',views.render_html(views.view_model(fake(status='resolved')),'local'))
        gone=views.render_html(views.missing_model({'note':'n','domain':'demo','title':'Old view'}),'local')
        self.assertIn('was not found',gone);self.assertIn('<title>Old view</title>',gone)

class Registry(Fixture):
    def test_add_list_render_remove_and_undo(self):
        note_id=self.plan_note();self.assertFalse((self.root/'views.json').exists())
        added=self.add(note_id);path=Path(added['path']);self.assertTrue(path.is_file());self.assertEqual(path.parent,(self.root/'.views'/'demo'))
        self.assertTrue(path.name.startswith('hook-plan-'+note_id[:8]),path.name)
        registry=json.loads((self.root/'views.json').read_text());entry=registry['views'][0]
        self.assertEqual((registry['version'],entry['note'],entry['domain'],entry['title'],entry['artifact_url'],entry['published_revision']),(1,note_id,'demo','Hook plan',None,None));self.assertTrue(entry['added'])
        row=self.ok('views')['views'][0]
        self.assertEqual((row['note'],row['domain'],row['title'],row['note_title'],row['path'],row['fresh'],row['artifact_url'],row['artifact_current'],row['exists']),
                         (note_id,'demo','Hook plan','Hook plan',str(path),True,None,False,True))
        self.assertEqual(self.ok('views','add',note_id)['status'],'exists')
        path.unlink();self.assertFalse(self.ok('views')['views'][0]['exists'])
        self.ok('views','render',note_id);self.assertTrue(path.is_file())
        removed=self.ok('views','remove',note_id);self.assertFalse(path.exists());self.assertFalse((self.root/'views.json').exists());self.assertEqual(self.ok('views')['views'],[])
        undo=self.ok('undo',removed['operation_id']);self.ok('apply',undo['plan_id'],'--expected-plan-sha256',undo['digest'])
        self.assertEqual(self.ok('views')['views'][0]['note'],note_id)
    def test_undo_of_an_add_removes_the_entry(self):
        added=self.add(self.plan_note());undo=self.ok('undo',added['operation_id']);self.ok('apply',undo['plan_id'],'--expected-plan-sha256',undo['digest'])
        self.assertFalse((self.root/'views.json').exists());self.assertEqual(self.ok('views')['views'],[])

    def test_published_records_the_revision(self):
        note_id=self.plan_note();self.add(note_id);url='https://claude.ai/artifact/abc'
        result=self.ok('views','published',note_id,'--url',url);revision=self.note_of(note_id)['revision']
        self.assertEqual(result['published_revision'],revision);row=self.ok('views')['views'][0];self.assertEqual((row['artifact_url'],row['artifact_current']),(url,True))
        self.ok('update',note_id,'--add-detail','a new fact');self.assertFalse(self.ok('views')['views'][0]['artifact_current'])
        code,error=self.cli('views','published',note_id,'--url','javascript:alert(1)');self.assertNotEqual(code,0)

    def test_views_of_other_domains_and_actors(self):
        note_id=self.plan_note();self.add(note_id)
        self.assertEqual(len(self.ok('views','--all-domains',domain=None)['views']),1)
        for actor in ('subagent','viewer'):
            self.assertEqual(len(self.ok('views',actor=actor)['views']),1)
            self.assertEqual(self.ok('views','render',note_id,'--format','json',actor=actor)['model']['id'],note_id)
            for argv in (('views','add',note_id),('views','remove',note_id),('views','render',note_id),('views','render',note_id,'--format','json','--out','x.json'),('views','published',note_id,'--url','https://x.y')):
                self.assertNotEqual(self.cli(*argv,actor=actor)[0],0,argv)
        self.assertTrue((self.root/'views.json').exists())

    def test_render_options(self):
        note_id=self.plan_note();self.add(note_id);local=Path(self.ok('views')['views'][0]['path'])
        artifact=self.ok('views','render',note_id,'--target','artifact');self.assertEqual(Path(artifact['path']),local.with_name(local.stem+'.artifact.html'))
        self.assertIn('data-target="artifact"',Path(artifact['path']).read_text(encoding='utf-8'))
        out=self.base/'out'/'x.html';self.ok('views','render',note_id,'--out',str(out));self.assertIn('data-target="local"',out.read_text(encoding='utf-8'))
        model=self.ok('views','render',note_id,'--format','json')['model'];self.assertEqual((model['title'],model['view_title'],model['counts']['total']),('Hook plan','Hook plan',6))
        everything=self.ok('views','render','--all');self.assertEqual(everything['count'],1)
        self.assertEqual(len(self.ok('views','render','--all','--format','json')['models']),1)
        other=self.plan_note('Other plan',['[P1] OT-1 one'])
        code,error=self.cli('views','render',other);self.assertNotEqual(code,0)                                     # no view yet: needs --out or json
        self.assertEqual(self.ok('views','render',other,'--format','json')['model']['title'],'Other plan')
        self.assertNotEqual(self.cli('views','render',note_id,'--all')[0],0)
        local.unlink();self.ok('views','render','--all');self.assertTrue(local.is_file())

class Refresh(Fixture):
    def test_renders_on_a_revision_change_and_not_otherwise(self):
        note_id=self.plan_note();self.add(note_id);path=Path(self.ok('views')['views'][0]['path']);first=path.read_text(encoding='utf-8')
        real=views.atomic_write;calls=[]
        with patch.object(views,'atomic_write',side_effect=lambda p,t:(calls.append(Path(p).name),real(p,t))[1]):
            reconcile('demo',self.root);reconcile('demo',self.root);self.assertEqual(calls,[])                       # nothing changed: no write
            self.ok('check',note_id,'--index','1')                                                                 # a note write reaches reconcile
            self.assertEqual(calls,[path.name,'views.json'])
        after=path.read_text(encoding='utf-8');self.assertNotEqual(first,after);self.assertIn('"done":true',after.replace(' ',''))
        self.assertEqual(self.ok('views')['views'][0]['fresh'],True)

    def test_a_note_without_views_costs_nothing(self):
        note_id=self.plan_note()
        with patch.object(views,'load',side_effect=AssertionError('no registry to read')):
            views.refresh('demo',self.root);self.ok('check',note_id,'--index','1')
        self.add(note_id);self.add(self.plan_note('Other plan',['[P1] OT-1 one']))
        with patch.object(views,'atomic_write') as write:
            self.ok('update',note_id,'--add-detail','x');self.assertEqual(write.call_count,2)   # the page and the cache; the other note's view stays
    def test_other_domains_are_left_alone(self):
        note_id=self.plan_note();self.add(note_id)
        with patch.object(views,'atomic_write') as write:
            views.refresh('elsewhere',self.root);self.assertEqual(write.call_count,0)

    def test_a_render_failure_never_fails_the_write(self):
        note_id=self.plan_note();self.add(note_id)
        with patch.object(views,'render_html',side_effect=RuntimeError('boom')),patch('core.logs.swallowed') as swallowed:
            receipt=self.ok('check',note_id,'--index','1');self.assertEqual((receipt['state'],receipt['status']),('committed','update'));self.assertNotIn('warnings',receipt)
            self.assertEqual(swallowed.call_args[0][0],'views.render')
            captured=self.ok('capture','--type','knowledge','--title','Another note','--summary','A fact.','--subject','project','--concern','architecture');self.assertEqual(captured['status'],'created')
        with patch.object(views,'load',side_effect=ValueError('bad registry')),patch('core.logs.swallowed') as swallowed:
            self.assertEqual(self.ok('check',note_id,'--index','2')['state'],'committed');self.assertEqual(swallowed.call_args[0][0],'views.refresh')
        self.ok('views','render','--all');self.assertTrue(self.ok('views')['views'][0]['fresh'])

    def test_resolved_superseded_and_missing_notes_still_render(self):
        first=self.plan_note();second=self.plan_note('New plan',['[P1] NW-1 x']);self.add(first)
        self.ok('supersede',first,'--by',second);page=self.html(first);self.assertIn('This note was superseded by New plan.',page)
        third=self.plan_note('Third plan',['[P1] TH-1 x']);self.add(third,'Third')
        self.ok('resolve',third);row=[r for r in self.ok('views')['views'] if r['note']==third][0];self.assertIn('This note is resolved.',Path(row['path']).read_text(encoding='utf-8'))
        path,_=find_note(third,'demo',self.root);path.unlink();reconcile('demo',self.root)
        self.assertIn('was not found',Path(row['path']).read_text(encoding='utf-8'))
        self.assertTrue(any('not found' in w for w in self.ok('doctor')['warnings']))

class Derived(Fixture):
    def test_views_are_excluded_from_snapshots_and_relocation(self):
        note_id=self.plan_note();self.add(note_id)
        self.assertTrue(list((self.root/'.views').rglob('*.html')));self.assertTrue((self.root/'.cache'/'views.json').is_file())
        files=backup.canonical_files(self.root);self.assertIn('views.json',files);self.assertFalse([f for f in files if '.views' in f or f.startswith('.cache')])
        manifest=storage._manifest(self.root);self.assertIn('views.json',manifest);self.assertFalse([k for k in manifest if k.startswith(('.views','.cache'))])
        self.assertIn('views.json',backup.TOP_LEVEL)

    def test_protected_paths(self):
        for rel in ('views.json','.views/demo/x.html'):self.assertEqual(protected_rule(str(self.root/rel),str(self.base),self.root),'config',rel)
        self.assertIsNone(protected_rule(str(self.root/'.cache'/'views.json'),str(self.base),self.root))

    def test_installer_ships_the_assets(self):
        payload=installer_lifecycle._payload(Path(installer_lifecycle.__file__).resolve().parent.parent)
        for name in ('assets/hivemind-logo.png.b64','assets/plus-jakarta-sans-latin-wght-normal.woff2.b64','assets/OFL-plus-jakarta-sans.txt'):self.assertIn(name,payload)

class Hint(Fixture):
    def setUp(self):
        super().setUp();self.cwd=str(self.base/'project');(self.base/'project').mkdir(exist_ok=True)
        self.note_id=self.plan_note();self.add(self.note_id);self.url='https://claude.ai/artifact/abc'
    def start(self,harness='claude',session='s1'):return runtime.process(harness,'session-start',{'cwd':self.cwd,'session_id':session},self.root).get('context','')
    def prompt(self,session='s1',harness='claude'):return runtime.process(harness,'user-prompt',{'cwd':self.cwd,'session_id':session,'prompt':'what is next?'},self.root).get('context','')
    def line(self):return f'Dashboard "Hook plan" is behind its note: run CMD views render demo:{self.note_id[:8]} --target artifact, publish the printed file to {self.url} with the Artifact tool, then CMD views published demo:{self.note_id[:8]} --url {self.url}'

    def test_only_when_behind_and_only_for_claude(self):
        self.assertNotIn('is behind its note',self.start())                                 # no artifact yet
        self.ok('views','published',self.note_id,'--url',self.url);self.assertNotIn('is behind its note',self.start(session='s2'))   # published and current
        self.ok('update',self.note_id,'--add-detail','changed')
        context=self.start(session='s3');self.assertIn(self.line(),context);self.assertEqual(context.count('is behind its note'),1)
        self.assertNotIn('is behind its note',self.start('codex','s4'));self.assertNotIn('is behind its note',self.prompt('s4','codex'))
        self.ok('views','published',self.note_id,'--url',self.url);self.assertNotIn('is behind its note',self.start(session='s5'))

    def test_a_prompt_says_it_once_per_session_and_revision(self):
        self.ok('views','published',self.note_id,'--url',self.url);self.start();self.ok('update',self.note_id,'--add-detail','changed')
        self.assertIn(self.line(),self.prompt());self.assertNotIn('is behind its note',self.prompt())
        self.ok('update',self.note_id,'--add-detail','changed again');self.assertIn(self.line(),self.prompt())
        self.assertIn(self.line(),self.start(session='s9'))                                  # a new context hears it again

    def test_stays_inside_the_budgets(self):
        from core.session_start import HARD_CAP
        self.ok('views','published',self.note_id,'--url','https://claude.ai/artifact/'+'a'*300);self.ok('update',self.note_id,'--add-detail','changed')
        self.assertLessEqual(len(self.start()),HARD_CAP)

    def test_a_broken_registry_never_costs_the_hook(self):
        (self.root/'views.json').write_text('{broken');self.assertTrue(self.start());self.assertNotIn('is behind its note',self.prompt())

class ReviewFixes(Fixture):
    """Findings of the Codex review of the views branch."""
    def command(self,rest):
        import sys
        return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'
    def decision(self,rest,cwd,**extra):
        return runtime.process('claude','pre-tool-use',{'cwd':cwd,'session_id':'allow','tool_name':'Bash','tool_input':{'command':self.command(rest)},**extra},self.root)

    def test_render_out_never_writes_canonical_store_files(self):
        note_id=self.plan_note();self.add(note_id);before=(self.root/'views.json').read_text(encoding='utf-8')
        for target in ('views.json','bindings.json',str(find_note(note_id,'demo',self.root)[0].relative_to(self.root))):
            for fmt in ('html','json'):
                with self.subTest(target=target,fmt=fmt):
                    code,error=self.cli('views','render',note_id,'--format',fmt,'--out',str(self.root/target));self.assertEqual(code,2);self.assertIn('outside the memory store',str(error))
        self.assertEqual((self.root/'views.json').read_text(encoding='utf-8'),before)
        self.ok('views','render',note_id,'--out',str(self.root/'.views'/'copy.html'));self.ok('views','render',note_id,'--out',str(self.base/'out.html'))

    def test_render_out_is_auto_allowed_only_inside_the_session(self):
        note_id=self.plan_note();project=self.base/'project';project.mkdir(exist_ok=True);cwd=str(project)
        self.assertIn('allow',self.decision(f'views render {note_id} --out "{project/"x.html"}"',cwd))
        self.assertIn('allow',self.decision(f'views render {note_id} --out x.html',cwd))
        for rest in (f'views render {note_id} --out "{self.base/"elsewhere.html"}"',f'views render {note_id} --out=../elsewhere.html'):
            with self.subTest(rest=rest):reply=self.decision(rest,cwd);self.assertNotIn('allow',reply);self.assertNotIn('deny',reply)

    def test_subagents_are_denied_view_writes_in_the_hook(self):
        note_id=self.plan_note();project=self.base/'project';project.mkdir(exist_ok=True);cwd=str(project)
        for rest in (f'views add {note_id}',f'views remove {note_id}',f'views published {note_id} --url https://x.example/a',f'views render {note_id}',
                     f'views render {note_id} --format json --out x.json',f'views render --all'):
            with self.subTest(rest=rest):self.assertIn('deny',self.decision(rest,cwd,agent_id='sub1'))
        for rest in ('views','views --all-domains',f'views render {note_id} --format json',f'views render {note_id} --format=json'):
            with self.subTest(rest=rest):self.assertIn('allow',self.decision(rest,cwd,agent_id='sub1'))

    def test_a_view_whose_note_is_gone_can_be_removed(self):
        for title,ref in (('First plan',lambda n:n[:8]),('Second plan',lambda n:f'demo:{n[:8]}')):
            with self.subTest(title=title):
                note_id=self.plan_note(title);self.add(note_id,title);page=Path(self.ok('views')['views'][0]['path'])
                path,_=find_note(note_id,'demo',self.root);path.unlink();reconcile('demo',self.root)
                self.assertEqual(self.ok('views','remove',ref(note_id))['status'],'removed')
                self.assertFalse(page.exists());self.assertEqual(self.ok('views')['views'],[])

    def test_a_malformed_reference_never_removes_an_orphaned_view(self):
        note_id=self.plan_note();self.add(note_id);path,_=find_note(note_id,'demo',self.root);path.unlink();reconcile('demo',self.root)
        for ref in ('demo:',note_id[:1],note_id[:7],':'):
            with self.subTest(ref=ref):self.assertEqual(self.cli('views','remove',ref)[0],2)
        self.assertEqual(len(self.ok('views')['views']),1)

    def test_a_view_follows_its_note_into_another_domain(self):
        note_id=self.plan_note();self.add(note_id);page=Path(self.ok('views')['views'][0]['path'])
        source,_=find_note(note_id,'demo',self.root);moved=self.root/'domains'/'other'/'notes'/source.name
        moved.parent.mkdir(parents=True,exist_ok=True);source.replace(moved)
        text=moved.read_text(encoding='utf-8');moved.write_text(text.replace('- [ ] [P2] [ALM] HOOK-4','- [x] [P2] [ALM] HOOK-4',1),encoding='utf-8')
        views.refresh('other',self.root)
        self.assertIn('"done":true',page.read_text(encoding='utf-8').replace(' ',''));self.assertNotIn('was not found',page.read_text(encoding='utf-8'))

if __name__=='__main__':unittest.main()
