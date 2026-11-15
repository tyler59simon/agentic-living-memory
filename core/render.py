"""Plain text for a person at a terminal. Agents, apps and pipes get the JSON result instead (cli._emit)."""
from __future__ import annotations
import json,textwrap
WIDTH=88
LISTS=('results','notes','items','sources')
TRAILER=('hint','warnings','quality_warnings')

def _s(v):return v if isinstance(v,str) else json.dumps(v,ensure_ascii=False)
def _wrap(text,indent='   '):return textwrap.wrap(' '.join(str(text).split()),WIDTH,initial_indent=indent,subsequent_indent=indent)
def _ref(row):
    ref=row.get('short_ref') or row.get('ref') or (f"{row.get('domain') or ''}:{row['id']}" if row.get('id') else '')
    domain,_,note=ref.rpartition(':')
    return f'{domain}:{note[:8]}' if domain else note[:8]

def _row(row,n):
    head=f'{n}. ' if n else '- '
    name=row.get('title') or row.get('path') or row.get('file') or row.get('source') or _ref(row)
    meta=', '.join(str(row[k])[:10] if k in ('updated','date') else str(row[k]) for k in ('type','updated','date','status') if row.get(k))
    lines=[head+str(name or json.dumps(row,ensure_ascii=False))+(f'  [{meta}]' if meta else '')+(f'  {_ref(row)}' if row.get('title') and _ref(row) else '')]
    if row.get('item'):lines+=_wrap(f"#{row['index']} {row['item']}" if 'index' in row else row['item'])   # open: the action item itself
    for key in ('summary','snippet','excerpt','text','reason','action'):
        if row.get(key):lines+=_wrap(row[key])
    if isinstance(row.get('files'),list) and row['files']:lines+=_wrap('files: '+', '.join(map(str,row['files'])))
    if isinstance(row.get('actions'),dict):lines.append(f"   actions: {row['actions'].get('open',0)} open, {row['actions'].get('done',0)} done")
    return lines

def _kv(value,depth=0):
    pad='  '*depth;out=[]
    if isinstance(value,dict):
        for k,v in value.items():
            if k=='ok' and v is True and not depth:continue
            if isinstance(v,(dict,list)) and v:out.append(f'{pad}{k}:');out+=_kv(v,depth+1)
            else:out.append(f'{pad}{k}: {_s(v)}')
    elif isinstance(value,list):
        for v in value:
            if isinstance(v,(dict,list)):out.append(pad+'-');out+=_kv(v,depth+1)
            else:out.append(f'{pad}- {_s(v)}')
    else:out.append(pad+_s(value))
    return out

def _plan(r):
    relocation='destination' in r   # setup's store relocation plan: its own fields, applied by setup, not by apply
    rest={k:v for k,v in r.items() if k not in ('ok','plan_id','digest','kind')+TRAILER} if relocation else {k:r[k] for k in ('summary','effects') if k in r}
    lines=[f"Plan {r['plan_id']}"+(f" ({r['kind']})" if r.get('kind') else '')]+_kv(rest,1)
    return lines+['','Apply it with:',f"  alm {'setup --apply-plan' if relocation else 'apply'} {r['plan_id']} --expected-plan-sha256 {r['digest']}"]

NOTE_HEAD=('ok','title','type','status','updated','tags','id','domain','body','truncated','stale','currency')
def _read(r):
    lines=[str(r['title']),'  '+'  '.join(str(r[k]) for k in ('type','status') if r.get(k))+(f"  updated {r['updated']}" if r.get('updated') else '')+f"  {_ref(r)}"]
    if r.get('tags'):lines.append('  tags: '+', '.join(r['tags']))
    if r.get('stale'):lines.append('  '+str(r['stale']))
    elif isinstance(r.get('currency'),dict) and r['currency'].get('state') not in (None,'current'):lines.append(f"  source: {r['currency']['state']}")
    if 'body' not in r:return lines+_kv({k:v for k,v in r.items() if k not in NOTE_HEAD+TRAILER},1)   # read --metadata-only
    return lines+['',str(r['body']).rstrip()]+(['','(truncated)'] if r.get('truncated') else [])

def _receipt(r):
    lines=[f"{r['status']}: {_ref(r)}"]
    for key in ('similar','related','superseded'):
        for item in r.get(key) or []:lines.append(f'  {key}: '+(f"{item.get('ref','')}  {item.get('title','')}".strip() if isinstance(item,dict) else str(item)))
    return lines

def _rows(r):
    lines=[]
    for key in LISTS:
        rows=[x for x in r[key] if isinstance(x,dict)] if isinstance(r.get(key),list) else []   # open's `notes` is a count
        if rows:
            lines+=([''] if lines else [])+(['Sources:'] if key=='sources' else [])
            for n,row in enumerate(rows,1):lines+=_row(row,n if key=='results' or 'rank' in row else 0)
    return lines or ['No matches.']

def render(result):
    """One result as text: by shape (plan, note, receipt, rows), else indented key: value."""
    if isinstance(result,list):
        if result and all(isinstance(x,dict) and 'exit_code' in x for x in result):
            lines=[]
            for x in result:lines+=[f"exit code {x['exit_code']}"]+(render(x['result']).splitlines() if x.get('result') is not None else [])+([x['error']] if x.get('error') else [])
            return '\n'.join(lines)
        return '\n'.join(' '.join(f'{k}={_s(v)}' for k,v in x.items() if not isinstance(v,(dict,list))) if isinstance(x,dict) else _s(x) for x in result) or 'Nothing.'
    if not isinstance(result,dict):return _s(result)
    if isinstance(result.get('markdown'),str):lines=[result['markdown'].rstrip()]   # recap, history --markdown: already written for a person
    elif result.get('plan_id') and result.get('digest') and result.get('status')!='approval_required':lines=_plan(result)
    elif 'title' in result and ('body' in result or 'type' in result):lines=_read(result)
    elif result.get('memory') in {'on','off'} and 'session' in result:   # alm off / on / status
        lines=[f"Living Memory is {result['memory']} for this session ({result['session']})"+(': started with LIVING_MEMORY_OFF=1.' if result.get('reason')=='env' else '.')]
        if result.get('known') is False:lines.append('No hook has seen this session yet.')
    elif result.get('status') and result.get('id'):lines=_receipt(result)
    elif any(isinstance(result.get(k),list) for k in LISTS):lines=_rows(result)
    else:lines=_kv({k:v for k,v in result.items() if k not in TRAILER})
    for key in TRAILER:
        for item in result.get(key) if isinstance(result.get(key),list) else [result[key]] if result.get(key) else []:lines+=_wrap(f'{key}: {item}',indent='')
    return '\n'.join(lines)
