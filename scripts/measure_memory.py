"""Peak memory per ALM entry point on a synthetic store (PERF-2), plus usage-diary read cost (PERF-1b). Windows only, stdlib only.

Build the store once, then measure; every run is its own process on a throwaway copy of that store (the model is shared by a junction).

  py -3.15 scripts/measure_memory.py build --out DIR [--notes 1000] [--model-from STORE]
      DIR/store and DIR/project, built with scripts/benchmark.py synthetic_store. --model-from copies STORE/.models (read only) so
      the prompt hook and `semantic rebuild` have a model; without it semantic search is off.
  py -3.15 scripts/measure_memory.py run --out DIR [--runs 3] [--only NAME ...] [--json FILE]
      NAME: serve-soak (300 requests; peak working set after each 50), baseline-bare, baseline-runtime, baseline-cli, hook-session-start, hook-prompt, cli-search, semantic-rebuild, serve.
      Each entry runs --runs times plain (wall time, peak working set and peak commit read from the process handle after exit) and
      --runs times under tracemalloc (peak of Python allocations, top call sites near that peak; wall time there is inflated).
  py -3.15 scripts/measure_memory.py diary --out DIR [--rows 5000 50000 500000] [--json FILE]
      How often the hooks read the usage diary, rows written per hook, and the time and memory of core.usage reads at those sizes.

Peak working set is RAM resident at the peak and includes the memory-mapped model pages touched. Peak commit (PeakPagefileUsage)
is the private bytes the process asked for. tracemalloc sees Python allocations only (not sqlite, mmap or the interpreter's own).
"""
import argparse,ctypes,json,os,random,shutil,statistics,subprocess,sys,tempfile,time
from ctypes import wintypes
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
ENTRIES=('baseline-bare','baseline-runtime','baseline-cli','hook-session-start','hook-prompt','cli-search','semantic-rebuild','serve')
SOAK=300   # serve-soak: this many requests (mostly searches) to one serve process, plain runs only; not in the default list
PROMPT='what does the Retry token deploy note 320 say about the retry policy'
SEARCH='cache deploy rollback retry'

class Counters(ctypes.Structure):
    _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),
              ('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),
              ('QuotaNonPagedPoolUsage',ctypes.c_size_t),('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t),('PrivateUsage',ctypes.c_size_t)]

def peaks(handle):
    """(peak working set, peak commit) in bytes of a process handle, alive or exited."""
    psapi=ctypes.WinDLL('psapi',use_last_error=True);psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    c=Counters();c.cb=ctypes.sizeof(c)
    if not psapi.GetProcessMemoryInfo(wintypes.HANDLE(int(handle)),ctypes.byref(c),c.cb):raise OSError(ctypes.get_last_error(),'GetProcessMemoryInfo')
    return c.PeakWorkingSetSize,c.PeakPagefileUsage

def own_peaks():
    kernel=ctypes.WinDLL('kernel32');kernel.GetCurrentProcess.restype=wintypes.HANDLE;return peaks(kernel.GetCurrentProcess())

# ---------------------------------------------------------------- stores
def build(out,notes,model_from):
    sys.path.insert(0,str(REPO));sys.path.insert(0,str(REPO/'scripts'))
    import benchmark
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    root,project,ms=benchmark.synthetic_store(out,notes,semantic_from=model_from)
    print(json.dumps({'store':str(root),'project':str(project),'notes':notes,'first_index_ms':ms,'model':bool(model_from)}))

def link(path,target):subprocess.run(['cmd','/c','mklink','/J',str(path),str(target)],check=True,capture_output=True)

class Copy:
    """A throwaway copy of the pristine store; .models is a junction to the pristine one, so a model is never copied or changed."""
    def __init__(self,out):
        self.out=Path(out);self.dir=Path(tempfile.mkdtemp(prefix='alm-mem-',dir=self.out));self.store=self.dir/'store';self.project=self.out/'project'
        shutil.copytree(self.out/'store',self.store,ignore=shutil.ignore_patterns('.models'))
        if (self.out/'store'/'.models').is_dir():link(self.store/'.models',self.out/'store'/'.models')
    def __enter__(self):return self
    def __exit__(self,*exc):
        if (self.store/'.models').exists():os.rmdir(self.store/'.models')
        shutil.rmtree(self.dir,ignore_errors=True)

# ---------------------------------------------------------------- entry points
def spec(name,store,project,run):
    """(argv after the interpreter, stdin bytes or a list of serve requests) for an entry point."""
    payload=lambda extra:json.dumps({'cwd':str(project),'session_id':f'mem-{name}-{run}','turn_id':'t','hook_event_name':'x',**extra}).encode()
    cli=lambda *a:[str(REPO/'living_memory.py'),'--json','--root',str(store),'--cwd',str(project),*a]
    if name=='baseline-bare':return ['-c','pass'],b''
    if name=='baseline-runtime':return ['-c','import core.runtime'],b''
    if name=='baseline-cli':return ['-c','import core.cli'],b''
    if name=='hook-session-start':return [str(REPO/'hooks'/'claude_hooks.py'),'session-start'],payload({'hook_event_name':'SessionStart','source':'startup'})
    if name=='hook-prompt':return [str(REPO/'hooks'/'claude_hooks.py'),'user-prompt'],payload({'hook_event_name':'UserPromptSubmit','prompt':PROMPT})
    if name=='cli-search':return cli('search',SEARCH),b''
    if name=='semantic-rebuild':return cli('semantic','rebuild'),b''
    if name=='serve-soak':
        rng=random.Random(3);words='cache deploy rollback session token queue index schema migration retry timeout webhook parser lexer renderer export import vault ledger invoice tenant shard replica gateway proxy cluster build release artifact pipeline lint format logger metric tracer alert budget quota limit policy audit secret rotate backup restore snapshot journal'.split()
        return cli('serve','--stdio'),[['search','--domain','scale','--actor','viewer']]+[['search',' '.join(rng.sample(words,3)),'--domain','scale'] if i%10 else ['read','@FIRST','--domain','scale'] for i in range(SOAK)]
    if name=='serve':
        # What an app front end sends: handshake, listings, a few searches, one read of the first listed note.
        return cli('serve','--stdio'),[
            ['version'],['domains'],['search','--domain','scale','--actor','viewer'],['search',SEARCH,'--domain','scale'],['search','session token expiry','--domain','scale'],
            ['search','backup restore snapshot','--domain','scale'],['read','@FIRST','--domain','scale'],['search',SEARCH,'--domain','scale']]
    raise SystemExit('unknown entry '+name)

def env_for(store):return {**os.environ,'LIVING_MEMORY_ROOT':str(store),'LIVING_MEMORY_NO_BACKGROUND':'1','PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}

def run_once(name,out,traced,diary=None):
    """One process. Returns wall_s, peak_ws, peak_commit (bytes), plus tracemalloc fields when traced, plus serve steps."""
    with Copy(out) as copy:
        if diary:seed_diary(copy.store,diary)
        argv,stdin=spec(name,copy.store,copy.project,int(time.time()*1000)%10**6)
        result_file=copy.dir/'traced.json'
        command=[sys.executable,'-X','utf8']+(([str(Path(__file__).resolve()),'traced','--result',str(result_file),'--']+argv) if traced else argv)
        started=time.perf_counter()
        proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env_for(copy.store),cwd=str(REPO))
        steps=[]
        if isinstance(stdin,list):
            ref=None
            for i,request in enumerate(stdin):
                request=[ref if a=='@FIRST' else a for a in request]
                t=time.perf_counter();proc.stdin.write((json.dumps({'id':i,'argv':request})+'\n').encode());proc.stdin.flush()
                line=proc.stdout.readline();elapsed=time.perf_counter()-t
                if not line:steps.append({'request':request[0],'error':'no reply'});break
                reply=json.loads(line)
                if request[0]=='search' and (reply.get('result') or {}).get('mode')=='list':
                    items=(reply.get('result') or {}).get('notes') or (reply.get('result') or {}).get('items') or []
                    ref=items[0].get('ref') or items[0].get('id') if items else None
                steps.append({'request':request[0],'exit_code':reply['exit_code'],'ms':round(elapsed*1000,1),'peak_ws':peaks(proc._handle)[0]})
            proc.stdin.close();stdout,stderr=proc.stdout.read(),proc.stderr.read();proc.wait(timeout=120)
        else:
            stdout,stderr=proc.communicate(stdin,timeout=300)
        wall=time.perf_counter()-started
        ws,commit=peaks(proc._handle)
        warm=None
        if not traced and not isinstance(stdin,list) and not name.startswith('baseline'):
            # The same command again on the same copy: the first run after a copy also pays for a cold file cache.
            t=time.perf_counter();again=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env_for(copy.store),cwd=str(REPO));again.communicate(stdin,timeout=300);warm=round(time.perf_counter()-t,3)
        value={'entry':name,'traced':traced,'warm_wall_s':warm,'exit_code':proc.returncode,'wall_s':round(wall,3),'peak_ws':ws,'peak_commit':commit,'steps':steps,'stdout_bytes':len(stdout),'stderr':stderr.decode('utf-8','replace')[-300:]}
        if traced and result_file.exists():value.update(json.loads(result_file.read_text(encoding='utf-8')))
        return value

def traced_child(result,argv):
    """Run `python ARGV` under tracemalloc in this process. The top sites are from the highest snapshot taken (a watcher thread
    snapshots whenever traced memory grows 10% past the last one), so they describe memory near the peak, not at exit."""
    import runpy,threading,tracemalloc
    tracemalloc.start(1);state={'best':0,'snap':None,'stop':False}
    def watch():
        while not state['stop']:
            current=tracemalloc.get_traced_memory()[0]
            if current>1_000_000 and current>state['best']*1.1:state['best']=current;state['snap']=tracemalloc.take_snapshot()
            time.sleep(0.002)
    thread=threading.Thread(target=watch,daemon=True);thread.start()
    sys.argv=argv[1:] if argv[0]=='-c' else argv;started=time.perf_counter()
    try:
        if argv[0]=='-c':sys.path.insert(0,os.getcwd());exec(compile(argv[1],'<c>','exec'),{'__name__':'__main__'})
        else:sys.path.insert(0,os.path.dirname(os.path.abspath(argv[0])));runpy.run_path(argv[0],run_name='__main__')
    except SystemExit:pass
    finally:
        state['stop']=True;thread.join();elapsed=time.perf_counter()-started;current,peak=tracemalloc.get_traced_memory()
        snap=state['snap'] or tracemalloc.take_snapshot();tracemalloc.stop()
        keep=tracemalloc.Filter(False,tracemalloc.__file__),tracemalloc.Filter(False,__file__)
        top=[{'where':f"{Path(s.traceback[0].filename).name}:{s.traceback[0].lineno}",'file':s.traceback[0].filename.replace('\\','/'),'kb':round(s.size/1024,1),'blocks':s.count}
             for s in snap.filter_traces(keep).statistics('lineno')[:10]]
        Path(result).write_text(json.dumps({'tm_peak':peak,'tm_current_at_exit':current,'tm_snapshot_total':sum(t.size for t in snap.traces),'tm_top':top,'tm_seconds':round(elapsed,3)}),encoding='utf-8')

# ---------------------------------------------------------------- run
mb=lambda b:f'{b/1048576:.1f}'
def summarize(values,fmt=lambda v:f'{v:.1f}'):
    return f'{fmt(statistics.median(values))} ({fmt(min(values))}-{fmt(max(values))})' if values else 'n/a'

def measure(out,runs,only,diary=None):
    rows={}
    for name in only:
        plain=[run_once(name,out,False,diary) for _ in range(runs)]
        # The baselines have no tracemalloc story worth the wait beyond one number, but keep them uniform.
        traced=[] if name=='serve-soak' else [run_once(name,out,True,diary) for _ in range(runs)]
        bad=[r for r in plain+traced if r['exit_code']]
        rows[name]={'plain':plain,'traced':traced,'failures':[{k:r[k] for k in ('exit_code','stderr')} for r in bad]}
    return rows

def table(rows):
    lines=['| entry | peak working set MB | peak commit MB | tracemalloc peak MB | wall s first run | wall s second run |','|---|---|---|---|---|---|']
    for name,r in rows.items():
        p,t=r['plain'],r['traced']
        lines.append(f"| {name} | {summarize([x['peak_ws']/1048576 for x in p])} | {summarize([x['peak_commit']/1048576 for x in p])} | {summarize([x['tm_peak']/1048576 for x in t if 'tm_peak' in x])} | {summarize([x['wall_s'] for x in p],lambda v:f'{v:.2f}')} | {summarize([x['warm_wall_s'] for x in p if x.get('warm_wall_s')],lambda v:f'{v:.2f}')} |")
    return '\n'.join(lines)

# ---------------------------------------------------------------- diary (PERF-1b)
def seed_diary(store,rows):
    """Write `rows` synthetic diary lines spread over the last 30 days, on real note refs of the store (deterministic)."""
    from datetime import datetime,timedelta,timezone
    ids=sorted(p.stem for p in (Path(store)/'domains'/'scale'/'notes').glob('*.md'))
    rng=random.Random(5);now=datetime.now(timezone.utc);turns=Path(store)/'.turns';turns.mkdir(exist_ok=True)
    refs=[f'scale:{i}' for i in ids];sessions=[''.join(rng.choices('0123456789abcdef',k=12)) for _ in range(max(10,rows//40))]
    events=['inject']*80+['read']*8+['update']*3+['surface']*5+['check','confirm','reminder','signal-capture']
    kinds=['prompt','prompt','session','prompt-map','file','command']
    per_day={}
    for _ in range(rows):
        day=now-timedelta(days=rng.randrange(30),seconds=rng.randrange(86400));e=rng.choice(events)
        row={'t':day.isoformat(timespec='seconds'),'e':e,'ref':rng.choice(refs),'s':rng.choice(sessions),'k':rng.choice(kinds) if e=='inject' else None}
        if e=='inject':row['l']=rng.choice(['pointer','evidence','expanded'])
        per_day.setdefault(day.date().isoformat(),[]).append(json.dumps(row,separators=(',',':')))
    for day,lines in per_day.items():(turns/f'usage-{day}.jsonl').write_text('\n'.join(lines)+'\n',encoding='utf-8',newline='\n')

def probe(mode,store,project):
    """Child for `diary`: count diary reads inside the hooks, or time one usage read, under tracemalloc."""
    sys.path.insert(0,str(REPO));root=Path(store)
    from core import usage
    if mode=='count':
        calls=[];orig={n:getattr(usage,n) for n in ('events','strength_map','used_refs','noisy','capture_coverage')}
        def wrap(n):
            def inner(*a,**k):
                t=time.perf_counter();r=orig[n](*a,**k);frame=sys._getframe(1)
                calls.append({'fn':n,'by':f"{Path(frame.f_code.co_filename).name}:{frame.f_code.co_name}",'ms':round((time.perf_counter()-t)*1000,2),'rows':len(r) if n=='events' else None});return r
            return inner
        for n in orig:setattr(usage,n,wrap(n))
        from core import runtime
        def lines():return sum(len(p.read_text(encoding='utf-8').splitlines()) for p in (root/'.turns').glob('usage-*.jsonl'))
        out={}
        for event,extra in (('session-start',{}),('user-prompt',{'prompt':PROMPT}),('user-prompt',{'prompt':'cache eviction deploy rollback token'})):
            calls.clear();before=lines();payload={'cwd':str(project),'session_id':f'probe-{event}-{len(out)}',**extra}
            runtime.process('claude',event,payload,root);out[f'{event}#{len(out)}']={'diary_reads':list(calls),'rows_written':lines()-before}
        print(json.dumps(out));return
    traced=mode.endswith('-traced');mode=mode.replace('-traced','')
    if traced:
        import tracemalloc;tracemalloc.start()
    t=time.perf_counter()
    result=usage.events(root) if mode=='events' else usage.strength_map(root,{'scale'})
    elapsed=time.perf_counter()-t;value={'mode':mode,'n':len(result),'ms':round(elapsed*1000,1),'peak_ws_mb':round(own_peaks()[0]/1048576,1)}
    if traced:current,peak=tracemalloc.get_traced_memory();value.update(tm_peak_mb=round(peak/1048576,1),tm_current_mb=round(current/1048576,1))
    print(json.dumps(value))

def diary(out,sizes,runs,hooks=True):
    result={}
    with Copy(out) as copy:
        r=subprocess.run([sys.executable,str(Path(__file__).resolve()),'probe','count','--store',str(copy.store),'--project',str(copy.project)],capture_output=True,text=True,env=env_for(copy.store),cwd=str(REPO))
        result['count']=json.loads(r.stdout) if r.returncode==0 else r.stderr[-500:]
    result['sizes']={}
    for n in sizes:
        entry={'rows':n}
        with Copy(out) as copy:
            seed_diary(copy.store,n);files=list((copy.store/'.turns').glob('usage-*.jsonl'));entry['files']=len(files);entry['mb']=round(sum(f.stat().st_size for f in files)/1048576,2)
            for mode in ('events','events-traced','strength','strength-traced'):
                samples=[]
                for _ in range(runs):
                    r=subprocess.run([sys.executable,str(Path(__file__).resolve()),'probe',mode,'--store',str(copy.store),'--project',str(copy.project)],capture_output=True,text=True,env=env_for(copy.store),cwd=str(REPO))
                    samples.append(json.loads(r.stdout) if r.returncode==0 else {'error':r.stderr[-300:]})
                entry[mode]=samples
        if hooks:entry['hooks']=measure(out,runs,['hook-prompt','hook-session-start'],diary=n)
        result['sizes'][n]=entry
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter);sub=p.add_subparsers(dest='cmd',required=True)
    b=sub.add_parser('build');b.add_argument('--out',required=True);b.add_argument('--notes',type=int,default=1000);b.add_argument('--model-from',type=Path)
    r=sub.add_parser('run');r.add_argument('--out',required=True);r.add_argument('--runs',type=int,default=3);r.add_argument('--only',nargs='+',choices=ENTRIES+('serve-soak',));r.add_argument('--json')
    d=sub.add_parser('diary');d.add_argument('--out',required=True);d.add_argument('--rows',type=int,nargs='+',default=[5000,50000,500000]);d.add_argument('--runs',type=int,default=3);d.add_argument('--no-hooks',action='store_true',help='Skip the hook processes at each size (the slow part)');d.add_argument('--json')
    t=sub.add_parser('traced');t.add_argument('--result',required=True);t.add_argument('argv',nargs=argparse.REMAINDER)
    q=sub.add_parser('probe');q.add_argument('mode',choices=['count','events','strength','events-traced','strength-traced']);q.add_argument('--store',required=True);q.add_argument('--project',required=True)
    a=p.parse_args()
    if a.cmd=='build':build(a.out,a.notes,a.model_from)
    elif a.cmd=='traced':traced_child(a.result,a.argv[1:] if a.argv and a.argv[0]=='--' else a.argv)
    elif a.cmd=='probe':probe(a.mode,a.store,a.project)
    elif a.cmd=='run':
        rows=measure(a.out,a.runs,a.only or list(ENTRIES))
        print(table(rows))
        for name,row in rows.items():
            if row['failures']:print('FAILURES',name,row['failures'])
        if a.json:Path(a.json).write_text(json.dumps(rows,indent=1),encoding='utf-8')
    elif a.cmd=='diary':
        value=diary(a.out,a.rows,a.runs,not a.no_hooks);print(json.dumps({k:v for k,v in value.items() if k!='sizes'},indent=1))
        for n,e in value['sizes'].items():print(n,e['mb'],'MB',e['files'],'files');print(table(e['hooks']) if 'hooks' in e else '')
        if a.json:Path(a.json).write_text(json.dumps(value,indent=1),encoding='utf-8')

if __name__=='__main__':main()
