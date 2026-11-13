"""Memory units: the sections of a canonical note, cut deterministically for section-level retrieval.

A note's summary is what it is about; the decision's reasons, the rejected alternatives and a runbook's steps sit
in its sections, where a title-plus-summary vector cannot see them. A unit is one such section (or the overview:
title and summary, the same text the note-level vector embeds), with its heading path, a role taken from the
heading, and its line span in the note file. Units are derived from the Markdown and never written back.
"""
from __future__ import annotations
import hashlib,re
from pathlib import Path

# Embedding input limit is 512 word pieces (semantic.MAX_TOKENS); about 1.3 pieces a word, so a section past this many
# words is split at paragraph boundaries rather than silently cut short by the tokenizer.
MAX_WORDS=360
MIN_WORDS=3
EXCERPT_MAX=1600
_HEADING=re.compile(r'^ {0,3}(#{2,4})\s+(.*?)\s*#*\s*$')
_FENCE=re.compile(r'^ {0,3}(`{3,}|~{3,})')
_CALLOUT=re.compile(r'^>\s*(?:ℹ️|ℹ|\[!\w+\])?\s*(?:\*\*\s*Important(?: Note)?\s*:?\s*\*\*\s*:?|Important(?: Note)?\s*:)',re.I)
# Layout sections that carry no retrievable fact of their own.
SKIP=('links','resource links','action items')
ROLES=(
    ('rationale',('why','rationale','reason','reasons','reasoning','motivation')),
    ('alternative',('alternatives considered','alternatives','rejected','rejected alternatives','options considered','not chosen')),
    ('procedure',('steps','procedure','how to','roll back','rollback','if it fails','check','how to check','usage','setup')),
    ('constraint',('when not to use','constraints','constraint','limits','important','caveats','gotchas')),
    ('applicability',('when to use',)),
    ('cause',('cause','root cause')),
    ('fix',('fix','resolution','workaround')),
    ('finding',('findings','results','outcome','outcomes')),
    ('question',('open questions','questions')),
    ('source',('sources','references')),
    ('detail',('details','key details')),
)

def _letters(text):return ' '.join(re.sub(r'[^a-z ]',' ',str(text).casefold()).split())

def role_of(heading):
    """A section's role from its heading ('Why' -> rationale); 'section' when the heading names its content."""
    name=_letters(heading)
    for role,names in ROLES:
        if name in names:return role
    return 'section'

def _plain(text):
    """Readable section text: markup removed outside fences (note_format.plain), fenced code kept exactly as written."""
    from .note_format import plain
    out=[];prose=[];fence=None
    for line in text.split('\n'):
        mark=_FENCE.match(line)
        if fence or mark:
            if prose:out.append(plain('\n'.join(prose)));prose=[]
            out.append(line.rstrip())
            if fence and mark and mark[1][0]==fence[0] and len(mark[1])>=len(fence) and not line.strip().strip(fence[0]):fence=None
            elif not fence and mark:fence=mark[1]
            continue
        prose.append(line)
    if prose:out.append(plain('\n'.join(prose)))
    return '\n'.join(p for p in out if p)

def _body_offset(note):
    """1-based file line of the body's first line, or None when the file cannot be read (spans are then body-relative)."""
    path=note.get('path')
    if not path:return None
    try:raw=Path(path).read_text(encoding='utf-8-sig').replace('\r\n','\n')
    except (OSError,UnicodeDecodeError):return None
    body=note.get('body') or ''
    first=body.split('\n',1)[0]
    match=re.match(r'\A---[ \t]*\n.*?\n---[ \t]*\n',raw,re.S)
    start=match.end() if match else 0
    at=raw.find(first,start) if first else -1
    return raw.count('\n',0,at)+1 if at>=0 else None

def _sections(body):
    """[(heading path, heading, [(line number, text)])] with 1-based body line numbers; fenced code is never a heading.
    Text before the first heading is the preamble (heading None)."""
    out=[[(),None,[]]];fence=None;path=[]
    for number,line in enumerate((body or '').split('\n'),1):
        mark=_FENCE.match(line)
        if fence:
            out[-1][2].append((number,line))
            if mark and mark[1][0]==fence[0] and len(mark[1])>=len(fence) and not line.strip().strip(fence[0]):fence=None
            continue
        if mark:fence=mark[1];out[-1][2].append((number,line));continue
        heading=_HEADING.match(line)
        if heading and heading[2]:
            level=len(heading[1]);path=[p for p in path if p[0]<level]+[(level,heading[2])]
            out.append([tuple(h for _,h in path),heading[2],[(number,line)]]);continue
        out[-1][2].append((number,line))
    return [(p,h,rows) for p,h,rows in out]

def _paragraphs(rows):
    """Rows grouped at blank lines outside fences: a fence, a list or a table stays with its paragraph."""
    groups=[[]];fence=None
    for number,line in rows:
        mark=_FENCE.match(line)
        if fence:
            groups[-1].append((number,line))
            if mark and mark[1][0]==fence[0]:fence=None
            continue
        if mark:fence=mark[1]
        if not line.strip() and not fence:
            if groups[-1]:groups.append([])
            continue
        groups[-1].append((number,line))
    return [g for g in groups if g]

def _words(rows):return sum(len(t.split()) for _,t in rows)

def _split(rows):
    """Pieces of at most MAX_WORDS words, cut only between paragraphs; one paragraph longer than that stays whole
    (its embedding then sees its first 512 word pieces; the excerpt keeps all of it)."""
    pieces=[[]]
    for paragraph in _paragraphs(rows):
        if pieces[-1] and _words(pieces[-1])+_words(paragraph)>MAX_WORDS:pieces.append([])
        pieces[-1].extend(paragraph)
    return [p for p in pieces if p]

def _norm(text):
    """A heading for identity: case, punctuation and spacing ignored, digits kept ("Part 1" is not "Part 2")."""
    return ' '.join(re.sub(r'[^\w ]',' ',str(text).casefold()).split())

def _unit_id(note_id,path,occurrence,part):
    key='\x1f'.join([str(note_id),'\x1e'.join(_norm(h) for h in path),str(occurrence),str(part)])
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]

def _sha(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()

def extract(note):
    """[unit] for one parsed note (repository.parse_text). Each unit: unit_id (stable while its heading path is),
    role, heading (path joined with ' > '), lines [first, last] in the file (body-relative when the file is
    unreadable), text (what is embedded), excerpt (the section's own readable text) and sha (of text)."""
    from .search import _embedding_text
    note_id=note.get('id') or note.get('path');title=note.get('title','')
    offset=_body_offset(note);shift=(offset-1) if offset else 0
    units=[];seen=set();occurrences={}
    overview=_embedding_text(note)
    sections=_sections(note.get('body',''))
    first=sections[0][2] if sections else []
    units.append({'unit_id':_unit_id(note_id,('',),0,0),'role':'overview','heading':'',
                  'lines':[first[0][0]+shift,first[-1][0]+shift] if first else None,'text':overview,
                  'excerpt':_plain('\n'.join(t for _,t in first))[:EXCERPT_MAX],'sha':_sha(overview)})
    seen.add(_sha(units[0]['excerpt']))
    # A callout in the preamble ("> **Important:** ...") is a caveat the reader must see: a unit of its own.
    callout=[r for r in first if _CALLOUT.match(r[1].strip())]
    if callout:
        start=first.index(callout[0]);rows=[callout[0]]+[r for r in first[start+1:] if r[1].lstrip().startswith('>')]
        text=_plain('\n'.join(t for _,t in rows))
        if len(text.split())>=MIN_WORDS:
            embedded=title+' > Important\n'+text
            units.append({'unit_id':_unit_id(note_id,('important',),0,0),'role':'constraint','heading':'Important','lines':[rows[0][0]+shift,rows[-1][0]+shift],
                          'text':embedded,'excerpt':text[:EXCERPT_MAX],'sha':_sha(embedded)})
    ids={u['unit_id'] for u in units}
    for path,heading,rows in sections[1:]:
        if _letters(heading) in SKIP:continue
        key=tuple(_norm(h) for h in path);occurrence=occurrences.get(key,0);occurrences[key]=occurrence+1
        content=rows[1:]
        if _words(content)<MIN_WORDS:continue
        for part,piece in enumerate(_split(content)):
            text=_plain('\n'.join(t for _,t in piece))
            if len(text.split())<MIN_WORDS or _sha(text) in seen:continue   # empty, or the same words as another unit
            seen.add(_sha(text))
            label=' > '.join(path);embedded=f'{title} > {label}\n{text}'
            unit_id=_unit_id(note_id,path,occurrence,part)
            while unit_id in ids:unit_id=_unit_id(note_id,path+('#',),occurrence,unit_id)   # a section headed like the callout unit
            ids.add(unit_id)
            units.append({'unit_id':unit_id,'role':role_of(heading),'heading':label,
                          'lines':[(rows[0][0] if part==0 else piece[0][0])+shift,piece[-1][0]+shift],'text':embedded,
                          'excerpt':text[:EXCERPT_MAX],'sha':_sha(embedded)})
    return units
