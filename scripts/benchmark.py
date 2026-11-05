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
if __name__=='__main__':main()
