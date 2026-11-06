"""Shared deterministic lifecycle behavior; adapters translate native wire formats."""
from __future__ import annotations
import hashlib,json,os,re,shlex,sys
from pathlib import Path
from .common import memory_root,load_bindings,resolve_domain,atomic_write,canonical_domain,file_lock
from .repository import read_json,canonical_json,now
from .capture import status as capture_status,turn_key
from .note_types import NOTE_TYPES

from .instructions import PROTOCOL,AUTO_MEMORY,usage,installed
SCRIPT=Path(__file__).resolve().parents[1]/'living_memory.py'
# Tools that cannot change files; anything else marks the turn as mutating.
READ_TOOLS={'read','read_file','glob','grep','search','view_image','view','ls','list','webfetch','websearch','web_fetch','web_search','todowrite','task','agent','toolsearch','askuserquestion','exitplanmode','enterplanmode'}
SHELL_TOOLS={'bash','powershell','shell','run_shell_command','exec_command','local_shell','execute'}
ALLOWED_SUBCOMMANDS={'search','read','taxonomy','doctor','history','inspect-plan','capture','capture-finalize','validate','update','supersede','review','backup','open','check','stats','confirm','timeline'}
# Subagents are read-only: these are denied for them even though the main agent gets them auto-allowed.
MUTATING_SUBCOMMANDS={'capture','capture-finalize','update','supersede','backup','check','confirm'}
GLOBAL_OPTIONS={'--root','--domain','--cwd','--actor'}
HOOK_LOCK_TIMEOUT=5
# Codex has no SessionEnd, so its session start picks up Obsidian edits, at most this often.
SESSION_START_SYNC_SECONDS=15*60

def _configured_root():
    try:
        from .storage import config_path
        return Path(json.loads(config_path().read_text(encoding='utf-8-sig'))['data_root']).resolve()
    except (OSError,ValueError,KeyError,TypeError):return None

def command(root,domain=None):
    """The script invocation agents should run. --root only when the hook is not using the shared store."""
    text=f'"{sys.executable}" "{SCRIPT}"'
    if Path(root).resolve()!=_configured_root():text+=f' --root "{root}"'
    return text+(f' --domain {domain}' if domain else '')

def _tag_summary(root):
    try:
        data=read_json(Path(root)/'tag_taxonomy.json',{'tags':[]});tags=data.get('tags',[]) if isinstance(data,dict) else data
    except ValueError:return ''
    facets={}
    for tag in sorted(tags):
        facet,_,value=str(tag).partition('/')
        if value:facets.setdefault(facet,[]).append(value)
    return '; '.join(f"{facet}/{{{','.join(values)}}}" for facet,values in facets.items())

def protocol(root,domain=None,harness='',session='SESSION'):
    """SessionStart context (re-fires after compaction). When the harness's instruction file carries the
    current static block, only per-session data is injected; otherwise the full protocol, so the agent
    never sees zero copies or two differing copies of the rules."""
    cmd=command(root,domain)
    dynamic=(f'CMD = {cmd}   (PowerShell: run it as & CMD ...)'
             f"\nTags (pick the closest; --platform/--technology optional): {_tag_summary(root)}")
    if installed(harness):
        return f'Living Memory is active for domain {domain}; follow the Living Memory section of your instructions.\n'+dynamic
    text=PROTOCOL+'\n'+dynamic+'\n'+usage(session)
    if harness=='claude':text+='\n'+AUTO_MEMORY
    return text

def protected(path,cwd,root):
    if path in {'/dev/null','dev/null'}:return False
    try:
        p=Path(os.path.expandvars(path)).expanduser()
        if not p.is_absolute():p=Path(cwd)/p
        p=p.resolve();root=Path(root).resolve()
        from .storage import config_path
        if p.is_relative_to(root/'domains') or p in {root/'bindings.json',root/'tag_taxonomy.json',root/'.relocated.json',config_path().resolve()}:return True
        # Vault edits are auto-applied and vault checkboxes approve plans, so agents must not write there either.
        return any(p.is_relative_to(v/folder) for v in vault_roots(root) for folder in ('Domains','Archive','Dashboards'))
    except (ValueError,OSError):return False

def vault_roots(root):
    found=set()
    for state in Path(root).glob('domains/*/sync-state.json'):
        try:vault=json.loads(state.read_text(encoding='utf-8')).get('vault')
        except (OSError,ValueError):continue
        if vault:found.add(Path(vault).resolve())
    return found

def raw_paths(value):
    if isinstance(value,dict):
        found=[]
        for key,item in value.items():
            if key.lower() in {'file','filepath','file_path','path','target','targetpath','target_path','notebook_path'} and isinstance(item,str):found.append(item)
            elif isinstance(item,(dict,list)):found.extend(raw_paths(item))
            elif isinstance(item,str) and key in {'command','cmd','patch','diff','input'}:found.extend(raw_paths(item))
        return found
    if isinstance(value,list):return [p for item in value for p in raw_paths(item)]
    if isinstance(value,str):
        patch=re.findall(r'(?m)^\*\*\* (?:Add|Delete|Update) File:\s*(.+)$',value)
        # Quoted absolute paths include spaces. This is an agent guard, not a shell parser.
        quoted=re.findall(r'[\"\x27]((?:[A-Za-z]:[\\/]|/)[^\"\x27]+)[\"\x27]',value)
        absolute=re.findall(r'(?<![\w])(?:[A-Za-z]:[\\/]|/)[^\s\"\x27;|<>]+',value)
        return patch+quoted+absolute
    return []

def _norm(path):return os.path.normcase(str(path).replace('/','\\') if os.name=='nt' else str(path))

def shell_command(args):
    if isinstance(args,dict):
        value=args.get('command',args.get('cmd'))
        if isinstance(value,list):value=value[-1] if value else ''
        return value if isinstance(value,str) else ''
    return args if isinstance(args,str) else ''

SINGLE_QUOTED=re.compile(r"'(?:[^']|'')*'")

def _inert(rest):
    """True when the arguments cannot run anything: shell metacharacters appear only inside single quotes
    (literal in Bash and PowerShell). A single quote inside double quotes is an ordinary character, so the
    quote state is tracked rather than stripping '...' spans. Double-quoted text may not expand ($, `) or
    escape (a backslash before $ ` " \\ or a newline); other backslashes, as in Windows paths, are literal."""
    state=None
    for i,ch in enumerate(rest):
        if state=="'":
            if ch=="'":state=None
        elif state=='"':
            if ch=='"':state=None
            elif ch in '$`' or (ch=='\\' and rest[i+1:i+2] in ('$','`','"','\\','\n','\r')):return False
        elif ch in "'\"":state=ch
        elif ch in ';|&<>`$\r\n':return False
    return state is None

def memory_subcommand(text):
    """Subcommand of an exact, unchained living_memory.py invocation, else None.

    Single-quoted arguments are literal in Bash and PowerShell, so note text inside them may
    contain anything (->, |, $, newlines). Outside them, any shell metacharacter disqualifies."""
    match=re.match(r'\s*(?:&\s*)?"([^"]+)"\s+"([^"]+)"(.*)\Z',text,re.S)
    if not match or _norm(match[1])!=_norm(sys.executable) or _norm(match[2])!=_norm(SCRIPT):return None
    rest=match[3]
    if not _inert(rest):return None
    rest=SINGLE_QUOTED.sub("'x'",rest)
    try:tokens=shlex.split(rest,posix=True)
    except ValueError:return None
    index=0
    while index<len(tokens):
        token=tokens[index]
        if token in GLOBAL_OPTIONS:index+=2;continue
        if token=='--json':index+=1;continue
        return token
    return None

READONLY_SIMPLE={'ls','dir','cat','type','head','tail','wc','grep','egrep','fgrep','cut','tr','echo','printf','pwd','which','where','whoami','stat','file','du','df','tree','diff','cmp','basename','dirname','realpath','readlink','test','[','true','false','sleep','cd','pushd','popd','set-location','gci','gc','printenv',
 'select-string','select-object','where-object','sort-object','measure-object','format-table','format-list','test-path','resolve-path','split-path','join-path','convertto-json','write-output','write-host',
 'nl','comm','paste','rev','fold','column','md5sum','sha1sum','sha256sum','jq','seq'}
READONLY_GIT={'status','log','diff','show','ls-files','ls-tree','rev-parse','blame','describe','shortlog','grep','cat-file','rev-list','merge-base','name-rev',
 'for-each-ref','show-ref','ls-remote','count-objects'}
FIND_WRITERS={'-delete','-exec','-execdir','-ok','-okdir','-fprint','-fprintf','-fls','-fprint0'}
NULL_SINKS={'/dev/null','nul','$null'}
# Control-flow words wrap a command without running anything themselves: `for f in *; do cat $f; done`.
SHELL_KEYWORDS={'do','then','else','elif','if','while','until','!'}
SHELL_CLOSERS={'done','fi'}
# sed: s///FLAGS may only print or match (w writes a file, e executes); other commands may only print/edit the stream.
SED_FLAGS=set('gpiImM0123456789')
SED_SAFE=re.compile(r'[\s0-9,$!;{}pdnNDPqQ=lgGhHx~+-]*')
# awk: shell-outs, pipes, extension loading and output redirection (`print > file`) can write.
AWK_UNSAFE=re.compile(r'system\s*\(|\||@load|@include|\bprintf?\b[^;}]*>')
XARGS_VALUED={'-n','-L','-I','-d','-P','-s','-a','-E','-e'}
UNIQ_VALUED={'-f','-s','-w'}

def _positional(args,valued=()):
    found=[];skip=False
    for a in args:
        if skip:skip=False;continue
        if a in valued:skip=True;continue
        if a.startswith('-') and a!='-':continue
        found.append(a)
    return found

def _sed_script_safe(script):
    text=re.sub(r'\\.','',script)
    def body(m):return ' ' if set(m[2])<=SED_FLAGS else '\0'
    text=re.sub(r's(.)(?:(?!\1).)*\1(?:(?!\1).)*\1([A-Za-z0-9]*)',body,text)
    text=re.sub(r'y(.)(?:(?!\1).)*\1(?:(?!\1).)*\1',' ',text)
    text=re.sub(r'/[^/]*/[IM]?',' ',text)
    return bool(SED_SAFE.fullmatch(text))

def _sed_readonly(args):
    scripts=[];positional=[];expect=False
    for a in args:
        if expect:scripts.append(a);expect=False;continue
        if a in {'-e','--expression'}:expect=True;continue
        if a.startswith('--expression='):scripts.append(a.split('=',1)[1]);continue
        if a.startswith('--'):
            if a.startswith(('--in-place','--file')):return False
            continue
        if a.startswith('-') and a!='-':
            flags=a[1:]
            if 'e' in flags:
                cut=flags.index('e');flags,attached=flags[:cut],flags[cut+1:]
                if attached:scripts.append(attached)
                else:expect=True
            if 'i' in flags or 'f' in flags:return False
            continue
        positional.append(a)
    if not scripts and positional:scripts=[positional[0]]
    return bool(scripts) and not expect and all(_sed_script_safe(s) for s in scripts)

def _awk_readonly(args):
    skip=False
    for a in args:
        if skip:skip=False;continue
        if a in {'-F','-v','--field-separator','--assign'}:skip=True;continue
        if a.startswith(('-f','-i','-l','-E','--file','--include','--load','--exec')):return False
        if a.startswith('-') and a!='-':continue
        return not AWK_UNSAFE.search(a)
    return False

def _xargs_command(words):
    index=0
    while index<len(words):
        a=words[index].strip('"\'')
        if a in XARGS_VALUED:index+=2;continue
        if a.startswith('-') and a!='-':index+=1;continue
        break
    return words[index:]

def _readonly_segment(words):
    """True when one simple command (already split on ; & | and parentheses) provably cannot write files."""
    while words and words[0].strip('"\'').lower() in SHELL_KEYWORDS:words=words[1:]
    if not words:return True
    if len(words)==1 and words[0].lower() in SHELL_CLOSERS:return True
    # A for-loop header only expands words; its body is judged as separate segments.
    if words[0].lower()=='for':return len(words)>=3 and words[2].lower()=='in'
    first=words[0].strip('"\'').replace('\\','/').rsplit('/',1)[-1].lower()
    first=first[:-4] if first.endswith('.exe') else first
    args=[w.strip('"\'') for w in words[1:]]
    if first in READONLY_SIMPLE:return True
    if first.startswith('get-'):return True  # PowerShell getters only read
    if first=='find':return not FIND_WRITERS.intersection(args)
    if first=='rg':return not any(a=='--pre' or a.startswith('--pre=') for a in args)
    if first=='sort':return not any(a.startswith('-o') or a.startswith('--output') for a in args)
    if first=='uniq':return len(_positional(args,UNIQ_VALUED))<=1  # `uniq in out` writes out
    if first=='sed':return _sed_readonly(args)
    if first in {'awk','gawk','mawk'}:return _awk_readonly(args)
    if first=='xargs':return _readonly_segment(_xargs_command(words[1:]))
    if first=='git':
        rest=args[:]
        while rest and rest[0]=='-C' and len(rest)>1:rest=rest[2:]
        if not rest or rest[0].startswith('-'):return False
        sub,tail=rest[0],rest[1:]
        if any(a.startswith('--output') or a=='-o' for a in tail):return False
        if sub in READONLY_GIT:return True
        if sub=='branch':return all(a in {'-a','-r','-v','-vv','--list','--show-current'} for a in tail)
        if sub=='remote':return all(a in {'-v','--verbose'} for a in tail)
        if sub=='tag':return all(a in {'-l','--list'} for a in tail)
        if sub=='config':return bool(tail) and tail[0] in {'--get','--get-all','--list','-l'}
        if sub=='reflog':return not tail or tail[0]=='show' or tail[0].startswith('-')  # expire/delete rewrite it
        if sub=='stash':return bool(tail) and tail[0] in {'list','show'}  # bare `git stash` saves and resets
        return False
    if first in {'python','python3','py','node'}:return args in (['--version'],['-V'],['-v'])
    if first in {'pip','pip3'}:return bool(args) and args[0] in {'list','show','freeze','--version'}
    if first=='npm':return bool(args) and args[0] in {'ls','list','view','--version','-v'}
    return False

def readonly_shell(text):
    """True only if a shell command is provably read-only. Anything doubtful returns False.
    This only decides whether the turn needs a memory reminder; it is not a security check
    (the PreToolUse deny path deliberately does not parse shell text)."""
    if not text or re.search(r'[`\r\n]|\$\(|<\(',text):return False
    try:
        lexer=shlex.shlex(text,posix=False,punctuation_chars=True);lexer.whitespace_split=True
        tokens=list(lexer)
    except ValueError:return False
    words=[];index=0
    while index<len(tokens):
        token=tokens[index]
        if token and set(token)<=set(';&|<>()') and not token.startswith(('"',"'")):
            if set(token)&set('<>'):
                # Only discard-to-null and fd duplication (2>&1) are harmless redirections.
                nxt=tokens[index+1].strip('"\'').lower() if index+1<len(tokens) else ''
                if '<' in token:return False
                if token.endswith('&') and nxt.isdigit():index+=2;continue
                if token in {'>','>>'} and nxt in NULL_SINKS:index+=2;continue
                return False
            # ; & | and subshell parentheses only separate commands; each command is judged on its own.
            if not _readonly_segment(words):return False
            words=[];index+=1;continue
        words.append(token);index+=1
    return _readonly_segment(words)

def _sync_vaults(root,trigger,min_interval=0):
    """Pull Obsidian edits into memory and refresh every managed vault. Never fails the hook."""
    try:
        from .sync import sync_all
        if min_interval:
            from datetime import datetime,timezone
            last=read_json(root/'hooks/last-sync.json',{}) or {}
            try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(last['at'])).total_seconds()
            except (KeyError,TypeError,ValueError):age=None
            if age is not None and age<min_interval:return None
        return sync_all(root,trigger)
    except Exception:return None

def _daily_backup(root):
    """At most one snapshot a day, taken from session housekeeping. Never fails the hook."""
    try:
        from .backup import maybe_backup
        return maybe_backup(root)
    except Exception:return None

def _subagent(payload):
    """Claude Code marks hooks that fire inside a subagent with agent_id. Subagents are read-only
    by protocol and share the parent session_id, so their tool calls must not arm the parent's reminder."""
    return bool(payload.get('agent_id') or payload.get('agentId'))

def _scratch_only(args,payload,cwd):
    """True when every path a file tool targets lies in the harness scratchpad (session-private temp files)."""
    scratch=payload.get('scratchpad_dir') or payload.get('scratchpadDir')
    paths=raw_paths(args)
    if not scratch or not paths:return False
    try:
        base=Path(scratch).resolve()
        for raw in paths:
            p=Path(os.path.expandvars(raw)).expanduser()
            if not p.is_absolute():p=Path(cwd)/p
            if not p.resolve().is_relative_to(base):return False
        return True
    except (OSError,ValueError):return False

MAX_TOUCHED=30
MAX_TOUCHED_SESSION=200

def touched_paths(args,payload,cwd,root,domain,bindings,shell=False):
    """Files a write tool targets, as notes name them (see display_path). Paths only, never content.
    Scratchpad and memory-store paths are skipped. Shell text yields loose matches (URLs, flags), so
    for shell tools only files inside the bound project count."""
    from .common import domain_prefixes,display_path
    try:
        scratch=payload.get('scratchpad_dir') or payload.get('scratchpadDir')
        base=Path(scratch).resolve() if scratch else None
        prefixes=domain_prefixes(domain,bindings);store=Path(root).resolve();found=[]
        for raw in raw_paths(args):
            if raw.strip().lower() in NULL_SINKS:continue
            try:
                p=Path(os.path.expandvars(raw.strip())).expanduser()
                if not p.is_absolute():p=Path(cwd)/p
                p=p.resolve()
            except (OSError,ValueError):continue
            if (base and p.is_relative_to(base)) or p.is_relative_to(store) or p.is_dir():continue
            if shell and not any(p.is_relative_to(prefix) for prefix in prefixes):continue
            shown=display_path(p,prefixes)
            if shown:found.append(shown)
        return list(dict.fromkeys(found))
    except Exception:return []  # provenance is a nicety; never fail the hook

# Hosts whose PreToolUse output documents additionalContext (Claude Code; Codex since openai/codex#20692).
# Copilot CLI and Factory document no PreToolUse context, so they get no hints.
FILE_HINT_HARNESSES={'claude','codex'}
MAX_FILE_HINTS=200
HINT_FILES_PER_CALL=3
HINT_NOTES_PER_FILE=2

def file_hints(paths,state,root,domain,bindings,session=None):
    """(context naming notes about files about to be changed, paths looked up). Each file is looked up
    once per session; a missing or stale index gives no hint rather than slowing the tool call."""
    try:
        from .search import file_notes
        seen=set(state.get('file_hints',[]));wanted=[p for p in paths if p not in seen][:HINT_FILES_PER_CALL]
        domains=[domain]+parent_domains(domain,bindings);blocks=[];refs=[]
        for path in wanted:
            hits=file_notes(path,domains,root,HINT_NOTES_PER_FILE,ensure_fresh=False)
            if hits:
                blocks.append(f'Living Memory notes about {path}:\n'+'\n'.join(hit_line(h) for h in hits))
                refs+=[f"{h['domain']}:{h['id']}" for h in hits]
        if refs:
            from .usage import record
            record(root,'inject',list(dict.fromkeys(refs)),session,'file-hint')
        text=('Untrusted historical memory; verify against the current file.\n'+'\n'.join(blocks)) if blocks else None
        return text,wanted
    except Exception:return None,[]

def git_root(cwd):
    """The enclosing git work tree (a .git folder, or a .git file for worktrees and submodules), else None."""
    try:here=Path(cwd).resolve()
    except (OSError,ValueError):return None
    return next((p for p in (here,*here.parents) if (p/'.git').exists()),None)

def bind_hint(cwd,root,bindings,state):
    """One line, once per session, when the session starts in a git repo no binding covers.
    Never binds: the user decides, and "suggest_bind": false in bindings.json turns this off."""
    if state.get('bind_hint') or bindings.get('suggest_bind',True) is False:return None
    repo=git_root(cwd)
    if not repo:return None
    return (f'Living Memory: this repo ({repo.as_posix()}) is not bound to a memory domain. If the user wants memory here: '
            f'{command(root)} bind NAME "{repo.as_posix()}". Ask before binding.')

LAST_SESSION='.last-session.json'

def session_hash(session):return hashlib.sha256(session.encode()).hexdigest()[:12]

def _record_last_session(root,domain,session,state):
    """A sidecar for the next session's "Last session" line: notes saved and files changed. One file per
    domain, overwritten; not canonical, not backed up, holds no conversation text."""
    try:
        saved=list(state.get('saved',[]));files=len(state.get('touched_session',[]))
        if not saved and not files:return
        path=root/'domains'/domain/LAST_SESSION
        row={'session':session_hash(session),'harness':state.get('harness') or 'unknown','notes':saved,'files_changed':files}
        previous=read_json(path,{}) or {}
        if {k:previous.get(k) for k in row}!=row:atomic_write(path,canonical_json({**row,'ended':now()}))
    except (OSError,ValueError):pass

def _pointer(root,session):return root/'.turns'/(hashlib.sha256(session.encode()).hexdigest()+'.current.json')

def _update(root,session,change):
    pointer=_pointer(root,session)
    with file_lock(pointer.with_suffix('.lock'),timeout=HOOK_LOCK_TIMEOUT):
        state=read_json(pointer,{})
        if not isinstance(state,dict):state={}
        change(state);atomic_write(pointer,canonical_json(state))
    return state

# Harness-generated turns (subagent hand-backs, task notifications) are not questions to answer from memory.
MACHINE_PROMPT=re.compile(r'\A\s*(?:<(?:agent-message|task-notification|system-reminder)\b|\[SYSTEM NOTIFICATION)')
HIT_SNIPPET=220
# Conversation is where preferences, corrections and decisions are stated, and those turns often edit no files.
# Deterministic and conservative: a false positive costs one reminder line and a capture-finalize.
QUOTED=re.compile(r'```.*?(?:```|\Z)|<(pasted_content|system-reminder|agent-message|task-notification)\b.*?(?:</\1>|\Z)|^[ \t]*>[^\r\n]*$',re.S|re.M|re.I)
SIGNALS=(
    ('remember',re.compile(r"\b(?:remember (?:that|this|we|it)|keep in mind|for (?:the )?record|don'?t forget)\b",re.I)),
    ('correction',re.compile(r"^\s*(?:no|nope)\s*[,.!:;—-]|^\s*actually\b|\bthat'?s (?:wrong|not right|incorrect|not what i)\b|\byou(?:'re| are| were) wrong\b|\bi meant\b",re.I|re.M)),
    ('preference',re.compile(r"\b(?:i(?:'d)? prefer|i'?d rather|i (?:hate|don'?t like)|from now on|going forward|in (?:the )?future(?!\s*\?)|please (?:don'?t|do not|never|always)|stop doing|don'?t ever|always use|never use|by default,? use)\b",re.I)),
    ('decision',re.compile(r"\b(?:let'?s (?:go with|use)|we(?:'ll| will) (?:go with|use)|we decided|i(?:'ve)? decided|go with option)\b",re.I)),
)
SIGNAL_TEXT={'remember':('asked you to remember something','--type TYPE --concern CONCERN'),
             'correction':('corrected you','--type knowledge --concern correction'),
             'preference':('stated a preference','--type preference --concern preference'),
             'decision':('made a decision','--type decision --concern decision')}

def durable_signal(prompt):
    """The kind of durable statement the user's own words carry ('preference', 'correction', 'decision',
    'remember'), else None. Pasted, quoted and harness-generated text is ignored."""
    if not isinstance(prompt,str) or MACHINE_PROMPT.match(prompt):return None
    text=QUOTED.sub(' ',prompt)
    return next((kind for kind,pattern in SIGNALS if pattern.search(text)),None)

def hit_line(hit):
    snippet=hit.get('snippet','')
    if len(snippet)>HIT_SNIPPET:
        cut=snippet.rfind(' ',0,HIT_SNIPPET);snippet=snippet[:cut if cut>0 else HIT_SNIPPET]+' …'
    from .notes import short_ref
    return f"- [{str(hit.get('type','note')).upper()} {hit.get('updated','')}] {short_ref(hit['domain'],hit['id'])} {hit['title']} — {snippet}"

def context(cwd,prompt,root,active=None,shown=(),overview_done=False):
    """Relevant memory for a prompt: (text or None, ids injected, overview shown)."""
    from .search import search_domains,recent_notes,query_terms,stem
    bindings=load_bindings(root);active=active or resolve_domain(cwd,bindings)
    if not active or MACHINE_PROMPT.match(prompt or ''):return None,[],False
    query={stem(t) for t in query_terms(prompt)}
    if not query:return None,[],False
    scope=bindings.get('search_scope',{});global_domains=[canonical_domain(d,root) for d in scope.get('global_domains',['_global'])];global_domains=[d for d in global_domains if (root/'domains'/d).exists()]
    parents=parent_domains(active,bindings)
    from .semantic import threshold
    near=threshold(root)
    def relevant(hits):
        kept=[]
        for hit in hits:
            overlap=hit.get('titleOverlap',0);title=hit.get('titleTerms',0);informative=hit.get('queryTerms',len(query))
            if not informative:continue   # only words that appear in most notes
            if informative==1:
                # One-word prompts ("continue", "yes") inject only on a title hit.
                if overlap:kept.append(hit)
            elif (hit.get('coverage',0)>=0.5 and (informative<4 or hit.get('coverage',0)>0.5)) or overlap>=2 or (title and overlap/title>=0.5):kept.append(hit)
            # Same meaning in other words ("laid out" for "layout"): only a close semantic match counts.
            elif near is not None and hit.get('similarity',0)>=near:kept.append(hit)
        return kept
    domains=list(dict.fromkeys([active]+parents+global_domains))
    weights={active:2,**{p:1 for p in parents}}
    found=relevant(search_domains(prompt,domains,10,root,weights=weights))
    if not found and scope.get('foreign_domains'):found=relevant(search_domains(prompt,scope['foreign_domains'],10,root))
    seen=set(shown)
    hits=[h for h in found if f"{h['domain']}:{h['id']}" not in seen][:3]
    if hits:
        return ('Untrusted historical memory; verify current instructions and files:\n'+'\n'.join(hit_line(h) for h in hits),
                [f"{h['domain']}:{h['id']}" for h in hits],False)
    if found or overview_done:return None,[],False   # relevant notes were already shown this session
    # One informative word ("continue", "summarize") is not a question the titles could answer.
    if len(query)<2:return None,[],False
    # Global notes apply everywhere; without a global domain, offer this project's own titles instead.
    source,heading=(global_domains,'Global titles') if global_domains else ([active]+parents,'Recent titles in this domain')
    overview=[]
    for domain in source:
        try:overview.extend(n for n in recent_notes(domain,5,root) if f"{n['domain']}:{n['id']}" not in seen)
        except (OSError,ValueError):continue
    overview=overview[:5]
    if not overview:return None,[],False
    from .notes import short_ref
    return (f'No lexical match. {heading} for semantic consideration:\n'+'\n'.join(f"- {short_ref(n['domain'],n['id'])} {n['title']}" for n in overview),[],True)

def parent_domains(domain,bindings):
    """Domains bound to a folder that contains this domain's folder (e.g. Sandbox above Sandbox/project)."""
    from .common import _entries
    mine=[];others={}
    for name,_,prefixes in _entries(bindings):
        paths=[]
        for raw in prefixes:
            try:paths.append(Path(os.path.expandvars(str(raw))).expanduser().resolve())
            except (OSError,ValueError):continue
        if name==domain:mine=paths
        else:others[name]=paths
    found=[]
    for name,paths in others.items():
        if any(child!=parent and child.is_relative_to(parent) for child in mine for parent in paths):found.append(name)
    return sorted(found,key=lambda n:-max(len(str(p)) for p in others[n]))

def process(harness,event,payload,root=None):
    """No store-wide lock: hooks must not serialize every session's tool calls.
    Writes below are atomic files; search and capture take the store lock themselves."""
    from . import repository
    from .storage import resolve_root
    root=resolve_root(root)[0]
    previous=repository.LOCK_TIMEOUT;repository.LOCK_TIMEOUT=HOOK_LOCK_TIMEOUT
    try:return _process(harness,event,payload,root)
    finally:repository.LOCK_TIMEOUT=previous

def _process(harness,event,payload,root=None):
    root=Path(root or memory_root()).resolve();cwd=payload.get('cwd') or payload.get('workingDirectory') or os.getcwd()
    session=str(payload.get('session_id') or payload.get('sessionId') or 'unknown');turn=str(payload.get('turn_id') or payload.get('turnId') or '')
    bindings=load_bindings(root)
    state=read_json(_pointer(root,session),{}) if session!='unknown' else {}
    if not isinstance(state,dict):state={}
    pinned=state.get('domain')
    domain=pinned if pinned and pinned in bindings.get('domains',{}) else resolve_domain(cwd,bindings)
    if event=='session-start' and session!='unknown':
        current=resolve_domain(cwd,bindings) or domain;domain=current
        def pin(s):
            s['harness']=harness
            if current:s['domain']=current
            else:s.pop('domain',None)
        state=_update(root,session,pin)
    if event=='user-prompt':
        turn=turn or str(payload.get('timestamp') or now())
        if session!='unknown':
            # Plan mode cannot save anything, so a statement there is not a reminder's business.
            planning=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan'
            text=payload.get('prompt') or payload.get('user_prompt') or payload.get('userPrompt') or payload.get('transformedPrompt') or ''
            signal=None if planning else durable_signal(text)
            from . import private
            def opened(s):
                s['turn']=turn;s['open_turns']=(list(s.get('open_turns',[]))+[turn])[-20:]
                if signal:s['signal']=signal  # only the kind is kept, never the prompt text
                # <private> spans: salted shingle hashes only, so a capture that copies one can be refused.
                if isinstance(text,str) and not MACHINE_PROMPT.match(text) and private.remember(s,text):s['private_turn']=turn
            state=_update(root,session,opened)
    elif not turn:
        turn=state.get('turn','')
    result={}
    if event in {'session-start','user-prompt','stop'}:
        try:atomic_write(root/'hooks/last-delivery.json',canonical_json({'harness':harness,'event':event,'timestamp':now(),'bound':bool(domain),'cwd':str(cwd),'domain':domain,'payload_keys':sorted(payload)}))
        except OSError:pass  # Diagnostics only; never cost the session its memory context.
    if event=='session-end':
        _sync_vaults(root,'session end');_daily_backup(root);return {}
    if not domain:
        if event=='session-start' and session!='unknown':
            hint=bind_hint(cwd,root,bindings,state)
            if hint:
                _update(root,session,lambda s:s.update(bind_hint=True));result['context']=hint
        return result
    if event=='session-start':
        # Codex has no SessionEnd: its session start does the end-of-session housekeeping.
        if harness=='codex':_sync_vaults(root,'session start',SESSION_START_SYNC_SECONDS);_daily_backup(root)
        from .session_start import build_context,BUDGET,MIN_BUDGET
        prefix=protocol(root,domain,harness,session)+f'\nLiving Memory session={session}'+'\n'
        text,shown,indexed=build_context(cwd,root,max(MIN_BUDGET,BUDGET-len(prefix)),session=session)
        if session!='unknown':
            # A new or compacted context has seen only what is injected now. Guidance shown in full is not
            # re-injected by prompts; index lines are titles, so those notes stay eligible. Once the index has
            # listed the newest titles, the prompt-time "recent titles" fallback has nothing to add.
            def started(s):
                s.pop('shown',None);s.pop('overview_shown',None)
                if shown:s['shown']=shown
                if indexed:s['overview_shown']=True
            _update(root,session,started)
            if shown:
                from .usage import record
                record(root,'inject',shown,session,'session')
        result['context']=prefix+(text or '')
    elif event=='user-prompt':
        prompt=payload.get('prompt') or payload.get('user_prompt') or payload.get('userPrompt') or payload.get('transformedPrompt') or ''
        hits,ids,overview=context(cwd,prompt,root,domain,state.get('shown',[]),state.get('overview_shown',False)) if isinstance(prompt,str) else (None,[],False)
        if (ids or overview) and session!='unknown':
            def remember(s):
                s['shown']=(list(s.get('shown',[]))+ids)[-50:]
                if overview:s['overview_shown']=True
            try:_update(root,session,remember)
            except (OSError,TimeoutError):pass
        if ids:
            from .usage import record
            record(root,'inject',ids,session,'prompt')
        result['context']=f'Living Memory session={session} turn={turn}'+('\n'+hits if hits else '')
        if state.get('private_turn')==turn:
            from .private import NOTICE
            result['context']+='\n'+NOTICE
    elif event=='subagent-start':result['context']='Living Memory: subagents may only search/read via scripts. Return capture proposals to the main agent; do not write canonical state.'
    elif event=='pre-tool-use':
        name=str(payload.get('tool_name') or payload.get('toolName') or '').lower()
        args=payload.get('tool_input',payload.get('toolArgs',payload.get('toolArguments',{})))
        if isinstance(args,str):
            try:args=json.loads(args)
            except json.JSONDecodeError:pass
        # Shell text is not checked: a regex over commands is trivially bypassed and blocks harmless reads.
        if name not in READ_TOOLS and name not in SHELL_TOOLS and any(protected(p,cwd,root) for p in raw_paths(args)):
            result['deny']='Use Living Memory scripts for canonical mutations; direct file edits bypass revision checks and receipts.'
            return result
        text=shell_command(args) if name in SHELL_TOOLS else ''
        subcommand=memory_subcommand(text) if text else None
        if subcommand in MUTATING_SUBCOMMANDS and _subagent(payload):
            result['deny']='Living Memory: subagents are read-only. Return the proposed note to the main agent instead of running '+subcommand+'.'
            return result
        if subcommand in ALLOWED_SUBCOMMANDS:
            result['allow']='Living Memory read/capture script command.'
        # Plan mode only permits writing the harness's own plan file, so it never changes project files.
        planning=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan'
        if (name not in READ_TOOLS and not subcommand and not (name in SHELL_TOOLS and readonly_shell(text))
                and not _subagent(payload) and not planning and not (name not in SHELL_TOOLS and _scratch_only(args,payload,cwd))
                and session!='unknown'):
            paths=touched_paths(args,payload,cwd,root,domain,bindings,shell=name in SHELL_TOOLS)
            hints,looked=file_hints(paths,state,root,domain,bindings,session) if harness in FILE_HINT_HARNESSES else (None,[])
            if hints:result['context']=hints
            if not state.get('mutating') or set(paths)-set(state.get('touched',[])) or looked:
                def touch(s):
                    s['mutating']=True
                    s['touched']=list(dict.fromkeys(list(s.get('touched',[]))+paths))[:MAX_TOUCHED]
                    s['touched_session']=list(dict.fromkeys(list(s.get('touched_session',[]))+paths))[-MAX_TOUCHED_SESSION:]
                    if looked:s['file_hints']=list(dict.fromkeys(list(s.get('file_hints',[]))+looked))[-MAX_FILE_HINTS:]
                _update(root,session,touch)
    elif event=='stop':
        if session=='unknown':return {}
        _record_last_session(root,domain,session,state)
        turns=list(dict.fromkeys(list(state.get('open_turns',[]))+([turn] if turn else [])))
        def close(s):
            s.pop('open_turns',None);s.pop('mutating',None);s.pop('signal',None);s.pop('touched',None)
        signal=state.get('signal')
        if any(capture_status(session,t,root) for t in turns) or not (state.get('mutating') or signal) or not turns:
            _update(root,session,close);return {}
        latest=turns[-1];reminder=root/'.turns'/(turn_key(session,latest)+'.reminded')
        if payload.get('stop_hook_active') or payload.get('stopHookActive') or reminder.exists():
            _update(root,session,close);return {}
        atomic_write(reminder,'reminded\n')
        from .usage import record
        record(root,'reminder',session=session,kind=signal if signal in SIGNAL_TEXT else 'mutating')
        cmd=command(root,domain)
        finalize=f"{cmd} capture-finalize --session {session} --turn {latest}"
        if signal in SIGNAL_TEXT:
            said,hint=SIGNAL_TEXT[signal]
            result['continue_reason']=(f"Living Memory: the user {said} this turn{' (and files changed)' if state.get('mutating') else ''}. "
                                       f"If it should shape future sessions, update the matching note or run {cmd} capture --session {session} --turn {latest} {hint} --subject SUBJECT --title 'Title' --summary '...'; "
                                       f"if it only mattered for this task, run {finalize}.")
        else:
            result['continue_reason']=(f"Living Memory: this turn changed files. Usually nothing here is durable: then run {finalize}. "
                                       f"Only if a future session needs it (a decision, preference, constraint or non-obvious fix), update the workstream's note or run {cmd} capture --session {session} --turn {latest} --type TYPE --subject SUBJECT --concern CONCERN 'Title' 'Body'.")
    return result
