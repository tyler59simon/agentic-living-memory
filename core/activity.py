"""Activity log: what changed in the store and when, derived from records the store already keeps, plus a session ledger.

`log` reads note edits from the operation journals (.operations/*/journal.json: every note change keeps its before and after text)
and sessions from ledger/sessions-YYYY.jsonl. The journal rows are a
derived, incremental cache (.cache/activity.json, keyed by operation ID): a deleted cache rebuilds from the journals, and a
journal that is not final yet is not cached; an entry also keeps its journal's mtime and size, so a journal that recovery rewrote is read again. The ledger is the only canonical part: append-only, never pruned, no text."""
from __future__ import annotations
import base64,difflib,json,os,re
from datetime import timedelta,timezone
from pathlib import Path
from .common import parse_iso
from .repository import BOOKKEEPING_KINDS,FINAL_STATES,_TAIL_STATE,canonical_json,digest,now,os_user,parse_text,read_json

CACHE_VERSION=4
NOTE_PATH=re.compile(r'domains/([^/]+)/(?:notes|archive)/.+\.md')
CONTENT=('title','body','type','tags','context','files','commands','sources')
STATUS_EVENT={'superseded':'superseded','resolved':'resolved','archived':'archived','active':'reopened'}
LIFECYCLE={'created','superseded','resolved','archived','restored','deleted','promoted'}
EVENTS=('created','updated','confirmed','superseded','resolved','archived','reopened','restored','pinned','unpinned','ruled','unruled','deleted','promoted','moved','undone','session')
RETIRED={'superseded','resolved','archived'}
PLAN_KINDS={'merge','rewrite','bulk_retag','reference_remap','import','ingest','schema_migration','note_type_migration','baseline_migration','taxonomy_expansion'}
PLAN_PREFIXES=('project_','system_','change_')   # change_: legacy journal kind (Changes removed, CON-17)

def _cache_file(root):return Path(root)/'.cache'/'activity.json'
def _ledger_dir(root):return Path(root)/'ledger'

def _final(path):
    """The journal's state when it is final, else None (unfinished or unreadable). Only the tail is read, as repository does."""
    try:
        with path.open('rb') as handle:
            handle.seek(0,os.SEEK_END)
            handle.seek(max(0,handle.tell()-64))
            match=_TAIL_STATE.search(handle.read())
        state=match[1].decode() if match else read_json(path)['state']
    except (OSError,ValueError,KeyError,TypeError):return None
    return state if state in FINAL_STATES else None

def _decode(data,path=''):
    return parse_text(base64.b64decode(data).decode('utf-8'),path) if data is not None else None

def _row(op,created,event,ref,title,kind,domain,projects,**extra):
    return {'time':created,'event':event,'ref':ref,'title':title,'type':kind,'domain':domain,'projects':projects,'op':op,**extra}

def _event(kind,old,new):
    """The event for one note's before and after, or None when nothing a reader asked for changed."""
    if kind.startswith('undo:'):return 'undone'
    if old is None:return 'created'
    if new is None:return 'deleted'
    (old_domain,o),(new_domain,n)=old,new
    was,became=o.get('status','active'),n.get('status','active')
    if was!=became:return 'restored' if (was,became)==('archived','active') else STATUS_EVENT.get(became,'updated')
    if old_domain!=new_domain:return 'promoted' if 'promote' in kind else 'moved'
    if any(o.get(k)!=n.get(k) for k in CONTENT):return 'updated'
    if bool(o.get('pinned'))!=bool(n.get('pinned')):return 'pinned' if n.get('pinned') else 'unpinned'
    if bool(o.get('rule'))!=bool(n.get('rule')):return 'ruled' if n.get('rule') else 'unruled'
    if o.get('confirmed')!=n.get('confirmed') or o.get('checked')!=n.get('checked'):return 'confirmed'
    return None

def _short(text,n=200):
    text=' '.join(str(text).split())
    return text if len(text)<=n else text[:n-3].rstrip()+'...'

def _tags(o,n):
    old,new=set(o.get('tags') or []),set(n.get('tags') or [])
    return ' '.join([f'+{t}' for t in sorted(new-old)]+[f'-{t}' for t in sorted(old-new)])

def _actions(o,n):
    from .note_format import action_items
    old,new=dict((t,d) for d,t in action_items(o.get('body') or '')),dict((t,d) for d,t in action_items(n.get('body') or ''))
    out=[f'ticked "{_short(t,40)}"' for t,d in new.items() if d and t in old and not old[t]]+[f'unticked "{_short(t,40)}"' for t,d in new.items() if not d and old.get(t)]
    return out+[f'added "{_short(t,40)}"' for t in new if t not in old]+[f'removed "{_short(t,40)}"' for t in old if t not in new]

def _body_change(o,n,skip_actions):
    """The changed section headings and the lines added and removed in them; the Action items section is left out when its checkbox changes are reported on their own."""
    from .note_format import parts
    def labelled(body):
        out={}
        for k,h,t in parts(body):
            base=label=h or k or 'text'
            i=2
            while label in out:
                label=f'{base} ({i})'
                i+=1
            out[label]=(k,t)
        return out
    old,new=labelled(o.get('body') or ''),labelled(n.get('body') or '')
    labels=[]
    plus=minus=0
    for label in [*old,*[x for x in new if x not in old]]:
        a,b=old.get(label,('',''))[1],new.get(label,('',''))[1]
        if a==b or (skip_actions and (old.get(label) or new.get(label))[0]=='actions'):continue
        labels.append(label)
        for line in difflib.unified_diff(a.splitlines(),b.splitlines(),lineterm='',n=0):
            if line.startswith('+') and not line.startswith('+++'):plus+=1
            elif line.startswith('-') and not line.startswith('---'):minus+=1
    return f"{', '.join(labels)} +{plus}/-{minus} lines" if labels else ''

def _updated(o,n):
    found=[]
    if o.get('title')!=n.get('title'):found.append(f"title \"{_short(o.get('title'),40)}\" -> \"{_short(n.get('title'),40)}\"")
    if o.get('tags')!=n.get('tags'):
        tag_diff=_tags(o,n)
        found.append('tags '+tag_diff if tag_diff else 'tags reordered')
    if o.get('type')!=n.get('type'):found.append(f"type {o.get('type')} -> {n.get('type')}")
    if o.get('files')!=n.get('files'):found.append('files changed')
    if o.get('review_by')!=n.get('review_by'):found.append(f"review date -> {n.get('review_by')}")
    items=_actions(o,n)
    found+=items
    if o.get('body')!=n.get('body'):
        body_diff=_body_change(o,n,bool(items))
        if body_diff:found.append(body_diff)
    found+=[f'{k} changed' for k in ('context','commands','sources') if o.get(k)!=n.get(k)]
    if o.get('distinct_from')!=n.get('distinct_from'):
        added=[e for e in n.get('distinct_from') or [] if e not in (o.get('distinct_from') or [])]
        found.append('marked distinct from '+str(added[0]['ref']).rpartition(':')[2][:8] if added else 'distinct record removed')
    return '; '.join(found) or 'edited'

def _summary(kind,event,old,new):
    """One line (200 characters at most) saying what an event did to a note; old and new are the (domain, note) pairs _event received."""
    o,n=(old[1] if old else {}),(new[1] if new else {})
    r=n.get('last_retirement') if isinstance(n.get('last_retirement'),dict) else {}
    if event=='created':
        from .note_format import summary_text
        first=re.split(r'(?<=[.!?])\s',' '.join(summary_text(n.get('body') or '').split()),maxsplit=1)[0]
        text=f"Created as {n.get('type')}"+(f': {first}' if first else '')
    elif event=='updated':text=_updated(o,n)
    elif event in ('resolved','archived'):
        # Who did it is the row's actor; the summary says why (maintenance records a rule and a reason).
        text=': '.join(str(r[k]) for k in ('rule','reason') if r.get(k)) or event
    elif event=='superseded':
        by=str(n.get('superseded_by') or r.get('successor') or '').rpartition(':')[2][:8]
        text=f'superseded by {by}' if by else 'superseded'
    elif event=='restored':text='restored and pinned' if n.get('pinned') else 'restored'
    elif event=='confirmed':text='confirmed' if o.get('confirmed')!=n.get('confirmed') else 'checked'
    elif event in ('promoted','moved'):text=f'{event} to {new[0]}'
    elif event=='undone':text='undid '+kind[5:13]
    else:text=event
    return _short(text)

def _actor(op,kind,old,new):
    """Who made the change: maintenance, auto_resolve, obsidian, undo, plan, agent:HARNESS (a capture with an origin) or command."""
    n=new[1] if new else {}
    r=n.get('last_retirement') if isinstance(n.get('last_retirement'),dict) else {}
    if str(op).startswith('m-'):return 'maintenance'
    if kind.startswith('undo:'):return 'undo'
    if kind.startswith('overlay_'):return 'obsidian'
    if kind=='rebaseline':return 'rebaseline'
    # A retirement records who asked for it: agent (a direct command), plan (before 2026-11-26), vault (an Obsidian change), auto_resolve or maintenance.
    if r.get('actor') and n.get('status') in RETIRED and (old[1].get('status','active') if old else None)!=n.get('status'):
        return {'agent':'command','vault':'obsidian'}.get(str(r['actor']),str(r['actor']))
    if (kind in PLAN_KINDS or kind.startswith(PLAN_PREFIXES)) and not str(op).startswith('d-'):return 'plan'   # a direct write (governance.commit) has an ID starting d-; older operations of these kinds were applied plans
    if kind.startswith('capture') and n.get('origin'):return 'agent:'+str(n['origin']).partition(':')[0]
    return 'command'

def _unlinked(body):
    """The body with Markdown link targets blanked: equal before and after when an edit only rewrote paths (migration.rewrite_type_links)."""
    body=re.sub(r'(!?\[[^\]\n]*\]\()[^)\n]*\)',r'\1)',body or '')
    return re.sub(r'(?m)^( {0,3}\[[^\]\r\n]+\]:)[^\r\n]*$',r'\1',body)

def _note_rows(op,kind,created,effects):
    slots={}
    for effect in effects:
        found=NOTE_PATH.fullmatch(effect['path'])
        if not found:continue
        for side in ('before','after'):
            note=_decode(effect[side],effect['path'])
            if note and isinstance(note.get('id'),str):slots.setdefault(note['id'],{}).setdefault(side,(found[1],note))
    events={i:_event(kind,s.get('before'),s.get('after')) for i,s in slots.items()}
    lifecycle=any(e in LIFECYCLE for e in events.values())
    rows=[]
    for note_id,slot in slots.items():
        event=events[note_id]
        if not event:continue
        if event=='updated' and lifecycle and kind!='merge' and slot.get('before') and slot.get('after'):
            o,n=slot['before'][1],slot['after'][1]
            # A referring note whose links were rewritten because another note moved: the same move, not an edit of its own.
            if all(o.get(k)==n.get(k) for k in CONTENT if k!='body') and _unlinked(o.get('body'))==_unlinked(n.get('body')):continue
        domain,note=slot.get('after') or slot['before']
        rows.append(_row(op,created,event,f"{domain}:{note_id}",note.get('title'),note.get('type'),domain,[str(t)[8:] for t in note.get('tags') or [] if str(t).startswith('project/')],summary=_summary(kind,event,slot.get('before'),slot.get('after')),actor=_actor(op,kind,slot.get('before'),slot.get('after'))))
    return rows

def _sig(path):
    """[mtime_ns, size] of a journal file, None when it is gone: recovery rewrites a final journal (committed <-> rolled_back), which changes both."""
    try:st=path.stat()
    except OSError:return None
    return [st.st_mtime_ns,st.st_size]

def _read(path):
    """The cache entry for one journal, or None while it is not final (it is read again next time)."""
    sig=_sig(path)
    state=_final(path)
    if state is None or sig is None:return None
    try:
        journal=read_json(path)
        op,kind,created,effects=journal['id'],str(journal['kind']),str(journal['created']),journal['effects']
        rows=[] if state!='committed' or kind in BOOKKEEPING_KINDS else _note_rows(op,kind,created,effects)
        user=journal.get('os_user')
        if isinstance(user,str) and user:   # who ran the process that wrote the journal (an older journal has none)
            for row in rows:row['user']=user
    except (ValueError,KeyError,TypeError,UnicodeError):return None
    return {'created':created,'kind':kind,'rows':rows,'files':[e['path'] for e in effects],'sig':sig}

def _cached(root):
    try:value=json.loads(_cache_file(root).read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}
    ops=value.get('ops') if isinstance(value,dict) and value.get('version')==CACHE_VERSION else None
    return ops if isinstance(ops,dict) else {}

def _entries(root):
    """The cache entry of every final journal, by operation ID. Journals already in the cache are not opened: a run after the first only lists and stats the folder."""
    root=Path(root)
    cached=_cached(root)
    found={}
    fresh=False
    ops_dir=root/'.operations'
    if ops_dir.is_dir():
        try:entries=sorted((e.name,e.path) for e in os.scandir(ops_dir) if e.name!='.pending' and e.is_dir(follow_symlinks=False))
        except OSError:entries=[]
        for op,folder in entries:
            path=Path(folder)/'journal.json'
            sig=_sig(path)
            if sig is None:continue
            hit=cached.get(op)
            if isinstance(hit,dict) and isinstance(hit.get('rows'),list) and hit.get('sig')==sig:
                found[op]=hit
                continue
            entry=_read(path)
            if entry is not None:
                found[op]=entry
                fresh=True
    if fresh or set(found)!=set(cached):
        try:
            from .common import atomic_write
            atomic_write(_cache_file(root),json.dumps({'version':CACHE_VERSION,'ops':found},separators=(',',':'),ensure_ascii=False),fsync=False)
        except OSError:pass   # derived: a cache that cannot be written only costs a rebuild
    return found

def journal_rows(root):
    """Rows of every final note, change and undo journal."""
    return [row for entry in _entries(root).values() for row in entry['rows']]

def note_history(root,note_id,limit=20):
    """The operations that changed one note itself, newest first, as history rows {operation_id, kind, state, created, files, event, title, summary, actor}."""
    rows=[]
    for op,entry in _entries(root).items():
        row=next((r for r in entry['rows'] if r['ref'].rpartition(':')[2]==note_id),None)
        if row:rows.append({'operation_id':op,'kind':entry['kind'],'state':'committed','created':entry['created'],'files':entry.get('files',[]),'event':row['event'],'title':row['title'],'summary':row.get('summary'),'actor':row.get('actor'),**({'user':row['user']} if row.get('user') else {})})
    return sorted(rows,key=lambda r:r['created'],reverse=True)[:max(1,limit)]

def operation_label(entry,op,kind):
    """(summary, actor) of one whole operation for a bare history row: its first row's, with a note count when it changed several;
    an operation with no row (unfinished, bookkeeping) is its kind, made by whoever its ID and kind say."""
    rows=entry.get('rows') if entry else None
    if not rows:return _short(kind),_actor(op,kind,None,None)
    first=rows[0]
    text=first.get('summary') or first.get('event') or kind
    return _short(text if len(rows)==1 else f"{len(rows)} changes: {text}"),first.get('actor') or 'command'

def markdown(rows):
    """A dated list a person can read, one line per history row: - 2026-11-14 18:25 archived (maintenance, user): summary (the actor, and the OS user when the row has one)."""
    return '\n'.join(f"- {str(r['created'])[:16].replace('T',' ')} {r.get('event') or r['kind']}"+(f" ({r['actor']}{', '+r['user'] if r.get('user') else ''})" if r.get('actor') else '')+(f": {r['summary']}" if r.get('summary') else '') for r in rows)

def _absolute(path,prefixes):
    """A touched path as the hooks store it (relative to a folder bound to the domain, else absolute) made absolute: the bound folder that holds the file, else the first."""
    if Path(path).is_absolute() or not prefixes:return path
    return str(next((p/path for p in prefixes if (p/path).exists()),prefixes[0]/path))

def _slugs(root,cwd,state,domain=None):
    from . import projects
    from .common import domain_prefixes,load_bindings
    try:
        entries=projects.load(root)['projects']
        prefixes=domain_prefixes(domain,load_bindings(root))
    except (OSError,ValueError):return []
    paths=([str(cwd)] if cwd else [])+[_absolute(x,prefixes) for x in state.get('touched_session') or []]
    return sorted({p['slug'] for p in (projects.match_path(x,entries) for x in paths) if p})

def record_session(root,domain,session,state,cwd=None):
    """Append this session's row to ledger/sessions-YYYY.jsonl: {t, s, h, d, p, n, f, r?} (time, session hash, harness, domain, project
    slugs, refs saved, files changed, r: 1 when a turn had a receipt but nothing was saved, such as "nothing durable", and u: the OS user name when known), no text. A quiet session has no row, and a row is written only when it differs from the
    session's last one (state['ledger_sig']). Returns the new signature when a row was appended, else None."""
    saved=list(dict.fromkeys(state.get('saved') or []))
    files=len(state.get('touched_session') or [])
    if not saved and not files:return None
    row={'s':session,'h':state.get('harness') or 'unknown','d':domain,'p':_slugs(root,cwd,state,domain),'n':saved,'f':files,**({'r':1} if state.get('receipted') and not saved else {})}
    sig=digest(canonical_json(row).encode())[:12]
    if sig==state.get('ledger_sig'):return None
    user=os_user()
    stamp=now()
    path=_ledger_dir(root)/f'sessions-{stamp[:4]}.jsonl'
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('ab') as handle:handle.write((json.dumps({'t':stamp,**row,**({'u':user} if user else {})},ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8'))
    return sig

def last_session_rows(root):
    """{session hash: its last raw ledger row}."""
    last={}
    for path in sorted(_ledger_dir(root).glob('sessions-*.jsonl')):
        try:lines=path.read_text(encoding='utf-8').splitlines()
        except (OSError,UnicodeError):continue
        for line in lines:
            try:row=json.loads(line)
            except ValueError:continue
            if isinstance(row,dict) and isinstance(row.get('s'),str) and isinstance(row.get('t'),str):last[row['s']]=row
    return last

def session_rows(root):
    """One row per session (its last ledger row), shaped like the journal rows."""
    rows=[]
    for sid,r in last_session_rows(root).items():
        notes=[n for n in r.get('n') or [] if isinstance(n,str)]
        files=r.get('f') if type(r.get('f')) is int else 0
        harness=str(r.get('h') or 'unknown')
        rows.append(_row(None,r['t'],'session',sid,f"{harness}: {len(notes)} note(s) saved, {files} file(s) changed",'session',r.get('d'),[p for p in r.get('p') or [] if isinstance(p,str)],harness=harness,notes=notes,files=files,**({'user':r['u']} if isinstance(r.get('u'),str) and r['u'] else {})))
    return rows

def _instant(value):
    """The aware datetime of an ISO date or time (naive counts as UTC, as the store writes UTC); ValueError when it is neither."""
    moment=parse_iso(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

def _bound(value,name,end=False):
    """The instant of a --since or --until value; a date alone covers its whole day, so --until DATE reaches the day's last microsecond."""
    try:moment=_instant(value)
    except ValueError:raise ValueError(f'--{name} must be an ISO date or time, such as 2026-11-13') from None
    return moment+timedelta(days=1,microseconds=-1) if end and 'T' not in value and ' ' not in value else moment

def _at(row):
    try:return _instant(row['time'])
    except (ValueError,TypeError):return None

def log(root,domain=None,project=None,since=None,until=None,note=None,kind=None,event=None,limit=50):
    """Rows newest first {time, event, ref, title, type, domain, projects, op}; a session row may add harness, notes and files; a row adds user (the OS user name) when the journal or ledger row recorded one.
    note: a full note ID. since and until are ISO dates or times compared as instants (naive is UTC; a date alone covers that whole UTC day), until inclusive."""
    if event and event not in EVENTS:raise ValueError('--event must be one of '+', '.join(EVENTS))
    low=_bound(since,'since') if since else None
    high=_bound(until,'until',True) if until else None
    wanted=[]
    for row in journal_rows(root)+session_rows(root):
        if domain and row['domain']!=domain:continue
        if project and project not in row['projects']:continue
        if low or high:
            when=_at(row)
            if when is None or (low and when<low) or (high and when>high):continue
        if kind and row['type']!=kind:continue
        if event and row['event']!=event:continue
        if note and note.casefold() not in [row['ref'].rpartition(':')[2].casefold(),*[n.rpartition(':')[2].casefold() for n in row.get('notes') or []]]:continue
        wanted.append(row)
    wanted.sort(key=lambda r:r['time'],reverse=True)
    return {'ok':True,'count':len(wanted),'rows':wanted[:max(1,limit)]}
