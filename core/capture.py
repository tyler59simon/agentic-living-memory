"""Per-turn completion receipts: no prompts, transcripts or note bodies.

Receipts are transient hook state, not canonical memory, so they are written
atomically without a recovery journal and pruned after RETENTION_DAYS.
"""
import hashlib,time
from pathlib import Path
from .common import memory_root,atomic_write
from .repository import canonical_json,read_json,now

RETENTION_DAYS=30

def turn_key(session,turn):return hashlib.sha256((session+':'+turn).encode()).hexdigest()
def status(session,turn,root=None):return read_json(Path(root or memory_root())/'.turns'/(turn_key(session,turn)+'.json'))

def prune(root,days=RETENTION_DAYS):
    cutoff=time.time()-days*86400;removed=0
    for path in (Path(root)/'.turns').glob('*'):
        try:
            if path.is_file() and path.stat().st_mtime<cutoff:path.unlink();removed+=1
        except OSError:pass
    return removed

def finalize(session,turn,notes=(),root=None):
    root=Path(root or memory_root());key=turn_key(session,turn)
    from .notes import find_note
    resolved=[]
    for reference in notes:
        domain,note_id=reference.split(':',1)
        resolved.append(domain+':'+find_note(note_id,domain,root)[1]['id'])  # short refs are stored in full
    notes=resolved
    if status(session,turn,root):return {'ok':True,'status':'already_finalized'}
    row={'schema':1,'completed':now(),'notes':list(notes)[:20],'outcome':'saved' if notes else 'nothing_durable'}
    atomic_write(root/'.turns'/(key+'.json'),canonical_json(row))
    prune(root)
    return {'ok':True,'status':row['outcome']}
