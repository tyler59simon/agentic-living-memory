import contextlib,io,json,unittest
from pathlib import Path
from test_core import StoreFixture
from core import runtime
from core.cli import main
from core.notes import find_note
from core.repository import find_secret,find_secrets,redact

TOKEN='ghp_'+'a1B2'*9

class CaptureErgonomics(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','subject/user','concern/architecture','concern/decision',
                                                                        'concern/preference','concern/knowledge']}))
        self.elsewhere=self.base/'elsewhere';self.elsewhere.mkdir()
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),*argv])
        return code,json.loads(out.getvalue() or err.getvalue())
    def start(self,session='s1'):
        payload={'cwd':str(self.base/'project'),'session_id':session,'prompt':'hello'}
        runtime.process('claude','session-start',payload,self.root);runtime.process('claude','user-prompt',payload,self.root)
    def tags(self,result):return find_note(result['id'],'demo',self.root)[1]['tags']

    def test_session_supplies_the_domain(self):
        self.start()
        code,result=self.cli('--cwd',str(self.elsewhere),'capture','--session','s1','--type','decision','--title','Use SQLite','--summary','We chose SQLite.')
        self.assertEqual(code,0,result);self.assertEqual(find_note(result['id'],'demo',self.root)[1]['domain'],'demo')
    def test_unbound_without_session_still_fails(self):
        code,result=self.cli('--cwd',str(self.elsewhere),'capture','--type','decision','--title','Use SQLite','--summary','We chose SQLite.')
        self.assertEqual(code,2);self.assertIn('unbound',result['error']['message'])
    def test_inferred_tags_by_type(self):
        cases={'decision':['concern/decision','subject/project'],'preference':['concern/preference','subject/user'],'knowledge':['concern/knowledge','subject/project']}
        for kind,expected in cases.items():
            with self.subTest(kind=kind):
                code,result=self.cli('--domain','demo','capture','--type',kind,'--title',f'Distinct {kind} subject','--summary','A fact.')
                self.assertEqual(code,0,result);self.assertEqual(sorted(result['inferred_tags']),expected);self.assertEqual(self.tags(result),expected)
    def test_explicit_tags_are_not_overridden(self):
        code,result=self.cli('--domain','demo','capture','--type','decision','--subject','user','--concern','architecture','--title','Layered cache','--summary','A fact.')
        self.assertEqual(code,0,result);self.assertNotIn('inferred_tags',result);self.assertEqual(self.tags(result),['concern/architecture','subject/user'])
    def test_no_default_outside_the_taxonomy_still_errors(self):
        # status has no concern default; bug's concern/bug is not in this taxonomy.
        for kind in ('status','bug'):
            with self.subTest(kind=kind):
                code,result=self.cli('--domain','demo','capture','--type',kind,'--title',f'Some {kind}','--summary','A fact.')
                self.assertEqual(code,2);self.assertIn('concern/',result['error']['message'])
    def test_facet_flags_accept_both_forms(self):
        code,result=self.cli('--domain','demo','capture','--subject','subject/project','--concern','architecture','--title','Cache tiers','--summary','A fact.')
        self.assertEqual(code,0,result);self.assertEqual(self.tags(result),['concern/architecture','subject/project'])
        code,result=self.cli('--domain','demo','update','demo:'+result['id'],'--subject','subject/user','--concern','concern/decision')
        self.assertEqual(code,0,result);self.assertEqual(self.tags(result),['concern/decision','subject/user'])

    def test_secret_rejected_by_default(self):
        code,result=self.cli('--domain','demo','capture','--title','Token location','--summary',f'The CI token is {TOKEN}.')
        self.assertEqual(code,2);self.assertIn('--redact',result['error']['message'])
    def test_redact_on_capture(self):
        code,result=self.cli('--domain','demo','capture','--redact','--title','Token location','--summary',f'The CI token is {TOKEN}.','--detail','**Config:** DB_PASSWORD=hunter2hunter2')
        self.assertEqual(code,0,result)
        self.assertEqual(result['redacted'],[{'kind':'GitHub token','field':'body'},{'kind':'credential assignment','field':'body'}])
        body=find_note(result['id'],'demo',self.root)[1]['body']
        self.assertNotIn(TOKEN,body);self.assertNotIn('hunter2',body)
        self.assertIn("<redacted type='GitHub token'/>",body);self.assertIn("DB_PASSWORD=<redacted type='credential assignment'/>",body)
        self.assertNotIn(TOKEN,Path(find_note(result['id'],'demo',self.root)[0]).read_text(encoding='utf-8'))
    def test_redact_on_update(self):
        n=self.note()
        code,result=self.cli('--domain','demo','update','demo:'+n['id'],'--redact','--summary',f'Rotated; old value {TOKEN}.')
        self.assertEqual(code,0,result);self.assertEqual(result['redacted'],[{'kind':'GitHub token','field':'body'}])
        self.assertNotIn(TOKEN,find_note(n['id'],'demo',self.root)[1]['body'])
    def test_redacted_text_passes_the_secret_check(self):
        text,kinds=redact(f'a {TOKEN} b api_key=sk-'+'x1'*15+' c')
        self.assertEqual(len(kinds),2);self.assertIsNone(find_secret(text));self.assertEqual(find_secrets(text),[])
    def test_overlapping_matches_are_redacted_once(self):
        jwt='eyJ'+'a'*12+'.eyJ'+'b'*12+'.'+'c'*12
        text,kinds=redact(f'Authorization: Bearer {jwt}')
        self.assertEqual(kinds,['bearer token']);self.assertNotIn('eyJ',text)

if __name__=='__main__':unittest.main()
