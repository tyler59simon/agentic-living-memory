"""`<private>…</private>` spans in the user's prompt: text the agent may use but must not save.

Only salted hashes of word shingles are kept in the session's turn state, never the text, so a capture or
update with --session can be refused when it copies a private span. Cooperative, like the rest of the
governance: vault edits, and captures without --session, are not checked."""
from __future__ import annotations
import hashlib,hmac,re,secrets

SPAN=re.compile(r'<private>(.*?)(?:</private>|\Z)',re.S|re.I)   # an unclosed tag runs to the end of the text
SHINGLE=8
MAX_SHINGLES=500
MIN_PER_SPAN=16   # thinning keeps at least this many evenly spaced shingles of each span
NOTICE='Content inside <private> must not be saved to memory.'

def _words(text):return re.findall(r'[^\W_]+',str(text).casefold())

def _hash(salt,words):return hmac.new(bytes.fromhex(salt),' '.join(words).encode(),hashlib.sha256).hexdigest()[:16]

def spans(prompt):return [s for s in SPAN.findall(prompt or '') if _words(s)]

def _spans(private):
    """Stored spans, each a list of (size, hash); an old flat `shingles` list counts as one legacy span."""
    found=[[tuple(s) for s in span] for span in private.get('spans') or []]
    if private.get('shingles'):found.insert(0,[tuple(s) for s in private['shingles']])
    return found

def _thin(span,keep):
    if len(span)<=keep:return span
    return [span[i*len(span)//keep] for i in range(keep)]

def remember(state,prompt):
    """Add the prompt's private spans to the session state; True if it had any."""
    found=spans(prompt)
    if not found:return False
    private=state.get('private') or {};salt=private.get('salt') or secrets.token_hex(16)
    stored=_spans(private)
    for span in found:
        words=_words(span);size=min(SHINGLE,len(words))
        stored.append(list(dict.fromkeys((size,_hash(salt,words[i:i+size])) for i in range(len(words)-size+1))))
    for i in range(len(stored)):   # oldest first, down to the floor, until the total fits
        excess=sum(map(len,stored))-MAX_SHINGLES
        if excess<=0:break
        stored[i]=_thin(stored[i],max(MIN_PER_SPAN,len(stored[i])-excess))
    state['private']={'salt':salt,'spans':[[list(s) for s in span] for span in stored]}
    return True

def overlap(state,fields):
    """The first field (label -> text) that repeats a private span, else None."""
    private=(state or {}).get('private') or {}
    salt=private.get('salt');wanted={}
    for span in _spans(private):
        for size,value in span:wanted.setdefault(int(size),set()).add(value)
    if not salt or not wanted:return None
    for label,text in fields:
        words=_words(text)
        for size,values in wanted.items():
            if any(_hash(salt,words[i:i+size]) in values for i in range(len(words)-size+1)):return label
    return None

def note_fields(values):
    """(label, text) pairs of a capture or update: title, context and each body section by heading."""
    from .note_format import parts,LABELS
    fields=[(k,values[k]) for k in ('title','context') if isinstance(values.get(k),str)]
    if isinstance(values.get('body'),str):
        for key,heading,text in parts(values['body']):
            label=LABELS.get(key) or heading or 'body'
            fields.append((label,text))
    return fields

def check(state,values):
    label=overlap(state,note_fields(values))
    if label:raise ValueError(f'private_content conflict: {label} repeats text the user marked <private>; leave it out of memory')
