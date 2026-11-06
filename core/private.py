"""`<private>…</private>` spans in the user's prompt: text the agent may use but must not save.

Only salted hashes of word shingles are kept in the session's turn state, never the text, so a capture or
update with --session can be refused when it copies a private span. Cooperative, like the rest of the
governance: vault edits, and captures without --session, are not checked."""
from __future__ import annotations
import hashlib,hmac,re,secrets

SPAN=re.compile(r'<private>(.*?)</private>',re.S|re.I)   # an unclosed tag marks nothing
SHINGLE=8
MAX_SHINGLES=500
NOTICE='Content inside <private> must not be saved to memory.'

def _words(text):return re.findall(r'[^\W_]+',str(text).casefold())

def _hash(salt,words):return hmac.new(bytes.fromhex(salt),' '.join(words).encode(),hashlib.sha256).hexdigest()[:16]

def spans(prompt):return [s for s in SPAN.findall(prompt or '') if _words(s)]

def remember(state,prompt):
    """Add the prompt's private spans to the session state; True if it had any."""
    found=spans(prompt)
    if not found:return False
    private=dict(state.get('private') or {});salt=private.get('salt') or secrets.token_hex(16)
    shingles=[tuple(s) for s in private.get('shingles',[])]
    for span in found:
        words=_words(span);size=min(SHINGLE,len(words))
        shingles+=[(size,_hash(salt,words[i:i+size])) for i in range(len(words)-size+1)]
    state['private']={'salt':salt,'shingles':[list(s) for s in dict.fromkeys(shingles)][-MAX_SHINGLES:]}
    return True

def overlap(state,fields):
    """The first field (label -> text) that repeats a private span, else None."""
    private=(state or {}).get('private') or {}
    salt=private.get('salt');wanted={}
    for size,value in private.get('shingles',[]):wanted.setdefault(int(size),set()).add(value)
    if not salt or not wanted:return None
    for label,text in fields:
        words=_words(text)
        for size,values in wanted.items():
            if any(_hash(salt,words[i:i+size]) in values for i in range(len(words)-size+1)):return label
    return None

def note_fields(values):
    """(label, text) pairs of a capture or update: title, context and each body section by heading."""
    from .note_format import split,format_body,HEADINGS
    fields=[(k,values[k]) for k in ('title','context') if isinstance(values.get(k),str)]
    if isinstance(values.get('body'),str):
        for key,heading,text in split(format_body(values['body'])):
            label=HEADINGS.get(key) or heading or ('Important Note' if key=='important' else 'body')
            fields.append((label,text))
    return fields

def check(state,values):
    label=overlap(state,note_fields(values))
    if label:raise ValueError(f'private_content conflict: {label} repeats text the user marked <private>; leave it out of memory')
