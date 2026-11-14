"""Recap: what happened in the store over the last days, built from the activity log, the maintenance run records, the open work and the review worklist. Read-only."""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
from pathlib import Path

GROUPS=('created','updated','resolved','superseded','archived','restored','deleted')
MAX_LISTED=15
TOP_OPEN=5

def _changes(rows):
    """Rows (newest first) as {event: [{ref,title,type,summary,actor,time,count}]}: one entry per note in a group, the latest row, with how many rows there were."""
    out={g:{} for g in GROUPS}
    for row in rows:
        group=out.get(row['event'])
        if group is None:continue
        if row['ref'] in group:group[row['ref']]['count']+=1;continue
        group[row['ref']]={'ref':row['ref'],'title':row.get('title'),'type':row.get('type'),'summary':row.get('summary'),'actor':row.get('actor'),'time':row['time'],'count':1}
    return {g:list(v.values()) for g,v in out.items() if v}

def _titles(root,domains):
    from .repository import inventory
    found={}
    for d in domains:
        try:found.update({f"{d}:{n['id']}":n.get('title') for n in inventory(d,root,retired=True)})
        except (OSError,ValueError):continue
    return found

def _maintenance(root,domains,low,high):
    from . import maintenance,maintenance_state as ms
    out={'runs':0,'reviewed':0,'kept':0,'deferred':0,'updated':0,'confirmed':0,'retired':0,'charged_usd':'0.00','retired_notes':[]}
    try:runs=[r for r in maintenance._recent(root) if low<=ms._parse(r['attempted_at'])<=high]
    except (OSError,ValueError,KeyError,ms.Unavailable):return out
    micro=0;wanted=set(domains);inside=lambda ref:str(ref).partition(':')[0] in wanted
    for run in runs:
        # Only this scope's notes count; a run that reviewed none of them is left out. Its charge cannot be split by domain, so charged_usd is the whole run's.
        selected=[s for s in run['selected'] if inside(s.get('ref'))]
        if not selected:continue
        decisions={ref:d for ref,d in run['decisions'].items() if inside(ref)}
        s=maintenance._counts(list(decisions.values()));micro+=run['charged_micro'];out['runs']+=1
        out['reviewed']+=0 if run.get('released') else len(selected)
        for k in ('kept','deferred','updated','confirmed'):out[k]+=s[k]
        for ref,d in decisions.items():
            if d.get('result')=='applied' and d.get('action') in ('archive','supersede','resolve'):
                out['retired_notes'].append({'ref':ref,'action':d['action'],'rule':d.get('rule'),'reason':d.get('reason')})
    out['retired']=len(out['retired_notes']);out['charged_usd']=ms.format_usd(micro)
    titles=_titles(root,domains) if out['retired_notes'] else {}
    for item in out['retired_notes']:item['title']=titles.get(item['ref'])
    return out

def _sessions(rows):
    by={}
    for r in rows:by[r['harness']]=by.get(r['harness'],0)+1
    return {'count':len(rows),'by_harness':by,'notes_saved':len({n for r in rows for n in r.get('notes') or []})}

def _more(items):
    return [f'- and {len(items)-MAX_LISTED} more'] if len(items)>MAX_LISTED else []

def _markdown(since,until,counts,changes,maint,sessions,work,review):
    summary=f"{sum(counts.values())} change(s)"+(': '+', '.join(f'{n} {e}' for e,n in counts.items()) if counts else '')+f". {maint['runs']} maintenance run(s), {sessions['count']} session(s), {work['notes']} note(s) with open work, {review['due']} due for review."
    lines=[f'# Recap: {since[:10]} to {until[:10]}','','## Summary','',summary]
    for g in GROUPS:
        if g not in changes:continue
        lines+=['',f'## {g.capitalize()} ({len(changes[g])})','']
        lines+=[f"- {c['title']} ({c['actor']}): {c['summary']}"+(f" ({c['count']} times)" if c['count']>1 else '') for c in changes[g][:MAX_LISTED]]+_more(changes[g])
    lines+=['','## Maintenance','']
    if not maint['runs']:lines.append('No maintenance runs.')
    else:
        lines.append(f"{maint['runs']} run(s) reviewed {maint['reviewed']} note(s): {maint['updated']} updated, {maint['confirmed']} confirmed, {maint['retired']} retired, {maint['kept']} kept, {maint['deferred']} deferred. Charged {maint['charged_usd']} USD for these runs (every domain they covered).")
        lines+=[f"- {r['title'] or r['ref']} ({r['action']}): "+': '.join(str(r[k]) for k in ('rule','reason') if r.get(k)) for r in maint['retired_notes'][:MAX_LISTED]]+_more(maint['retired_notes'])
    lines+=['','## Sessions','']
    lines.append(f"{sessions['count']} session(s) ("+', '.join(f'{h} {n}' for h,n in sessions['by_harness'].items())+f"), {sessions['notes_saved']} note(s) saved." if sessions['count'] else 'No sessions.')
    lines+=['','## Open work','']
    lines+=[f"- {t['title']}: {t['open']} open item(s)" for t in work['top']] or ['Nothing open.']
    lines+=['','## Due for review','']
    lines.append(f"{review['due']} note(s): "+', '.join(f'{k} {n}' for k,n in review['by_kind'].items())+'.' if review['due'] else 'Nothing due.')
    return '\n'.join(lines)+'\n'

def recap(root,domains,days=7,project=None,now=None):
    from .activity import log
    from .notes import open_work
    from .worklist import worklist
    root=Path(root);domains=list(dict.fromkeys(domains));high=(now or datetime.now(timezone.utc)).astimezone(timezone.utc);low=high-timedelta(days=days)
    since,until=low.isoformat(),high.isoformat()
    rows=[r for r in log(root,None,project,since,until,limit=10**9)['rows'] if r['domain'] in domains]
    notes=[r for r in rows if r['event']!='session'];sessions=_sessions([r for r in rows if r['event']=='session'])
    counts={}
    for r in notes:counts[r['event']]=counts.get(r['event'],0)+1
    changes=_changes(notes);maint=_maintenance(root,domains,low,high)
    tags=[f'project/{project}'] if project else []
    found=open_work(domains,root,tags)
    work={'notes':len(found),'items':sum(len(i) for _,_,i in found),'top':[{'ref':f"{d}:{n['id']}",'title':n['title'],'open':len(i)} for d,n,i in sorted(found,key=lambda f:-len(f[2]))[:TOP_OPEN]]}   # the most open items first; ties stay newest first
    wl=worklist(root,domains,cheap=True,tags=tags or None);review={'due':wl['total'],'by_kind':{k:v for k,v in wl['counts'].items() if v}}
    return {'ok':True,'since':since,'until':until,'days':days,'domains':domains,'counts':counts,'changes':changes,'maintenance':maint,'sessions':sessions,'open_work':work,'review':review,
            'markdown':_markdown(since,until,counts,changes,maint,sessions,work,review)}
