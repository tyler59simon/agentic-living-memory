"""Recall: search the session's own transcript for detail that compaction summarized away.

The transcript is the harness's file (Claude Code JSONL or a Codex rollout). Living Memory keeps only its
path in the session state and reads it on demand: nothing is copied into the store. Excerpts are trimmed,
<private> spans are masked and credentials redacted. Unknown or changed formats yield no records, never an error."""
from __future__ import annotations
import json,math,re
from pathlib import Path
from .search import query_terms,terms,stem,snippet,COMMON_SHARE,COMMON_MIN_NOTES

EXCERPT=320
TEXT_MAX=4000     # indexed characters per record: a huge tool output is mostly noise
TOOL_INPUT_MAX=600
# Harness-injected wrappers inside user turns; they are context, not what anyone said.
NOISE=re.compile(r'<(system-reminder|command-name|command-message|command-args|local-command-stdout|local-command-stderr|environment_context|user_instructions|turn_aborted|task-notification)\b.*?(?:</\1>|\Z)',re.S|re.I)
PRIVATE=re.compile(r'<private>.*?</private>',re.S|re.I)
INPUT_KEYS=('command','cmd','file_path','path','pattern','query','url','description','prompt','code')
USER_WEIGHT=1.25  # the user's own words say what mattered
AUTO_HITS=2
AUTO_COVERAGE=0.6

def _clean(text):
    return ' '.join(NOISE.sub(' ',str(text))[:TEXT_MAX*2].split())[:TEXT_MAX]

def _safe(text):
    """Text fit to show: <private> spans masked, credentials redacted. Only returned hits pay for this."""
    from .repository import redact
    return ' '.join(redact(PRIVATE.sub('[private]',text))[0].split())

def _blocks_text(value):
    """Text of a content value: a string, or a list of text-like blocks."""
    if isinstance(value,str):return value
    if isinstance(value,list):
        return ' '.join(b.get('text','') if isinstance(b,dict) else str(b) for b in value if isinstance(b,(dict,str)))
    return ''

def _tool_input(name,value):
    if isinstance(value,str):
        try:value=json.loads(value)
        except ValueError:return f'{name} {value[:TOOL_INPUT_MAX]}'
    if isinstance(value,dict):
        shown=[str(value[k]) for k in INPUT_KEYS if isinstance(value.get(k),(str,int,float))]
        text=' '.join(shown) if shown else json.dumps(value,ensure_ascii=False)
    else:text=str(value)
    return f'{name} {text[:TOOL_INPUT_MAX]}'

def _claude(entry):
    """(role, text) pairs of one Claude Code transcript line; 'boundary' marks a compaction."""
    kind=entry.get('type')
    if kind=='system' and entry.get('subtype')=='compact_boundary':return [('boundary','')]
    if entry.get('isSidechain') or entry.get('isMeta') or entry.get('isCompactSummary'):return []
    content=(entry.get('message') or {}).get('content') if isinstance(entry.get('message'),dict) else None
    if kind=='user':
        if isinstance(content,str):return [('user',content)]
        found=[]
        for block in content if isinstance(content,list) else []:
            if not isinstance(block,dict):continue
            if block.get('type')=='text':found.append(('user',block.get('text','')))
            elif block.get('type')=='tool_result':found.append(('output',_blocks_text(block.get('content'))))
        return found
    if kind=='assistant':
        found=[]
        for block in content if isinstance(content,list) else []:
            if not isinstance(block,dict):continue
            if block.get('type')=='text':found.append(('assistant',block.get('text','')))
            elif block.get('type')=='tool_use':found.append(('tool',_tool_input(block.get('name','tool'),block.get('input'))))
        return found
    return []

def _codex(entry):
    """(role, text) pairs of one Codex rollout line."""
    if entry.get('type')=='compacted':return [('boundary','')]
    if entry.get('type')!='response_item':return []
    item=entry.get('payload') or {};kind=item.get('type')
    if kind=='message' and item.get('role') in {'user','assistant'}:return [(item['role'],_blocks_text(item.get('content')))]
    if kind=='custom_tool_call':return [('tool',_tool_input(item.get('name','tool'),item.get('input')))]
    if kind=='function_call':return [('tool',_tool_input(item.get('name','tool'),item.get('arguments')))]
    if kind in {'custom_tool_call_output','function_call_output'}:return [('output',_blocks_text(item.get('output')))]
    return []

def records(path,scope='before'):
    """(records, compacted). scope 'before': only what precedes the latest compaction (all of it when
    there was none); 'all': the whole transcript. A record is {i, t, role, text}."""
    found=[];cut=None
    try:handle=Path(path).open(encoding='utf-8',errors='replace')
    except (OSError,TypeError,ValueError):return [],False
    with handle:
        for line in handle:
            try:entry=json.loads(line)
            except ValueError:continue
            if not isinstance(entry,dict):continue
            parse=_codex if 'payload' in entry and entry.get('type') in {'response_item','compacted'} else _claude
            for role,text in parse(entry):
                if role=='boundary':cut=len(found);continue
                text=_clean(text)
                if text:found.append({'i':len(found),'t':str(entry.get('timestamp') or '')[:16].replace('T',' '),'role':role,'text':text})
    if scope=='before' and cut is not None:found=found[:cut]
    return found,cut is not None

def _probe(term):
    """A substring every token stemming to `term` contains (stem folds 'ies' to 'y')."""
    return term[:-1] if term.endswith('y') and len(term)>3 else term

def search(path,query,limit=5,scope='before',exclude=(),width=EXCERPT):
    """Ranked excerpts {i, t, role, excerpt, coverage, score} by BM25 over the records, plus the record
    count and whether the transcript was compacted. Words found in most records do not count."""
    rows,compacted=records(path,scope)
    wanted=list(dict.fromkeys(stem(t) for t in query_terms(query or '')))
    meta={'records':len(rows),'compacted':compacted}
    if not rows or not wanted:return [],meta
    skip=set(exclude);probes=[(q,_probe(q)) for q in wanted]
    docs={}
    for row in rows:
        if row['i'] in skip:continue
        low=row['text'].casefold()
        if any(p in low for _,p in probes):
            counts={}
            for token in terms(row['text']):
                s=stem(token)
                if s in wanted:counts[s]=counts.get(s,0)+1
            if counts:docs[row['i']]=counts
    total=len(rows);df={q:sum(1 for c in docs.values() if q in c) for q in wanted}
    informative=[q for q in wanted if not (total>=COMMON_MIN_NOTES and df[q]>COMMON_SHARE*total)]
    meta['terms']=len(informative)
    if not informative:return [],meta
    average=sum(len(r['text']) for r in rows)/total or 1;by_index={r['i']:r for r in rows};hits=[]
    for index,counts in docs.items():
        matched=[q for q in informative if q in counts]
        if not matched:continue
        row=by_index[index];norm=1.2*(0.25+0.75*len(row['text'])/average)
        score=sum(math.log(1+(total-df[q]+0.5)/(df[q]+0.5))*counts[q]*2.2/(counts[q]+norm) for q in matched)
        if row['role']=='user':score*=USER_WEIGHT
        hits.append({'i':index,'t':row['t'],'role':row['role'],'matched':matched,
                     'coverage':round(len(matched)/len(informative),3),'score':round(score,4)})
    hits.sort(key=lambda h:(-h['coverage'],-h['score'],-h['i']))
    hits=hits[:max(1,limit)]
    for hit in hits:hit['excerpt']=snippet(_safe(by_index[hit['i']]['text']),hit.pop('matched'),width)
    return hits,meta

def line(hit):return f"- [{hit['role']} {hit['t']}] {hit['excerpt']}"

def auto(path,prompt,exclude=(),trace=None):
    """(context block or None, record indices used): strict, for prompts right after a compaction.
    Needs two or more informative prompt words, most of them in one record. trace, when a dict, receives
    the gate's numbers: informative terms, whether the transcript was compacted, candidate and kept hits."""
    trace=trace if isinstance(trace,dict) else {}
    try:
        hits,meta=search(path,prompt,AUTO_HITS*3,'before',exclude,240)
        trace.update(terms=meta.get('terms',0),compacted=bool(meta.get('compacted')),candidates=len(hits),hits=0)
        if meta.get('terms',0)<2 or not meta.get('compacted'):return None,[]
        hits=[h for h in hits if h['coverage']>=AUTO_COVERAGE and h['coverage']*meta['terms']>=2][:AUTO_HITS]
        trace['hits']=len(hits)
        if not hits:return None,[]
        return '\n'.join(line(h) for h in hits),[h['i'] for h in hits]
    except Exception as exc:  # recall is a nicety; never fail the prompt hook
        from .logs import swallowed
        swallowed('recall.auto',exc);return None,[]
