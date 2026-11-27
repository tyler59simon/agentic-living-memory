"""Shared deterministic lifecycle behavior; adapters translate native wire formats."""
from __future__ import annotations
import hashlib,json,os,re,sys,time
from pathlib import Path
from .common import memory_root,load_bindings,resolve_domain,atomic_write,canonical_domain,file_lock,head_oid,parse_iso
from .repository import read_json,canonical_json,now
from . import clock
from .capture import status as capture_status,turn_key
from .note_types import NOTE_TYPES

from .instructions import FALLBACK_PROTOCOL,AUTO_MEMORY,fallback_usage,installed
from .shell_guard import (
    SCRIPT,GLOBAL_OPTIONS,VALUE_OPTIONS,ALLOWED_SUBCOMMANDS,MUTATING_SUBCOMMANDS,
    HIVEMIND_GOVERNED,HIVEMIND_DENY,DOC_WRITE_ACTIONS,DOC_AGENT_ACTIONS,PRIVATE_DENY,SERVE_DENY,SUBAGENT_DENY,
    SHELL_META,POWERSHELL_META,STOP_PARSING,POWERSHELL_QUOTES,SHELL_GRAMMARS,
    SHELL_WRAPPERS,PREFIX_COMMANDS,INTERPRETER,ASSIGNMENT,PAGE_ID,FILE_OPTIONS,
    READONLY_SIMPLE,READONLY_GIT,VARIABLE_PARAMETER,FOREACH_PROPERTIES,FIND_WRITERS,NULL_SINKS,
    SHELL_KEYWORDS,SHELL_CLOSERS,SED_FLAGS,SED_SAFE,AWK_UNSAFE,XARGS_VALUED,UNIQ_VALUED,
    active,_norm,shell_command,shell_grammar,_words,_parsed,_option_width,_subcommand,memory_subcommand,
    _names_memory,_doc_action,doc_write,serve_command,_command_words,_memory_option_words,include_private,
    _settles_page,page_settle,_subcommand_rest,config_write,hivemind_governed,_option_values,remote_sync,
    views_write,_json_format,wiki_build,wiki_write,maintenance_write,maintenance_paid,rebaseline_write,
    proposals_write,taxonomy_write,memory_write,subagent_write_verb,auto_allowed,
    _positional,_sed_script_safe,_sed_readonly,_awk_readonly,_xargs_command,_readonly_segment,_shell_pipelines,
    readonly_shell,writing_shell_text,
)
from .signals import (
    MACHINE_PROMPT,IGNORED,QUOTED,QUOTED_CHARS,STATEMENT_BREAK,TRANSIENT,SIGNALS,
    DIRECTIVE_STRONG,DIRECTIVE_WEAK,PRIORITY,SIGNAL_TEXT,BLOCKING_SIGNALS,
    SIGNALS_SEEN,MAX_SIGNALS_SEEN,MAX_SIGNAL_WORDS,MIN_COVER_WORDS,SIGNAL_STOP,
    NUDGE_KINDS,NUDGE_NOTE_TYPES,NUDGE_NOTES,
    _statements,signals_of,signal_of,durable_signal,_statement_hash,signals_seen,
    settle_signal,settle_signals,content_hashes,covers_signal,signal_notes,signal_nudge,
)
from .prompt_context import (
    HIT_SNIPPET,LONG_PROMPT,LONG_SEGMENTS,LONG_LEAST,HEADING,
    LIST_ITEM,LIST_MARK,LIST_MIN,LIST_QUERIES,LIST_HITS,ITEM_LEAST,LIST_CAP,LIST_SNIPPET,LIST_BUDGET,
    MAX_QUESTIONS,DELIVERY,SIBLING_DOMAINS,SIBLING_LEAST_TERMS,
    hit_marks,hit_line,revision_key,_snippeted,_unseen,relevance_gate,
    _is_item,_pieces,prompt_segments,list_items,questions,item_queries,long_queries,
    context,_own_scope,sibling_hit,_deliver,parent_domains,child_domains,scope_domains,child_path,scoped_file_hits,
)
# Tools that cannot change files; anything else marks the turn as mutating.
READ_TOOLS={'read','read_file','glob','grep','search','view_image','view','ls','list','webfetch','websearch','web_fetch','web_search','todowrite','task','agent','toolsearch','askuserquestion','exitplanmode','enterplanmode'}
SHELL_TOOLS={'bash','powershell','shell','run_shell_command','exec_command','local_shell','execute'}
HOOK_LOCK_TIMEOUT=5
# Harnesses without SessionEnd pick up Obsidian edits at session start, at most this often.
SESSION_START_SYNC_SECONDS=15*60

def _configured_root():
    try:
        from .storage import config_path
        return Path(json.loads(config_path().read_text(encoding='utf-8-sig'))['data_root']).resolve()
    except (OSError,ValueError,KeyError,TypeError):return None

def command(root,domain=None):
    """The script invocation agents should run: bare `alm` when the launcher is active (launcher.active), else the quoted interpreter and script. Always --json. --root only when the hook is not using the shared store."""
    text='alm --json' if active(SCRIPT) else f'"{sys.executable}" "{SCRIPT}" --json'
    if Path(root).resolve()!=_configured_root():text+=f' --root "{root}"'
    return text+(f' --domain {domain}' if domain else '')

def hint_command(root,state,domain=None):
    """How a hint to the main agent names the script: CMD when this session's start defined it (a bound
    domain was pinned then), else the whole command line. Shorter, and it needs no shell-specific prefix."""
    return 'CMD' if isinstance(state,dict) and state.get('domain') else command(root,domain)

def _tag_summary(root):
    from .tag_taxonomy import load_tags
    try:
        tags=load_tags(root)
    except ValueError:return ''
    facets={}
    for tag in sorted(tags):
        facet,_,value=str(tag).partition('/')
        if value:facets.setdefault(facet,[]).append(value)
    return '; '.join(f"{facet}/{{{','.join(values)}}}" for facet,values in facets.items())

CUSTOM_TYPES_BUDGET=600

def _custom_types_line(root):
    """One line naming the store's active custom note types, with their sections and whether they take action items, or ''."""
    try:
        from .note_types import custom_types,definition
        parts=[]
        for name,entry in sorted(custom_types(root).items()):
            if entry.get('status','active')!='active':continue
            found=definition(name,root)
            extra=list(found['sections'])+(['action items'] if found['actions'] else [])
            parts.append(name+(f" ({', '.join(extra)})" if extra else '')+': '+found['description'])
        if not parts:return ''
        line='Custom note types (--type NAME): '+'; '.join(parts)
        return line if len(line)<=CUSTOM_TYPES_BUDGET else line[:CUSTOM_TYPES_BUDGET-30].rsplit(' ',1)[0]+' ... (CMD types lists them all)'
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.custom_types_line',exc,root)
        return ''

def protocol(root,domain=None,harness='',session='SESSION',room=None):
    """SessionStart context (re-fires after compaction). When the harness's instruction file carries the
    current static block, only per-session data is injected; otherwise the shorter fallback protocol, so the agent never
    sees zero copies or two differing copies of the rules.
    room: the characters the rules may take (SESSION_LIMITS less the session marker that follows them). Past it, the custom
    types line and then the tag summary give way to the command that lists them."""
    cmd=command(root,domain)
    head=f'CMD = {cmd}'+('' if cmd.startswith('alm ') else '   (Bash: CMD as is; PowerShell: & CMD ...)')
    tags=f"\nTags (pick the closest; --platform/--technology optional): {_tag_summary(root)}"
    custom=_custom_types_line(root)
    def build(tags,custom):
        dynamic=head+tags+('\n'+custom if custom else '')
        if installed(harness):
            return f'Living Memory is active for domain {domain}; follow the Living Memory section of your instructions.\n'+dynamic
        text=FALLBACK_PROTOCOL+'\n'+dynamic+'\n'+fallback_usage(session)
        if harness=='claude':text+='\n'+AUTO_MEMORY
        return text
    text=build(tags,custom)
    if room is not None and len(text)>room and custom:
        custom='Custom note types: CMD types lists them.'
        text=build(tags,custom)
    if room is not None and len(text)>room:
        text=build('\nTags: CMD taxonomy lists them; new notes need existing subject/ and concern/ tags.',custom)
    return text

# The same rule as the instruction block: the hook can refuse editor tools, but shell writes are only covered by that instruction.
STORE_WRITE_DENY=('Change memory only through the Living Memory script (CMD): never create, edit, move or delete store files any other way, by editor tools or shell writes, '
                  "because direct writes skip the revision check and can overwrite another agent's edit.")

def protected(path,cwd,root):return protected_rule(path,cwd,root) is not None

# Who may not edit a page of a folder notebook (a registered folder of Markdown outside the store) directly: 'all' agent sessions,
# 'hivemind' (only the agent terminals Hivemind starts) or 'off'. They stage the edit with doc propose --file instead.
FOLDER_GUARD='all'

def folder_rule(path,cwd,root):
    """The registry entry of the folder notebook that a .md or .markdown file belongs to, else None. Shell writes, copies of the
    folder (a git worktree) and folders known only from a .hivemind sidecar are not covered. The registry is cached by its mtime."""
    if path in {'/dev/null','dev/null'}:return None
    try:
        from .folder_docs import locate
        from .folder_policy import Policy,level_of,stricter
        hit=locate(root,path,cwd)
        if not hit:return None
        entry,folder,rel=hit
        policy=Policy(root)
        base=level_of(entry.get('visibility'),bool(entry.get('private')))
        eff='private' if policy.broken else stricter(base,policy.file(folder/rel)[0])
        return dict(entry,effective_level=eff)
    except (ValueError,OSError):return None

def folder_deny(path,entry,cmd):
    """The refusal for an agent that edits a page of a folder notebook directly."""
    from .folder_policy import level_of
    if entry.get('effective_level')=='private' or level_of(entry.get('visibility'),bool(entry.get('private')))=='private':
        return f'Living Memory: {path} is a page of a private folder notebook; agents may not change it. Tell the user.'
    return (f'Living Memory: {path} is a page of folder notebook {entry.get("path")}. Read it with {cmd} doc read --file "{path}", then stage your edit with '
            f'{cmd} doc propose --file "{path}" --base-revision R --body-file F; the user reviews it with proposals accept|reject (or in a connected UI).')

def protected_rule(path,cwd,root):
    """Which canonical location a path falls in: 'domains', 'config' or 'vault'; None when it is not protected."""
    if path in {'/dev/null','dev/null'}:return None
    try:
        p=Path(os.path.expandvars(path)).expanduser()
        if not p.is_absolute():p=Path(cwd)/p
        p=p.resolve()
        root=Path(root).resolve()
        from .storage import config_path
        from .paths import is_within,path_key
        if is_within(p,root/'domains'):return 'domains'
        if any(is_within(p,root/part) for part in ('ledger','.views','maintenance','.operations','backups','.turns')):return 'config'
        if path_key(p) in {path_key(x) for x in (root/'bindings.json',root/'tag_taxonomy.json',root/'projects.json',root/'systems.json',root/'sources.json',root/'note_types.json',root/'maintenance.json',root/'views.json',root/'.relocated.json',
                 root/'notebook-folders.json',config_path())}:return 'config'
        # Vault edits, archives and deletions are applied at sync, so agents must not write there either.
        return 'vault' if any(is_within(p,v/folder) for v in vault_roots(root) for folder in ('Domains','Archive','Dashboards','Notebooks')) else None
    except (ValueError,OSError):return None

_VAULT_ROOTS_CACHE={}   # str(root) -> (sig, frozenset(Path))

def vault_roots(root):
    root_p=Path(root)
    key=str(root_p)
    domains_dir=root_p/'domains'
    if not domains_dir.is_dir():return set()
    sig=[]
    paths=[]
    try:
        for entry in os.scandir(domains_dir):
            if not entry.is_dir(follow_symlinks=False):continue
            state=Path(entry.path)/'sync-state.json'
            try:st=os.stat(state)
            except OSError:continue
            sig.append((entry.name,st.st_mtime_ns,st.st_size))
            paths.append(state)
    except OSError:return set()
    sig_t=tuple(sorted(sig))
    hit=_VAULT_ROOTS_CACHE.get(key)
    if hit is not None and hit[0]==sig_t:return set(hit[1])
    found=set()
    for state in paths:
        try:vault=json.loads(state.read_text(encoding='utf-8')).get('vault')
        except (OSError,ValueError):continue
        if vault:found.add(Path(vault).resolve())
    if len(_VAULT_ROOTS_CACHE)>16:_VAULT_ROOTS_CACHE.clear()
    _VAULT_ROOTS_CACHE[key]=(sig_t,frozenset(found))
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
        patch=[m.strip() for m in re.findall(r'(?m)^\*\*\* (?:(?:Add|Delete|Update) File|Move to):\s*(.+)$',value) if m.strip()]
        # Quoted absolute paths include spaces. This is an agent guard, not a shell parser.
        quoted=re.findall(r'[\"\x27]((?:[A-Za-z]:[\\/]|/)[^\"\x27]+)[\"\x27]',value)
        absolute=re.findall(r'(?<![\w])(?:[A-Za-z]:[\\/]|/)[^\s\"\x27;|<>]+',value)
        return patch+quoted+absolute
    return []

def _sync_vaults(root,trigger,min_interval=0):
    """Pull Obsidian edits into memory and refresh every managed vault. Never fails the hook."""
    try:
        from .sync import sync_all
        if min_interval:
            last=read_json(root/'hooks/last-sync.json',{}) or {}
            try:age=(clock.now()-parse_iso(last['at'])).total_seconds()
            except (KeyError,TypeError,ValueError):age=None
            if age is not None and age<min_interval:return None
        return sync_all(root,trigger)
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.sync_vaults',exc,root)
        return None

def _daily_backup(root):
    """At most one snapshot a day, taken from session housekeeping. Never fails the hook."""
    try:
        from .backup import maybe_backup
        return maybe_backup(root)
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.daily_backup',exc,root)
        return None

def _old_plan_folders():
    """Where install.py and integrations/setup.py kept their plans before 2026-11-26: each harness home, and the install folder when
    this code is the installed copy (app/ beside it; a source checkout never reaches into the user's install folder)."""
    from .instructions import harness_home
    app=SCRIPT.parent
    homes=[harness_home(h) for h in ('claude','codex','copilot','factory','antigravity')]
    if (app/'.core-ownership-v1.json').is_file():homes.append(app.parent)
    return [home for home in dict.fromkeys(homes) if (home/'.plans').is_dir()]

def _prune_due(root):
    marker=Path(root)/'hooks'/'last-prune.json'
    try:age=(clock.now()-parse_iso((read_json(marker,{}) or {})['at'])).total_seconds()
    except (KeyError,TypeError,ValueError):return True
    return age>=24*3600

def _backup_due(root):
    from datetime import datetime,timezone
    from .backup import INTERVAL_HOURS
    marker=Path(root)/'hooks'/'last-backup.json'
    try:age=(datetime.now(timezone.utc)-parse_iso((read_json(marker,{}) or {})['at'])).total_seconds()/3600
    except (KeyError,TypeError,ValueError):return True
    return age>=INTERVAL_HOURS

def _housekeeping_due(root):return _backup_due(root) or _prune_due(root)

HOUSEKEEP_COOLDOWN=600

def _spawn_housekeeping(root):
    """Start `housekeep` detached via core/background.py."""
    from .background import spawn_detached
    spawn_detached([sys.executable,str(SCRIPT),'--root',str(root),'housekeep'])

def schedule_housekeeping(root):
    """Start a detached `housekeep` worker when daily backup or pruning is due. Never waits and never raises."""
    try:
        from .background import in_cooldown,touch_cooldown
        if os.environ.get('LIVING_MEMORY_NO_BACKGROUND'):return 'background_disabled'
        if os.environ.get('LIVING_MEMORY_MAINTENANCE_CHILD'):return 'maintenance_child'
        if not _housekeeping_due(root):return 'not_due'
        try:
            with file_lock(Path(root)/'hooks'/'housekeep.lock',timeout=0):pass
        except (TimeoutError,OSError):return 'already_running'
        marker=Path(root)/'hooks'/'housekeep.spawned'
        if in_cooldown(marker,HOUSEKEEP_COOLDOWN):return 'cooldown'
        _spawn_housekeeping(root)
        touch_cooldown(marker)
        return 'spawned'
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.schedule_housekeeping',exc,root)
        return 'error'

def housekeep(root):
    """Run daily backup and pruning under a non-blocking worker lock; one worker at a time."""
    from contextlib import ExitStack
    root=Path(root).resolve()
    with ExitStack() as stack:
        try:stack.enter_context(file_lock(root/'hooks'/'housekeep.lock',timeout=0))
        except (TimeoutError,OSError):return {'ok':True,'skipped':'already_running','backup':False,'pruned':False}
        backup=_daily_backup(root)
        pruned=_prune_journals(root)
        return {'ok':True,'backup':bool(backup),'pruned':pruned is not None,'counts':pruned}

def _prune_journals(root):
    """At most once a day, drop old sync-bookkeeping journals from the store and each managed vault.
    Never fails the hook."""
    try:
        from contextlib import ExitStack
        from .repository import prune_journals,remove_leftover_plans
        root=Path(root).resolve()
        if not _prune_due(root):return None
        with ExitStack() as stack:
            try:stack.enter_context(file_lock(root/'hooks'/'prune.lock',timeout=0))
            except (TimeoutError,OSError):return None
            if not _prune_due(root):return None
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
            from .docs import prune_journals as prune_page_journals
            out['notebook_operations']=prune_page_journals(root)
            # Plans were removed on 2026-11-26; their leftover files go. A harness home is not ours: only files shaped like a plan are touched.
            for folder in sorted(roots):remove_leftover_plans(Path(folder))
            for home in _old_plan_folders():remove_leftover_plans(home,require_plan=True)
            atomic_write(root/'hooks'/'last-prune.json',canonical_json({'at':now()}))
            return out
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.prune_journals',exc,root)
        return None

AUTO_RESOLVE_DAYS=3

def auto_resolve(root,domain,bindings,session=None):
    """Resolve the active domain's finished work: each active plan, bug or risk note whose Action Items are all done and that nobody has touched
    for AUTO_RESOLVE_DAYS days. At most once a day per domain (hooks/last-auto-resolve.json, like _prune_journals), never in a headless run unless the clock is simulated,
    and off with `"auto_resolve": false` at the top level of bindings.json. Each resolve is the journaled, undoable `resolve` change (`reopen REF`
    brings the note back) and is recorded as use. Returns [(short ref, title)] for the notes resolved; fails open: []."""
    try:
        from datetime import timedelta,timezone
        if bindings.get('auto_resolve',True) is False or (headless() and not clock.simulated()):return []
        marker=root/'hooks'/'last-auto-resolve.json'
        marks=read_json(marker,{})
        marks=marks if isinstance(marks,dict) else {}
        try:age=(clock.now()-parse_iso(marks[domain])).total_seconds()
        except (KeyError,TypeError,ValueError):age=None
        if age is not None and age<24*3600:return []
        atomic_write(marker,canonical_json({**marks,domain:now()}))
        from .note_format import action_items
        from .note_types import takes_actions
        from .notes import change,short_ref
        from .repository import inventory
        from .usage import record
        cutoff=clock.now()-timedelta(days=AUTO_RESOLVE_DAYS)
        done=[]
        for note in inventory(domain,root):
            if not takes_actions(note.get('type'),root) or not note.get('id') or not note.get('title') or note.get('pinned') is True:continue   # a pin blocks automatic retirement
            items=action_items(note.get('body',''))
            if not items or not all(d for d,_ in items):continue
            try:
                stamp=parse_iso(str(note.get('updated') or note.get('created') or ''))
                if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
            except ValueError:continue
            if stamp>cutoff:continue
            try:change(note['id'],domain,'resolve',root,expected_revision=note.get('revision'),actor='auto_resolve')
            except (OSError,ValueError,TimeoutError) as exc:   # one stale or locked note must not stop the rest
                from .logs import swallowed
                swallowed('runtime.auto_resolve',exc,root)
                continue
            record(root,'resolve',[f"{domain}:{note['id']}"],session,'auto-resolve')
            done.append((short_ref(domain,note['id']),note['title']))
        return done
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.auto_resolve',exc,root)
        return []

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
    for shell tools only files inside the bound project count, only from the commands that may write
    (writing_shell_text), and a comma-joined list (`-Path a,b`) counts as separate paths."""
    from .common import domain_prefixes,display_path
    try:
        scratch=payload.get('scratchpad_dir') or payload.get('scratchpadDir')
        base=Path(scratch).resolve() if scratch else None
        prefixes=domain_prefixes(domain,bindings)
        store=Path(root).resolve()
        found=[]
        # Only a command line is split; an argv list (["rm", "a", "b"]) keeps every element.
        text=args.get('command',args.get('cmd')) if shell and isinstance(args,dict) else None
        text=text if isinstance(text,str) else ''
        raws=raw_paths(writing_shell_text(text)) if text else raw_paths(args)
        if shell:raws=[part for raw in raws for part in re.split(r',(?=[A-Za-z]:[\\/]|/)',raw)]
        for raw in raws:
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
        swallowed('runtime.touched_paths',exc,root)
        return []

# Hosts whose PreToolUse output documents additionalContext (Claude Code; Codex since openai/codex#20692).
# Copilot CLI, Factory and Antigravity CLI document no PreToolUse context, so they get no hints.
FILE_HINT_HARNESSES={'claude','codex'}
MAX_FILE_HINTS=200
HINT_FILES_PER_CALL=3
HINT_NOTES_PER_FILE=2
MAX_HINTED=100
FOLLOW_BODY_TYPES={'bug','decision'}
MAX_DOCS=10
MAX_DOCS_REMINDED=50
MAX_SETTLED=50   # approvals (approved_decisions keys) already reminded about or receipted this session

def file_hints(paths,state,root,domain,bindings,session=None,trace=None,follow=None,digested=None):
    """(context naming notes about files about to be changed, paths looked up). Each file is looked up
    once per session; a missing or stale index gives no hint rather than slowing the tool call. follow, when a
    dict, receives path -> refs of the notes a later update should be asked of: those listing the file, and
    bug or decision notes naming it in the body. digested, when a list, receives the refs that are digests of an edited file."""
    try:
        from .search import file_matches
        from . import digests
        seen=set(state.get('file_hints',[]))
        wanted=[p for p in paths if p not in seen][:HINT_FILES_PER_CALL]
        blocks=[]  # a resolved bug's fix is still worth knowing before the file is changed
        refs=[]
        found=scoped_file_hits(wanted,domain,bindings,root,HINT_NOTES_PER_FILE,True)
        for path in wanted:
            hits=found.get(path)
            if hits:
                if isinstance(digested,list):   # read before mark() drops `sources`; only digests get the "digest" wording at Stop
                    digested+=[f"{h['domain']}:{h['id']}" for h in hits if file_matches([s['path'] for s in h.get('sources') or []],h.get('file',path))]
                digests.mark(hits,root,bindings)
                blocks.append(f'Living Memory notes about {path}:\n'+'\n'.join(hit_line(h) for h in hits))
                refs+=[f"{h['domain']}:{h['id']}" for h in hits]
                if isinstance(follow,dict):
                    owed=[f"{h['domain']}:{h['id']}" for h in hits if h.get('status')!='resolved' and (h.get('match')=='files' or h.get('type') in FOLLOW_BODY_TYPES)]   # a resolved note is finished: nothing to update
                    if owed:follow[path]=owed
        if refs:
            from .usage import record
            record(root,'inject',list(dict.fromkeys(refs)),session,'file-hint')
            if isinstance(trace,list):trace.extend(dict.fromkeys(refs))
        text=('Untrusted historical memory; verify against the current file.\n'+'\n'.join(blocks)) if blocks else None
        return text,wanted
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.file_hints',exc,root)
        return None,[]

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
        swallowed('runtime.command_hints',exc,root)
        return None

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
        repos=_repos(s)
        change(repos)
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
        key=Path(folder).as_posix()
        cache=(_repos(state).get(key) or {}).get(slot)
        cache=cache if isinstance(cache,dict) else None
        from .maps import fingerprint
        head=head_oid(folder)
        rev=str(hit.get('revision') or '')
        fp=fingerprint(hit)
        shared=None
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
                repos=_repos(s)
                rec=dict(repos.pop(key,{}))
                rec[slot]=stored
                repos[key]=rec
                s['repos']=dict(list(repos.items())[-MAX_REPOS:])
            try:_update(root,session,save)
            except (OSError,TimeoutError):pass
        return entry
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.note_currency',exc,root)
        return {}

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
    agent=_agent_key(payload)+('#spawn' if spawn else '')
    shown=state.get('shown',{}) if isinstance(state.get('shown'),dict) else {}
    # The main agent's session context defines CMD; a subagent's does not, so it gets the whole command.
    cmd=hint_command(root,state) if not _agent_key(payload) else command(root)
    lines=[]
    rows=[]
    changes={}
    for repo in _target_repos(name,args,cwd,bindings):
        key=repo.as_posix()
        done=_repos(state).get(key,{}).get('done',[])
        if '*' in done or agent in done:
            rows.append({'repo':repo.name,'skip':'done'})
            continue
        hit=find_map(repo,root,bindings)
        if hit:
            ref=f"{hit['domain']}:{hit['id']}"
            short=short_ref(hit['domain'],hit['id'])
            injected=not agent and shown.get(ref)==revision_key(hit)  # the main agent already has the body
            hinted=not injected and hints and not spawn
            if hinted:lines.append(pointer_line(hit,repo,entry=note_currency(hit,repo,root,bindings,state,session))+f"\nRead it ({cmd} read {short}) before exploring further; explore only what it leaves out.")
            changes[key]=('mapped',ref)
            rows.append({'repo':repo.name,'map':ref,'hint':hinted})
        else:
            changes[key]=('spawn' if name in SPAWN_TOOLS else 'read',None)
            rows.append({'repo':repo.name,'map':None})
    def track(repos):
        for key,(kind,ref) in changes.items():
            rec=dict(repos.pop(key,{}))
            if kind=='mapped':
                rec['map']=ref
                rec['done']=list(dict.fromkeys(list(rec.get('done',[]))+[agent]))
            else:
                rec.pop('map',None)
                rec['reads']=EXPLORE_READS if kind=='spawn' else min(EXPLORE_READS,int(rec.get('reads') or 0)+1)
                if rec['reads']>=EXPLORE_READS:
                    rec['explored']=True
                    rec['done']=['*']
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

def prompt_maps(cwd,prompt,root,bindings,state,trace=None,briefed=None,session=None,level=0):
    """(text or None, {ref: revision key} delivered, {repo path: map ref or None}) for the repos a prompt names
    and the repo holding cwd: a mapped repo's map body once per revision, followed by one-line summaries of the notes
    about that repo (refs appended to briefed); an unmapped one a single line, once.
    level (ORIENTATION_LEVELS) trims to fit the hook cap: 1 drops the summaries, 2 makes maps after the first a pointer
    line, 3 makes every map one. A pointer is not delivered, so the body comes on a later prompt or with the tool hint."""
    from .maps import named_repos,candidate_repos,find_map,map_block,repo_name,repo_notes,pointer_line,MAX_MAPS
    from .notes import short_ref
    briefed=[] if briefed is None else briefed
    repos=named_repos(prompt,candidate_repos(cwd,bindings),bindings.get('repo_aliases'))
    here=git_root(cwd)
    if here and here.resolve() not in repos:repos.append(here.resolve())
    shown=state.get('shown',{}) if isinstance(state.get('shown'),dict) else {}
    maps=[]
    lines=[]
    delivered={}
    seen={}
    pointers={}
    for repo in repos[:MAX_MAPS+1]:
        key=repo.as_posix()
        hit=find_map(repo,root,bindings)
        if hit:
            ref=f"{hit['domain']}:{hit['id']}"
            seen[key]=ref
            if len(maps)<MAX_MAPS and shown.get(ref)!=revision_key(hit):
                entry=note_currency(hit,repo,root,bindings,state,session)
                if level>=3 or (level>=2 and maps):
                    maps.append(pointer_line(hit,repo,entry=entry)+f"\nRead it ({hint_command(root,state)} read {short_ref(hit['domain'],hit['id'])}) before exploring further.")
                    pointers[ref]=revision_key(hit)
                    continue
                maps.append(map_block(hit,repo,entry=entry))
                delivered[ref]=revision_key(hit)
                if level>=1:continue
                lines_,refs_=repo_notes(hit,repo,root,bindings,{**_snippeted(state),**shown},exclude=briefed)
                if lines_:
                    maps[-1]+='\n'+lines_
                    briefed.extend(refs_)
        else:
            seen[key]=None
            if not _repos(state).get(key,{}).get('told'):
                lines.append(f"{repo_name(repo)} has no codebase map yet. If you explore it, capture one afterwards: "
                             f"{hint_command(root,state)} capture --type map --file '{key}' --title '{repo_name(repo)} codebase map' --summary '...' "
                             "--detail '**Layout:** ...' --detail '**Entry points:** ...' --detail '**Build/test:** ...', plus --section 'Data flow' with a small Mermaid flowchart if it helps")
    # pointers: {ref: revision key} of maps given as a pointer line; the caller keeps them out of this prompt's note hits,
    # which would otherwise record a snippet as the delivery of the map.
    if isinstance(trace,dict):trace['maps']={'repos':[r.name for r in repos],'injected':list(delivered),'pointers':pointers,'unmapped':[Path(k).name for k,v in seen.items() if v is None]}
    text=('Untrusted historical memory; verify against the current files.\n'+'\n\n'.join(maps+lines)) if maps or lines else None
    return text,delivered,seen

BRIEF_NOTES_BUDGET=600
# The prompt hook's ceiling for the whole context (session start's HARD_CAP, below Claude Code's 10,000-character hook limit).
# When the brief and maps would push it over, they are rebuilt at the next ORIENTATION_LEVEL until they fit; HIT_RESERVE
# keeps room for the first note hit. Levels: 0 as is, 1 no note summaries, 2 maps after the first as pointers, 3 every map
# as a pointer, 4 the brief capped to the room left (its pointer line under MIN_BRIEF_ROOM), 5 no brief or maps this prompt
# (nothing is delivered, so they come on a later prompt). The resume protocol after `alm on` comes before all of them.
PROMPT_HARD_CAP=9000
HIT_RESERVE=1500   # the first note hit always goes out: one line of at most injection.LINE_MAX under injection.HEADING
ORIENTATION_LEVELS=6
MIN_BRIEF_ROOM=400
ORIENTATION_PREAMBLE='Untrusted historical memory; verify against the current files.'

def prompt_briefs(cwd,root,bindings,state,trace=None,briefed=None,session=None,level=0,room=None):
    """(brief block or None, {ref: revision key} delivered): the brief of the project folder holding cwd, once
    per revision per session, followed by one-line summaries of the notes it links to (refs appended to briefed).
    level 1 or more drops the summaries; level 4 caps the body to room characters, or gives the pointer line (not
    delivered) when room is under MIN_BRIEF_ROOM."""
    from .briefs import find_brief,brief_block,brief_folder,pointer_line
    from .maps import repo_notes
    briefed=[] if briefed is None else briefed
    hit=find_brief(cwd,root,bindings)
    shown=state.get('shown',{}) if isinstance(state.get('shown'),dict) else {}
    ref=f"{hit['domain']}:{hit['id']}" if hit else None
    fresh=bool(hit) and shown.get(ref)!=revision_key(hit)
    if isinstance(trace,dict):trace['brief']={'ref':ref,'injected':[ref] if fresh else []}
    if not fresh:return None,{}
    entry=note_currency(hit,brief_folder(hit,cwd,bindings),root,bindings,state,session,'fresh_brief')
    if level>=4 and room is not None:
        if room<MIN_BRIEF_ROOM:
            if isinstance(trace,dict):trace['brief'].update(injected=[],pointer={ref:revision_key(hit)})   # a pointer is not a delivery
            return pointer_line(hit,entry=entry),{}
        return brief_block(hit,budget=room,entry=entry),{ref:revision_key(hit)}
    block=brief_block(hit,entry=entry)
    if level>=1:return block,{ref:revision_key(hit)}
    lines_,refs_=repo_notes(hit,None,root,bindings,{**_snippeted(state),**shown},budget=BRIEF_NOTES_BUDGET,files=False,exclude=briefed)
    if lines_:
        block+='\n'+lines_
        briefed.extend(refs_)
    return block,{ref:revision_key(hit)}

# Source excerpts (core/sources.py): when notes answer a prompt poorly, the project's own docs and code may.
SOURCE_EXCERPTS=2
SOURCE_BUDGET=800
SOURCE_SECONDS=0.3   # the excerpt channel stops asking further queries after this long
SOURCE_GATE=0.67
MAX_SOURCES_SHOWN=50

def prompt_sources(prompt,root,domain,state,trace=None):
    """(context block or None, [keys shown]): up to SOURCE_EXCERPTS excerpts from the domain's knowledge-base index, SOURCE_BUDGET
    characters in all. The prompt's most informative terms (the long-prompt machinery, with the index's own vocabulary) must
    be carried by the chunk: SOURCE_GATE of their weight and at least two terms (three for a long prompt). With a semantic model the chunk's
    vector must also be at least the model's threshold from the query (a test file that merely shares the words fails it). An excerpt shown
    earlier this session is not shown again. Reads the index without refreshing it.
    Injectable folder notebooks (NS-2B) share the channel and its rules: their pages come from the same index, each labelled
    [notebook TITLE]. Searchable, hidden and private notebooks never reach it. No further query starts after SOURCE_SECONDS."""
    from . import sources
    from .folder_policy import Policy
    trace=trace if isinstance(trace,dict) else {}
    started=time.monotonic()
    if not sources.db_path(root,domain).is_file():
        trace['index']=False
        return None,[]
    from .search import terms
    text=sources.split_camel(prompt)
    words=terms(text)
    vocab=sources.vocabulary(root,domain,words)
    if not vocab[0]:
        trace['index']=False
        return None,[]
    long=len(prompt)>LONG_PROMPT
    queries=[(text,True)]
    if long:
        queries=[]
        for q in long_queries(text,vocab)+[(q,True) for q in item_queries(text,vocab)]:   # list items are asked on their own (at most LIST_QUERIES)
            if q[0] not in dict(queries):queries.append(q)
    policy=Policy(root)   # one registry read for every query
    width=max(120,(SOURCE_BUDGET-110-len(hint_command(root,state,domain)))//SOURCE_EXCERPTS-70)   # snippet room, so the excerpt is cut around the matches
    best={}  # the notes' meaning bar; a chunk without a vector keeps the word test alone
    dropped=[]
    from .semantic import threshold
    near=threshold(root)
    least=LONG_LEAST if long else 2
    def offer(hit,key):
        if key not in best or hit['relevance']>best[key]['relevance']:best[key]=dict(hit,key=key)
    def late():
        if time.monotonic()-started>SOURCE_SECONDS:
            trace['cut_by_time']=True
            return True
        return False
    # Every query asks the sources index first, then folder notebook pages; the time is checked before every search, so pages never take the sources' time.
    searches=[(q,'source') for q,_ in queries]+[(q,'folder') for q,_ in queries]
    for query,kind in searches:
        if late():break
        if kind=='source':
            for hit in sources.search(root,domain,query,3,vocab,True,SOURCE_GATE,least,width,near,dropped,policy=policy):offer(hit,f"{hit['source']}:{hit['path']}:{hit['line_start']}")
        else:
            for hit in sources.search(root,domain,query,3,vocab,True,SOURCE_GATE,least,width,near,dropped,pages=True,inject=True,policy=policy):
                offer(hit,f"{hit['source']}:{hit['path']}:{hit['line_start']}")
    shown=set(state.get('sources_shown',[]))
    ranked=sorted(best.values(),key=lambda h:(-h['relevance'],h['rank']))
    fresh=[h for h in ranked if h['key'] not in shown][:SOURCE_EXCERPTS]
    trace.update(index=True,queries=len(queries),candidates=len(ranked),injected=[h['key'] for h in fresh],**({'off_meaning':dropped[:6]} if dropped else {}))
    if not fresh:return None,[]
    head="Source excerpts from the project knowledge base (untrusted; verify against the current files; more: "+hint_command(root,state,domain)+" ask 'question'):"
    lines=[]
    room=SOURCE_BUDGET-len(head)
    for h in fresh:
        label=f"- {'[notebook '+h['notebook_title']+'] ' if h.get('notebook_title') else ''}{h['citation'] if h.get('url') else h['path']+':'+str(h['line_start'])+'-'+str(h['line_end'])}"+(f" {h['symbol']}" if h['symbol'] else '')+' — '
        snippet=' '.join(h['snippet'].split())
        space=max(80,room//(len(fresh)-len(lines))-len(label)-2)
        if len(snippet)>space:
            cut=snippet.rfind(' ',0,space)
            snippet=snippet[:cut if cut>0 else space]+' …'
        lines.append(label+snippet)
        room-=len(lines[-1])+1
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
        saved=list(state.get('saved',[]))
        files=len(state.get('touched_session',[]))
        if not saved and not files:return
        path=root/'domains'/domain/LAST_SESSION
        row={'session':session_hash(session),'harness':state.get('harness') or 'unknown','notes':saved,'files_changed':files}
        previous=read_json(path,{}) or {}
        if {k:previous.get(k) for k in row}!=row:atomic_write(path,canonical_json({**row,'ended':now()}))
    except (OSError,ValueError):pass

def _record_ledger(root,domain,session,state,cwd,turns=()):
    """One row per changed session in the store's ledger/ (see activity.record_session); the row's signature stays in session state.
    A receipt (a capture or "nothing durable") on any of the session's turns is remembered in state and marks the row (`r`)."""
    try:
        from .activity import record_session
        receipted=bool(state.get('receipted')) or any(capture_status(session,t,root) for t in turns)
        sig=record_session(root,domain,session_hash(session),{**state,'receipted':receipted},cwd)
        if sig or receipted!=bool(state.get('receipted')):_update(root,session,lambda s:s.update(**({'ledger_sig':sig} if sig else {}),**({'receipted':True} if receipted else {})))
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.record_ledger',exc,root)

def _pointer(root,session):return root/'.turns'/(hashlib.sha256(session.encode()).hexdigest()+'.current.json')

# The per-session off switch: `alm off` sets state['off'], LIVING_MEMORY_OFF=1 in the harness's environment turns a whole session off.
# Off means the session sees no memory and leaves nothing behind: no injection, saves, reminders, diary rows or session state.
OFF_ENV='LIVING_MEMORY_OFF'
OFF_NOTICE=('Living Memory is off for this session (the user turned it off). Do not search, read or save memory, '
            'and ignore earlier Living Memory instructions until it is turned back on.')
BACK_ON='Living Memory is back on for this session.'

def env_off():return os.environ.get(OFF_ENV,'').strip().lower() in {'1','true','yes'}

def session_off(state):
    # 'env' when the environment turns memory off, 'switch' when `alm off` did, else None.
    if env_off():return 'env'
    return 'switch' if isinstance(state,dict) and state.get('off') else None

# Compaction. The harness's summary carries the conversation; Living Memory adds what it does not: the notes
# this session saved and surfaced, and a way back to exact detail (recall over the transcript, read on demand).
AUTO_RECALL_PROMPTS=5
HANDOFF_BUDGET=1200
HANDOFF_NOTES=5
MAX_SURFACED=30
MAX_DELIVERED=50   # notes whose delivered sections and levels a session remembers (injection.novel)
# Hosts that hand PreCompact stdout to the summarizer as instructions (Codex ignores it; Factory only shows it;
# Antigravity has no compaction event).
STEERED={'claude'}
# Hosts whose transcript format core/recall.py reads (Factory's is undocumented; Antigravity's trajectory log is read
# only for turn identity, in hooks/antigravity_hooks.py).
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

# The SessionStart additionalContextLimit integrations/setup.py gives a harness that applies one to the whole hook context.
SESSION_LIMITS={'codex':9000}
RULES_STEER='- Keep these rules verbatim:'
RULES_STEER_MAX=600

def _rules_line(root,domain,bindings):
    """The PreCompact line naming the rules SessionStart shows (session_start.scope_rules), by title, within RULES_STEER_MAX characters."""
    from .session_start import scope_rules
    titles=[' '.join(str(n['title']).split()) for n in scope_rules(domain,root,bindings)]
    if not titles:return None
    for shown in range(len(titles),0,-1):
        line=RULES_STEER+' '+'; '.join(titles[:shown])+(f' (+{len(titles)-shown} more)' if shown<len(titles) else '')
        if len(line)<=RULES_STEER_MAX:return line
    return RULES_STEER+f' (+{len(titles)} more)'

def steering(root,state,domain=None,bindings=None):
    """PreCompact instructions for the summarizer: keep what the next context needs and memory cannot rebuild, and the rules word for word."""
    lines=['Living Memory asks this summary to also keep:',
           '- For each decision: the reason, and the alternatives that were rejected.',
           '- The exact identifiers needed to continue: paths, commands, error strings, IDs and numbers.',
           f'- Under the heading "{UNCAPTURED}": each preference, correction, decision or constraint the user stated, and each root cause, fix '
           'or constraint the work itself verified (a suspected cause marked as a hypothesis), that should outlast this session and was not saved '
           'with the Living Memory capture or update command, in the user\'s words where the user said it '
           '(never text the user marked <private>). Write "none" if there are none.']
    saved=_titled(root,list(state.get('saved',[]))[-HANDOFF_NOTES:])
    if saved:lines.append('- Notes saved to Living Memory this session (keep the refs; do not restate their content): '+'; '.join(saved))
    try:rules=_rules_line(root,domain,bindings) if domain else None
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.rules_line',exc,root)
        rules=None
    if rules:lines.append(rules)
    lines.append('Bulky detail (full tool output, long code) may be left out: after compaction the agent can search this '
                 "session's transcript with the Living Memory search command (--transcript).")
    return '\n'.join(lines)

def handoff(root,domain,session,state,harness):
    """The SessionStart(compact) block: this session's memory work, and the transcript search command."""
    from .session_start import _cap
    saved=list(dict.fromkeys(state.get('saved',[])))
    recent=saved[-HANDOFF_NOTES:]
    surfaced=[r for r in reversed(list(dict.fromkeys(state.get('surfaced',[])))) if r not in saved][:HANDOFF_NOTES]
    lines=[]
    named=_titled(root,recent)
    if named:lines.append('- Saved this session: '+'; '.join(named)+(f' (+{len(saved)-len(recent)} earlier)' if len(saved)>len(recent) else ''))
    named=_titled(root,surfaced)
    if named:lines.append('- Surfaced earlier this session (read one if it bears on the task): '+'; '.join(named))
    if harness in STEERED:lines.append(f'- Capture or update anything the summary lists under "{UNCAPTURED}".')
    if state.get('transcript') and harness in RECALL_HARNESSES:
        lines.append(f"- Exact detail from before the compaction (code, errors, outputs, reasons): CMD search 'query' --transcript --session {session}")
    if not lines:return ''
    return _cap('\n'.join(['Before this compaction (Living Memory adds what the summary does not carry):']+lines),HANDOFF_BUDGET)+'\n'

def _update(root,session,change):
    pointer=_pointer(root,session)
    with file_lock(pointer.with_suffix('.lock'),timeout=HOOK_LOCK_TIMEOUT):
        state=read_json(pointer,{})
        if not isinstance(state,dict):state={}
        change(state)
        atomic_write(pointer,canonical_json(state))
    return state

def process(harness,event,payload,root=None):
    """No store-wide lock: hooks must not serialize every session's tool calls.
    Writes below are atomic files; search and capture take the store lock themselves."""
    if os.environ.get('LIVING_MEMORY_MAINTENANCE_CHILD')=='1':return {}   # the maintenance reviewer's own process: no hook may reach the store
    from . import repository
    from .storage import resolve_root
    root=resolve_root(root)[0]
    from . import logs
    logs.use(root)
    info={}
    start=time.perf_counter()
    session=str(payload.get('session_id') or payload.get('sessionId') or '')
    fields=lambda:{'harness':harness,'domain':info.get('domain'),'session':session_hash(session) if session and session!='unknown' else None,
                   'ms':round((time.perf_counter()-start)*1000,1)}
    previous=repository.LOCK_TIMEOUT
    repository.LOCK_TIMEOUT=HOOK_LOCK_TIMEOUT
    def extra(result):
        """Decision detail at debug (refs, numbers, reason codes); text only for a sandbox store (logs.content_allowed)."""
        found={}
        if logs.enabled('debug'):
            detail=dict(info.get('detail') or {})
            if isinstance(detail.get('candidates'),list):detail['candidates']=detail['candidates'][:20]
            found.update(turn=session_hash(info['turn']) if info.get('turn') else None,detail=detail or None)
        if logs.content_allowed(root) and not info.get('off'):
            args=payload.get('tool_input',payload.get('toolArgs',payload.get('toolArguments')))
            found['content']=logs.content({'prompt':payload.get('prompt') or payload.get('user_prompt') or payload.get('userPrompt') or payload.get('transformedPrompt'),
                                           'tool_input':args,**{k:result.get(k) for k in ('context','deny','allow','continue_reason','warning','instructions')}})
        return found
    try:result=_process(harness,event,payload,root,info)
    except Exception as exc:
        logs.write('error','hook',event,root,**fields(),**extra({}),error=logs.error_fields(exc))
        raise
    finally:repository.LOCK_TIMEOUT=previous
    context=result.get('context') or result.get('instructions')
    outcome=sorted(k for k in ('context','deny','allow','continue_reason','warning','instructions') if result.get(k))
    # Most tool calls get no decision and no context; those rows (about three in four) are kept at debug only (LOG-1).
    level='warning' if result.get('warning') else 'debug' if event=='pre-tool-use' and not outcome else 'info'
    logs.write(level,'hook',event,root,**fields(),outcome=outcome,context_chars=len(context) if isinstance(context,str) else None,**extra(result))
    return result

def _parse_tool(payload):
    """(lowered tool name, decoded args, shell command text, shell grammar) from a pre-tool-use payload."""
    name=str(payload.get('tool_name') or payload.get('toolName') or '').lower()
    args=payload.get('tool_input',payload.get('toolArgs',payload.get('toolArguments',{})))
    if isinstance(args,str):
        try:args=json.loads(args)
        except json.JSONDecodeError:pass
    text=shell_command(args) if name in SHELL_TOOLS else ''
    return name,args,text,shell_grammar(name)

def _pre_tool_guards(name,args,text,grammar,cwd,root,domain,state,payload,detail):
    """All security/governance pre-tool-use denials; runs before both off-mode and unbound-directory returns (REL-S1)."""
    rule=next((r for r in (protected_rule(p,cwd,root) for p in raw_paths(args)) if r),None) if name not in READ_TOOLS and name not in SHELL_TOOLS else None
    if rule:
        detail.update(tool=name,decision='deny:protected:'+rule)
        return {'deny':STORE_WRITE_DENY}
    if os.environ.get('HIVEMIND_SESSION') and text and hivemind_governed(text,grammar):
        detail.update(tool=name,decision='deny:hivemind')
        return {'deny':HIVEMIND_DENY}
    if text and include_private(text,grammar):
        detail.update(tool=name,decision='deny:private')
        return {'deny':PRIVATE_DENY}
    if text and serve_command(text,grammar):   # G8: its requests could carry --include-private past this check
        detail.update(tool=name,decision='deny:serve')
        return {'deny':SERVE_DENY}
    if name in EDIT_TOOLS and (FOLDER_GUARD=='all' or (FOLDER_GUARD=='hivemind' and os.environ.get('HIVEMIND_SESSION'))):
        for raw in raw_paths(args):
            entry=folder_rule(raw,cwd,root)
            if entry:
                detail.update(tool=name,decision='deny:folder')
                return {'deny':folder_deny(raw,entry,hint_command(root,state,domain))}
    if _subagent(payload) and text:
        verb=subagent_write_verb(text,grammar)
        if verb:
            detail.update(tool=name,subcommand=verb,decision='deny:subagent')
            return {'deny':SUBAGENT_DENY+verb+'.'}
        sub=memory_subcommand(text,grammar)
        if sub=='source-sync' and remote_sync(text,grammar,root):
            detail.update(tool=name,subcommand=sub,network=True,decision='deny:subagent')
            return {'deny':'Living Memory: syncing a remote source reaches the network and is not run by subagents; ask the main agent to run source-sync.'}
    return None

def _process(harness,event,payload,root=None,info=None):
    info=info if info is not None else {}
    detail=info.setdefault('detail',{})  # why this hook did what it did: refs, numbers and reason codes only
    root=Path(root or memory_root()).resolve()
    cwd=payload.get('cwd') or payload.get('workingDirectory') or os.getcwd()
    session=str(payload.get('session_id') or payload.get('sessionId') or 'unknown')
    turn=str(payload.get('turn_id') or payload.get('turnId') or '')
    bindings=load_bindings(root)
    state=read_json(_pointer(root,session),{}) if session!='unknown' else {}
    if not isinstance(state,dict):state={}
    pinned=state.get('domain')
    domain=pinned if pinned and pinned in bindings.get('domains',{}) else resolve_domain(cwd,bindings)
    info['domain']=domain
    tool_info=_parse_tool(payload) if event=='pre-tool-use' else None
    if tool_info is not None:
        denied=_pre_tool_guards(*tool_info,cwd,root,domain,state,payload,detail)
        if denied is not None:return denied
    off=session_off(state)
    if off:
        info['off']=True
        detail['reason']='off'
        detail['off']=off
        if event=='session-end':
            # Store-wide housekeeping only; nothing about this session is recorded.
            _sync_vaults(root,'session end')
            detail['housekeeping']={'worker':schedule_housekeeping(root)}
            return {}
        if event=='pre-tool-use':
            return {}
        if off=='switch' and session!='unknown' and not _subagent(payload):
            # The session saw the protocol before `alm off`: say once that it no longer applies (again after a compaction).
            if event=='session-start':return {'context':OFF_NOTICE}
            if event=='user-prompt' and not state.get('off_told'):
                _update(root,session,lambda s:s.update(off_told=True))
                return {'context':OFF_NOTICE}
        return {}
    resumed=bool(state.get('resumed'))
    source=str(payload.get('source') or '').lower()
    transcript=None if _subagent(payload) else _transcript(payload)
    if event=='session-start' and session!='unknown':
        current=resolve_domain(cwd,bindings) or domain
        domain=current
        info['domain']=domain
        def pin(s):
            s['harness']=harness
            if current:s['domain']=current
            else:s.pop('domain',None)
            if transcript:s['transcript']=transcript
            # The first prompts after a compaction may ask about something only the transcript still holds.
            if source=='compact':
                s['recall_prompts']=AUTO_RECALL_PROMPTS
                s['recalled']=[]
        state=_update(root,session,pin)
    statements={}
    if event=='user-prompt':
        turn=turn or str(payload.get('timestamp') or now())
        if session!='unknown':
            # Plan mode cannot save anything, so a statement there is not a reminder's business.
            planning=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan'
            text=payload.get('prompt') or payload.get('user_prompt') or payload.get('userPrompt') or payload.get('transformedPrompt') or ''
            raised,hashed=signals_seen(root,domain) if domain else (None,None)
            kinds=[] if planning else signals_of(text,raised)
            signal,source=kinds[0] if kinds else (None,None)
            # Leaving plan mode is the user approving a plan: a decision even when the prompt says nothing durable (UX-C4).
            mode=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()
            # A blocking statement already holds Stop; a preference or correction does not, so the approval outranks it.
            approved=bool(mode) and mode!='plan' and state.get('last_mode')=='plan' and signal not in BLOCKING_SIGNALS
            statements=dict(kinds)   # held for this hook only, to search the notes a statement may change
            # Only the kind and a salted hash of the statement are kept, never the prompt text; the hash settles at Stop.
            shash=hashed(source) if signal and hashed else None
            # Every statement the nudge names settles at Stop, so none of them is raised again when repeated.
            shashes=list(dict.fromkeys(hashed(s) for _,s in kinds)) if signal and hashed else []
            from . import private
            detail.update(signal=signal,signals=[k for k,_ in kinds],private_spans=len(private.spans(text)) if isinstance(text,str) and not MACHINE_PROMPT.match(text) else 0)
            def opened(s):
                if not s.get('open_turns'):
                    s['turn_started']=time.time()  # the turn's first prompt (shell edits are found by mtime from the first writing shell command)
                s['turn']=turn
                s['open_turns']=(list(s.get('open_turns',[]))+[turn])[-20:]
                if mode:s['last_mode']=mode
                if signal:
                    s['signal']=signal
                    s['signal_hash']=shash
                    s['signal_hashes']=shashes
                    # Hashes of the statement's words, under a per-session salt, so Stop can check a note covers it.
                    if not s.get('signal_salt'):
                        import secrets
                        s['signal_salt']=secrets.token_hex(16)
                    s['signal_words']=content_hashes(s['signal_salt'],source,MAX_SIGNAL_WORDS)
                # No words to cover: any receipt settles the approval at Stop. A nonblocking statement's hashes still settle.
                if approved:
                    s['signal']='decision'
                    s.pop('signal_words',None)
                    if not signal:
                        s['signal_hash']=None
                        s['signal_hashes']=[]
                # <private> spans: salted shingle hashes only, so a capture that copies one can be refused.
                if isinstance(text,str) and not MACHINE_PROMPT.match(text) and private.remember(s,text):s['private_turn']=turn
                if transcript:s['transcript']=transcript
                s.pop('resumed',None)
                s.pop('off_told',None)
            state=_update(root,session,opened)
    elif not turn:
        turn=state.get('turn','')
    info['turn']=turn
    result={}
    if event in {'session-start','user-prompt','stop'}:
        try:atomic_write(root/'hooks/last-delivery.json',canonical_json({'harness':harness,'event':event,'timestamp':now(),'bound':bool(domain),'cwd':str(cwd),'domain':domain,'payload_keys':sorted(payload)}),fsync=False)
        except OSError:pass  # Diagnostics only; never cost the session its memory context.
    if event=='session-end':
        synced=_sync_vaults(root,'session end')
        worker=schedule_housekeeping(root)
        detail['housekeeping']={'synced':len(synced.get('results',[])) if isinstance(synced,dict) else None,'worker':worker}
        return {}
    if not domain:
        if event=='stop':detail['reason']='unbound'
        if event=='session-start' and session!='unknown':
            hint=bind_hint(cwd,root,bindings,state)
            detail['bind_hint']=bool(hint)
            if hint:
                _update(root,session,lambda s:s.update(bind_hint=True))
                result['context']=hint
        return result
    if event=='session-start':
        # Harnesses without SessionEnd (Codex, Copilot, Factory, Antigravity) run throttled vault sync and background housekeeping at session start (REL-U5).
        if harness!='claude':
            _sync_vaults(root,'session start',SESSION_START_SYNC_SECONDS)
            detail['housekeeping']=schedule_housekeeping(root)
        from .session_start import build_context,BUDGET,MIN_BUDGET,HARD_CAP,_cap
        # A harness with a SESSION_LIMITS entry gets rules that leave room for the session marker after them.
        limit=SESSION_LIMITS.get(harness)
        marker=f'\nLiving Memory session={session}\n'
        rules=protocol(root,domain,harness,session,room=None if limit is None else limit-len(marker))
        # The fallback protocol stands in for the instruction block, which BUDGET does not count either: without this the briefing got only
        # MIN_BUDGET whenever the fallback was injected (INST-9). HARD_CAP still bounds the whole context.
        rules_outside_budget=0 if installed(harness) else len(rules)
        # Rules that still pass the limit after shrinking (a long explicit CMD path): the marker goes first, so it always arrives.
        prefix=rules+marker if limit is None or len(rules)+len(marker)<=limit else marker.lstrip('\n')+rules+'\n'
        # After a compaction the handoff goes first; the title index gives up the room.
        if source=='compact' and session!='unknown':
            handed=handoff(root,domain,session,state,harness)
            prefix+=handed
            detail['handoff_chars']=len(handed)
        # A session started inside a mapped repo sees a pointer to its map; the body comes when a prompt needs it.
        pointer=session_map(cwd,root,bindings,state,session)
        detail['map']=bool(pointer)
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
                behind=views.behind(root,lambda:_scope(domain,root,bindings),project=views.project_key(cwd))
                if behind:prefix+='\n'.join(line for _,line in behind)+'\n'
                if session!='unknown' and (behind or state.get('views_told')):_update(root,session,lambda s:s.update(views_told=[key for key,_ in behind]))
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.views_hint',exc,root)
        # Automatic maintenance (core/maintenance.py): the latest run's summary or a blocked notice, then a detached worker when one is due.
        if not _subagent(payload):
            try:
                from . import maintenance
                lines,told_run,told_blocked=maintenance.session_lines(root,state.get('maintenance_told'),state.get('maintenance_blocked'))
                if lines:
                    prefix+='\n'.join(lines)+'\n'
                    if session!='unknown':_update(root,session,lambda s:s.update(maintenance_told=told_run,maintenance_blocked=told_blocked))
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.maintenance_lines',exc,root)
        try:
            from . import maintenance
            detail['maintenance']=maintenance.schedule(root,subagent=_subagent(payload),plan=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan',compact=source=='compact')
        except Exception as exc:
            from .logs import swallowed
            swallowed('runtime.maintenance_schedule',exc,root)
        # Diff-aware staleness (core/rebaseline.py): a detached sweep, at most hourly, re-baselines notes a file change did not touch.
        from . import rebaseline
        detail['rebaseline']=rebaseline.schedule(root,bindings,subagent=_subagent(payload),plan=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan',compact=source=='compact')
        # The daily digest refresh (core/digest_refresh.py): off unless the user set digest_refresh with a budget.
        from . import digest_refresh
        detail['digest_refresh']=digest_refresh.schedule(root,bindings,subagent=_subagent(payload),plan=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan',compact=source=='compact')
        # Prompts and tool hints read the index without checking it: bring a stale one (older schema, notes edited outside the scripts) up to date now.
        try:
            from .search import refresh_stale
            detail['reindexed']=refresh_stale(domain,root)
        except Exception as exc:
            from .logs import swallowed
            swallowed('runtime.refresh_stale',exc,root)
        from .briefs import brief_folder
        resolved=[] if _subagent(payload) else auto_resolve(root,domain,bindings,session)
        detail['auto_resolved']=len(resolved)
        # A harness that cuts the hook's whole context (Codex: additionalContextLimit) gets a Rules block that ends inside that limit.
        text,shown,indexed=build_context(cwd,root,max(MIN_BUDGET,BUDGET-(len(prefix)-rules_outside_budget)),session=session,trace=detail,resolved=resolved,
                                         limit=None if limit is None else max(0,limit-len(prefix)),
                                         currency=lambda hit:note_currency(hit,brief_folder(hit,cwd,bindings),root,bindings,state,session,'fresh_brief'))
        detail.update(shown=list(shown),indexed=indexed)
        if session!='unknown':
            # A new or compacted context has seen only what is injected now. Guidance and gotchas carry a snippet
            # there (shown_snippet, never `shown`): a prompt that finds one relevant injects it in full, and only that
            # full injection is deduplicated. Index lines are titles, so those notes stay eligible too. Once the index
            # has listed the newest titles, the prompt-time "recent titles" fallback has nothing to add.
            def started(s):
                s.pop('shown',None)
                s.pop('shown_snippet',None)
                s.pop('overview_shown',None)
                s.pop('delivered',None)
                s.pop('topic',None)
                if shown:s['shown_snippet']=shown
                # The new context has not seen any map hint either; exploration counts and reminders stand.
                for rec in _repos(s).values():
                    if isinstance(rec,dict) and rec.get('map'):rec.pop('done',None)
                if indexed:s['overview_shown']=True
            _update(root,session,started)
            if shown:
                from .usage import record
                record(root,'inject',list(shown),session,'session')
        # BUDGET is the target besides the rules; the fallback protocol and a compaction handoff can push the whole
        # context past it, but never past HARD_CAP.
        result['context']=_cap(prefix+(text or ''),HARD_CAP)
    elif event=='user-prompt':
        prompt=payload.get('prompt') or payload.get('user_prompt') or payload.get('userPrompt') or payload.get('transformedPrompt') or ''
        from . import injection
        header=f'Living Memory session={session} turn={turn}'
        # The first prompt after `alm on` repeats the session-start protocol: a compaction while off may have dropped it.
        resume=BACK_ON+'\n'+protocol(root,domain,harness,session)+f'\nLiving Memory session={session}\n' if resumed else ''
        # Governance lines are never cut, so they are known before the brief and maps are sized.
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
                behind=[(key,line) for key,line in views.behind(root,lambda:_scope(domain,root,bindings),hint_command(root,state,domain),views.project_key(cwd)) if key not in told]
                governance+=[line for _,line in behind]
                if behind and session!='unknown':_update(root,session,lambda s:s.update(views_told=(list(s.get('views_told') or [])+[key for key,_ in behind])[-50:]))
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.views_hint',exc,root)
        governance_len=sum(len(g)+1 for g in governance)
        # Everything is bounded before anything is recorded as delivered. The resume protocol comes first (it restores the
        # rules and CMD) and is cut to whole lines only when it could not fit even with no brief or maps.
        if resume:
            from .session_start import _cap
            resume=_cap(resume,max(0,PROMPT_HARD_CAP-len(header)-governance_len-HIT_RESERVE))
            if resume and not resume.endswith('\n'):resume+='\n'
        # The brief and maps get what PROMPT_HARD_CAP leaves after the header, resume prefix, governance and HIT_RESERVE; when
        # they would not fit, they are rebuilt at the next ORIENTATION_LEVEL (fewer summaries, then pointers for maps).
        orientation_room=PROMPT_HARD_CAP-len(header)-len(resume)-governance_len-HIT_RESERVE-1
        delivered={}
        maps_text,map_refs,mapped=None,{},{}
        brief_text,brief_refs=None,{}
        briefed=[]
        if isinstance(prompt,str) and not MACHINE_PROMPT.match(prompt):
            for level in range(ORIENTATION_LEVELS):
                maps_text,map_refs,mapped=None,{},{}
                brief_text,brief_refs=None,{}
                briefed=[]
                try:maps_text,map_refs,mapped=prompt_maps(cwd,prompt,root,bindings,state,detail,briefed,session,level)
                except Exception as exc:
                    from .logs import swallowed
                    swallowed('runtime.prompt_maps',exc,root)
                # The brief's room at level 4 is what the maps (already pointers) leave.
                left=orientation_room-(len(maps_text)+2 if maps_text else 0)-len(ORIENTATION_PREAMBLE)-1
                try:brief_text,brief_refs=prompt_briefs(cwd,root,bindings,state,detail,briefed,session,min(level,4),max(0,left))
                except Exception as exc:
                    from .logs import swallowed
                    swallowed('runtime.prompt_briefs',exc,root)
                if level==ORIENTATION_LEVELS-1:
                    # Nothing fits: no brief or maps this prompt. The level-4 trace stands, with the brief moved to pointer (not delivered).
                    if brief_refs and isinstance(detail.get('brief'),dict):detail['brief'].update(injected=[],pointer=brief_refs)
                    maps_text,map_refs,brief_text,brief_refs,briefed=None,{},None,{},[]
                    mapped={k:v for k,v in mapped.items() if v}   # an unmapped repo's notice did not go out, so it is not "told"
                    break
                # The project brief leads the orientation: what the project is, then how its repos are organized.
                if brief_text:maps_text=ORIENTATION_PREAMBLE+'\n'+brief_text+('\n\n'+maps_text.split('\n',1)[1] if maps_text else '')
                if not maps_text or len(maps_text)<=orientation_room:break
            if level:detail['orientation_level']=level
        # A map or brief injected now counts as shown, so the note search below does not repeat it as a hit.
        shown_now=state.get('shown',{})
        shown_now=dict(shown_now) if isinstance(shown_now,dict) else dict.fromkeys(shown_now if isinstance(shown_now,list) else [],'')
        shown_now.update(map_refs)
        shown_now.update(brief_refs)
        # A map or brief given only as a pointer is kept out of the hits too, but not recorded: its body comes later.
        deferred={**((detail.get('maps') or {}).get('pointers') or {}),**((detail.get('brief') or {}).get('pointer') or {})}
        shown_now.update(deferred)
        units_out=[]
        topic=state.get('topic') if isinstance(state.get('topic'),list) else None
        session_delivered=state.get('delivered') if isinstance(state.get('delivered'),dict) else {}
        # One budget for note hits (injection.PROMPT_CAP): they get what the header, brief, maps and governance lines leave,
        # and source excerpts and recall join only when they still fit.
        fixed=len(header)+(len(maps_text)+1 if maps_text else 0)+governance_len
        room=max(0,min(injection.PROMPT_CAP,PROMPT_HARD_CAP-len(resume))-fixed-1) if DELIVERY else None
        if isinstance(prompt,str):hits,ids,overview=context(cwd,prompt,root,domain,shown_now,state.get('overview_shown',False),delivered,detail,session_delivered,topic,units_out,room)
        else:
            hits,ids,overview=None,[],False
            detail['path']='not_text'
        # A recall prompt ("remind me") bypasses shown_now; a pointer-only map or brief it returned as a hit is still not its delivery.
        for ref in deferred:delivered.pop(ref,None)
        relevant_notes=len(ids)+len(detail.get('suppressed_shown') or [])
        delivered.update(map_refs)
        delivered.update(brief_refs)
        ids=list(map_refs)+list(brief_refs)+ids
        set_topic='topic' in detail and not detail.get('follow_up')
        if session!='unknown' and (mapped or ids or overview or set_topic):
            def remember_prompt(s):
                if mapped:
                    repos=_repos(s)
                    for key,ref in mapped.items():
                        rec=dict(repos.pop(key,{}))
                        if ref:rec['map']=ref
                        else:rec['told']=True
                        repos[key]=rec
                    s['repos']=dict(list(repos.items())[-MAX_REPOS:])
                if ids or overview:
                    previous=s.get('shown',{})
                    previous=dict(previous) if isinstance(previous,dict) else dict.fromkeys(previous if isinstance(previous,list) else [],'')
                    for ref,key in delivered.items():
                        previous.pop(ref,None)
                        previous[ref]=key
                    s['shown']=dict(list(previous.items())[-50:])
                    # Unlike shown, surfaced survives session start, so the compaction handoff can name these notes.
                    s['surfaced']=list(dict.fromkeys(list(s.get('surfaced',[]))+ids))[-MAX_SURFACED:]
                    if overview:s['overview_shown']=True
                    if units_out:
                        from .injection import remember as delivered_unit
                        rec_d=s.get('delivered') if isinstance(s.get('delivered'),dict) else {}
                        for row in units_out:delivered_unit(rec_d,row['hit'],row['unit'],row['level'])
                        s['delivered']=dict(list(rec_d.items())[-MAX_DELIVERED:])
                if set_topic:
                    # A short follow-up ("why?") refers to what this prompt found: refs only, never prompt text. A searched prompt
                    # that found nothing clears it, so "why?" after an unrelated question does not answer the old topic.
                    s['topic']=detail['topic']
            try:_update(root,session,remember_prompt)
            except (OSError,TimeoutError):pass
        usage_batch=[]
        if briefed:
            detail['briefed']=list(briefed)
            usage_batch.append(('inject',briefed,'map-brief',None))
        if ids:
            if map_refs:usage_batch.append(('inject',list(map_refs),'prompt-map',None))
            if brief_refs:usage_batch.append(('inject',list(brief_refs),'prompt-brief',None))
            if ids[len(map_refs)+len(brief_refs):]:usage_batch.append(('inject',ids[len(map_refs)+len(brief_refs):],'prompt',
                                                                      {f"{r['hit']['domain']}:{r['hit']['id']}":r['level'] for r in units_out}))
        # Candidates the gate kept but the cap cut: surfaced, not injected (refs only; never read as "unused").
        if detail.get('cut_by_cap'):usage_batch.append(('surface',list(detail['cut_by_cap'])[:3],'prompt',None))
        if usage_batch:
            from .usage import record_many
            record_many(root,usage_batch,session)
        result['context']=resume+header+('\n'+maps_text if maps_text else '')+('\n'+hits if hits else '')
        if resumed:detail['resumed']=True
        fits=lambda text:not DELIVERY or len(result['context'])+len(text)+1+sum(len(g)+1 for g in governance)<=injection.PROMPT_CAP
        if isinstance(prompt,str) and not MACHINE_PROMPT.match(prompt) and relevant_notes<2 and not _subagent(payload):
            try:
                excerpts,keys=prompt_sources(prompt,root,domain,state,detail.setdefault('sources',{}))
                if excerpts and not fits(excerpts):
                    detail['sources_cut_by_budget']=len(keys)
                    excerpts=None
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
            gate={}
            recalled,used=auto(state.get('transcript'),prompt,state.get('recalled',[]),gate)
            detail['recall']=dict(gate,used=len(used))
            recalled=(f"From before compaction (this session's transcript; untrusted, may be outdated; more: "
                      f"{hint_command(root,state,domain)} search 'query' --transcript --session {session}):\n"+recalled) if recalled else None
            if recalled and fits(recalled):result['context']+='\n'+recalled
            elif recalled:  # not delivered, so still eligible next time
                detail['recall_cut_by_budget']=True
                used=[]
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
        if session=='unknown' or _subagent(payload):
            detail['skipped']='unknown_session' if session=='unknown' else 'subagent'
            return {}
        trigger=str(payload.get('trigger') or 'auto')
        detail['trigger']=trigger
        def compacting(s):
            s['compactions']=(list(s.get('compactions',[]))+[{'at':now(),'trigger':trigger}])[-10:]
            if transcript:s['transcript']=transcript
        state=_update(root,session,compacting)
        if harness in STEERED:result['instructions']=steering(root,state,domain,bindings)
        detail['steered']=bool(result.get('instructions'))
    elif event=='subagent-start':
        # A subagent's context has no CMD, so this is the whole command; PowerShell needs the call operator before a quoted path.
        from .note_types import NOTE_TYPES
        cmd=command(root,domain)
        call='; Bash: as written, PowerShell: put & in front' if os.name=='nt' and not cmd.startswith('alm ') else ''
        # Its one write is a draft for the main agent (core/proposals.py), so the evidence it found survives the hand-back
        # while notes still come only from the main agent.
        result['context']=('Living Memory: subagents may only search/read via scripts, plus one staging write. To keep a verified finding a future '
                           f"session needs (a cause with its evidence, an exact command, a constraint), run {cmd} propose"+(f' --session {session}' if session!='unknown' else '')+
                           f" --type TYPE ({', '.join(NOTE_TYPES)}) --title 'Title' --summary 'The fact' [--section 'Heading' 'Text'] [--detail 'Fact']"+call+
                           '. Include file:line evidence and exact commands; skip progress and guesses. Name each returned proposal ID in your report; '
                           'the main agent saves or rejects it. Do not capture, update or confirm notes.')
        # Maps of the repos this session works in, so a subagent starts from them instead of exploring from scratch.
        maps=list(dict.fromkeys(r['map'] for r in _repos(state).values() if isinstance(r,dict) and r.get('map')))[-3:]
        named=_titled(root,maps) if maps else []
        detail['maps']=maps
        if named:result['context']+=(f"\nCodebase maps for the repos this session is working in (read the one for your repo before exploring it: "
                                     f"{cmd} read REF"+call+"): "+'; '.join(named))
    elif event=='pre-tool-use':
        name,args,text,grammar=tool_info
        detail['tool']=name
        if _subagent(payload):detail['subagent']=True   # who ran a command (for evaluations that count subagent propose calls)
        subcommand=memory_subcommand(text,grammar) if text else None
        detail['subcommand']=subcommand
        # The call operator is PowerShell's: Bash would background the command. Say so, with the command that works.
        if subcommand and grammar=='posix' and re.match(r'\s*&',text):
            detail['decision']='deny:call-operator'
            result['deny']='In Bash, drop the leading &: '+re.sub(r'^\s*&\s*','',text)
            return result
        # A remote source sync reaches the network (the only command that does): the user approves it, it is never auto-allowed.
        networked=subcommand=='source-sync' and remote_sync(text,grammar,root)
        if networked:detail['network']=True
        if subcommand in ALLOWED_SUBCOMMANDS and not networked and not (subcommand=='taxonomy' and taxonomy_write(text,grammar)) and not (subcommand=='doc' and doc_write(text,grammar)) and not (subcommand=='proposals' and page_settle(text,grammar)):   # `taxonomy add`, `doc` writes and settling a page edit keep the normal prompt
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
            detail['command_hints']=[]
            hint=command_hints(text,state,root,domain,bindings,session,detail['command_hints'])
            if hint:result['context']='\n'.join(filter(None,[result.get('context'),hint]))
        if not skip and name not in SHELL_TOOLS:
            try:created=created_top_level(args,cwd,root,bindings)
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.created_top_level',exc,root)
                created={}
            if created:
                detail['created_top_level']={Path(k).name:names for k,(_,names) in created.items()}
                def grew(repos):
                    for key,(ref,names) in created.items():
                        rec=dict(repos.pop(key,{}))
                        rec['map']=ref
                        rec['created']=list(dict.fromkeys(list(rec.get('created',[]))+names))[:MAX_CREATED]
                        repos[key]=rec
                _track_repos(root,session,grew)
        if not skip:
            paths=touched_paths(args,payload,cwd,root,domain,bindings,shell=name in SHELL_TOOLS)
            detail['touched']=len(paths)
            detail['hints']=[]
            follow={}
            digested=[]
            hints,looked=file_hints(paths,state,root,domain,bindings,session,detail['hints'],follow,digested) if harness in FILE_HINT_HARNESSES else (None,[])
            # Files past the per-call hint cap were never looked up; Stop looks them up in one batch (follow_pending).
            unlooked=[p for p in paths if p not in looked and p not in set(state.get('file_hints',[]))] if harness in FILE_HINT_HARNESSES else []
            if hints:result['context']='\n'.join(filter(None,[result.get('context'),hints]))
            # Decision documents (ADRs, changelogs, boards) changed this turn; Stop asks for a note about them.
            from .triggers import decision_docs
            docs=decision_docs(paths,bindings)
            detail['docs']=len(docs)
            # The repos this command may change, with HEAD now: Stop asks git about these only (PROV-2).
            repos=shell_repos(text,cwd,bindings,state) if name in SHELL_TOOLS else []
            new_repos=[k for k in repos if k not in (state.get('shell_repos') or [])]
            new_heads={k:h for k in new_repos if (h:=head_oid(Path(k)))}
            if not state.get('mutating') or set(paths)-set(state.get('touched',[])) or looked or (name in SHELL_TOOLS and not state.get('shell')) or new_repos:
                def touch(s):
                    s['mutating']=True  # edited outlives the turn: read-only sessions get no housekeeping reminders
                    s['edited']=True
                    if name in SHELL_TOOLS:   # shell edits often use relative paths no hook can see; Stop asks git
                        if not s.get('shell'):s['shell_started']=time.time()
                        s['shell']=True
                        known=list(s.get('shell_repos') or [])
                        heads=s.get('heads') if isinstance(s.get('heads'),dict) else {}
                        for key in repos:
                            if key not in known and key in new_heads:heads[key]=new_heads[key]
                        s['heads']=heads
                        s['shell_repos']=list(dict.fromkeys(known+repos))[:MAX_SHELL_REPOS]
                    if docs:s['docs']=list(dict.fromkeys(list(s.get('docs',[]))+docs))[:MAX_DOCS]
                    if follow:
                        hinted={p:r for p,r in (s.get('hinted') or {}).items() if p not in follow}
                        hinted.update(follow)
                        s['hinted']=dict(list(hinted.items())[-MAX_HINTED:])
                        if digested:s['hinted_digests']=list(dict.fromkeys(list(s.get('hinted_digests',[]))+digested))[-MAX_HINTED:]
                    s['touched']=list(dict.fromkeys(list(s.get('touched',[]))+paths))[:MAX_TOUCHED]
                    if unlooked:s['unlooked']=list(dict.fromkeys(list(s.get('unlooked',[]))+unlooked))[:MAX_TOUCHED]
                    s['touched_session']=list(dict.fromkeys(list(s.get('touched_session',[]))+paths))[-MAX_TOUCHED_SESSION:]
                    if looked:s['file_hints']=list(dict.fromkeys(list(s.get('file_hints',[]))+looked))[-MAX_FILE_HINTS:]
                _update(root,session,touch)
    elif event=='stop':
        if session=='unknown':
            detail['reason']='unknown_session'
            return {}
        _record_last_session(root,domain,session,state)
        turns=list(dict.fromkeys(list(state.get('open_turns',[]))+([turn] if turn else [])))
        _record_ledger(root,domain,session,state,cwd,turns)
        # A plan approved inside this turn (ExitPlanMode) is raised at this Stop, so the next prompt is not a second exit from plan mode.
        stop_mode=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()
        if stop_mode and state.get('last_mode') and stop_mode!=state['last_mode']:_update(root,session,lambda s:s.update(last_mode=stop_mode))
        approvals=[]   # every approval in the transcript turn; a receipt settles them all
        def settle(s,keys):
            if keys:s['decisions_settled']=list(dict.fromkeys(list(s.get('decisions_settled') or [])+keys))[-MAX_SETTLED:]
        def close(s):
            for key in TURN_STATE:s.pop(key,None)
            if left or s.get('docs_left'):s['docs_left']=left
            if receipted:settle(s,approvals)
        signal=state.get('signal')
        detail.update(turns=len(turns),mutating=bool(state.get('mutating')),signal=signal)
        # The turn ends here whatever happens next, so its statement is settled: a header repeated in every brief raises once.
        settle_signals(root,domain,state.get('signal_hashes') or [state.get('signal_hash')])
        # Plan mode cannot save anything, so a repo explored there is reminded about at the next Stop outside plan mode.
        planning=str(payload.get('permission_mode') or payload.get('permissionMode') or '').lower()=='plan'
        try:pending=stop_pending(root,bindings,state,session,turns,domain,cwd,planning,harness,approvals)
        except Exception as exc:
            from .logs import swallowed
            swallowed('runtime.stop_pending',exc,root)
            pending=[]
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
        receipted=any(capture_status(session,t,root) for t in turns)
        quiet=('receipt' if not pending and receipted else 'no_change' if not (state.get('mutating') or signal or pending)
               else 'no_turns' if not turns else None if pending else 'nudged' if signal in SIGNAL_TEXT else 'files_only')
        if quiet:
            detail['reason']=quiet
            _update(root,session,close)
            return {}
        latest=turns[-1]
        reminder=root/'.turns'/(turn_key(session,latest)+'.reminded')
        if payload.get('stop_hook_active') or payload.get('stopHookActive') or reminder.exists():
            detail['reason']='stop_active' if payload.get('stop_hook_active') or payload.get('stopHookActive') else 'already_reminded'
            _update(root,session,close)
            return {}
        atomic_write(reminder,'reminded\n')
        used=[]
        text=stop_text(pending,session,used)
        def reminded(s):
            # The reminder settles these turns: a later turn is not reminded again about this one's statement
            # or changes, even when the harness sends no stop_hook_active follow-up. The turn's touched files
            # stay for a capture that answers the reminder, and `reminded` lets that capture count as a signal capture.
            for key in TURN_STATE:
                if key not in ('touched','unlooked'):s.pop(key,None)
            s['reminded']={'turn':latest,'signal':signal}
            # An approval is reminded about once a session (HOOK-5): only one the reminder text named, or all on a receipt.
            settle(s,approvals if receipted else [d['key'] for k,d in used if k=='decision' and d.get('key')])
            # Each document and each note is asked about once per session.
            asked=[d['path'] for k,d in used if k=='doc']
            s['docs_reminded']=list(dict.fromkeys(list(s.get('docs_reminded',[]))+asked))[-MAX_DOCS_REMINDED:]
            s['docs_left']=[p for p in left if p not in asked]
            s['briefed']=list(dict.fromkeys(list(s.get('briefed',[]))+[k for k,d in used if k in BRIEF_KINDS]))
            s['followed']=list(dict.fromkeys(list(s.get('followed',[]))+[d['ref'] for k,d in used if k=='follow']+[n['ref'] for k,d in used if k=='doc' for n in d['notes']]))[-MAX_HINTED:]
            s['stale_told']=list(dict.fromkeys(list(s.get('stale_told',[]))+[d[1]['ref'] for k,d in used if k=='stale']))[-MAX_HINTED:]
            s['proposals_told']=list(dict.fromkeys(list(s.get('proposals_told',[]))+[r['id'] for k,d in used if k=='proposals' for r in d]))[-MAX_HINTED:]
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
    pending=[]
    stale=[]
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
    told=set(state.get('stale_told',[]))
    touched=state.get('touched') or []
    found=[]
    prefixes=domain_prefixes(domain,bindings) if domain else []
    def inside(repo,path):
        q=Path(path)
        return any(c.is_relative_to(repo) for c in ([q] if q.is_absolute() else [b/path for b in prefixes]))
    for key,slot,entry in stale:
        if entry['ref'] in told:continue
        repo=Path(key)
        here=git_root(cwd) if cwd else None
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
                "--detail '**Layout:** ...' (and Entry points, Key modules, Data flow, Build/test/run, Conventions), plus --section 'Data flow' with a small Mermaid flowchart if it helps.")
    if kind=='stale':
        paths=data['paths']
        names=', '.join(paths[:2])+(f' and {len(paths)-2} more' if len(paths)>2 else '')
        short=data['short']
        return (f"{data['what']} {short} is stale ({names} changed since it was written): CMD read {short}, then CMD update {short} --session {session} "
                f"--set-detail '{'**Conventions:**' if data['what']=='map' else '**Key decisions:**'} ...' --expected-revision REV for each bullet that changed.")
    ref,names=data
    short=short_ref(*ref.split(':',1))
    return (f"new top-level paths in {name} ({', '.join(names)}): if they change its layout, update map {short} "
            f"(CMD read {short}, then CMD update {short} --session {session} --set-detail '**Layout:** ...' --expected-revision REV for each bullet that changed).")

# Per-turn state a Stop clears (reminded keeps `touched` for a capture that answers it).
TURN_STATE=('open_turns','mutating','signal','signal_hash','signal_hashes','signal_words','touched','unlooked','docs','shell','shell_started','shell_repos','turn_started','heads')
MAP_KINDS=('unmapped','grown','stale')
BRIEF_KINDS=('unbriefed','brief')
# Kinds that name their own reminder in the usage diary; the map kinds report as 'explored' or 'grown'.
STOP_KINDS=frozenset(BLOCKING_SIGNALS|{'doc','follow','stale','fix','proposals'}|set(BRIEF_KINDS))
STOP_TEXT_CAP=700
STOP_DOCS=2
STOP_NOTES=2
STOP_FOLLOW=5   # notes about edited files one Stop reminder names, in a single sentence
STOP_PROPOSALS=3   # subagent proposals one Stop reminder names
CHANGE_GUARD_COMMITS=3  # more than this changed under a shell command is a pull or checkout, not the agent's work
CHANGE_GUARD_FILES=30
FOLLOW_BATCH_FILES=30
BOILERPLATE_HEADINGS={'context','decision','decisions','consequences','status','background','summary','overview','alternatives','options','notes','rationale','changelog','unreleased'}

def stop_pending(root,bindings,state,session,turns,domain=None,cwd=None,planning=False,harness=None,approvals=None):
    """[(kind, data)] the Stop reminder may raise, most important first. Kinds: remember, decision, directive (the
    user's own statement); doc (a decision document changed this turn); follow (a file a note is about was edited);
    unbriefed, brief (the project has no brief, or a decision this turn may have outdated it); unmapped, grown (a
    repo explored without a map, or grown at its top level). approvals, when given, collects the key of every approval
    in the transcript turn (approved_decisions), for the caller to settle."""
    pending=[]
    signal=state.get('signal')
    # A receipt settles the housekeeping, so only the brief (which a captured decision may have outdated) is still asked
    # about. A blocking statement, a decision document or a file a note covers is settled only by a note that covers it
    # (or by "nothing durable"), so an unrelated capture does not silence it. A statement that stored no words
    # (older state, or nothing but stopwords) is settled by any receipt.
    receipts=[r for r in (capture_status(session,t,root) for t in turns) if r]
    receipted=bool(receipts)
    dismissed=any(r.get('outcome')=='nothing_durable' or r.get('dismissed') for r in receipts)
    saved=[ref for r in receipts for ref in r.get('notes',[])]
    if signal in BLOCKING_SIGNALS and not (dismissed or (covers_signal(root,saved,state) if state.get('signal_words') else receipted)):pending.append((signal,{'files':bool(state.get('mutating'))}))
    # What the turn's tool calls and their results show (core/recall.turn_tools): an approved plan or an answered
    # question is a decision (UX-C4); a command that failed, then passed after an edit, may carry a root cause (UX-C3).
    # The transcript turn runs back to the last real prompt, so hook turns that a task notification started see the same
    # approvals again (HOOK-5): one already reminded about or answered by a receipt is settled for the session.
    if domain and not planning and state.get('transcript') and (harness or state.get('harness')) in RECALL_HARNESSES:
        try:
            from .recall import turn_tools
            tools=turn_tools(state['transcript'])
            if signal!='decision':
                found=approved_decisions(tools)
                settled=set(state.get('decisions_settled') or [])
                if approvals is not None:approvals.extend(key for _,key in found)
                chosen=next(((label,key) for label,key in found if key not in settled),None)
                if chosen and not receipted:pending.append(('decision',{'files':bool(state.get('mutating')),'plan':chosen[0],'key':chosen[1]}))
            key=verified_fix(tools) if state.get('mutating') and not receipted else None
            if key:pending.append(('fix',{'command':key}))
        except Exception as exc:
            from .logs import swallowed
            swallowed('runtime.turn_tools',exc,root)
    if (state.get('mutating') or state.get('docs_left')) and domain and not dismissed:
        memo=[]
        def changed():   # the shell-changed files, asked of git once for both reminders
            if not memo:memo.append(turn_changed(root,bindings,state,domain,cwd,session))
            return memo[0]
        for name,find in (('doc',lambda:doc_pending(root,bindings,state,domain,cwd,changed)),('follow',lambda:follow_pending(root,state,bindings,domain,changed) if state.get('mutating') else [])):
            try:found=[(kind,data) for kind,data in find() if not (kind=='doc' and saved and covered_by(root,saved,data['path']))]
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.'+name+'_pending',exc,root)
                continue
            # A note a document reminder already offers to update is not asked about a second time.
            named={n['ref'] for kind,data in pending if kind=='doc' for n in data['notes']}
            pending+=[(kind,data) for kind,data in found if not (kind=='follow' and data['ref'] in named)]
    # Drafts a subagent staged this session (core/proposals.py): each is asked about once, receipt or not, because the
    # turn's own receipt says nothing about them. Plan mode cannot save, so they wait for the next Stop outside it.
    if not planning:
        try:
            from .proposals import rows
            told=set(state.get('proposals_told') or [])
            fresh=[r for r in rows(root,'pending',session) if r.get('id') not in told]
            if fresh:pending.append(('proposals',fresh[:STOP_PROPOSALS]))
        except Exception as exc:
            from .logs import swallowed
            swallowed('runtime.proposals_pending',exc,root)
    if domain and cwd and not planning:
        try:pending+=brief_pending(root,bindings,state,session,turns,domain,cwd,pending,receipted)
        except Exception as exc:
            from .logs import swallowed
            swallowed('runtime.brief_pending',exc,root)
    # Housekeeping (maps, a brief) is for sessions that change something: a read-only Q&A session is not asked.
    if not planning and not receipted and state.get('edited'):pending+=[(kind,(key,data)) for key,kind,data in explore_pending(root,bindings,state,domain,cwd)]
    return pending

EDIT_TOOLS={'edit','write','multiedit','notebookedit','apply_patch'}
MEMORY_COMMANDS={'alm','living_memory.py'}
# Positive evidence that a shell command wrote a file (a guess either way, so only what plainly writes counts): in-place
# editors, a redirect to a file, file moves and package installs. A diagnostic or test run that merely is not provably
# read-only is no edit.
SHELL_WRITE=re.compile(r"\b(?:sed|perl)\s+(?:-\w+\s+)*(?:-\w*i|--in-place)|(?<![\d&])>{1,2}\s*(?!&|/dev/null|\$null|nul\b)[\w./\\~\"'$-]"
                       r"|\b(?:tee|mv|cp|rm|touch|patch|truncate)\s|\bgit\s+(?:apply|am|checkout\s+--|restore|stash|cherry-pick|revert|merge|rebase|reset)\b"
                       r"|\b(?:npm|pnpm|yarn|pip|uv|cargo|go)\s+(?:install|add|i|get|sync)\b"
                       r"|\b(?:set-content|add-content|out-file|new-item|remove-item|move-item|copy-item|rename-item)\b",re.I)
# Commands whose nonzero exit means "not found" or "differs", not a failure: grep finding nothing, then finding it after an edit, is no fix.
PROBE_COMMANDS={'grep','egrep','fgrep','rg','findstr','select-string','sls','diff','cmp','test','[','which','where','command','get-command','test-path'}
LEADING_CD=re.compile(r'^cd\s+(?:"[^"]*"|\'[^\']*\'|[^\s;&]+)\s*(?:&&|;)\s*')
ENV_ASSIGN=re.compile(r'^[A-Za-z_]\w*=\S*\s+')

def command_key(text):
    """A shell command's identity for "the same command": whitespace collapsed, leading `cd X &&` or `cd X;` and env
    assignments dropped, then the first two words that are not options (`py -3.15 -m unittest -v` is `py unittest`)."""
    text=' '.join(str(text).split())
    while True:
        short=ENV_ASSIGN.sub('',LEADING_CD.sub('',text,1),1)
        if short==text:break
        text=short
    return ' '.join([w for w in text.split() if not w.startswith('-')][:2])

def verified_fix(tools):
    """The command key of a shell command that failed, then (after an edit) passed, in one turn's tool calls; else None.
    An edit is an edit tool that did not fail, or a successful shell command that plainly writes (SHELL_WRITE: `sed -i`, a
    redirect to a file, `npm install`) and is not one of the failed commands. Memory commands and results with no exit code are ignored."""
    from .recall import _command_text
    failed=set()
    ready=set()
    for tool in tools:
        name=str(tool.get('name','')).lower()
        if name in EDIT_TOOLS:
            if tool.get('ok') is not False:ready|=failed
            continue
        if name not in SHELL_TOOLS or tool.get('ok') is None:continue
        args=tool.get('input') or {}
        text=_command_text(args.get('command',args.get('cmd')))
        key=command_key(text)
        if not key or key.split()[0] in MEMORY_COMMANDS or key.split()[0].lower() in PROBE_COMMANDS or 'living_memory.py' in text:continue
        if not tool['ok']:
            failed.add(key)
            ready.discard(key)
        elif key in ready:return key
        elif key not in failed and SHELL_WRITE.search(text) and not readonly_shell(text):ready|=failed
    return None

def approved_decisions(tools):
    """[(label, key)] for what the user approved in this turn, in order. label: the first heading of an ExitPlanMode plan the
    user accepted, or the question and answer of an AskUserQuestion. key: a hash of the call id, the whole call and its
    result, so two plans that share a heading, or the same question asked again later, are different approvals (HOOK-5).
    A rejected plan is an error result and arms nothing."""
    found=[]
    for tool in tools:
        name=str(tool.get('name','')).lower()
        args=tool.get('input') or {}
        if tool.get('ok') is not True:continue
        if name=='exitplanmode':
            plan=str(args.get('plan') or '')
            label=next((l.lstrip('# ').strip() for l in plan.splitlines() if l.lstrip().startswith('#')),'')[:120] or 'approved plan'
        elif name=='askuserquestion':
            asked=args.get('questions')
            first=asked[0] if isinstance(asked,list) and asked and isinstance(asked[0],dict) else {}
            label=(str(first.get('question') or 'question')[:100]+' -> '+' '.join(str(tool.get('text') or '').split())[:100]).strip()
        else:continue
        whole=json.dumps([str(tool.get('id') or ''),name,args,str(tool.get('text') or '')],sort_keys=True,default=str)
        found.append((label,hashlib.sha256(whole.encode('utf-8')).hexdigest()[:16]))
    return found

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
        try:
            domain,note_id=ref.split(':',1)
            note=find_note(note_id,domain,root)[1]
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
    asked=set(state.get('briefed',[]))
    signal=state.get('signal')
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
        folder=project_dir(cwd,bindings)
        return [('unbriefed',{'dir':project_key(folder,bindings),'name':repo_name(folder)})]
    return []

def _doc_file(shown,prefixes):
    """The file a path shown to notes names, else None."""
    for base in prefixes:
        if (base/shown).is_file():return base/shown
    p=Path(shown)
    return p if p.is_absolute() and p.is_file() else None

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
        try:hits+=search_domains(query,domains,2,root,keep=relevance_gate(query,root),ensure_fresh=False)
        except (OSError,ValueError):pass
    from .session_start import _title
    found={}
    for h in hits:found.setdefault(f"{h['domain']}:{h['id']}",{'ref':f"{h['domain']}:{h['id']}",'short':short_ref(h['domain'],h['id']),'title':_title(h['title'])})
    return list(found.values())[:STOP_NOTES]

MAX_SHELL_REPOS=3
OTHER_SESSION_HOURS=12

def shell_repos(text,cwd,bindings,state):
    """The repos a writing shell command may change, as resolved posix paths: the one holding cwd and those the command
    text names. When it names none and cwd is in no repo, the repo the session explored last (the user's choice, PROV-2):
    every explored repo would pick up a parallel session's edits."""
    from .maps import named_repos,candidate_repos
    here=git_root(cwd) if cwd else None
    try:named=named_repos(text,candidate_repos(cwd,bindings),bindings.get('repo_aliases')) if text else []
    except (OSError,ValueError):named=[]
    found=[p.as_posix() for p in ([here] if here else [])+list(named)]
    if not found and _repos(state):found=[list(_repos(state))[-1]]
    return list(dict.fromkeys(found))

def _turn_repos(cwd,state):
    """The repos a turn's shell edits may have changed: those its writing shell commands named (shell_repos); for a state
    written before shell_repos existed, the one holding cwd. At most MAX_SHELL_REPOS."""
    if 'shell_repos' in state:return [Path(k) for k in state.get('shell_repos') or []][:MAX_SHELL_REPOS]
    here=git_root(cwd) if cwd else None
    return [here] if here else []

def others_touched(root,session):
    """Files other sessions active in the last OTHER_SESSION_HOURS hours named in write tool calls (their touched_session),
    as notes name them. A parallel session's edits show up in this session's git status; they are not this session's work."""
    mine=_pointer(root,session) if session else None
    cutoff=time.time()-OTHER_SESSION_HOURS*3600
    found=set()
    try:pointers=list((Path(root)/'.turns').glob('*.current.json'))
    except OSError:return found
    for pointer in pointers:
        try:
            if pointer==mine or pointer.stat().st_mtime<cutoff:continue
        except OSError:continue
        other=read_json(pointer,{})
        if isinstance(other,dict):found.update(p for p in other.get('touched_session') or [] if isinstance(p,str))
    return found

def turn_changed(root,bindings,state,domain,cwd,session=None):
    """({shown name: absolute path}, pulled, edited) for files a shell command changed this turn, which no hook can name (relative paths): the files
    git reports changed since the turn's first writing shell command, or committed since HEAD was recorded when that command first named the repo,
    inside the domain's bound folders and the repos the turn's shell commands named, less the files other sessions edited (others_touched).
    pulled is True when HEAD moved by more than CHANGE_GUARD_COMMITS commits or more than CHANGE_GUARD_FILES files changed: a pull or checkout, not the agent's work.
    edited is the set of shown names of existing files the moved HEAD modified (empty unless pulled): what doc_pending leaves out after a pull."""
    from .common import domain_prefixes,display_path,head_oid
    from .triggers import changed_since,committed_since,commit_count
    prefixes=domain_prefixes(domain,bindings)
    files={}
    pulled=False
    total=0
    moved=[]
    def show(path):
        try:p=path.resolve()
        except OSError:return None,None
        if p.is_relative_to(root) or not any(p.is_relative_to(x) for x in prefixes):return None,None
        return display_path(p,prefixes),p
    if state.get('shell') and cwd:
        heads=state.get('heads') if isinstance(state.get('heads'),dict) else {}
        since=state.get('shell_started') or state.get('turn_started')
        theirs=others_touched(root,session)
        for repo in _turn_repos(cwd,state):
            found=changed_since(repo,since) if since else []
            start=heads.get(repo.as_posix())
            head=head_oid(repo) if start else None
            if start and head and head!=start:
                found+=committed_since(repo,start,head)
                moved.append((repo,start,head))
                pulled=pulled or (commit_count(repo,start,head) or 0)>CHANGE_GUARD_COMMITS
            total+=len(found)
            for path in found:
                name,p=show(path)
                if name and name not in theirs:files[name]=p
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
    prefixes=domain_prefixes(domain,bindings)
    files,pulled,edited=(changed or (lambda:turn_changed(root,bindings,state,domain,cwd)))()
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
    skip=set(state.get('saved',[]))|set(state.get('followed',[]))
    wanted={}
    digested=set(state.get('hinted_digests') or [])
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
        return (f"The user {said}{' (approved: '+data['plan']+')' if data.get('plan') else ''}{' (files changed too)' if data.get('files') else ''}. "
                f"Durable: update its note or CMD capture --session {session} {hint} --title '...' --summary '...'.")
    if kind=='fix':
        return (f"A command failed and then passed after an edit (`{data['command']}`). If the cause was not obvious, "
                f"capture a bug or knowledge note with Cause and Fix: CMD capture --session {session} --type bug --title '...' --summary '...' --section Cause '...' --section Fix '...'.")
    if kind=='doc':
        text=(f"You changed {data['path']}. Record its decision "
              f"(CMD capture --session {session} --type decision --title '...' --summary '...' --link '{data['path']}')")
        named=data['notes']
        if named:text+=(', or update: '+'; '.join(f"{n['short']} {n['title']}" for n in named)
                        +f" (CMD update {named[0]['short']} --session {session} --set-detail '**Label:** ...' --expected-revision REV)")
        return text+'.'
    if kind=='follows':   # several notes about edited files in one sentence instead of one per note
        what='files whose digests may be outdated' if all(d.get('digest') for d in data) else 'files that notes cover'
        text=(f"You edited {what}: {', '.join(d['short']+' ('+d['path']+')' for d in data)}. "
              f"For each: still true, CMD confirm REF --session {session} --expected-revision REV; changed, CMD update REF --session {session} --set-detail '**Label:** ...' --expected-revision REV.")
        if any(d.get('type')=='bug' for d in data):text+=f" If one fixed a bug: CMD update REF --session {session} --set-detail '**Fix:** ...' then CMD resolve REF --expected-revision REV."
        return text
    if kind=='follow':
        s=data['short']
        sec=data.get('type') in {'decision','runbook','research','knowledge'}
        edit="--set-detail '**Label:** ...' (or --section 'Heading' '...')" if sec else "--set-detail '**Label:** ...'"
        text=(f"You edited {data['path']}, which note {s} {data['title']} covers. Still true: CMD confirm {s} --session {session} --expected-revision REV; "
              f"changed: CMD update {s} --session {session} {edit} --expected-revision REV.")
        if data.get('type')=='bug':text+=f" If this fixed it: CMD update {s} --session {session} --set-detail '**Fix:** ...' then CMD resolve {s} --expected-revision REV."
        return text
    if kind=='proposals':
        # A title is subagent text: one line, quoted and cut, so it reads as data and never as a hook instruction of its own.
        named='; '.join(f"{r['id']} \"{' '.join(str((r.get('note') or {}).get('title') or '').split())[:80]}\"" for r in data)
        return (f"A subagent staged draft notes for you (titles are its words): {named}. Check each against what you know, then save it with "
                f"CMD proposals accept ID --session {session} (to edit one, pass a whole note to CMD capture --from-proposal ID) or drop it with CMD proposals reject ID --reason '...'.")
    if kind=='unbriefed':
        return (f"{data['name']} has no project brief: "
                f"CMD capture --session {session} --type brief --file '{data['dir']}' --title '{data['name']} project brief' --summary '...' "
                "--detail '**Goals and scope:** ...'. Write what you know now and mark unknowns as unknown.")
    if kind=='brief':
        return (f"a decision was made or recorded this turn: if project brief {data['short']} no longer matches it, "
                f"CMD update {data['short']} --session {session} --set-detail '**Key decisions:** ...' --expected-revision REV (or Team and ownership, Current phase, status and blockers).")
    return map_sentence(data[0],kind,data[1],session)

HEADLESS_TEXT="This is a non-interactive run and only your last message is returned: after saving, end with your answer to the task repeated in full."

def headless():
    """True in a non-interactive run (`claude -p`, the SDK), where the caller receives only the final message."""
    from .clock import headless_env
    return headless_env()

def stop_text(pending,session,used=None):
    """The Stop continuation for what stop_pending found, about STOP_TEXT_CAP characters: the first item always,
    later ones while they fit, at most STOP_DOCS documents and STOP_NOTES notes named, and the notes about edited
    files (STOP_FOLLOW at most) together in one sentence when there are several. Short refs and exact commands,
    closing with how to say nothing is durable. used, when a list, receives the items the text covers."""
    parts=[]
    docs=notes=0
    closing=f" Nothing durable: CMD capture-finalize --session {session}."
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
        while not fits(part) and kind=='doc' and data['notes']:
            data={**data,'notes':data['notes'][:-1]}
            part=_stop_part(kind,data,session)
        # The batch is trimmed to the real budget even as the first part (fits() always admits the first), keeping one note.
        while kind=='follows' and len(data)>1 and len('Living Memory: '+' '.join(parts+[part]))+len(closing)+len(tail)>STOP_TEXT_CAP:
            data=data[:-1]
            part=_stop_part(kind,data,session)
        if not fits(part):continue
        parts.append(part)
        if kind=='doc':
            docs+=1
            notes+=len(data['notes'])
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
        swallowed('runtime.session_map',exc,root)
        return None
