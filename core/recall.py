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
PRIVATE=re.compile(r'<private>.*?(?:</private>|\Z)',re.S|re.I)   # an unclosed tag runs to the end
INPUT_KEYS=('command','cmd','file_path','path','pattern','query','url','description','prompt','code')
USER_WEIGHT=1.25  # the user's own words say what mattered
AUTO_HITS=2
AUTO_COVERAGE=0.6

def _clean(text):
    return ' '.join(PRIVATE.sub('[private]',NOISE.sub(' ',str(text)))[:TEXT_MAX*2].split())[:TEXT_MAX]

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

TURN_TAIL=4_000_000   # bytes read from the end of a transcript: a turn is never longer, and a long session's file can be huge
EXIT_CODE=re.compile(r'"exit_code"\s*:\s*(-?\d+)|\bExit code:\s*(-?\d+)|\bexited with code\s+(-?\d+)',re.I)

def _command_text(value):
    """The command of a tool input as one string: a string, or an argv list (`bash -lc CMD` yields CMD)."""
    if isinstance(value,list):
        parts=[str(p) for p in value]
        return parts[-1] if len(parts)>=3 and parts[1] in {'-c','-lc','-Command','/c'} else ' '.join(parts)
    return value if isinstance(value,str) else ''

def _tool_args(value):
    if isinstance(value,str):
        try:value=json.loads(value)
        except ValueError:return {'input':value}
    return value if isinstance(value,dict) else {}

def _exit_ok(text):
    """True or False from the exit code a Codex shell result reports; None when it reports none."""
    codes=[int(next(g for g in m.groups() if g is not None)) for m in EXIT_CODE.finditer(text)]
    return codes[0]==0 if codes else None

def _turn_prompt(entry):
    """Whether a transcript line is a real user prompt (not a tool result, harness-injected text or a compaction summary)."""
    if 'payload' in entry and entry.get('type')=='response_item':
        item=entry.get('payload') or {}
        return item.get('type')=='message' and item.get('role')=='user' and bool(_clean(_blocks_text(item.get('content'))))
    if entry.get('type')!='user' or entry.get('isSidechain') or entry.get('isMeta') or entry.get('isCompactSummary'):return False
    content=(entry.get('message') or {}).get('content') if isinstance(entry.get('message'),dict) else None
    if isinstance(content,str):return bool(_clean(content))
    return any(isinstance(b,dict) and b.get('type')=='text' and _clean(b.get('text','')) for b in content or [] if isinstance(content,list))

def turn_tools(path):
    """[{name, input, ok, text, id}] for the tool calls of the current turn (after the last real user prompt), each paired with its
    result by id: Claude `tool_use_id`, Codex `call_id`. ok is False for an error result (Claude `is_error`, a nonzero Codex exit
    code), True otherwise, and None when a Codex result reports no exit code or a call has no result yet. text is the result's
    text; id is the call id ('' when the harness gives none). Only the last TURN_TAIL bytes are read. Harnesses without a reader (Copilot, Factory, Antigravity) give []. Never raises."""
    try:
        with Path(path).open('rb') as handle:
            handle.seek(0,2);size=handle.tell();handle.seek(max(0,size-TURN_TAIL));data=handle.read()
        lines=data.decode('utf-8',errors='replace').splitlines()
        if size>TURN_TAIL:lines=lines[1:]   # the first line is cut
        tools=[];by_id={}
        for line in lines:
            try:entry=json.loads(line)
            except ValueError:continue
            if not isinstance(entry,dict):continue
            if _turn_prompt(entry):tools=[];by_id={};continue
            if entry.get('isSidechain'):continue
            payload=entry.get('payload') if entry.get('type')=='response_item' else None
            if isinstance(payload,dict):
                kind=payload.get('type');call=payload.get('call_id')
                if kind in {'function_call','custom_tool_call'}:
                    args=_tool_args(payload.get('arguments') if kind=='function_call' else payload.get('input'))
                    tool={'name':str(payload.get('name') or ''),'input':args,'ok':None,'text':'','id':str(call or '')}
                    tools.append(tool)
                    if call:by_id[call]=tool
                elif kind in {'function_call_output','custom_tool_call_output'} and call in by_id:
                    text=_blocks_text(payload.get('output')) or str(payload.get('output') or '')
                    by_id[call].update(ok=_exit_ok(text),text=text[:TEXT_MAX])
                continue
            message=entry.get('message') if isinstance(entry.get('message'),dict) else {}
            content=message.get('content')
            for block in content if isinstance(content,list) else []:
                if not isinstance(block,dict):continue
                if entry.get('type')=='assistant' and block.get('type')=='tool_use':
                    tool={'name':str(block.get('name') or ''),'input':block.get('input') if isinstance(block.get('input'),dict) else {},'ok':None,'text':'','id':str(block.get('id') or '')}
                    tools.append(tool);by_id[block.get('id')]=tool
                elif entry.get('type')=='user' and block.get('type')=='tool_result' and block.get('tool_use_id') in by_id:
                    by_id[block['tool_use_id']].update(ok=not block.get('is_error'),text=_blocks_text(block.get('content'))[:TEXT_MAX])
        return tools
    except Exception as exc:
        from .logs import swallowed
        swallowed('recall.turn_tools',exc);return []

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
