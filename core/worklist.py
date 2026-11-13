"""The review worklist: notes that need a person's or agent's decision, by kind, each note once.

Kinds, in this order: stale (a hashed file or a map's cited file changed), due (review date or age), interim (an unconfirmed
belief left for INTERIM_DAYS), duplicate (a same-type note with a similar title or body), warned (a content check of
notes.QUALITY_CHECKS), noisy (retrieved often, never used). The worklist only proposes: it writes nothing and runs no git
(maps and briefs are judged from the currency cache SessionStart and prompts keep). Archive and merge are plans the user applies."""
from __future__ import annotations
from collections import Counter
from datetime import date,timedelta
from pathlib import Path

KINDS=('stale','due','interim','duplicate','warned','noisy')
INTERIM_DAYS=14
DUPLICATE_TITLE=0.6       # the lower bound of the capture-time `similar` list
STEM_CAP=40               # a word in more notes than this (per domain and type) does not pair notes up
NOISE_DAYS=30

def _ref(note):return f"{note['domain']}:{note['id']}"

def _stamp(note):
    """When the note was last confirmed, comparable with an ISO `updated`: the `checked` timestamp, else the last confirmed day (end of day)."""
    from .note_types import last_confirmed
    if note.get('checked'):return str(note['checked'])
    day=last_confirmed(note)
    return day+'T23:59:59' if day else ''

def _confirmed_since(note,when):
    """True when a confirmation came on or after `when` (an ISO timestamp, a note's last update)."""
    stamp=_stamp(note)
    return bool(stamp and stamp>=str(when or ''))

def _currency(root):
    try:
        from .runtime import _currency_entries
        return _currency_entries(root)
    except Exception:return []

def _stale(pool,root,bindings):
    """{ref: reason} for notes with a changed digest source or hashed file, and maps and briefs whose cited files changed according to the currency cache."""
    from .digests import stale_note
    from .note_types import KEYED_TYPES
    from .maps import last_current,stale_clause
    found={};cache=None
    for note in pool:
        if note.get('sources') or note.get('file_hashes'):
            text=stale_note(note,root,bindings)
            if text:found[_ref(note)]=text
        elif note.get('type') in KEYED_TYPES:
            if cache is None:
                cache={}
                for row in _currency(root):cache[row.get('n')]=row   # oldest first: the newest entry for a note wins
            row=cache.get(_ref(note))
            if row and row['e'].get('rev')==str(note.get('revision') or '') and row.get('u')==last_current(note):
                text=stale_clause(note,row['e'],note['type'])
                if text:found[_ref(note)]=text
    return found

def _due(pool,root,domain):
    from .note_types import review_due,review_order,effective_date
    from .usage import used_refs
    due=review_order([n for n in pool if review_due(n)],used_refs(root),domain)
    found={}
    for n in due:
        review=str(n.get('review_by') or '')[:10]
        found[_ref(n)]=f"review date {review} has passed" if review else f"not updated or confirmed since {effective_date(n)}"
    return list(found.items())

def _interim(pool,today):
    from .notes import is_interim
    from .note_types import effective_date
    found={}
    for n in pool:
        known=effective_date(n)
        if known and known<today-timedelta(days=INTERIM_DAYS) and is_interim(n):
            found[_ref(n)]=f"still an unconfirmed belief, unchanged since {known}: confirm it, record the cause, or say it is still a hypothesis"
    return found

def _warned(pool):
    from .notes import quality_checks
    found={}
    for n in pool:
        messages=quality_checks(n)
        if messages and not _confirmed_since(n,n.get('updated')):
            found[_ref(n)]=messages[0].replace('REF','this note')+(f' (+{len(messages)-1} more)' if len(messages)>1 else '')
    return found

def _noisy(pool,root,domains):
    from . import usage
    notes={_ref(n):n for n in pool}
    return {ref:f"shown {c['inject']} times in {NOISE_DAYS} days and never read, updated or confirmed: a sharper title or summary may help, or confirm it"
            for ref,c in usage.noisy(usage.events(root,NOISE_DAYS),domains) if ref in notes}

SKETCH=4   # smallest shingle hashes kept per body: two notes with 70% of their shingles in common share one almost always

def _duplicates(pool):
    """{ref: pair} for the later note of each similar same-type pair in a domain: a title scoring DUPLICATE_TITLE or more
    (notes.pair_score, so numbered status titles count), or a nearly identical body. Candidates come from inverted indexes (title
    word stems, and a small sketch of each body's shingles), so the scan is not quadratic."""
    from .notes import TitleScorer,pair_score,_title_words,_shingles,NEAR_BODY,NEAR_TITLE
    from .note_types import effective_type,KEYED_TYPES
    found={};by_domain={}
    for n in pool:
        if not n.get('sources') and effective_type(n) not in KEYED_TYPES and n.get('title'):by_domain.setdefault(n['domain'],[]).append(n)
    for domain,notes in by_domain.items():
        scorer=TitleScorer(notes);words=[_title_words(n['title']) for n in notes];shingles=[_shingles(n.get('body','')) for n in notes]
        titles={};sketches={}
        for i,n in enumerate(notes):
            kind=effective_type(n)
            for stem in words[i]:titles.setdefault((kind,stem),[]).append(i)
            for h in sorted(hash(g) for g in shingles[i])[:SKETCH]:sketches.setdefault((kind,h),[]).append(i)
        shared=Counter();same_body=set()
        for posting in titles.values():
            if 1<len(posting)<=STEM_CAP:
                for a in range(len(posting)):
                    for b in range(a+1,len(posting)):shared[(posting[a],posting[b])]+=1
        for posting in sketches.values():
            if 1<len(posting)<=STEM_CAP:
                for a in range(len(posting)):
                    for b in range(a+1,len(posting)):same_body.add((posting[a],posting[b]))
        for i,j in sorted({k for k,c in shared.items() if c>=2 or words[k[0]]==words[k[1]]}|same_body):
            a,b=notes[i],notes[j];kind=effective_type(a)
            score=pair_score(scorer,a['title'],b['title'],kind,kind) if shared.get((i,j)) else 0.0
            x,y=shingles[i],shingles[j];body=len(x&y)/len(x|y) if x and y else 0.0
            if score>=DUPLICATE_TITLE:why=f"title {score:.2f} like";same=score>=NEAR_TITLE and body>=NEAR_BODY
            elif body>=NEAR_BODY:why='body nearly identical to';score=1.0;same=True
            else:continue
            older,newer=(a,b) if str(a.get('updated') or '')<=str(b.get('updated') or '') else (b,a)
            if _confirmed_since(older,newer.get('updated')) or _confirmed_since(newer,newer.get('updated')):continue
            row={'ref':_ref(older),'title':older['title'],'score':round(score,2),'same':bool(same),'reason':f"{why} {_ref(older)} {older['title']}"}
            if _ref(newer) not in found or found[_ref(newer)]['score']<row['score']:found[_ref(newer)]=row
    return found

def _acts(note):
    from .note_format import action_items,ACTION_TYPES
    if note.get('type') in ACTION_TYPES:
        items=action_items(note.get('body',''))
        return bool(items) and all(done for done,_ in items)
    return False

def _action(kinds,note,pair):
    """What a person or agent does first for a note: its first kind decides."""
    first=kinds[0]
    if first=='duplicate':return 'supersede' if pair and pair.get('same') else 'merge-plan'
    if first=='due':return 'resolve' if _acts(note) else 'archive-plan' if note.get('type')=='status' else 'confirm'
    return 'update'   # stale, interim, warned and noisy (a sharper title or summary; or confirm it): never an archive

def worklist(root,domains,kinds=None,limit=100,tags=None,cheap=False,notes=None,today=None):
    """{'items': rows, 'counts': {kind: notes}, 'total': distinct notes} for the active notes of these domains. Each row is one note with all
    its kinds (in KINDS order), a reason, an action (confirm, update, supersede, resolve, merge-plan, archive-plan) and for a duplicate its
    `pair`. kinds limits what is computed; cheap skips the duplicate scan. notes are already loaded active notes carrying their `domain`
    (SessionStart has them); tags keeps notes with all of these tags. Read-only: no git, no usage rows."""
    from .repository import inventory
    from .common import load_bindings
    root=Path(root);today=today or date.today();domains=list(dict.fromkeys(domains))
    wanted=[k for k in KINDS if not kinds or k in kinds]
    if cheap:wanted=[k for k in wanted if k!='duplicate']
    if notes is None:
        notes=[]
        for d in domains:
            try:notes+=[dict(n,domain=d) for n in inventory(d,root)]
            except (OSError,ValueError):continue
    pool=[n for n in notes if n.get('status','active')=='active' and all(t in (n.get('tags') or []) for t in tags or ())]
    by_ref={_ref(n):n for n in pool};found={};order=[]
    def add(kind,ref,reason,**extra):
        if ref not in by_ref:return
        if ref not in found:found[ref]={'kinds':[],'reasons':[],'extra':{}};order.append(ref)
        found[ref]['kinds'].append(kind);found[ref]['reasons'].append(reason);found[ref]['extra'].update(extra)
    if 'stale' in wanted:
        for ref,text in _stale(pool,root,load_bindings(root)).items():add('stale',ref,text,stale=text)
    if 'due' in wanted:
        # One review order over the active domain first, like `review` before: its own notes lead.
        for ref,text in _due(pool,root,domains[0] if len(domains)==1 else None):add('due',ref,text)
    if 'interim' in wanted:
        for ref,text in _interim(pool,today).items():add('interim',ref,text)
    if 'duplicate' in wanted:
        for ref,row in _duplicates(pool).items():add('duplicate',ref,row['reason'],pair={k:row[k] for k in ('ref','title','score')},_same=row['same'])
    if 'warned' in wanted:
        for ref,text in _warned(pool).items():add('warned',ref,text)
    if 'noisy' in wanted:
        for ref,text in _noisy(pool,root,domains).items():add('noisy',ref,text)
    from .note_types import last_confirmed
    rank=lambda ref:min(KINDS.index(k) for k in found[ref]['kinds'])
    # Stable: within a kind the order each computation gave (review order for due) stays.
    order=sorted(order,key=rank);items=[]
    for ref in order:
        n=by_ref[ref];row=found[ref];extra=dict(row['extra']);same=extra.pop('_same',False)
        pair=extra.get('pair');kinds_=[k for k in KINDS if k in row['kinds']]
        items.append({'ref':ref,'title':n['title'],'type':n.get('type'),'updated':str(n.get('updated',''))[:10],'review_by':n.get('review_by'),
                      'confirmed':last_confirmed(n),'revision':n['revision'],'domain':n['domain'],'kinds':kinds_,'reason':'; '.join(row['reasons']),
                      'action':_action(kinds_,n,dict(pair,same=same) if pair else None),**extra})
    counts={k:sum(1 for r in found.values() if k in r['kinds']) for k in wanted}
    return {'items':items[:max(1,limit)],'counts':counts,'total':len(items)}
