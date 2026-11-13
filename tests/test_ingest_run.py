"""scripts/ingest_run.py driven with a FAKE agent (a small Python script that reads the prompt, calls the real LM CLI against a temp store to
capture a digest per listed file, and prints a JSON report with a cost): batching, new/stale only by default (--all, the --changed-only alias), skip markers, --budget-usd, --dry-run, idempotent
reruns, and a changed file's new digest replacing the old one. No real claude or codex process is ever started."""
import importlib.util,io,json,re,subprocess,sys,unittest,unittest.mock
from pathlib import Path
from test_digests import Digests,put
from core import sources
from core.repository import inventory

SCRIPT=Path(__file__).resolve().parents[1]/'scripts'/'ingest_run.py'
CLI=Path(__file__).resolve().parents[1]/'living_memory.py'
FAKE='''
import json,re,subprocess,sys
from pathlib import Path
root,cli,cwd,calls,*options=sys.argv[1:]
cost=float(options[options.index('--cost')+1]) if '--cost' in options else 0.5
prompt=sys.stdin.read()
with open(calls,'a',encoding='utf-8') as f:f.write(json.dumps(prompt)+'\\n')
if '--fail' in options:sys.exit(3)
digested,skipped,failed=[],[],[]
for line in prompt.splitlines():
    found=re.match(r'^\\d+\\. ("(?:[^"\\\\]|\\\\.)*") \\((\\w+), (\\d+) bytes, (\\w+)(, no --file)?\\) read: (.+)$',line)
    if not found:continue
    path=json.loads(found[1]);kind=found[2];status=found[4];abs_=found[6]
    if '--skip-util' in options and path.endswith('util.py'):skipped.append({'path':path,'reason':'trivial'});continue
    if '--fail-util' in options and path.endswith('util.py'):failed.append({'path':path,'reason':'capture blocked'});continue
    wording=len(Path(abs_).read_text(encoding='utf-8'))
    args=[sys.executable,cli,'--root',root,'--domain','demo','--cwd',cwd,'capture','--type','decision' if kind=='adr' else 'knowledge',
          '--title',f'{path}: a {kind} file of the kb','--summary',f'What {path} is.','--detail',f'**Facts:** {path} is {status}, {wording} characters long ({path}:1).',
          '--subject','project','--concern','architecture','--source',path]
    if not found[5]:args+=['--file',path]
    done=subprocess.run(args,capture_output=True,text=True,encoding='utf-8')
    if done.returncode:print(done.stdout,done.stderr,file=sys.stderr);sys.exit(4)
    digested.append(path)
report={'digested':digested,'skipped':skipped,**({'failed':failed} if '--no-failed-key' not in options else {})}
denials=[{'tool_name':'PowerShell','tool_input':{'command':'Remove-Item '+'x'*300}}] if '--denials' in options else []
if '--wrap' in options:print(json.dumps({'type':'result','is_error':False,'total_cost_usd':cost,'permission_denials':denials,'result':'Done.\\n'+json.dumps(report)}))
else:print(json.dumps({**report,'cost_usd':cost}))
'''

class IngestRun(Digests):
    def setUp(self):
        super().setUp()
        (self.repo/'src'/'server.py').write_bytes(('def serve():\n    pass\n'+'# padding\n'*60).encode())
        self.agent=self.base/'agent';self.agent.mkdir();(self.agent/'fake_agent.py').write_text(FAKE,encoding='utf-8')
        self.calls=self.agent/'calls.jsonl';self.log=self.agent/'run.jsonl'
        sources.sync(self.root,'demo')
    def prompts(self):return [json.loads(l) for l in self.calls.read_text(encoding='utf-8').splitlines()] if self.calls.exists() else []
    def listed(self,prompt):return [json.loads(m[1]) for m in (re.match(r'\d+\. ("(?:[^"\\]|\\.)*")',l) for l in prompt.splitlines()) if m]
    def run_script(self,*extra,agent=(),expect=0):
        command=[sys.executable,str(self.agent/'fake_agent.py'),str(self.root),str(CLI),str(self.project),str(self.calls),*agent]
        done=subprocess.run([sys.executable,'-X','utf8',str(SCRIPT),'--root',str(self.root),'--domain','demo','--cwd',str(self.project),'--log',str(self.log),
                             '--command-json',json.dumps(command),*extra],capture_output=True,text=True,encoding='utf-8',timeout=300)
        self.assertEqual(done.returncode,expect,done.stdout+done.stderr)
        return json.loads(done.stdout) if done.stdout.strip().startswith('{') else None
    def active(self):return [n for n in inventory('demo',self.root) if n.get('status','active')=='active']
    def total(self):return len([i for i in self.cli('ingest-scan','--limit','100')[1]['items'] if i['kind']!='test'])   # a default run leaves tests out

class Batches(IngestRun):
    def test_dry_run_prints_the_batches_and_prompts_and_runs_no_agent(self):
        out=self.run_script('--batch','3','--dry-run')
        self.assertTrue(out['dry_run']);self.assertEqual(out['files'],self.total());self.assertEqual(len(out['batches']),-(-self.total()//3))
        first=out['batches'][0];self.assertEqual(first['paths'],['kb/docs/adr/0003-use-event-sourcing.md','kb/README.md','kb/docs/guide.md'])
        for text in ('capture --type TYPE','--source','--file','300-1,200 characters','decision for decision records','project brief','codebase map','"digested"','read: '):self.assertIn(text,first['prompt'])
        self.assertFalse(self.calls.exists());self.assertEqual(self.active(),[])

    def test_files_go_to_the_agent_in_ranked_batches_and_get_digests(self):
        out=self.run_script('--batch','3',agent=('--skip-util',));total=self.total()
        prompts=self.prompts();self.assertEqual(len(prompts),-(-total//3));self.assertTrue(all(len(self.listed(p))<=3 for p in prompts))
        order=[path for p in prompts for path in self.listed(p)];self.assertEqual(order[:3],['kb/docs/adr/0003-use-event-sourcing.md','kb/README.md','kb/docs/guide.md'])
        self.assertEqual(len(order),total)
        self.assertEqual((out['batches_run'],out['files_digested'],out['files_skipped']),(len(prompts),total-1,1));self.assertEqual(out['cost_usd'],0.5*len(prompts))
        self.assertEqual(out['verified'],{'current':total-1,'skipped':1,'still_open':0});self.assertTrue(out['ok'])
        notes=self.active();self.assertEqual(len(notes),total-1)
        adr=next(n for n in notes if n['sources'][0]['path']=='kb/docs/adr/0003-use-event-sourcing.md');self.assertEqual(adr['type'],'decision')
        rows=[json.loads(l) for l in self.log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['batch'] for r in rows[:-1]],list(range(1,len(prompts)+1)));self.assertIn('summary',rows[-1]);self.assertEqual(rows[0]['skipped'],[])

    def test_max_batches_limits_the_run(self):
        out=self.run_script('--batch','2','--max-batches','2');self.assertEqual((out['batches_run'],out['batches_planned']),(2,2));self.assertEqual(len(self.active()),4)
        self.assertEqual(self.cli('ingest-scan','--limit','100')[1]['counts']['current'],4)

    def test_the_budget_stops_further_batches(self):
        out=self.run_script('--batch','2','--budget-usd','1.0');self.assertEqual(out['batches_run'],2);self.assertEqual(out['cost_usd'],1.0);self.assertGreater(out['batches_planned'],2)
        self.assertEqual(len(self.prompts()),2)

    def test_a_claude_style_wrapped_report_gives_its_cost_and_digests(self):
        out=self.run_script('--batch','4','--max-batches','1',agent=('--wrap','--cost','0.25'));self.assertEqual((out['cost_usd'],out['files_digested']),(0.25,4))

    def test_parallel_runs_the_batches_in_waves(self):
        out=self.run_script('--batch','3','--parallel','2');self.assertEqual(out['batches_run'],-(-self.total()//3));self.assertEqual(len(self.active()),self.total())

    def test_a_failing_agent_is_reported_and_the_run_exits_nonzero(self):
        out=self.run_script('--batch','4','--max-batches','1',agent=('--fail',),expect=1)
        self.assertFalse(out['ok']);self.assertEqual((out['batches_failed'],out['files_digested']),(1,0));self.assertEqual(self.active(),[])

class Kinds(IngestRun):
    TEST='kb/tests/test_ledger.py'
    def sent(self,*extra):
        self.run_script('--batch','100',*extra);return [path for p in self.prompts() for path in self.listed(p)]
    def test_the_default_run_does_not_send_test_files(self):
        out=self.run_script('--dry-run');self.assertNotIn(self.TEST,out['batches'][0]['paths']);self.assertEqual(out['files'],self.total())
        sent=self.sent();self.assertNotIn(self.TEST,sent);self.assertEqual(len(sent),self.total());self.assertIn('kb/src/ledger.py',sent)
    def test_kinds_module_sends_only_modules(self):
        items={i['path']:i['kind'] for i in self.cli('ingest-scan','--limit','100')[1]['items']}
        self.assertEqual(self.run_script('--kinds','module,readme','--dry-run')['files'],len([k for k in items.values() if k in ('module','readme')]))
        sent=self.sent('--kinds','module');self.assertTrue(sent);self.assertEqual(set(sent),{p for p,k in items.items() if k=='module'})
    def test_kinds_all_includes_the_test_file(self):
        self.assertIn(self.TEST,self.sent('--kinds','all'))
    def test_an_unknown_kind_is_refused(self):
        done=subprocess.run([sys.executable,str(SCRIPT),'--root',str(self.root),'--cwd',str(self.project),'--kinds','modules','--dry-run'],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(done.returncode,2);self.assertIn('--kinds takes',done.stderr)

class Reruns(IngestRun):
    def test_a_rerun_writes_nothing_new(self):
        self.run_script('--batch','4');before={n['id']:n['revision'] for n in self.active()};self.assertTrue(before)
        out=self.run_script('--batch','4','--all');self.assertEqual(out['batches_run'],-(-self.total()//4));after={n['id']:n['revision'] for n in self.active()}
        self.assertEqual(after,before);self.assertEqual(out['verified']['current'],len(before))
        self.assertEqual(len([n for n in inventory('demo',self.root,retired=True)]),len(before))   # not even a retired copy

    def test_by_default_only_new_and_stale_files_are_sent(self):
        self.run_script('--batch','4');self.calls.unlink()
        out=self.run_script();self.assertEqual(out['batches_run'],0);self.assertFalse(self.calls.exists())
        put(self.repo/'README.md',self.repo.joinpath('README.md').read_text(encoding='utf-8')+'## Timeouts\n\nFive seconds.\n')
        put(self.repo/'docs'/'ops.md','# On-call\n\nPage the billing team.\n');sources.sync(self.root,'demo')
        out=self.run_script('--changed-only');prompts=self.prompts();self.assertEqual(len(prompts),1)   # the old flag is a no-op alias
        self.assertEqual(sorted(self.listed(prompts[0])),['kb/README.md','kb/docs/ops.md'])
        self.assertIn('stale',prompts[0]);self.assertIn('new',prompts[0]);self.assertEqual(out['verified'],{'current':2,'skipped':0,'still_open':0})

    def test_a_stale_file_is_re_digested_by_replacing_the_old_digest(self):
        self.run_script('--batch','4');old=next(n for n in self.active() if n['sources'][0]['path']=='kb/README.md')
        put(self.repo/'README.md',self.repo.joinpath('README.md').read_text(encoding='utf-8')+'## Timeouts\n\nFive seconds.\n');sources.sync(self.root,'demo')
        out=self.run_script();self.assertEqual(out['files_digested'],1)
        active=[n for n in self.active() if n['sources'][0]['path']=='kb/README.md'];self.assertEqual(len(active),1);self.assertNotEqual(active[0]['id'],old['id'])
        retired={n['id']:n for n in inventory('demo',self.root,retired=True)}[old['id']];self.assertEqual(retired['status'],'superseded');self.assertEqual(retired['superseded_by'],active[0]['id'])
        self.assertEqual(self.cli('ingest-scan','--changed','--limit','100')[1]['counts']['stale'],0)

class Failures(IngestRun):
    UTIL='kb/src/util.py'
    def skip_sidecar(self):return json.loads(p.read_text(encoding='utf-8')) if (p:=self.root/'domains'/'demo'/'.ingest-skips.json').exists() else {}
    def status(self):return {i['path']:i['status'] for i in self.cli('ingest-scan','--limit','100')[1]['items']}[self.UTIL]
    def test_a_failed_capture_is_logged_as_failed_and_never_recorded_as_a_skip(self):
        out=self.run_script('--batch','100',agent=('--fail-util',));self.assertEqual((out['files_failed'],out['files_skipped'],out['verified']['still_open']),(1,0,1))
        self.assertEqual(self.skip_sidecar(),{});self.assertEqual(self.status(),'new')
        row=json.loads(self.log.read_text(encoding='utf-8').splitlines()[0]);self.assertEqual((row['failed'],row['skipped'],row['permission_denials']),([{'path':self.UTIL,'reason':'capture blocked'}],[],0))
        self.calls.unlink();self.run_script();self.assertEqual([self.listed(p) for p in self.prompts()],[[self.UTIL]])   # offered again
    def test_permission_denials_cancel_the_batchs_skips(self):
        out=self.run_script('--batch','100',agent=('--wrap','--skip-util','--denials'));self.assertEqual((out['files_failed'],out['files_skipped']),(1,0))
        self.assertEqual(self.skip_sidecar(),{});self.assertEqual(self.status(),'new')
        row=json.loads(self.log.read_text(encoding='utf-8').splitlines()[0]);self.assertEqual((row['permission_denials'],row['skipped'],[f['path'] for f in row['failed']]),(1,[],[self.UTIL]))
    def test_a_trivial_skip_is_still_recorded_and_an_agent_without_a_failed_key_works(self):
        out=self.run_script('--batch','100',agent=('--wrap','--skip-util','--no-failed-key'));self.assertEqual((out['files_failed'],out['files_skipped']),(0,1))
        self.assertEqual(list(self.skip_sidecar()),[self.UTIL]);self.assertEqual(self.status(),'skipped')

class Agent(IngestRun):
    """The default agent command: its allowlist matches how the prompt tells the agent to type the CLI, and a denied call is named in the log."""
    def script(self):
        spec=importlib.util.spec_from_file_location('ingest_run_under_test',SCRIPT);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
    def test_the_default_allowlist_matches_the_typed_command(self):
        module=self.script();seen=[]
        def fake(args,number,items):
            seen.append(args.allowed_tools);return {'batch':number,'paths':[],'cost_usd':0.0,'digested':[],'skipped':[],'failed':[],'permission_denials':0,'denied':[],'returncode':0}
        with unittest.mock.patch.object(module,'run_batch',fake),unittest.mock.patch('sys.stdout',io.StringIO()),unittest.mock.patch('sys.stderr',io.StringIO()):
            module.main(['--root',str(self.root),'--domain','demo','--cwd',str(self.project),'--log',str(self.log),'--max-batches','1'])
        py,cli=sys.executable,str(CLI)
        self.assertEqual(seen,[['Read','Glob','Grep',f'Bash("{py}" "{cli}" *)']])
    def test_a_denied_call_is_named_in_the_log_row_with_its_command_cut_to_200_characters(self):
        self.run_script('--batch','100',agent=('--wrap','--denials'))
        row=json.loads(self.log.read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(row['permission_denials'],1);self.assertEqual(row['denied'],[{'tool':'PowerShell','input':'Remove-Item '+'x'*188}])
    def test_a_denial_without_a_command_records_its_input_as_json(self):
        reply=json.dumps({'total_cost_usd':0.1,'result':'{}','permission_denials':[{'tool_name':'Read','tool_input':{'file_path':'a.py'}},{'tool_name':'Bash'}]})
        cost,digested,skipped,failed,count,denied=self.script().read_report(reply)
        self.assertEqual(count,2);self.assertEqual(denied,[{'tool':'Read','input':'{"file_path": "a.py"}'},{'tool':'Bash','input':'null'}])

class Skips(IngestRun):
    UTIL='kb/src/util.py'
    def setUp(self):
        super().setUp()
        self.run_script('--batch','4',agent=('--skip-util',));self.calls.unlink()
    def test_a_skipped_file_is_remembered_and_not_sent_again(self):
        sidecar=json.loads((self.root/'domains'/'demo'/'.ingest-skips.json').read_text(encoding='utf-8'))
        self.assertEqual(list(sidecar),[self.UTIL]);self.assertEqual((sidecar[self.UTIL]['reason'],len(sidecar[self.UTIL]['sha256'])),('trivial',64));self.assertIn('at',sidecar[self.UTIL])
        found=self.cli('ingest-scan','--limit','100')[1];self.assertEqual(found['counts']['skipped'],1)
        self.assertEqual({i['path']:i['status'] for i in found['items']}[self.UTIL],'skipped');self.assertNotIn(self.UTIL,[i['path'] for i in self.cli('ingest-scan','--changed','--limit','100')[1]['items']])
        out=self.run_script();self.assertEqual(out['batches_run'],0);self.assertFalse(self.calls.exists())
    def test_all_sends_skipped_and_current_files_again(self):
        self.run_script('--batch','100','--all');prompts=self.prompts();self.assertEqual(len(prompts),1);self.assertIn(self.UTIL,self.listed(prompts[0]));self.assertEqual(len(self.listed(prompts[0])),self.total())
    def test_a_changed_skipped_file_is_offered_again(self):
        put(self.repo/'src'/'util.py','def tiny():'+chr(10)+'    return 2'+chr(10)+('# now it matters'+chr(10))*30);sources.sync(self.root,'demo')
        self.assertEqual({i['path']:i['status'] for i in self.cli('ingest-scan','--limit','100')[1]['items']}[self.UTIL],'new')
        out=self.run_script();self.assertEqual([self.listed(p) for p in self.prompts()],[[self.UTIL]])
    def test_the_skip_list_keeps_the_newest_five_thousand(self):
        from core import digests
        with unittest.mock.patch.object(digests,'MAX_SKIPS',2):
            for name in ('a','b','c'):
                put(self.repo/f'{name}.md',name);digests.record_skips(self.root,'demo',[{'path':f'kb/{name}.md','reason':'x'}])
        self.assertEqual(sorted(digests.skips(self.root,'demo')),['kb/b.md','kb/c.md'])
    def skip_cli(self,payload):
        file=self.base/'skip-payload.json';file.write_text(json.dumps(payload),encoding='utf-8');return self.cli('ingest-skip','--payload-file',str(file))
    def test_clearing_a_skip_makes_the_file_new_again_and_an_unknown_path_clears_nothing(self):
        code,out=self.skip_cli({'clear':[self.UTIL,'kb/nothing.md']});self.assertEqual(code,0,out)
        self.assertEqual((out['recorded'],out['cleared']),(0,1));self.assertEqual({i['path']:i['status'] for i in self.cli('ingest-scan','--limit','100')[1]['items']}[self.UTIL],'new')
        self.assertEqual(self.skip_cli({'clear':[self.UTIL]})[1]['cleared'],0)
    def test_one_payload_may_record_and_clear(self):
        put(self.repo/'extra.md','x');code,out=self.skip_cli({'skipped':[{'path':'kb/extra.md','reason':'tiny'}],'clear':[self.UTIL]});self.assertEqual((out['recorded'],out['cleared']),(1,1))
        self.assertEqual(sorted(json.loads((self.root/'domains'/'demo'/'.ingest-skips.json').read_text(encoding='utf-8'))),['kb/extra.md'])
    def test_ingest_skip_is_a_main_agent_write(self):
        code,_=self.cli('--actor','subagent','ingest-skip');self.assertNotEqual(code,0)

if __name__=='__main__':unittest.main()
