import contextlib,hashlib,io,json,sqlite3,unittest
from pathlib import Path
from test_core import StoreFixture
from core import runtime
from core.cli import main
from core.notes import find_note
from core.search import rebuild_index,file_notes,mentions_file
from core.sync import build_overlay,sync

class Provenance(StoreFixture):
    def setUp(self):
        super().setUp()
        self.project=self.base/'project';(self.project/'core').mkdir(parents=True)
        self.scratch=self.base/'scratch';self.scratch.mkdir()
        self.payload={'cwd':str(self.project),'session_id':'s1','prompt':'edit things','scratchpad_dir':str(self.scratch)}
        runtime.process('claude','session-start',self.payload,self.root);runtime.process('claude','user-prompt',self.payload,self.root)
    def tool(self,name,args,**extra):
        return runtime.process('claude','pre-tool-use',{**self.payload,**extra,'tool_name':name,'tool_input':args},self.root)
    def state(self):return json.loads(runtime._pointer(self.root,'s1').read_text())
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),*argv])
        return code,json.loads(out.getvalue() or err.getvalue())

    def test_write_tools_record_relative_paths(self):
        self.tool('Edit',{'file_path':str(self.project/'core'/'cli.py')})
        self.tool('Write',{'file_path':'core/new.py'})
        self.tool('Edit',{'file_path':str(self.base/'outside.txt')})
        state=self.state()
        self.assertEqual(state['touched'],['core/cli.py','core/new.py',(self.base/'outside.txt').resolve().as_posix()])
        self.assertEqual(state['harness'],'claude')
    def test_scratchpad_store_and_reads_are_skipped(self):
        self.tool('Write',{'file_path':str(self.scratch/'tmp.md')})
        self.tool('Edit',{'file_path':str(self.root/'hooks'/'x.json')})
        self.tool('Read',{'file_path':str(self.project/'core'/'cli.py')})
        self.assertEqual(self.state().get('touched',[]),[])
    def test_shell_counts_only_project_files(self):
        target=self.project/'core'/'cli.py';target.write_text('x')
        self.tool('Bash',{'command':f'cp a.txt "{target}" && curl https://example.com/api /dev/null'})
        self.assertEqual(self.state()['touched'],['core/cli.py'])
    def test_subagent_and_plan_mode_record_nothing(self):
        self.tool('Edit',{'file_path':'core/a.py'},agent_id='sub')
        self.tool('Edit',{'file_path':'core/b.py'},permission_mode='plan')
        self.assertNotIn('touched',self.state())
    def test_touched_is_capped_and_cleared_at_stop(self):
        for i in range(40):self.tool('Edit',{'file_path':f'core/f{i}.py'})
        state=self.state();self.assertEqual(len(state['touched']),runtime.MAX_TOUCHED);self.assertEqual(len(state['touched_session']),40)
        from core.capture import finalize
        finalize('s1',state['turn'],[],self.root);runtime.process('claude','stop',self.payload,self.root)
        state=self.state();self.assertNotIn('touched',state);self.assertEqual(len(state['touched_session']),40)

    def test_files_auto_explicit_and_suggested(self):
        self.tool('Edit',{'file_path':'core/cli.py'});self.tool('Edit',{'file_path':'core/notes.py'})
        code,result=self.cli('capture','--session','s1','--type','decision','--concern','decision','--title','CLI parses refs','--summary','A fact.','--file','core/cli.py')
        self.assertEqual(code,0,result);self.assertEqual(result['suggested_files'],['core/notes.py'])
        note=find_note(result['id'],'demo',self.root)[1];self.assertEqual(note['files'],['core/cli.py'])
        code,result=self.cli('update','demo:'+note['id'],'--session','s1','--files','auto')
        self.assertEqual(code,0,result);self.assertNotIn('suggested_files',result)
        self.assertEqual(find_note(note['id'],'demo',self.root)[1]['files'],['core/cli.py','core/notes.py'])
        code,result=self.cli('update','demo:'+note['id'],'--files','none')
        self.assertEqual(code,0,result);self.assertNotIn('files',find_note(note['id'],'demo',self.root)[1])
    def test_absolute_file_flag_is_made_relative(self):
        code,result=self.cli('--domain','demo','capture','--concern','decision','--title','Abs path note','--summary','A fact.','--file',str(self.project/'core'/'x.py'))
        self.assertEqual(code,0,result);self.assertEqual(find_note(result['id'],'demo',self.root)[1]['files'],['core/x.py'])
    def test_files_auto_needs_session(self):
        code,result=self.cli('--domain','demo','capture','--concern','decision','--title','No session','--summary','A fact.','--files','auto')
        self.assertEqual(code,2);self.assertIn('--session',result['error']['message'])
    def test_origin_is_hashed(self):
        code,result=self.cli('capture','--session','s1','--concern','decision','--title','Origin note','--summary','A fact.')
        self.assertEqual(code,0,result)
        text=Path(find_note(result['id'],'demo',self.root)[0]).read_text(encoding='utf-8')
        self.assertIn('origin: "claude:'+hashlib.sha256(b's1').hexdigest()[:12]+'"',text);self.assertNotIn('"s1"',text)
        # files follows context in the person-facing properties; origin is bookkeeping
        self.assertNotIn('files:',text)
    def test_frontmatter_order(self):
        n=self.note(files=['core/cli.py'],origin='claude:abc')
        lines=[l.split(':')[0] for l in Path(n['path']).read_text(encoding='utf-8').split('---')[1].strip().splitlines()]
        self.assertEqual(lines[:6],['title','type','status','tags','context','files'])
        self.assertLess(lines.index('capture_key'),lines.index('origin'))

    def test_search_by_file(self):
        a=self.note('CLI gotcha','The parser in core/cli.py is dense.')
        b=self.note('Listed note','Something else.',files=['core/cli.py'])
        self.note('Unrelated','Nothing about score/cli.py here.')
        hits=file_notes('core/cli.py',['demo'],self.root,5)
        self.assertEqual([(h['id'],h['match']) for h in hits],[(b['id'],'files'),(a['id'],'body')])
        self.assertEqual([h['id'] for h in file_notes('core',['demo'],self.root,5)],[b['id']])  # folders match files, not bodies
        code,result=self.cli('--domain','demo','search','--file','core/cli.py')
        self.assertEqual(code,0,result);self.assertEqual([h['id'] for h in result['results']],[b['id'],a['id']])
        code,result=self.cli('--domain','demo','search','note','--file','core/cli.py','--rebuild')
        self.assertEqual(code,0,result);self.assertEqual([h['id'] for h in result['results']],[b['id']])
    def test_file_only_search_honors_scope_and_rejects_filters(self):
        from core.init_domain import init_domain
        from core.notes import capture
        init_domain('other',[str(self.base/'other')],self.root)
        far=capture('Other domain note','Body.',['subject/project','concern/architecture'],'other',self.root,files=['core/cli.py'])
        code,result=self.cli('--domain','demo','search','--file','core/cli.py')
        self.assertEqual((code,result['results']),(0,[]))
        code,result=self.cli('--domain','demo','search','--file','core/cli.py','--scope','all')
        self.assertEqual(code,0,result);self.assertEqual([h['id'] for h in result['results']],[far['id']])
        code,result=self.cli('--domain','demo','search','--file','core/cli.py','--filter','type=bug')
        self.assertEqual(code,2);self.assertIn('--filter needs a query',result['error']['message'])
        self.assertFalse((self.root/'domains'/'missing').exists())
    def test_file_search_all_scope_includes_global_domains_and_children(self):
        from core.init_domain import init_domain
        from core.notes import capture,ensure_global
        ensure_global(self.root);ours=capture('Global file note','Body.',['subject/project','concern/architecture'],'_global',self.root,files=['core/cli.py'])
        child=self.base/'project'/'pkg';child.mkdir(parents=True);init_domain('pkg',[str(child)],self.root)
        kid=capture('Child file note','Body.',['subject/project','concern/architecture'],'pkg',self.root,files=['core/cli.py'])
        code,result=self.cli('--domain','demo','search','--file','core/cli.py','--scope','all')
        self.assertEqual(code,0,result);self.assertEqual({h['id'] for h in result['results']},{ours['id']});self.assertIn('_global',result['domains'])   # the child's core/cli.py is pkg/core/cli.py from here
        code,result=self.cli('--domain','demo','search','--file','pkg/core/cli.py','--scope','all')
        self.assertEqual(code,0,result);self.assertEqual({h['id'] for h in result['results']},{kid['id']})
        code,result=self.cli('--domain','demo','search','--file','pkg/core/cli.py')   # relative to the session domain; the child names it core/cli.py
        self.assertEqual(code,0,result);self.assertEqual([h['id'] for h in result['results']],[kid['id']])
    def test_file_search_translates_child_paths_with_a_query_and_for_folders(self):
        from core.init_domain import init_domain
        from core.notes import capture
        from core.runtime import child_path
        from core.common import load_bindings
        child=self.base/'project'/'pkg';child.mkdir(parents=True);init_domain('pkg',[str(child)],self.root)
        kid=capture('Parser notes','The parser collects errors.',['subject/project','concern/architecture'],'pkg',self.root,files=['src/parser.py'])
        own=self.note('Parser note elsewhere','The parser in src/parser.py of the project.',files=['src/parser.py'])
        code,result=self.cli('--domain','demo','search','parser','--file','pkg/src/parser.py')
        self.assertEqual(code,0,result);self.assertEqual([h['id'] for h in result['results']],[kid['id']])   # not the project's own src/parser.py note
        code,result=self.cli('--domain','demo','search','parser','--file','src/parser.py')
        self.assertEqual(code,0,result);self.assertEqual([h['id'] for h in result['results']],[own['id']])
        code,result=self.cli('--domain','demo','search','parser','--file','pkg')
        self.assertEqual(code,0,result);self.assertEqual([h['id'] for h in result['results']],[kid['id']])
        code,result=self.cli('--domain','demo','search','--file','pkg')   # the child's root: every file its notes name
        self.assertEqual(code,0,result);self.assertEqual([h['id'] for h in result['results']],[kid['id']])
        config=load_bindings(self.root);self.assertEqual((child_path('pkg','demo','pkg',config),child_path('pkg/src','demo','pkg',config),child_path('other','demo','pkg',config)),('','src',None))
        for extra in ((),('parser',)):   # --scope all still translates a child's paths
            code,result=self.cli('--domain','demo','search',*extra,'--file','pkg/src/parser.py','--scope','all')
            self.assertEqual(code,0,result);self.assertIn(kid['id'],[h['id'] for h in result['results']])
        code,result=self.cli('--domain','demo','read','Parser notes')   # an exact title resolves in a child domain too
        self.assertEqual(code,0,result);self.assertEqual(result['id'],kid['id'])
    def test_mentions_need_a_path_token(self):
        self.assertTrue(mentions_file('see core\\cli.py.','core/cli.py'))
        self.assertFalse(mentions_file('see core/cli.pyc','core/cli.py'))
        self.assertFalse(mentions_file('the cli tool','cli'))
    def test_old_index_is_rebuilt(self):
        self.note();rebuild_index('demo',self.root)
        db=sqlite3.connect(self.root/'domains/demo/search.sqlite3');db.execute('UPDATE metadata SET fingerprint=?',('old',));db.commit();db.close()
        from core.search import _fresh
        from core.repository import inventory
        self.assertFalse(_fresh('demo',self.root,inventory('demo',self.root,retired=True)))
        file_notes('x/y.py',['demo'],self.root);self.assertTrue(_fresh('demo',self.root,inventory('demo',self.root,retired=True)))

    def test_vault_edit_of_files_round_trips(self):
        n=self.note(files=['core/cli.py'])
        build_overlay('demo',self.base/'vault',self.root)
        copy=self.base/'vault/Domains/demo'/Path(n['path']).name
        text=copy.read_text(encoding='utf-8')
        copy.write_text(text.replace('files: ["core/cli.py"]','files:\n  - core/cli.py\n  - core/notes.py'),encoding='utf-8')
        sync('demo',self.base/'vault',self.root)
        self.assertEqual(find_note(n['id'],'demo',self.root)[1]['files'],['core/cli.py','core/notes.py'])

if __name__=='__main__':unittest.main()
