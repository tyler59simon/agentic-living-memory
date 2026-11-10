"""Usage diary: which notes were shown, opened, changed or re-captured, and when.

Local only and never sent anywhere. Each line holds an event name, a note ref, a 12-character session hash and a
date: no prompt text, titles or bodies. One file per UTC day in .turns/, so the 30-day .turns prune
(capture.prune) removes old days. Writes fail open: the diary must never cost a hook or a command.
"""
from __future__ import annotations
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path

EVENTS=('inject','read','update','check','supersede','confirm','duplicate','resolve','reopen','reminder','signal-capture')
USE=('read','update','check','supersede','confirm','duplicate','resolve','reopen')   # evidence that a note mattered
NOISY_MIN=3
# Injection kinds that are standing context rather than an answer retrieved for a question: session-start
# guidance (the same few notes every session) and a repo's codebase map (delivered whole).
STANDING=('session','prompt-map')
LOCK_SECONDS=1

def _path(root,when):return Path(root)/'.turns'/f'usage-{when:%Y-%m-%d}.jsonl'

def record(root,event,refs=(None,),session=None,kind=None):
    """Append one line per ref. Never raises."""
    try:
        from .common import file_lock
        from .runtime import session_hash
        when=datetime.now(timezone.utc);path=_path(root,when)
        s=session_hash(session) if session and session!='unknown' else None
        lines=''.join(json.dumps({'t':when.isoformat(timespec='seconds'),'e':event,'ref':ref,'s':s,'k':kind},separators=(',',':'))+'\n' for ref in refs)
        if not lines:return
        with file_lock(path.with_suffix('.lock'),timeout=LOCK_SECONDS):
            with path.open('a',encoding='utf-8',newline='\n') as out:out.write(lines)
    except Exception as exc:
        from .logs import swallowed
        swallowed('usage.record',exc,root)

def events(root,days=30):
    """Diary rows from the last `days` days, oldest first; unreadable lines are skipped."""
    cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).date();rows=[]
    for path in sorted((Path(root)/'.turns').glob('usage-*.jsonl')):
        try:
            if datetime.strptime(path.stem[6:],'%Y-%m-%d').date()<cutoff:continue
            text=path.read_text(encoding='utf-8')
        except (OSError,ValueError):continue
        for line in text.splitlines():
            try:row=json.loads(line)
            except ValueError:continue
            if isinstance(row,dict) and row.get('e') in EVENTS:rows.append(row)
    return rows

# Reinforcement (#9): ACT-R-style base-level activation, ln(1 + sum of w * age_days^-DECAY), where each
# presentation is a creation, content update, confirmation or diary use. Recent and repeated use scores higher.
# Ordering only: prompt-injection thresholds stay purely about relevance.
USE_STRENGTH=True   # on since the 2026-11-06 eval: index coverage 0.714 -> 0.762 with hit@3 unchanged; off = recency
DECAY=0.5
WEIGHTS={'duplicate':1.0,'update':1.0,'check':1.0,'confirm':1.0,'supersede':1.0,'read':0.5}
STRENGTH_NUDGE=0.05  # at most +5% on a search hit's fused score

def _day(value):
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')[:25]).date()
    except ValueError:
        try:return datetime.strptime(str(value)[:10],'%Y-%m-%d').date()
        except ValueError:return None

def strength(note,uses=(),today=None):
    """Activation for one note. uses: (event, iso time) pairs from the diary."""
    import math
    today=today or datetime.now(timezone.utc).date();presented=[]
    for value in (note.get('created'),note.get('updated') if note.get('updated')!=note.get('created') else None):
        if value and _day(value):presented.append((1.0,_day(value)))
    presented+=[(1.0,_day(d)) for d in note.get('confirmed') or [] if _day(d)]
    presented+=[(WEIGHTS[e],_day(t)) for e,t in uses if e in WEIGHTS and _day(t)]
    return math.log(1+sum(w*max(1,(today-day).days)**-DECAY for w,day in presented))

def strength_map(root,domains,today=None):
    """{'domain:id': strength} for the active notes of these domains."""
    from .repository import inventory
    uses={}
    for row in events(root):
        if isinstance(row.get('ref'),str):uses.setdefault(row['ref'],[]).append((row['e'],row.get('t')))
    found={}
    for domain in dict.fromkeys(domains):
        try:notes=inventory(domain,root)
        except (OSError,ValueError):continue
        for note in notes:
            ref=f"{domain}:{note['id']}";found[ref]=strength(note,uses.get(ref,()),today)
    return found

def used_refs(root,days=30):
    """Refs the diary saw read or used in the window."""
    return {row['ref'] for row in events(root,days) if row['e'] in USE and isinstance(row.get('ref'),str)}

def stats(root,domains,days=30,limit=10):
    """Per-note counts for notes in `domains`, the noisy (shown, never used) and the most used notes,
    reminders by kind and captures made on turns that carried a durable signal, per ISO week."""
    from .repository import inventory
    from .notes import short_ref
    rows=events(root,days);counts={};reminders={};weekly={};sessions=set()
    for row in rows:
        event=row['e']
        if event=='reminder':reminders[row.get('k') or 'unknown']=reminders.get(row.get('k') or 'unknown',0)+1;continue
        if event=='signal-capture':
            try:year,week,_=datetime.fromisoformat(row['t']).isocalendar();key=f'{year}-W{week:02d}'
            except (KeyError,ValueError,TypeError):continue
            weekly[key]=weekly.get(key,0)+1;continue
        ref=row.get('ref')
        if not isinstance(ref,str) or ref.split(':',1)[0] not in domains:continue
        entry=counts.setdefault(ref,dict.fromkeys(('inject','standing')+USE,0))
        # Standing context is delivered whatever the prompt, so going unread says nothing about the note.
        entry['standing' if event=='inject' and row.get('k') in STANDING else event]+=1
        if event=='inject' and row.get('s'):sessions.add(row['s'])
    notes={}
    for domain in domains:
        try:notes.update({f'{domain}:{n["id"]}':n for n in inventory(domain,root,retired=True)})
        except (OSError,ValueError):continue
    def item(ref,c):
        note=notes.get(ref);domain,note_id=ref.split(':',1)
        return {'ref':ref,'short_ref':short_ref(domain,note_id),'title':note['title'] if note else None,
                'status':note.get('status','active') if note else 'missing','counts':{k:v for k,v in c.items() if v}}
    used=lambda c:sum(c[k] for k in USE)
    noisy=sorted(((r,c) for r,c in counts.items() if c['inject']>=NOISY_MIN and not used(c)),key=lambda x:(-x[1]['inject'],x[0]))
    top=sorted(((r,c) for r,c in counts.items() if used(c)),key=lambda x:(-used(x[1]),-x[1]['inject'],x[0]))
    return {'ok':True,'days':days,'domains':list(domains),'events':len(rows),'sessions_with_injections':len(sessions),
            'notes':len(counts),
            'noisy':[item(r,c) for r,c in noisy[:limit]],
            'used':[item(r,c) for r,c in top[:limit]],
            'reminders':reminders,'signal_captures':dict(sorted(weekly.items())),
            'hint':'noisy = retrieved for a prompt or a file at least %d times and never read, updated, confirmed or re-captured: consider a sharper title or an archive plan. '
                   'The injected snippet alone may have been enough, so treat it as a lead, not a verdict.'%NOISY_MIN}
