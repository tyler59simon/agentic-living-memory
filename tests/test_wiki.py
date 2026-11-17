"""Domain wiki: the model (projects, sections, code tree, links), the page, and the wiki command."""
import contextlib,io,json,re,unittest
from pathlib import Path
from test_core import StoreFixture
from core.cli import main
from core.notes import capture,change
from core import runtime,wiki

BASE=['subject/project','concern/architecture']
SLASH=lambda p:str(p).replace(chr(92),'/')
EVIL='</script><script>alert(1)</script><img src=x onerror=alert(2)>'

class Fixture(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':BASE+['concern/decision','project/hive','project/lm']}))
        self.hive=self.base/'project'/'hive';self.lm=self.base/'project'/'lm'
        for p in (self.hive/'src'/'lib',self.lm):p.mkdir(parents=True,exist_ok=True)
        self.registry(charter=None)
    def registry(self,charter):
        entries=[{'slug':'hive','name':'Hive','description':'The hive app.','aliases':['HV'],'status':'active','repos':[SLASH(self.hive)],**({'charter':charter} if charter else {})},
                 {'slug':'lm','name':'LM','description':'The memory store.','status':'active','repos':[SLASH(self.lm)]}]
        (self.root/'projects.json').write_text(json.dumps({'version':1,'projects':entries}))
    def add(self,title,body,tags=(),domain='demo',**kw):
        return capture(title,body,BASE+list(tags),domain,self.root,**kw)['id']
    def cli(self,*argv,actor='main'):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            try:code=main(['--root',str(self.root),'--actor',actor,'--domain','demo',*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,(json.loads(text) if text.strip()[:1] in '{[' else text)
    def ok(self,*argv,**kw):
        code,result=self.cli(*argv,**kw);self.assertEqual(code,0,result);return result
    def sample(self):
        ids={}
        ids['brief']=self.add('Hive brief','The hive app reads memory.\n\n## Details\n- **Goal:** ship it.',['project/hive'],type='brief',files=[SLASH(self.hive)])
        ids['decision']=self.add('Use SQLite','Store indexes in SQLite. See [[Hive brief]], [[Shared rule]] and [[Nowhere page]].',['project/hive'],type='decision')
        ids['app']=self.add('hive/src/app.ts: root component','The root component.',['project/hive'],files=[SLASH(self.hive/'src'/'app.ts')])
        ids['util']=self.add('hive/src/lib/util.ts: helpers','Small helpers.',['project/hive'],files=['hive/src/lib/util.ts'])
        ids['both']=self.add('lm/store.py: the store','Tagged twice; its file decides.',['project/hive','project/lm'],files=['lm/store.py'])
        ids['stray']=self.add('A loose script','Lives outside every repo.',['project/hive'],files=['C:/elsewhere/tool.py'])
        ids['general']=self.add('Plain fact','Belongs to no project.')
        ids['bug']=self.add('Crash on start','The app crashed.',['project/hive'],type='bug')
        ids['shared']=self.add('Shared rule','Holds everywhere.',domain='_global')
        return ids

class Model(Fixture):
    def test_projects_sections_and_tree(self):
        ids=self.sample();m=wiki.wiki_model('demo',self.root);notes={n['id']:n for n in m['notes']}
        self.assertEqual([p['slug'] for p in m['projects']],['hive','lm','general'])   # by page count, General last
        hive=m['projects'][0];self.assertEqual((hive['name'],hive['aliases'],hive['repos']),('Hive',['HV'],['hive']))
        self.assertEqual(notes[ids['both']]['project'],'lm')          # two project tags: the repo of its first file breaks the tie
        self.assertEqual(notes[ids['general']]['project'],'general')
        self.assertEqual({k:notes[ids[k]]['section'] for k in ('brief','decision','app','general','bug')},
                         {'brief':'overview','decision':'decisions','app':'code','general':'architecture','bug':'issues'})   # BASE tags concern/architecture
        self.assertEqual(set(hive['sections']),{'overview','decisions','code','issues'});self.assertEqual(hive['briefs'],[ids['brief']])
        folders={f['id']:f for f in m['folders']};hive_roots=[folders[i] for i in hive['sections']['code']['folders']]
        self.assertEqual(sorted(f['name'] for f in hive_roots),['hive',wiki.OTHER])
        root=next(f for f in hive_roots if f['name']=='hive')
        self.assertEqual((root['notes'],root['count'],notes[ids['brief']]['filename']),([ids['brief']],3,''))   # the brief names the folder: About this folder
        src=folders[root['folders'][0]]
        self.assertEqual((src['name'],src['notes'],[folders[c]['name'] for c in src['folders']],src['count']),('src',[ids['app']],['lib'],2))
        self.assertEqual((notes[ids['app']]['files'],notes[ids['app']]['filename']),(['hive/src/app.ts'],'app.ts'))   # absolute path made repo-relative
        self.assertEqual(notes[ids['util']]['folder'],src['folders'][0])
        self.assertEqual(folders[notes[ids['stray']]['folder']]['name'],wiki.OTHER)

    def test_lead_links_and_external(self):
        ids=self.sample();m=wiki.wiki_model('demo',self.root);notes={n['id']:n for n in m['notes']}
        brief=notes[ids['brief']];self.assertEqual(brief['lead'],'The hive app reads memory.');self.assertTrue(brief['body'].startswith('## Details'))
        self.assertEqual(notes[ids['decision']]['out'],[ids['brief']]);self.assertEqual(brief['backlinks'],[ids['decision']])
        self.assertEqual(m['external'],{'shared rule':{'title':'Shared rule','domain':'_global'}})
        self.assertNotIn(ids['shared'],notes)

    def test_charter_and_archived(self):
        ids=self.sample();self.registry(charter='demo:'+ids['brief'])
        m=wiki.wiki_model('demo',self.root);self.assertEqual(m['projects'][0]['charter'],ids['brief']);self.assertFalse(m['archived'])
        self.registry(charter='other:'+ids['brief']);self.assertIsNone(wiki.wiki_model('demo',self.root)['projects'][0]['charter'])   # another domain's charter is not a page here
        change(ids['bug'],'demo','resolve',self.root)
        self.assertNotIn(ids['bug'],{n['id'] for n in wiki.wiki_model('demo',self.root)['notes']})
        retired={n['id']:n for n in wiki.wiki_model('demo',self.root,archived=True)['notes']}[ids['bug']]
        self.assertEqual((retired['status'],retired['retired']),('resolved',True))

    def test_empty_domain(self):
        m=wiki.wiki_model('demo',self.root);self.assertEqual((m['notes'],m['projects'],m['folders']),([],[],[]))
        self.assertIn('id="wiki-data"',wiki.render_html(m))

class Page(Fixture):
    def test_notes_are_escaped(self):
        self.add('Plan '+EVIL,'Summary '+EVIL+'\n\n## Section <u>x</u>\nbody '+EVIL,['project/hive'],files=['hive/<b>x</b>.ts'])
        page=wiki.render_html(wiki.wiki_model('demo',self.root))
        self.assertEqual(page.count('<script'),2);self.assertEqual(page.count('</script>'),2)
        for raw in ('<img','<u>x','<b>x'):self.assertNotIn(raw,page)
        data=json.loads(re.search(r'<script type="application/json" id="wiki-data">(.*?)</script>',page,re.S)[1])
        self.assertEqual(data['notes'][0]['title'],'Plan '+EVIL)
        self.assertEqual(page.count('.innerHTML='),1)   # only the fixed icon shapes

    def test_single_child_folders_collapse(self):
        self.add('hive/src/lib/util.ts: helpers','Small helpers.',['project/hive'],files=['hive/src/lib/util.ts'])
        folders=wiki.wiki_model('demo',self.root)['folders']
        self.assertEqual([(f['name'],f['path'],f['parent']) for f in folders],[('hive/src/lib','hive/src/lib',None)])

    def test_no_network_references(self):
        self.sample();page=wiki.render_html(wiki.wiki_model('demo',self.root))
        self.assertFalse(re.findall(r'(?:src|href|action|data)\s*=\s*["\']?\s*(?:https?:)?//',page,re.I))
        self.assertFalse(re.findall(r'url\(\s*(?!["\']?\s*data:)',page,re.I))
        for word in ('fetch(','XMLHttpRequest','@import','<link','<img','WebSocket','sendBeacon'):self.assertNotIn(word,page)

    def test_theme_contract(self):
        names=lambda block:sorted(re.findall(r'(--[a-z0-9-]+):',block))
        self.assertEqual(names(wiki.DARK),names(wiki.LIGHT))
        page=wiki.render_html(wiki.wiki_model('demo',self.root))
        self.assertIn(':root{'+wiki.LIGHT,page);self.assertIn('@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){'+wiki.DARK,page)
        self.assertIn(':root[data-theme="dark"]{'+wiki.DARK,page);self.assertIn('body{margin:0;background:var(--bg)',page)
        used=set(re.findall(r'var\((--[a-z0-9-]+)',wiki.CSS))-{'--font-sans','--font-mono'}
        self.assertFalse(used-set(names(wiki.DARK)),'CSS uses tokens the themes do not define')
        for t in __import__('core.note_types',fromlist=['NOTE_TYPES']).NOTE_TYPES:self.assertIn(f'.type-{t}{{color:var(--t-{t})}}',page)

    def test_document_and_fragment(self):
        m=wiki.wiki_model('demo',self.root);full=wiki.render_html(m);part=wiki.render_html(m,document=False)
        self.assertTrue(full.startswith('<!doctype html>'));self.assertTrue(part.startswith('<title>Demo memory wiki</title>'));self.assertIn(part,full)

    def test_routes_cover_the_model(self):
        ids=self.sample();m=wiki.wiki_model('demo',self.root)
        # Route ids are plain hash names (letters, digits, hyphens), so the page also works as a claude.ai artifact.
        routes=['p-'+p['slug'] for p in m['projects']]+['s-'+p['slug']+'-'+k for p in m['projects'] for k in p['sections']]+[f['id'] for f in m['folders']]+['n-'+n['ref'] for n in m['notes']]
        for r in routes:self.assertRegex(r,r'^[a-z0-9-]+$')
        self.assertEqual(len(routes),len(set(routes)))

class Command(Fixture):
    def test_default_path_out_and_json(self):
        self.sample();r=self.ok('wiki');path=Path(r['path'])
        self.assertEqual(path,(self.root/'.views'/'demo'/'wiki.html').resolve());self.assertTrue(path.is_file());self.assertEqual(r['pages'],8)
        out=self.base/'export'/'demo-wiki.html';self.assertEqual(Path(self.ok('wiki','--out',str(out))['path']),out.resolve());self.assertTrue(out.is_file())
        model=self.ok('wiki','--format','json')['model'];self.assertEqual(len(model['notes']),8)
        self.assertEqual(len(self.ok('wiki','--format','json','--archived')['model']['notes']),8)
        code,err=self.cli('wiki','--out',str(self.root/'domains'/'demo'/'notes'/'x.html'));self.assertNotEqual(code,0);self.assertIn('--out must be outside',err['error']['message'])

    def test_viewers_and_subagents_only_read(self):
        self.sample();self.ok('wiki','--format','json',actor='viewer');self.ok('wiki','--format','json',actor='subagent')
        for actor in ('viewer','subagent'):
            for argv in (('wiki',),('wiki','--format','json','--out',str(self.base/'x.json'))):
                code,err=self.cli(*argv,actor=actor);self.assertNotEqual(code,0);self.assertIn('read-only',err['error']['message'])
        self.assertIn('wiki',self.ok('version')['commands'])

    def test_hook_allows_the_main_agent_and_lets_subagents_only_read(self):
        import sys
        base=f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo wiki'
        self.assertIn('wiki',runtime.ALLOWED_SUBCOMMANDS)
        for extra,writes in (('',True),(' --format json',False),(' --format json --out x.json',True),(' --archived --out x.html',True)):
            self.assertEqual(runtime.wiki_write(base+extra,'posix'),writes,extra)
            payload={'cwd':str(self.base/'project'),'session_id':'h','tool_name':'Bash','tool_input':{'command':base+extra}}
            self.assertIn('allow',runtime.process('claude','pre-tool-use',payload,self.root),extra)
            self.assertEqual('deny' in runtime.process('claude','pre-tool-use',{**payload,'agent_id':'sub-1'},self.root),writes,extra)

if __name__=='__main__':unittest.main()
