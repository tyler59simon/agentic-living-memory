import contextlib,io,json,unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core import runtime
from core.cli import main
from core.common import load_bindings
from core.maps import named_repos,find_map,repo_of,candidate_repos
from core.notes import find_note
from core.init_domain import init_domain

class Maps(StoreFixture):
    def setUp(self):
        super().setUp()
        self.project=self.base/'project'
        for repo in ('architecture-suite-plugin','agentic-hivemind'):(self.project/repo/'.git').mkdir(parents=True)
        (self.project/'architecture-suite-plugin'/'runtime').mkdir()
        self.suite=(self.project/'architecture-suite-plugin').resolve();self.hive=(self.project/'agentic-hivemind').resolve()
        self.payload={'cwd':str(self.project),'session_id':'s1'}
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),*argv])
        return code,json.loads(out.getvalue() or err.getvalue())
    def capture_map(self,repo=None,domain='demo',title='architecture-suite-plugin codebase map',*extra):
        return self.cli('--domain',domain,'capture','--type','map','--file',str(repo or self.suite),'--title',title,
                        '--summary','A C4 diagram plugin: skills generated from one runtime.','--detail','**Layout:** runtime/ holds the CLI; skills/ is generated.',*extra)
    def hook(self,event,**extra):return runtime.process('claude',event,{**self.payload,**extra},self.root)
    def prompt(self,text,**extra):return self.hook('user-prompt',prompt=text,**extra).get('context','')
    def tool(self,name,args,**extra):return self.hook('pre-tool-use',tool_name=name,tool_input=args,**extra)
    def state(self):return json.loads(runtime._pointer(self.root,'s1').read_text())

    def test_map_capture_defaults_key_and_dedup(self):
        code,result=self.capture_map()
        self.assertEqual(code,0,result)
        note=find_note(result['id'],'demo',self.root)[1]
        self.assertEqual(note['type'],'map');self.assertEqual(note['files'],['architecture-suite-plugin'])
        self.assertIn('concern/architecture',note['tags']);self.assertIn('subject/project',note['tags'])
        self.assertTrue(note.get('review_by'))
        self.assertEqual(repo_of(note,'demo',load_bindings(self.root)),self.suite)
        code,again=self.capture_map(None,'demo','Suite plugin structure overview','--allow-similar')
        self.assertNotEqual(code,0);self.assertIn('map_exists conflict',json.dumps(again))
        code,missing=self.cli('--domain','demo','capture','--type','map','--title','Loose map','--summary','No repo named.')
        self.assertNotEqual(code,0);self.assertIn('--file',json.dumps(missing))

    def test_map_for_a_repo_bound_as_its_own_domain_is_stored_as_dot_and_found_from_the_parent(self):
        solo=(self.project/'architecture-suite-plugin').resolve()
        init_domain('suite',[str(solo)],self.root)
        code,result=self.capture_map(solo,'suite')
        self.assertEqual(code,0,result)
        self.assertEqual(find_note(result['id'],'suite',self.root)[1]['files'],['.'])
        hit=find_map(solo,self.root,load_bindings(self.root))
        self.assertEqual((hit['domain'],hit['id']),('suite',result['id']))

    def test_repo_names_match_leniently_without_common_word_false_positives(self):
        candidates=candidate_repos(self.project,load_bindings(self.root))
        self.assertEqual(set(candidates),{self.suite,self.hive})
        cases={'I want to add a feature to architecture suite':[self.suite],
               'fix the architecture-suite-plugin explorer':[self.suite],
               'hivemind phase 3 then the suite plugin':[self.hive,self.suite],
               'add a plugin for memory architecture':[],
               'what does the architecture look like':[],
               'agentic workflows are fun':[]}
        for text,expected in cases.items():
            with self.subTest(text=text):self.assertEqual(named_repos(text,candidates),expected)
        self.assertEqual(named_repos('the c4 tool',candidates,{'architecture-suite-plugin':['c4 tool']}),[self.suite])

    def test_prompt_injects_the_map_once_per_revision_outside_the_hit_cap(self):
        code,result=self.capture_map();ref='demo:'+result['id']
        self.hook('session-start')
        text=self.prompt('add a feature to architecture suite')
        self.assertIn('Codebase map for architecture-suite-plugin',text);self.assertIn('runtime/ holds the CLI',text)
        self.assertEqual(text.count(result['id'][:8]),1)
        self.assertEqual(self.state()['repos'][self.suite.as_posix()]['map'],ref)
        self.assertNotIn('Codebase map for',self.prompt('another feature for architecture suite'))
        code,_=self.cli('--domain','demo','update',ref,'--detail','**Layout:** runtime/ and skills/ and examples/.')
        self.assertEqual(code,0)
        self.assertIn('examples/',self.prompt('architecture suite again'))

    def test_unmapped_repo_gets_one_line_once(self):
        self.hook('session-start')
        text=self.prompt('explore the hivemind app')
        self.assertIn('agentic-hivemind has no codebase map yet',text);self.assertIn('--type map',text)
        self.assertNotIn('no codebase map',self.prompt('more hivemind work'))

    def test_reads_in_a_mapped_repo_hint_once_and_not_after_injection(self):
        self.capture_map();self.hook('session-start');self.prompt('hello there friend')
        target=str(self.suite/'runtime'/'cli.py')
        first=self.tool('Read',{'file_path':target})
        self.assertIn('Codebase map for architecture-suite-plugin',first.get('context',''))
        with patch('core.maps.find_map') as looked:
            self.assertNotIn('context',self.tool('Grep',{'pattern':'x','path':str(self.suite)}))
            looked.assert_not_called()
        self.prompt('add a feature to architecture suite')   # injects the body
        self.hook('session-start')                              # a new context: hints are owed again
        self.prompt('architecture suite')                       # ... but the body was just injected
        self.assertNotIn('context',self.tool('Read',{'file_path':target}))

    def test_subagents_get_their_own_hint_and_the_map_at_start(self):
        code,result=self.capture_map();self.hook('session-start');self.prompt('add a feature to architecture suite')
        start=self.hook('subagent-start',agent_id='a1')
        self.assertIn(result['id'][:8],start['context'])
        spawn=self.tool('Agent',{'description':'Explore suite','prompt':'Map the architecture suite plugin runtime'})
        self.assertNotIn('context',spawn)   # the brief is already written when PreToolUse fires; SubagentStart hands over the map
        sub=self.tool('Read',{'file_path':str(self.suite/'runtime'/'cli.py')},agent_id='a1')
        self.assertIn('Codebase map',sub.get('context',''))

    def test_exploring_an_unmapped_repo_reminds_once(self):
        self.hook('session-start');self.prompt('look around')
        for i in range(2):self.tool('Read',{'file_path':str(self.hive/f'f{i}.py')})
        self.assertEqual(self.hook('stop'),{})
        self.prompt('keep looking');self.tool('Glob',{'pattern':'**/*.py','path':str(self.hive)})
        stop=self.hook('stop')
        self.assertIn('agentic-hivemind, which has no codebase map',stop['continue_reason'])
        self.assertIn("CMD capture --session s1 --type map --file '"+self.hive.as_posix()+"'",stop['continue_reason'])
        self.assertLess(len(stop['continue_reason']),600)   # Claude Code prints it in the terminal
        self.prompt('and more');[self.tool('Read',{'file_path':str(self.hive/f'h{i}.py')}) for i in range(4)]
        self.assertEqual(self.hook('stop'),{})

    def test_repos_outside_every_binding_are_not_tracked(self):
        outside=self.base/'elsewhere';(outside/'.git').mkdir(parents=True)
        self.hook('session-start');self.prompt('look around')
        for i in range(3):self.tool('Read',{'file_path':str(outside/f'f{i}.py')})
        self.assertNotIn(outside.resolve().as_posix(),self.state().get('repos',{}))
        self.assertEqual(self.hook('stop'),{})

    def test_subagent_reads_and_spawns_count_for_the_parent(self):
        self.hook('session-start');self.prompt('look around')
        self.tool('Agent',{'description':'Explore','prompt':'Map the hivemind repo'})
        self.assertTrue(self.state()['repos'][self.hive.as_posix()]['explored'])
        self.assertNotIn('mutating',self.state())
        self.assertIn('hivemind',self.hook('stop')['continue_reason'])

    def test_capturing_the_map_quiets_the_reminder(self):
        self.hook('session-start');self.prompt('look around')
        for i in range(3):self.tool('Read',{'file_path':str(self.hive/f'f{i}.py')})
        self.capture_map(self.hive,'demo','agentic-hivemind codebase map')
        self.assertEqual(self.hook('stop'),{})

    def test_plan_mode_defers_the_reminder(self):
        self.hook('session-start');self.prompt('plan it',permission_mode='plan')
        for i in range(3):self.tool('Read',{'file_path':str(self.hive/f'f{i}.py')},permission_mode='plan')
        self.assertEqual(self.hook('stop',permission_mode='plan'),{})
        self.prompt('go ahead')
        self.assertIn('hivemind',self.hook('stop')['continue_reason'])

    def test_new_top_level_paths_in_a_mapped_repo_remind_although_file_changes_alone_do_not(self):
        code,result=self.capture_map();self.hook('session-start');self.prompt('change it')
        self.tool('Write',{'file_path':str(self.suite/'runtime'/'new.py')})
        self.tool('Write',{'file_path':str(self.suite/'exporters'/'vsdx.py')})
        self.assertEqual(self.state()['repos'][self.suite.as_posix()]['created'],['exporters'])
        stop=self.hook('stop')['continue_reason']
        self.assertIn('new top-level paths in architecture-suite-plugin (exporters)',stop);self.assertIn('CMD update',stop)
        self.prompt('change it again');self.tool('Write',{'file_path':str(self.suite/'runtime'/'other.py')})
        self.assertEqual(self.hook('stop'),{})   # files_only: quiet, and the grown paths were settled
        self.assertIn(result['id'][:8],stop)

    def test_session_start_points_at_the_cwd_repo_map(self):
        code,result=self.capture_map()
        text=runtime.process('claude','session-start',{'cwd':str(self.suite/'runtime'),'session_id':'s2'},self.root)['context']
        self.assertIn('Codebase map for architecture-suite-plugin',text);self.assertIn('A C4 diagram plugin',text)

    def test_protocol_carves_the_codebase_map_out_of_the_skip_rule(self):
        from core.instructions import PROTOCOL,usage
        self.assertIn('Exception: one codebase map (type map) per repo',PROTOCOL)
        self.assertIn('map',usage())   # the type list; the full capture shape comes with each map hint and reminder

    def test_fast_path_answers_only_what_the_runtime_has_settled(self):
        import os
        from core.hookfast import quick
        self.capture_map();self.hook('session-start');self.prompt('hello there friend')
        read={**self.payload,'tool_name':'Read','tool_input':{'file_path':str(self.suite/'runtime'/'cli.py')}}
        with patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(self.root)}):
            self.assertIsNone(quick(read))                      # the hint is still owed
            self.tool('Read',read['tool_input'])
            self.assertEqual(quick(read),{})                     # runtime._pointer and the fast path agree
            self.assertIsNone(quick({**read,'agent_id':'a9'}))   # a subagent has its own hint
            self.assertIsNone(quick({**read,'tool_name':'Write'}))
            hive={**read,'tool_input':{'file_path':str(self.hive/'x.py')}}
            self.assertIsNone(quick(hive))
            for i in range(3):self.tool('Read',{'file_path':str(self.hive/f'x{i}.py')})
            self.assertEqual(quick({**hive,'agent_id':'a9'}),{})   # explored: nothing left for anyone

if __name__=='__main__':unittest.main()
