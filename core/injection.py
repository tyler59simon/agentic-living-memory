"""The injection controller: how much of a retrieved note the prompt hook delivers: nothing, a pointer, a section as evidence, or the section in full.

Retrieval (search.py) decides which notes are plausible; the relevance gate (runtime.relevance_gate) decides which
are relevant; this module decides how much of each to inject, and whether this session already has it.

  abstain   nothing passed the gate, or the prompt is an acknowledgment ("thanks", "go ahead")
  pointer   the note is relevant as a whole: title, reference and a one-line snippet (the line format before units)
  evidence  one section answers the prompt: its heading, file lines and the opening of its text, at most EVIDENCE chars
  expanded  the prompt asks for detail ("all the alternatives", "the full steps"): the section's text, at most EXPANDED

Deterministic: regular expressions over the prompt, the gate's own signals and each unit's similarity and word overlap.
No model is called and no prompt text is stored; session state keeps note refs, unit ids and levels only.
"""
from __future__ import annotations
import re

EVIDENCE=300
SUMMARY=140   # the summary opening that leads an evidence line
EXPANDED=1200
LEVELS={'pointer':1,'evidence':2,'expanded':3}
# The whole prompt-hook context, in characters of the final string. Matches Codex's additionalContextLimit for this
# hook. The header, maps and brief, and governance notices are never cut for it; note hits get the room they leave
# (the first hit always), and source excerpts and pre-compaction recall are added only when they fit.
PROMPT_CAP=4000
HEADING='Untrusted historical memory; verify current instructions and files:'
TOPIC_REFS=3

# Whole-prompt acknowledgments: no question, so no note retrieval (maps and briefs are unaffected).
ACK=re.compile(r"^\W*(?:(?:yes|yep|yeah|yup|ok|okay|k|sure|thanks|thank you|thx|ty|cool|great|nice|perfect|lgtm|got it|sounds good|"
               r"looks good|go ahead|do it|go for it|proceed|continue|carry on|done|right|agreed|makes sense|please do)\W*)+$",re.I)
# A short follow-up that names no topic of its own refers to the previous one.
FOLLOW_UP=(
    ('rationale',re.compile(r"\b(?:why|how come|what(?:'s| is| was) the (?:reason|rationale)|reasons?\b|rationale)",re.I)),
    ('alternative',re.compile(r"\b(?:alternatives?|rejected|other (?:approach|approaches|option|options)|instead|what else did we (?:consider|try))\b",re.I)),
    ('overview',re.compile(r"\b(?:what did we (?:decide|choose|pick|go with|settle on)|still (?:apply|applies|hold|holds|true|valid|current|the case)|is that still|does (?:that|it|this) still)\b",re.I)),
    ('procedure',re.compile(r"\b(?:how do (?:i|we) (?:do|run|use) (?:it|that|this)|what are the steps|steps\b)",re.I)),
)
FOLLOW_UP_MAX_TERMS=2
FOLLOW_UP_MAX_CHARS=120
EXPAND=re.compile(r"\b(?:in (?:full|detail)|full (?:note|details?|procedure|steps|history|text)|all (?:of )?(?:the )?(?:details|alternatives|steps|options|reasons)|"
                  r"complete (?:procedure|steps|list|history)|step[- ]by[- ]step|everything (?:about|on)|show me the (?:whole|full|entire)|exact (?:steps|wording|command))\b",re.I)
# An explicit request to see a note again; a bare "again" ("the cache again") is not one.
RECALL=re.compile(r"\b(?:remind me|recall|repeat (?:it|that|the)|re-?read|show (?:me )?(?:it|that|this|the note) again|once more)\b",re.I)
# Section roles a polarity label must travel with: a rejected option is never to be read as the choice.
LABELS={'alternative':'rejected or considered options, not the decision','constraint':'caveat'}

def is_ack(prompt):return bool(ACK.match(prompt or ''))

def follow_up(prompt,terms):
    """The section role a short, topic-less follow-up asks for ('rationale', 'alternative', 'overview', 'procedure'),
    else None. terms: the prompt's informative query terms."""
    text=(prompt or '').strip()
    if not text or len(text)>FOLLOW_UP_MAX_CHARS or len(terms)>FOLLOW_UP_MAX_TERMS:return None
    for role,pattern in FOLLOW_UP:
        if pattern.search(text):return role
    return None

def wants_expanded(prompt):return bool(EXPAND.search(prompt or ''))
def wants_again(prompt):return bool(RECALL.search(prompt or ''))

# Words that name the kind of section a question is about, anywhere in a prompt: "why" asks for the reasons, "what did we
# turn down" for the alternatives. A note with a section of that role delivers it; the cue never makes a note relevant.
ROLE_CUES=(
    (('rationale','cause'),re.compile(r"\b(?:why|reasons?|rationale|root cause|caused?)\b",re.I)),
    (('alternative',),re.compile(r"\b(?:alternatives?|rejected|turn(?:ed)? down|ruled out|other options?|instead of|options? (?:we )?(?:considered|rejected))\b",re.I)),
    (('fix',),re.compile(r"\b(?:fix|fixed|fixes|resolved?|workaround)\b",re.I)),
    (('procedure',),re.compile(r"\b(?:how (?:do|can|should|would) (?:i|we)|steps?|roll ?back|procedure|runbook)\b",re.I)),
)
# Within one note, a section's word overlap with the prompt adds this much to its similarity: two sections that share
# the note's title differ mostly in a few words ("failed fetch" in Retries, "fetcher" in Components).
WORD_BONUS=0.05

def roles_of(prompt):
    return [r for roles,pattern in ROLE_CUES if pattern.search(prompt or '') for r in roles]

def _overlap(prompt_stems,unit,title=''):
    from .search import stems
    return len(prompt_stems&stems(title+' '+unit.get('heading','')+' '+unit.get('excerpt','')))

def choose(hit,prompt_stems,near,role=None,prompt=''):
    """The unit to deliver for a hit, or None for the note as a whole (a pointer).

    A section role the prompt asks for (a follow-up's role, else a cue such as "why" or "turned down") picks among the
    note's sections of that role. Otherwise a section is chosen over the overview when it answers better: by meaning
    when its similarity clears the model's threshold and its score (similarity plus WORD_BONUS per shared prompt word)
    beats the overview's; without a model, when it carries at least two prompt words and more than the overview (with
    the title) does. Ties go to the earlier section. Deterministic."""
    units=hit.get('units') or []
    if not units or role=='overview':return None
    sections=[u for u in units if u.get('role')!='overview']
    if not sections:return None
    overview=next((u for u in units if u.get('role')=='overview'),{})
    title=hit.get('title','')
    score=lambda u,t='':(u.get('similarity') or 0.0)+WORD_BONUS*_overlap(prompt_stems,u,t)
    order=lambda u:(score(u),-sections.index(u))
    wanted=[role] if role else roles_of(prompt)
    matched=[u for u in sections if u.get('role') in wanted]
    if matched:return max(matched,key=order)
    best=max(sections,key=order)
    if near is not None and best.get('similarity') is not None:
        if best['similarity']>=near and score(best)>score(overview,title):return best
    words=_overlap(prompt_stems,best)
    if words>=2 and words>_overlap(prompt_stems,overview,title):return best
    return None

def level_of(unit,prompt):
    if wants_expanded(prompt):return 'expanded'
    return 'evidence' if unit else 'pointer'

def unit_key(unit):return unit['unit'] if unit else 'overview'

def _cut(text,width):
    """At most width characters, ended at a sentence (or failing that a word) so no fact is cut mid-clause."""
    text=' '.join(str(text).split())
    if len(text)<=width:return text
    window=text[:width]
    end=max(window.rfind('. '),window.rfind('; '),window.rfind('! '),window.rfind('? '))
    if end>=width//2:return window[:end+1]+' …'
    space=window.rfind(' ')
    return window[:space if space>0 else width]+' …'

def line(hit,unit,level,width):
    """One hit as an injected line. A pointer is the line format the hook used before units (runtime.hit_line)."""
    from .runtime import hit_line
    if level=='pointer' or (unit is None and level!='expanded'):return hit_line(hit,width)
    from .notes import short_ref
    stale=f"{hit['stale']} — " if hit.get('stale') else ''
    mark=f"UNCONFIRMED (as of {hit.get('updated','')}): " if hit.get('interim') else ''
    head=f"- [{str(hit.get('type','note')).upper()} {hit.get('updated','')}] {short_ref(hit['domain'],hit['id'])} {mark}{hit['title']}"
    if unit is None:
        body=_cut((next((u for u in hit.get('units') or [] if u.get('role')=='overview'),{}) or {}).get('excerpt') or hit.get('snippet',''),EXPANDED)
        return f"{head} — {stale}{body}"
    label=LABELS.get(unit.get('role'))
    lines=unit.get('lines')
    where=f" (lines {lines[0]}-{lines[1]})" if lines and lines[0] else ''
    text=_cut(unit.get('excerpt',''),EXPANDED if level=='expanded' else EVIDENCE)
    # The summary's opening leads: it is what the note decided or states, which a section's reasons or options qualify.
    summary=_cut((next((u for u in hit.get('units') or [] if u.get('role')=='overview'),{}) or {}).get('excerpt',''),SUMMARY)
    return f"{head} — {stale}{summary+' ' if summary else ''}§ {unit.get('heading') or 'Summary'}{' ['+label+']' if label else ''}{where}: {text}"

def novel(hit,key,level,delivered,shown,again):
    """True unless this session already has this unit of this revision at this level or more. A different section, a
    new revision, a deeper level or an explicit request ("remind me", "again") is always delivered. A note injected
    whole (a map or brief, in shown without a delivery record) counts as delivered in full."""
    from .runtime import revision_key,_unseen
    if again:return True
    ref=f"{hit['domain']}:{hit['id']}";record=delivered.get(ref) if isinstance(delivered,dict) else None
    if not isinstance(record,dict):return _unseen(hit,shown)
    if record.get('rev')!=revision_key(hit):return True
    prior=(record.get('units') or {}).get(key)
    return not prior or LEVELS.get(level,0)>LEVELS.get(prior,0)

def remember(delivered,hit,key,level):
    """Record a delivery in the session's `delivered` map: ref -> {rev, units: {unit key: level}}. Bounded by the caller."""
    from .runtime import revision_key
    ref=f"{hit['domain']}:{hit['id']}";rev=revision_key(hit)
    record=delivered.get(ref) if isinstance(delivered.get(ref),dict) and delivered[ref].get('rev')==rev else {'rev':rev,'units':{}}
    if LEVELS.get(level,0)>LEVELS.get(record['units'].get(key),0):record['units'][key]=level
    delivered.pop(ref,None);delivered[ref]=record

def within(lines,heading,room):
    """How many of these hit lines fit in room characters under their heading line; the first always does, so a
    budget can shorten what is delivered but never silence a relevant note."""
    used=len(heading)+1
    for count,text in enumerate(lines):
        used+=len(text)+1
        if count and used>room:return count
    return len(lines)
