"""Shared deterministic lifecycle behavior; adapters translate native wire formats."""
from __future__ import annotations
import hashlib,json,os,re,shlex,sys,time
from pathlib import Path
from .common import memory_root,load_bindings,resolve_domain,atomic_write,canonical_domain,file_lock,head_oid
from .repository import read_json,canonical_json,now
from .capture import status as capture_status,turn_key
from .note_types import NOTE_TYPES

from .instructions import PROTOCOL,AUTO_MEMORY,usage,installed
SCRIPT=Path(__file__).resolve().parents[1]/'living_memory.py'
# Tools that cannot change files; anything else marks the turn as mutating.
READ_TOOLS={'read','read_file','glob','grep','search','view_image','view','ls','list','webfetch','websearch','web_fetch','web_search','todowrite','task','agent','toolsearch','askuserquestion','exitplanmode','enterplanmode'}
SHELL_TOOLS={'bash','powershell','shell','run_shell_command','exec_command','local_shell','execute'}
ALLOWED_SUBCOMMANDS={'ask','sources','source-sync','ingest-scan','search','read','taxonomy','doctor','history','logs','inspect-plan','capture','capture-finalize','validate','update','supersede','review','backup','open','check','stats','confirm','timeline','recall','promote-plan','projects','systems','changes','views','resolve','reopen','learn','log'}
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
    dynamic=(f'CMD = {cmd}   (Bash: CMD as is; PowerShell: & CMD ...)'
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
        if p.is_relative_to(root/'ledger') or p.is_relative_to(root/'.views'):return 'config'
        if p in {root/'bindings.json',root/'tag_taxonomy.json',root/'projects.json',root/'systems.json',root/'sources.json',root/'changes.json',root/'views.json',root/'.relocated.json',config_path().resolve()}:return 'config'
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
    """The argument words of an exact, unchained living_memory.py invocation (interpreter and script paths each double- or single-quoted, an optional leading `&`), once per shell grammar that
    may apply, else None. A tool that does not name its shell must be inert under both grammars."""
    match=re.match(r'''\s*(?:&\s*)?(?:"([^"]+)"|'([^']+)')\s+(?:"([^"]+)"|'([^']+)')(.*)\Z''',text,re.S)   # each path double- or single-quoted: literal in both shells
    if not match or _norm(match[1] or match[2])!=_norm(sys.executable) or _norm(match[3] or match[4])!=_norm(SCRIPT):return None
    found=[_words(match[5],g) for g in ((grammar,) if grammar in SHELL_GRAMMARS else SHELL_GRAMMARS)]
    return None if any(words is None for words in found) else found

def _subcommand(words):
    index=0
    while index<len(words):
        token=words[index]
        if token in GLOBAL_OPTIONS:index+=2;continue
        if token=='--json':index+=1;continue
        return token
    return None

def learn_operation(text,grammar=None):
    """(entity, action) of an exact, unchained `learn` invocation; (None, None) when absent."""
    parsed=_parsed(text,grammar)
    if not parsed:return None,None
    words=parsed[0];index=0;found=[]
    while index<len(words) and len(found)<3:
        token=words[index]
        if token in GLOBAL_OPTIONS:index+=2;continue
        if token.startswith('--'):index+=1;continue
        found.append(token);index+=1
    return (found[1] if len(found)>1 else None),(found[2] if len(found)>2 else None)

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

def remote_sync(text,grammar,root):
    """True when an exact `source-sync` command would reach the network: --all-remote (or an abbreviation of it) or the name of a
    registered confluence or notion source. Plain `source-sync` never does (it only re-indexes what remote caches hold)."""
    parsed=_parsed(text,grammar)
    if not parsed:return False
    try:
        from . import sources
        remote={s['name'] for s in sources.load(root)['sources'] if s.get('kind') in sources.REMOTE}
    except (ValueError,OSError):remote=set()   # without a readable registry no remote source resolves either
    for words in parsed:
        index=0
        while index<len(words) and words[index]!='source-sync':index+=2 if words[index] in GLOBAL_OPTIONS else 1
        for word in words[index+1:]:
            if word in remote or (word.startswith('--a') and '--all-remote'.startswith(word.split('=')[0])):return True
    return False

def views_write(text,grammar):
    """True when a `views` command writes: add, remove, published, or render except `--format json` with no --out."""
    for words in _parsed(text,grammar) or []:
        index=0
        while index<len(words) and words[index]!='views':index+=2 if words[index] in GLOBAL_OPTIONS else 1
        rest=words[index+1:]
        if not rest:continue
        action=next((w for w in rest if not w.startswith('-')),'list')
        if action in {'add','remove','published'}:return True
        if action=='render' and (_option_values(rest,('--out',)) or 'json' not in _option_values(rest,('--format',))):return True
    return False

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
            # Input files are read into a note; --out (views render) writes a file. Both stay inside what the session can see.
            for value in _option_values(words,FILE_OPTIONS+('--out',)):
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
        from .repository import prune_journals,prune_plans
        marker=root/'hooks'/'last-prune.json'
        try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(read_json(marker,{})['at'])).total_seconds()
        except (KeyError,TypeError,ValueError):age=None
        if age is not None and age<24*3600:return None
        atomic_write(marker,canonical_json({'at':now()}))
        from .logs import prune
        prune(root)
        # Session state and the usage diary (.turns/) keep 30 days, also for users who rarely finalize a capture.
        from .capture import prune as prune_turns
        prune_turns(root)
        roots={str(root)}
        for state in (root/'domains').glob('*/sync-state.json'):
            vault=(read_json(state,{}) or {}).get('vault')
            if vault and Path(vault).is_dir():roots.add(str(vault))
        out={r:prune_journals(Path(r)) for r in sorted(roots)}
        prune_plans(root)
        return out
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.prune_journals',exc,root);return None

AUTO_RESOLVE_DAYS=3

def auto_resolve(root,domain,bindings,session=None):
    """Resolve the active domain's finished work: each active plan, bug or risk note whose Action Items are all done and that nobody has touched
    for AUTO_RESOLVE_DAYS days. At most once a day per domain (hooks/last-auto-resolve.json, like _prune_journals), never in a headless run,
    and off with `"auto_resolve": false` at the top level of bindings.json. Each resolve is the journaled, undoable `resolve` change (`reopen REF`
    brings the note back) and is recorded as use. Returns [(short ref, title)] for the notes resolved; fails open: []."""
    try:
        from datetime import datetime,timedelta,timezone
        if bindings.get('auto_resolve',True) is False or headless():return []
        marker=root/'hooks'/'last-auto-resolve.json'
        marks=read_json(marker,{});marks=marks if isinstance(marks,dict) else {}
        try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(marks[domain])).total_seconds()
        except (KeyError,TypeError,ValueError):age=None
        if age is not None and age<24*3600:return []
        atomic_write(marker,canonical_json({**marks,domain:now()}))
        from .note_format import action_items,ACTION_TYPES
        from .notes import change,short_ref
        from .repository import inventory
        from .usage import record
        cutoff=datetime.now(timezone.utc)-timedelta(days=AUTO_RESOLVE_DAYS);done=[]
        for note in inventory(domain,root):
            if note.get('type') not in ACTION_TYPES or not note.get('id') or not note.get('title'):continue
            items=action_items(note.get('body',''))
            if not items or not all(d for d,_ in items):continue
            try:
                stamp=datetime.fromisoformat(str(note.get('updated') or note.get('created') or ''))
                if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
            except ValueError:continue
            if stamp>cutoff:continue
            try:change(note['id'],domain,'resolve',root,expected_revision=note.get('revision'))
            except (OSError,ValueError,TimeoutError) as exc:   # one stale or locked note must not stop the rest
                from .logs import swallowed
                swallowed('runtime.auto_resolve',exc,root);continue
            record(root,'resolve',[f"{domain}:{note['id']}"],session,'auto-resolve');done.append((short_ref(domain,note['id']),note['title']))
        return done
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.auto_resolve',exc,root);return []

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
MAX_HINTED=100
FOLLOW_BODY_TYPES={'bug','decision'}
MAX_DOCS=10
MAX_DOCS_REMINDED=50

def file_hints(paths,state,root,domain,bindings,session=None,trace=None,follow=None,digested=None):
    """(context naming notes about files about to be changed, paths looked up). Each file is looked up
    once per session; a missing or stale index gives no hint rather than slowing the tool call. follow, when a
    dict, receives path -> refs of the notes a later update should be asked of: those listing the file, and
    bug or decision notes naming it in the body. digested, when a list, receives the refs that are digests of an edited file."""
    try:
        from .search import file_matches
        from . import digests
        seen=set(state.get('file_hints',[]));wanted=[p for p in paths if p not in seen][:HINT_FILES_PER_CALL]
        blocks=[];refs=[];found=scoped_file_hits(wanted,domain,bindings,root,HINT_NOTES_PER_FILE)
        for path in wanted:
            hits=found.get(path)
            if hits:
                if isinstance(digested,list):   # read before mark() drops `sources`; only digests get the "digest" wording at Stop
                    digested+=[f"{h['domain']}:{h['id']}" for h in hits if file_matches([s['path'] for s in h.get('sources') or []],h.get('file',path))]
                digests.mark(hits,root,bindings)
                blocks.append(f'Living Memory notes about {path}:\n'+'\n'.join(hit_line(h) for h in hits))
                refs+=[f"{h['domain']}:{h['id']}" for h in hits]
                if isinstance(follow,dict):
                    owed=[f"{h['domain']}:{h['id']}" for h in hits if h.get('match')=='files' or h.get('type') in FOLLOW_BODY_TYPES]
                    if owed:follow[path]=owed
        if refs:
            from .usage import record
            record(root,'inject',list(dict.fromkeys(refs)),session,'file-hint')
            if isinstance(trace,list):trace.extend(dict.fromkeys(refs))
        text=('Untrusted historical memory; verify against the current file.\n'+'\n'.join(blocks)) if blocks else None
        return text,wanted
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.file_hints',exc,root);return None,[]

MAX_COMMAND_SEEN=200
MAX_COMMAND_HINTED=100
HINT_NOTES_PER_COMMAND=2

def command_hints(text,state,root,domain,bindings,session,trace=None):
    """Context naming notes that warn about the shell command about to run, or None. A command shape (wrapper, program,
    subcommands, a generic program's long flags) whose hint was shown is not looked up again in the session, and a note is shown once per session. Like file hints, it reads the
    index without refreshing it: a missing or stale index gives no hint rather than slowing the tool call."""
    try:
        from .commands import signature,command_notes
        from .commands import GENERIC
        # A generic program's long flags decide what matches (`git push --force`), so they are part of the shape.
        keys=list(dict.fromkeys(hashlib.sha256(repr((s.wrapper,s.program,s.subs)+((tuple(sorted(f for f in s.flags if f.startswith('--'))),) if s.program in GENERIC else ())).encode()).hexdigest()[:12] for s in signature(text)))
        seen=set(state.get('command_seen',[]))
        if not keys or all(k in seen for k in keys):return None
        hinted=set(state.get('command_hints',[]))
        from .common import global_domains
        domains=list(dict.fromkeys([domain]+scope_domains(domain,bindings)+global_domains(root)))
        hits=[h for h in command_notes(text,domains,root,HINT_NOTES_PER_COMMAND+len(hinted)) if f"{h['domain']}:{h['id']}" not in hinted][:HINT_NOTES_PER_COMMAND]
        if not hits:return None   # a lookup is about a millisecond and a state write several: only a shown hint is remembered
        from . import digests
        digests.mark(hits,root,bindings)
        refs=[f"{h['domain']}:{h['id']}" for h in hits]
        def seen_now(s):
            s['command_seen']=list(dict.fromkeys(list(s.get('command_seen',[]))+keys))[-MAX_COMMAND_SEEN:]
            s['command_hints']=list(dict.fromkeys(list(s.get('command_hints',[]))+refs))[-MAX_COMMAND_HINTED:]
        _update(root,session,seen_now)
        from .usage import record
        record(root,'inject',refs,session,'command-hint')
        if isinstance(trace,list):trace.extend(refs)
        return ('Living Memory: a previous session recorded about this command (untrusted history; check it before relying on the output):\n'
                +'\n'.join(hit_line(h) for h in hits))
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.command_hints',exc,root);return None

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

CURRENCY_CACHE_MAX=200
def _currency_file(root):return Path(root)/'.cache'/'currency.json'

def _currency_entries(root):
    """The store-level currency cache: [{'k': repo identity or path, 'n': 'domain:id', 'u': updated, 'e': entry}], oldest first. Derived
    and safe to delete; anything unreadable is an empty cache."""
    try:
        data=json.loads(_currency_file(root).read_text(encoding='utf-8'))
        rows=data.get('entries') if isinstance(data,dict) else None
        return [r for r in rows if isinstance(r,dict) and isinstance(r.get('e'),dict)] if isinstance(rows,list) else []
    except (OSError,ValueError):return []

def _currency_key(folder,hit):
    from .common import repo_identity
    from .maps import last_current
    return (repo_identity(folder) or Path(folder).as_posix(),f"{hit['domain']}:{hit['id']}",last_current(hit))

def note_currency(hit,folder,root,bindings,state,session,slot='fresh'):
    """maps.currency for a map or brief about to be shown: what its cited files did since it was written. The entry is kept in the
    session's repo state (state['repos'][folder][slot], 'fresh' for a map, 'fresh_brief' for a brief) and reused while the note and
    HEAD are the same, so repeated pointers run no git; a stale one is what the Stop reminder asks about. Every computed entry is also
    kept in the store's .cache/currency.json (200 entries, keyed by repo identity or path, note and its verification baseline, valid for the
    HEAD and the note fingerprint (maps.fingerprint: baseline, body, files, repo; not tags) it was computed at), so a new session with the same HEAD runs no git either. {} when anything fails."""
    try:
        from .maps import currency
        if folder is None:return {}
        key=Path(folder).as_posix();cache=(_repos(state).get(key) or {}).get(slot);cache=cache if isinstance(cache,dict) else None
        from .maps import fingerprint
        head=head_oid(folder);rev=str(hit.get('revision') or '');fp=fingerprint(hit);shared=None
        if cache and (cache.get('fp'),cache.get('head'))!=(fp,head):cache=None
        if head and rev and not cache:
            k,n,u=_currency_key(folder,hit)
            shared=next((r['e'] for r in reversed(_currency_entries(root)) if (r.get('k'),r.get('n'),r.get('u'))==(k,n,u) and r['e'].get('fp')==fp and r['e'].get('head')==head),None)
        entry,computed=currency(hit,Path(folder),bindings,cache or shared)
        if entry.get('failed'):return entry   # git could not tell: neither the shared cache nor the session keeps it, so the next call retries
        if computed and head and entry.get('head') and rev:
            try:
                k,n,u=_currency_key(folder,hit)
                rows=[r for r in _currency_entries(root) if (r.get('k'),r.get('n'))!=(k,n)]+[{'k':k,'n':n,'u':u,'f':key,'e':entry}]
                atomic_write(_currency_file(root),json.dumps({'v':1,'entries':rows[-CURRENCY_CACHE_MAX:]},separators=(',',':')))
            except (OSError,ValueError,TypeError,KeyError):pass
        refreshed=bool(cache) and cache.get('rev')!=entry.get('rev')   # a retag or rename: same finding, new revision (no git ran), and the Stop reminder compares revisions
        if (computed or shared or refreshed) and entry.get('head') and session and session!='unknown':
            stored=dict(entry,ref=f"{hit['domain']}:{hit['id']}")
            def save(s):
                repos=_repos(s);rec=dict(repos.pop(key,{}));rec[slot]=stored;repos[key]=rec
                s['repos']=dict(list(repos.items())[-MAX_REPOS:])
            try:_update(root,session,save)
            except (OSError,TimeoutError):pass
        return entry
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.note_currency',exc,root);return {}

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
            if hinted:lines.append(pointer_line(hit,repo,entry=note_currency(hit,repo,root,bindings,state,session))+f"\nRead it ({cmd} read {short}) before exploring further; explore only what it leaves out.")
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

def prompt_maps(cwd,prompt,root,bindings,state,trace=None,briefed=None,session=None):
    """(text or None, {ref: revision key} delivered, {repo path: map ref or None}) for the repos a prompt names
    and the repo holding cwd: a mapped repo's map body once per revision, followed by one-line summaries of the notes
    about that repo (refs appended to briefed); an unmapped one a single line, once."""
    from .maps import named_repos,candidate_repos,find_map,map_block,repo_name,repo_notes,MAX_MAPS
    briefed=[] if briefed is None else briefed
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
                maps.append(map_block(hit,repo,entry=note_currency(hit,repo,root,bindings,state,session)));delivered[ref]=revision_key(hit)
                lines_,refs_=repo_notes(hit,repo,root,bindings,{**_snippeted(state),**shown},exclude=briefed)
                if lines_:maps[-1]+='\n'+lines_;briefed.extend(refs_)
        else:
            seen[key]=None
            if not _repos(state).get(key,{}).get('told'):
                lines.append(f"{repo_name(repo)} has no codebase map yet. If you explore it, capture one afterwards: "
                             f"{hint_command(root,state)} capture --type map --file '{key}' --title '{repo_name(repo)} codebase map' --summary '...' "
                             "--detail '**Layout:** ...' --detail '**Entry points:** ...' --detail '**Build/test:** ...'")
    if isinstance(trace,dict):trace['maps']={'repos':[r.name for r in repos],'injected':list(delivered),'unmapped':[Path(k).name for k,v in seen.items() if v is None]}
    text=('Untrusted historical memory; verify against the current files.\n'+'\n\n'.join(maps+lines)) if maps or lines else None
    return text,delivered,seen

BRIEF_NOTES_BUDGET=600

def prompt_briefs(cwd,root,bindings,state,trace=None,briefed=None,session=None):
    """(brief block or None, {ref: revision key} delivered): the brief of the project folder holding cwd, once
    per revision per session, followed by one-line summaries of the notes it links to (refs appended to briefed)."""
    from .briefs import find_brief,brief_block,brief_folder
    from .maps import repo_notes
    briefed=[] if briefed is None else briefed
    hit=find_brief(cwd,root,bindings)
    shown=state.get('shown',{}) if isinstance(state.get('shown'),dict) else {}
    ref=f"{hit['domain']}:{hit['id']}" if hit else None
    fresh=bool(hit) and shown.get(ref)!=revision_key(hit)
    if isinstance(trace,dict):trace['brief']={'ref':ref,'injected':[ref] if fresh else []}
    if not fresh:return None,{}
    block=brief_block(hit,entry=note_currency(hit,brief_folder(hit,cwd,bindings),root,bindings,state,session,'fresh_brief'))
    lines_,refs_=repo_notes(hit,None,root,bindings,{**_snippeted(state),**shown},budget=BRIEF_NOTES_BUDGET,files=False,exclude=briefed)
    if lines_:block+='\n'+lines_;briefed.extend(refs_)
    return block,{ref:revision_key(hit)}

# Source excerpts (core/sources.py): when notes answer a prompt poorly, the project's own docs and code may.
SOURCE_EXCERPTS=2
SOURCE_BUDGET=800
SOURCE_GATE=0.67
MAX_SOURCES_SHOWN=50

def prompt_sources(prompt,root,domain,state,trace=None):
    """(context block or None, [keys shown]): up to SOURCE_EXCERPTS excerpts from the domain's knowledge-base index, SOURCE_BUDGET
    characters in all. The prompt's most informative terms (the long-prompt machinery, with the index's own vocabulary) must
    be carried by the chunk: SOURCE_GATE of their weight and at least two terms (three for a long prompt). An excerpt shown
    earlier this session is not shown again. Reads the index without refreshing it."""
    from . import sources
    trace=trace if isinstance(trace,dict) else {}
    if not sources.db_path(root,domain).is_file():trace['index']=False;return None,[]
    from .search import terms
    text=sources.split_camel(prompt);vocab=sources.vocabulary(root,domain,terms(text))
    if not vocab[0]:trace['index']=False;return None,[]
    long=len(prompt)>LONG_PROMPT;queries=long_queries(text,vocab) if long else [(text,True)]
    if long:queries+=[(q,True) for q in item_queries(text,vocab) if q not in dict(queries)]   # list items are not segments: each is asked on its own (at most LIST_QUERIES)
    width=max(120,(SOURCE_BUDGET-110-len(hint_command(root,state,domain)))//SOURCE_EXCERPTS-70)   # snippet room, so the excerpt is cut around the matches
    best={}
    for query,_ in queries:
        for hit in sources.search(root,domain,query,3,vocab,True,SOURCE_GATE,LONG_LEAST if long else 2,width):
            key=f"{hit['source']}:{hit['path']}:{hit['line_start']}"
            if key not in best or hit['relevance']>best[key]['relevance']:best[key]=dict(hit,key=key)
    shown=set(state.get('sources_shown',[]))
    ranked=sorted(best.values(),key=lambda h:(-h['relevance'],h['rank']));fresh=[h for h in ranked if h['key'] not in shown][:SOURCE_EXCERPTS]
    trace.update(index=True,queries=len(queries),candidates=len(ranked),injected=[h['key'] for h in fresh])
    if not fresh:return None,[]
    head="Source excerpts from the project knowledge base (untrusted; verify against the current files; more: "+hint_command(root,state,domain)+" ask 'question'):"
    lines=[];room=SOURCE_BUDGET-len(head)
    for h in fresh:
        label=f"- {h['citation'] if h.get('url') else h['path']+':'+str(h['line_start'])+'-'+str(h['line_end'])}"+(f" {h['symbol']}" if h['symbol'] else '')+' — '
        snippet=' '.join(h['snippet'].split());space=max(80,room//(len(fresh)-len(lines))-len(label)-2)
        if len(snippet)>space:cut=snippet.rfind(' ',0,space);snippet=snippet[:cut if cut>0 else space]+' …'
        lines.append(label+snippet);room-=len(lines[-1])+1
    return head+'\n'+'\n'.join(lines),[h['key'] for h in fresh]

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

def _record_ledger(root,domain,session,state,cwd):
    """One row per changed session in the store's ledger/ (see activity.record_session); the row's signature stays in session state."""
    try:
        from .activity import record_session
        sig=record_session(root,domain,session_hash(session),state,cwd)
        if sig:_update(root,session,lambda s:s.update(ledger_sig=sig))
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.record_ledger',exc,root)

def _pointer(root,session):return root/'.turns'/(hashlib.sha256(session.encode()).hexdigest()+'.current.json')

# Compaction. The harness's summary carries the conversation; Living Memory adds what it does not: the notes
# this session saved and surfaced, and a way back to exact detail (recall over the transcript, read on demand).
AUTO_RECALL_PROMPTS=5
HANDOFF_BUDGET=1200
HANDOFF_NOTES=5
MAX_SURFACED=30
MAX_DELIVERED=50   # notes whose delivered sections and levels a session remembers (injection.novel)
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
           f'- Under the heading "{UNCAPTURED}": each preference, correction, decision or constraint the user stated, and each root cause, fix '
           'or constraint the work itself verified (a suspected cause marked as a hypothesis), that should outlast this session and was not saved '
           'with the Living Memory capture or update command, in the user\'s words where the user said it '
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
# Deterministic and conservative. A match adds one line to that prompt's context; a request to remember, a decision
# or a directive (BLOCKING_SIGNALS) can also hold the turn open at Stop, so those are matched most narrowly.
# A prompt can carry several kinds: they are tried in priority order (remember, decision, directive, correction,
# preference), so a blocking kind is never hidden by a non-blocking one ("No, we decided to use X").
# Fenced code and machine tags are never read. Pasted blocks and blockquotes (a memo, a ticket) are read only for a directive.
IGNORED=re.compile(r'```.*?(?:```|\Z)|<(system-reminder|agent-message|task-notification)\b.*?(?:</\1>|\Z)',re.S|re.I)
QUOTED=re.compile(r'<pasted_content\b[^>]*>(.*?)(?:</pasted_content>|\Z)|^[ \t]*>[ \t]*([^\r\n]*)$',re.S|re.M|re.I)
QUOTED_CHARS=2000
# Statements are judged one at a time. Each pattern's ^ is the start of a statement.
STATEMENT_BREAK=re.compile(r'(?<=[.!?])\s+|\n+')
# A remark about this task only is not something to keep: "please don't touch the tests for now".
TRANSIENT=re.compile(r"\b(?:for now|for the moment|this time|just this once|right now|for this (?:task|pr|change|commit|session|turn))\b",re.I)
SIGNALS=(
    # "don't forget to run the tests" is an instruction for this turn, not a fact to keep.
    ('remember',re.compile(r"\b(?:remember (?:that|this|we|it)|keep in mind|for (?:the )?record|don'?t forget(?! to\b))\b",re.I)),
    # A bare "must", "approved" or "regulator" describes the task, so only a statement addressed to the team
    # ("we must never ...") or a decision's own wording counts. "You must not ..." and "you are not allowed to ..."
    # constrain the agent's task; they are not a decision of the team.
    ('decision',re.compile(r"\b(?:let'?s (?:go with|use)|we(?:'ll| will) (?:go with|use)|we decided|i(?:'ve)? decided|go with option|we(?:'ve| have)? (?:agreed|chosen|settled on)"
                           r"|the decision is|effective (?:immediately|today|from|as of)|(?<!\byou )(?:is|are) (?:not )?(?:allowed|permitted|prohibited)|(?:we|the team) must (?:never|not|always|stay|remain))\b|\bdecision:",re.I)),
    ('correction',re.compile(r"^\s*(?:no|nope)\s*[,.!:;—-]|^\s*actually,?\s+(?:it'?s|it is|that'?s|the|we|i|you)\b|\bthat'?s (?:wrong|not right|incorrect|not what i)\b|\byou(?:'re| are| were) wrong\b|\bi meant\b",re.I)),
    # "always"/"never" count when they address the agent or state a habit, not inside a description
    # ("the API should always use HTTPS").
    ('preference',re.compile(r"\b(?:i(?:'d)? prefer|i'?d rather|i (?:hate|don'?t like)|from now on|going forward|in (?:the )?future|please (?:don'?t|do not|never|always)|stop doing|don'?t ever|by default,? use"
                             r"|(?:you should|you must|we) (?:always|never)|i (?:always|never) (?:use|want|like|do))\b|^\s*(?:always|never)\s+\w",re.I)),
)
# A memo quoted in the prompt states a directive: one strong phrase, or two weaker ones.
DIRECTIVE_STRONG=re.compile(r"\b(?:decided|decision|directive|policy|effective (?:immediately|today|from|as of)|must never|must not|may not)\b",re.I)
DIRECTIVE_WEAK=tuple(re.compile(p,re.I) for p in (r"\bmust (?:be|use|stay|remain|always)\b",r"\b(?:replac(?:es|ing)|supersed(?:es|ing)|no longer)\b",
                                                   r"\b(?:going forward|from now on|in future)\b",r"\b(?:all|every|each) [\w -]{0,40}\b(?:must|shall)\b"))
PRIORITY=('remember','decision','directive','correction','preference')
SIGNAL_TEXT={'remember':('asked you to remember something','--type TYPE --concern CONCERN'),
             'correction':('corrected you','--type knowledge --concern correction'),
             'preference':('stated a preference','--type preference --concern preference'),
             'decision':('made a decision','--type decision --concern decision'),
             'directive':('shared a directive (a memo, ticket or decision record)','--type decision --concern decision --link SOURCE')}
# Kinds whose missing capture decision still holds the turn open at Stop. The others are raised once, in the
# prompt's own context, where acting on them costs no extra turn.
BLOCKING_SIGNALS=frozenset({'remember','decision','directive'})

def _statements(text):
    return [s for s in STATEMENT_BREAK.split(text) if s.strip() and not s.rstrip().endswith('?') and not TRANSIENT.search(s)]

def signals_of(prompt,skip=None):
    """[(kind, the statement that carries it)], one per kind the user's own words carry, highest priority first
    (PRIORITY), else []. Pasted and harness-generated text is ignored, and so are questions ("why does it never use
    the cache?") and remarks about this task only. Quoted text (a blockquote, a pasted block) yields only a
    'directive'. skip, when given, is called with a statement and returns True for one already settled, which then
    signals nothing."""
    if not isinstance(prompt,str) or MACHINE_PROMPT.match(prompt):return []
    plain=IGNORED.sub(' ',prompt)
    statements=[s for s in _statements(QUOTED.sub(' ',plain)) if not (skip and skip(s))]
    found={}
    for kind,pattern in SIGNALS:
        hit=next((s for s in statements if pattern.search(s)),None)
        if hit:found[kind]=hit
    quoted=[s for s in _statements('\n'.join(m[1] or m[2] or '' for m in QUOTED.finditer(plain))[:QUOTED_CHARS]) if not (skip and skip(s))]
    strong=next((s for s in quoted if DIRECTIVE_STRONG.search(s)),None)
    weak=[(s,p) for s in quoted for p in DIRECTIVE_WEAK if p.search(s)]
    if strong:found['directive']=strong
    elif len({p for _,p in weak})>=2:found['directive']=weak[0][0]
    return [(kind,found[kind]) for kind in PRIORITY if kind in found]

def signal_of(prompt,skip=None):
    """(kind, statement) of the highest-priority kind signals_of finds, else (None, None)."""
    found=signals_of(prompt,skip)
    return found[0] if found else (None,None)

def durable_signal(prompt):
    """The kind of durable statement the user's own words carry ('preference', 'correction', 'decision',
    'remember', or 'directive' from a quoted memo), else None."""
    return signal_of(prompt)[0]

# Which statements already raised a signal, so a standing header repeated in every brief is not raised again.
# A non-canonical sidecar per domain, like .last-session.json: salted hashes of the normalized statement only.
SIGNALS_SEEN='.signals-seen.json'
MAX_SIGNALS_SEEN=500

def _statement_hash(salt,statement):
    from .private import _hash,_words
    return _hash(salt,_words(statement))

def signals_seen(root,domain):
    """(raised, hashed): raised(statement) is True for a statement settled before; hashed(statement) is its
    salted hash, which creates the sidecar's salt when there is none."""
    path=root/'domains'/domain/SIGNALS_SEEN
    row=read_json(path,{}) or {};row=row if isinstance(row,dict) else {}
    raised=lambda statement:bool(row.get('salt')) and _statement_hash(row['salt'],statement) in (row.get('seen') or [])
    def hashed(statement):
        if not row.get('salt'):
            import secrets
            row['salt']=secrets.token_hex(16)
            try:atomic_write(path,canonical_json(row))
            except OSError:pass
        return _statement_hash(row['salt'],statement)
    return raised,hashed

def settle_signal(root,domain,value):
    """Remember a statement's hash once its turn is settled (reminded, captured, finalized or closed)."""
    if not value or not domain:return
    try:
        path=root/'domains'/domain/SIGNALS_SEEN;row=read_json(path,{}) or {}
        seen=[h for h in row.get('seen',[]) if h!=value]+[value]
        if row.get('salt') and seen!=row.get('seen'):atomic_write(path,canonical_json({**row,'seen':seen[-MAX_SIGNALS_SEEN:]}))
    except (OSError,ValueError,AttributeError):pass

MAX_SIGNAL_WORDS=12
MIN_COVER_WORDS=2

# Function words and the signals' own wording ("remember that", "we decided"): a note about the statement need not
# repeat them. Not search.STOP, which drops words a fact is often about ("go", "fix", "run").
SIGNAL_STOP=frozenset('''a an the and or but of to in on at by for with from into as so if then than that this these those there here
is are was were be been being am do does did done it its we i you they he she our my your their me us them not no yes ok please just
also too very will would should can could may might must shall let lets ll ve re
remember keep mind forget record prefer rather like hate decided decide decision agreed chosen settled going forward future now
always never actually wrong meant team effective immediately today allowed permitted prohibited'''.split())

def content_hashes(salt,text,limit=None):
    """Salted hashes of the distinct content words (no SIGNAL_STOP words, no single letters, light stemming) of a
    text, in order. A blocking statement keeps these instead of its text, so Stop can tell whether a note covers it.
    Short identifiers count ("CI uses Go")."""
    from .private import _hash,_words
    from .search import stem
    words=dict.fromkeys(stem(w) for w in _words(text) if w not in SIGNAL_STOP and len(w)>1)
    return [_hash(salt,[w]) for w in list(words)[:limit]]

def covers_signal(root,refs,state):
    """True when one of the notes (refs, DOMAIN:ID) a turn's receipts touched shares at least min(2, n) content words
    with the statement that armed the turn, n being the number of word hashes the prompt stored."""
    from .notes import find_note
    wanted=set(state.get('signal_words') or []);salt=state.get('signal_salt')
    for ref in dict.fromkeys(refs):
        try:domain,note_id=ref.split(':',1);note=find_note(note_id,domain,root)[1]
        except (ValueError,OSError):continue
        if len(wanted&set(content_hashes(salt,(note.get('title') or '')+' '+(note.get('body') or ''))))>=min(MIN_COVER_WORDS,len(wanted)):return True
    return False

NUDGE_KINDS=('preference','correction','decision')   # statements that often change a note already saved
NUDGE_NOTE_TYPES=frozenset({'preference','policy','decision','knowledge'})
NUDGE_NOTES=2

def signal_notes(statement,domain,root,bindings):
    """Up to NUDGE_NOTES saved notes (preference, policy, decision, knowledge; not digests) in this domain, its parents, its children and the global
    domains that the statement may change, through the relevance gate. The statement is searched, never stored. Fails open: []."""
    try:
        from .search import search_domains
        from .common import global_domains
        domains=list(dict.fromkeys([domain,*scope_domains(domain,bindings),*global_domains(root)]))
        hits=search_domains(statement,domains,6,root,keep=relevance_gate(statement,root))
        return [h for h in hits if h.get('type') in NUDGE_NOTE_TYPES and not h.get('sources')][:NUDGE_NOTES]
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.signal_notes',exc,root);return []

def signal_nudge(signal,session,kinds=None,notes=None):
    """The one line a prompt with a durable statement adds to its own context. It names every kind the prompt carried
    (kinds, in priority order) but suggests the capture command of the primary one (signal). notes (signal_notes hits) are
    named as notes the statement may change."""
    said,hint=SIGNAL_TEXT[signal]
    said=[SIGNAL_TEXT[k][0] for k in kinds if k in SIGNAL_TEXT] if kinds and signal in kinds else [said]
    said=said[0] if len(said)==1 else ', '.join(said[:-1])+' and '+said[-1]
    line=(f"Living Memory: the user {said} in this prompt. If it should outlast this session, update its note or "
          f"CMD capture --session {session} {hint} --title '...' --summary '...' before you finish; if not, nothing is needed.")
    if notes:
        from .notes import short_ref
        names=' or '.join(f"{short_ref(h['domain'],h['id'])} {h['title'][:70]}" for h in notes)
        line+=f" It may change {names}; update that note instead of capturing a new one."
    return line

def hit_line(hit,width=HIT_SNIPPET):
    snippet=hit.get('snippet','')
    if len(snippet)>width:
        cut=snippet.rfind(' ',0,width);snippet=snippet[:cut if cut>0 else width]+' …'
    from .notes import short_ref
    stale=f"{hit['stale']} — " if hit.get('stale') else ''   # a digest whose source file changed (core/digests.py)
    mark=f"UNCONFIRMED (as of {hit.get('updated','')}): " if hit.get('interim') else ''   # notes.is_interim
    line=f"- [{str(hit.get('type','note')).upper()} {hit.get('updated','')}] {short_ref(hit['domain'],hit['id'])} {mark}{hit['title']} — {stale}{snippet}"
    if hit.get('procedure_id'):
        from .learning import use_hint
        line+=f" ({use_hint(hit['procedure_id'],hit.get('procedure_version'))})"
    return line

def revision_key(note):return str(note.get('revision') or '')[:12]

def _snippeted(state):
    """ref -> revision key of the notes SessionStart showed with a snippet: a one-line summary would only repeat them."""
    found=state.get('shown_snippet')
    return found if isinstance(found,dict) else {}

def _unseen(hit,shown):
    """True unless this revision of the note was already delivered. shown maps ref -> revision key;
    a legacy list of refs (or a ref without a revision) suppresses the note itself."""
    ref=f"{hit['domain']}:{hit['id']}"
    if ref not in shown:return True
    seen=shown.get(ref) if isinstance(shown,dict) else None
    return bool(seen) and seen!=revision_key(hit)

from .search import relevance_gate   # moved to search.py so notes.capture can use it; kept here for callers and patches

# A task brief is mostly boilerplate: past this many characters a prompt is searched in pieces, so the assignment
# in the middle is not lost to the team header and the closing checklist.
LONG_PROMPT=600
LONG_SEGMENTS=4
LONG_LEAST=3
HEADING=re.compile(r'^\s{0,3}#{1,6}\s')

# A numbered, lettered, bulleted or question-per-line list (a quiz, a checklist): each item is searched on its own.
LIST_ITEM=re.compile(r'^\s*(?:\d+[.)]|[qQ]\d+[.:)]|[-*])\s+\S')
LIST_MARK=re.compile(r'^\s*(?:\d+[.)]|[qQ]\d+[.:)]|[-*])\s+')
LIST_MIN=3        # items that make a prompt a list
LIST_QUERIES=8    # items searched, richest by IDF first
LIST_HITS=2       # notes one item may contribute
ITEM_LEAST=2      # query words body coverage must find for an item (see relevance_gate)
LIST_CAP=6        # notes injected for a list prompt, at most (3 otherwise)
LIST_SNIPPET=160
LIST_BUDGET=2400  # characters of notes a list prompt adds

def _is_item(line):
    return not HEADING.match(line) and bool(LIST_ITEM.match(line) or line.rstrip().endswith('?'))

def _pieces(text):
    """[(segment, is_item)]: split at markdown headings (which open a segment) and blank lines; in a prompt with at least
    LIST_MIN list items each item is a segment of its own, and the lines before the list stay together."""
    lines=text.splitlines();listy=sum(map(_is_item,lines))>=LIST_MIN
    segments=[[]];flags=[False]
    for line in lines:
        item=listy and _is_item(line)
        if not line.strip():segments.append([]);flags.append(False)
        elif segments[-1] and (item or HEADING.match(line)):segments.append([line]);flags.append(item)
        else:
            if not segments[-1]:flags[-1]=item
            segments[-1].append(line)
    return [('\n'.join(s),f) for s,f in zip(segments,flags) if s]

def prompt_segments(text):
    """The prompt's paragraphs, or its list items (see _pieces)."""
    return [s for s,_ in _pieces(text)]

def list_items(text):
    """The items of a list prompt (at least LIST_MIN of them), else []."""
    return [s for s,item in _pieces(text) if item]

MAX_QUESTIONS=10

def questions(text):
    """The separate questions in text, markers removed: its list items when there are at least LIST_MIN, else its
    ?-terminated sentences when there are at least two; [] for a single question (or none)."""
    found=[LIST_MARK.sub('',s).strip() for s in list_items(text)]
    if not found:found=[s.strip() for s in re.findall(r'[^?]*\?',text) if len(s.strip())>1]
    return found if len(found)>=2 else []

def item_queries(prompt,vocab):
    """Queries for up to LIST_QUERIES list items, the richest by summed IDF first: each at most 16 terms the notes' own
    vocabulary selected, judged by the usual gate with body coverage (ITEM_LEAST words)."""
    from .search import idf_terms,idf_sum
    queries=[]
    for item in sorted(list_items(prompt),key=lambda s:-idf_sum(s,vocab))[:LIST_QUERIES]:
        query=' '.join(idf_terms(item,vocab))
        if query and query not in queries:queries.append(query)
    return queries

def long_queries(prompt,vocab):
    """[(query, body)] for a long prompt: the last segment (the ask usually comes last) and the three richest others by
    summed IDF, then the whole prompt's most informative terms. Each query is at most 16 terms the notes' own vocabulary
    selected. A segment is judged by the usual gate; the whole-prompt query (body False) only by titles. Empty and
    repeated queries are dropped. List items are item_queries' business, not segments here."""
    from .search import idf_terms,idf_sum
    segments=[s for s,item in _pieces(prompt) if not item];segments=segments if len(segments)>1 else []
    rest=sorted(segments[:-1],key=lambda s:-idf_sum(s,vocab))[:LONG_SEGMENTS-1]
    queries={}
    for text,body in [(s,True) for s in ([segments[-1]]+rest if segments else [])]+[(prompt,False)]:
        query=' '.join(idf_terms(text,vocab))
        if query and query not in queries:queries[query]=body
    return list(queries.items())

# The injection controller (core/injection.py): section evidence, follow-ups, acknowledgments and per-section dedupe.
# The eval switches it off to measure the line-per-note delivery used before units.
DELIVERY=True

def context(cwd,prompt,root,active=None,shown=(),overview_done=False,delivered=None,trace=None,session_delivered=None,topic=None,units_out=None,room=None):
    """Relevant memory for a prompt: (text or None, ids injected, overview shown). A note shown earlier
    is offered again once it has a new revision. delivered, when a dict, receives ref -> revision key.
    trace, when a dict, receives the decision: the path taken, the gate's candidates and the refs injected,
    suppressed as already shown, or cut by the cap (refs and numbers only).
    With DELIVERY: session_delivered is the session's {ref: {rev, units: {unit: level}}} (injection.novel); topic is
    the refs the previous substantive prompt found, which a short follow-up ("why?") refers to; units_out, when a
    list, receives {ref, unit, level} per injected hit; room is the characters the hits may use (injection.within)."""
    from .search import search_domains,recent_notes,query_terms,stem
    from . import injection
    trace=trace if isinstance(trace,dict) else {}
    bindings=load_bindings(root);active=active or resolve_domain(cwd,bindings)
    if not active or MACHINE_PROMPT.match(prompt or ''):trace['path']='machine_prompt' if active else 'unbound';return None,[],False
    if DELIVERY and injection.is_ack(prompt):trace['path']='ack';return None,[],False
    query={stem(t) for t in query_terms(prompt)};trace['terms']=len(query)
    role=injection.follow_up(prompt,query) if DELIVERY and topic else None
    topic_hits=[]
    if role:
        from .search import note_hits
        topic_hits=note_hits(list(topic)[:injection.TOPIC_REFS],root,prompt);trace['follow_up']=role
    if not query and not topic_hits:trace['path']='no_terms';return None,[],False
    if not query:
        return _deliver(prompt,root,topic_hits,[],shown,delivered,trace,session_delivered,units_out,role,False,set(),room)
    scope=bindings.get('search_scope',{});global_domains=[canonical_domain(d,root) for d in scope.get('global_domains',['_global'])];global_domains=[d for d in global_domains if (root/'domains'/d).exists()]
    parents=scope_domains(active,bindings)
    trace['candidates']=[];relevant=relevance_gate(prompt,root,trace['candidates'])
    domains=list(dict.fromkeys([active]+parents+global_domains))
    # Global notes hold in every project, so they rank like the project's own; a parent's or child's count half.
    weights={**{g:2 for g in global_domains},active:2,**{p:1 for p in parents if p!=active}}
    queries=None;items=list_items(prompt)
    if len(prompt)>LONG_PROMPT or items:
        from .search import vocabulary,terms
        vocab=vocabulary(domains,root,terms(prompt))
        if vocab[0]:
            # A list prompt's items first (a looser gate, a few notes each), then the long-prompt queries; one entry per query text.
            queries={q:(True,ITEM_LEAST,LIST_HITS) for q in item_queries(prompt,vocab)} if items else {}
            for q,body in long_queries(prompt,vocab):queries.setdefault(q,(body,LONG_LEAST,10))
    if items:trace['items']=len(items)
    if queries:
        # Each query has its own relevance gate; a note any query finds counts once, at its best rank.
        trace['queries']=len(queries);best={}
        for text,(body,least,limit) in queries.items():
            for h in search_domains(text,domains,limit,root,weights=weights,keep=relevance_gate(text,root,trace['candidates'],body,least)):
                key=(h['domain'],h['id'])
                if key not in best or h['score']>best[key]['score']:best[key]=h
        # Rank-one notes of different items tie on score; the better covered one goes first.
        found=sorted(best.values(),key=lambda h:(-h['score'],-h.get('relevance',0) if items else 0,h['domain'],h['id']))
    else:found=search_domains(prompt,domains,10,root,weights=weights,keep=relevant)
    if not found and scope.get('foreign_domains'):found=search_domains(prompt,scope['foreign_domains'],10,root,keep=relevant)
    from .search import LAST_SEMANTIC
    # Which vector backend served each domain and why not native (debug log; counts and codes only).
    trace['semantic']={d:LAST_SEMANTIC[d] for d in domains if d in LAST_SEMANTIC}
    if DELIVERY:
        trace['topic']=[f"{h['domain']}:{h['id']}" for h in found][:injection.TOPIC_REFS]
        if found or topic_hits:
            listed={(h['domain'],h['id']) for h in topic_hits}
            return _deliver(prompt,root,topic_hits,[h for h in found if (h['domain'],h['id']) not in listed],shown,delivered,trace,session_delivered,units_out,role,bool(items),query,room)
    seen=set(shown);ref=lambda h:f"{h['domain']}:{h['id']}"
    unseen=[h for h in found if _unseen(h,shown)];hits=unseen[:min(LIST_CAP,len(items)) if items else 3]
    width=LIST_SNIPPET if items else HIT_SNIPPET
    if items:
        # The lowest-scored notes go first when the lines would exceed the list budget (one note always stays).
        used=0
        for i,h in enumerate(hits):
            used+=len(hit_line(h,width))+1
            if used>LIST_BUDGET and i:hits=hits[:i];break
    trace.update(injected=[ref(h) for h in hits],suppressed_shown=[ref(h) for h in found if not _unseen(h,shown)],cut_by_cap=[ref(h) for h in unseen[len(hits):]])
    if hits:
        trace['path']='injected'
        from . import digests
        digests.mark(hits,root,bindings)
        if isinstance(delivered,dict):delivered.update({f"{h['domain']}:{h['id']}":revision_key(h) for h in hits})
        return ('Untrusted historical memory; verify current instructions and files:\n'+'\n'.join(hit_line(h,width) for h in hits),
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

def _deliver(prompt,root,topic_hits,found,shown,delivered,trace,session_delivered,units_out,role,listy,query,room=None):
    """context() with the delivery controller: per hit, the unit to deliver and its level (injection.choose, level_of),
    dropped when this session already has it (injection.novel), at most 3 hits (a list prompt: up to LIST_CAP within
    LIST_BUDGET). The previous topic's notes (a follow-up) come first and skip the gate: the prompt names them."""
    from . import injection,digests
    from .semantic import threshold
    near=threshold(root);again=injection.wants_again(prompt);ref=lambda h:f"{h['domain']}:{h['id']}"
    topical={id(h) for h in topic_hits};chosen=[];suppressed=[]
    for h in topic_hits+found:
        mine=id(h) in topical
        unit=injection.choose(h,query,near,role if mine else None,prompt)
        level=injection.level_of(unit,prompt);key=injection.unit_key(unit)
        # "Does that still apply?" re-states the note's current summary and date even if it was shown before.
        if injection.novel(h,key,level,session_delivered or {},shown,again or (mine and role=='overview')):chosen.append((h,unit,level,key))
        else:suppressed.append(ref(h))
    picked=chosen[:min(LIST_CAP,trace.get('items') or 0) if listy else 3]
    width=LIST_SNIPPET if listy else HIT_SNIPPET
    if picked:digests.mark([h for h,_,_,_ in picked],root,load_bindings(root))
    lines=[injection.line(h,u,level,width) for h,u,level,_ in picked]
    # The lowest-scored notes go first when the lines would exceed the list budget or the hook's room (one note always stays).
    room=injection.PROMPT_CAP if room is None else room;keep=injection.within(lines,injection.HEADING,min(LIST_BUDGET,room) if listy else room)
    if keep<len(lines):trace['cut_by_budget']=[ref(h) for h,_,_,_ in picked[keep:]];picked,lines=picked[:keep],lines[:keep]
    trace.update(injected=[ref(h) for h,_,_,_ in picked],suppressed_shown=suppressed,cut_by_cap=[ref(h) for h,_,_,_ in chosen[len(picked):]],
                 levels={ref(h):[level,u['role'] if u else 'overview'] for h,u,level,_ in picked})   # roles, not headings: the log holds no note text
    if not picked:trace['path']='all_shown';return None,[],False
    trace['path']='injected'
    if isinstance(delivered,dict):delivered.update({ref(h):revision_key(h) for h,_,_,_ in picked})
    if isinstance(units_out,list):units_out.extend({'hit':h,'unit':key,'level':level} for h,_,level,key in picked)
    return (injection.HEADING+'\n'+'\n'.join(lines),[ref(h) for h,_,_,_ in picked],False)

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

def child_domains(domain,bindings):
    """Domains bound to a folder inside this domain's folder (e.g. Sandbox/project under Sandbox), nearest first. A domain that is also a parent is left out."""
    from .common import _entries
    mine=[];others={}
    for name,_,prefixes in _entries(bindings):
        paths=[]
        for raw in prefixes:
            try:paths.append(Path(os.path.expandvars(str(raw))).expanduser().resolve())
            except (OSError,ValueError):continue
        if name==domain:mine=paths
        else:others[name]=paths
    depth={}
    for name,paths in others.items():
        found=[len(child.relative_to(parent).parts) for child in paths for parent in mine if child!=parent and child.is_relative_to(parent)]
        if found:depth[name]=min(found)
    parents=set(parent_domains(domain,bindings))
    return sorted((n for n in depth if n not in parents),key=lambda n:(depth[n],n))

def scope_domains(domain,bindings):
    """Parent and child domains: the neighbours whose notes count at weight 1 beside the domain's own."""
    return parent_domains(domain,bindings)+child_domains(domain,bindings)

def child_path(shown,domain,child,bindings):
    """A path as shown for `domain` (touched_paths: relative to its folder, else absolute), rewritten relative to the folder of its child domain `child`,
    which is how that domain's notes name files; None when the file is not inside the child's folder. A folder that is the child's root or
    contains it gives '' (every file the child's notes name): search_domains' file filter and search.file_matches read '' that way. Hooks test the
    result for truth, so they skip it: a touched path is a file, never a folder."""
    from .common import domain_prefixes
    try:
        mine=domain_prefixes(domain,bindings);theirs=domain_prefixes(child,bindings)
        raw=Path(shown);found=[raw] if raw.is_absolute() else [p/raw for p in mine]
        for p in found:
            p=p.resolve()
            for t in theirs:
                if p.is_relative_to(t):return '' if p==t else p.relative_to(t).as_posix()
                if t.is_relative_to(p):return ''
    except (OSError,ValueError):pass
    return None

def scoped_file_hits(paths,domain,bindings,root,per_file):
    """{path: notes about it} from the domain, its parents and its child domains (a child's notes name files relative to the child's folder:
    child_path rewrites the path for them, and each such hit carries it as 'file'). Reads the index without refreshing it, like every hook lookup."""
    from .search import files_notes
    paths=list(dict.fromkeys(paths));out=files_notes(paths,[domain]+parent_domains(domain,bindings),root,per_file)
    for child in child_domains(domain,bindings):
        mapped={c:p for p in paths if (c:=child_path(p,domain,child,bindings))}
        if not mapped:continue
        for c,hits in files_notes(list(mapped),[child],root,per_file).items():
            for h in hits:h['file']=c
            out[mapped[c]]=out[mapped[c]]+hits
    for p,hits in out.items():out[p]=sorted(sorted(hits,key=lambda h:h.get('updated',''),reverse=True),key=lambda h:h.get('match')!='files')[:per_file]
    return out

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
    statements={}
    if event=='user-prompt':
        turn=turn or str(payload.get('timestamp') or now())
        if session!='unknown':
            # Plan mode cannot save anything, so a statement there is not a reminder's business.
            planning=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan'
            text=payload.get('prompt') or payload.get('user_prompt') or payload.get('userPrompt') or payload.get('transformedPrompt') or ''
            raised,hashed=signals_seen(root,domain) if domain else (None,None)
            kinds=[] if planning else signals_of(text,raised);signal,source=kinds[0] if kinds else (None,None)
            statements=dict(kinds)   # held for this hook only, to search the notes a statement may change
            # Only the kind and a salted hash of the statement are kept, never the prompt text; the hash settles at Stop.
            shash=hashed(source) if signal and hashed else None
            # Every statement the nudge names settles at Stop, so none of them is raised again when repeated.
            shashes=list(dict.fromkeys(hashed(s) for _,s in kinds)) if signal and hashed else []
            from . import private
            detail.update(signal=signal,signals=[k for k,_ in kinds],private_spans=len(private.spans(text)) if isinstance(text,str) and not MACHINE_PROMPT.match(text) else 0)
            heads=repo_heads(cwd,state)
            def opened(s):
                if not s.get('open_turns'):
                    s['turn_started']=time.time()  # the turn's first prompt: shell edits are found by mtime
                    s['heads']=heads  # ...and a shell commit by HEAD moving
                s['turn']=turn;s['open_turns']=(list(s.get('open_turns',[]))+[turn])[-20:]
                if signal:
                    s['signal']=signal;s['signal_hash']=shash;s['signal_hashes']=shashes
                    # Hashes of the statement's words, under a per-session salt, so Stop can check a note covers it.
                    if not s.get('signal_salt'):
                        import secrets
                        s['signal_salt']=secrets.token_hex(16)
                    s['signal_words']=content_hashes(s['signal_salt'],source,MAX_SIGNAL_WORDS)
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
        pointer=session_map(cwd,root,bindings,state,session);detail['map']=bool(pointer)
        if pointer:prefix+=pointer+'\n'
        # The knowledge base (core/sources.py): one line, and a detached background sync when the sources changed since the last one.
        from .sources import session_line
        kb=session_line(root,domain,bindings,detail.setdefault('sources',{}))
        if kb:prefix+=kb+'\n'
        # A dashboard published as a Claude artifact only changes when a session republishes it, so say when it is behind its note.
        if harness=='claude':
            try:
                from . import views
                from .session_start import _scope
                behind=views.behind(root,lambda:_scope(domain,root,bindings))
                if behind:prefix+='\n'.join(line for _,line in behind)+'\n'
                if session!='unknown' and (behind or state.get('views_told')):_update(root,session,lambda s:s.update(views_told=[key for key,_ in behind]))
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.views_hint',exc,root)
        # Prompts and tool hints read the index without checking it: bring a stale one (older schema, notes edited outside the scripts) up to date now.
        try:
            from .search import refresh_stale
            detail['reindexed']=refresh_stale(domain,root)
        except Exception as exc:
            from .logs import swallowed
            swallowed('runtime.refresh_stale',exc,root)
        from .briefs import brief_folder
        resolved=[] if _subagent(payload) else auto_resolve(root,domain,bindings,session);detail['auto_resolved']=len(resolved)
        text,shown,indexed=build_context(cwd,root,max(MIN_BUDGET,BUDGET-len(prefix)),session=session,trace=detail,resolved=resolved,
                                         currency=lambda hit:note_currency(hit,brief_folder(hit,cwd,bindings),root,bindings,state,session,'fresh_brief'))
        detail.update(shown=list(shown),indexed=indexed)
        if session!='unknown':
            # A new or compacted context has seen only what is injected now. Guidance and gotchas carry a snippet
            # there (shown_snippet, never `shown`): a prompt that finds one relevant injects it in full, and only that
            # full injection is deduplicated. Index lines are titles, so those notes stay eligible too. Once the index
            # has listed the newest titles, the prompt-time "recent titles" fallback has nothing to add.
            def started(s):
                s.pop('shown',None);s.pop('shown_snippet',None);s.pop('overview_shown',None);s.pop('delivered',None);s.pop('topic',None)
                if shown:s['shown_snippet']=shown
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
        delivered={};maps_text,map_refs,mapped=None,{},{};brief_text,brief_refs=None,{};briefed=[]
        if isinstance(prompt,str) and not MACHINE_PROMPT.match(prompt):
            try:maps_text,map_refs,mapped=prompt_maps(cwd,prompt,root,bindings,state,detail,briefed,session)
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.prompt_maps',exc,root)
            try:brief_text,brief_refs=prompt_briefs(cwd,root,bindings,state,detail,briefed,session)
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.prompt_briefs',exc,root)
        # The project brief leads the orientation: what the project is, then how its repos are organized.
        if brief_text:maps_text='Untrusted historical memory; verify against the current files.\n'+brief_text+('\n\n'+maps_text.split('\n',1)[1] if maps_text else '')
        # A map or brief injected now counts as shown, so the note search below does not repeat it as a hit.
        shown_now=state.get('shown',{})
        shown_now=dict(shown_now) if isinstance(shown_now,dict) else dict.fromkeys(shown_now if isinstance(shown_now,list) else [],'')
        shown_now.update(map_refs);shown_now.update(brief_refs)
        units_out=[];topic=state.get('topic') if isinstance(state.get('topic'),list) else None
        session_delivered=state.get('delivered') if isinstance(state.get('delivered'),dict) else {}
        # One budget for this hook (injection.PROMPT_CAP): the header, maps and brief and the governance lines below always
        # go out; note hits get what they leave, and source excerpts and recall join only when they still fit.
        from . import injection
        header=f'Living Memory session={session} turn={turn}'
        governance=[]
        if state.get('private_turn')==turn:
            from .private import NOTICE
            governance.append(NOTICE)
        if detail.get('signal') in SIGNAL_TEXT:
            changing=next((statements[k] for k in NUDGE_KINDS if k in statements),None)
            governance.append(signal_nudge(detail['signal'],session,detail.get('signals'),signal_notes(changing,domain,root,bindings) if changing else None))
        if harness=='claude':
            # Once per session and revision: the session start already said so, and a prompt every turn would only repeat it.
            try:
                from . import views
                from .session_start import _scope
                told=state.get('views_told') if isinstance(state.get('views_told'),list) else []
                behind=[(key,line) for key,line in views.behind(root,lambda:_scope(domain,root,bindings),hint_command(root,state,domain)) if key not in told]
                governance+=[line for _,line in behind]
                if behind and session!='unknown':_update(root,session,lambda s:s.update(views_told=(list(s.get('views_told') or [])+[key for key,_ in behind])[-50:]))
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.views_hint',exc,root)
        fixed=len(header)+(len(maps_text)+1 if maps_text else 0)+sum(len(g)+1 for g in governance)
        room=max(0,injection.PROMPT_CAP-fixed-1) if DELIVERY else None
        if isinstance(prompt,str):hits,ids,overview=context(cwd,prompt,root,domain,shown_now,state.get('overview_shown',False),delivered,detail,session_delivered,topic,units_out,room)
        else:hits,ids,overview=None,[],False;detail['path']='not_text'
        relevant_notes=len(ids)+len(detail.get('suppressed_shown') or [])
        delivered.update(map_refs);delivered.update(brief_refs);ids=list(map_refs)+list(brief_refs)+ids
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
                if units_out:
                    from .injection import remember as delivered_unit
                    record=s.get('delivered') if isinstance(s.get('delivered'),dict) else {}
                    for row in units_out:delivered_unit(record,row['hit'],row['unit'],row['level'])
                    s['delivered']=dict(list(record.items())[-MAX_DELIVERED:])
            try:_update(root,session,remember)
            except (OSError,TimeoutError):pass
        if session!='unknown' and 'topic' in detail and not detail.get('follow_up'):
            # A short follow-up ("why?") refers to what this prompt found: refs only, never prompt text. A searched prompt
            # that found nothing clears it, so "why?" after an unrelated question does not answer the old topic.
            try:_update(root,session,lambda s:s.update(topic=detail['topic']))
            except (OSError,TimeoutError):pass
        if briefed:
            from .usage import record
            detail['briefed']=list(briefed);record(root,'inject',briefed,session,'map-brief')
        if ids:
            from .usage import record
            if map_refs:record(root,'inject',list(map_refs),session,'prompt-map')
            if brief_refs:record(root,'inject',list(brief_refs),session,'prompt-brief')
            if ids[len(map_refs)+len(brief_refs):]:record(root,'inject',ids[len(map_refs)+len(brief_refs):],session,'prompt',
                                                          levels={f"{r['hit']['domain']}:{r['hit']['id']}":r['level'] for r in units_out})
        # Candidates the gate kept but the cap cut: surfaced, not injected (refs only; never read as "unused").
        if detail.get('cut_by_cap'):
            from .usage import record
            record(root,'surface',list(detail['cut_by_cap'])[:3],session,'prompt')
        result['context']=header+('\n'+maps_text if maps_text else '')+('\n'+hits if hits else '')
        fits=lambda text:not DELIVERY or len(result['context'])+len(text)+1+sum(len(g)+1 for g in governance)<=injection.PROMPT_CAP
        if isinstance(prompt,str) and not MACHINE_PROMPT.match(prompt) and relevant_notes<2 and not _subagent(payload):
            try:
                excerpts,keys=prompt_sources(prompt,root,domain,state,detail.setdefault('sources',{}))
                if excerpts and not fits(excerpts):detail['sources_cut_by_budget']=len(keys);excerpts=None
                if excerpts:
                    result['context']+='\n'+excerpts
                    from .usage import record
                    record(root,'inject',['source:'+k for k in keys],session,'source')
                    if session!='unknown':
                        def remember_sources(s):s['sources_shown']=(list(s.get('sources_shown',[]))+keys)[-MAX_SOURCES_SHOWN:]
                        try:_update(root,session,remember_sources)
                        except (OSError,TimeoutError):pass
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.prompt_sources',exc,root)
        if state.get('recall_prompts') and harness in RECALL_HARNESSES and session!='unknown' and isinstance(prompt,str) and not MACHINE_PROMPT.match(prompt):
            from .recall import auto
            gate={};recalled,used=auto(state.get('transcript'),prompt,state.get('recalled',[]),gate);detail['recall']=dict(gate,used=len(used))
            recalled=(f"From before compaction (this session's transcript; untrusted, may be outdated; more: "
                      f"{hint_command(root,state,domain)} recall 'query' --session {session}):\n"+recalled) if recalled else None
            if recalled and fits(recalled):result['context']+='\n'+recalled
            elif recalled:detail['recall_cut_by_budget']=True;used=[]   # not delivered, so still eligible next time
            def spent(s):
                s['recall_prompts']=max(0,int(s.get('recall_prompts') or 0)-1)
                if used:s['recalled']=(list(s.get('recalled',[]))+used)[-50:]
            try:_update(root,session,spent)
            except (OSError,TimeoutError):pass
        # Governance lines go last, as before, and were counted in the budget first.
        for line in governance:result['context']+='\n'+line
        if detail.get('signal') in SIGNAL_TEXT:
            # Raised here, in the turn that can act on it, instead of costing an extra turn at Stop.
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
                                     f"{command(root,domain)} read REF"+('; Bash: as written, PowerShell: put & in front' if os.name=='nt' else '')+"): "+'; '.join(named))
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
        # The call operator is PowerShell's: Bash would background the command. Say so, with the command that works.
        if subcommand and grammar=='posix' and re.match(r'\s*&',text):
            detail['decision']='deny:call-operator'
            result['deny']='In Bash, drop the leading &: '+re.sub(r'^\s*&\s*','',text)
            return result
        learn_write=learn_settings=False
        if subcommand=='learn':
            from .learning import is_read
            entity,action=learn_operation(text,grammar);learn_write=not is_read(entity,action)
            # Turning learning on or off is the user's call, so it keeps the host's normal prompt.
            learn_settings=(entity,action)==('config','set')
        writes_view=subcommand=='views' and views_write(text,grammar)
        if (subcommand in MUTATING_SUBCOMMANDS or learn_write or writes_view) and _subagent(payload):
            detail['decision']='deny:subagent'
            result['deny']='Living Memory: subagents are read-only. Return the proposed note to the main agent instead of running '+subcommand+'.'
            return result
        # A remote source sync reaches the network (the only command that does): the user approves it, it is never auto-allowed.
        networked=subcommand=='source-sync' and remote_sync(text,grammar,root)
        if networked:detail['network']=True
        if networked and _subagent(payload):
            detail['decision']='deny:subagent'
            result['deny']='Living Memory: syncing a remote source reaches the network and is not run by subagents; ask the main agent to run source-sync.'
            return result
        if subcommand in ALLOWED_SUBCOMMANDS and not learn_settings and not networked:
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
        # What the agent is about to run is looked up even when the command only reads: a note may warn that a build exits 0 and builds nothing.
        if name in SHELL_TOOLS and text and not subcommand and session!='unknown' and domain and harness in FILE_HINT_HARNESSES and bindings.get('command_hints',True) is not False:
            detail['command_hints']=[];hint=command_hints(text,state,root,domain,bindings,session,detail['command_hints'])
            if hint:result['context']='\n'.join(filter(None,[result.get('context'),hint]))
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
            follow={};digested=[]
            hints,looked=file_hints(paths,state,root,domain,bindings,session,detail['hints'],follow,digested) if harness in FILE_HINT_HARNESSES else (None,[])
            # Files past the per-call hint cap were never looked up; Stop looks them up in one batch (follow_pending).
            unlooked=[p for p in paths if p not in looked and p not in set(state.get('file_hints',[]))] if harness in FILE_HINT_HARNESSES else []
            if hints:result['context']='\n'.join(filter(None,[result.get('context'),hints]))
            # Decision documents (ADRs, changelogs, boards) changed this turn; Stop asks for a note about them.
            from .triggers import decision_docs
            docs=decision_docs(paths,bindings);detail['docs']=len(docs)
            if not state.get('mutating') or set(paths)-set(state.get('touched',[])) or looked or (name in SHELL_TOOLS and not state.get('shell')):
                def touch(s):
                    s['mutating']=True;s['edited']=True   # edited outlives the turn: read-only sessions get no housekeeping reminders
                    if name in SHELL_TOOLS:s['shell']=True   # shell edits often use relative paths no hook can see; Stop asks git
                    if docs:s['docs']=list(dict.fromkeys(list(s.get('docs',[]))+docs))[:MAX_DOCS]
                    if follow:
                        hinted={p:r for p,r in (s.get('hinted') or {}).items() if p not in follow};hinted.update(follow)
                        s['hinted']=dict(list(hinted.items())[-MAX_HINTED:])
                        if digested:s['hinted_digests']=list(dict.fromkeys(list(s.get('hinted_digests',[]))+digested))[-MAX_HINTED:]
                    s['touched']=list(dict.fromkeys(list(s.get('touched',[]))+paths))[:MAX_TOUCHED]
                    if unlooked:s['unlooked']=list(dict.fromkeys(list(s.get('unlooked',[]))+unlooked))[:MAX_TOUCHED]
                    s['touched_session']=list(dict.fromkeys(list(s.get('touched_session',[]))+paths))[-MAX_TOUCHED_SESSION:]
                    if looked:s['file_hints']=list(dict.fromkeys(list(s.get('file_hints',[]))+looked))[-MAX_FILE_HINTS:]
                _update(root,session,touch)
    elif event=='stop':
        if session=='unknown':detail['reason']='unknown_session';return {}
        _record_last_session(root,domain,session,state)
        _record_ledger(root,domain,session,state,cwd)
        turns=list(dict.fromkeys(list(state.get('open_turns',[]))+([turn] if turn else [])))
        def close(s):
            for key in TURN_STATE:s.pop(key,None)
            if left or s.get('docs_left'):s['docs_left']=left
        signal=state.get('signal');detail.update(turns=len(turns),mutating=bool(state.get('mutating')),signal=signal)
        # The turn ends here whatever happens next, so its statement is settled: a header repeated in every brief raises once.
        for value in state.get('signal_hashes') or [state.get('signal_hash')]:settle_signal(root,domain,value)
        # Plan mode cannot save anything, so a repo explored there is reminded about at the next Stop outside plan mode.
        planning=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan'
        try:pending=stop_pending(root,bindings,state,session,turns,domain,cwd,planning)
        except Exception as exc:
            from .logs import swallowed
            swallowed('runtime.stop_pending',exc,root);pending=[]
        detail['explore_pending']=[(Path(data[0]).name,kind) for kind,data in pending if kind in MAP_KINDS]
        detail['pending']=[kind for kind,_ in pending]
        # Documents not asked about yet (the reminder names STOP_DOCS at a time) are asked about at the next Stop.
        left=[d['path'] for kind,d in pending if kind=='doc']
        # File changes alone end the turn quietly: the agent already decides silently at the end of each turn,
        # and Claude Code prints every Stop continuation in the user's terminal. A preference or correction was
        # raised in its own prompt's context, so it ends quietly too. What still holds the turn is knowledge that
        # is easily lost: an explicit request to remember, a decision or directive the user stated, a decision
        # document edited, an edit to a file a note is about, and a repo explored without a codebase map (or
        # grown at its top level), because rebuilding that orientation costs a whole exploration.
        quiet=('receipt' if not pending and any(capture_status(session,t,root) for t in turns) else 'no_change' if not (state.get('mutating') or signal or pending)
               else 'no_turns' if not turns else None if pending else 'nudged' if signal in SIGNAL_TEXT else 'files_only')
        if quiet:
            detail['reason']=quiet;_update(root,session,close);return {}
        latest=turns[-1];reminder=root/'.turns'/(turn_key(session,latest)+'.reminded')
        if payload.get('stop_hook_active') or payload.get('stopHookActive') or reminder.exists():
            detail['reason']='stop_active' if payload.get('stop_hook_active') or payload.get('stopHookActive') else 'already_reminded';_update(root,session,close);return {}
        atomic_write(reminder,'reminded\n')
        used=[];text=stop_text(pending,session,used)
        def reminded(s):
            # The reminder settles these turns: a later turn is not reminded again about this one's statement
            # or changes, even when the harness sends no stop_hook_active follow-up. The turn's touched files
            # stay for a capture that answers the reminder, and `reminded` lets that capture count as a signal capture.
            for key in TURN_STATE:
                if key not in ('touched','unlooked'):s.pop(key,None)
            s['reminded']={'turn':latest,'signal':signal}
            # Each document and each note is asked about once per session.
            asked=[d['path'] for k,d in used if k=='doc']
            s['docs_reminded']=list(dict.fromkeys(list(s.get('docs_reminded',[]))+asked))[-MAX_DOCS_REMINDED:]
            s['docs_left']=[p for p in left if p not in asked]
            s['briefed']=list(dict.fromkeys(list(s.get('briefed',[]))+[k for k,d in used if k in BRIEF_KINDS]))
            s['followed']=list(dict.fromkeys(list(s.get('followed',[]))+[d['ref'] for k,d in used if k=='follow']+[n['ref'] for k,d in used if k=='doc' for n in d['notes']]))[-MAX_HINTED:]
            s['stale_told']=list(dict.fromkeys(list(s.get('stale_told',[]))+[d[1]['ref'] for k,d in used if k=='stale']))[-MAX_HINTED:]
        _update(root,session,reminded)
        maps=[(data[0],kind) for kind,data in used if kind in MAP_KINDS]
        if maps:
            def settled(repos):
                # One map reminder per repo per session; the new top-level paths it named are settled too.
                for key,kind in maps:
                    if kind=='stale':continue   # asked once per note: stale_told
                    rec=dict(repos.pop(key,{}))
                    if kind=='unmapped':rec['reminded']=True
                    else:rec.pop('created',None)
                    repos[key]=rec
            _track_repos(root,session,settled)
        from .usage import record
        lead=(used or pending)[0][0]
        kind=lead if lead in STOP_KINDS else 'explored' if any(k=='unmapped' for k,_ in used) else 'grown'
        detail['reason']='reminded:'+kind
        record(root,'reminder',session=session,kind=kind)
        result['continue_reason']=text
    return result

def explore_pending(root,bindings,state,domain=None,cwd=None):
    """[(repo path, 'unmapped' | 'grown' | 'stale', data)] the Stop reminder should raise: a repo explored without a map
    (one captured since quiets it), new top-level paths in a mapped repo, or a map or brief that was shown as stale, is unchanged
    since and sits in a repo the turn touched (data: its ref, short ref, kind and the cited paths that changed)."""
    from .maps import find_map
    pending=[];stale=[]
    for key,rec in _repos(state).items():
        if not isinstance(rec,dict):continue
        if rec.get('explored') and not rec.get('reminded') and not rec.get('map') and not find_map(Path(key),root,bindings):
            pending.append((key,'unmapped',None))
        elif rec.get('created') and rec.get('map'):pending.append((key,'grown',(rec['map'],list(rec['created']))))
        stale+=[(key,slot,rec[slot]) for slot in ('fresh','fresh_brief') if isinstance(rec.get(slot),dict) and rec[slot].get('changed') and rec[slot].get('ref')]
    return (pending+stale_pending(root,bindings,state,domain,cwd,stale))[:3]

def stale_pending(root,bindings,state,domain,cwd,stale):
    """[(repo path, 'stale', {ref, short, what, paths})] for the shown-stale notes in `stale` ((repo path, slot, currency entry))."""
    from .maps import find_map
    from .briefs import find_brief
    from .common import domain_prefixes
    from .notes import short_ref
    told=set(state.get('stale_told',[]));touched=state.get('touched') or [];found=[]
    prefixes=domain_prefixes(domain,bindings) if domain else []
    def inside(repo,path):
        q=Path(path);return any(c.is_relative_to(repo) for c in ([q] if q.is_absolute() else [b/path for b in prefixes]))
    for key,slot,entry in stale:
        if entry['ref'] in told:continue
        repo=Path(key);here=git_root(cwd) if cwd else None
        if not (any(inside(repo,p) for p in touched) or (state.get('shell') and state.get('mutating') and here and here.as_posix()==key)):continue
        hit=(find_map if slot=='fresh' else find_brief)(repo,root,bindings)
        # Updated (or replaced) since it was shown: nothing left to ask.
        if not hit or f"{hit['domain']}:{hit['id']}"!=entry['ref'] or str(hit.get('revision') or '')!=entry.get('rev'):continue
        found.append((key,'stale',{'ref':entry['ref'],'short':short_ref(hit['domain'],hit['id']),'what':'map' if slot=='fresh' else 'project brief','paths':list(entry['changed'])}))
    return found

def map_sentence(key,kind,data,session):
    from .notes import short_ref
    from .maps import repo_name
    name=repo_name(key)
    if kind=='unmapped':
        return (f"this turn explored {name}, which has no codebase map. If that would spare a future session re-exploring it: "
                f"CMD capture --session {session} --type map --file '{key}' --title '{name} codebase map' --summary '...' "
                "--detail '**Layout:** ...' (and Entry points, Key modules, Data flow, Build/test/run, Conventions).")
    if kind=='stale':
        paths=data['paths'];names=', '.join(paths[:2])+(f' and {len(paths)-2} more' if len(paths)>2 else '');short=data['short']
        return (f"{data['what']} {short} is stale ({names} changed since it was written): CMD read {short}, then CMD update {short} --session {session} "
                f"--set-detail '{'**Conventions:**' if data['what']=='map' else '**Key decisions:**'} ...' for each bullet that changed.")
    ref,names=data;short=short_ref(*ref.split(':',1))
    return (f"new top-level paths in {name} ({', '.join(names)}): if they change its layout, update map {short} "
            f"(CMD read {short}, then CMD update {short} --session {session} --set-detail '**Layout:** ...' for each bullet that changed).")

# Per-turn state a Stop clears (reminded keeps `touched` for a capture that answers it).
TURN_STATE=('open_turns','mutating','signal','signal_hash','signal_hashes','signal_words','touched','unlooked','docs','shell','turn_started','heads')
MAP_KINDS=('unmapped','grown','stale')
BRIEF_KINDS=('unbriefed','brief')
# Kinds that name their own reminder in the usage diary; the map kinds report as 'explored' or 'grown'.
STOP_KINDS=frozenset(BLOCKING_SIGNALS|{'doc','follow','stale'}|set(BRIEF_KINDS))
STOP_TEXT_CAP=700
STOP_DOCS=2
STOP_NOTES=2
STOP_FOLLOW=5   # notes about edited files one Stop reminder names, in a single sentence
CHANGE_GUARD_COMMITS=3;CHANGE_GUARD_FILES=30   # more than this changed under a shell command is a pull or checkout, not the agent's work
FOLLOW_BATCH_FILES=30
BOILERPLATE_HEADINGS={'context','decision','decisions','consequences','status','background','summary','overview','alternatives','options','notes','rationale','changelog','unreleased'}

def stop_pending(root,bindings,state,session,turns,domain=None,cwd=None,planning=False):
    """[(kind, data)] the Stop reminder may raise, most important first. Kinds: remember, decision, directive (the
    user's own statement); doc (a decision document changed this turn); follow (a file a note is about was edited);
    unbriefed, brief (the project has no brief, or a decision this turn may have outdated it); unmapped, grown (a
    repo explored without a map, or grown at its top level)."""
    pending=[];signal=state.get('signal')
    # A receipt settles the housekeeping, so only the brief (which a captured decision may have outdated) is still asked
    # about. A blocking statement, a decision document or a file a note covers is settled only by a note that covers it
    # (or by "nothing durable"), so an unrelated capture does not silence it. A statement that stored no words
    # (older state, or nothing but stopwords) is settled by any receipt.
    receipts=[r for r in (capture_status(session,t,root) for t in turns) if r]
    receipted=bool(receipts);dismissed=any(r.get('outcome')=='nothing_durable' or r.get('dismissed') for r in receipts)
    saved=[ref for r in receipts for ref in r.get('notes',[])]
    if signal in BLOCKING_SIGNALS and not (dismissed or (covers_signal(root,saved,state) if state.get('signal_words') else receipted)):pending.append((signal,{'files':bool(state.get('mutating'))}))
    if (state.get('mutating') or state.get('docs_left')) and domain and not dismissed:
        memo=[]
        def changed():   # the shell-changed files, asked of git once for both reminders
            if not memo:memo.append(turn_changed(root,bindings,state,domain,cwd))
            return memo[0]
        for name,find in (('doc',lambda:doc_pending(root,bindings,state,domain,cwd,changed)),('follow',lambda:follow_pending(root,state,bindings,domain,changed) if state.get('mutating') else [])):
            try:found=[(kind,data) for kind,data in find() if not (kind=='doc' and saved and covered_by(root,saved,data['path']))]
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.'+name+'_pending',exc,root);continue
            # A note a document reminder already offers to update is not asked about a second time.
            named={n['ref'] for kind,data in pending if kind=='doc' for n in data['notes']}
            pending+=[(kind,data) for kind,data in found if not (kind=='follow' and data['ref'] in named)]
    if domain and cwd and not planning:
        try:pending+=brief_pending(root,bindings,state,session,turns,domain,cwd,pending,receipted)
        except Exception as exc:
            from .logs import swallowed
            swallowed('runtime.brief_pending',exc,root)
    # Housekeeping (maps, a brief) is for sessions that change something: a read-only Q&A session is not asked.
    if not planning and not receipted and state.get('edited'):pending+=[(kind,(key,data)) for key,kind,data in explore_pending(root,bindings,state,domain,cwd)]
    return pending

def _norm_path(path):return re.sub(r'^(?:\./)+','',str(path).strip().replace('\\','/')).casefold()

def note_covers(note,path):
    """Whether a note is about a document: it lists the path in files or sources, or names it in its Links."""
    from .note_format import sections
    want=_norm_path(path)
    listed=[_norm_path(f) for f in list(note.get('files') or [])+[s.get('path','') for s in note.get('sources') or [] if isinstance(s,dict)]]
    if any(f==want or f.endswith('/'+want) for f in listed):return True
    return re.search(r'(?<![\w.-])'+re.escape(want)+r'(?![\w-])',_norm_path(sections(note.get('body') or '').get('links','')))is not None

def covered_by(root,refs,path):
    """True when one of the notes (refs, DOMAIN:ID) a turn's receipts touched covers the document at path."""
    from .notes import find_note
    for ref in dict.fromkeys(refs):
        try:domain,note_id=ref.split(':',1);note=find_note(note_id,domain,root)[1]
        except (ValueError,OSError):continue
        if note_covers(note,path):return True
    return False

def brief_pending(root,bindings,state,session,turns,domain,cwd,pending,receipted=False):
    """[('unbriefed', {name, dir})] when the project has no brief and the turn did something worth briefing (a
    decision document, a decision or directive, or any change in a domain with MIN_NOTES notes); [('brief', {short})]
    when a decision was made or recorded this turn (a decision document, a decision or directive, a decision note
    saved) and the brief was not updated. Each once per session. A turn with a receipt already made its capture
    decision, so it is asked about an outdated brief (a decision note was saved) but not about a missing one."""
    from .briefs import find_brief,project_dir,project_key,active_count,MIN_NOTES
    from .maps import repo_name
    from .notes import short_ref
    from .search import active_notes
    asked=set(state.get('briefed',[]));signal=state.get('signal')
    if set(BRIEF_KINDS)<=asked:return []
    docs=bool(state.get('docs')) or any(kind=='doc' for kind,_ in pending)
    saved=[r for t in turns for r in (capture_status(root=root,session=session,turn=t) or {}).get('notes',[])]
    hit=find_brief(cwd,root,bindings)
    if hit:
        ref=f"{hit['domain']}:{hit['id']}"
        decided=docs or signal in {'decision','directive'} or any(kind=='decision' for _,kind in active_notes(saved,root).values())
        if 'brief' in asked or not decided or ref in saved:return []
        return [('brief',{'short':short_ref(hit['domain'],hit['id']),'title':hit['title']})]
    if 'unbriefed' in asked or receipted or not state.get('edited'):return []
    if docs or signal in BLOCKING_SIGNALS or (state.get('mutating') and active_count(domain,root,MIN_NOTES)>=MIN_NOTES):
        folder=project_dir(cwd,bindings);return [('unbriefed',{'dir':project_key(folder,bindings),'name':repo_name(folder)})]
    return []

def _doc_file(shown,prefixes):
    """The file a path shown to notes names, else None."""
    for base in prefixes:
        if (base/shown).is_file():return base/shown
    p=Path(shown);return p if p.is_absolute() and p.is_file() else None

def doc_query(path,file):
    """Search words for a document: its H1 and H2 headings (the first 8 KB), else its file name."""
    heads=[]
    try:text=file.read_bytes()[:8192].decode('utf-8','replace') if file else ''
    except OSError:text=''
    for match in re.finditer(r'(?m)^#{1,2}[ \t]+(.+?)[ \t#]*$',text):
        if match[1].strip().casefold() not in BOILERPLATE_HEADINGS:heads.append(match[1].strip())
    return (' '.join(heads) or re.sub(r'[-_.]+',' ',Path(path).stem))[:300]

def doc_notes(root,bindings,domain,path,file):
    """Notes a changed document may update: those listing or naming its path, then those matching its headings."""
    from .search import search_domains
    from .notes import short_ref
    domains=[domain]+scope_domains(domain,bindings)
    hits=scoped_file_hits([path],domain,bindings,root,2)[path]
    query=doc_query(path,file)
    if query:
        try:hits+=search_domains(query,domains,2,root,keep=relevance_gate(query,root))
        except (OSError,ValueError):pass
    from .session_start import _title
    found={}
    for h in hits:found.setdefault(f"{h['domain']}:{h['id']}",{'ref':f"{h['domain']}:{h['id']}",'short':short_ref(h['domain'],h['id']),'title':_title(h['title'])})
    return list(found.values())[:STOP_NOTES]

def _turn_repos(cwd,state):
    """The repos a turn's shell edits may have changed: the one holding cwd, then those explored so far (at most 3)."""
    here=git_root(cwd) if cwd else None
    return list(dict.fromkeys(([here] if here else [])+[Path(k) for k in _repos(state)]))[:3]

def repo_heads(cwd,state):
    """{repo path: HEAD commit} for _turn_repos, read from the .git files (no subprocess)."""
    from .common import head_oid
    return {r.as_posix():h for r in _turn_repos(cwd,state) if (h:=head_oid(r))}

def turn_changed(root,bindings,state,domain,cwd):
    """({shown name: absolute path}, pulled, edited) for files a shell command changed this turn, which no hook can name (relative paths): the files
    git reports changed since the turn began, or committed since HEAD was recorded at its first prompt, inside the domain's bound folders.
    pulled is True when HEAD moved by more than CHANGE_GUARD_COMMITS commits or more than CHANGE_GUARD_FILES files changed: a pull or checkout, not the agent's work.
    edited is the set of shown names of existing files the moved HEAD modified (empty unless pulled): what doc_pending leaves out after a pull."""
    from .common import domain_prefixes,display_path,head_oid
    from .triggers import changed_since,committed_since,commit_count
    prefixes=domain_prefixes(domain,bindings);files={};pulled=False;total=0;moved=[]
    def show(path):
        try:p=path.resolve()
        except OSError:return None,None
        if p.is_relative_to(root) or not any(p.is_relative_to(x) for x in prefixes):return None,None
        return display_path(p,prefixes),p
    if state.get('shell') and cwd:
        heads=state.get('heads') if isinstance(state.get('heads'),dict) else {}
        for repo in _turn_repos(cwd,state):
            found=changed_since(repo,state['turn_started']) if state.get('turn_started') else []
            start=heads.get(repo.as_posix());head=head_oid(repo) if start else None
            if start and head and head!=start:
                found+=committed_since(repo,start,head);moved.append((repo,start,head))
                pulled=pulled or (commit_count(repo,start,head) or 0)>CHANGE_GUARD_COMMITS
            total+=len(found)
            for path in found:
                name,p=show(path)
                if name:files[name]=p
    pulled=pulled or total>CHANGE_GUARD_FILES
    return files,pulled,({show(p)[0] for repo,start,head in moved for p in committed_since(repo,start,head,edited=True)} - {None}) if pulled else set()

def doc_pending(root,bindings,state,domain,cwd,changed=None):
    """[('doc', {path, notes})] for decision documents changed this turn that no reminder has covered yet: the
    paths PreToolUse recorded, those carried over from an earlier Stop (docs_left), and, after shell edits (whose
    relative paths no hook can see), the files turn_changed reports. All of them: stop_text names STOP_DOCS at a time.
    After a pull-sized change the existing docs the pulled commits edited are left out (follow_pending ignores the whole pull); a doc the
    pull added (a teammate's new ADR), one the agent wrote in the working tree, and docs and docs_left (edit-tool edits) still count. changed, when given, is the memoized turn_changed."""
    from .common import domain_prefixes
    from .triggers import decision_docs
    prefixes=domain_prefixes(domain,bindings);files,pulled,edited=(changed or (lambda:turn_changed(root,bindings,state,domain,cwd)))()
    shown=list(state.get('docs_left',[]))+list(state.get('docs',[]))+[f for f in files if not pulled or f not in edited]
    done=set(state.get('docs_reminded',[]))
    docs=[d for d in decision_docs(shown,bindings) if d not in done][:MAX_DOCS]
    return [('doc',{'path':d,'notes':doc_notes(root,bindings,domain,d,files.get(d) or _doc_file(d,prefixes))}) for d in docs]

def follow_pending(root,state,bindings=None,domain=None,changed=None):
    """[('follow', {ref, short, title, type, path, digest})]: notes about a file edited this turn that have not been updated, confirmed or
    asked about in this session. Candidates, in order: the notes a hint named for a touched file; then one batch lookup (search.files_notes)
    for touched files no hint looked up (past HINT_FILES_PER_CALL) and for files a shell command or git changed (turn_changed, nothing when
    that was a pull). digest marks a note that records hashes of the file (a digest or file_hashes); it only changes the wording."""
    from .search import active_notes,file_matches
    from .notes import short_ref
    from .session_start import _title
    hinted=state.get('hinted') if isinstance(state.get('hinted'),dict) else {}
    skip=set(state.get('saved',[]))|set(state.get('followed',[]));wanted={};digested=set(state.get('hinted_digests') or [])
    for path in state.get('touched',[]):
        for ref in hinted.get(path,[]):
            if ref not in skip:wanted.setdefault(ref,path)
    paths=[p for p in state.get('unlooked',[]) if p not in hinted]
    if changed:
        files,pulled,_=changed()
        if not pulled:paths+=[p for p in files if p not in hinted]
    paths=list(dict.fromkeys(paths))[:FOLLOW_BATCH_FILES]
    if paths and domain:
        for path,hits in scoped_file_hits(paths,domain,bindings or {},root,HINT_NOTES_PER_FILE).items():
            for h in hits:
                ref=f"{h['domain']}:{h['id']}"
                if ref in skip or not (h.get('match')=='files' or h.get('type') in FOLLOW_BODY_TYPES):continue
                wanted.setdefault(ref,path)
                if file_matches([s['path'] for s in h.get('sources') or []],h.get('file',path)):digested.add(ref)   # "digest" wording only for digests (sources), not hashed notes
    active=active_notes(list(wanted)[:2*STOP_FOLLOW],root)
    return [('follow',{'ref':ref,'short':short_ref(*ref.split(':',1)),'title':_title(active[ref][0]),'type':active[ref][1],'path':wanted[ref],'digest':ref in digested}) for ref in wanted if ref in active]

def _stop_part(kind,data,session):
    if kind in BLOCKING_SIGNALS:
        said,hint=SIGNAL_TEXT[kind]
        return (f"The user {said}{' (files changed too)' if data.get('files') else ''}. "
                f"Durable: update its note or CMD capture --session {session} {hint} --title '...' --summary '...'.")
    if kind=='doc':
        text=(f"You changed {data['path']}. Record its load-bearing decision as a concise note pointing to it "
              f"(CMD capture --session {session} --type decision --title '...' --summary '...' --link '{data['path']}')")
        named=data['notes']
        if named:text+=(', or update the note it changes: '+'; '.join(f"{n['short']} {n['title']}" for n in named)
                        +f" (CMD update {named[0]['short']} --session {session} --set-detail '**Label:** ...')")
        return text+'.'
    if kind=='follows':   # several notes about edited files in one sentence instead of one per note
        what='files whose digests may be outdated' if all(d.get('digest') for d in data) else 'files that notes cover'
        text=(f"You edited {what}: {', '.join(d['short']+' ('+d['path']+')' for d in data)}. "
              f"For each: still true, CMD confirm REF --session {session}; changed, CMD update REF --session {session} --set-detail '**Label:** ...'.")
        if any(d.get('type')=='bug' for d in data):text+=f" If one fixed a bug: CMD update REF --session {session} --set-detail '**Fix:** ...' then CMD resolve REF."
        return text
    if kind=='follow':
        s=data['short']
        text=(f"You edited {data['path']}, which note {s} {data['title']} covers. Still true: CMD confirm {s} --session {session}; "
              f"changed: CMD update {s} --session {session} --set-detail '**Label:** ...'.")
        if data.get('type')=='bug':text+=f" If this fixed it: CMD update {s} --session {session} --set-detail '**Fix:** ...' then CMD resolve {s}."
        return text
    if kind=='unbriefed':
        return (f"{data['name']} has no project brief: "
                f"CMD capture --session {session} --type brief --file '{data['dir']}' --title '{data['name']} project brief' --summary '...' "
                "--detail '**Goals and scope:** ...'. "
                "Write what you know now and mark unknowns as unknown; the brief is updated as the project moves.")
    if kind=='brief':
        return (f"a decision was made or recorded this turn: if project brief {data['short']} no longer matches it, "
                f"CMD update {data['short']} --session {session} --set-detail '**Key decisions:** ...' (or Team and ownership, Current phase, status and blockers).")
    return map_sentence(data[0],kind,data[1],session)

HEADLESS_TEXT="This is a non-interactive run and only your last message is returned: after saving, end with your answer to the task repeated in full."

def headless():
    """True in a non-interactive run (`claude -p`, the SDK), where the caller receives only the final message."""
    env=os.environ
    return env.get('LIVING_MEMORY_HEADLESS')=='1' or env.get('CLAUDE_CODE_SESSION_ATTENDED')=='0' or env.get('CLAUDE_CODE_ENTRYPOINT','').startswith('sdk')

def stop_text(pending,session,used=None):
    """The Stop continuation for what stop_pending found, about STOP_TEXT_CAP characters: the first item always,
    later ones while they fit, at most STOP_DOCS documents and STOP_NOTES notes named, and the notes about edited
    files (STOP_FOLLOW at most) together in one sentence when there are several. Short refs and exact commands,
    closing with how to say nothing is durable. used, when a list, receives the items the text covers."""
    parts=[];docs=notes=0;closing=f" Nothing durable: CMD capture-finalize --session {session}."
    # A non-interactive run returns only the last message, so housekeeping after the answer would replace it.
    tail=' '+HEADLESS_TEXT if headless() else ''
    followed=[d for kind,d in pending if kind=='follow']
    if len(followed)>1:   # one sentence, where the first of them stood
        grouped=[]
        for kind,data in pending:
            if kind!='follow':grouped.append((kind,data))
            elif not any(k=='follows' for k,_ in grouped):grouped.append(('follows',followed[:STOP_FOLLOW]))
        pending=grouped
    for kind,data in pending:
        if kind=='doc':
            if docs>=STOP_DOCS:continue
            data={**data,'notes':data['notes'][:max(0,STOP_NOTES-notes)]}
        if kind in ('follow','follows') and notes>=STOP_NOTES:continue
        fits=lambda text:not parts or len('Living Memory: '+' '.join(parts+[text]))+len(closing)+len(tail)<=STOP_TEXT_CAP
        part=_stop_part(kind,data,session)
        if kind in MAP_KINDS+BRIEF_KINDS and parts:part='Also: '+part
        # A document that does not fit with its notes is still asked about, with fewer of them.
        while not fits(part) and kind=='doc' and data['notes']:data={**data,'notes':data['notes'][:-1]};part=_stop_part(kind,data,session)
        # The batch is trimmed to the real budget even as the first part (fits() always admits the first), keeping one note.
        while kind=='follows' and len(data)>1 and len('Living Memory: '+' '.join(parts+[part]))+len(closing)+len(tail)>STOP_TEXT_CAP:data=data[:-1];part=_stop_part(kind,data,session)
        if not fits(part):continue
        parts.append(part)
        if kind=='doc':docs+=1;notes+=len(data['notes'])
        if kind=='follow':notes+=1
        if kind=='follows':notes+=len(data)
        if isinstance(used,list):used.extend([('follow',d) for d in data] if kind=='follows' else [(kind,data)])
    return 'Living Memory: '+' '.join(parts)+closing+tail

def session_map(cwd,root,bindings,state=None,session=None):
    """A pointer to the map of the repo holding cwd, else None; never fails the hook."""
    try:
        from .maps import find_map,pointer_line
        here=git_root(cwd)
        hit=find_map(here,root,bindings) if here else None
        return pointer_line(hit,here,entry=note_currency(hit,here,root,bindings,state or {},session)) if hit else None
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.session_map',exc,root);return None
