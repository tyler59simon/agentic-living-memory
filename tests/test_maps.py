import contextlib,io,json,os,shutil,subprocess,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core import runtime
from core.cli import main
from core.common import load_bindings
from core.maps import named_repos,find_map,repo_of,candidate_repos,changed_since,cited_paths,currency,freshness
from core.notes import find_note,change
from core import usage
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
        self.prompt('keep looking');self.tool('Glob',{'pattern':'**/*.py','path':str(self.hive)});self.tool('Write',{'file_path':str(self.project/'notes.txt')})
        stop=self.hook('stop')
        self.assertIn('agentic-hivemind, which has no codebase map',stop['continue_reason'])
        self.assertIn("CMD capture --session s1 --type map --file '"+self.hive.as_posix()+"'",stop['continue_reason'])
        self.assertLess(len(stop['continue_reason']),600)   # Claude Code prints it in the terminal
        self.prompt('and more');[self.tool('Read',{'file_path':str(self.hive/f'h{i}.py')}) for i in range(4)]
        self.assertEqual(self.hook('stop'),{})

    def test_a_read_only_session_is_not_reminded_to_map_but_a_later_edit_does(self):
        self.hook('session-start');self.prompt('look around')
        for i in range(4):self.tool('Read',{'file_path':str(self.hive/f'f{i}.py')})
        self.assertTrue(self.state()['repos'][self.hive.as_posix()]['explored'])   # explore() still counts read-only calls
        self.assertEqual(self.hook('stop'),{})
        self.prompt('now fix it');self.tool('Write',{'file_path':str(self.project/'notes.txt')})
        self.assertIn('agentic-hivemind, which has no codebase map',self.hook('stop')['continue_reason'])

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
        self.assertEqual(self.hook('stop'),{})   # nothing was edited: no housekeeping
        self.prompt('and fix it');self.tool('Write',{'file_path':str(self.project/'notes.txt')})
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
        self.prompt('go ahead');self.tool('Write',{'file_path':str(self.project/'notes.txt')})
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
        self.assertIn('One codebase map (type map) per repo: read it before exploring, capture or update it after',PROTOCOL)
        self.assertNotIn('anything git, code or docs already record',PROTOCOL)
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

GIT=shutil.which('git')
LONG_AGO='2020-01-01T00:00:00+00:00'

def later(hours=1):return (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat(timespec='seconds')

def git(repo,*args,when=None):
    env={**os.environ,'GIT_AUTHOR_NAME':'t','GIT_AUTHOR_EMAIL':'t@example.org','GIT_COMMITTER_NAME':'t','GIT_COMMITTER_EMAIL':'t@example.org'}
    if when:env.update(GIT_AUTHOR_DATE=when,GIT_COMMITTER_DATE=when)
    return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,env=env,text=True).stdout

def commit(repo,files,when,message='change'):
    for name,text in files.items():
        path=Path(repo)/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
    git(repo,'add','-A');git(repo,'commit','-q','-m',message,when=when)

@unittest.skipUnless(GIT,'git is not installed')
class Staleness(StoreFixture):
    """A map or brief is stale when a file it cites changed after it was written (core/maps.changed_since, cited_paths, currency)."""
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','concern/architecture','concern/decision','concern/knowledge']}))
        self.project=self.base/'project';self.app=self.project/'app';self.app.mkdir(parents=True)
        git(self.app,'init','-q');git(self.app,'symbolic-ref','HEAD','refs/heads/main')
        self.app=self.app.resolve()
        commit(self.app,{'pom.xml':'<v>1</v>','README.md':'one','src/Money.java':'long cents;','src/Other.java':'x','docs/adr/ADR-004-money.md':'# ADR-004\nlong cents\n'},LONG_AGO,'initial')
        self.payload={'cwd':str(self.app),'session_id':'s1'}
        self.bindings=load_bindings(self.root)
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),'--domain','demo',*argv])
        return code,json.loads(out.getvalue() or err.getvalue())
    def capture(self,kind='map',*extra):
        code,made=self.cli('capture','--type',kind,'--file',str(self.app),'--title',f'app {kind}','--summary','A money service.',
                           '--detail','**Layout:** src/Money.java holds the money type; src/ is the code.' if kind=='map' else '**Key decisions:** Money is long cents.',*extra)
        self.assertEqual(code,0,made);self.ref='demo:'+made['id'];self.short=self.ref[:13]
        return made
    def hit(self,kind='map'):return find_map(self.app,self.root,load_bindings(self.root),kind=kind)
    def adr_changed(self,when=None):
        commit(self.app,{'docs/adr/ADR-004-money.md':'# ADR-004\nBigDecimal\n','pom.xml':'<v>2</v>','README.md':'two'},when or later(),'adr four')
    def hook(self,event,**extra):return runtime.process('claude',event,{**self.payload,**extra},self.root)
    def prompt(self,text='work on the app',**extra):return self.hook('user-prompt',prompt=text,**extra)
    def tool(self,name,args,**extra):return self.hook('pre-tool-use',tool_name=name,tool_input=args,**extra)
    def state(self):return json.loads(runtime._pointer(self.root,'s1').read_text(encoding='utf-8'))

    def test_a_changed_decision_doc_and_build_file_make_the_map_stale(self):
        self.capture();self.adr_changed()
        hit=self.hit();changed=changed_since(self.app,hit['updated'])
        self.assertEqual(changed,['README.md','docs/adr/ADR-004-money.md','pom.xml'])
        self.assertEqual(cited_paths(hit,changed,self.app,self.bindings),['docs/adr/ADR-004-money.md','pom.xml'])
        day=hit['updated'][:10]
        self.assertIn(f'STALE: docs/adr/ADR-004-money.md, pom.xml changed since {day} — check and update this map',freshness(hit,self.app))
        self.assertIn('STALE: docs/adr/ADR-004-money.md, pom.xml changed since',self.hook('session-start')['context'])
        self.assertIn('STALE:',self.tool('Read',{'file_path':str(self.app/'src'/'Money.java')}).get('context',''))   # the pointer a read gets
        self.hook('session-start')
        text=self.prompt('work on the app')['context']
        self.assertIn('Codebase map for app',text);self.assertIn('STALE: docs/adr/ADR-004-money.md, pom.xml changed since',text)
        self.assertEqual(self.state()['repos'][self.app.as_posix()]['fresh']['changed'],['docs/adr/ADR-004-money.md','pom.xml'])

    def test_ordinary_docs_and_ticket_edits_do_not_make_the_map_stale_but_a_decision_record_does(self):
        # Docs-heavy teams edit docs/*.md and tickets every session; only decision records stale a whole-repo summary by themselves.
        self.capture();commit(self.app,{'docs/runbook.md':'steps','team/board/LH-7.md':'ticket','CHANGELOG.md':'v1'},later())
        hit=self.hit();self.assertEqual(cited_paths(hit,changed_since(self.app,hit['updated']),self.app,self.bindings),[])
        commit(self.app,{'docs/decisions/0007-fees.md':'cap'},later(2))
        self.assertEqual(cited_paths(self.hit(),changed_since(self.app,self.hit()['updated']),self.app,self.bindings),['docs/decisions/0007-fees.md'])

    def test_changes_nobody_cites_do_not_make_it_stale_but_a_named_path_or_a_files_entry_does(self):
        self.capture();commit(self.app,{'README.md':'two','src/Other.java':'y','notes.txt':'z'},later())
        hit=self.hit();self.assertEqual(cited_paths(hit,changed_since(self.app,hit['updated']),self.app,self.bindings),[])
        text=freshness(hit,self.app);self.assertNotIn('STALE',text);self.assertIn('updated '+hit['updated'][:10],text);self.assertIn('commit',text)
        self.assertNotIn('STALE',self.hook('session-start')['context'])
        commit(self.app,{'src/Money.java':'BigDecimal'},later(2))   # the body names it
        self.assertIn('STALE: src/Money.java changed since',freshness(self.hit(),self.app,currency(self.hit(),self.app,self.bindings)[0]))
        change(hit['id'],'demo','update',self.root,files=['app','app/src/Other.java'])   # now `files` lists another path
        entry=currency(self.hit(),self.app,self.bindings)[0]
        self.assertEqual(entry['changed'],['src/Money.java','src/Other.java'])

    def test_a_merge_commit_is_handled(self):
        self.capture()
        git(self.app,'checkout','-q','-b','feature')
        commit(self.app,{'docs/adr/ADR-004-money.md':'# ADR-004\nBigDecimal\n'},later(1),'adr on a branch')
        git(self.app,'checkout','-q','main');commit(self.app,{'README.md':'main moved'},later(2),'main moved')
        git(self.app,'merge','-q','--no-ff','-m','merge feature','feature',when=later(3))
        self.assertEqual(git(self.app,'log','-1','--name-only','--format=').strip(),'')   # what `log --name-only` would have said of the merge
        hit=self.hit()
        self.assertEqual(changed_since(self.app,hit['updated']),['README.md','docs/adr/ADR-004-money.md'])
        self.assertIn('STALE: docs/adr/ADR-004-money.md changed since',freshness(hit,self.app))

    def test_a_cache_hit_runs_no_git_and_a_new_commit_or_revision_recomputes(self):
        self.capture();self.adr_changed();hit=self.hit()
        entry,computed=currency(hit,self.app,self.bindings)
        self.assertTrue(computed);self.assertEqual(entry['head'],git(self.app,'rev-parse','HEAD').strip());self.assertEqual(entry['rev'],hit['revision'])
        with patch('core.maps.subprocess.run') as run:
            again,computed=currency(hit,self.app,self.bindings,entry)
            self.assertFalse(computed);self.assertIs(again,entry);run.assert_not_called()
        commit(self.app,{'src/Other.java':'z'},later(2))
        self.assertTrue(currency(hit,self.app,self.bindings,entry)[1])
        change(hit['id'],'demo','update',self.root,body=hit['body']+'\n\n* **Extra:** more.')
        self.assertTrue(currency(self.hit(),self.app,self.bindings,entry)[1])
        # Through the hooks: SessionStart pays once, later pointers and prompts read the session's entry.
        self.hook('session-start')
        calls=[]
        real=subprocess.run
        def counting(*a,**k):calls.append(a[0]);return real(*a,**k)
        with patch('core.maps.subprocess.run',side_effect=counting):
            self.assertIn('STALE:',self.tool('Read',{'file_path':str(self.app/'src'/'Money.java')}).get('context',''))
            self.assertIn('STALE:',self.prompt('work on the app again')['context'])
        self.assertEqual(calls,[])

    def test_without_git_or_with_a_timeout_nothing_is_stale_and_nothing_fails(self):
        self.capture();self.adr_changed();hit=self.hit()
        for error in (subprocess.TimeoutExpired('git',2),FileNotFoundError('git'),OSError('boom')):
            with self.subTest(error=type(error).__name__),patch('core.maps.subprocess.run',side_effect=error):
                self.assertEqual(changed_since(self.app,hit['updated']),[])
                text=freshness(hit,self.app);self.assertNotIn('STALE',text);self.assertIn('updated',text)
                self.assertNotIn('STALE',self.hook('session-start')['context'])
        plain=self.base/'plain';(plain/'.git').mkdir(parents=True)
        self.assertEqual(changed_since(plain,hit['updated']),[]);self.assertEqual(changed_since(self.app,''),[])
        self.assertEqual(changed_since(self.base/'missing',hit['updated']),[])
        # A repo with nothing before the note was written cannot say what changed.
        fresh=self.base/'fresh';fresh.mkdir();git(fresh,'init','-q');commit(fresh,{'a.txt':'a'},later())
        self.assertEqual(changed_since(fresh,hit['updated']),[])

    def test_the_changed_list_is_capped_and_the_names_shown_are_capped(self):
        with patch('core.maps._git',side_effect=['abc\n','\n'.join(f'f{i}.txt' for i in range(600))]):
            self.assertEqual(len(changed_since(self.app,'2026-01-01')),500)
        names=[f'docs/adr/ADR-00{i}.md' for i in range(1,6)]
        text=freshness({'updated':'2026-11-10T00:00:00+00:00'},self.app,{'changed':names,'count':5})
        self.assertIn('STALE: docs/adr/ADR-001.md, docs/adr/ADR-002.md, docs/adr/ADR-003.md and 2 more changed since 2026-11-10 — check and update this map',text)
        self.assertIn('5 commits since',text)

    def edit_app(self):self.tool('Edit',{'file_path':str(self.app/'src'/'Money.java')})

    def test_stop_asks_once_per_session_to_update_a_stale_map(self):
        self.capture();self.adr_changed();self.hook('session-start');self.prompt()
        self.edit_app()
        stop=self.hook('stop')
        self.assertEqual(self.state()['repos'][self.app.as_posix()]['fresh']['ref'],self.ref)
        reason=stop['continue_reason']
        self.assertIn(f'map {self.short} is stale (docs/adr/ADR-004-money.md, pom.xml changed since it was written): CMD read {self.short}, then CMD update {self.short} --session s1',reason)
        self.assertIn("--set-detail '**Conventions:** ...' for each bullet that changed.",reason)
        self.assertEqual([r['k'] for r in usage.events(self.root) if r['e']=='reminder'],['stale'])
        self.prompt('next');self.edit_app()
        self.assertEqual(self.hook('stop'),{})   # once per note per session
        self.assertIn(self.ref,self.state()['stale_told'])

    def test_stop_stays_quiet_when_the_map_was_updated_the_turn_did_not_touch_the_repo_or_nothing_cited_changed(self):
        self.capture();self.adr_changed();self.hook('session-start');self.prompt()
        self.edit_app()
        code,_=self.cli('update',self.ref,'--set-detail','**Conventions:** Money is BigDecimal.')   # no session: no receipt, the revision moved
        self.assertEqual(code,0)
        self.assertEqual(self.hook('stop'),{})
        self.assertNotIn('stale_told',self.state())
        # A turn that touched nothing in the repo owes nothing.
        self.hook('session-start',session_id='s2');self.hook('user-prompt',session_id='s2',prompt='talk')
        self.assertEqual(self.hook('stop',session_id='s2'),{})
        self.hook('user-prompt',session_id='s2',prompt='edit elsewhere');self.hook('pre-tool-use',session_id='s2',tool_name='Edit',tool_input={'file_path':str(self.project/'notes.txt')})
        self.assertNotIn('is stale',self.hook('stop',session_id='s2').get('continue_reason',''))

    def test_a_stale_brief_is_flagged_and_asked_about_too(self):
        self.capture('brief');self.adr_changed()
        start=self.hook('session-start')['context']
        self.assertIn('Project brief:',start);self.assertIn('STALE: docs/adr/ADR-004-money.md, pom.xml changed since',start);self.assertIn('check and update this brief',start)
        text=self.prompt()['context'];self.assertIn('Project brief — '+self.short,text);self.assertIn('STALE:',text)
        self.assertEqual(self.state()['repos'][self.app.as_posix()]['fresh_brief']['ref'],self.ref)
        self.edit_app()
        reason=self.hook('stop')['continue_reason']
        self.assertIn(f'project brief {self.short} is stale (docs/adr/ADR-004-money.md, pom.xml changed since it was written)',reason)
        self.assertIn("--set-detail '**Key decisions:** ...'",reason)

    def test_a_note_shared_from_another_clone_is_checked_against_the_active_checkout(self):
        remote='https://github.com/acme/app.git'
        clone=self.project/'clone';git(self.project,'clone','-q',str(self.app),str(clone));clone=clone.resolve()
        git(clone,'remote','set-url','origin',remote);git(self.app,'remote','add','origin',remote)
        map_short=self.capture()['id'][:8];self.capture('brief')
        commit(clone,{'docs/adr/ADR-004-money.md':'# ADR-004\nBigDecimal\n','pom.xml':'<v>2</v>'},later(),'adr four in the clone')
        self.assertEqual(self.hit('brief')['repo'],'github.com/acme/app')   # the notes are shared by identity, but the original checkout is untouched
        extra={'cwd':str(clone)}
        start=self.hook('session-start',**extra)['context']
        line=next(l for l in start.splitlines() if l.startswith('Project brief:'))
        self.assertIn('STALE: docs/adr/ADR-004-money.md, pom.xml changed since',line)
        text=self.prompt(**extra)['context']
        self.assertIn('Project brief — ',text);self.assertEqual(text.count('STALE:'),2)   # the map and the brief
        repos=self.state()['repos']
        self.assertEqual(repos[clone.as_posix()]['fresh_brief']['changed'],['docs/adr/ADR-004-money.md','pom.xml'])
        self.assertNotIn(self.app.as_posix(),repos)
        self.tool('Edit',{'file_path':str(clone/'src'/'Money.java')},**extra)
        reason=self.hook('stop',**extra)['continue_reason']
        self.assertIn(f'project brief {self.short} is stale',reason);self.assertIn(f'map demo:{map_short} is stale',reason)

    def test_a_map_and_a_brief_are_both_reported_in_one_reminder(self):
        self.capture();self.capture('brief');self.adr_changed()
        self.hook('session-start');self.prompt();self.edit_app()
        reason=self.hook('stop')['continue_reason']
        self.assertEqual(reason.count('is stale'),2);self.assertEqual(reason.count('Living Memory:'),1)
        self.assertIn('Also: ',reason)

if __name__=='__main__':unittest.main()
