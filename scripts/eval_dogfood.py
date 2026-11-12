"""Dogfood evaluation: index a repository as the knowledge base of a fresh store and ask written questions about it.

No model and no network. The repository's tracked files are copied into a temporary folder (so nothing in the real
checkout or the real store is touched), bound to a domain of an empty temporary store, indexed with `source-sync`, and each
question goes through the real `ask` command. A question passes when one of its expected files is among the cited excerpts.

    py -3.15 scripts/eval_dogfood.py [--repo PATH] [--questions FILE] [--limit 5] [--json] [--out DIR]

Questions: {"questions": [{"q": "...", "expect": ["path/one.py", "DOC.md"]}, ...]}. Reports hit@1, hit@LIMIT, MRR, per-question
results, sync time and ask p50/p95 in ms, and writes the report to .cache/eval/dogfood-<UTC timestamp>.json.
"""
from __future__ import annotations
import argparse,contextlib,io,json,os,shutil,subprocess,sys,tempfile,time
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.cli import main

TARGET=0.8
DEFAULT_QUESTIONS=ROOT/'tests'/'fixtures'/'dogfood_questions.json'
SKIP_PARTS={'.git','.cache','__pycache__','node_modules'}

def tracked_files(repo):
    """Relative paths of the repo's tracked files (git ls-files), else every file outside caches and VCS folders."""
    repo=Path(repo)
    try:
        out=subprocess.run(['git','ls-files','-z'],cwd=repo,capture_output=True,check=True,timeout=60).stdout
        names=[n for n in out.decode('utf-8').split('\0') if n]
        if names:return names
    except (OSError,subprocess.SubprocessError):pass
    names=[]
    for base,dirs,files in os.walk(repo):
        dirs[:]=[d for d in dirs if d not in SKIP_PARTS]
        names+=[(Path(base)/f).relative_to(repo).as_posix() for f in files]
    return sorted(names)

def copy_repo(repo,dest,skip=()):
    """Copy text files into dest and make it a git repo; returns the number copied. Binaries and the skipped paths stay out."""
    repo=Path(repo).resolve();dest=Path(dest);skip={Path(s).resolve() for s in skip};copied=0
    for rel in tracked_files(repo):
        source=repo/rel
        if SKIP_PARTS&set(Path(rel).parts) or source.resolve() in skip or not source.is_file():continue
        try:
            with open(source,'rb') as handle:
                if b'\0' in handle.read(8192):continue
        except OSError:continue
        target=dest/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target);copied+=1
    try:subprocess.run(['git','init','-q'],cwd=dest,capture_output=True,check=True,timeout=60)
    except (OSError,subprocess.SubprocessError):(dest/'.git').mkdir(exist_ok=True)   # source discovery needs only the folder
    return copied

def cli(root,domain,cwd,*argv):
    """Run one real command in-process; returns (exit code, parsed JSON)."""
    out,err=io.StringIO(),io.StringIO()
    with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
        try:code=main(['--root',str(root),'--domain',domain,'--cwd',str(cwd),*argv])
        except SystemExit as exc:code=exc.code if isinstance(exc.code,int) else 2
    text=(out.getvalue() or err.getvalue()).strip()
    try:return code,json.loads(text)
    except ValueError:return code,{'text':text[:2000]}

def percentile(values,fraction):
    if not values:return None
    ordered=sorted(values);return ordered[min(len(ordered)-1,int(round(fraction*(len(ordered)-1))))]

def score(question,cited):
    """Rank (1-based) of the first cited path that the question expects, else None."""
    expect={e.replace('\\','/') for e in question['expect']}
    for rank,path in enumerate(cited,1):
        if path.replace('\\','/') in expect:return rank
    return None

def load_questions(path):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    questions=data['questions'] if isinstance(data,dict) else data
    for q in questions:
        if not isinstance(q.get('q'),str) or not q['q'].strip() or not q.get('expect') or not all(isinstance(e,str) for e in q['expect']):
            raise ValueError(f'Question needs "q" text and a non-empty "expect" list of paths: {q!r}')
    return questions

def evaluate(repo,questions,limit=5,skip=()):
    """Copy repo into a fresh store, sync, ask every question; returns the report dict. skip: files left out of the copy (the questions file)."""
    repo=Path(repo).resolve();limit=max(1,limit)
    with tempfile.TemporaryDirectory(prefix='lm-dogfood-') as tmp:
        tmp=Path(tmp);project=tmp/'project';project.mkdir();store=tmp/'store';store.mkdir()
        saved=os.environ.get('LIVING_MEMORY_ROOT');os.environ['LIVING_MEMORY_ROOT']=str(store)   # nothing may reach the user's real store
        try:
            files=copy_repo(repo,project,skip)
            code,bound=cli(store,'dogfood',project,'bind','dogfood',str(project))
            if code:raise RuntimeError(f'bind failed: {bound}')
            started=time.perf_counter();code,synced=cli(store,'dogfood',project,'source-sync');sync_ms=(time.perf_counter()-started)*1000
            if code:raise RuntimeError(f'source-sync failed: {synced}')
            results=[]
            for q in questions:
                started=time.perf_counter();code,answer=cli(store,'dogfood',project,'ask','--limit',str(limit),q['q']);ms=(time.perf_counter()-started)*1000
                if code:raise RuntimeError(f'ask failed for {q["q"]!r}: {answer}')
                cited=[s['path'] for s in answer.get('sources',[])][:limit];rank=score(q,cited)
                results.append({'q':q['q'],'expect':q['expect'],'cited':cited,'rank':rank,'hit':rank is not None,'ms':round(ms,1)})
        finally:
            if saved is None:os.environ.pop('LIVING_MEMORY_ROOT',None)
            else:os.environ['LIVING_MEMORY_ROOT']=saved
    n=len(results);times=[r['ms'] for r in results]
    hit1=sum(1 for r in results if r['rank']==1)/n if n else 0.0;hitk=sum(r['hit'] for r in results)/n if n else 0.0
    return {'repo':str(repo),'files_copied':files,'limit':limit,'questions':n,'hit@1':round(hit1,3),f'hit@{limit}':round(hitk,3),
            'mrr':round(sum(1/r['rank'] for r in results if r['rank'])/n,3) if n else 0.0,'target':TARGET,'target_met':hitk>=TARGET,
            'sync_ms':round(sync_ms,1),'ask_ms_p50':round(percentile(times,0.5) or 0,1),'ask_ms_p95':round(percentile(times,0.95) or 0,1),
            'knowledge_base':{k:synced.get(k) for k in ('ok','files','chunks','indexed','skipped') if k in synced},'results':results}

def render(report):
    limit=report['limit'];lines=[f"Dogfood: {report['repo']} ({report['files_copied']} files copied)"]
    for i,r in enumerate(report['results'],1):
        lines.append(f"{'PASS' if r['hit'] else 'FAIL'} {i:>2} rank={r['rank'] or '-'} {r['ms']:>6.1f}ms  {r['q'][:78]}")
        if not r['hit']:lines.append(f"       expected {', '.join(r['expect'])}; cited {', '.join(r['cited']) or 'nothing'}")
    lines.append(f"hit@1 {report['hit@1']:.0%}  hit@{limit} {report[f'hit@{limit}']:.0%}  MRR {report['mrr']:.3f}  sync {report['sync_ms']:.0f} ms  ask p50 {report['ask_ms_p50']:.0f} ms p95 {report['ask_ms_p95']:.0f} ms")
    lines.append(f"Target hit@{limit} >= {report['target']:.0%}: {'MET' if report['target_met'] else 'NOT MET'}")
    return '\n'.join(lines)

def run(argv=None):
    p=argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--repo',type=Path,default=ROOT);p.add_argument('--questions',type=Path,default=DEFAULT_QUESTIONS)
    p.add_argument('--limit',type=int,default=5);p.add_argument('--json',action='store_true',help='Print the full report as JSON')
    p.add_argument('--out',type=Path,help='Folder for the report file (default: .cache/eval in this checkout)')
    a=p.parse_args(argv)
    report=evaluate(a.repo,load_questions(a.questions),a.limit,[a.questions,DEFAULT_QUESTIONS])   # a questions file must not be indexed as an answer to itself
    out=(a.out or ROOT/'.cache'/'eval');out.mkdir(parents=True,exist_ok=True)
    path=out/f"dogfood-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json";path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2) if a.json else render(report)+f'\nReport: {path}')
    return report

if __name__=='__main__':
    try:run()
    except (OSError,ValueError,RuntimeError,KeyError) as exc:
        print(f'eval_dogfood: {exc}',file=sys.stderr);raise SystemExit(1)
