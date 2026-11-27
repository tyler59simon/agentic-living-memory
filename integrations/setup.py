"""Native adapter setup: one direct, journaled write per harness home (undo OPERATION reverses it). Never changes sandbox or trust policies."""
from __future__ import annotations
import argparse,difflib,hashlib,json,os,re,shlex,shutil,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.common import retry_sharing
from core.repository import read_json,canonical_json,digest
from core.governance import commit
from core.instructions import BLOCK,INSTRUCTION_FILE,harness_home,instruction_block as _block,merge_block
ROOT=Path(__file__).resolve().parents[1]
EVENTS={'SessionStart':'session-start','UserPromptSubmit':'user-prompt','SubagentStart':'subagent-start','PreToolUse':'pre-tool-use','Stop':'stop','PreCompact':'pre-compact'}
# Tools that can write files or run the memory script need the guard. Read, Glob, Grep and subagent spawns
# (Agent, formerly Task) come for codebase maps; hooks/claude_hooks.py answers repeat reads from core/hookfast.py.
PRE_TOOL_MATCHER='Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|Read|Glob|Grep|Agent|Task'

CONFIG={'claude':'settings.json','codex':'hooks.json','copilot':'hooks/agentic-living-memory.json','factory':'hooks.json','antigravity':'hooks.json'}
ANTIGRAVITY_GROUP='agentic-living-memory'
ANTIGRAVITY_OWNERSHIP='.living-memory-antigravity-ownership.json'
# Claude and Codex nest the event map under "hooks"; Factory's hooks.json is the event map itself.
NESTED={'claude','codex'}

# Claude Code's system folder for managed-settings.json and managed-settings.d/ (code.claude.com/docs/en/managed-settings).
MANAGED_DIR=Path(r'C:\Program Files\ClaudeCode') if os.name=='nt' else Path('/Library/Application Support/ClaudeCode') if sys.platform=='darwin' else Path('/etc/claude-code')

def _script(harness,root=None):return Path(root or ROOT)/'hooks'/(harness+'_hooks.py')

def _registry_policies():
    """(label, text) for the Claude Code policy JSON in the HKLM and HKCU registry, on Windows."""
    if os.name!='nt':return
    import winreg
    for hive,name in ((winreg.HKEY_LOCAL_MACHINE,'HKLM'),(winreg.HKEY_CURRENT_USER,'HKCU')):
        try:
            with winreg.OpenKey(hive,r'SOFTWARE\Policies\ClaudeCode') as key:value,kind=winreg.QueryValueEx(key,'Settings')
        except OSError:continue
        if kind in (winreg.REG_SZ,winreg.REG_EXPAND_SZ):yield name+r'\SOFTWARE\Policies\ClaudeCode\Settings',value

def _managed_policies():
    """(label, settings) for each local Claude Code managed settings source that holds a JSON object. Missing,
    unreadable and invalid sources are skipped. Server-managed settings have no documented local file, so they are not seen."""
    files=[MANAGED_DIR/'managed-settings.json']
    try:files+=sorted((MANAGED_DIR/'managed-settings.d').glob('*.json'))
    except OSError:pass
    texts=[]
    for path in files:
        try:texts.append((str(path),path.read_text(encoding='utf-8-sig')))
        except (OSError,UnicodeError):pass
    for label,text in texts+list(_registry_policies()):
        try:value=json.loads(text)
        except ValueError:continue
        if isinstance(value,dict):yield label,value

def _managed_hooks_lock():
    """Managed sources that set allowManagedHooksOnly, under which Claude Code skips the hooks in ~/.claude/settings.json."""
    return [label for label,policy in _managed_policies() if policy.get('allowManagedHooksOnly') is True]

def _hooks_disabled():
    """Managed sources that set disableAllHooks, under which Claude Code runs no hooks at all."""
    return [label for label,policy in _managed_policies() if policy.get('disableAllHooks') is True]

def _legacy_script(harness):
    """Where releases before the harness-neutral install put this adapter (CODEX_HOME/agentic-living-memory)."""
    return Path(os.environ.get('CODEX_HOME',Path.home()/'.codex')).expanduser()/'agentic-living-memory'/'hooks'/(harness+'_hooks.py')

def _copies():
    """Folders this package may have been wired from: this copy, the installed copy, and the source checkout this copy was installed from."""
    found=[ROOT]
    try:
        import install
        found.append(install.INSTALL_DIR)
    except Exception:pass   # no install.py beside this copy: nothing more to know
    try:recorded=read_json(ROOT/'.core-ownership-v1.json',{}).get('source')
    except (ValueError,AttributeError):recorded=None   # an unreadable record: this copy was not installed by install.py
    return found+([Path(recorded)] if isinstance(recorded,str) and recorded else [])

def _runs(text,script):
    """True when text runs this script: path case and, on Windows, slash direction are ignored, and the path must end at a quote, whitespace or the end (not claude_hooks.py.bak)."""
    text,script=os.path.normcase(text),os.path.normcase(str(script));start=text.find(script)
    while start>=0:
        end=start+len(script)
        if end==len(text) or text[end] in '"\' \t\r\n':return True
        start=text.find(script,start+1)
    return False

def _ours(handler,harness):
    """A handler this package manages: it runs our adapter script from this copy, the installed copy, its source checkout or the legacy location."""
    if not isinstance(handler,dict):return False
    text=' '.join([str(handler.get(key,'')) for key in ('command','commandWindows','bash','powershell')]+[str(a) for a in handler.get('args',[]) or []])
    if any(_runs(text,marker) for marker in [c/'hooks'/(harness+'_hooks.py') for c in _copies()]+[_legacy_script(harness)]):return True
    # The old Codex installer recognised its handlers by these two names (its Windows command may be encoded).
    return harness=='codex' and 'agentic-living-memory' in text and any(_runs(text,lead+'codex_hooks.py') for lead in ('/','\\',' ','"',"'"))   # the whole file name: not my_codex_hooks.py

def _events(config,harness):return config.get('hooks',{}) if harness in NESTED else config

def _copilot_file(config):
    """A Copilot hooks file: an object of version 1 (the one Copilot documents) whose hooks, if any, map each event to a list of handlers."""
    events=config.get('hooks',{}) if isinstance(config,dict) else None
    version=config.get('version',1) if isinstance(config,dict) else None
    return isinstance(events,dict) and version==1 and not isinstance(version,bool) and all(isinstance(handlers,list) for handlers in events.values())

def managed_handlers(config,harness):
    if harness=='antigravity':
        return {ANTIGRAVITY_GROUP:config[ANTIGRAVITY_GROUP]} if isinstance(config,dict) and ANTIGRAVITY_GROUP in config else {}
    if harness=='copilot':   # Copilot lists handlers under each event directly, with no groups
        events=config.get('hooks',{}) if _copilot_file(config) else {}
        return {event:[h for h in handlers if _ours(h,harness)] for event,handlers in events.items() if any(_ours(h,harness) for h in handlers)}
    events=_events(config,harness)
    return {event:[{**group,'hooks':[h for h in group.get('hooks',[]) if _ours(h,harness)]} for group in groups if any(_ours(h,harness) for h in group.get('hooks',[]))] for event,groups in events.items() if isinstance(groups,list) and any(isinstance(group,dict) and any(_ours(h,harness) for h in group.get('hooks',[])) for group in groups)}

def _plain_path(path):
    """True when a program path can sit unquoted in both PowerShell and cmd.exe: no whitespace and no shell syntax characters."""
    return bool(path) and not re.search(r"""[\s"'`$%&|<>()^;,{}\[\]@#!]""",path)

def _short_path(path):
    """The Windows 8.3 short form of a path, or None when the call fails. A volume without 8.3 names returns the long path unchanged."""
    try:
        import ctypes
        buffer=ctypes.create_unicode_buffer(32768)
        size=ctypes.windll.kernel32.GetShortPathNameW(str(path),buffer,len(buffer))
        return buffer.value if 0<size<len(buffer) else None
    except (AttributeError,OSError):return None

def hook_command(script,action):
    """A Codex command handler. On Windows Codex runs commandWindows through a shell that is not pinned down (PowerShell or
    cmd.exe), and a quoted executable alone is a string expression in PowerShell, so the program must be a bare path without
    spaces: the py launcher, else this interpreter, else its 8.3 short path. Only when none of them qualifies does it fall
    back to an encoded PowerShell command, which EDR tools may flag (setup warns)."""
    bad='"$`'+('%' if os.name=='nt' else '')
    if any(c in str(script) for c in bad):raise ValueError(f'The hook script path {script} contains one of {" ".join(bad)}, which no quoting makes safe in both PowerShell and cmd.exe. Install Living Memory in a folder without them.')
    script_arg=f'"{script}"'
    if os.name=='nt':
        launcher=shutil.which('py');tail=f'-X utf8 {script_arg} {action}'
        if _plain_path(launcher):windows=f'{launcher} -{sys.version_info.major}.{sys.version_info.minor} {tail}'
        else:
            program=next((p for p in (sys.executable,_short_path(sys.executable)) if _plain_path(p)),None)
            if program:windows=f'{program} {tail}'
            else:
                import base64
                expression='& "'+str(sys.executable).replace('"','`"')+'" '+tail
                windows='powershell.exe -NoProfile -NonInteractive -EncodedCommand '+base64.b64encode(expression.encode('utf-16-le')).decode('ascii')
        return {'type':'command','command':f'python3 {script_arg} {action}','commandWindows':windows}
    return {'type':'command','command':f'"{sys.executable}" {script_arg} {action}'}

def codex_groups(script):
    def handler(action,status,limit=None):
        item={**hook_command(script,action),'statusMessage':status,'timeout':30}
        if limit is not None:item['additionalContextLimit']=limit
        return item
    return {'SessionStart':[{'matcher':'startup|resume|clear|compact|fork','hooks':[handler('session-start','Loading project memory',9000)]}],
            'UserPromptSubmit':[{'hooks':[handler('user-prompt','Searching project memory',9000)]}],
            'SubagentStart':[{'hooks':[handler('subagent-start','Applying memory access rules',1000)]}],
            'PreToolUse':[{'hooks':[handler('pre-tool-use','Checking protected memory paths')]}],
            'Stop':[{'hooks':[handler('stop','Checking durable capture completion')]}],
            'PreCompact':[{'hooks':[handler('pre-compact','Recording the memory handoff')]}]}

def hooks(harness,root=None):
    """The handlers setup writes for a harness. root is the package copy they run (default: this copy); install.py passes
    the installed copy to tell which harnesses an update would rewire."""
    script=_script(harness,root)
    if harness=='antigravity':
        from hooks.antigravity_hooks import EVENTS as ANTIGRAVITY_EVENTS, TOOLS
        if os.name=='nt':
            for path in (sys.executable,str(script)):
                if any(char in path for char in ('"', '%', '!', '\r', '\n')):
                    raise ValueError(f'Antigravity CLI cannot safely quote the hook path {path}')
            command=f'"{sys.executable}" -X utf8 "{script}"'
        else:
            command=f'{shlex.quote(sys.executable)} -X utf8 {shlex.quote(str(script))}'
        events={}
        for event in ANTIGRAVITY_EVENTS:
            handlers=[{'type':'command','command':f'{command} {event}','timeout':30}]
            events[event]=[{'matcher':'|'.join(TOOLS),'hooks':handlers}] if event=='PreToolUse' else handlers
        return {ANTIGRAVITY_GROUP:events}
    if harness=='codex':return codex_groups(script)
    if harness=='copilot':
        mapped={'sessionStart':'sessionStart','userPromptTransformed':'userPromptTransformed','subagentStart':'subagentStart','preToolUse':'preToolUse','agentStop':'agentStop'}
        return {'version':1,'hooks':{event:[{'type':'command','bash':f'"{sys.executable}" "{script}" {action}','powershell':f'& "{sys.executable}" "{script}" {action}','timeoutSec':15}] for event,action in mapped.items()}}
    groups={}
    for event,action in EVENTS.items():
        if harness=='factory' and event=='SubagentStart':continue  # Factory has no SubagentStart event.
        # Factory shows PreCompact stdout only in transcript views and cannot block on it, so the hook would add nothing;
        # its SessionStart(source=compact) still delivers the handoff.
        if harness=='factory' and event=='PreCompact':continue
        if harness=='claude':
            # Exec form runs without a shell, so it works with or without Git Bash on Windows.
            handler={'type':'command','command':sys.executable,'args':[str(script),action],'timeout':15}
        else:
            # Factory documents only shell-form command strings.
            handler={'type':'command','command':f'"{sys.executable}" "{script}" {action}','timeout':15}
        group={'hooks':[handler]}
        if event=='PreToolUse':group={'matcher':PRE_TOOL_MATCHER,**group}
        groups[event]=[group]
    if harness=='claude':
        # Session end syncs managed Obsidian vaults (a no-op without one), takes a backup when one is due and prunes old journals.
        groups['SessionEnd']=[{'hooks':[{'type':'command','command':sys.executable,'args':[str(script),'session-end'],'timeout':60}]}]
    return groups

def instruction_block(harness,enable_hooks,root=None):
    # Hooked harnesses get the machine-independent static protocol; instructions-only Factory needs the script path.
    return _block(harness,enable_hooks,f'"{sys.executable}" "{Path(root or ROOT)/"living_memory.py"}"')

def pretty(value):return json.dumps(value,indent=2,ensure_ascii=False)+'\n'

def _diff(name,edited,managed):
    """A unified diff from the edited text to what this package would write."""
    return '\n'.join(difflib.unified_diff(edited.splitlines(),managed.splitlines(),'edited '+name,'managed '+name,lineterm=''))

def _refuse(what,path,harness,uninstall):
    flag='--uninstall' if uninstall else '--hooks';script=ROOT/'integrations'/'setup.py'   # outside the f-string: nested quotes need Python 3.12
    raise ValueError(f'{what} preserved: {path} was edited. Run "{sys.executable}" "{script}" {harness} {flag} --adopt to take ownership (it prints the difference it replaced), or undo the edit and run it without --adopt')

def _reader(home):
    """Read files under home once each and record the digest of the bytes read, so the write refuses a file changed after this read."""
    read_set={}
    def read(rel):
        try:data=retry_sharing(lambda:(home/rel).read_bytes())
        except FileNotFoundError:data=None
        read_set[rel]=digest(data);return data
    def read_config(rel,default):
        """read_json() on the bytes read: the same default and the same error."""
        try:
            data=read(rel)
            return default if data is None else json.loads(data.decode('utf-8-sig'))
        except (OSError,UnicodeError,json.JSONDecodeError) as exc:raise ValueError(f'Invalid or unreadable configuration preserved: {home/rel}') from exc
    return read,read_config,read_set

def _write(home,harness,changes,uninstall,summary,read_set):
    """One journaled write in the harness home; its `s-HARNESS-` operation ID is what undo takes."""
    receipt=commit(home,changes,harness+'_uninstall' if uninstall else harness+'_setup',summary=summary,guards={'read_set':read_set},
                   operation_id=f's-{harness}-{uuid.uuid4().hex}')
    home=Path(home).resolve()
    if home!=Path(harness_home(harness)).expanduser().resolve():   # undo looks only in the configured home; name this one
        receipt['undo']=f'--root {_quote(str(home))} undo {receipt["operation_id"]}'
    return receipt

def _quote(text):
    """Double quotes as before; a path with $, a backtick or a double quote, or on POSIX a backslash (double quotes collapse
    a doubled one), gets single quotes, which PowerShell and POSIX shells read literally (an apostrophe is then doubled for
    PowerShell or closed and escaped for POSIX shells)."""
    if not any(c in text for c in '$`"'+('' if os.name=='nt' else '\\')):return f'"{text}"'
    return "'"+text.replace("'","''" if os.name=='nt' else "'\\''")+"'"

def _antigravity(home,uninstall,adopt):
    """Own one named hook group in Antigravity's shared config file and the managed block in its global GEMINI.md."""
    read,read_config,read_set=_reader(home)
    name=CONFIG['antigravity']
    current=read_config(name,{})
    state=read_config(ANTIGRAVITY_OWNERSHIP,{})
    if not isinstance(current,dict) or not isinstance(state,dict):
        raise ValueError(f'Invalid Antigravity hook configuration or ownership record preserved under {home}')
    previous=managed_handlers(current,'antigravity')
    wanted={} if uninstall else hooks('antigravity')
    summary={'harness':'antigravity','hooks':not uninstall,'package':str(ROOT),'interpreter':sys.executable,'sandbox':'unchanged','trust':'native user review required',
             'note':'Antigravity: wire format verified live with agy 1.5.15 on Windows; the CLI and App read GEMINI.md from this folder, the IDE does not.'}
    adopted={}
    if (previous or state) and digest(canonical_json(previous).encode())!=state.get('hooks_hash'):
        if not adopt:
            _refuse('Unknown or modified managed hook group',home/name,'antigravity',uninstall)
        adopted[name]=(pretty(previous),pretty(wanted))
    instruction=INSTRUCTION_FILE['antigravity'];data=read(instruction)
    original=data.decode('utf-8').replace('\r\n','\n').replace('\r','\n') if data is not None else ''
    block=instruction_block('antigravity',True)
    owned_hash=state.get('instruction_hash')
    found=BLOCK.findall(original)
    if len(found)==1 and digest(found[0].encode())!=owned_hash:
        if not adopt:_refuse('Unknown or modified managed instructions',home/instruction,'antigravity',uninstall)
        adopted[instruction]=(found[0],'' if uninstall else block);owned_hash=digest(found[0].encode())
    text=merge_block(original,block,owned_hash,digest,uninstall)
    if adopted:summary['adopt']={path:_diff(path,*texts) for path,texts in adopted.items()}
    updated={k:v for k,v in current.items() if k!=ANTIGRAVITY_GROUP}
    updated.update(wanted)
    changes={ANTIGRAVITY_OWNERSHIP:None if uninstall else canonical_json({'hooks_hash':digest(canonical_json(wanted).encode()),'instruction_hash':digest(block.encode())})}
    if wanted or previous:
        changes[name]=pretty(updated)
    if data is not None or not uninstall:
        # A GEMINI.md that held only the block is removed with it.
        changes[instruction]=None if uninstall and not text.strip() else text
    return _write(home,'antigravity',changes,uninstall,summary,read_set)

def setup(harness,home=None,enable_hooks=False,uninstall=False,adopt=False):
    if harness not in CONFIG:raise ValueError('Unknown harness')
    home=Path(home or harness_home(harness)).expanduser().resolve();home.mkdir(parents=True,exist_ok=True)
    if harness=='antigravity':return _antigravity(home,uninstall,adopt)
    read,read_config,read_set=_reader(home)
    state=read_config('.living-memory-ownership.json',{'files':{},'instruction_hash':None})
    instruction=INSTRUCTION_FILE[harness]
    path=home/instruction;data=read(instruction)
    original=data.decode('utf-8').replace('\r\n','\n').replace('\r','\n') if data is not None else ''   # as read_text() did
    block=instruction_block(harness,enable_hooks)
    owned_hash=state.get('instruction_hash');adopted={};edited=None
    found=BLOCK.findall(original)
    if len(found)==1 and digest(found[0].encode())!=owned_hash:
        if not adopt:_refuse('Unknown or modified managed instructions',path,harness,uninstall)
        adopted[instruction]=(found[0],'' if uninstall else block);owned_hash=digest(found[0].encode())
    text=merge_block(original,block,owned_hash,digest,uninstall)
    changes={instruction:text};owned=dict(state.get('files',{}));config_name=CONFIG[harness]
    config_path=home/config_name
    if harness!='factory' or enable_hooks or config_name in owned:
        current=read_config(config_name,{})
        owned_handlers=state.get('handlers',{}).get(config_name)
        if owned_handlers is not None:
            if digest(canonical_json(managed_handlers(current,harness)).encode())!=owned_handlers:
                if not adopt:_refuse('Modified managed handlers',config_path,harness,uninstall)
                edited=pretty(managed_handlers(current,harness))
        elif config_name in owned and digest(canonical_json(current).encode())!=owned[config_name]:
            if not adopt:_refuse('Modified managed hook configuration',config_path,harness,uninstall)
            edited=pretty(managed_handlers(current,harness) if harness=='copilot' else current)   # Copilot keeps the user's hooks, so the diff is ours only
        generated=hooks(harness)
        if harness=='copilot':
            # Our handlers are replaced and every other hook and key is kept; uninstall deletes the file only when nothing else is in it.
            if current and not _copilot_file(current):raise ValueError('Unknown existing Copilot file preserved')
            # A file that still matches a whole-file record from an older build is all ours, even handlers this copy no longer
            # recognises (a checkout that moved since), so it is replaced whole.
            whole=owned_handlers is None and config_name in owned and digest(canonical_json(current).encode())==owned[config_name]
            events=dict(current.get('hooks',{})) if current and not whole else {}
            for event in list(events)+[e for e in generated['hooks'] if e not in events]:
                kept=[h for h in events.get(event,[]) if not _ours(h,harness)]
                if not uninstall:kept+=generated['hooks'].get(event,[])
                if kept:events[event]=kept
                else:events.pop(event,None)
            updated={'version':generated['version'],**(current or {}),'hooks':events}
            if uninstall and not events and set(updated)<={'version','hooks'}:updated=None
        else:
            updated=dict(current);events=dict(_events(updated,harness))
            for event in sorted(set(generated)|set(EVENTS)):
                kept=[]
                for group in events.get(event,[]):
                    handlers=[h for h in group.get('hooks',[]) if not _ours(h,harness)]
                    if handlers:kept.append({**group,'hooks':handlers})
                if not uninstall:kept+=generated.get(event,[])
                if kept:events[event]=kept
                else:events.pop(event,None)
            if harness in NESTED:updated['hooks']=events
            else:updated=events
            if harness=='codex' and not uninstall:updated.setdefault('description','Codex user-level lifecycle hooks.')
        changes[config_name]=pretty(updated) if updated is not None else None
        if edited is not None:
            managed=pretty(managed_handlers(updated,harness)) if updated is not None else pretty({})
            adopted[config_name]=(edited,managed)
        if updated is not None:owned[config_name]=digest(canonical_json(updated).encode())
    handler_state=dict(state.get('handlers',{}))
    if config_name in changes and not uninstall:handler_state[config_name]=digest(canonical_json(managed_handlers(updated,harness)).encode())
    changes['.living-memory-ownership.json']=None if uninstall else canonical_json({'schema':2,'files':owned,'handlers':handler_state,'instruction_hash':digest(block.encode())})
    summary={'harness':harness,'hooks':harness!='factory' or enable_hooks,'package':str(ROOT),'interpreter':sys.executable,'sandbox':'unchanged','trust':'native user review required'}
    if adopted:summary['adopt']={name:_diff(name,*texts) for name,texts in adopted.items()}
    if harness=='factory':summary['note']='Factory: hooks.json shape verified against docs; not exercised in a live Droid session.'
    if harness=='copilot':summary['note']='Copilot: event names verified against the CLI hooks reference; not exercised in a live session.'
    if harness=='codex':summary['note']='Codex: handlers carry status messages and context limits; handlers of the old CODEX_HOME/agentic-living-memory copy are replaced.'
    if harness=='codex' and not uninstall and any('-EncodedCommand' in h.get('commandWindows','') for groups in generated.values() for group in groups for h in group['hooks']):
        summary['warning']='No py launcher or space-free Python path is available, so the Windows hook commands use an encoded PowerShell command, which EDR tools may flag. Installing the py launcher or Python in a folder without spaces in its path avoids it.'
    if harness=='claude' and not uninstall:
        locked=_managed_hooks_lock()
        if locked:summary['warning']=f'allowManagedHooksOnly is true in managed settings ({", ".join(locked)}): Claude Code will not run the hooks this setup adds to settings.json. Living Memory works only through the CLI until an administrator deploys the hooks in managed settings.'
    return _write(home,harness,changes,uninstall,summary,read_set)

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('harness',choices=sorted(CONFIG));p.add_argument('--home',type=Path);p.add_argument('--location',type=Path);p.add_argument('--hooks',action='store_true');p.add_argument('--uninstall',action='store_true');p.add_argument('--adopt',action='store_true',help='Take ownership of a managed block or handler you edited; the difference it replaced is printed');a=p.parse_args(argv)
    from core.storage import refuse_agent_setup
    refuse_agent_setup()
    home=a.home or harness_home(a.harness)
    if not a.uninstall:
        from core.storage import ensure_setup
        ensure_setup(a.location)
    result=setup(a.harness,home,a.hooks,a.uninstall,a.adopt)
    for name,diff in result['summary'].get('adopt',{}).items():print(f'Adopted {name}; this difference was replaced ({result["undo"]} puts it back):{chr(10)}{diff}',file=sys.stderr)
    if result['summary'].get('warning'):print('Warning: '+result['summary']['warning'],file=sys.stderr)
    print(json.dumps(result));return 0
if __name__=='__main__':raise SystemExit(main())
