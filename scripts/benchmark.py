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

def synthetic_store(base,count,semantic_from=None):
    """(store root, project folder, ms to build the first index) for `count` generated notes under `base`.
    Notes are written as files and indexed once: capturing them one by one would time the very cost being measured."""
    import random,shutil
    from core.notes import build_note
    from core.repository import reconcile
    rng=random.Random(7);root=base/'store';project=base/'project';project.mkdir()
    init_domain('scale',[str(project)],root);p=migration_plan(root);apply(root,p['plan_id'],p['digest'])
    if semantic_from:shutil.copytree(Path(semantic_from)/'.models',root/'.models')
    for i in range(count):
        words=rng.sample(WORDS,6);title=f"{' '.join(words[:3]).capitalize()} note {i}"
        body=('## \U0001f4cc Executive Summary\n'+' '.join(rng.choices(WORDS,k=30))+'.\n\n---\n\n## \U0001f4dd Key Details\n'
              +'\n'.join(f"* **{word.capitalize()}:** "+' '.join(rng.choices(WORDS,k=25))+'.' for word in words))
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

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scale',type=int,help='Time a prompt hook and a capture on a synthetic store of this many notes')
    parser.add_argument('--semantic-from',type=Path,help='With --scale: a store whose installed semantic model is copied into the synthetic one')
    args=parser.parse_args()
    if args.scale:raise SystemExit(0 if scale(args.scale,args.semantic_from)['met'] else 1)
    main()
