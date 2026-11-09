"""Operational log: hook runs, CLI command runs and exceptions that would otherwise be swallowed.

Local only and never sent anywhere. One JSON object per line in STORE/logs/log-YYYY-MM-DD.jsonl (UTC day),
pruned after RETENTION_DAYS. Metadata only: no prompt text, injected context, titles, bodies or argv.
Exception messages are kept (capped, credentials redacted); tracebacks only as file:line function frames.
LIVING_MEMORY_LOG_LEVEL = debug|info|warning|error|off (default info). Writes fail open: the log must
never cost a hook or a command.
"""
from __future__ import annotations
import json,os,traceback
from datetime import datetime,timedelta,timezone
from pathlib import Path

LEVELS={'debug':10,'info':20,'warning':30,'error':40}
KINDS=('hook','command','internal')
DEFAULT_LEVEL='info'
RETENTION_DAYS=30
MESSAGE_CHARS=300
MAX_FRAMES=10
LOCK_SECONDS=1
APP=Path(__file__).resolve().parents[1]
_root=None

def use(root):
    """The store this process logs to, so deep call sites need no root parameter."""
    global _root;_root=Path(root)

def setting():
    value=os.environ.get('LIVING_MEMORY_LOG_LEVEL','').strip().lower()
    return value if value in LEVELS or value=='off' else DEFAULT_LEVEL

def enabled(level):
    current=setting()
    return current!='off' and LEVELS[level]>=LEVELS[current]

def _dir(root):return Path(root)/'logs'
def _path(root,when):return _dir(root)/f'log-{when:%Y-%m-%d}.jsonl'

def write(level,kind,event,root=None,**fields):
    """Append one line. Never raises."""
    try:
        root=root or _root
        if root is None or not enabled(level):return
        from .common import file_lock
        when=datetime.now(timezone.utc);path=_path(root,when)
        row={'ts':when.isoformat(timespec='milliseconds'),'level':level,'kind':kind,'event':event,**{k:v for k,v in fields.items() if v is not None},'pid':os.getpid()}
        line=json.dumps(row,ensure_ascii=False,separators=(',',':'),default=str)+'\n'
        with file_lock(path.with_suffix('.lock'),timeout=LOCK_SECONDS):
            with path.open('a',encoding='utf-8',newline='\n') as out:out.write(line)
    except Exception:pass

def message(text):
    """An exception message as it may be logged: credentials redacted, capped."""
    text=str(text)
    try:
        from .repository import redact
        text=redact(text)[0]
    except Exception:text='<unredactable message>'
    return text[:MESSAGE_CHARS]

def frames(exc):
    """file:line function for the innermost frames; no source lines, no locals."""
    out=[]
    for frame in traceback.extract_tb(exc.__traceback__)[-MAX_FRAMES:]:
        try:name=Path(frame.filename).resolve().relative_to(APP).as_posix()
        except (OSError,ValueError):name=Path(frame.filename).name
        out.append(f'{name}:{frame.lineno} {frame.name}')
    return out

def error_fields(exc):return {'type':type(exc).__name__,'message':message(exc),'frames':frames(exc)}

def swallowed(event,exc,root=None):
    """An exception a call site deliberately absorbs (fail-open housekeeping)."""
    write('warning','internal',event,root,error=error_fields(exc))

def _day(path):
    try:return datetime.strptime(path.stem[4:],'%Y-%m-%d').date()
    except ValueError:return None

def entries(root,level='info',kind=None,event=None,days=1,limit=50):
    """(matching rows newest first, at most `limit`; total matched)."""
    cutoff=(datetime.now(timezone.utc)-timedelta(days=max(1,days)-1)).date();rows=[]
    for path in sorted(_dir(root).glob('log-*.jsonl')):
        day=_day(path)
        if day is None or day<cutoff:continue
        try:text=path.read_text(encoding='utf-8')
        except OSError:continue
        for line in text.splitlines():
            try:row=json.loads(line)
            except ValueError:continue
            if not isinstance(row,dict) or LEVELS.get(row.get('level'),0)<LEVELS[level]:continue
            if (kind and row.get('kind')!=kind) or (event and row.get('event')!=event):continue
            rows.append(row)
    rows.reverse()
    return rows[:limit],len(rows)

def summary(root,days=7):
    """For doctor: the level setting, file count, and recent problems."""
    problems,_=entries(root,'warning',days=days,limit=10**6)
    errors=[r for r in problems if r.get('level')=='error']
    last=errors[0] if errors else None
    return {'level':setting(),'dir':str(_dir(root)),'files':len(list(_dir(root).glob('log-*.jsonl'))),
            f'errors_{days}d':len(errors),f'warnings_{days}d':len(problems)-len(errors),
            'last_error':{k:last.get(k) for k in ('ts','kind','event','error')} if last else None}

def prune(root,days=RETENTION_DAYS):
    """Remove day files (and their lock files) older than `days`, judged by the date in the name."""
    cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).date();removed=0
    for path in _dir(root).glob('log-*'):
        day=_day(path)
        if day is None or day>=cutoff:continue
        try:path.unlink();removed+=1
        except OSError:pass
    return removed
