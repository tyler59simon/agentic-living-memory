"""`search` is the one find command: notes, sources, a file, notebook pages, the session transcript, the link graph, and a list of notes with no query.
Each form, its default limit, and the refusals when a flag belongs to another form."""
import contextlib,io,json,unittest
from test_core import StoreFixture
from core import sources
from core.cli import main

class Fixture(StoreFixture):
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            try:code=main(['--root',str(self.root),'--domain','demo',*argv])
            except SystemExit as exc:code=exc.code
        return code,json.loads(out.getvalue() or err.getvalue() or 'null')
    def ok(self,*argv):
        code,result=self.cli(*argv);self.assertEqual(code,0,result);return result
    def many(self,count):
        return [self.note(f'Zork note {n}',f'Zork number {n} is described here with its own words {"abcdefghij"[n%10]*5}.',allow_similar=True)['id'] for n in range(count)]

class Forms(Fixture):
    def test_notes_form_is_unchanged(self):
        n=self.note();found=self.ok('search','cache')
        self.assertEqual(found['results'][0]['id'],n['id']);self.assertIn(found['mode'],{'active','fallback','all'})

    def test_file_form(self):
        self.note('Layout','The layout is described in src/app.py.',files=['src/app.py'])
        found=self.ok('search','--file','src/app.py');self.assertEqual((found['mode'],len(found['results'])),('file',1))

    def test_sources_form(self):
        found=self.ok('search','cache','--sources');self.assertEqual(found['mode'],'sources')

    def folder(self,**pages):
        """A registered folder notebook with these pages, indexed by the knowledge base."""
        nb=self.base/'project'/'wiki';nb.mkdir(parents=True,exist_ok=True)
        for name,text in pages.items():(nb/name).write_text(text,encoding='utf-8')
        self.ok('doc','folder','set',str(nb),'--visibility','searchable');sources.sync(self.root,'demo');return nb

    def test_pages_form_finds_folder_pages_in_every_domain(self):
        self.folder(**{'Retry.md':'# Retry\n\nBackoff doubles each attempt up to a ceiling.\n'})
        found=self.ok('search','backoff ceiling','--pages')
        self.assertEqual((found['mode'],found['query'],found['domains'][0]),('pages','backoff ceiling','demo'));self.assertEqual([p['path'] for p in found['pages']],['Retry.md'])
        self.assertEqual({p['origin'] for p in found['pages']},{'folder'});self.assertNotIn('pages_stale',found)
        self.assertEqual([p['path'] for p in self.ok('search','backoff','--pages','--all-domains')['pages']],['Retry.md'])
        self.assertNotIn('pages',self.ok('search','backoff','--sources'))
        code,error=self.cli('search','backoff','--pages','--rebuild');self.assertEqual(code,2)   # the page index it rebuilt is gone: --rebuild belongs to a notes search
        self.assertIn('--rebuild',error['error']['message'])

    def test_pages_default_limit_is_10_and_the_cap_50(self):
        self.folder(**{f'P{n}.md':f'# Page {n}\n\nZephyr gust number {n}.\n' for n in range(12)})
        self.assertEqual(len(self.ok('search','zephyr','--pages')['pages']),10)
        self.assertEqual(len(self.ok('search','zephyr','--pages','--limit','20')['pages']),12)

    def test_transcript_form_needs_a_session_with_a_transcript(self):
        code,error=self.cli('search','upload','--transcript','--session','nobody');self.assertEqual(code,2);self.assertIn('No transcript recorded',error['error']['message'])

    def test_links_form(self):
        a=self.note('Cache','The cache is rebuildable.');b=self.note('Uses cache','Depends on [[Cache]].')
        found=self.ok('search','--links');self.assertEqual(found['mode'],'links');self.assertEqual([(e['from'],e['to']) for e in found['edges']],[('demo:'+b['id'],'demo:'+a['id'])])

    def test_list_form_has_no_query_and_keeps_its_rows(self):
        a=self.note();found=self.ok('search')
        self.assertEqual((found['mode'],found['total'],found['offset']),('list',1,0));self.assertEqual(found['notes'][0]['ref'],'demo:'+a['id'])
        self.assertEqual(self.ok('search','--type','decision')['notes'],[]);self.assertEqual(len(self.ok('search','--tag','concern/architecture')['notes']),1)

class Limits(Fixture):
    def test_each_form_has_its_own_default(self):
        ids=self.many(7)
        self.assertEqual(len(self.ok('search','zork')['results']),5)
        self.assertEqual(len(self.ok('search','zork','--limit','7')['results']),7)
        listed=self.ok('search');self.assertEqual((listed['total'],len(listed['notes'])),(7,7))   # a list defaults to 200, not 5
        self.assertEqual(len(self.ok('search','--limit','3')['notes']),3);self.assertEqual(len(ids),7)

class Refusals(Fixture):
    def test_flags_of_another_form_are_refused_with_the_right_one_named(self):
        self.note()
        for argv,said in ((('search','cache','--type','decision'),'--filter type=T'),(('search','cache','--tag','x'),'--filter tag=T'),(('search','cache','--status','any'),'--filter status=S'),
                          (('search','cache','--all-domains'),'--scope all'),(('search','cache','--with-summary'),'list'),(('search','cache','--offset','1'),'list'),
                          (('search','cache','--session','s'),'--transcript'),(('search','cache','--all'),'--transcript'),
                          (('search','--scope','all'),'notes search'),(('search','--explain'),'notes search'),(('search','--filter','type=decision'),'notes search'),
                          (('search','cache','--sources','--scope','all'),'search --sources'),(('search','cache','--sources','--file','a.py'),'search --sources'),
                          (('search','cache','--pages','--filter','type=decision'),'search --pages'),(('search','cache','--pages','--type','plan'),'search --pages'),
                          (('search','--links','--tag','x'),'search --links'),(('search','--links','--scope','all'),'search --links'),(('search','cache','--transcript','--session','s','--rebuild'),'search --transcript'),(('search','cache','--pages','--rebuild'),'search --pages'),
                          (('search','cache','--links'),'takes no query'),(('search','--links','--status','resolved'),'active or any')):
            with self.subTest(argv=argv):
                code,error=self.cli(*argv);self.assertEqual(code,2,error);self.assertIn(said,error['error']['message'])

    def test_mode_flags_exclude_each_other_and_need_what_they_work_on(self):
        for argv,said in ((('search','cache','--sources','--pages'),'each choose a search'),(('search','--pages','--links'),'each choose a search'),(('search','cache','--transcript','--links'),'each choose a search'),
                          (('search','--pages'),'needs a query with --pages'),(('search','--sources'),'needs a query'),(('search','--transcript','--session','s'),'needs a query with --transcript'),
                          (('search','cache','--transcript'),'needs --session')):
            with self.subTest(argv=argv):
                code,error=self.cli(*argv);self.assertEqual(code,2,error);self.assertIn(said,error['error']['message'])

class RemovedForms(Fixture):
    def test_the_old_commands_are_gone(self):
        for argv in (('list',),('links',),('recall','x','--session','s'),('doc','search','x'),('plan','delete','demo:x')):
            with self.subTest(argv=argv):self.assertEqual(self.cli(*argv)[0],2)
        commands=set(self.ok('version')['commands']);self.assertFalse(commands&{'list','links','recall','plan'});self.assertIn('delete-plan',commands)

if __name__=='__main__':unittest.main()
