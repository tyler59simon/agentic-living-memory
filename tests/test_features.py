import io,json,os,subprocess,sys,unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core.repository import read_json,parse,serialize,transaction,recover,inventory
from core.notes import change,find_note
from core.governance import apply,remap
from core.sync import sync,build_overlay
from core import runtime

class Features(StoreFixture):
    def test_sync_baseline_and_auto_applied_update(self):
        a=self.note();vault=self.base/'vault';dst=vault/'Domains/demo'/Path(a['path']).name;dst.parent.mkdir(parents=True)
        dst.write_text(Path(a['path']).read_text());sync('demo',vault,self.root)
        dst.write_text(dst.read_text().replace('rebuildable','replaceable'))
        result=sync('demo',vault,self.root)
        self.assertEqual(result['conflicts'],0);self.assertEqual(result['plans'],[]);self.assertEqual(len(result['applied']),1)
        self.assertIn('replaceable',find_note(a['id'],'demo',self.root)[1]['body'])
        self.assertEqual(sync('demo',vault,self.root)['conflicts'],0)
    def test_canvas_and_six_dashboards(self):
        a=self.note();vault=self.base/'vault';result=build_overlay('demo',vault,self.root)
        self.assertEqual(result['dashboards'],7);self.assertEqual(result['canvas_nodes'],1)
        change(a['id'],'demo','rename',self.root,title='Renamed')
        canvas=json.loads((vault/'Canvas/demo.canvas').read_text())
        self.assertIn('renamed',canvas['nodes'][0]['file'])
    def test_native_adapters_and_capture_completion(self):
        from hooks import codex_hooks,claude_hooks,copilot_hooks
        cwd=str(self.base/'project');payload={'cwd':cwd,'session_id':'session','turn_id':'turn','prompt':'Please explain cache'}
        self.note()
        with patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(self.root)}):
            a=claude_hooks.handle('session-start',payload);self.assertIn('hookSpecificOutput',a)
            b=copilot_hooks.handle('sessionStart',{'cwd':cwd,'sessionId':'s'});self.assertIn('additionalContext',b)
            c=copilot_hooks.handle('userPromptTransformed',{'cwd':cwd,'sessionId':'s','prompt':'cache','transformedPrompt':'original','timestamp':1});self.assertTrue(c['modifiedTransformedPrompt'].startswith('original'))
        self.assertEqual(runtime.process('codex','stop',payload,self.root),{})  # conversational turn: no receipt demanded
        runtime.process('codex','user-prompt',{**payload,'prompt':'Remember that the cache is explained first'},self.root)
        runtime.process('codex','pre-tool-use',{**payload,'tool_name':'Edit','tool_input':{'file_path':str(self.base/'project/x.py')}},self.root)
        first=runtime.process('codex','stop',payload,self.root);second=runtime.process('codex','stop',payload,self.root)
        self.assertIn('continue_reason',first);self.assertNotIn('continue_reason',second)
        self.assertNotIn('Subagents are read-only',first['continue_reason'])  # one-line reminder, not the protocol
        from core.capture import finalize
        runtime.process('codex','user-prompt',payload,self.root)
        runtime.process('codex','pre-tool-use',{**payload,'tool_name':'Bash','tool_input':{'command':'touch x'}},self.root)
        finalize('session','turn',[],self.root);self.assertEqual(runtime.process('codex','stop',payload,self.root),{})
    def test_delivery_diagnostics_exclude_prompt_content(self):
        payload={'cwd':str(self.base/'project'),'prompt':'private prompt content'}
        runtime.process('codex','user-prompt',payload,self.root)
        delivery=read_json(self.root/'hooks/last-delivery.json')
        self.assertEqual(delivery['cwd'],payload['cwd'])
        self.assertEqual(delivery['domain'],'demo')
        self.assertTrue(delivery['bound'])
        self.assertEqual(delivery['payload_keys'],['cwd','prompt'])
        self.assertNotIn(payload['prompt'],json.dumps(delivery))
    def test_guard_paths_structured_tools_only(self):
        self.assertFalse(runtime.protected('config/bindings.json',str(self.base),self.root))
        cwd=str(self.base/'project');target=f'{self.root}/domains/demo/notes/x.md'
        # Shell text is not guarded: a command regex is bypassable and blocked harmless reads.
        command=f'python -c "Path(\'{target}\').write_text(\'x\')"'
        self.assertNotIn('deny',runtime.process('codex','pre-tool-use',{'cwd':cwd,'tool_name':'Bash','tool_input':{'command':command}},self.root))
        self.assertNotIn('deny',runtime.process('claude','pre-tool-use',{'cwd':cwd,'tool_name':'PowerShell','tool_input':{'command':f'Get-Content "{target}"'}},self.root))
        # Structured file tools and patches still are.
        self.assertIn('deny',runtime.process('claude','pre-tool-use',{'cwd':cwd,'tool_name':'Write','tool_input':{'file_path':target}},self.root))
        self.assertIn('deny',runtime.process('codex','pre-tool-use',{'cwd':cwd,'tool_name':'apply_patch','tool_input':{'input':f'*** Update File: {target}'}},self.root))
    def test_remap_transaction_and_undo(self):
        from core.init_domain import init_domain
        from core.governance import undo
        init_domain('other',[str(self.base/'other')],self.root);a=self.note()
        proposal=remap(self.root,[{'before':{'domain':'demo','id':a['id']},'after':{'domain':'other','id':a['id']}}])
        receipt=apply(self.root,proposal['plan_id'],proposal['digest']);self.assertEqual(find_note(a['id'],'other',self.root)[1]['domain'],'other')
        proposal=undo(self.root,receipt['operation_id']);apply(self.root,proposal['plan_id'],proposal['digest']);self.assertEqual(find_note(a['id'],'demo',self.root)[1]['domain'],'demo')
    def test_remap_keeps_the_verification_baseline(self):
        from core.init_domain import init_domain
        from core.note_types import verified_at
        init_domain('other',[str(self.base/'other')],self.root);(self.base/'project').mkdir(exist_ok=True);(self.base/'project'/'x.py').write_text('x=1',encoding='utf-8')
        a=self.note(files=['x.py']);before=find_note(a['id'],'demo',self.root)[1]
        self.assertTrue(before.get('file_hashes'));self.assertFalse(before.get('checked'))
        proposal=remap(self.root,[{'before':{'domain':'demo','id':a['id']},'after':{'domain':'other','id':a['id']}}])
        apply(self.root,proposal['plan_id'],proposal['digest']);after=find_note(a['id'],'other',self.root)[1]
        self.assertEqual(verified_at(after),verified_at(before))   # a move vouches for nothing
    def test_script_capture_batch_compact_output(self):
        payload=[{'title':str(i),'body':'durable '+str(i),'tags':['subject/project','concern/architecture'],'domain':'demo'} for i in range(2)]
        script=Path(__file__).resolve().parents[1]/'living_memory.py'
        result=subprocess.run([sys.executable,str(script),'capture','--root',str(self.root)],input=json.dumps(payload),text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr);rows=json.loads(result.stdout);self.assertEqual(len(rows),2);self.assertNotIn('body',rows[0])
        denied=subprocess.run([sys.executable,str(script),'capture','--root',str(self.root),'--actor','subagent'],input=json.dumps(payload),text=True,capture_output=True)
        self.assertNotEqual(denied.returncode,0)
    def test_adapter_setup_preserves_unrelated_state(self):
        from integrations.setup import setup_plan
        home=self.base/'claude';home.mkdir();(home/'settings.json').write_text(json.dumps({'theme':'dark','hooks':{}}))
        proposal=setup_plan('claude',home);apply(home,proposal['plan_id'],proposal['digest'])
        self.assertEqual(read_json(home/'settings.json')['theme'],'dark')
        self.assertIn('agentic-living-memory:begin',(home/'CLAUDE.md').read_text())
    def test_process_interruption_recovers(self):
        code="""import os,sys
from pathlib import Path
from core import repository as r
real=r._write
count=[0]
def crash(path,data):
 count[0]+=1
 if count[0]==2:os._exit(31)
 real(path,data)
r._write=crash
r.transaction(Path(sys.argv[1]),{'first.txt':'first','second.txt':'second'},'process_crash',operation_id='process-crash')
"""
        run=subprocess.run([sys.executable,'-c',code,str(self.root)])
        self.assertEqual(run.returncode,31);recover(self.root,'process-crash')
        self.assertEqual((self.root/'second.txt').read_text(),'second')
    def test_simultaneous_script_writers(self):
        script=Path(__file__).resolve().parents[1]/'living_memory.py';processes=[]
        for i in range(4):
            command=[sys.executable,str(script),'capture','--root',str(self.root),'--domain','demo','--tag','subject/project','--tag','concern/architecture',str(i),'parallel '+str(i)]
            processes.append(subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        for process in processes:
            out,error=process.communicate(timeout=30);self.assertEqual(process.returncode,0,error)
        self.assertEqual(len(inventory('demo',self.root)),4)

class ClaudeIntegrationFixes(StoreFixture):
    def run_hook(self,event,payload):
        return runtime.process('claude',event,payload,self.root)
    def payload(self,**extra):
        return {'cwd':str(self.base/'project'),'session_id':'s1',**extra}
    def memory(self,rest):
        return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'
    def test_protocol_injected_once_and_prompt_is_one_line(self):
        start=self.run_hook('session-start',self.payload())['context']
        self.assertIn('Subagents are read-only',start);self.assertIn('auto-memory',start)
        prompt=self.run_hook('user-prompt',self.payload(prompt='thanks'))['context']
        self.assertTrue(prompt.startswith('Living Memory session=s1 turn='));self.assertNotIn('Subagents',prompt)
    def test_session_start_is_short_only_with_the_current_instruction_block(self):
        from core.instructions import instruction_block
        home=Path(os.environ['CLAUDE_HOME']);home.mkdir(parents=True,exist_ok=True)
        (home/'CLAUDE.md').write_text('# mine\n\n'+instruction_block('claude')+'\n',encoding='utf-8')
        short=self.run_hook('session-start',self.payload())['context']
        self.assertNotIn(runtime.PROTOCOL,short);self.assertIn('CMD = ',short);self.assertIn('Living Memory session=s1',short)
        (home/'CLAUDE.md').write_text(instruction_block('claude').replace('Read relevant','Read any'),encoding='utf-8')
        self.assertIn(runtime.PROTOCOL,self.run_hook('session-start',self.payload())['context'])   # stale block: full injection
    def test_subagents_cannot_run_memory_writes(self):
        for rest in ("capture --session s1 --type knowledge --subject project --concern knowledge 'T' 'B'",'capture-finalize --session s1',"update demo:x --body 'B'"):
            with self.subTest(rest=rest):
                event=self.payload(tool_name='Bash',tool_input={'command':self.memory(rest)})
                self.assertIn('deny',self.run_hook('pre-tool-use',{**event,'agent_id':'a1'}))
                self.assertIn('allow',self.run_hook('pre-tool-use',event))
        event=self.payload(tool_name='Bash',tool_input={'command':self.memory("search 'q'")},agent_id='a1')
        self.assertIn('allow',self.run_hook('pre-tool-use',event))
    def test_retrieval_relevance_floor(self):
        self.note()
        self.assertIn('Cache',self.run_hook('user-prompt',self.payload(prompt='How is the cache rebuilt?'))['context'])
        self.assertNotIn('Cache',self.run_hook('user-prompt',self.payload(prompt='continue'))['context'])
        self.assertNotIn('Cache',self.run_hook('user-prompt',self.payload(prompt='refactor the login form validation layout'))['context'])
    def test_allow_and_deny_decisions(self):
        bash=lambda command:self.run_hook('pre-tool-use',self.payload(tool_name='Bash',tool_input={'command':command}))
        self.assertIn('allow',bash(self.memory('search "cache policy"')))
        self.assertIn('allow',bash(self.memory('capture-finalize --session s1 --turn t1')))
        self.assertIn('deny',bash('& '+self.memory('capture-finalize --session s1 --turn t1')))   # Bash has no call operator
        self.assertIn('allow',bash(self.memory('capture --subject project --concern knowledge "Apply rule" "We apply X"')))
        # Plan application gets no hook decision: the host's normal permissions apply.
        self.assertEqual(bash(self.memory('apply 0123 --expected-plan-sha256 abc')),{})
        self.assertEqual(bash(f'"{sys.executable}" integrations/setup.py claude --apply-plan x --expected-plan-sha256 y'),{})
        self.assertNotIn('allow',bash(self.memory('search cache')+' ; rm -rf x'))
        self.assertNotIn('allow',bash(f'"{sys.executable}" "C:/elsewhere/living_memory.py" search cache'))
        edit=self.run_hook('pre-tool-use',self.payload(tool_name='Edit',tool_input={'file_path':str(self.root/'bindings.json')}))
        self.assertIn('deny',edit)
    def test_memory_commands_and_reads_do_not_mark_turn_mutating(self):
        self.run_hook('user-prompt',self.payload(prompt='hello there'))
        self.run_hook('pre-tool-use',self.payload(tool_name='Bash',tool_input={'command':self.memory('search cache')}))
        self.run_hook('pre-tool-use',self.payload(tool_name='Read',tool_input={'file_path':'x'}))
        self.assertEqual(self.run_hook('stop',self.payload()),{})
    def test_queued_prompt_receipt_satisfies_stop(self):
        from core.capture import finalize
        self.run_hook('user-prompt',self.payload(prompt='one',timestamp='t1'))
        self.run_hook('pre-tool-use',self.payload(tool_name='Write',tool_input={'file_path':str(self.base/'project/a.txt')}))
        finalize('s1','t1',[],self.root)
        self.run_hook('user-prompt',self.payload(prompt='two',timestamp='t2'))
        self.assertEqual(self.run_hook('stop',self.payload()),{})
    def test_session_domain_is_pinned(self):
        from core.init_domain import init_domain
        init_domain('sub',[str(self.base/'project/sub')],self.root)
        self.run_hook('session-start',self.payload())
        self.run_hook('user-prompt',{**self.payload(prompt='hello'),'cwd':str(self.base/'project/sub')})
        self.assertEqual(read_json(self.root/'hooks/last-delivery.json')['domain'],'demo')
    def test_pre_tool_use_does_not_write_delivery(self):
        self.run_hook('pre-tool-use',self.payload(tool_name='Bash',tool_input={'command':'ls'}))
        self.assertFalse((self.root/'hooks/last-delivery.json').exists())
    def test_receipts_are_transient_and_pruned(self):
        from core.capture import finalize,prune
        finalize('s','t',[],self.root)
        self.assertFalse(any((self.root/'.operations').glob('turn-*')))
        old=self.root/'.turns/old.json';old.write_text('{}');os.utime(old,(0,0))
        self.assertEqual(prune(self.root),1);self.assertFalse(old.exists())
    def test_near_duplicate_title_guard(self):
        self.note('Cache invalidation policy','Entries expire after one hour.')
        with self.assertRaisesRegex(ValueError,'possible_duplicate'):self.note('cache invalidation policy!','Entries expire after two hours.')
        self.assertEqual(self.note('Cache invalidation policy','Different body',allow_similar=True)['status'],'created')
        self.assertEqual(self.note('Parallel 1','x')['status'],'created');self.assertEqual(self.note('Parallel 2','y')['status'],'created')
    def test_extended_secret_screening(self):
        for secret in ('ghp_'+'a'*36,'xoxb-1234567890-abcdef','password = hunter2hunter2','AIza'+'b'*35):
            with self.assertRaisesRegex(ValueError,'credential'):self.note('Secret '+secret[:4],'token '+secret)
        self.assertEqual(self.note('Password policy','Passwords require twelve characters.')['status'],'created')
    def test_cli_capture_session_flags_finalize(self):
        from core.cli import main
        from core.capture import status
        with patch('sys.stdout',new_callable=io.StringIO):
            code=main(['--root',str(self.root),'--domain','demo','capture','--subject','project','--concern','architecture','--session','s9','--turn','t9','Flag title','Flag body'])
        self.assertEqual(code,0);self.assertEqual(status('s9','t9',self.root)['outcome'],'saved')
    def test_hook_errors_are_quiet_except_context_events(self):
        from hooks import claude_hooks
        broken=self.base/'broken';broken.mkdir();(broken/'bindings.json').write_text('{not json')
        for event,expect_message in (('pre-tool-use',False),('session-start',True)):
            stdin=io.TextIOWrapper(io.BytesIO(b'{}'))
            with patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(broken)}),patch('sys.argv',['x',event]),patch('sys.stdin',stdin),patch('sys.stdout',new_callable=io.StringIO) as out:
                claude_hooks.main()
            result=json.loads(out.getvalue())
            self.assertEqual('systemMessage' in result,expect_message)
    def test_claude_setup_exec_form_matcher_and_pretty_settings(self):
        from integrations.setup import setup_plan
        home=self.base/'claude';home.mkdir();(home/'settings.json').write_text(json.dumps({'theme':'dark'}))
        with patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(self.root)}):
            proposal=setup_plan('claude',home);apply(home,proposal['plan_id'],proposal['digest'])
        text=(home/'settings.json').read_text();settings=json.loads(text)
        self.assertIn('\n  "hooks"',text);self.assertEqual(settings['theme'],'dark')
        pre=settings['hooks']['PreToolUse'][0];handler=pre['hooks'][0]
        self.assertEqual(pre['matcher'],'Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|Read|Glob|Grep|Agent|Task')
        self.assertEqual(handler['command'],sys.executable);self.assertEqual(handler['args'][1],'pre-tool-use')
        end=settings['hooks']['SessionEnd'][0]['hooks'][0];self.assertEqual((end['args'][1],end['timeout']),('session-end',60))
        claude_md=(home/'CLAUDE.md').read_text();self.assertIn('applies only when',claude_md);self.assertIn('Subagents are read-only',claude_md)
        self.assertNotIn(sys.executable,claude_md)   # the static block is machine-independent
        proposal=setup_plan('claude',home,uninstall=True);apply(home,proposal['plan_id'],proposal['digest'])
        self.assertEqual(json.loads((home/'settings.json').read_text()),{'theme':'dark','hooks':{}})
    def test_factory_setup_has_no_subagent_start_and_plain_instructions(self):
        from integrations.setup import setup_plan
        home=self.base/'factory'
        proposal=setup_plan('factory',home,enable_hooks=True);apply(home,proposal['plan_id'],proposal['digest'])
        self.assertNotIn('SubagentStart',json.loads((home/'hooks.json').read_text()))
        other=self.base/'factory-plain'
        proposal=setup_plan('factory',other);apply(other,proposal['plan_id'],proposal['digest'])
        text=(other/'AGENTS.md').read_text();self.assertIn('No per-turn receipt',text);self.assertNotIn('capture-finalize',text)

if __name__=='__main__':unittest.main()
