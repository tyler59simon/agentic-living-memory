"""Measure how often the capture-time related check would fire, before a Stop reminder is built on it.

For every active decision, policy and preference note, runs the related check as if that note were being captured now (widened to the
note's parent, child and global domains), skipping the note itself and the near-title notes capture already lists as `similar`. Two bars
are reported: `listing` (everything the check returns, top 3) and `owed` (rank 1, a strong match, and a second signal: title score
>= 0.5 or a meaning match well above the model's threshold). It never writes the store: use --copy on a live store.

    py -3.15 scripts/eval_related.py --root STORE [--copy [--keep]] [--pair NEW OLD]... [--labels FILE] [--sample 20] [--days 30] [--json]

--pair NEW OLD (repeatable; refs or exact titles) reports whether the check finds OLD for NEW; a retired OLD is restored to active in the
copy only (needs --copy). --labels FILE is JSON {"NEWREF|OLDREF": true|false}; precision is counted over labelled pairs.
"""
from __future__ import annotations
import argparse,json,shutil,sys,tempfile
from datetime import datetime,timedelta,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.common import atomic_write
from core.notes import RELATED_GROUPS,_related_hits,related_domains,near_titles,resolve_ref,domain_folders
from core.repository import inventory,serialize,reconcile

COPY_SKIP=('*.lock','.operations','backups','logs','.views')   # journals, snapshots and logs a related check never reads
SCANNED=tuple(RELATED_GROUPS)
BARS=('listing','owed')

def _ratio(a,b):return round(a/b,3) if b else None

def _ref(domain,note_id):return f'{domain}:{note_id}'

def _find(value,root):
    """(domain, note) for a ref, ID prefix or exact title, retired notes included."""
    try:
        domain,note_id=resolve_ref(value,None,root)
        return domain,next(n for n in inventory(domain,root,retired=True) if n['id']==note_id)
    except (ValueError,StopIteration):pass
    wanted=str(value).strip().casefold()
    found=[(d,n) for d in domain_folders(root) for n in inventory(d,root,retired=True) if n['title'].strip().casefold()==wanted or (len(wanted)>=8 and n['id'].startswith(wanted))]
    if len(found)!=1:raise ValueError(f'{len(found)} notes match {value!r}')
    return found[0]

def _restore(domain,note,root):
    """Make a retired note active again by rewriting its Markdown (the copy only), then reindex its domain."""
    if note.get('status','active')=='active':return
    source=Path(note['path']);target=Path(root)/'domains'/domain/'notes'/source.name
    fresh={k:v for k,v in note.items() if k not in ('superseded_by','last_retirement','path')};fresh['status']='active'
    atomic_write(target,serialize(fresh));source.unlink();reconcile(domain,root,sync_overlay=False)

def _signals(hit):return {k:hit.get(k) for k in ('rank','gate','coverage','similarity','pair_score','strong','owed_bar')}

def _scan(root):
    """Every active decision-group note run through the widened related check: (scanned notes, rows of new/old pairs)."""
    rows=[];scanned=[]
    for domain in domain_folders(root):
        active=[n for n in inventory(domain,root) if n.get('status','active')=='active']
        near_pool=[n for n in active if not n.get('sources')]
        widened=related_domains(domain,root)
        for note in active:
            if note.get('type') not in SCANNED:continue
            skip={note['id']}|{n['id'] for n in near_titles(note['title'],[n for n in near_pool if n['id']!=note['id']],note_type=note.get('type'))}
            scanned.append({'ref':_ref(domain,note['id']),'created':note.get('created')})
            for h in _related_hits(note,domain,root,skip,widened)[:3]:
                rows.append({'new':{'ref':_ref(domain,note['id']),'title':note['title'],'type':note.get('type'),'created':note.get('created')},
                             'old':{'ref':_ref(h['domain'],h['id']),'title':h['title'],'type':h.get('type')},
                             'same_domain':h['domain']==domain,'signals':_signals(h)})
    return scanned,rows

def _counts(scanned,rows,bar):
    pick=[r for r in rows if bar=='listing' or r['signals']['owed_bar']]
    pairs={(r['new']['ref'],r['old']['ref']) for r in pick}
    same={(r['new']['ref'],r['old']['ref']) for r in pick if r['same_domain']}
    firing={r['new']['ref'] for r in pick}
    return {'notes_scanned':len(scanned),'notes_firing':len(firing),'fire_rate':_ratio(len(firing),len(scanned)),
            'unique_pairs':len(pairs),'same_domain_pairs':len(same),'cross_domain_pairs':len(pairs-same)}

def _recent(scanned,rows,bar,days):
    cut=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    ok={s['ref'] for s in scanned if str(s.get('created') or '')>=cut}
    return _counts([s for s in scanned if s['ref'] in ok],[r for r in rows if r['new']['ref'] in ok],bar)

def evaluate(root,pairs=(),labels=None,sample=20,days=30):
    root=Path(root);wanted=[]
    for new,old in pairs:
        (nd,nn),(od,on)=_find(new,root),_find(old,root)
        wanted.append((_ref(nd,nn['id']),_ref(od,on['id']),nn['title'],on['title']))
    scanned,rows=_scan(root)
    report={'scanned_types':list(SCANNED),'days':days}
    for bar in BARS:report[bar]={**_counts(scanned,rows,bar),'recent':_recent(scanned,rows,bar,days)}
    if wanted:
        report['pairs']=[]
        for nref,oref,ntitle,otitle in wanted:
            hit=next((r for r in rows if r['new']['ref']==nref and r['old']['ref']==oref),None)
            report['pairs'].append({'new':nref,'old':oref,'new_title':ntitle,'old_title':otitle,'found_listing':hit is not None,
                                    'found_owed':bool(hit and hit['signals']['owed_bar']),'signals':hit['signals'] if hit else None})
    if labels is not None:
        for bar in BARS:
            pick={f"{r['new']['ref']}|{r['old']['ref']}" for r in rows if bar=='listing' or r['signals']['owed_bar']}
            known=[k for k in pick if k in labels]
            report[bar].update(labelled=len(known),unlabelled=len(pick)-len(known),precision=_ratio(sum(1 for k in known if labels[k]),len(known)))
    ordered=[r for r in rows if r['signals']['owed_bar']]+[r for r in rows if not r['signals']['owed_bar']]
    seen=set();picked=[]
    for r in ordered:
        key=f"{r['new']['ref']}|{r['old']['ref']}"
        if key in seen:continue
        seen.add(key)
        picked.append({'key':key,'bar':'owed' if r['signals']['owed_bar'] else 'listing','new':r['new'],'old':r['old'],'same_domain':r['same_domain'],'signals':r['signals']})
    report['sample']=picked[:sample]
    return report

def run(root=None,pairs=(),labels=None,sample=20,days=30,copy=False,keep=False):
    from core.storage import resolve_root
    from core import semantic
    store=resolve_root(root)[0]
    if not copy:
        if pairs:raise ValueError('--pair restores retired notes: it needs --copy so the store is never written')
        return evaluate(store,pairs,labels,sample,days)
    folder=Path(tempfile.mkdtemp(prefix='living-memory-eval-related-'))
    try:
        copy_root=folder/'store'
        shutil.copytree(store,copy_root,ignore=shutil.ignore_patterns(*COPY_SKIP))
        try:
            for new,old in pairs:
                domain,note=_find(old,copy_root);_restore(domain,note,copy_root)
            return {**evaluate(copy_root,pairs,labels,sample,days),'copy':str(copy_root) if keep else None}
        finally:semantic._unload()   # Windows cannot delete a mapped model file
    finally:
        if not keep:shutil.rmtree(folder,ignore_errors=True)

def _table(report):
    lines=[f"Related check over active {', '.join(report['scanned_types'])} notes (widened to parent, child and global domains)"]
    for bar in BARS:
        c=report[bar];r=c['recent']
        lines+=[f"{bar}: {c['notes_firing']}/{c['notes_scanned']} notes fire (rate {c['fire_rate']}); {c['unique_pairs']} pairs ({c['same_domain_pairs']} same domain, {c['cross_domain_pairs']} cross)",
                f"  last {report['days']} days: {r['notes_firing']}/{r['notes_scanned']} (rate {r['fire_rate']}); {r['unique_pairs']} pairs ({r['same_domain_pairs']} same, {r['cross_domain_pairs']} cross)"]
        if 'precision' in c:lines.append(f"  precision {c['precision']} over {c['labelled']} labelled; {c['unlabelled']} unlabelled")
    for p in report.get('pairs',[]):
        lines.append(f"pair {p['new']} -> {p['old']}: listing {p['found_listing']}, owed {p['found_owed']}, signals {json.dumps(p['signals'])}")
    if report['sample']:lines.append('Sample (label each key true or false in a labels file):')
    for s in report['sample']:
        lines.append(f"  [{s['bar']}] {s['key']}\n    {s['new']['title']} ({s['new']['type']})\n    -> {s['old']['title']} ({s['old']['type']}) {json.dumps(s['signals'])}")
    return '\n'.join(lines)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--root');parser.add_argument('--json',action='store_true')
    parser.add_argument('--copy',action='store_true',help='Run in a temporary copy of the store, which is never written')
    parser.add_argument('--keep',action='store_true',help='With --copy: keep the copy and name it in the report')
    parser.add_argument('--pair',nargs=2,action='append',metavar=('NEW','OLD'),default=[],help='A pair the check should find (refs or exact titles); a retired OLD is restored in the copy')
    parser.add_argument('--labels',help='JSON file {"NEWREF|OLDREF": true|false}')
    parser.add_argument('--sample',type=int,default=20);parser.add_argument('--days',type=int,default=30)
    args=parser.parse_args(argv)
    if args.keep and not args.copy:parser.error('--keep needs --copy')
    if args.pair and not args.copy:parser.error('--pair needs --copy')
    import os
    os.environ.setdefault('LIVING_MEMORY_NO_BACKGROUND','1')
    labels=json.loads(Path(args.labels).expanduser().read_text(encoding='utf-8-sig')) if args.labels else None
    report=run(args.root,[tuple(p) for p in args.pair],labels,args.sample,args.days,args.copy,args.keep)
    if args.copy and report.get('copy'):print(f"Store copy kept: {report['copy']}",file=sys.stderr)
    print(json.dumps(report,indent=2,ensure_ascii=False) if args.json else _table(report))

if __name__=='__main__':main()
