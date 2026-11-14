import json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from core import maintenance_agent as agent,runtime
from core import maintenance_state as ms
from test_core import StoreFixture

FAKE='''import json,os,subprocess,sys,time
args=sys.argv[1:];mode=os.environ.get('FAKE_MODE','ok')
if '--help' in args:
    drop=os.environ.get('FAKE_DROP_FLAG')
    print('Usage: claude [options]\\n'+'\\n'.join('  '+f+' <value>' for f in %r if f!=drop));sys.exit(0)
data=sys.stdin.read()
json.dump({'argv':args,'cwd':os.getcwd(),'stdin':data,'child':os.environ.get('LIVING_MEMORY_MAINTENANCE_CHILD'),'nobg':os.environ.get('LIVING_MEMORY_NO_BACKGROUND')},open(os.environ['FAKE_LOG'],'w'))
report=json.loads(os.environ.get('FAKE_REPORT','{}'))
env={'type':'result','subtype':'success','is_error':False,'result':'','structured_output':report,'total_cost_usd':0.0123}
if mode=='ok':print(json.dumps(env))
elif mode=='nocost':env.pop('total_cost_usd');print(json.dumps(env))
elif mode=='error':env.update(is_error=True,subtype='error_during_execution');print(json.dumps(env));sys.exit(1)
elif mode=='budget':env.update(is_error=True,subtype='error_max_budget_usd',total_cost_usd=0.5);print(json.dumps(env));sys.exit(1)
elif mode=='nostructured':env['structured_output']='text';print(json.dumps(env))
elif mode=='malformed':print('{"type": "result", "subtype"')
elif mode=='prose':print('Here is the report: '+json.dumps(env)+' Thanks!')
elif mode=='list':print(json.dumps([env]))
elif mode=='overflow':sys.stdout.write('x'*200000);sys.stdout.flush();time.sleep(30)
elif mode=='stderr':sys.stderr.write('e'*100000);sys.stderr.flush();time.sleep(30)
elif mode=='noisy':sys.stderr.write('warning sk-ant-'+'Ab1'*12+' leaked');print(json.dumps(env))
elif mode=='hang':
    child=subprocess.Popen([sys.executable,'-c','import os,time;open(os.environ["FAKE_CHILD_PID"],"w").write(str(os.getpid()));time.sleep(120)'])
    open(os.environ['FAKE_PARENT_PID'],'w').write(str(os.getpid()));time.sleep(120)
''' % (list(agent.REQUIRED_FLAGS),)

def alive(pid):
    if os.name=='nt':
        import ctypes
        k=ctypes.windll.kernel32;k.OpenProcess.restype=ctypes.c_void_p
        handle=k.OpenProcess(0x1000,False,pid)
        if not handle:return False
        code=ctypes.c_ulong();k.GetExitCodeProcess(ctypes.c_void_p(handle),ctypes.byref(code));k.CloseHandle(ctypes.c_void_p(handle))
        return code.value==259
    try:os.kill(pid,0);return True
    except OSError:return False

class Fake(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.dir=Path(self.tmp.name)
        self.script=self.dir/'fake_claude.py';self.script.write_text(FAKE,encoding='utf-8');self.exe=[sys.executable,str(self.script)]
        self.report={'version':1,'run_id':'r1','decisions':[{'ref':'demo:a','revision':'a'*64,'action':'keep','reason':'Fine as it is.'}]}
        self.env=patch.dict(os.environ,{'FAKE_LOG':str(self.dir/'log.json'),'FAKE_REPORT':json.dumps(self.report),'FAKE_PARENT_PID':str(self.dir/'parent.pid'),
                                          'FAKE_CHILD_PID':str(self.dir/'child.pid')});self.env.start();self.addCleanup(self.env.stop)
        self.request={'cfg':ms.validate({'enabled':True,'domains':[]}),'packet':'{"run_id":"r1","candidates":[]}','run':{'id':'r1','budget_passed_micro':250000},
                      'manifest':{'candidates':{'demo:a':{'revision':'a'*64}},'evidence':{'E1':{}}},'timeout':30}
    def review(self,mode='ok',**kw):
        with patch.dict(os.environ,{'FAKE_MODE':mode}):return agent.review({**self.request,**kw},executable=self.exe)
    def log(self):return json.loads((self.dir/'log.json').read_text(encoding='utf-8'))

class Provider(Fake):
    def test_argv_env_stdin_and_a_fresh_working_directory(self):
        out=self.review();self.assertTrue(out['ok'],out)
        log=self.log()
        self.assertEqual(log['argv'],['-p','--output-format','json','--model','sonnet','--tools','','--safe-mode','--strict-mcp-config','--no-session-persistence',
                                      '--disable-slash-commands','--system-prompt',agent.POLICY,'--json-schema',agent.SCHEMA_JSON,'--max-budget-usd','0.25'])
        self.assertEqual((log['child'],log['nobg'],log['stdin']),('1','1',self.request['packet']))
        self.assertNotEqual(Path(log['cwd']).resolve(),Path.cwd().resolve());self.assertFalse(Path(log['cwd']).exists())   # a temp folder, removed afterwards
        self.assertEqual(out['report']['decisions'][0]['ref'],'demo:a');self.assertEqual(out['usage_micro'],12300)
    def test_schema_and_policy_are_well_formed(self):
        self.assertEqual(json.loads(agent.SCHEMA_JSON),agent.SCHEMA);self.assertIn('untrusted data',agent.POLICY)
        self.assertEqual(set(agent.SCHEMA['properties']['decisions']['items']['properties']['action']['enum']),set(agent.ACTIONS))
    def test_a_missing_executable(self):
        with patch('core.maintenance_agent.shutil.which',return_value=None):
            self.assertEqual(agent.provider(),{'ok':False,'reason_code':'provider_unavailable'})
            out=agent.review(self.request);self.assertEqual((out['ok'],out['reason_code']),(False,'provider_unavailable'))
        self.assertEqual(agent.provider(executable=[str(self.dir/'nothing')])['reason_code'],'provider_unavailable')
    def test_a_script_shim_is_refused(self):
        with patch('core.maintenance_agent.shutil.which',return_value=str(self.dir/'claude.cmd')):self.assertEqual(agent.provider()['reason_code'],'provider_unsupported')
    def test_a_missing_flag_is_unsupported_and_nothing_runs(self):
        for flag in agent.REQUIRED_FLAGS:
            with self.subTest(flag=flag),patch.dict(os.environ,{'FAKE_DROP_FLAG':flag}):
                found=agent.provider(executable=self.exe);self.assertEqual((found['ok'],found['reason_code']),(False,'provider_unsupported'));self.assertIn(flag,found['detail'])
        (self.dir/'log.json').unlink(missing_ok=True)
        with patch.dict(os.environ,{'FAKE_DROP_FLAG':'--safe-mode'}):out=self.review()
        self.assertEqual(out['reason_code'],'provider_unsupported');self.assertFalse((self.dir/'log.json').exists())
    def test_provider_error_and_budget_envelopes(self):
        out=self.review('error');self.assertEqual((out['ok'],out['reason_code'],out['usage_micro']),(False,'provider_error',12300))
        out=self.review('budget');self.assertEqual((out['reason_code'],out['usage_micro']),('provider_budget',500000))
    def test_envelopes_that_are_not_one_clean_object_are_invalid(self):
        for mode in ('malformed','prose','list','nostructured'):
            with self.subTest(mode=mode):out=self.review(mode);self.assertEqual((out['ok'],out['reason_code']),(False,'invalid_report'))
    def test_a_report_that_breaks_the_contract_is_invalid(self):
        with patch.dict(os.environ,{'FAKE_REPORT':json.dumps({**self.report,'run_id':'other'})}):out=self.review()
        self.assertEqual((out['ok'],out['reason_code']),(False,'invalid_report'));self.assertEqual(out['usage_micro'],12300)
    def test_missing_cost_is_unknown(self):
        out=self.review('nocost');self.assertTrue(out['ok']);self.assertIsNone(out['usage_micro'])
    def test_stdout_overflow_kills_the_child(self):
        began=time.monotonic();out=self.review('overflow');self.assertLess(time.monotonic()-began,20)
        self.assertEqual((out['ok'],out['reason_code']),(False,'invalid_report'));self.assertIn('size limit',out['detail'])
    def test_stderr_overflow_kills_the_child_and_stderr_is_sanitized(self):
        began=time.monotonic();out=self.review('stderr');self.assertLess(time.monotonic()-began,20);self.assertEqual(out['reason_code'],'invalid_report')
        self.assertLessEqual(len(out['stderr']),agent.STDERR_EXCERPT)
        out=self.review('noisy');self.assertNotIn('sk-ant-',out['stderr']);self.assertIn('redacted',out['stderr'])
    def test_a_timeout_kills_the_child_and_its_grandchild(self):
        began=time.monotonic();out=self.review('hang',timeout=3);self.assertLess(time.monotonic()-began,30)
        self.assertEqual((out['ok'],out['reason_code'],out['usage_micro']),(False,'timeout',None))
        pids=[int((self.dir/n).read_text()) for n in ('parent.pid','child.pid')]
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and any(alive(p) for p in pids):time.sleep(0.1)
        self.assertEqual([alive(p) for p in pids],[False,False])

class Hook(StoreFixture):
    def test_the_reviewer_child_runs_no_hook(self):
        payload={'cwd':str(self.base/'project'),'session_id':'s','prompt':'how does the cache work'}
        self.assertTrue(runtime.process('claude','user-prompt',payload,self.root))   # an ordinary process answers
        with patch.dict(os.environ,{'LIVING_MEMORY_MAINTENANCE_CHILD':'1'}):
            for event in ('session-start','user-prompt','pre-tool-use','stop'):self.assertEqual(runtime.process('claude',event,payload,self.root),{})

if __name__=='__main__':unittest.main()
