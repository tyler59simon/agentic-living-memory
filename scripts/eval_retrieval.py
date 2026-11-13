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
        extra={'supersedes':[keys[n['supersedes'][1:]]]} if n.get('supersedes') else {}
        made=capture(n['title'],n['body'],n.get('tags',['subject/project','concern/knowledge']),domain,root,type=n.get('type','knowledge'),allow_similar=True,**extra)
        keys[n['key']]=f"{domain}:{made['id']}"
    # Seeded filler notes on unrelated everyday topics, so rank metrics are not decided by a dozen notes.
    import random
    rng=random.Random(11);topics=('garden','recipe','travel','bicycle','music','library','aquarium','hiking','pottery','chess','sailing','baking')
    words=('morning','blue','quiet','river','paper','window','autumn','basket','lantern','meadow','pebble','violet','harbor','candle','orchard','thread')
    for i in range(int(fixture.get('distractors',0))):
        topic=topics[i%len(topics)];text=' '.join(rng.choice(words) for _ in range(18))
        capture(f'{topic.title()} note {i}',f'Notes about {topic}: {text}.\n\n## Details\n'+' '.join(rng.choice(words) for _ in range(30))+'.',
                ['subject/project','concern/knowledge'],domain,root,allow_similar=True)
    if fixture.get('source_files'):
        # The project holds a git repo of docs and code (a .git folder is all discovery needs): the knowledge base indexes it.
        from core import sources
        repo=project/'kb';(repo/'.git').mkdir(parents=True)
        for rel,text in fixture['source_files'].items():
            path=repo/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
        sources.sync(root,domain)
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

# The retrieval variants compared on one fixture (VALIDATION.md). Each sets the switches below before the store is built:
#   A  lexical: FTS5 and the gates, no model, the line-per-note delivery
#   B  Python note-level: the model's title+summary vectors in search.sqlite3 (the behaviour before units)
#   C  native note-level: the same vectors through sqlite-vec (the unit index with overview units only)
#   P  Python units: every section embedded, scored in Python (separates indexing from acceleration)
#   D  native units: every section embedded, sqlite-vec KNN
#   E  D plus the injection controller (core/injection.py): evidence, follow-ups, acks, per-section dedupe, one budget
CONFIGS={
    'A':{'semantic':False,'source':'units','units':True,'native':None,'delivery':False},
    'B':{'semantic':True,'source':'notes','units':True,'native':None,'delivery':False},
    'C':{'semantic':True,'source':'units','units':False,'native':None,'delivery':False},
    'P':{'semantic':True,'source':'units','units':True,'native':False,'delivery':False},
    'D':{'semantic':True,'source':'units','units':True,'native':None,'delivery':False},
    'E':{'semantic':True,'source':'units','units':True,'native':None,'delivery':True},
}

def configure(name):
    """Set the switches for one variant; returns a function that restores them."""
    from core import search,semantic,vectors
    saved=(semantic.USE_SEMANTIC,search.SEMANTIC_SOURCE,vectors.USE_UNITS,vectors.NATIVE,runtime.DELIVERY)
    c=CONFIGS[name]
    semantic.USE_SEMANTIC,search.SEMANTIC_SOURCE,vectors.USE_UNITS,vectors.NATIVE,runtime.DELIVERY=c['semantic'],c['source'],c['units'],c['native'],c['delivery']
    def restore():semantic.USE_SEMANTIC,search.SEMANTIC_SOURCE,vectors.USE_UNITS,vectors.NATIVE,runtime.DELIVERY=saved
    return restore

def _pct(values,q):
    values=sorted(values)
    return round(values[min(len(values)-1,int(round(q*(len(values)-1))))],1) if values else None

def _read_cost(root,ref,answer,sections):
    """Characters an agent reads to find the answer after the hook gave it only a pointer: the matching section with
    `read REF --section` when the controller is on (sections True), else the whole body with `read REF`."""
    from core.notes import find_note
    domain,note_id=ref.split(':',1);_,note=find_note(note_id,domain,root);body=note['body']
    if sections and answer:
        from core.units import extract
        for unit in extract(note):
            if unit['role']!='overview' and answer.casefold() in unit['excerpt'].casefold():return len(unit['excerpt'])
    return len(body)

def evaluate_units(fixture,root,cwd,domain,resolve):
    """Section-level cases: rank metrics over the ranked notes (search_domains, no gate), injection, answerability and
    token cost (injected characters plus a follow-up read when the answer was not injected; tokens = chars/4, an
    estimate), polarity errors (a rejected option shown without a rejection marker on its line), and latency."""
    from core.search import search_domains
    rows=[]
    for case in fixture.get('units',[]):
        expect=[resolve(r) for r in case.get('expect',[])];forbid=[resolve(r) for r in case.get('forbid',[])]
        ranked=[f"{h['domain']}:{h['id']}" for h in search_domains(case['prompt'],[domain],10,root)]
        started=time.perf_counter();text,ids,overview=runtime.context(cwd,case['prompt'],root,domain);ms=(time.perf_counter()-started)*1000
        text=text or '';answer=case.get('answer')
        answered=bool(answer) and answer.casefold() in text.casefold() and bool(set(expect)&set(ids))
        read=0
        if expect and not answered and set(expect)&set(ids):
            read=_read_cost(root,next(r for r in expect if r in ids),answer,runtime.DELIVERY);answered=True   # one follow-up read finds it
        first=next((i for i,r in enumerate(ranked,1) if r in expect),None)
        polarity=case.get('polarity');bad=False
        if polarity:
            bad=any(polarity['phrase'].casefold() in line.casefold() and polarity['marker'].casefold() not in line.casefold() for line in text.splitlines())
        rows.append({'kind':case.get('kind'),'prompt':case['prompt'],'expect':expect,'ranked':ranked[:5],'injected':ids,'overview':overview,
                     'first_rank':first,'hit':bool(set(expect)&set(ids)) if expect else None,'false':bool(ids) if not expect else None,
                     'answered_in_context':bool(answer) and answer.casefold() in text.casefold(),'answered':answered if expect else None,
                     'chars':len(text),'read_chars':read,'polarity_error':bad,'forbidden':sorted(set(forbid)&set(ids)),'ms':ms})
    positive=[r for r in rows if r['expect']];negative=[r for r in rows if not r['expect']]
    def at(k):return _ratio(sum(1 for r in positive if r['first_rank'] and r['first_rank']<=k),len(positive))
    answered=[r for r in positive if r['answered']]
    by_kind={}
    for r in positive:
        k=by_kind.setdefault(r['kind'],{'cases':0,'hit':0,'answered_in_context':0})
        k['cases']+=1;k['hit']+=bool(r['hit']);k['answered_in_context']+=bool(r['answered_in_context'])
    total=sum(r['chars']+r['read_chars'] for r in positive)
    return {'cases':len(rows),'positive':len(positive),'negative':len(negative),
            'recall@1':at(1),'recall@3':at(3),'recall@5':at(5),
            'mrr':round(sum(1/r['first_rank'] for r in positive if r['first_rank'])/len(positive),3) if positive else None,
            'hit':_ratio(sum(bool(r['hit']) for r in positive),len(positive)),
            'false_injection':_ratio(sum(bool(r['false']) for r in negative),len(negative)),
            'answered_in_context':_ratio(sum(r['answered_in_context'] for r in positive),len(positive)),
            'answered':_ratio(len(answered),len(positive)),
            'polarity_errors':sum(r['polarity_error'] for r in rows),'forbid_violations':sum(bool(r['forbidden']) for r in rows),
            'injected_chars':sum(r['chars'] for r in rows),'read_chars':sum(r['read_chars'] for r in positive),
            'tokens_per_answered':round(total/4/len(answered),1) if answered else None,
            'p50_ms':_pct([r['ms'] for r in rows],.5),'p95_ms':_pct([r['ms'] for r in rows],.95),
            'by_kind':by_kind,
            'misses':[{'kind':r['kind'],'prompt':r['prompt'],'ranked':r['ranked'],'injected':r['injected']} for r in positive if not r['hit']],
            'false_injections':[{'prompt':r['prompt'],'injected':r['injected']} for r in negative if r['false']]}

def evaluate_sessions(fixture,root,cwd,domain,resolve):
    """Prompt sequences in one session through the real prompt hook: per step, the expected note injected, the answer
    phrase present, or nothing injected (acknowledgments, repeats). Also the injected characters and how many of them
    repeat a line this session already received (redundant)."""
    from core.search import rebuild_index
    steps=[];total=0;redundant=0
    for number,session in enumerate(fixture.get('sessions',[])):
        sid=f'eval-session-{number}';seen=set()
        for turn,step in enumerate(session['steps']):
            if step.get('edit'):
                from core.notes import find_note
                d,i=resolve(step['edit']).split(':',1);path,_=find_note(i,d,root);path=Path(path)
                old,new=step['replace'];path.write_text(path.read_text(encoding='utf-8').replace(old,new),encoding='utf-8');rebuild_index(d,root);continue
            payload={'cwd':cwd,'session_id':sid,'turn_id':f't{turn}','prompt':step['prompt']}
            text=runtime.process('claude','user-prompt',payload,root).get('context') or ''
            lines=[l for l in text.splitlines() if l.startswith('- [')]
            ids=[r for r in (resolve(x) for x in step.get('expect',[]))]
            from core.notes import short_ref
            got_ids=all(any(short_ref(*r.split(':',1)) in l for l in lines) for r in ids)
            ok=got_ids and (not step.get('answer') or step['answer'].casefold() in text.casefold()) and (not step.get('expect_none') or not lines)
            chars=sum(len(l)+1 for l in lines);total+=chars
            redundant+=sum(len(l)+1 for l in lines if l in seen);seen|=set(lines)
            steps.append({'session':session['name'],'prompt':step['prompt'],'ok':ok,'lines':len(lines),'chars':chars})
    return {'steps':len(steps),'correct':_ratio(sum(s['ok'] for s in steps),len(steps)),'injected_chars':total,'redundant_chars':redundant,
            'wrong':[s for s in steps if not s['ok']]}

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
    # Long task briefs: boilerplate around an assignment. Positives expect the note the assignment is about.
    long_cases=[]
    for case in fixture.get('long_prompts',[]):
        expect=[resolve(r) for r in case.get('expect',[])]
        started=time.perf_counter();text,ids,_=runtime.context(cwd,case['prompt'],root,domain)
        long_cases.append({'prompt':case['prompt'][:80]+'…','expect':expect,'injected':ids,'ms':(time.perf_counter()-started)*1000,'hit':bool(set(expect)&set(ids)) if expect else None,'false':bool(ids) if not expect else None})
    long_positive=[c for c in long_cases if c['expect']];long_negative=[c for c in long_cases if not c['expect']]
    # Quizzes: one question per line. A positive must inject at least `min` of its expected notes (the cap is six); a negative none.
    quiz_cases=[]
    for case in fixture.get('quiz_prompts',[]):
        expect=[resolve(r) for r in case.get('expect',[])]
        started=time.perf_counter();text,ids,_=runtime.context(cwd,case['prompt'],root,domain)
        got=sorted(set(expect)&set(ids))
        quiz_cases.append({'prompt':case['prompt'].splitlines()[0][:60]+'…','expect':expect,'min':case.get('min',len(expect)),'injected':ids,'recalled':len(got),'ms':(time.perf_counter()-started)*1000,
                           'hit':len(got)>=case.get('min',len(expect)) if expect else None,'false':bool(ids) if not expect else None,'chars':len(text or '')})
    quiz_positive=[c for c in quiz_cases if c['expect']];quiz_negative=[c for c in quiz_cases if not c['expect']]
    # An unconfirmed note's injected line says so, with its date; a settled note's does not.
    interim_cases=[]
    for case in fixture.get('interim',[]):
        expect=resolve(case['expect']);text,ids,_=runtime.context(cwd,case['prompt'],root,domain)
        line=next((l for l in (text or '').splitlines() if short_ref(*expect.split(':',1)) in l),'')
        marked='UNCONFIRMED (as of ' in line
        interim_cases.append({'prompt':case['prompt'],'expect':expect,'injected':expect in ids,'marked':marked,'ok':expect in ids and marked==case['marked']})
    # Commands an agent is about to run: positives expect the note that warns about them, negatives expect no hint at all.
    from core.commands import command_notes
    command_cases=[]
    for case in fixture.get('commands',[]):
        expect=[resolve(r) for r in case.get('expect',[])]
        started=time.perf_counter();found=[f"{h['domain']}:{h['id']}" for h in command_notes(case['command'],[domain],root,limit=2)]
        command_cases.append({'command':case['command'],'expect':expect,'found':found,'ms':(time.perf_counter()-started)*1000,'hit':bool(set(expect)&set(found)) if expect else None,'false':bool(found) if not expect else None})
    command_positive=[c for c in command_cases if c['expect']];command_negative=[c for c in command_cases if not c['expect']]
    # Questions only the project's own docs and code answer: ask must cite the file in its top three, the prompt hook must inject an excerpt
    # of it, and nothing the notes or the sources cannot answer (these and the retrieval and long-prompt negatives) may inject one.
    source_cases=[]
    if fixture.get('source_files'):
        from core import sources
        for case in fixture.get('sources',[]):
            started=time.perf_counter();hits=sources.search(root,domain,case['question'],3);ask_ms=(time.perf_counter()-started)*1000
            started=time.perf_counter();_,keys=runtime.prompt_sources(case['question'],root,domain,{},{});hook_ms=(time.perf_counter()-started)*1000
            expect=case.get('expect');found=[h['path'] for h in hits]
            source_cases.append({'question':case['question'],'expect':expect,'found':found,'injected':keys,'ms':ask_ms+hook_ms,
                                 'hit':expect in found if expect else None,'right':any(f':{expect}:' in k for k in keys) if expect else None,'false':bool(keys) if not expect else None})
        for case in fixture.get('retrieval',[]):
            if not case.get('expect'):
                keys=runtime.prompt_sources(case['prompt'],root,domain,{},{})[1];source_cases.append({'question':case['prompt'],'expect':None,'found':[],'injected':keys,'ms':0,'hit':None,'right':None,'false':bool(keys)})
        for case in fixture.get('long_prompts',[]):
            if not case.get('expect'):
                keys=runtime.prompt_sources(case['prompt'],root,domain,{},{})[1];source_cases.append({'question':case['prompt'][:80]+'…','expect':None,'found':[],'injected':keys,'ms':0,'hit':None,'right':None,'false':bool(keys)})
    source_positive=[c for c in source_cases if c['expect']];source_negative=[c for c in source_cases if not c['expect']]
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
    extra={}
    if fixture.get('units'):extra['units']=evaluate_units(fixture,root,cwd,domain,resolve)
    if fixture.get('sessions'):extra['sessions']=evaluate_sessions(fixture,root,cwd,domain,resolve)   # last: it edits notes
    return {**extra,
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
        'long_prompts':{'cases':len(long_cases),'positive':len(long_positive),'negative':len(long_negative),
                        'recall':_ratio(sum(c['hit'] for c in long_positive),len(long_positive)),
                        'false_injection':_ratio(sum(c['false'] for c in long_negative),len(long_negative)),
                        'mean_ms':round(sum(c['ms'] for c in long_cases)/len(long_cases),1) if long_cases else None,
                        'misses':[{'prompt':c['prompt'],'expect':c['expect'],'injected':c['injected']} for c in long_positive if not c['hit']],
                        'false_injections':[{'prompt':c['prompt'],'injected':c['injected']} for c in long_negative if c['false']]},
        'quiz':{'cases':len(quiz_cases),'positive':len(quiz_positive),'negative':len(quiz_negative),
                'recall':_ratio(sum(c['hit'] for c in quiz_positive),len(quiz_positive)),
                'notes_recalled':_ratio(sum(c['recalled'] for c in quiz_positive),sum(len(c['expect']) for c in quiz_positive)),
                'false_injection':_ratio(sum(c['false'] for c in quiz_negative),len(quiz_negative)),
                'mean_ms':round(sum(c['ms'] for c in quiz_cases)/len(quiz_cases),1) if quiz_cases else None,
                'max_chars':max((c['chars'] for c in quiz_cases),default=None),
                'misses':[{'prompt':c['prompt'],'expect':c['expect'],'min':c['min'],'injected':c['injected']} for c in quiz_positive if not c['hit']],
                'false_injections':[{'prompt':c['prompt'],'injected':c['injected']} for c in quiz_negative if c['false']]},
        'interim':{'cases':len(interim_cases),'correct':_ratio(sum(c['ok'] for c in interim_cases),len(interim_cases)),'wrong':[c for c in interim_cases if not c['ok']]},
        'commands':{'cases':len(command_cases),'positive':len(command_positive),'negative':len(command_negative),
                    'recall':_ratio(sum(c['hit'] for c in command_positive),len(command_positive)),
                    'false_hint':_ratio(sum(c['false'] for c in command_negative),len(command_negative)),
                    'misses':[{'command':c['command'],'expect':c['expect'],'found':c['found']} for c in command_positive if not c['hit']],
                    'false_hints':[{'command':c['command'],'found':c['found']} for c in command_negative if c['false']]},
        'sources':{'cases':len(source_cases),'positive':len(source_positive),'negative':len(source_negative),
                   'hit@3':_ratio(sum(c['hit'] for c in source_positive),len(source_positive)),'prompt_recall':_ratio(sum(c['right'] for c in source_positive),len(source_positive)),
                   'false_injection':_ratio(sum(c['false'] for c in source_negative),len(source_negative)),
                   'mean_ms':round(sum(c['ms'] for c in source_positive)/len(source_positive),1) if source_positive else None,
                   'misses':[{'question':c['question'],'expect':c['expect'],'found':c['found']} for c in source_positive if not c['hit']],
                   'prompt_misses':[{'question':c['question'],'expect':c['expect'],'injected':c['injected']} for c in source_positive if not c['right']],
                   'false_injections':[{'question':c['question'],'injected':c['injected']} for c in source_negative if c['false']]},
        'session':{'chars':len(prefix)+len(text),'fallback_chars':fallback,'budget':session_start.BUDGET,
                   'index_coverage':_ratio(len(listed),len(expected)),
                   'not_listed':[r for r in expected if r not in listed]},
        'signals':{'cases':len(signals),'per_kind':per_kind,
                   'false_arm':_ratio(sum(1 for s in neutral if s['got']),len(neutral)),
                   'wrong':[s for s in signals if s['got']!=s['kind']]},
    }

def run(fixture_path,root=None,semantic_mode=None,config=None,model_dir=None):
    """semantic_mode 'on'/'off' measures with or without the installed semantic model, then puts the indexes back.
    A synthetic fixture runs in a temporary store; model_dir installs that model (a folder holding the pinned files,
    as `semantic install --from` takes) into it, and config picks a variant (CONFIGS)."""
    fixture=json.loads(Path(fixture_path).expanduser().read_text(encoding='utf-8-sig'))
    if fixture.get('notes'):
        if semantic_mode:raise ValueError('--semantic needs a real store: synthetic fixtures run in a temporary store with no model (use --model-dir)')
        from core import semantic,vectors
        wants=CONFIGS[config]['semantic'] if config else bool(model_dir)
        if wants and not model_dir:raise ValueError(f'config {config} needs --model-dir')
        restore=configure(config) if config else (lambda:None)
        try:
            with tempfile.TemporaryDirectory(prefix='living-memory-eval-',ignore_cleanup_errors=True) as directory:
                store=Path(directory)/'store'
                cwd,domain,keys=_build(fixture,store);index={}
                if wants:
                    semantic.install(store,Path(model_dir).name,model_dir)
                    started=time.perf_counter();semantic.rebuild_all(store,units=True);index['build_s']=round(time.perf_counter()-started,2)
                index.update(vectors.status(domain,store))
                try:
                    report=evaluate(fixture,store,cwd,domain,keys)
                    return {**report,'config':config,'index':index}
                finally:semantic._unload()   # Windows cannot delete a mapped model file
        finally:restore()
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

def _units_table(report):
    lines=[]
    if report.get('config'):lines.append(f"Config {report['config']}; index {json.dumps(report.get('index'))}")
    u=report.get('units')
    if u:
        lines+=[f"Units: {u['cases']} cases ({u['positive']} positive, {u['negative']} negative)",
                f"  recall@1/3/5     {u['recall@1']} / {u['recall@3']} / {u['recall@5']}   MRR {u['mrr']}",
                f"  injected hit     {u['hit']}   false injection {u['false_injection']}",
                f"  answered         {u['answered_in_context']} in the hook's text, {u['answered']} with one follow-up read",
                f"  polarity errors  {u['polarity_errors']}   forbidden injected {u['forbid_violations']}",
                f"  chars            {u['injected_chars']} injected + {u['read_chars']} read; ~{u['tokens_per_answered']} tokens per answered case (chars/4)",
                f"  latency          p50 {u['p50_ms']} ms, p95 {u['p95_ms']} ms (prompt-time retrieval, in process)"]
        lines+=[f"  {k:<11} {v['hit']}/{v['cases']} injected, {v['answered_in_context']} answered in the hook's text" for k,v in u['by_kind'].items()]
        for title,rows in (('Unit misses',u['misses']),('Unit false injections',u['false_injections'])):
            if rows:lines+=[title+':']+[f'  - {json.dumps(row,ensure_ascii=False)}' for row in rows]
    s=report.get('sessions')
    if s:
        lines+=[f"Sessions: {s['steps']} steps; correct {s['correct']}; {s['injected_chars']} chars injected, {s['redundant_chars']} of them repeated"]
        if s['wrong']:lines+=['Session steps wrong:']+[f'  - {json.dumps(row,ensure_ascii=False)}' for row in s['wrong']]
    return lines

def _table(report):
    r,s,g,lp,cm,src=report['retrieval'],report['session'],report['signals'],report['long_prompts'],report['commands'],report['sources']
    qz,im=report['quiz'],report['interim']
    lines=_units_table(report)+[f"Retrieval: {r['cases']} cases ({r['positive']} positive, {r['negative']} negative)",
           f"  hit@3            {r['hit@3']}   (target >= 0.8)",
           f"  false injection  {r['false_injection']}   (target <= 0.1)",
           f"  forbid hits      {len(r['forbid_violations'])}",
           f"  mean chars       {r['mean_injected_chars']}",
           f"  mean ms          {r['mean_ms']}   (prompt-time retrieval, in process)",
           f"  overview rate    {r['overview_rate']}",
           f"Long prompts: {lp['cases']} cases ({lp['positive']} positive, {lp['negative']} negative)",
           f"  recall           {lp['recall']}   (target >= 0.8)",
           f"  false injection  {lp['false_injection']}   (target 0)",
           f"  mean ms          {lp['mean_ms']}",
           *([f"Quizzes: {qz['cases']} cases ({qz['positive']} positive, {qz['negative']} negative)",
              f"  recall           {qz['recall']}   (target 1.0: at least `min` expected notes each)",
              f"  notes recalled   {qz['notes_recalled']}",
              f"  false injection  {qz['false_injection']}   (target 0)",
              f"  mean ms          {qz['mean_ms']}   (max {qz['max_chars']} chars injected)"] if qz['cases'] else []),
           *([f"Interim notes: {im['cases']} cases; UNCONFIRMED marker correct {im['correct']}   (target 1.0)"] if im['cases'] else []),
           f"Command hints: {cm['cases']} cases ({cm['positive']} positive, {cm['negative']} negative)",
           f"  recall           {cm['recall']}   (target >= 0.8)",
           f"  false hint       {cm['false_hint']}   (target 0)",
           *([f"Sources: {src['cases']} cases ({src['positive']} positive, {src['negative']} negative; the negatives include the retrieval and long-prompt ones)",
              f"  ask hit@3        {src['hit@3']}   (target >= 0.8)",
              f"  prompt recall    {src['prompt_recall']}   (target >= 0.8)",
              f"  false injection  {src['false_injection']}   (target 0)",
              f"  mean ms          {src['mean_ms']}   (ask plus the hook's lookup)"] if src['cases'] else []),
           f"Session start: {s['chars']} chars (target <= {s['budget']}); index coverage {s['index_coverage']}; {s['fallback_chars']} chars if the instruction block is stale",
           f"Signals: {g['cases']} cases; false-arm {g['false_arm']}"]
    lines+=[f"  {k:<11} precision {v['precision']} recall {v['recall']} ({v['cases']} cases)" for k,v in g['per_kind'].items()]
    for title,rows in (('Misses',r['misses']),('Long-prompt misses',lp['misses']),('Long-prompt false injections',lp['false_injections']),('Quiz misses',qz['misses']),('Quiz false injections',qz['false_injections']),('Interim errors',im['wrong']),('Command-hint misses',cm['misses']),('Command false hints',cm['false_hints']),('Source misses',src['misses']),('Source prompt misses',src['prompt_misses']),('Source false injections',src['false_injections']),('False injections',r['false_injections']),('Forbidden injected',r['forbid_violations']),('Signal errors',g['wrong'])):
        if rows:lines+=[title+':']+[f'  - {json.dumps(row,ensure_ascii=False)}' for row in rows]
    return '\n'.join(lines)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--fixture',required=True);parser.add_argument('--root');parser.add_argument('--json',action='store_true')
    parser.add_argument('--strength',choices=['on','off'],help='Override core.usage.USE_STRENGTH for this run, to compare orderings')
    parser.add_argument('--semantic',choices=['on','off'],help='Override core.semantic.USE_SEMANTIC (on needs `semantic install` in the store); indexes are rebuilt to match and restored afterwards')
    parser.add_argument('--config',choices=sorted(CONFIGS),help='Retrieval variant for a synthetic fixture: A lexical, B Python note-level, C native note-level, P Python units, D native units, E D plus the injection controller')
    parser.add_argument('--model-dir',help='Folder with the pinned model files, installed into the temporary store of a synthetic fixture')
    args=parser.parse_args(argv)
    if args.strength:
        from core import usage
        usage.USE_STRENGTH=args.strength=='on'
    report=run(args.fixture,args.root,args.semantic,args.config,args.model_dir)
    print(json.dumps(report,indent=2,ensure_ascii=False) if args.json else _table(report))

if __name__=='__main__':main()
