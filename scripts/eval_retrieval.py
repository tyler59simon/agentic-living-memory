"""Score Living Memory retrieval against a fixture of prompts and the notes they should (not) surface.

Runs the real prompt-injection (runtime.context), SessionStart (session_start.build_context) and
conversational-signal (runtime.durable_signal) code. It never changes canonical notes; derived indexes
may refresh. A fixture with a "notes" list is synthetic: it is built into a temporary store and its
cases name notes as "@key". Otherwise cases name real notes by ref (short refs and exact titles work).

    py -3.15 scripts/eval_retrieval.py --fixture ~/LivingMemory/.eval/sandbox.json [--root STORE] [--json]
"""
from __future__ import annotations
import argparse,json,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import runtime,session_start
from core.notes import resolve_ref,short_ref

SIGNAL_KINDS=('remember','correction','preference','decision','directive')

def _build(fixture,root):
    """A temporary store holding the fixture's notes; returns (cwd, domain, {key: 'domain:id'})."""
    from core.init_domain import init_domain
    from core.notes import capture
    from core.repository import atomic_write,canonical_json
    domain=fixture.get('domain','eval');project=root.parent/'project';project.mkdir(exist_ok=True)
    init_domain(domain,[str(project)],root)
    tags=sorted({t for n in fixture['notes'] for t in n.get('tags',['subject/project','concern/knowledge'])})
    atomic_write(root/'tag_taxonomy.json',canonical_json({'tags':tags}))
    keys={}
    for n in fixture['notes']:
        made=capture(n['title'],n['body'],n.get('tags',['subject/project','concern/knowledge']),domain,root,type=n.get('type','knowledge'),allow_similar=True)
        keys[n['key']]=f"{domain}:{made['id']}"
    return str(project),domain,keys

def _resolver(domain,root,keys):
    def resolve(ref):
        if ref.startswith('@'):
            if ref[1:] not in keys:raise ValueError(f'Fixture names unknown synthetic note {ref}')
            return keys[ref[1:]]
        try:d,i=resolve_ref(ref,domain,root)
        except ValueError as error:raise ValueError(f'Fixture ref {ref!r} does not resolve: {error}') from None
        return f'{d}:{i}'
    return resolve

def _ratio(num,den):return round(num/den,3) if den else None

def evaluate(fixture,root,cwd=None,domain=None,keys=None,harness='claude'):
    """Metrics for one fixture against the store at root."""
    root=Path(root).resolve();cwd=cwd or fixture.get('cwd');domain=domain or fixture.get('domain')
    if not cwd or not domain:raise ValueError('Fixture needs "cwd" and "domain"')
    resolve=_resolver(domain,root,keys or {})
    cases=[]
    for case in fixture.get('retrieval',[]):
        expect=[resolve(r) for r in case.get('expect',[])];forbid=[resolve(r) for r in case.get('forbid',[])]
        started=time.perf_counter();text,ids,overview=runtime.context(cwd,case['prompt'],root,domain)
        cases.append({'prompt':case['prompt'],'expect':expect,'forbid':forbid,'injected':ids,'overview':overview,'ms':(time.perf_counter()-started)*1000,
                      'chars':len(text or ''),'hit':bool(set(expect)&set(ids)) if expect else None,
                      'forbidden':sorted(set(forbid)&set(ids)),'false':bool(ids) if not expect else None})
    positive=[c for c in cases if c['expect']];negative=[c for c in cases if not c['expect']]
    from unittest.mock import patch
    def session(installed):
        # Steady state: the instruction file carries the current block (setup re-run). Fallback: it does not,
        # so SessionStart injects the full protocol and the notes get only the minimum share.
        with patch.object(runtime,'installed',return_value=installed):
            prefix=runtime.protocol(root,domain,harness,'SESSION')+'\nLiving Memory session=SESSION\n'
        text=session_start.build_context(cwd,root,max(session_start.MIN_BUDGET,session_start.BUDGET-len(prefix)))[0] or ''
        return prefix,text
    prefix,text=session(True);fallback=sum(map(len,session(False)))
    expected=list(dict.fromkeys(r for c in positive for r in c['expect']))
    listed=[r for r in expected if short_ref(*r.split(':',1)) in text]
    signals=[]
    for case in fixture.get('signals',[]):
        signals.append({'prompt':case['prompt'],'kind':case.get('kind'),'got':runtime.durable_signal(case['prompt'])})
    per_kind={}
    for kind in SIGNAL_KINDS:
        tp=sum(1 for s in signals if s['got']==kind and s['kind']==kind)
        predicted=sum(1 for s in signals if s['got']==kind);actual=sum(1 for s in signals if s['kind']==kind)
        if predicted or actual:per_kind[kind]={'precision':_ratio(tp,predicted),'recall':_ratio(tp,actual),'cases':actual}
    neutral=[s for s in signals if not s['kind']]
    return {
        'retrieval':{
            'cases':len(cases),'positive':len(positive),'negative':len(negative),
            'hit@3':_ratio(sum(c['hit'] for c in positive),len(positive)),
            'false_injection':_ratio(sum(c['false'] for c in negative),len(negative)),
            'forbid_violations':[{'prompt':c['prompt'],'refs':c['forbidden']} for c in cases if c['forbidden']],
            'mean_injected_chars':round(sum(c['chars'] for c in cases)/len(cases)) if cases else None,
            'mean_ms':round(sum(c['ms'] for c in cases)/len(cases),1) if cases else None,
            'overview_rate':_ratio(sum(c['overview'] for c in cases),len(cases)),
            'misses':[{'prompt':c['prompt'],'expect':c['expect'],'injected':c['injected']} for c in positive if not c['hit']],
            'false_injections':[{'prompt':c['prompt'],'injected':c['injected']} for c in negative if c['false']],
        },
        'session':{'chars':len(prefix)+len(text),'fallback_chars':fallback,'budget':session_start.BUDGET,
                   'index_coverage':_ratio(len(listed),len(expected)),
                   'not_listed':[r for r in expected if r not in listed]},
        'signals':{'cases':len(signals),'per_kind':per_kind,
                   'false_arm':_ratio(sum(1 for s in neutral if s['got']),len(neutral)),
                   'wrong':[s for s in signals if s['got']!=s['kind']]},
    }

def run(fixture_path,root=None,semantic_mode=None):
    """semantic_mode 'on'/'off' measures with or without the installed semantic model, then puts the indexes back."""
    fixture=json.loads(Path(fixture_path).expanduser().read_text(encoding='utf-8-sig'))
    if fixture.get('notes'):
        if semantic_mode:raise ValueError('--semantic needs a real store: synthetic fixtures run in a temporary store with no model')
        with tempfile.TemporaryDirectory(prefix='living-memory-eval-') as directory:
            store=Path(directory)/'store'
            cwd,domain,keys=_build(fixture,store)
            return evaluate(fixture,store,cwd,domain,keys)
    from core.storage import resolve_root
    from core import semantic
    store=resolve_root(root)[0]
    if not semantic_mode:return evaluate(fixture,store)
    # Hooks never rebuild, so the derived indexes must match the setting being measured, and afterwards the
    # setting the hooks use, or they would fall back to lexical search until the next write.
    default=semantic.USE_SEMANTIC;semantic.USE_SEMANTIC=semantic_mode=='on'
    try:
        semantic.rebuild_all(store)
        return evaluate(fixture,store)
    finally:
        semantic.USE_SEMANTIC=default;semantic.rebuild_all(store)

def _table(report):
    r,s,g=report['retrieval'],report['session'],report['signals']
    lines=[f"Retrieval: {r['cases']} cases ({r['positive']} positive, {r['negative']} negative)",
           f"  hit@3            {r['hit@3']}   (target >= 0.8)",
           f"  false injection  {r['false_injection']}   (target <= 0.1)",
           f"  forbid hits      {len(r['forbid_violations'])}",
           f"  mean chars       {r['mean_injected_chars']}",
           f"  mean ms          {r['mean_ms']}   (prompt-time retrieval, in process)",
           f"  overview rate    {r['overview_rate']}",
           f"Session start: {s['chars']} chars (target <= {s['budget']}); index coverage {s['index_coverage']}; {s['fallback_chars']} chars if the instruction block is stale",
           f"Signals: {g['cases']} cases; false-arm {g['false_arm']}"]
    lines+=[f"  {k:<11} precision {v['precision']} recall {v['recall']} ({v['cases']} cases)" for k,v in g['per_kind'].items()]
    for title,rows in (('Misses',r['misses']),('False injections',r['false_injections']),('Forbidden injected',r['forbid_violations']),('Signal errors',g['wrong'])):
        if rows:lines+=[title+':']+[f'  - {json.dumps(row,ensure_ascii=False)}' for row in rows]
    return '\n'.join(lines)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--fixture',required=True);parser.add_argument('--root');parser.add_argument('--json',action='store_true')
    parser.add_argument('--strength',choices=['on','off'],help='Override core.usage.USE_STRENGTH for this run, to compare orderings')
    parser.add_argument('--semantic',choices=['on','off'],help='Override core.semantic.USE_SEMANTIC (on needs `semantic install` in the store); indexes are rebuilt to match and restored afterwards')
    args=parser.parse_args(argv)
    if args.strength:
        from core import usage
        usage.USE_STRENGTH=args.strength=='on'
    report=run(args.fixture,args.root,args.semantic)
    print(json.dumps(report,indent=2,ensure_ascii=False) if args.json else _table(report))

if __name__=='__main__':main()
