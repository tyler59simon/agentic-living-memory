"""Shared deterministic lifecycle behavior; adapters translate native wire formats."""
from __future__ import annotations
import hashlib,json,os,re,shlex,sys,time
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
ALLOWED_SUBCOMMANDS={'search','read','taxonomy','doctor','history','logs','inspect-plan','capture','capture-finalize','validate','update','supersede','review','backup','open','check','stats','confirm','timeline','recall','promote-plan','projects','resolve','reopen'}
# Subagents are read-only: these are denied for them even though the main agent gets them auto-allowed.
MUTATING_SUBCOMMANDS={'capture','capture-finalize','update','supersede','backup','check','confirm','resolve','reopen'}
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

def hint_command(root,state,domain=None):
    """How a hint to the main agent names the script: CMD when this session's start defined it (a bound
    domain was pinned then), else the whole command line. Shorter, and it needs no shell-specific prefix."""
    return 'CMD' if isinstance(state,dict) and state.get('domain') else command(root,domain)

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

def protected(path,cwd,root):return protected_rule(path,cwd,root) is not None

def protected_rule(path,cwd,root):
    """Which canonical location a path falls in: 'domains', 'config' or 'vault'; None when it is not protected."""
    if path in {'/dev/null','dev/null'}:return None
    try:
        p=Path(os.path.expandvars(path)).expanduser()
        if not p.is_absolute():p=Path(cwd)/p
        p=p.resolve();root=Path(root).resolve()
        from .storage import config_path
        if p.is_relative_to(root/'domains'):return 'domains'
        if p in {root/'bindings.json',root/'tag_taxonomy.json',root/'projects.json',root/'.relocated.json',config_path().resolve()}:return 'config'
        # Vault edits are auto-applied and vault checkboxes approve plans, so agents must not write there either.
        return 'vault' if any(p.is_relative_to(v/folder) for v in vault_roots(root) for folder in ('Domains','Archive','Dashboards')) else None
    except (ValueError,OSError):return None

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

# Outside quotes these run, chain, redirect or expand. Parentheses open a subshell in Bash and evaluate an
# expression in PowerShell: `search (Remove-Item x)` runs the cmdlet before the script sees its arguments.
# Braces are a script block in PowerShell and expand in Bash ({a,/etc/passwd} becomes two arguments).
SHELL_META=frozenset(';|&<>`$(){}\r\n')
# PowerShell only: splatting, array expressions and here-strings.
POWERSHELL_META=frozenset('@')
# PowerShell's stop-parsing token: what follows is no longer read by the rules tracked here.
STOP_PARSING='--%'
# PowerShell reads these as quotation marks too, so a curly quote can close a string an ASCII quote opened.
POWERSHELL_QUOTES=frozenset('\u2018\u2019\u201a\u201b\u201c\u201d\u201e')
SHELL_GRAMMARS=('posix','powershell')

def shell_grammar(tool):
    """The quoting rules a shell tool's command follows: 'posix', 'powershell', or None when the tool does not say."""
    return 'posix' if tool=='bash' else 'powershell' if tool=='powershell' else None

def _words(rest,grammar):
    """The argument words of a command line that cannot run or expand anything, else None.

    Quote state is tracked per shell, because the two disagree. In Bash a backslash outside quotes makes the
    next character literal, so \\' does not open a string and '\\'' is a quote inside single-quoted text. In
    PowerShell a backslash is an ordinary character and '' is the quote inside single-quoted text.
    Single-quoted text is literal in both, so note text there may hold anything (->, |, $, newlines).
    Double-quoted text may not expand ($, `) or, in Bash, escape (a backslash before $ ` " \\ or a newline);
    other backslashes, as in Windows paths, are literal."""
    words=[];word=None;state=None;i=0;ps=grammar=='powershell'
    while i<len(rest):
        ch=rest[i];nxt=rest[i+1:i+2]
        if ps and ch in POWERSHELL_QUOTES:return None
        if state=="'":
            if ch!="'":word+=ch
            elif ps and nxt=="'":word+="'";i+=1
            else:state=None
        elif state=='"':
            if ch=='"':
                if ps and nxt=='"':word+='"';i+=1
                else:state=None
            elif ch in '$`' or (not ps and ch=='\\' and nxt in ('$','`','"','\\','\n','\r','')):return None
            else:word+=ch
        elif ch in "'\"":state=ch;word=word or ''
        elif ch=='\\' and not ps:
            if nxt in ('','\n','\r'):return None
            word=(word or '')+nxt;i+=1
        elif ch in SHELL_META or (ps and ch in POWERSHELL_META):return None
        elif ch.isspace():
            if word is not None:words.append(word);word=None
        else:word=(word or '')+ch
        i+=1
    if state is not None:return None
    if word is not None:words.append(word)
    return None if ps and STOP_PARSING in words else words

def _parsed(text,grammar=None):
    """The argument words of an exact, unchained living_memory.py invocation, once per shell grammar that
    may apply, else None. A tool that does not name its shell must be inert under both grammars."""
    match=re.match(r'\s*(?:&\s*)?"([^"]+)"\s+"([^"]+)"(.*)\Z',text,re.S)
    if not match or _norm(match[1])!=_norm(sys.executable) or _norm(match[2])!=_norm(SCRIPT):return None
    found=[_words(match[3],g) for g in ((grammar,) if grammar in SHELL_GRAMMARS else SHELL_GRAMMARS)]
    return None if any(words is None for words in found) else found

def _subcommand(words):
    index=0
    while index<len(words):
        token=words[index]
        if token in GLOBAL_OPTIONS:index+=2;continue
        if token=='--json':index+=1;continue
        return token
    return None

def memory_subcommand(text,grammar=None):
    """Subcommand of an exact, unchained living_memory.py invocation, else None. grammar: see shell_grammar."""
    parsed=_parsed(text,grammar)
    if not parsed:return None
    found={_subcommand(words) for words in parsed}
    return found.pop() if len(found)==1 else None

# Options that read a file into a note. Auto-approval covers only files the session could already see.
FILE_OPTIONS=('--content-file','--body-file','--payload-file')

def _option_values(words,names):
    """Values given to these options as `--opt VALUE` or `--opt=VALUE`, including argparse abbreviations (--content)."""
    found=[]
    for index,word in enumerate(words):
        if not word.startswith('--') or len(word)<4:continue
        name,sep,value=word.partition('=')
        if any(option.startswith(name) for option in names):found.append(value if sep else words[index+1] if index+1<len(words) else '')
    return found

def auto_allowed(text,grammar,root,cwd,scratch=None):
    """True when an allow-listed memory command may skip the permission prompt: it uses the hook's own store
    and reads input files only from the working directory or the session scratchpad. Anything else is left to
    the harness's normal permission prompt (this never denies)."""
    parsed=_parsed(text,grammar)
    if not parsed:return False
    try:
        store=Path(root).resolve();bases=[Path(cwd).resolve()]+([Path(scratch).resolve()] if scratch else [])
        for words in parsed:
            for value in _option_values(words,('--root',)):
                if not value or Path(os.path.expandvars(value)).expanduser().resolve()!=store:return False
            for value in _option_values(words,FILE_OPTIONS):
                if not value:return False
                path=Path(os.path.expandvars(value)).expanduser()
                path=(path if path.is_absolute() else bases[0]/path).resolve()
                if not any(path.is_relative_to(base) for base in bases):return False
        return True
    except (OSError,ValueError,RuntimeError):return False   # RuntimeError: ~user with no such user

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
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.sync_vaults',exc,root);return None

def _daily_backup(root):
    """At most one snapshot a day, taken from session housekeeping. Never fails the hook."""
    try:
        from .backup import maybe_backup
        return maybe_backup(root)
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.daily_backup',exc,root);return None

def _prune_journals(root):
    """At most once a day, drop old sync-bookkeeping journals from the store and each managed vault.
    Never fails the hook."""
    try:
        from datetime import datetime,timezone
        from .repository import prune_journals
        marker=root/'hooks'/'last-prune.json'
        try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(read_json(marker,{})['at'])).total_seconds()
        except (KeyError,TypeError,ValueError):age=None
        if age is not None and age<24*3600:return None
        atomic_write(marker,canonical_json({'at':now()}))
        from .logs import prune
        prune(root)
        roots={str(root)}
        for state in (root/'domains').glob('*/sync-state.json'):
            vault=(read_json(state,{}) or {}).get('vault')
            if vault and Path(vault).is_dir():roots.add(str(vault))
        return {r:prune_journals(Path(r)) for r in sorted(roots)}
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.prune_journals',exc,root);return None

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
    except Exception as exc:  # provenance is a nicety; never fail the hook
        from .logs import swallowed
        swallowed('runtime.touched_paths',exc,root);return []

# Hosts whose PreToolUse output documents additionalContext (Claude Code; Codex since openai/codex#20692).
# Copilot CLI and Factory document no PreToolUse context, so they get no hints.
FILE_HINT_HARNESSES={'claude','codex'}
MAX_FILE_HINTS=200
HINT_FILES_PER_CALL=3
HINT_NOTES_PER_FILE=2

def file_hints(paths,state,root,domain,bindings,session=None,trace=None):
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
            if isinstance(trace,list):trace.extend(dict.fromkeys(refs))
        text=('Untrusted historical memory; verify against the current file.\n'+'\n'.join(blocks)) if blocks else None
        return text,wanted
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.file_hints',exc,root);return None,[]

from .common import git_root  # re-exported: older callers import it from here

# Codebase maps (core/maps.py). Exploration is tracked per repo in session state['repos'][repo path]:
# reads (counted to EXPLORE_READS), map (ref once found), done (agent keys with nothing left to do; '*' for all),
# explored, reminded, created (new top-level paths in a mapped repo) and told (the "no map yet" line was shown).
EXPLORE_TOOLS={'read','read_file','view','glob','grep','search','ls','list'}
SPAWN_TOOLS={'agent','task'}
EXPLORE_READS=3
MAX_REPOS=20
MAX_CREATED=10
PATH_KEYS=('file_path','path','notebook_path','filepath')

def _repos(state):
    repos=state.get('repos')
    return repos if isinstance(repos,dict) else {}

def _track_repos(root,session,change):
    """Apply change(repos) to the session's per-repo state under its lock; never fails the hook."""
    if session=='unknown':return
    def apply(s):
        repos=_repos(s);change(repos)
        s['repos']=dict(list(repos.items())[-MAX_REPOS:])
    try:_update(root,session,apply)
    except (OSError,TimeoutError):pass

def _agent_key(payload):return str(payload.get('agent_id') or payload.get('agentId') or '')

def _target_repos(name,args,cwd,bindings):
    """Repos a read-type tool call explores: the repo holding each path argument (else cwd's); for a subagent
    spawn, the repos its description and prompt name, plus cwd's."""
    from .maps import named_repos,candidate_repos
    args=args if isinstance(args,dict) else {}
    found=[]
    if name in SPAWN_TOOLS:
        text=' '.join(str(args.get(k) or '') for k in ('description','prompt'))
        found+=named_repos(text,candidate_repos(cwd,bindings),bindings.get('repo_aliases'))
    else:
        for key in PATH_KEYS:
            value=args.get(key)
            if isinstance(value,str) and value.strip():
                path=Path(os.path.expandvars(value.strip())).expanduser()
                found.append(git_root(path if path.is_absolute() else Path(cwd)/path))
    here=git_root(cwd)
    if here and (name in SPAWN_TOOLS or not found):found.append(here)
    # Only repos under a bound folder: a map for any other repo would be stored where no lookup finds it.
    return [r for r in dict.fromkeys(r.resolve() for r in found if r) if resolve_domain(r,bindings)][:2]

def explore(name,args,payload,cwd,root,bindings,state,session,hints=True):
    """Track a read-type call against the repos it explores: (hint text or None, detail rows).
    A mapped repo gets one hint per agent (none for the main agent once the map was injected); an unmapped
    one counts reads until it counts as explored, which arms the Stop reminder."""
    from .maps import find_map,pointer_line
    from .notes import short_ref
    # A spawn is tracked but gets no hint: PreToolUse fires once the brief is written, so "put the map in the
    # brief" could not be acted on. Recording the map here is what lets SubagentStart hand it to the subagent.
    spawn=name in SPAWN_TOOLS
    agent=_agent_key(payload)+('#spawn' if spawn else '');shown=state.get('shown',{}) if isinstance(state.get('shown'),dict) else {}
    # The main agent's session context defines CMD; a subagent's does not, so it gets the whole command.
    cmd=hint_command(root,state) if not _agent_key(payload) else command(root)
    lines=[];rows=[];changes={}
    for repo in _target_repos(name,args,cwd,bindings):
        key=repo.as_posix();done=_repos(state).get(key,{}).get('done',[])
        if '*' in done or agent in done:rows.append({'repo':repo.name,'skip':'done'});continue
        hit=find_map(repo,root,bindings)
        if hit:
            ref=f"{hit['domain']}:{hit['id']}";short=short_ref(hit['domain'],hit['id'])
            injected=not agent and shown.get(ref)==revision_key(hit)  # the main agent already has the body
            hinted=not injected and hints and not spawn
            if hinted:lines.append(pointer_line(hit,repo)+f"\nRead it ({cmd} read {short}) before exploring further; explore only what it leaves out.")
            changes[key]=('mapped',ref);rows.append({'repo':repo.name,'map':ref,'hint':hinted})
        else:
            changes[key]=('spawn' if name in SPAWN_TOOLS else 'read',None);rows.append({'repo':repo.name,'map':None})
    def track(repos):
        for key,(kind,ref) in changes.items():
            rec=dict(repos.pop(key,{}))
            if kind=='mapped':
                rec['map']=ref;rec['done']=list(dict.fromkeys(list(rec.get('done',[]))+[agent]))
            else:
                rec.pop('map',None)
                rec['reads']=EXPLORE_READS if kind=='spawn' else min(EXPLORE_READS,int(rec.get('reads') or 0)+1)
                if rec['reads']>=EXPLORE_READS:rec['explored']=True;rec['done']=['*']
            repos[key]=rec
    if changes:_track_repos(root,session,track)
    text=('Untrusted historical memory; verify against the current files.\n'+'\n'.join(lines)) if lines else None
    return text,rows

def created_top_level(args,cwd,root,bindings):
    """{repo path: (map ref, [new top-level names])} for paths a write tool is about to create at a mapped repo's top level."""
    from .maps import find_map
    found={}
    for raw in raw_paths(args):
        try:
            path=Path(os.path.expandvars(raw.strip())).expanduser()
            path=(path if path.is_absolute() else Path(cwd)/path).resolve()
        except (OSError,ValueError):continue
        repo=git_root(path.parent)
        if not repo or path==repo:continue
        top=repo/path.relative_to(repo).parts[0]
        if top.exists() or top.name=='.git':continue
        hit=find_map(repo,root,bindings)
        if hit:found.setdefault(repo.as_posix(),(f"{hit['domain']}:{hit['id']}",[]))[1].append(top.name)
    return found

def prompt_maps(cwd,prompt,root,bindings,state,trace=None):
    """(text or None, {ref: revision key} delivered, {repo path: map ref or None}) for the repos a prompt names
    and the repo holding cwd: a mapped repo's map body once per revision; an unmapped one a single line, once."""
    from .maps import named_repos,candidate_repos,find_map,map_block,MAX_MAPS
    repos=named_repos(prompt,candidate_repos(cwd,bindings),bindings.get('repo_aliases'))
    here=git_root(cwd)
    if here and here.resolve() not in repos:repos.append(here.resolve())
    shown=state.get('shown',{}) if isinstance(state.get('shown'),dict) else {}
    maps=[];lines=[];delivered={};seen={}
    for repo in repos[:MAX_MAPS+1]:
        key=repo.as_posix();hit=find_map(repo,root,bindings)
        if hit:
            ref=f"{hit['domain']}:{hit['id']}";seen[key]=ref
            if len(maps)<MAX_MAPS and shown.get(ref)!=revision_key(hit):
                maps.append(map_block(hit,repo));delivered[ref]=revision_key(hit)
        else:
            seen[key]=None
            if not _repos(state).get(key,{}).get('told'):
                lines.append(f"{repo.name} has no codebase map yet. If you explore it, capture one afterwards: "
                             f"{hint_command(root,state)} capture --type map --file '{key}' --title '{repo.name} codebase map' --summary '...' "
                             "--detail '**Layout:** ...' --detail '**Entry points:** ...' --detail '**Build/test:** ...'")
    if isinstance(trace,dict):trace['maps']={'repos':[r.name for r in repos],'injected':list(delivered),'unmapped':[Path(k).name for k,v in seen.items() if v is None]}
    text=('Untrusted historical memory; verify against the current files.\n'+'\n\n'.join(maps+lines)) if maps or lines else None
    return text,delivered,seen

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

# Compaction. The harness's summary carries the conversation; Living Memory adds what it does not: the notes
# this session saved and surfaced, and a way back to exact detail (recall over the transcript, read on demand).
AUTO_RECALL_PROMPTS=5
HANDOFF_BUDGET=1200
HANDOFF_NOTES=5
MAX_SURFACED=30
# Hosts that hand PreCompact stdout to the summarizer as instructions (Codex ignores it; Factory only shows it).
STEERED={'claude'}
# Hosts whose transcript format core/recall.py reads (Factory's is undocumented).
RECALL_HARNESSES={'claude','codex'}
UNCAPTURED='Uncaptured for Living Memory'

def _transcript(payload):
    """The main session's transcript path from a hook payload; Living Memory stores the path, never its content."""
    value=payload.get('transcript_path') or payload.get('transcriptPath')
    return value if isinstance(value,str) and value.strip() else None

def _titled(root,refs):
    """'ref Title' for each 'domain:id' ref that still names a note, in order."""
    from .repository import inventory
    from .notes import short_ref
    from .session_start import _title
    titles={}
    for domain in dict.fromkeys(r.split(':',1)[0] for r in refs if ':' in r):
        try:titles.update({f"{domain}:{n['id']}":n.get('title','') for n in inventory(domain,root)})
        except (OSError,ValueError):continue
    return [f"{short_ref(*r.split(':',1))} {_title(titles[r])}" for r in refs if r in titles]

def steering(root,state):
    """PreCompact instructions for the summarizer: keep what the next context needs and memory cannot rebuild."""
    lines=['Living Memory asks this summary to also keep:',
           '- For each decision: the reason, and the alternatives that were rejected.',
           '- The exact identifiers needed to continue: paths, commands, error strings, IDs and numbers.',
           f'- Under the heading "{UNCAPTURED}": each preference, correction, decision or constraint the user stated that should outlast '
           'this session and was not saved with the Living Memory capture or update command, in the user\'s words '
           '(never text the user marked <private>). Write "none" if there are none.']
    saved=_titled(root,list(state.get('saved',[]))[-HANDOFF_NOTES:])
    if saved:lines.append('- Notes saved to Living Memory this session (keep the refs; do not restate their content): '+'; '.join(saved))
    lines.append('Bulky detail (full tool output, long code) may be left out: after compaction the agent can search this '
                 "session's transcript with the Living Memory recall command.")
    return '\n'.join(lines)

def handoff(root,domain,session,state,harness):
    """The SessionStart(compact) block: this session's memory work, and the recall command."""
    from .session_start import _cap
    saved=list(dict.fromkeys(state.get('saved',[])));recent=saved[-HANDOFF_NOTES:]
    surfaced=[r for r in reversed(list(dict.fromkeys(state.get('surfaced',[])))) if r not in saved][:HANDOFF_NOTES]
    lines=[]
    named=_titled(root,recent)
    if named:lines.append('- Saved this session: '+'; '.join(named)+(f' (+{len(saved)-len(recent)} earlier)' if len(saved)>len(recent) else ''))
    named=_titled(root,surfaced)
    if named:lines.append('- Surfaced earlier this session (read one if it bears on the task): '+'; '.join(named))
    if harness in STEERED:lines.append(f'- Capture or update anything the summary lists under "{UNCAPTURED}".')
    if state.get('transcript') and harness in RECALL_HARNESSES:
        lines.append(f"- Exact detail from before the compaction (code, errors, outputs, reasons): CMD recall 'query' --session {session}")
    if not lines:return ''
    return _cap('\n'.join(['Before this compaction (Living Memory adds what the summary does not carry):']+lines),HANDOFF_BUDGET)+'\n'

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
# Deterministic and conservative. A match adds one line to that prompt's context; only an explicit request to
# remember (BLOCKING_SIGNALS) can also hold the turn open at Stop, so that is the kind matched most narrowly.
QUOTED=re.compile(r'```.*?(?:```|\Z)|<(pasted_content|system-reminder|agent-message|task-notification)\b.*?(?:</\1>|\Z)|^[ \t]*>[^\r\n]*$',re.S|re.M|re.I)
# Statements are judged one at a time. Each pattern's ^ is the start of a statement.
STATEMENT_BREAK=re.compile(r'(?<=[.!?])\s+|\n+')
# A remark about this task only is not something to keep: "please don't touch the tests for now".
TRANSIENT=re.compile(r"\b(?:for now|for the moment|this time|just this once|right now|for this (?:task|pr|change|commit|session|turn))\b",re.I)
SIGNALS=(
    # "don't forget to run the tests" is an instruction for this turn, not a fact to keep.
    ('remember',re.compile(r"\b(?:remember (?:that|this|we|it)|keep in mind|for (?:the )?record|don'?t forget(?! to\b))\b",re.I)),
    ('correction',re.compile(r"^\s*(?:no|nope)\s*[,.!:;—-]|^\s*actually,?\s+(?:it'?s|it is|that'?s|the|we|i|you)\b|\bthat'?s (?:wrong|not right|incorrect|not what i)\b|\byou(?:'re| are| were) wrong\b|\bi meant\b",re.I)),
    # "always"/"never" count when they address the agent or state a habit, not inside a description
    # ("the API should always use HTTPS").
    ('preference',re.compile(r"\b(?:i(?:'d)? prefer|i'?d rather|i (?:hate|don'?t like)|from now on|going forward|in (?:the )?future|please (?:don'?t|do not|never|always)|stop doing|don'?t ever|by default,? use"
                             r"|(?:you should|you must|we) (?:always|never)|i (?:always|never) (?:use|want|like|do))\b|^\s*(?:always|never)\s+\w",re.I)),
    ('decision',re.compile(r"\b(?:let'?s (?:go with|use)|we(?:'ll| will) (?:go with|use)|we decided|i(?:'ve)? decided|go with option)\b",re.I)),
)
SIGNAL_TEXT={'remember':('asked you to remember something','--type TYPE --concern CONCERN'),
             'correction':('corrected you','--type knowledge --concern correction'),
             'preference':('stated a preference','--type preference --concern preference'),
             'decision':('made a decision','--type decision --concern decision')}
# Kinds whose missing capture decision still holds the turn open at Stop. The others are raised once, in the
# prompt's own context, where acting on them costs no extra turn.
BLOCKING_SIGNALS=frozenset({'remember'})

def durable_signal(prompt):
    """The kind of durable statement the user's own words carry ('preference', 'correction', 'decision',
    'remember'), else None. Pasted, quoted and harness-generated text is ignored, and so are questions
    ("why does it never use the cache?") and remarks about this task only."""
    if not isinstance(prompt,str) or MACHINE_PROMPT.match(prompt):return None
    statements=[s for s in STATEMENT_BREAK.split(QUOTED.sub(' ',prompt)) if s.strip() and not s.rstrip().endswith('?') and not TRANSIENT.search(s)]
    return next((kind for kind,pattern in SIGNALS if any(pattern.search(s) for s in statements)),None)

def signal_nudge(signal,session):
    """The one line a prompt with a durable statement adds to its own context."""
    said,hint=SIGNAL_TEXT[signal]
    return (f"Living Memory: the user {said} in this prompt. If it should outlast this session, update its note or "
            f"CMD capture --session {session} {hint} --title '...' --summary '...' before you finish; if not, nothing is needed.")

def hit_line(hit):
    snippet=hit.get('snippet','')
    if len(snippet)>HIT_SNIPPET:
        cut=snippet.rfind(' ',0,HIT_SNIPPET);snippet=snippet[:cut if cut>0 else HIT_SNIPPET]+' …'
    from .notes import short_ref
    return f"- [{str(hit.get('type','note')).upper()} {hit.get('updated','')}] {short_ref(hit['domain'],hit['id'])} {hit['title']} — {snippet}"

def revision_key(note):return str(note.get('revision') or '')[:12]

def _unseen(hit,shown):
    """True unless this revision of the note was already delivered. shown maps ref -> revision key;
    a legacy list of refs (or a ref without a revision) suppresses the note itself."""
    ref=f"{hit['domain']}:{hit['id']}"
    if ref not in shown:return True
    seen=shown.get(ref) if isinstance(shown,dict) else None
    return bool(seen) and seen!=revision_key(hit)

def relevance_gate(query,root,trace=None):
    """The prompt hook's relevance test as a search_domains `keep` filter: enough of the query's informative
    words in a note's title or body, or (with a semantic model) a close match in meaning. trace, when a list,
    receives one row per candidate: ref, rank, engine, engine score, relevance, similarity and the verdict."""
    from .search import query_terms,stem
    from .semantic import threshold
    count=len({stem(t) for t in query_terms(query or '')});near=threshold(root)
    def relevant(hits):
        kept=[]
        for hit in hits:
            overlap=hit.get('titleOverlap',0);title=hit.get('titleTerms',0);informative=hit.get('queryTerms',count)
            if not informative:gate='dropped:no_informative'   # only words that appear in most notes
            # One-word prompts ("continue", "yes") inject only on a title hit.
            elif informative==1:gate='kept:title' if overlap else 'dropped:one_word_no_title'
            elif hit.get('coverage',0)>=0.5 and (informative<4 or hit.get('coverage',0)>0.5):gate='kept:coverage'
            elif overlap>=2 or (title and overlap/title>=0.5):gate='kept:title'
            # Same meaning in other words ("laid out" for "layout"): only a close semantic match counts.
            elif near is not None and hit.get('similarity',0)>=near:gate='kept:semantic'
            else:gate='dropped:weak'
            if gate.startswith('kept'):kept.append(hit)
            if isinstance(trace,list):
                # keep runs before search_domains replaces score with the fused rank score
                trace.append({'ref':f"{hit['domain']}:{hit['id']}",'rank':hit.get('rank'),'engine':hit.get('engine'),
                              'engine_score':round(hit['score'],4) if isinstance(hit.get('score'),(int,float)) else None,
                              'relevance':hit.get('relevance'),'similarity':hit.get('similarity'),'gate':gate})
        return kept
    return relevant

def context(cwd,prompt,root,active=None,shown=(),overview_done=False,delivered=None,trace=None):
    """Relevant memory for a prompt: (text or None, ids injected, overview shown). A note shown earlier
    is offered again once it has a new revision. delivered, when a dict, receives ref -> revision key.
    trace, when a dict, receives the decision: the path taken, the gate's candidates and the refs injected,
    suppressed as already shown, or cut by the cap (refs and numbers only)."""
    from .search import search_domains,recent_notes,query_terms,stem
    trace=trace if isinstance(trace,dict) else {}
    bindings=load_bindings(root);active=active or resolve_domain(cwd,bindings)
    if not active or MACHINE_PROMPT.match(prompt or ''):trace['path']='machine_prompt' if active else 'unbound';return None,[],False
    query={stem(t) for t in query_terms(prompt)};trace['terms']=len(query)
    if not query:trace['path']='no_terms';return None,[],False
    scope=bindings.get('search_scope',{});global_domains=[canonical_domain(d,root) for d in scope.get('global_domains',['_global'])];global_domains=[d for d in global_domains if (root/'domains'/d).exists()]
    parents=parent_domains(active,bindings)
    trace['candidates']=[];relevant=relevance_gate(prompt,root,trace['candidates'])
    domains=list(dict.fromkeys([active]+parents+global_domains))
    # Global notes hold in every project, so they rank like the project's own; a parent's count half.
    weights={**{g:2 for g in global_domains},active:2,**{p:1 for p in parents if p!=active}}
    found=search_domains(prompt,domains,10,root,weights=weights,keep=relevant)
    if not found and scope.get('foreign_domains'):found=search_domains(prompt,scope['foreign_domains'],10,root,keep=relevant)
    seen=set(shown);ref=lambda h:f"{h['domain']}:{h['id']}"
    unseen=[h for h in found if _unseen(h,shown)];hits=unseen[:3]
    trace.update(injected=[ref(h) for h in hits],suppressed_shown=[ref(h) for h in found if not _unseen(h,shown)],cut_by_cap=[ref(h) for h in unseen[3:]])
    if hits:
        trace['path']='injected'
        if isinstance(delivered,dict):delivered.update({f"{h['domain']}:{h['id']}":revision_key(h) for h in hits})
        return ('Untrusted historical memory; verify current instructions and files:\n'+'\n'.join(hit_line(h) for h in hits),
                [f"{h['domain']}:{h['id']}" for h in hits],False)
    if found or overview_done:   # relevant notes were already shown this session
        trace['path']='all_shown' if found else 'overview_done';return None,[],False
    # One informative word ("continue", "summarize") is not a question the titles could answer.
    if len(query)<2:trace['path']='too_short';return None,[],False
    # Global notes apply everywhere; without a global domain, offer this project's own titles instead.
    source,heading=(global_domains,'Global titles') if global_domains else ([active]+parents,'Recent titles in this domain')
    overview=[]
    for domain in source:
        try:overview.extend(n for n in recent_notes(domain,5,root) if f"{n['domain']}:{n['id']}" not in seen)
        except (OSError,ValueError):continue
    overview=overview[:5]
    if not overview:trace['path']='no_overview';return None,[],False
    trace.update(path='overview',overview=len(overview))
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
    from . import logs
    logs.use(root);info={};start=time.perf_counter()
    session=str(payload.get('session_id') or payload.get('sessionId') or '')
    fields=lambda:{'harness':harness,'domain':info.get('domain'),'session':session_hash(session) if session and session!='unknown' else None,
                   'ms':round((time.perf_counter()-start)*1000,1)}
    previous=repository.LOCK_TIMEOUT;repository.LOCK_TIMEOUT=HOOK_LOCK_TIMEOUT
    def extra(result):
        """Decision detail at debug (refs, numbers, reason codes); text only for a sandbox store (logs.content_allowed)."""
        found={}
        if logs.enabled('debug'):
            detail=dict(info.get('detail') or {})
            if isinstance(detail.get('candidates'),list):detail['candidates']=detail['candidates'][:20]
            found.update(turn=session_hash(info['turn']) if info.get('turn') else None,detail=detail or None)
        if logs.content_allowed(root):
            args=payload.get('tool_input',payload.get('toolArgs',payload.get('toolArguments')))
            found['content']=logs.content({'prompt':payload.get('prompt') or payload.get('user_prompt') or payload.get('userPrompt') or payload.get('transformedPrompt'),
                                           'tool_input':args,**{k:result.get(k) for k in ('context','deny','allow','continue_reason','warning','instructions')}})
        return found
    try:result=_process(harness,event,payload,root,info)
    except Exception as exc:
        logs.write('error','hook',event,root,**fields(),**extra({}),error=logs.error_fields(exc));raise
    finally:repository.LOCK_TIMEOUT=previous
    context=result.get('context') or result.get('instructions')
    logs.write('warning' if result.get('warning') else 'info','hook',event,root,**fields(),
               outcome=sorted(k for k in ('context','deny','allow','continue_reason','warning','instructions') if result.get(k)),
               context_chars=len(context) if isinstance(context,str) else None,**extra(result))
    return result

def _process(harness,event,payload,root=None,info=None):
    info=info if info is not None else {}
    detail=info.setdefault('detail',{})  # why this hook did what it did: refs, numbers and reason codes only
    root=Path(root or memory_root()).resolve();cwd=payload.get('cwd') or payload.get('workingDirectory') or os.getcwd()
    session=str(payload.get('session_id') or payload.get('sessionId') or 'unknown');turn=str(payload.get('turn_id') or payload.get('turnId') or '')
    bindings=load_bindings(root)
    state=read_json(_pointer(root,session),{}) if session!='unknown' else {}
    if not isinstance(state,dict):state={}
    pinned=state.get('domain')
    domain=pinned if pinned and pinned in bindings.get('domains',{}) else resolve_domain(cwd,bindings)
    info['domain']=domain
    source=str(payload.get('source') or '').lower()
    transcript=None if _subagent(payload) else _transcript(payload)
    if event=='session-start' and session!='unknown':
        current=resolve_domain(cwd,bindings) or domain;domain=current;info['domain']=domain
        def pin(s):
            s['harness']=harness
            if current:s['domain']=current
            else:s.pop('domain',None)
            if transcript:s['transcript']=transcript
            # The first prompts after a compaction may ask about something only the transcript still holds.
            if source=='compact':s['recall_prompts']=AUTO_RECALL_PROMPTS;s['recalled']=[]
        state=_update(root,session,pin)
    if event=='user-prompt':
        turn=turn or str(payload.get('timestamp') or now())
        if session!='unknown':
            # Plan mode cannot save anything, so a statement there is not a reminder's business.
            planning=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan'
            text=payload.get('prompt') or payload.get('user_prompt') or payload.get('userPrompt') or payload.get('transformedPrompt') or ''
            signal=None if planning else durable_signal(text)
            from . import private
            detail.update(signal=signal,private_spans=len(private.spans(text)) if isinstance(text,str) and not MACHINE_PROMPT.match(text) else 0)
            def opened(s):
                s['turn']=turn;s['open_turns']=(list(s.get('open_turns',[]))+[turn])[-20:]
                if signal:s['signal']=signal  # only the kind is kept, never the prompt text
                # <private> spans: salted shingle hashes only, so a capture that copies one can be refused.
                if isinstance(text,str) and not MACHINE_PROMPT.match(text) and private.remember(s,text):s['private_turn']=turn
                if transcript:s['transcript']=transcript
            state=_update(root,session,opened)
    elif not turn:
        turn=state.get('turn','')
    info['turn']=turn
    result={}
    if event in {'session-start','user-prompt','stop'}:
        try:atomic_write(root/'hooks/last-delivery.json',canonical_json({'harness':harness,'event':event,'timestamp':now(),'bound':bool(domain),'cwd':str(cwd),'domain':domain,'payload_keys':sorted(payload)}))
        except OSError:pass  # Diagnostics only; never cost the session its memory context.
    if event=='session-end':
        synced=_sync_vaults(root,'session end');backup=_daily_backup(root);pruned=_prune_journals(root)
        detail['housekeeping']={'synced':len(synced.get('results',[])) if isinstance(synced,dict) else None,'backup':bool(backup),'pruned':pruned is not None}
        return {}
    if not domain:
        if event=='stop':detail['reason']='unbound'
        if event=='session-start' and session!='unknown':
            hint=bind_hint(cwd,root,bindings,state);detail['bind_hint']=bool(hint)
            if hint:
                _update(root,session,lambda s:s.update(bind_hint=True));result['context']=hint
        return result
    if event=='session-start':
        # Codex has no SessionEnd: its session start does the end-of-session housekeeping.
        if harness=='codex':_sync_vaults(root,'session start',SESSION_START_SYNC_SECONDS);_daily_backup(root);_prune_journals(root)
        from .session_start import build_context,BUDGET,MIN_BUDGET,HARD_CAP,_cap
        prefix=protocol(root,domain,harness,session)+f'\nLiving Memory session={session}'+'\n'
        # After a compaction the handoff goes first; the title index gives up the room.
        if source=='compact' and session!='unknown':
            handed=handoff(root,domain,session,state,harness);prefix+=handed;detail['handoff_chars']=len(handed)
        # A session started inside a mapped repo sees a pointer to its map; the body comes when a prompt needs it.
        pointer=session_map(cwd,root,bindings);detail['map']=bool(pointer)
        if pointer:prefix+=pointer+'\n'
        text,shown,indexed=build_context(cwd,root,max(MIN_BUDGET,BUDGET-len(prefix)),session=session,trace=detail)
        detail.update(shown=list(shown),indexed=indexed)
        if session!='unknown':
            # A new or compacted context has seen only what is injected now. Guidance shown in full is not
            # re-injected by prompts; index lines are titles, so those notes stay eligible. Once the index has
            # listed the newest titles, the prompt-time "recent titles" fallback has nothing to add.
            def started(s):
                s.pop('shown',None);s.pop('overview_shown',None)
                if shown:s['shown']=shown
                # The new context has not seen any map hint either; exploration counts and reminders stand.
                for rec in _repos(s).values():
                    if isinstance(rec,dict) and rec.get('map'):rec.pop('done',None)
                if indexed:s['overview_shown']=True
            _update(root,session,started)
            if shown:
                from .usage import record
                record(root,'inject',list(shown),session,'session')
        # BUDGET is the target with the instruction block installed; the fallback protocol and a compaction
        # handoff can exceed it, but never the harness's own limit.
        result['context']=_cap(prefix+(text or ''),HARD_CAP)
    elif event=='user-prompt':
        prompt=payload.get('prompt') or payload.get('user_prompt') or payload.get('userPrompt') or payload.get('transformedPrompt') or ''
        delivered={};maps_text,map_refs,mapped=None,{},{}
        if isinstance(prompt,str) and not MACHINE_PROMPT.match(prompt):
            try:maps_text,map_refs,mapped=prompt_maps(cwd,prompt,root,bindings,state,detail)
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.prompt_maps',exc,root)
        # A map injected now counts as shown, so the note search below does not repeat it as a hit.
        shown_now=state.get('shown',{})
        shown_now=dict(shown_now) if isinstance(shown_now,dict) else dict.fromkeys(shown_now if isinstance(shown_now,list) else [],'')
        shown_now.update(map_refs)
        if isinstance(prompt,str):hits,ids,overview=context(cwd,prompt,root,domain,shown_now,state.get('overview_shown',False),delivered,detail)
        else:hits,ids,overview=None,[],False;detail['path']='not_text'
        delivered.update(map_refs);ids=list(map_refs)+ids
        if mapped:
            def noted(repos):
                for key,ref in mapped.items():
                    rec=dict(repos.pop(key,{}))
                    if ref:rec['map']=ref
                    else:rec['told']=True
                    repos[key]=rec
            _track_repos(root,session,noted)
        if (ids or overview) and session!='unknown':
            def remember(s):
                previous=s.get('shown',{})
                previous=dict(previous) if isinstance(previous,dict) else dict.fromkeys(previous if isinstance(previous,list) else [],'')
                for ref,key in delivered.items():previous.pop(ref,None);previous[ref]=key
                s['shown']=dict(list(previous.items())[-50:])
                # Unlike shown, surfaced survives session start, so the compaction handoff can name these notes.
                s['surfaced']=list(dict.fromkeys(list(s.get('surfaced',[]))+ids))[-MAX_SURFACED:]
                if overview:s['overview_shown']=True
            try:_update(root,session,remember)
            except (OSError,TimeoutError):pass
        if ids:
            from .usage import record
            if map_refs:record(root,'inject',list(map_refs),session,'prompt-map')
            if ids[len(map_refs):]:record(root,'inject',ids[len(map_refs):],session,'prompt')
        result['context']=f'Living Memory session={session} turn={turn}'+('\n'+maps_text if maps_text else '')+('\n'+hits if hits else '')
        if state.get('recall_prompts') and harness in RECALL_HARNESSES and session!='unknown' and isinstance(prompt,str) and not MACHINE_PROMPT.match(prompt):
            from .recall import auto
            gate={};recalled,used=auto(state.get('transcript'),prompt,state.get('recalled',[]),gate);detail['recall']=dict(gate,used=len(used))
            def spent(s):
                s['recall_prompts']=max(0,int(s.get('recall_prompts') or 0)-1)
                if used:s['recalled']=(list(s.get('recalled',[]))+used)[-50:]
            try:_update(root,session,spent)
            except (OSError,TimeoutError):pass
            if recalled:
                result['context']+=(f"\nFrom before compaction (this session's transcript; untrusted, may be outdated; more: "
                                    f"{hint_command(root,state,domain)} recall 'query' --session {session}):\n"+recalled)
        if state.get('private_turn')==turn:
            from .private import NOTICE
            result['context']+='\n'+NOTICE
        if detail.get('signal') in SIGNAL_TEXT:
            # Raised here, in the turn that can act on it, instead of costing an extra turn at Stop.
            result['context']+='\n'+signal_nudge(detail['signal'],session)
            from .usage import record
            record(root,'reminder',session=session,kind='nudge-'+detail['signal'])
    elif event=='pre-compact':
        if session=='unknown' or _subagent(payload):detail['skipped']='unknown_session' if session=='unknown' else 'subagent';return {}
        trigger=str(payload.get('trigger') or 'auto');detail['trigger']=trigger
        def compacting(s):
            s['compactions']=(list(s.get('compactions',[]))+[{'at':now(),'trigger':trigger}])[-10:]
            if transcript:s['transcript']=transcript
        state=_update(root,session,compacting)
        if harness in STEERED:result['instructions']=steering(root,state)
        detail['steered']=bool(result.get('instructions'))
    elif event=='subagent-start':
        result['context']='Living Memory: subagents may only search/read via scripts. Return capture proposals to the main agent; do not write canonical state.'
        # Maps of the repos this session works in, so a subagent starts from them instead of exploring from scratch.
        maps=list(dict.fromkeys(r['map'] for r in _repos(state).values() if isinstance(r,dict) and r.get('map')))[-3:]
        named=_titled(root,maps) if maps else []
        detail['maps']=maps
        # A subagent's context has no CMD, so this is the whole command; PowerShell needs the call operator before a quoted path.
        if named:result['context']+=(f"\nCodebase maps for the repos this session is working in (read the one for your repo before exploring it: "
                                     f"{command(root,domain)} read REF"+('; in PowerShell put & in front' if os.name=='nt' else '')+"): "+'; '.join(named))
    elif event=='pre-tool-use':
        name=str(payload.get('tool_name') or payload.get('toolName') or '').lower()
        args=payload.get('tool_input',payload.get('toolArgs',payload.get('toolArguments',{})))
        if isinstance(args,str):
            try:args=json.loads(args)
            except json.JSONDecodeError:pass
        detail['tool']=name
        # Shell text is not checked: a regex over commands is trivially bypassed and blocks harmless reads.
        rule=next((r for r in (protected_rule(p,cwd,root) for p in raw_paths(args)) if r),None) if name not in READ_TOOLS and name not in SHELL_TOOLS else None
        if rule:
            detail['decision']='deny:protected:'+rule
            result['deny']='Use Living Memory scripts for canonical mutations; direct file edits bypass revision checks and receipts.'
            return result
        text=shell_command(args) if name in SHELL_TOOLS else '';grammar=shell_grammar(name)
        subcommand=memory_subcommand(text,grammar) if text else None;detail['subcommand']=subcommand
        if subcommand in MUTATING_SUBCOMMANDS and _subagent(payload):
            detail['decision']='deny:subagent'
            result['deny']='Living Memory: subagents are read-only. Return the proposed note to the main agent instead of running '+subcommand+'.'
            return result
        if subcommand in ALLOWED_SUBCOMMANDS:
            # Another store, or an input file from outside the project, is the user's call: no auto-approval.
            if auto_allowed(text,grammar,root,cwd,payload.get('scratchpad_dir') or payload.get('scratchpadDir')):
                result['allow']='Living Memory read/capture script command.'
            else:detail['unconfined']=True
        detail['decision']='allow' if result.get('allow') else 'none'
        # Plan mode only permits writing the harness's own plan file, so it never changes project files.
        planning=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan'
        skip=('read_tool' if name in READ_TOOLS else 'memory_cmd' if subcommand else 'readonly_shell' if name in SHELL_TOOLS and readonly_shell(text)
              else 'subagent' if _subagent(payload) else 'plan_mode' if planning else 'scratch' if name not in SHELL_TOOLS and _scratch_only(args,payload,cwd)
              else 'unknown_session' if session=='unknown' else None)
        detail['mutation']='skipped:'+skip if skip else 'tracked'
        # Reads, searches and subagent spawns explore a repo: point them at its map, or count toward a map reminder.
        if session!='unknown' and ((skip=='read_tool' and (name in EXPLORE_TOOLS or name in SPAWN_TOOLS)) or skip=='readonly_shell'):
            try:
                hint,detail['explore']=explore(name,args if skip=='read_tool' else {},payload,cwd,root,bindings,state,session,hints=harness in FILE_HINT_HARNESSES)
                if hint:result['context']=hint
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.explore',exc,root)
        if not skip and name not in SHELL_TOOLS:
            try:created=created_top_level(args,cwd,root,bindings)
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.created_top_level',exc,root);created={}
            if created:
                detail['created_top_level']={Path(k).name:names for k,(_,names) in created.items()}
                def grew(repos):
                    for key,(ref,names) in created.items():
                        rec=dict(repos.pop(key,{}));rec['map']=ref
                        rec['created']=list(dict.fromkeys(list(rec.get('created',[]))+names))[:MAX_CREATED];repos[key]=rec
                _track_repos(root,session,grew)
        if not skip:
            paths=touched_paths(args,payload,cwd,root,domain,bindings,shell=name in SHELL_TOOLS)
            detail['touched']=len(paths);detail['hints']=[]
            hints,looked=file_hints(paths,state,root,domain,bindings,session,detail['hints']) if harness in FILE_HINT_HARNESSES else (None,[])
            if hints:result['context']=hints
            if not state.get('mutating') or set(paths)-set(state.get('touched',[])) or looked:
                def touch(s):
                    s['mutating']=True
                    s['touched']=list(dict.fromkeys(list(s.get('touched',[]))+paths))[:MAX_TOUCHED]
                    s['touched_session']=list(dict.fromkeys(list(s.get('touched_session',[]))+paths))[-MAX_TOUCHED_SESSION:]
                    if looked:s['file_hints']=list(dict.fromkeys(list(s.get('file_hints',[]))+looked))[-MAX_FILE_HINTS:]
                _update(root,session,touch)
    elif event=='stop':
        if session=='unknown':detail['reason']='unknown_session';return {}
        _record_last_session(root,domain,session,state)
        turns=list(dict.fromkeys(list(state.get('open_turns',[]))+([turn] if turn else [])))
        def close(s):
            s.pop('open_turns',None);s.pop('mutating',None);s.pop('signal',None);s.pop('touched',None)
        signal=state.get('signal');detail.update(turns=len(turns),mutating=bool(state.get('mutating')),signal=signal)
        # Plan mode cannot save, so a repo explored there is reminded about at the next Stop outside plan mode.
        planning=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan'
        try:pending=[] if planning else explore_pending(root,bindings,state)
        except Exception as exc:
            from .logs import swallowed
            swallowed('runtime.explore_pending',exc,root);pending=[]
        detail['explore_pending']=[(Path(k).name,kind) for k,kind,_ in pending]
        # File changes alone end the turn quietly: the agent already decides silently at the end of each turn,
        # and Claude Code prints every Stop continuation in the user's terminal. A preference, correction or
        # decision was raised in its own prompt's context, so it ends quietly too. Two things still hold the turn:
        # an explicit request to remember that got no capture decision, and a repo explored without a codebase
        # map (or grown at its top level), because rebuilding that orientation costs a whole exploration.
        asked=signal if signal in BLOCKING_SIGNALS else None
        quiet=('receipt' if any(capture_status(session,t,root) for t in turns) else 'no_change' if not (state.get('mutating') or signal or pending)
               else 'no_turns' if not turns else None if asked or pending else 'nudged' if signal in SIGNAL_TEXT else 'files_only')
        if quiet:
            detail['reason']=quiet;_update(root,session,close);return {}
        latest=turns[-1];reminder=root/'.turns'/(turn_key(session,latest)+'.reminded')
        if payload.get('stop_hook_active') or payload.get('stopHookActive') or reminder.exists():
            detail['reason']='stop_active' if payload.get('stop_hook_active') or payload.get('stopHookActive') else 'already_reminded';_update(root,session,close);return {}
        atomic_write(reminder,'reminded\n')
        def reminded(s):
            # The reminder settles these turns: a later turn is not reminded again about this one's statement
            # or changes, even when the harness sends no stop_hook_active follow-up. The turn's touched files
            # stay for a capture that answers the reminder, and `reminded` lets that capture count as a signal capture.
            s.pop('open_turns',None);s.pop('mutating',None);s.pop('signal',None)
            s['reminded']={'turn':latest,'signal':signal}
        _update(root,session,reminded)
        if pending:
            def settled(repos):
                # One map reminder per repo per session; the new top-level paths it named are settled too.
                for key,kind,_ in pending:
                    rec=dict(repos.pop(key,{}))
                    if kind=='unmapped':rec['reminded']=True
                    else:rec.pop('created',None)
                    repos[key]=rec
            _track_repos(root,session,settled)
        from .usage import record
        kind=asked or ('explored' if any(k=='unmapped' for _,k,_ in pending) else 'grown')
        detail['reason']='reminded:'+kind
        record(root,'reminder',session=session,kind=kind)
        # Short on purpose: Claude Code shows it in the terminal. CMD is defined by the session context, and
        # --turn defaults to the session's latest prompt, which is this one.
        maps=' '.join(map_sentence(key,k,data,session) for key,k,data in pending)
        finalize=f"otherwise CMD capture-finalize --session {session}."
        if asked:
            said,hint=SIGNAL_TEXT[asked]
            result['continue_reason']=(f"Living Memory: the user {said}{' (files changed too)' if state.get('mutating') else ''}. "
                                       f"Durable: update its note or CMD capture --session {session} {hint} --title '...' --summary '...'; "
                                       +finalize+(' Also: '+maps if maps else ''))
        else:result['continue_reason']=f"Living Memory: {maps} {finalize[0].upper()+finalize[1:]}"
    return result

def explore_pending(root,bindings,state):
    """[(repo path, 'unmapped' | 'grown', data)] the Stop reminder should raise: a repo explored without a map
    (one captured since quiets it), or new top-level paths in a mapped repo."""
    from .maps import find_map
    pending=[]
    for key,rec in _repos(state).items():
        if not isinstance(rec,dict):continue
        if rec.get('explored') and not rec.get('reminded') and not rec.get('map') and not find_map(Path(key),root,bindings):
            pending.append((key,'unmapped',None))
        elif rec.get('created') and rec.get('map'):pending.append((key,'grown',(rec['map'],list(rec['created']))))
    return pending[:3]

def map_sentence(key,kind,data,session):
    from .notes import short_ref
    name=Path(key).name
    if kind=='unmapped':
        return (f"this turn explored {name}, which has no codebase map. If that would spare a future session re-exploring it: "
                f"CMD capture --session {session} --type map --file '{key}' --title '{name} codebase map' --summary '...' "
                "--detail '**Layout:** ...' (and Entry points, Key modules, Data flow, Build/test/run, Conventions).")
    ref,names=data;short=short_ref(*ref.split(':',1))
    return (f"new top-level paths in {name} ({', '.join(names)}): if they change its layout, update map {short} "
            f"(CMD read {short}, then CMD update {short} --session {session} --set-detail '**Layout:** ...' for each bullet that changed).")

def session_map(cwd,root,bindings):
    """A pointer to the map of the repo holding cwd, else None; never fails the hook."""
    try:
        from .maps import find_map,pointer_line
        here=git_root(cwd)
        hit=find_map(here,root,bindings) if here else None
        return pointer_line(hit,here) if hit else None
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.session_map',exc,root);return None
