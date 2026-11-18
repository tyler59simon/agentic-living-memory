"""Subagent proposal inbox: draft notes a subagent stages for the main agent to accept or reject.

A proposal is transient hook state like a capture receipt: one JSON file per draft in .turns/ (pruned with the rest
of .turns/ after 30 days), never a note, an index row or a journal entry. It stores the session as a hash, as the
usage diary does. Only the main agent turns a proposal into a note (capture --from-proposal ID) or drops it
(proposals reject ID); the hooks and the CLI refuse both for subagents.
"""
import re,secrets
from pathlib import Path
from .common import atomic_write
from .repository import canonical_json,read_json,now

PREFIX='proposal-'
# What a proposal carries into capture. Supersedes and allow_similar stay the main agent's call; a digest keeps its
# sources and the idempotency key derived from them, so the saved note still goes stale when its file changes.
FIELDS=('title','body','tags','type','context','files','commands','review_by','sources','idempotency_key','rule')

def _path(root,pid):
    if not isinstance(pid,str) or not re.fullmatch(r'[0-9a-f]{8}',pid):raise ValueError('A proposal ID is 8 hex characters, as proposals list shows it')
    return Path(root)/'.turns'/f'{PREFIX}{pid}.json'

def _hash(session):
    from .runtime import session_hash
    return session_hash(session)

def write(root,session,domain,data,warnings=()):
    pid=secrets.token_hex(4)
    note={k:data[k] for k in FIELDS if data.get(k) not in (None,'',[])}
    row={'schema':1,'id':pid,'session':_hash(session),'domain':domain,'created':now(),'status':'pending','note':note,'warnings':list(warnings)}
    atomic_write(_path(root,pid),canonical_json(row))
    return row

def get(root,pid):
    row=read_json(_path(root,pid))
    if not isinstance(row,dict):raise ValueError(f'No proposal {pid}; run proposals list')
    return row

def rows(root,status='pending',session=None):
    """Proposals oldest first; status 'any' lists all, session limits them to one session."""
    wanted=_hash(session) if session else None;out=[]
    for path in (Path(root)/'.turns').glob(PREFIX+'*.json'):
        row=read_json(path)
        if not isinstance(row,dict):continue
        if status!='any' and row.get('status')!=status:continue
        if wanted and row.get('session')!=wanted:continue
        out.append(row)
    return sorted(out,key=lambda r:(r.get('created',''),r.get('id','')))

def settle(root,pid,status,note_ref=None,reason=None):
    row=get(root,pid)
    if row.get('status')!='pending':raise ValueError(f"Proposal {pid} is already {row.get('status')}")
    row.update(status=status,settled=now())
    if note_ref:row['note_ref']=note_ref
    if reason:row['reason']=reason
    atomic_write(_path(root,pid),canonical_json(row))
    return row

def listing(row):
    """The short form proposals list shows."""
    note=row.get('note') or {}
    return {k:v for k,v in {'id':row.get('id'),'status':row.get('status'),'created':row.get('created'),'domain':row.get('domain'),
                            'type':note.get('type','knowledge'),'title':note.get('title'),'note_ref':row.get('note_ref'),'reason':row.get('reason')}.items() if v}
