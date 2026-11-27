"""Plain text for a person at a terminal. Agents, apps and pipes get the JSON result instead (cli._emit)."""
from __future__ import annotations
import json,os,textwrap
WIDTH=88
LISTS=('results','notes','items','sources','pages')
TRAILER=('hint','warnings','quality_warnings','rule_warning','pin_warning')

def _s(v):return v if isinstance(v,str) else json.dumps(v,ensure_ascii=False)
def _wrap(text,indent='   '):return textwrap.wrap(' '.join(str(text).split()),WIDTH,initial_indent=indent,subsequent_indent=indent)
def _ts(value):
    text=str(value or '')
    return text[:16].replace('T',' ') if 'T' in text else text[:10]
def _rev(row):
    rev=str(row.get('revision') or '')
    return f'rev {rev[:12]}' if rev else ''
def _ref(row):
    ref=row.get('short_ref') or row.get('ref') or (f"{row.get('domain') or ''}:{row['id']}" if row.get('id') else '')
    domain,_,note=ref.rpartition(':')
    return f'{domain}:{note[:8]}' if domain else note[:8]

def _row(row,n):
    head=f'{n}. ' if n else '- '
    name=row.get('title') or row.get('path') or row.get('file') or row.get('source') or _ref(row)
    if row.get('label')=='notebook page':name=f"{name}  ({row.get('domain')}/{row.get('notebook')}/{row.get('path')}:{row.get('line_start')})"
    meta=', '.join(str(row[k])[:10] if k in ('updated','date') else str(row[k]) for k in ('type','updated','date','status') if row.get(k))
    rev=_rev(row)
    tail='  '.join(p for p in (f'[{meta}]' if meta else '',_ref(row) if row.get('title') and _ref(row) else '',rev) if p)
    lines=[head+str(name or json.dumps(row,ensure_ascii=False))+(f'  {tail}' if tail else '')]
    if row.get('item'):lines+=_wrap(f"#{row['index']} {row['item']}" if 'index' in row else row['item'])   # open: the action item itself
    for key in ('summary','snippet','excerpt','text','reason','action'):
        if row.get(key):lines+=_wrap(row[key])
    if isinstance(row.get('files'),list) and row['files']:lines+=_wrap('files: '+', '.join(map(str,row['files'])))
    if isinstance(row.get('actions'),dict):lines.append(f"   actions: {row['actions'].get('open',0)} open, {row['actions'].get('done',0)} done")
    return lines

def _kv(value,depth=0):
    pad='  '*depth
    out=[]
    if isinstance(value,dict):
        for k,v in value.items():
            if k=='ok' and v is True and not depth:continue
            if isinstance(v,(dict,list)) and v:
                out.append(f'{pad}{k}:')
                out+=_kv(v,depth+1)
            else:out.append(f'{pad}{k}: {_s(v)}')
    elif isinstance(value,list):
        for v in value:
            if isinstance(v,(dict,list)):
                out.append(pad+'-')
                out+=_kv(v,depth+1)
            else:out.append(f'{pad}- {_s(v)}')
    else:out.append(pad+_s(value))
    return out

def _direct(r):
    rest={k:v for k,v in r.items() if k not in ('ok','operation_id','state','kind','undo','idempotent')+TRAILER}
    return [f"Done ({r.get('kind') or 'change'}), operation {r['operation_id']}"]+_kv(rest,1)

NOTE_HEAD=('ok','title','type','status','updated','tags','id','domain','body','truncated','stale','currency','revision')
def _read(r):
    parts=[str(r[k]) for k in ('type','status') if r.get(k)]
    if r.get('updated'):parts.append(f"updated {_ts(r['updated'])}")
    if _ref(r):parts.append(_ref(r))
    if _rev(r):parts.append(_rev(r))
    lines=[str(r['title']),'  '+'  '.join(parts)]
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
            lines+=([''] if lines else [])+({'sources':['Sources:'],'pages':['Notebook pages:']}.get(key,[]))
            for n,row in enumerate(rows,1):lines+=_row(row,n if key=='results' or 'rank' in row else 0)
    return lines or ['No matches.']

def _types(r):
    """`types`: one line per type (name, built-in or custom, retired), then its description and what it writes."""
    lines=[]
    for t in r['types']:
        kind='built-in' if t.get('builtin') else 'custom'+(', retired' if t.get('status')=='retired' else '')
        lines.append(f"{t.get('name')} ({kind})")
        lines+=_wrap(t.get('description',''))
        shape=[('sections: '+', '.join(t['sections'])) if t.get('sections') else '',
               'action items' if t.get('actions') else '',f"review every {t['review_days']} days" if t.get('review_days') else '',
               t.get('concern') or '']
        if any(shape):lines+=_wrap('; '.join(x for x in shape if x))
    return lines

def _upgrade(r):
    """`upgrade`: what moved, then the wiring commands to run (paste-ready) or that wiring is current."""
    moved=f"{r['from']} to {r['to']}"+(f" ({r['commits']} commits)" if r.get('commits') else '')
    head={'current':f"Already current: {r['source']} ({r['branch']}) is at the installed commit {r['to']}.",
          'upgraded':f"Upgraded from {r['source']} ({r['branch']}), {moved}: installed {r.get('files')} files."}[r['status']]
    lines=_wrap(head,indent='')  # PowerShell runs a quoted program only after &
    lead='& ' if os.name=='nt' else ''
    if r.get('next_steps'):lines+=['','Rewire these harnesses:']+['  '+lead+step for step in r['next_steps']]
    elif r.get('wiring_current'):lines+=['',f"Harness wiring is current ({', '.join(r['wiring_current'])}); nothing to rerun."]
    if r.get('notice'):lines+=['']+_wrap(r['notice'],indent='')
    if r.get('restart'):lines+=['',r['restart']]
    return lines

def _review(r):
    counts=', '.join(f'{c} {k}' for k,c in (r.get('counts') or {}).items() if c)
    lines=[f"Review: {r.get('due',0)} note(s) due"+(f' ({counts})' if counts else '')]
    if not r.get('notes'):return lines+['Nothing due for review.']
    for row in r['notes']:
        badges=' '.join(f'[{k}]' for k in row.get('kinds') or [])
        head='  '.join(p for p in (badges,_ref(row),_rev(row),row.get('title')) if p)
        lines.append('- '+head)
        if row.get('reason'):lines+=_wrap(row['reason'])
        if row.get('action'):lines.append(f"   next: {row['action']}")
    return lines

def _doctor(r):
    status='OK' if r.get('ok') else 'ISSUES FOUND'
    lines=[f"Living Memory doctor: {status}"]
    if r.get('warnings'):
        lines.append('Warnings:')
        for w in r['warnings']:lines+=_wrap('- '+str(w),indent='  ')
    lines.append(f"Store: {r.get('root')}  (domain: {r.get('domain') or 'unbound'}, index: {r.get('index')})")
    sem=r.get('semantic') or {}
    lines.append(f"Python: {r.get('python')}  FTS5: {'yes' if r.get('fts5') else 'no'}  Semantic: {sem.get('backend','lexical')}"+(f" ({sem['model']})" if sem.get('model') else ''))
    if isinstance(r.get('notes'),dict) and r['notes']:
        lines.append('Notes: '+', '.join(f'{d}={c}' for d,c in sorted(r['notes'].items())))
    wiring=(r.get('hooks') or {}).get('wiring') or {}
    if isinstance(wiring,dict) and wiring:
        wired=[f"{h} ({'ok' if not (v.get('problems') if isinstance(v,dict) else []) else 'needs attention'})" for h,v in sorted(wiring.items()) if isinstance(v,dict)]
        if wired:lines.append('Hooks: '+', '.join(wired))
    return lines

def _domains(r):
    rows=r.get('domains') or []
    if not rows:return ['No domains bound yet.']
    lines=['Domains:']
    for d in rows:
        state='bound' if d.get('bound') else 'folder only'
        lines.append(f"- {d['name']}  ({state}, {d.get('active',0)} active, {d.get('archived',0)} archived)")
        for p in d.get('prefixes') or []:lines.append(f'   {p}')
    return lines

def _named_list(r,key):
    rows=r.get(key) or []
    if not rows:return [f'No {key}.']
    lines=[f'{key.capitalize()}:']
    for item in rows:
        meta=', '.join(str(item[k]) for k in ('status','domain','kind') if item.get(k))
        label=item.get('slug') or item.get('id') or item.get('title') or ''
        title=item.get('name') or item.get('title') or item.get('path') or ''
        head=f"{label}  {title}" if title and title!=label else str(label or title)
        lines.append(f"- {head}"+(f'  [{meta}]' if meta else ''))
        if item.get('repos'):lines.append('   repos: '+', '.join(map(str,item['repos'])))
    return lines

def _logs(r):
    entries=r.get('entries') or []
    lines=[f"Logs (level={r.get('level_setting')}, matched={r.get('matched',len(entries))}):"]
    if not entries:return lines+['No log entries.']
    for e in entries:
        ts=_ts(e.get('ts') or e.get('at') or e.get('t'))
        lines.append(f"- {(ts+' ') if ts else ''}[{e.get('level','')}] {e.get('kind','')}/{e.get('event','')}"+(f" ({e['ms']}ms)" if e.get('ms') is not None else ''))
    return lines

def _maintenance_status(r):
    state='enabled' if r.get('enabled') else 'disabled'
    limits=r.get('limits') or {}
    acc=r.get('accounting') or {}
    lines=[f"Maintenance: {state}"+(f"  (domains: {', '.join(r.get('domains') or [])})" if r.get('domains') else '')]
    if limits.get('model'):lines.append(f"  model: {limits['model']}  daily budget: ${limits.get('daily_budget_usd','?')}  max notes/day: {limits.get('max_notes_per_day','?')}")
    if acc:lines.append(f"  today ({acc.get('day','?')}): reviewed={acc.get('reviewed',0)}  charged=${acc.get('charged_usd','0.00')}")
    if r.get('last_run'):lines.append(f"  last run: {_ts(r['last_run'])}")
    if r.get('skip_reason'):lines.append(f"  status: {r['skip_reason']}")
    return lines

def render(result):
    """One result as text: by shape (note, receipt, rows), else indented key: value."""
    if isinstance(result,list):
        if result and all(isinstance(x,dict) and 'exit_code' in x for x in result):
            lines=[]
            for x in result:lines+=[f"exit code {x['exit_code']}"]+(render(x['result']).splitlines() if x.get('result') is not None else [])+([x['error']] if x.get('error') else [])
            return '\n'.join(lines)
        if result and all(isinstance(x,dict) and 'event' in x and 'time' in x for x in result):
            return '\n'.join(f"- {_ts(x['time'])} {x['event']}  {_ref(x)}  {x.get('title') or ''}".rstrip()+(f" ({x['actor']})" if x.get('actor') else '') for x in result)
        return '\n'.join(' '.join(f'{k}={_s(v)}' for k,v in x.items() if not isinstance(v,(dict,list))) if isinstance(x,dict) else _s(x) for x in result) or 'Nothing.'
    if not isinstance(result,dict):return _s(result)
    if isinstance(result.get('markdown'),str):lines=[result['markdown'].rstrip()]   # recap, history --markdown: already written for a person
    elif result.get('status') in ('current','upgraded') and 'branch' in result and 'source' in result:lines=_upgrade(result)
    elif 'fts5' in result and 'hooks' in result and 'warnings' in result:lines=_doctor(result)
    elif 'due' in result and 'counts' in result and isinstance(result.get('notes'),list):lines=_review(result)
    elif 'enabled' in result and 'domains' in result and 'limits' in result and 'accounting' in result:lines=_maintenance_status(result)
    elif isinstance(result.get('domains'),list) and 'aliases' in result:lines=_domains(result)
    elif isinstance(result.get('entries'),list) and 'level_setting' in result:lines=_logs(result)
    elif isinstance(result.get('views'),list):lines=_named_list(result,'views')
    elif isinstance(result.get('projects'),list):lines=_named_list(result,'projects')
    elif isinstance(result.get('systems'),list):lines=_named_list(result,'systems')
    elif isinstance(result.get('proposals'),list):lines=_named_list(result,'proposals')
    elif result.get('operation_id') and result.get('undo') and isinstance(result.get('summary'),dict):lines=_direct(result)   # a direct write (governance.commit)
    elif 'title' in result and ('body' in result or 'type' in result):lines=_read(result)
    elif result.get('memory') in {'on','off'} and 'session' in result:   # alm off / on / status
        lines=[f"Living Memory is {result['memory']} for this session ({result['session']})"+(': started with LIVING_MEMORY_OFF=1.' if result.get('reason')=='env' else '.')]
        if result.get('known') is False:lines.append('No hook has seen this session yet.')
    elif result.get('status')=='proposed' and result.get('id'):   # propose: the bare ID is what proposals accept takes
        lines=[f"proposed: {result['id']}  {result.get('title','')}".rstrip()]+_wrap(result.get('next',''),indent='  ')
    elif result.get('status') and result.get('id'):lines=_receipt(result)
    elif isinstance(result.get('types'),list) and all(isinstance(t,dict) and 'name' in t for t in result['types']):lines=_types(result)
    elif any(isinstance(result.get(k),list) for k in LISTS):lines=_rows(result)
    else:lines=_kv({k:v for k,v in result.items() if k not in TRAILER})
    if result.get('operation_id') and result.get('undo') and not lines[0].startswith('Done ('):lines+=['',f"Operation {result['operation_id']}. Undo it with: alm {result['undo']}"]
    elif result.get('operation_id') and result.get('undo'):lines+=['',f"Undo it with: alm {result['undo']}"]
    if result.get('pages_stale'):lines+=['','Notebook pages: the index was not fully brought up to date in time; run the command again for complete results.']
    for key in TRAILER:
        if key=='warnings' and 'fts5' in result:continue   # _doctor already rendered warnings at the top
        for item in result.get(key) if isinstance(result.get(key),list) else [result[key]] if result.get(key) else []:lines+=_wrap(f'{key}: {item}',indent='')
    return '\n'.join(lines)
