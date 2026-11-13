"""Activity log: what changed in the store and when, derived from records the store already keeps, plus a session ledger.

`log` reads note edits from the operation journals (.operations/*/journal.json: every note change keeps its before and after text),
the change registry (changes.json) from the same journals, and sessions from ledger/sessions-YYYY.jsonl. The journal rows are a
derived, incremental cache (.cache/activity.json, keyed by operation ID): a deleted cache rebuilds from the journals, and a
journal that is not final yet is not cached; an entry also keeps its journal's mtime and size, so a journal that recovery rewrote is read again. The ledger is the only canonical part: append-only, never pruned, no text."""
from __future__ import annotations
import base64,json,os,re
from datetime import datetime,timedelta,timezone
from pathlib import Path
from .repository import BOOKKEEPING_KINDS,FINAL_STATES,_TAIL_STATE,canonical_json,digest,now,parse_text,read_json

CACHE_VERSION=1
NOTE_PATH=re.compile(r'domains/([^/]+)/(?:notes|archive)/.+\.md')
CONTENT=('title','body','type','tags','context','files','commands','sources')
STATUS_EVENT={'superseded':'superseded','resolved':'resolved','archived':'archived','active':'reopened'}
LIFECYCLE={'created','superseded','resolved','archived','deleted','promoted'}
EVENTS=('created','updated','confirmed','superseded','resolved','archived','reopened','deleted','promoted','moved','undone','stage','closed','session')
CHANGE_FIELDS=('title','repo','branch','system','element')

def _cache_file(root):return Path(root)/'.cache'/'activity.json'
def _ledger_dir(root):return Path(root)/'ledger'

def _final(path):
    """The journal's state when it is final, else None (unfinished or unreadable). Only the tail is read, as repository does."""
    try:
        with path.open('rb') as handle:
            handle.seek(0,os.SEEK_END);handle.seek(max(0,handle.tell()-64));match=_TAIL_STATE.search(handle.read())
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
    if was!=became:return STATUS_EVENT.get(became,'updated')
    if old_domain!=new_domain:return 'promoted' if 'promote' in kind else 'moved'
    if any(o.get(k)!=n.get(k) for k in CONTENT):return 'updated'
    if o.get('confirmed')!=n.get('confirmed') or o.get('checked')!=n.get('checked'):return 'confirmed'
    return None

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
            if all(o.get(k)==n.get(k) for k in CONTENT if k!='body'):continue
        domain,note=slot.get('after') or slot['before']
        rows.append(_row(op,created,event,f"{domain}:{note_id}",note.get('title'),note.get('type'),domain,[str(t)[8:] for t in note.get('tags') or [] if str(t).startswith('project/')]))
    return rows

def _registry(data):
    try:value=json.loads(base64.b64decode(data).decode('utf-8')) if data is not None else {}
    except (ValueError,UnicodeError):return {}
    return {c['id']:c for c in value.get('changes',[]) if isinstance(c,dict) and type(c.get('id')) is int} if isinstance(value,dict) else {}

def _change_rows(op,kind,created,effects):
    from .changes import display
    rows=[]
    for effect in effects:
        if effect['path']!='changes.json':continue
        old,new=_registry(effect['before']),_registry(effect['after'])
        for number in sorted(set(old)|set(new)):
            before,after=old.get(number),new.get(number);entry=after or before;extra={}
            if kind.startswith('undo:'):event='undone'
            elif not before:event='created'
            elif not after:event='deleted'
            elif before.get('stage')!=after.get('stage'):event='stage';extra={'detail':f"{before.get('stage')} -> {after.get('stage')}"}
            elif before.get('status')!=after.get('status'):event='closed' if after.get('status')!='active' else 'reopened';extra={'detail':after.get('status')}
            elif any(before.get(k)!=after.get(k) for k in CHANGE_FIELDS):event='updated'
            else:continue
            rows.append(_row(op,created,event,display(number),entry.get('title'),'change',str(entry.get('brief') or '').partition(':')[0] or None,[entry['project']] if entry.get('project') else [],**extra))
    return rows

def _sig(path):
    """[mtime_ns, size] of a journal file, None when it is gone: recovery rewrites a final journal (committed <-> rolled_back), which changes both."""
    try:st=path.stat()
    except OSError:return None
    return [st.st_mtime_ns,st.st_size]

def _read(path):
    """The cache entry for one journal, or None while it is not final (it is read again next time)."""
    sig=_sig(path);state=_final(path)
    if state is None or sig is None:return None
    try:
        journal=read_json(path);op,kind,created,effects=journal['id'],str(journal['kind']),str(journal['created']),journal['effects']
        rows=[] if state!='committed' or kind in BOOKKEEPING_KINDS else _note_rows(op,kind,created,effects)+_change_rows(op,kind,created,effects)
    except (ValueError,KeyError,TypeError,UnicodeError):return None
    return {'created':created,'kind':kind,'rows':rows,'sig':sig}

def _cached(root):
    try:value=json.loads(_cache_file(root).read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}
    ops=value.get('ops') if isinstance(value,dict) and value.get('version')==CACHE_VERSION else None
    return ops if isinstance(ops,dict) else {}

def journal_rows(root):
    """Rows of every final note, change and undo journal. Journals already in the cache are not opened: a run after the first only lists and stats the folder."""
    root=Path(root);cached=_cached(root);found={};fresh=False
    for path in sorted(root.glob('.operations/*/journal.json')):
        op=path.parent.name;hit=cached.get(op)
        if isinstance(hit,dict) and isinstance(hit.get('rows'),list) and hit.get('sig')==_sig(path):found[op]=hit;continue
        entry=_read(path)
        if entry is not None:found[op]=entry;fresh=True
    if fresh or set(found)!=set(cached):
        try:
            from .common import atomic_write
            atomic_write(_cache_file(root),json.dumps({'version':CACHE_VERSION,'ops':found},separators=(',',':'),ensure_ascii=False))
        except OSError:pass   # derived: a cache that cannot be written only costs a rebuild
    return [row for entry in found.values() for row in entry['rows']]

def _absolute(path,prefixes):
    """A touched path as the hooks store it (relative to a folder bound to the domain, else absolute) made absolute: the bound folder that holds the file, else the first."""
    if Path(path).is_absolute() or not prefixes:return path
    return str(next((p/path for p in prefixes if (p/path).exists()),prefixes[0]/path))

def _slugs(root,cwd,state,domain=None):
    from . import projects
    from .common import domain_prefixes,load_bindings
    try:entries=projects.load(root)['projects'];prefixes=domain_prefixes(domain,load_bindings(root))
    except (OSError,ValueError):return []
    paths=([str(cwd)] if cwd else [])+[_absolute(x,prefixes) for x in state.get('touched_session') or []]
    return sorted({p['slug'] for p in (projects.match_path(x,entries) for x in paths) if p})

def record_session(root,domain,session,state,cwd=None):
    """Append this session's row to ledger/sessions-YYYY.jsonl: {t, s, h, d, p, n, f} (time, session hash, harness, domain, project
    slugs, refs saved, files changed), no text. A quiet session has no row, and a row is written only when it differs from the
    session's last one (state['ledger_sig']). Returns the new signature when a row was appended, else None."""
    saved=list(dict.fromkeys(state.get('saved') or []));files=len(state.get('touched_session') or [])
    if not saved and not files:return None
    row={'s':session,'h':state.get('harness') or 'unknown','d':domain,'p':_slugs(root,cwd,state,domain),'n':saved,'f':files}
    sig=digest(canonical_json(row).encode())[:12]
    if sig==state.get('ledger_sig'):return None
    stamp=now();path=_ledger_dir(root)/f'sessions-{stamp[:4]}.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('ab') as handle:handle.write((json.dumps({'t':stamp,**row},ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8'))
    return sig

def session_rows(root):
    """One row per session (its last ledger row), shaped like the journal rows."""
    last={}
    for path in sorted(_ledger_dir(root).glob('sessions-*.jsonl')):
        try:lines=path.read_text(encoding='utf-8').splitlines()
        except (OSError,UnicodeError):continue
        for line in lines:
            try:row=json.loads(line)
            except ValueError:continue
            if isinstance(row,dict) and isinstance(row.get('s'),str) and isinstance(row.get('t'),str):last[row['s']]=row
    rows=[]
    for sid,r in last.items():
        notes=[n for n in r.get('n') or [] if isinstance(n,str)];files=r.get('f') if type(r.get('f')) is int else 0;harness=str(r.get('h') or 'unknown')
        rows.append(_row(None,r['t'],'session',sid,f"{harness}: {len(notes)} note(s) saved, {files} file(s) changed",'session',r.get('d'),[p for p in r.get('p') or [] if isinstance(p,str)],harness=harness,notes=notes,files=files))
    return rows

def _instant(value):
    """The aware datetime of an ISO date or time (naive counts as UTC, as the store writes UTC); ValueError when it is neither."""
    moment=datetime.fromisoformat(value[:-1]+'+00:00' if value.endswith('Z') else value)
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
    """Rows newest first {time, event, ref, title, type, domain, projects, op}; a change row may add detail and a session row harness, notes and files.
    note: a full note ID or a change number such as CHG-014. since and until are ISO dates or times compared as instants (naive is UTC; a date alone covers that whole UTC day), until inclusive."""
    if event and event not in EVENTS:raise ValueError('--event must be one of '+', '.join(EVENTS))
    low=_bound(since,'since') if since else None;high=_bound(until,'until',True) if until else None
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
