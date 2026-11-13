"""Reproducible synthetic retrieval quality/latency check; never touches user memory."""
import json,statistics,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.init_domain import init_domain
from core.migration import migration_plan
from core.governance import apply
from core.notes import capture
from core.search import search_domains

def main():
    cases=[('Cache eviction policy','The cache uses least recently used eviction.','Please explain how our cache eviction works'),('Deployment rollback','Deployment rollback restores the prior immutable release.','How should we handle a deployment rollback?'),('Authentication sessions','Authentication sessions expire after thirty minutes.','What is the authentication session expiry?'),('Database backups','Database backups run before schema migrations.','Tell me about database backups before migrations')]
    with tempfile.TemporaryDirectory(prefix='living-memory-benchmark-') as directory:
        root=Path(directory);init_domain('benchmark',[str(root/'project')],root);p=migration_plan(root);apply(root,p['plan_id'],p['digest']);expected=[]
        for title,body,query in cases:expected.append(capture(title,body,['subject/project','concern/knowledge'],'benchmark',root)['id'])
        for i in range(96):capture('Reference '+str(i),'Synthetic unrelated reference number '+str(i)+': violet meadow pebble.',['subject/project','concern/knowledge'],'benchmark',root)
        durations=[];quality=[]
        for i,(title,body,query) in enumerate(cases):
            hits=search_domains(query,['benchmark'],3,root);quality.append({'query':query,'expected_title':title,'top1_correct':bool(hits and hits[0]['id']==expected[i])})
        for i in range(40):
            start=time.perf_counter();search_domains(cases[i%4][2],['benchmark'],3,root);durations.append((time.perf_counter()-start)*1000)
        value={'schema':1,'corpus':'100 synthetic notes: 4 targets + 96 distractors','runs':len(durations),'quality_cases':quality,'top1_accuracy':sum(q['top1_correct'] for q in quality)/len(quality),'median_ms':round(statistics.median(durations),3),'p95_ms':round(sorted(durations)[int(len(durations)*.95)-1],3),'includes':'canonical fingerprint checks, FTS5, ranking, bounded output; excludes interpreter startup','limitation':'Small synthetic check, not real-world relevance certification or a latency guarantee'}
        output=Path(__file__).resolve().parents[1]/'.cache/benchmark.json';output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(value,indent=2),encoding='utf-8');print(json.dumps(value))

# What keeps a hook inside its timeout, and a write tolerable, as a store grows (in-process, median).
TARGETS={'prompt_ms':400,'capture_ms':1500,'bash_pre_extra_p95_ms':15}
LONG_RUNS=20
WORDS=('cache deploy rollback session token queue index schema migration retry timeout webhook parser lexer renderer export import vault ledger '
       'invoice tenant shard replica gateway proxy cluster build release artifact pipeline lint format logger metric tracer alert budget quota '
       'limit policy audit secret rotate backup restore snapshot journal').split()

def synthetic_store(base,count,semantic_from=None,model_dir=None):
    """(store root, project folder, ms to build the first index) for `count` generated notes under `base`.
    Notes are written as files and indexed once: capturing them one by one would time the very cost being measured."""
    import random,shutil
    from core.notes import build_note
    from core.repository import reconcile
    rng=random.Random(7);root=base/'store';project=base/'project';project.mkdir()
    init_domain('scale',[str(project)],root);p=migration_plan(root);apply(root,p['plan_id'],p['digest'])
    if semantic_from:shutil.copytree(Path(semantic_from)/'.models',root/'.models')
    if model_dir:shutil.copytree(model_dir,root/'.models'/Path(model_dir).name)   # a copy: the store being measured is never the real one
    for i in range(count):
        words=rng.sample(WORDS,6);title=f"{' '.join(words[:3]).capitalize()} note {i}"
        body=(' '.join(rng.choices(WORDS,k=30))+'.\n\n## Details\n'
              +'\n'.join(f"- **{word.capitalize()}:** "+' '.join(rng.choices(WORDS,k=25))+'.' for word in words))
        path,_,text=build_note(title,body,['subject/project','concern/knowledge'],'scale',root)
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8',newline='\n')
    started=time.perf_counter();reconcile('scale',root)
    return root,project,round((time.perf_counter()-started)*1000)

def scale(count,semantic_from=None,runs=7):
    """Latency at `count` notes: a prompt hook, a capture and a session start, run in-process on a synthetic store.
    semantic_from: a store whose installed model (.models) is copied in, so writes pay for embeddings as they do in use."""
    from core import repository,runtime,semantic
    with tempfile.TemporaryDirectory(prefix='living-memory-scale-',ignore_cleanup_errors=True) as directory:
      try:
        root,project,built=synthetic_store(Path(directory),count,semantic_from)
        payload={'cwd':str(project),'session_id':'scale'}
        runtime.process('claude','session-start',payload,root)
        def timed(action,cold=True):
            # cold: forget the notes this process has read before each run, as a hook or a command (a new
            # process every time) starts. warm is what a long-lived `serve` process sees.
            samples=[]
            for i in range(runs):
                if cold:repository._PARSED.clear()
                start=time.perf_counter();action(i);samples.append((time.perf_counter()-start)*1000)
            return round(statistics.median(samples),1)
        ask=lambda i:runtime.process('claude','user-prompt',{**payload,'prompt':f'how does the {WORDS[i]} {WORDS[i+9]} retry policy work'},root)
        prompt=timed(ask);warm=timed(lambda i:ask(i+runs),cold=False)
        # A ~3 KB task brief (team boilerplate around a one-sentence assignment), as a coordinator sends a sub-agent.
        boiler=('Team rules for this sprint. Never push and never touch other branches. Work in your own worktree only. Role: backend engineer, '
                'owning the service layer; the reviewer reads every diff. Report format: three bullets, what changed, what you verified, what is left. ')
        sections=lambda i:'\n\n'.join(['# Team brief',boiler*3+f'Owner: note {i+5}.','## Your assignment',f'Check note {i+20} and note {i+60} on the {WORDS[i]} {WORDS[i+9]} retry policy, then note {i+100}.','## Background',boiler*3+f'See note {i+130}.','## Checklist',boiler*3+f'Then note {i+170}.'])
        brief=lambda i:runtime.process('claude','user-prompt',{**payload,'session_id':f'brief{i}','prompt':sections(i)},root)
        longs=[]
        for i in range(LONG_RUNS):
            repository._PARSED.clear();start=time.perf_counter();brief(i);longs.append((time.perf_counter()-start)*1000)
        longs.sort()
        # PreToolUse on a Bash call: command hints off (the lookup stubbed out) against on. Every call is a new command shape
        # in a new session, so none is skipped as already looked up; the hit rows run a command three notes warn about.
        for i in range(3):capture(f'Command gotcha {i} zebra{i}',f'Running `./deploy{i} apply` skips the lint step and exits 0.',['subject/project','concern/knowledge'],'scale',root)
        from unittest.mock import patch
        def bash(kind,i):
            start=time.perf_counter();runtime.process('claude','pre-tool-use',{**payload,'session_id':f'{kind}{i}','tool_name':'Bash','tool_input':{'command':f'./{"deploy"+str(i%3) if kind=="hit" else "build"+str(i)} apply'}},root)
            return (time.perf_counter()-start)*1000
        with patch.object(runtime,'command_hints',lambda *a,**k:None):base=sorted(bash('base',i) for i in range(LONG_RUNS))
        miss=sorted(bash('miss',i) for i in range(LONG_RUNS));hit=sorted(bash('hit',i) for i in range(LONG_RUNS))
        p95=lambda xs:xs[int(len(xs)*.95)-1]
        written=timed(lambda i:capture(f'Scale capture {i} zebra{i}',f'Distinct fact number {i} about quokka{i}.',['subject/project','concern/knowledge'],'scale',root))
        started_ms=timed(lambda i:runtime.process('claude','session-start',{**payload,'session_id':f'scale{i}'},root))
        value={'schema':1,'notes':count,'semantic':bool(semantic_from),'runs':runs,'first_index_build_ms':built,'prompt_ms':prompt,'prompt_warm_ms':warm,'prompt_long_ms':round(statistics.median(longs),1),'prompt_long_p95_ms':round(longs[int(len(longs)*.95)-1],1),
                              'capture_ms':written,'session_start_ms':started_ms,'bash_pre_off_ms':round(statistics.median(base),1),'bash_pre_off_p95_ms':round(p95(base),1),'bash_pre_ms':round(statistics.median(miss),1),'bash_pre_p95_ms':round(p95(miss),1),'bash_pre_hit_ms':round(statistics.median(hit),1),'bash_pre_hit_p95_ms':round(p95(hit),1),'targets_ms':TARGETS,'met':prompt<=TARGETS['prompt_ms'] and written<=TARGETS['capture_ms'] and max(p95(miss),p95(hit))-p95(base)<=TARGETS['bash_pre_extra_p95_ms'],
               'includes':'everything a hook or command does in one new process; excludes interpreter start, imports and the first model load'}
        print(json.dumps(value));return value
      finally:semantic._unload()   # the model file is memory-mapped; Windows cannot delete the folder while it is

# The knowledge base (core/sources.py) at scale: a first sync, an idle one, ask and the two hook paths on a repo of generated files.
SOURCE_TARGETS={'first_sync_s':60,'idle_sync_s':5,'ask_p95_ms':300,'session_extra_ms':100,'prompt_extra_p95_ms':40}
TOPICS=('invoice ledger payment settlement refund currency tenant shard replica gateway proxy cluster build release artifact pipeline quota '
        'limit policy audit secret rotate backup restore snapshot journal webhook parser lexer renderer export import vault queue index schema').split()

def sources_scale(count=5000,runs=30):
    """Time the knowledge base on `count` small generated files (markdown, Python and Java in nested folders, a few lines each).
    Returns first_sync_s, idle_sync_s (nothing changed), ask_p95_ms (notes plus excerpts, in process), session_check_ms (the
    staleness check alone), session_extra_ms and _p95_ms (a SessionStart with the knowledge-base line against one without) and
    prompt_extra_p95_ms (a prompt hook with the excerpt lookup against one without)."""
    import os,random
    from unittest.mock import patch
    from core import runtime,sources
    from core.cli import _ask
    rng=random.Random(11)
    with tempfile.TemporaryDirectory(prefix='living-memory-sources-',ignore_cleanup_errors=True) as directory,patch.dict(os.environ,{'LIVING_MEMORY_NO_BACKGROUND':'1'}):
        base=Path(directory);root=base/'store';repo=base/'project'/'repo';init_domain('kb',[str(base/'project')],root);(repo/'.git').mkdir(parents=True)
        for i in range(count):
            folder=repo/'src'/f'pkg{i%40}'/f'mod{i%7}';folder.mkdir(parents=True,exist_ok=True);words=rng.choices(TOPICS,k=40)
            kind=i%3
            if kind==0:(folder/f'doc{i}.md').write_text(f'# {words[0].title()} {words[1]} {i}\n\n'+' '.join(words[2:20])+f'.\n\n## {words[3].title()} rules\n\n'+' '.join(words[20:])+f' unique{i}.\n',encoding='utf-8')
            elif kind==1:(folder/f'mod{i}.py').write_text(f'\"\"\"{" ".join(words[:8])}\"\"\"\n\nclass {words[0].title()}Handler{i}:\n    def {words[1]}_{words[2]}(self, x):\n        # {" ".join(words[8:20])}\n        return x\n\n    def {words[3]}_{words[4]}(self):\n        return "{" ".join(words[20:30])}"\n',encoding='utf-8')
            else:(folder/f'Svc{i}.java').write_text(f'public class {words[0].title()}Service{i} {{\n    /** {" ".join(words[:10])} */\n    public void {words[1]}{words[2].title()}(String a) {{\n        // {" ".join(words[10:25])}\n    }}\n}}\n',encoding='utf-8')
        started=time.perf_counter();first=sources.sync(root,'kb');first_s=time.perf_counter()-started
        started=time.perf_counter();idle=sources.sync(root,'kb');idle_s=time.perf_counter()-started
        topic=lambda i:TOPICS[i%len(TOPICS)];questions=[f'how does the {topic(i)} {topic(i+11)} {topic(i+5)} work' for i in range(runs)]
        asked=[]
        for q in questions:
            started=time.perf_counter();_ask(root,'kb',q,5);asked.append((time.perf_counter()-started)*1000)
        started=time.perf_counter();sources.stale(root,'kb');check=(time.perf_counter()-started)*1000
        payload={'cwd':str(base/'project'),'session_id':'bench'};runtime.process('claude','session-start',payload,root)
        def starts(on):
            samples=[]
            for i in range(runs):
                with patch.object(sources,'session_line',(lambda *a,**k:'') if not on else sources.session_line):
                    started=time.perf_counter();runtime.process('claude','session-start',{**payload,'session_id':f's{on}{i}'},root);samples.append((time.perf_counter()-started)*1000)
            return sorted(samples)
        def prompts(on):
            samples=[]
            for i,q in enumerate(questions):
                text=f'what is the {topic(i+3)} {topic(i+17)} {topic(i+9)} policy for tenant{i}'
                with patch.object(runtime,'prompt_sources',(lambda *a,**k:(None,[])) if not on else runtime.prompt_sources):
                    started=time.perf_counter();runtime.process('claude','user-prompt',{**payload,'session_id':f'p{on}{i}','prompt':text},root);samples.append((time.perf_counter()-started)*1000)
            return sorted(samples)
        off=prompts(False);on=prompts(True);p95=lambda xs:xs[int(len(xs)*.95)-1];start_off=starts(False);start_on=starts(True)
        value={'schema':1,'files':count,'indexed_files':first['files'],'chunks':first['chunks'],'first_sync_s':round(first_s,2),'idle_sync_s':round(idle_s,2),'idle_unchanged':idle['sources'][0]['unchanged'],
               'ask_median_ms':round(statistics.median(asked),1),'ask_p95_ms':round(p95(sorted(asked)),1),'session_check_ms':round(check,1),'session_off_p95_ms':round(p95(start_off),1),'session_on_p95_ms':round(p95(start_on),1),'session_extra_ms':round(statistics.median(start_on)-statistics.median(start_off),1),'session_extra_p95_ms':round(p95(start_on)-p95(start_off),1),
               'prompt_off_p95_ms':round(p95(off),1),'prompt_on_p95_ms':round(p95(on),1),'prompt_extra_p95_ms':round(p95(on)-p95(off),1),'targets':SOURCE_TARGETS}
        value['met']=value['first_sync_s']<=SOURCE_TARGETS['first_sync_s'] and value['idle_sync_s']<=SOURCE_TARGETS['idle_sync_s'] and value['ask_p95_ms']<=SOURCE_TARGETS['ask_p95_ms'] and value['session_extra_ms']<=SOURCE_TARGETS['session_extra_ms'] and value['prompt_extra_p95_ms']<=SOURCE_TARGETS['prompt_extra_p95_ms']
        print(json.dumps(value));return value

# What a user feels: the SessionStart and prompt hooks (in-process p50/p95; each is also a Python start of about 100-150 ms).
LATENCY_TARGETS={'session_second_p50_ms':250,'semantic_prompt_p50_ms':120}

def _git(repo,*argv,when=None):
    import os,subprocess
    env={**os.environ,'GIT_AUTHOR_NAME':'b','GIT_AUTHOR_EMAIL':'b@example.org','GIT_COMMITTER_NAME':'b','GIT_COMMITTER_EMAIL':'b@example.org'}
    if when:env.update(GIT_AUTHOR_DATE=when,GIT_COMMITTER_DATE=when)
    subprocess.run(['git','-C',str(repo),*argv],check=True,capture_output=True,env=env)

def _stats(samples):
    samples=sorted(samples);return {'p50_ms':round(statistics.median(samples),1),'p95_ms':round(samples[max(0,int(len(samples)*.95)-1)],1)}

def session_start_case(runs=15):
    """SessionStart in a bound git repo with a codebase map and a brief that cite a file changed after they were written, so both are
    STALE and need git: the first session of a store (no currency cache) against later sessions at the same HEAD."""
    import os
    from datetime import datetime,timedelta,timezone
    from unittest.mock import patch
    from core import repository,runtime
    with tempfile.TemporaryDirectory(prefix='living-memory-session-',ignore_cleanup_errors=True) as directory,patch.dict(os.environ,{'LIVING_MEMORY_NO_BACKGROUND':'1'}):
        base=Path(directory);root=base/'store';project=base/'project';repo=project/'app';repo.mkdir(parents=True)
        init_domain('bench',[str(project)],root);p=migration_plan(root);apply(root,p['plan_id'],p['digest'])
        _git(repo,'init','-q');_git(repo,'remote','add','origin','https://example.org/acme/app.git')
        for name in ('pom.xml','src/Money.java','src/Other.java','README.md'):
            path=repo/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(f'{name} v1\n',encoding='utf-8')
        _git(repo,'add','-A');_git(repo,'commit','-q','-m','initial',when='2020-01-01T00:00:00+00:00')
        tags=['subject/project','concern/architecture']
        capture('app codebase map','Money service.\n\n* **Layout:** src/Money.java holds the money type; src/Other.java the rest; pom.xml builds it.',tags,'bench',root,type='map',files=[str(repo)])
        capture('app project brief','Money service for Acme.\n\n* **Goals:** ship the ledger.\n* **Key decisions:** money is long cents in src/Money.java.',tags,'bench',root,type='brief',files=[str(repo)])
        for i in range(30):capture(f'Reference {i}',f'Synthetic reference number {i}.',tags,'bench',root)
        (repo/'src'/'Money.java').write_text('src/Money.java v2\n',encoding='utf-8');_git(repo,'add','-A')
        _git(repo,'commit','-q','-m','money changed',when=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(timespec='seconds'))
        cache=root/'.cache'/'currency.json';n=[0]
        def start(clear):
            if clear:cache.unlink(missing_ok=True)
            repository._PARSED.clear();n[0]+=1;began=time.perf_counter()
            context=runtime.process('claude','session-start',{'cwd':str(repo),'session_id':f'b{n[0]}'},root)['context']
            return (time.perf_counter()-began)*1000,context
        _,context=start(True)   # unmeasured: first import and index work
        first=[start(True)[0] for _ in range(runs)];second=[start(False)[0] for _ in range(runs)]
        return {'stale_shown':'STALE:' in context,'first_session':_stats(first),'second_session':_stats(second),'cache_file':cache.is_file()}

def prompt_cases(model_dir=None,runs=20,count=200):
    """A prompt hook on `count` generated notes, run as a new process would (notes re-read, the model loaded again): a lexical prompt, a
    10-question quiz prompt (one query per question), and with `model_dir` (a potion model folder, copied into the temporary store) the
    same two with semantic search plus the model load with and without the tokenizer cache."""
    import os
    from unittest.mock import patch
    from core import repository,runtime,semantic
    with tempfile.TemporaryDirectory(prefix='living-memory-prompts-',ignore_cleanup_errors=True) as directory,patch.dict(os.environ,{'LIVING_MEMORY_NO_BACKGROUND':'1'}):
      try:
        root,project,_=synthetic_store(Path(directory),count,model_dir=model_dir)
        payload={'cwd':str(project),'session_id':'bench'};runtime.process('claude','session-start',payload,root)
        words=lambda i:(WORDS[i%len(WORDS)],WORDS[(i+9)%len(WORDS)])
        single=lambda i:'how does the %s %s retry policy work'%words(i)
        quiz=lambda i:'Answer these questions about the project:\n'+'\n'.join(f'{k+1}. How does the %s %s retry policy work?'%words(i+k*3) for k in range(10))
        n=[0];runtime.process('claude','user-prompt',{**payload,'session_id':'warm','prompt':single(0)},root)   # unmeasured: first imports and page cache
        def timed(text):
            samples=[]
            for i in range(runs):
                repository._PARSED.clear();semantic._unload();n[0]+=1;began=time.perf_counter()
                runtime.process('claude','user-prompt',{**payload,'session_id':f'p{n[0]}','prompt':text(i)},root);samples.append((time.perf_counter()-began)*1000)
            return _stats(samples)
        result={'notes':count,'semantic':bool(model_dir)}
        if model_dir:   # the lexical numbers come from the same store with the model switched off
            with patch.object(semantic,'USE_SEMANTIC',False):result['lexical_prompt']=timed(single);result['lexical_list_prompt']=timed(quiz)
            result['semantic_prompt']=timed(single);result['semantic_list_prompt']=timed(quiz)
            marshaled=semantic.models_dir(root)/semantic.installed(root)/'tokenizer.marshal'
            def load_ms():
                semantic._unload();began=time.perf_counter();semantic.load(root);return (time.perf_counter()-began)*1000
            marshaled.unlink(missing_ok=True);result['model_load_without_cache_ms']=round(load_ms(),1)
            result['model_load_cached_ms']=round(statistics.median(load_ms() for _ in range(5)),1)
        else:result['lexical_prompt']=timed(single);result['lexical_list_prompt']=timed(quiz)
        return result
      finally:semantic._unload()

def latency(model_dir=None):
    """The session-start and prompt cases; prints one JSON line and returns it. `met`: every target that could be measured was met."""
    session=session_start_case();prompts=prompt_cases(model_dir)
    value={'schema':1,'session_start':session,'prompts':prompts,'targets_ms':LATENCY_TARGETS,'model_dir':str(model_dir) if model_dir else None}
    met=[session['second_session']['p50_ms']<=LATENCY_TARGETS['session_second_p50_ms']]
    if model_dir:met.append(prompts['semantic_prompt']['p50_ms']<=LATENCY_TARGETS['semantic_prompt_p50_ms'])
    value['met']=all(met);print(json.dumps(value));return value

def vectors_case(count,dim=512,queries=20,top=20,seed=7):
    """SYNTHETIC: `count` random unit vectors (not a real corpus). The unit index's two backends over the same blobs:
    the Python dot-product scan and sqlite-vec's exact vec0 KNN. Build time, file size, query p50/p95, extension load
    time and top-`top` agreement. Says so and skips the native half when sqlite-vec does not load."""
    import array,math,os,random,sqlite3
    from core import vectors
    rng=random.Random(seed)
    def unit():
        v=[rng.gauss(0,1) for _ in range(dim)];n=math.sqrt(sum(x*x for x in v));return array.array('f',(x/n for x in v))
    with tempfile.TemporaryDirectory(prefix='living-memory-vectors-') as directory:
        path=Path(directory)/'v.sqlite3';db=sqlite3.connect(path)
        started=time.perf_counter()
        db.execute('CREATE TABLE blob(rowid INTEGER PRIMARY KEY,vec BLOB NOT NULL)')
        db.executemany('INSERT INTO blob VALUES(?,?)',((i,vectors._blob(unit())) for i in range(1,count+1)));db.commit()
        python_build=time.perf_counter()-started;python_size=path.stat().st_size
        loads=[]
        for _ in range(5):
            probe=sqlite3.connect(':memory:');started=time.perf_counter();ok,info=vectors.load(probe);loads.append((time.perf_counter()-started)*1000);probe.close()
        value={'schema':1,'synthetic':True,'vectors':count,'dim':dim,'queries':queries,'top':top,'python':{'build_s':round(python_build,2),'size_bytes':python_size},
               'native':{'loadable':ok,**({'version':info} if ok else {'reason':info})}}
        asked=[unit() for _ in range(queries)]
        def python_top(q):
            return [r for _,r in heapq.nsmallest(top,((-vectors._dot(q,vectors._vector(b)),r) for r,b in db.execute('SELECT rowid,vec FROM blob')))]
        import heapq
        times=[];python_hits=[]
        for q in asked:
            started=time.perf_counter();python_hits.append(python_top(q));times.append((time.perf_counter()-started)*1000)
        value['python'].update(p50_ms=round(statistics.median(times),2),p95_ms=round(sorted(times)[max(0,int(len(times)*.95)-1)],2))
        if ok:
            vectors.load(db)
            started=time.perf_counter()
            db.execute(f'CREATE VIRTUAL TABLE knn USING vec0(embedding float[{dim}] distance_metric=cosine)')
            db.execute('INSERT INTO knn(rowid,embedding) SELECT rowid,vec FROM blob');db.commit()
            native_build=time.perf_counter()-started
            times=[];same=0
            for q,expected in zip(asked,python_hits):
                started=time.perf_counter()
                got=[r for r,_ in sorted(db.execute('SELECT rowid,distance FROM knn WHERE embedding MATCH ? AND k=? ORDER BY distance',(vectors._blob(q),top)),key=lambda r:(r[1],r[0]))]
                times.append((time.perf_counter()-started)*1000);same+=got==expected
            value['native'].update(build_s=round(native_build,2),size_bytes_with_vec0=path.stat().st_size,p50_ms=round(statistics.median(times),2),
                                   p95_ms=round(sorted(times)[max(0,int(len(times)*.95)-1)],2),load_ms=[round(x,2) for x in loads],identical_top=f'{same}/{queries}')
        db.close()
    print(json.dumps(value));return value

def hook_case(store,cwd,prompt,runs=10):
    """The real prompt hook as a fresh process (as harnesses run it) against a store, native sqlite-vec on and off.
    The store's session state is written (use a copy). First run reported apart (cold file cache), then p50/p95."""
    import os,subprocess
    script=Path(__file__).resolve().parents[1]/'hooks'/'claude_hooks.py';value={'schema':1,'store':'(given copy)','prompt_chars':len(prompt),'runs':runs}
    for label,flag in (('native','1'),('python','0')):
        times=[];chars=[]
        for i in range(runs):
            payload=json.dumps({'cwd':str(cwd),'session_id':f'bench-{label}-{i}','turn_id':'t','prompt':prompt,'hook_event_name':'UserPromptSubmit'})
            env={**os.environ,'LIVING_MEMORY_ROOT':str(store),'LIVING_MEMORY_NATIVE':flag,'PYTHONUTF8':'1'}
            started=time.perf_counter();out=subprocess.run([sys.executable,str(script),'user-prompt'],input=payload,capture_output=True,text=True,env=env,timeout=60)
            times.append((time.perf_counter()-started)*1000)
            try:chars.append(len(json.loads(out.stdout or '{}').get('hookSpecificOutput',{}).get('additionalContext','')))
            except ValueError:chars.append(None)
        rest=sorted(times[1:])
        value[label]={'first_ms':round(times[0],1),'p50_ms':round(statistics.median(rest),1),'p95_ms':round(rest[max(0,int(len(rest)*.95)-1)],1),'context_chars':chars[-1]}
    print(json.dumps(value));return value

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vectors',type=int,help='SYNTHETIC: compare the Python scan and sqlite-vec KNN over this many random 512-dimension vectors')
    parser.add_argument('--queries',type=int,default=20,help='With --vectors: queries to time')
    parser.add_argument('--hook-store',type=Path,help='Time the real prompt hook process against this store (a copy: session state is written), native on and off')
    parser.add_argument('--hook-cwd',type=Path,help='With --hook-store: a folder bound to a domain in that store')
    parser.add_argument('--hook-prompt',default='why did we choose static embeddings over onnx for semantic search',help='With --hook-store: the prompt')
    parser.add_argument('--scale',type=int,help='Time a prompt hook and a capture on a synthetic store of this many notes')
    parser.add_argument('--semantic-from',type=Path,help='With --scale: a store whose installed semantic model is copied into the synthetic one')
    parser.add_argument('--model-dir',type=Path,help='A potion model folder (e.g. <store>/.models/potion-retrieval-32M); copied into a temporary store for the semantic prompt case, skipped when absent')
    parser.add_argument('--sources',type=int,help='Time the knowledge base (sync, ask, the hook paths) on this many generated files')
    args=parser.parse_args()
    if args.vectors:vectors_case(args.vectors,queries=args.queries);raise SystemExit(0)
    if args.hook_store:hook_case(args.hook_store,args.hook_cwd or Path.cwd(),args.hook_prompt);raise SystemExit(0)
    if args.sources:raise SystemExit(0 if sources_scale(args.sources)['met'] else 1)
    if args.scale:raise SystemExit(0 if scale(args.scale,args.semantic_from)['met'] else 1)
    main()
    model=args.model_dir if args.model_dir and (args.model_dir/'verified.json').is_file() else None
    if args.model_dir and not model:print(f'semantic case skipped: no verified model in {args.model_dir}')
    raise SystemExit(0 if latency(model)['met'] else 1)
