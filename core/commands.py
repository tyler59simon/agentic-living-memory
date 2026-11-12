"""Shell commands a note warns about, and the notes to show when an agent is about to run one.

A note's commands (inline code spans and shell fences in its title, summary and details, plus the optional `commands`
property) are derived when the index is built (search.note_commands). PreToolUse compares what the agent is about to run
with them by signature: wrapper, program, leading subcommands and flags, so `cmd //c mvnw.cmd -q -B verify`, `cmd /c mvnw.cmd`
and `./mvnw verify` all name the program `mvnw`. A regex over command text is a hint, never a guard.
"""
from __future__ import annotations
import json,re,sqlite3
from pathlib import Path
from typing import NamedTuple
from . import search

MAX_COMMANDS=10
MAX_CHARS=200
KNOWN_TOOLS=frozenset('mvn mvnw gradle gradlew npm npx yarn pnpm pip python py node cargo go make docker kubectl git dotnet java javac pytest terraform helm az aws gcloud psql sqlite3 curl'.split())
# Common words as programs: a note about `git push --force` says nothing about `git status`, so these need a matching subcommand.
GENERIC=frozenset('git python py node npm pip java dotnet docker kubectl curl'.split())
INTERPRETERS=frozenset('python node ruby perl php deno bun'.split())
INLINE=frozenset('-c -e -p --eval --print'.split())   # code on the command line instead of a script
SHELL_LANGS=frozenset('sh bash shell powershell pwsh cmd console zsh bat batch'.split())
WRAPPERS=frozenset('cmd bash sh zsh dash ksh powershell pwsh wsl'.split())
EXTENSIONS=('.cmd','.bat','.exe','.sh','.ps1','.com')
PREFIXES=frozenset('sudo time nohup exec command'.split())
NEVER={'living_memory'}   # the memory script itself: runtime already classifies it
_TOKEN=re.compile(r'''(?:[^\s"']|"[^"]*"|'[^']*')+''')
_ENV=re.compile(r'[A-Za-z_]\w*=')
_WORD=re.compile(r'[A-Za-z][\w:.-]*')
_PYTHON=re.compile(r'(?:python[\d.]*w?|py[\d.]*)\Z')
_PIP=re.compile(r'pip[\d.]*\Z')
_FENCE=re.compile(r'(?ms)^[ \t]*(`{3,}|~{3,})[ \t]*([\w+-]*)[^\n]*\n(.*?)^[ \t]*\1[`~]*[ \t]*$')
_SPAN=re.compile(r'`([^`\n]+)`')
_PROMPT=re.compile(r'\A(?:PS [^>\n]*>|[$>#])\s+')

class Sig(NamedTuple):
    wrapper: str|None
    program: str
    subs: tuple
    flags: frozenset

def _unquote(token):
    return token[1:-1] if len(token)>=2 and token[0]==token[-1] and token[0] in '"\'' else token

def _split(text):
    """The commands of a line: split on && || ; | and newlines outside quotes."""
    parts=[];buf=[];quote='';i=0
    while i<len(text):
        c=text[i]
        if quote:
            buf.append(c);quote='' if c==quote else quote
        elif c in '"\'':quote=c;buf.append(c)
        elif c in ';|\n\r' or text.startswith('&&',i):
            if c=='&' or text.startswith('||',i):i+=1
            parts.append(''.join(buf));buf=[]
        else:buf.append(c)
        i+=1
    return [p for p in parts+[''.join(buf)] if p.strip()]

def _program(token):
    base=_unquote(token).replace('\\','/').rstrip('/').rsplit('/',1)[-1].lower()
    for ext in EXTENSIONS:
        if base.endswith(ext) and len(base)>len(ext):base=base[:-len(ext)];break
    return 'python' if _PYTHON.match(base) else 'pip' if _PIP.match(base) else base

def _inner(program,rest):
    """The command text a wrapper runs, or None when it runs none (a bare `cmd` opens a shell)."""
    low=[_unquote(t).lower() for t in rest]
    if program=='cmd':
        for i,t in enumerate(low):
            if t in {'/c','//c','/k','//k'}:tail=rest[i+1:];break
            if not t.startswith('/'):return None
        else:return None
    elif program=='powershell' or program=='pwsh':
        valued={'-executionpolicy','-ep','-workingdirectory','-wd','-version','-inputformat','-outputformat','-windowstyle','-configurationname'}
        i=0
        while i<len(low) and low[i].startswith('-'):
            if low[i] in {'-c','-command','-file','-f'}:break
            i+=2 if low[i] in valued else 1
        if i>=len(low):return None
        tail=rest[i+1:] if low[i] in {'-c','-command','-file','-f'} else rest[i:]
    elif program=='wsl':
        i=0
        while i<len(low) and low[i].startswith('-'):i+=2 if low[i] in {'-d','--distribution','-u','--user','--cd'} else 1
        tail=rest[i:]
    else:   # bash, sh, ...: `-c STRING`
        for i,t in enumerate(low):
            if re.fullmatch(r'-[a-z]*c[a-z]*',t):tail=rest[i+1:i+2];break
        else:return None
    if not tail:return None
    return _unquote(tail[0]) if len(tail)==1 else ' '.join(tail)

def _strip(tokens):
    while tokens:
        token=_unquote(tokens[0])
        if token=='&' or _ENV.match(token) or token.lower() in PREFIXES:tokens=tokens[1:]
        elif token.startswith('&') and len(token)>1:tokens=[token[1:]]+tokens[1:]
        elif _PROMPT.match(token+' '):tokens=tokens[1:]
        else:break
    return tokens

def _sig(wrapper,tokens):
    program=_program(tokens[0]);args=[_unquote(t) for t in tokens[1:]]
    if program=='python' and '-m' in args[:-1]:
        i=args.index('-m');program=args[i+1].lower();args=args[i+2:]
    elif program in {'npx','pnpx','bunx'}:
        rest=[a for a in args if not a.startswith('-')]
        if not rest:return None
        i=args.index(rest[0]);program=_program(rest[0]);args=args[i+1:]
    if not program or program[0] in '-$':return None
    subs=[];flags=set()
    # An interpreter's script is its discriminating operand even as a path: `python scripts/build.py` is not `python scripts/other.py`.
    script=next((i for i,a in enumerate(args) if not a.startswith('-')),None) if program in INTERPRETERS and not INLINE.intersection(args) else None
    for i,a in enumerate(args):
        if a.startswith('-') and len(a)>1:flags.add(a.split('=',1)[0].lower())
        elif i==script:subs.append(a.replace('\\','/').removeprefix('./').lower())
        elif len(subs)<2 and _WORD.fullmatch(a):subs.append(a.lower())
    return Sig(wrapper,program,tuple(subs),frozenset(flags))

def signature(cmd,_wrapper=None,_depth=0):
    """[Sig] for the commands in a command line: one per command in a chain, wrappers unwrapped (the outermost is recorded)."""
    found=[]
    for segment in _split(cmd or ''):
        tokens=_strip(_TOKEN.findall(segment))
        if not tokens:continue
        program=_program(tokens[0])
        if program in WRAPPERS:
            inner=_inner(program,tokens[1:]) if _depth<4 else None
            if inner:found+=signature(inner,_wrapper or program,_depth+1)
            continue
        sig=_sig(_wrapper,tokens)
        if sig:found.append(sig)
    return found

def _is_command(text):
    """A code span (or shell line) reads as a command: a wrapper, a script, or a known tool with arguments."""
    for segment in _split(text):
        tokens=_strip(_TOKEN.findall(segment))
        if not tokens:continue
        first=_unquote(tokens[0]);program=_program(first)
        if program in WRAPPERS:
            if _inner(program,tokens[1:]):return True
        elif first.startswith(('./','.\\')) and (not _program(first).count('.') or first.lower().endswith(EXTENSIONS)):return True
        elif first.lower().endswith(EXTENSIONS) or (program in KNOWN_TOOLS and len(tokens)>1 and not first.startswith('-')):return True
    return False

def _lines(block):
    """The commands of a shell block: one per line, a trailing backslash joins the next, comments and prompts dropped."""
    joined=[];carry=''
    for line in block.splitlines():
        line=line.strip()
        if line.endswith('\\'):carry+=line[:-1].rstrip()+' ';continue
        if carry:line=carry+line;carry=''
        line=_PROMPT.sub('',line)
        if line and not line.startswith('#'):joined.append(line)
    return joined

def extract(title,body,explicit=()):
    """Up to MAX_COMMANDS commands a note records: explicit ones first, then code spans in the title, summary, details and
    other sections (not links or action items) and shell-tagged fences. Identifiers, paths and bare flags are not commands."""
    from .note_format import split
    found=list(explicit or [])
    candidates=list(_SPAN.findall(title or ''))
    for key,_,text in split(body or ''):
        if key in {'links','actions'}:continue
        for block in _FENCE.finditer(text):
            if block[2].lower() in SHELL_LANGS:candidates+=_lines(block[3])
        candidates+=_SPAN.findall(_FENCE.sub('',text))
    from .repository import find_secret
    for candidate in candidates:
        text=' '.join(candidate.split())
        if text and len(text)<=MAX_CHARS and _is_command(text) and not find_secret(text):found.append(text)   # never index a credential
    seen=set();result=[]
    for text in found:
        if text.casefold() not in seen:seen.add(text.casefold());result.append(text)
    return result[:MAX_COMMANDS]

_READONLY=[]
def _ignored(program):
    if not _READONLY:
        from .runtime import READONLY_SIMPLE
        _READONLY.append(READONLY_SIMPLE)
    return program in _READONLY[0] or program in NEVER or program.startswith('living_memory')

def match(note_sig,cmd_sig):
    """'exact' (same wrapper), 'related' (another wrapper around the same program) or None: the programs agree and the
    note's subcommands lead the command's. A generic program needs a subcommand and its long flags, and read-only programs never match."""
    if note_sig.program!=cmd_sig.program or _ignored(note_sig.program) or note_sig.subs!=cmd_sig.subs[:len(note_sig.subs)]:return None
    if note_sig.program in GENERIC:
        # `git push --force` is about the flag as much as the subcommand: a plain `git push` is not what the note warns about.
        if not note_sig.subs or any(f.startswith('--') and f not in cmd_sig.flags for f in note_sig.flags):return None
    return 'exact' if note_sig.wrapper==cmd_sig.wrapper else 'related'

TYPE_ORDER={'bug':0,'runbook':0,'knowledge':0}

def command_notes(text,domains,root=None,limit=2):
    """Active notes whose recorded commands match this command line: exact before related, bug/runbook/knowledge before
    other types, newest first. Read-only like search.file_notes: a missing or older index gives nothing, and a note
    whose file changed since the index was built is skipped."""
    sigs=signature(text)
    if not sigs:return []
    root=Path(root or search.memory_root()).resolve();ranked=[]
    for domain in dict.fromkeys(domains):
        try:db_path=search.domain_dir(domain,root)/'search.sqlite3'
        except ValueError:continue
        if not db_path.exists():continue
        try:
            db=sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True)
            try:rows=db.execute("SELECT id,title,type,updated,commands,body,path,revision FROM notes WHERE status='active' AND commands<>'[]' AND commands<>''").fetchall()
            finally:db.close()
        except sqlite3.Error:continue
        if not rows:continue
        from .learning import published
        try:manifests,servable=published(root,domain)
        except (ValueError,OSError):manifests,servable={},set()
        for note_id,title,kind,updated,commands,body,note_path,revision in rows:
            if note_id in manifests and note_id not in servable:continue
            try:listed=[c for c in json.loads(commands) if isinstance(c,str)]
            except ValueError:continue
            best=None
            for command in listed:
                for note_sig in signature(command):
                    for cmd_sig in sigs:
                        kind_of=match(note_sig,cmd_sig)
                        if kind_of and (best is None or (kind_of=='exact' and best[0]!='exact')):best=(kind_of,command)
            if not best or not search._unchanged(note_path,revision):continue
            hit={'id':note_id,'domain':domain,'title':title,'type':kind or 'knowledge','updated':str(updated or '')[:10],'match':best[0],'command':best[1],
                 'snippet':search.snippet(body,search.terms(best[1]),200)}
            from .notes import is_interim
            if is_interim({'type':kind,'title':title,'body':body}):hit['interim']=True
            if note_id in manifests:hit.update(procedure_id=manifests[note_id]['id'],procedure_version=manifests[note_id]['current_version'])
            ranked.append(hit)
    ranked.sort(key=lambda h:h['updated'],reverse=True)
    ranked.sort(key=lambda h:(h['match']!='exact',TYPE_ORDER.get(h['type'],1)))
    return ranked[:max(1,limit)]
