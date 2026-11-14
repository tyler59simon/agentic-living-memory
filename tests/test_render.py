import contextlib,io,json,sys,unittest
from unittest.mock import patch
from test_core import StoreFixture
from core import cli,render

class Tty(io.StringIO):
    def isatty(self):return True

class RenderShapes(unittest.TestCase):
    HIT={'id':'a6e9955e-ed50-460b-b70b-6f8650ed010c','domain':'demo','title':'Cache','type':'knowledge','updated':'2026-11-14','rank':1,'snippet':'The cache is   rebuildable.'}
    def test_search_hits_and_empty(self):
        text=render.render({'ok':True,'results':[self.HIT]});self.assertEqual(text.splitlines(),['1. Cache  [knowledge, 2026-11-14]  demo:a6e9955e','   The cache is rebuildable.'])
        self.assertEqual(render.render({'ok':True,'results':[]}),'No matches.')
    def test_ask_shows_notes_then_sources(self):
        text=render.render({'ok':True,'notes':[self.HIT],'sources':[{'path':'docs/a.md','snippet':'excerpt text'}]});self.assertIn('Sources:',text);self.assertIn('docs/a.md',text)
    def test_read(self):
        text=render.render({'title':'Cache','type':'knowledge','status':'active','tags':['a/b'],'id':self.HIT['id'],'domain':'demo','updated':'2026-11-14','body':'Body text','truncated':True})
        self.assertEqual(text.splitlines()[0],'Cache');self.assertIn('demo:a6e9955e',text);self.assertIn('tags: a/b',text);self.assertTrue(text.endswith('(truncated)'))
    def test_rows(self):
        text=render.render({'ok':True,'notes':[{'ref':'demo:a6e9955e-ed50','title':'T','type':'plan','date':'2026-11-14T10:00:00','reason':'due'}],'hint':'Do this.'});self.assertIn('- T  [plan, 2026-11-14]  demo:a6e9955e',text);self.assertIn('hint: Do this.',text)
    def test_plan_names_the_apply_command(self):
        text=render.render({'ok':True,'plan_id':'p'*32,'digest':'d'*64,'kind':'rename','summary':{'id':'x','files':2}})
        self.assertIn('alm apply '+'p'*32+' --expected-plan-sha256 '+'d'*64,text);self.assertIn('files: 2',text)
    def test_receipt_lists_similar_and_warnings(self):
        text=render.render({'ok':True,'status':'created','id':self.HIT['id'],'domain':'demo','similar':[{'ref':'demo:b','title':'Other'}],'quality_warnings':['style: x']})
        self.assertEqual(text.splitlines()[:2],['created: demo:a6e9955e','  similar: demo:b  Other']);self.assertIn('quality_warnings: style: x',text)
    # Codex review of feat/alm-command: the shapes below were crashed, dropped or misrouted.
    def test_open_lists_items_beside_an_integer_note_count(self):
        text=render.render({'ok':True,'domain':'demo','notes':1,'count':1,'items':[{'ref':'demo:'+self.HIT['id'],'short_ref':'demo:a6e9955e','title':'Plan','type':'plan','updated':'2026-11-14','index':2,'item':'Ship it'}]})
        self.assertIn('Plan  [plan, 2026-11-14]  demo:a6e9955e',text);self.assertIn('#2 Ship it',text)
    def test_metadata_only_read_shows_metadata(self):
        text=render.render({'title':'Cache','type':'knowledge','status':'active','id':self.HIT['id'],'domain':'demo','files':['core/a.py'],'review_by':'2027-01-01'})
        self.assertEqual(text.splitlines()[0],'Cache');self.assertIn('review_by: 2027-01-01',text);self.assertIn('core/a.py',text)
    def test_read_shows_the_stale_source_warning(self):
        text=render.render({'title':'Cache','type':'knowledge','id':self.HIT['id'],'domain':'demo','body':'B','stale':'STALE: core/a.py changed since this digest'})
        self.assertIn('STALE: core/a.py changed',text)
        text=render.render({'title':'Cache','type':'knowledge','id':self.HIT['id'],'domain':'demo','body':'B','currency':{'state':'missing'}});self.assertIn('source: missing',text)
    def test_list_with_summary_shows_summary_files_and_actions(self):
        text=render.render({'ok':True,'notes':[dict(self.HIT,summary='The cache is rebuildable.',files=['core/a.py'],actions={'open':2,'done':1})]})
        self.assertIn('The cache is rebuildable.',text);self.assertIn('files: core/a.py',text);self.assertIn('actions: 2 open, 1 done',text)
    def test_relocation_inspection_names_setup_apply_and_keeps_its_checks(self):
        text=render.render({'ok':True,'plan_id':'p'*32,'digest':'d'*64,'source':'C:/old','destination':'D:/new','config_changed':False,'added':[],'removed':['x.md'],'changed':[]})
        self.assertIn('alm setup --apply-plan '+'p'*32+' --expected-plan-sha256 '+'d'*64,text);self.assertNotIn('alm apply ',text)
        self.assertIn('destination: D:/new',text);self.assertIn('config_changed: false',text);self.assertIn('x.md',text)
    def test_markdown_results_print_their_markdown(self):   # recap and history --markdown
        self.assertEqual(render.render({'ok':True,'rows':[{'op':'x'}],'markdown':'## Recap\n- one\n'}),'## Recap\n- one')
    def test_batch_and_fallback(self):
        text=render.render([{'exit_code':0,'result':{'ok':True,'status':'created','id':self.HIT['id'],'domain':'demo'},'error':None},{'exit_code':2,'result':None,'error':'bad'}]);self.assertIn('exit code 0',text);self.assertIn('bad',text)
        self.assertEqual(render.render({'ok':True,'python':'3.15','nested':{'a':1},'items_':[1,2]}),'python: 3.15\nnested:\n  a: 1\nitems_:\n  - 1\n  - 2')

class Cli(StoreFixture):
    def setUp(self):
        super().setUp();self.note(title='Cache policy',body='The cache is rebuildable from the notes.')
    def run_main(self,argv,tty_out=True,tty_err=True):
        out,err=(Tty if tty_out else io.StringIO)(),(Tty if tty_err else io.StringIO)()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=cli.main(['--root',str(self.root),'--domain','demo']+argv)
        return code,out.getvalue(),err.getvalue()
    def test_text_on_a_terminal_json_otherwise(self):
        code,out,_=self.run_main(['search','cache']);self.assertEqual(code,0);self.assertTrue(out.startswith('1. Cache policy'));self.assertRaises(ValueError,json.loads,out)
        self.assertTrue(json.loads(self.run_main(['search','cache'],tty_out=False)[1])['ok'])
    def test_json_flag_forces_json_before_or_after_the_command(self):
        for argv in (['--json','search','cache'],['search','cache','--json']):
            with self.subTest(argv=argv):self.assertTrue(json.loads(self.run_main(argv)[1])['ok'])
    def test_error_is_one_line_with_the_exit_code(self):
        code,out,err=self.run_main(['read','nothing-here']);self.assertEqual((code,out),(2,''));self.assertEqual(len(err.splitlines()),1);self.assertTrue(err.startswith('alm: error (invalid): '))
        code,_,err=self.run_main(['read','nothing-here'],tty_err=False);self.assertEqual(json.loads(err)['error']['code'],'invalid')
        self.assertEqual(self.run_main(['--json','read','nothing-here'])[2][0],'{')
    def test_serve_replies_stay_json(self):
        out=Tty()   # even a terminal on the serving side must not turn the replies into text
        with patch.object(sys,'stdin',io.StringIO(json.dumps({'id':1,'argv':['search','cache']})+'\n')),contextlib.redirect_stdout(out):cli.serve(self.root,self.base/'project',set(cli.build_parser().subs.choices))
        reply=json.loads(out.getvalue());self.assertEqual(reply['exit_code'],0);self.assertEqual(reply['result']['results'][0]['title'],'Cache policy')
    def test_help_starts_with_alm_and_every_command_has_a_line(self):
        parser=cli.build_parser();text=parser.format_help();self.assertTrue(text.startswith('usage: alm '))
        for name in parser.subs.choices:self.assertIn(name,cli.HELP);self.assertRegex(text,r'\n    '+name+r'\s+\S')

if __name__=='__main__':unittest.main()
