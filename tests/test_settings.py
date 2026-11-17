"""config get and config set: validated, journaled changes to the settings in bindings.json (FEAT-5)."""
import contextlib,io,json
from test_core import StoreFixture
from core import cli
from core.governance import undo
from core.repository import read_json

class Config(StoreFixture):
    def run_cli(self,*argv,actor='main'):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=cli.main(['--root',str(self.root),'--actor',actor,'--domain','demo','config',*argv])
        return code,json.loads(out.getvalue() or err.getvalue())

    def ok(self,*argv):
        code,result=self.run_cli(*argv);self.assertEqual(code,0,result);return result

    def error(self,*argv,actor='main'):
        code,result=self.run_cli(*argv,actor=actor);self.assertEqual(code,2,result);return result['error']

    def bindings(self):return read_json(self.root/'bindings.json',{})

    def test_get_shows_defaults_and_stored_values(self):
        settings=self.ok('get')['settings']
        self.assertEqual(set(settings),{'search_scope','repo_aliases','suggest_bind','auto_resolve','cross_project'})
        self.assertEqual(settings['auto_resolve'],{'value':True,'default':True})
        self.assertEqual(settings['search_scope']['value']['global_domains'],['_global'])
        self.assertEqual(self.ok('get','cross_project')['settings'],{'cross_project':{'value':True,'default':True}})

    def test_each_setting_round_trips(self):
        cases={'suggest_bind':False,'auto_resolve':False,'cross_project':False,'repo_aliases':{'agentic-living-memory':['LM','alm']},
               'search_scope':{'global_domains':['_global'],'foreign_domains':['other']}}
        for key,value in cases.items():
            with self.subTest(key=key):
                result=self.ok('set',key,json.dumps(value))
                self.assertEqual((result['value'],result['default']),(value,False));self.assertTrue(result['operation_id'])
                self.assertEqual(self.bindings()[key],value);self.assertEqual(self.ok('get',key)['settings'][key]['value'],value)
        self.assertIn('demo',self.bindings()['domains'])   # the domain bindings are kept

    def test_null_restores_the_default_and_an_unchanged_value_writes_nothing(self):
        self.ok('set','auto_resolve','false')
        self.assertIsNone(self.ok('set','auto_resolve','false')['operation_id'])
        result=self.ok('set','auto_resolve','null');self.assertEqual((result['value'],result['default']),(True,True))
        self.assertNotIn('auto_resolve',self.bindings())

    def test_bad_values_and_unknown_keys_are_refused(self):
        before=(self.root/'bindings.json').read_bytes()
        for argv,start in ((('set','auto_resolve','"no"'),'invalid_value'),(('set','auto_resolve','yes'),'invalid_value'),
                           (('set','repo_aliases','{"x": "LM"}'),'invalid_value'),(('set','search_scope','{"global_domains": []}'),'invalid_value'),
                           (('set','search_scope','{"foreign_domains": ["../up"]}'),'invalid_value'),(('set','search_scope','{"other": []}'),'invalid_value'),
                           (('set','domains','{}'),'unknown_setting'),(('get','nope'),'unknown_setting')):
            with self.subTest(argv=argv):self.assertTrue(self.error(*argv)['message'].startswith(start+': '))
        self.assertEqual((self.root/'bindings.json').read_bytes(),before)

    def test_set_is_for_the_main_agent_only(self):
        for actor in ('subagent','viewer'):
            with self.subTest(actor=actor):
                self.assertIn('read-only',self.error('set','auto_resolve','false',actor=actor)['message'])
                self.assertEqual(self.run_cli('get',actor=actor)[0],0)

    def test_undo_restores_the_previous_file(self):
        before=(self.root/'bindings.json').read_text(encoding='utf-8')
        op=self.ok('set','cross_project','false')['operation_id']
        planned=undo(self.root,op)
        from core.governance import apply
        apply(self.root,planned['plan_id'],planned['digest'])
        self.assertEqual((self.root/'bindings.json').read_text(encoding='utf-8'),before)
