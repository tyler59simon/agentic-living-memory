"""Plan-bound native adapter setup. Never changes sandbox or trust policies."""
from __future__ import annotations
import argparse,hashlib,json,os,re,shutil,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.repository import read_json,canonical_json,digest
from core.governance import plan,apply
from core.instructions import INSTRUCTION_FILE,instruction_block as _block,merge_block
ROOT=Path(__file__).resolve().parents[1]
EVENTS={'SessionStart':'session-start','UserPromptSubmit':'user-prompt','SubagentStart':'subagent-start','PreToolUse':'pre-tool-use','Stop':'stop','PreCompact':'pre-compact'}
# Only tools that can write files or run the memory script need the guard; read tools skip Python entirely.
PRE_TOOL_MATCHER='Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell'

CONFIG={'claude':'settings.json','codex':'hooks.json','copilot':'hooks/agentic-living-memory.json','factory':'hooks.json'}
# Claude and Codex nest the event map under "hooks"; Factory's hooks.json is the event map itself.
NESTED={'claude','codex'}

def _script(harness):return ROOT/'hooks'/(harness+'_hooks.py')

def _legacy_script(harness):
    """Where releases before the harness-neutral install put this adapter (CODEX_HOME/agentic-living-memory)."""
    return Path(os.environ.get('CODEX_HOME',Path.home()/'.codex')).expanduser()/'agentic-living-memory'/'hooks'/(harness+'_hooks.py')

def _ours(handler,harness):
    """A handler this package manages: it runs our adapter script from this copy or the legacy location."""
    if not isinstance(handler,dict):return False
    text=' '.join([str(handler.get('command','')),str(handler.get('commandWindows',''))]+[str(a) for a in handler.get('args',[]) or []])
    if any(str(marker) in text for marker in (_script(harness),_legacy_script(harness))):return True
    # The old Codex installer recognised its handlers by these two names (its Windows command may be encoded).
    return harness=='codex' and 'agentic-living-memory' in text and 'codex_hooks.py' in text

def _events(config,harness):return config.get('hooks',{}) if harness in NESTED else config

def managed_handlers(config,harness):
    events=_events(config,harness)
    return {event:[{**group,'hooks':[h for h in group.get('hooks',[]) if _ours(h,harness)]} for group in groups if any(_ours(h,harness) for h in group.get('hooks',[]))] for event,groups in events.items() if isinstance(groups,list) and any(isinstance(group,dict) and any(_ours(h,harness) for h in group.get('hooks',[])) for group in groups)}

def hook_command(script,action):
    """A Codex command handler. On Windows Codex runs commandWindows; a quoted executable alone is a string
    expression in PowerShell, so use the py launcher, or an encoded PowerShell command when it is unavailable."""
    script_arg=f'"{script}"'
    if os.name=='nt':
        launcher=shutil.which('py')
        if launcher and not any(c.isspace() for c in launcher):
            windows=f'{launcher} -{sys.version_info.major}.{sys.version_info.minor} -X utf8 {script_arg} {action}'
        else:
            import base64
            expression='& "'+str(sys.executable).replace('"','`"')+'" -X utf8 '+script_arg+' '+action
            windows='powershell.exe -NoProfile -NonInteractive -EncodedCommand '+base64.b64encode(expression.encode('utf-16-le')).decode('ascii')
        return {'type':'command','command':f'python3 {script_arg} {action}','commandWindows':windows}
    return {'type':'command','command':f'"{sys.executable}" {script_arg} {action}'}

def codex_groups(script):
    def handler(action,status,limit=None):
        item={**hook_command(script,action),'statusMessage':status,'timeout':30}
        if limit is not None:item['additionalContextLimit']=limit
        return item
    return {'SessionStart':[{'matcher':'startup|resume|clear|compact|fork','hooks':[handler('session-start','Loading project memory',4000)]}],
            'UserPromptSubmit':[{'hooks':[handler('user-prompt','Searching project memory',4000)]}],
            'SubagentStart':[{'hooks':[handler('subagent-start','Applying memory access rules',1000)]}],
            'PreToolUse':[{'hooks':[handler('pre-tool-use','Checking protected memory paths')]}],
            'Stop':[{'hooks':[handler('stop','Checking durable capture completion')]}],
            'PreCompact':[{'hooks':[handler('pre-compact','Recording the memory handoff')]}]}

def hooks(harness):
    script=_script(harness)
    if harness=='codex':return codex_groups(script)
    if harness=='copilot':
        mapped={'sessionStart':'sessionStart','userPromptTransformed':'userPromptTransformed','subagentStart':'subagentStart','preToolUse':'preToolUse','agentStop':'agentStop'}
        return {'version':1,'hooks':{event:[{'type':'command','bash':f'"{sys.executable}" "{script}" {action}','powershell':f'& "{sys.executable}" "{script}" {action}','timeoutSec':15}] for event,action in mapped.items()}}
    groups={}
    for event,action in EVENTS.items():
        if harness=='factory' and event=='SubagentStart':continue  # Factory has no SubagentStart event.
        if harness=='factory' and event=='PreCompact':continue  # Not verified against Factory's docs.
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
        # Session end pulls Obsidian edits into memory and refreshes managed vaults.
        groups['SessionEnd']=[{'hooks':[{'type':'command','command':sys.executable,'args':[str(script),'session-end'],'timeout':60}]}]
    return groups

def instruction_block(harness,enable_hooks):
    # Hooked harnesses get the machine-independent static protocol; instructions-only Factory needs the script path.
    return _block(harness,enable_hooks,f'"{sys.executable}" "{ROOT/"living_memory.py"}"')

def pretty(value):return json.dumps(value,indent=2,ensure_ascii=False)+'\n'

def setup_plan(harness,home=None,enable_hooks=False,uninstall=False):
    if harness not in CONFIG:raise ValueError('Unknown harness')
    home=Path(home or os.environ.get(harness.upper()+'_HOME',Path.home()/('.'+harness))).expanduser().resolve();home.mkdir(parents=True,exist_ok=True)
    state=read_json(home/'.living-memory-ownership.json',{'files':{},'instruction_hash':None})
    instruction=INSTRUCTION_FILE[harness]
    path=home/instruction;original=path.read_text(encoding='utf-8') if path.exists() else ''
    block=instruction_block(harness,enable_hooks)
    text=merge_block(original,block,state.get('instruction_hash'),digest,uninstall)
    changes={instruction:text};owned=dict(state.get('files',{}));config_name=CONFIG[harness]
    config_path=home/config_name
    if harness!='factory' or enable_hooks or config_name in owned:
        current=read_json(config_path,{})
        owned_handlers=state.get('handlers',{}).get(config_name)
        if owned_handlers is not None:
            if digest(canonical_json(managed_handlers(current,harness)).encode())!=owned_handlers:raise ValueError('Modified managed handlers preserved')
        elif config_name in owned and digest(canonical_json(current).encode())!=owned[config_name]:raise ValueError('Modified managed hook configuration preserved')
        generated=hooks(harness)
        if harness=='copilot':
            if current and config_name not in owned:raise ValueError('Unknown existing Copilot file preserved')
            updated=None if uninstall else generated
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
        if updated is not None:owned[config_name]=digest(canonical_json(updated).encode())
    handler_state=dict(state.get('handlers',{}))
    if config_name in changes and harness!='copilot' and not uninstall:handler_state[config_name]=digest(canonical_json(managed_handlers(updated,harness)).encode())
    changes['.living-memory-ownership.json']=None if uninstall else canonical_json({'schema':2,'files':owned,'handlers':handler_state,'instruction_hash':digest(block.encode())})
    summary={'harness':harness,'hooks':harness!='factory' or enable_hooks,'package':str(ROOT),'interpreter':sys.executable,'sandbox':'unchanged','trust':'native user review required'}
    if harness=='factory':summary['note']='Factory: hooks.json shape verified against docs; not exercised in a live Droid session.'
    if harness=='copilot':summary['note']='Copilot: event names verified against the CLI hooks reference; not exercised in a live session.'
    if harness=='codex':summary['note']='Codex: handlers carry status messages and context limits; handlers of the old CODEX_HOME/agentic-living-memory copy are replaced.'
    return plan(home,changes,harness+'_uninstall' if uninstall else harness+'_setup',summary=summary)

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('harness',choices=sorted(CONFIG));p.add_argument('--home',type=Path);p.add_argument('--location',type=Path);p.add_argument('--hooks',action='store_true');p.add_argument('--uninstall-plan',action='store_true');p.add_argument('--apply-plan');p.add_argument('--expected-plan-sha256');a=p.parse_args(argv)
    home=a.home or Path(os.environ.get(a.harness.upper()+'_HOME',Path.home()/('.'+a.harness)))
    if not a.uninstall_plan and not a.apply_plan:
        from core.storage import ensure_setup
        ensure_setup(a.location)
    if a.apply_plan:
        if not a.expected_plan_sha256:p.error('Expected plan digest required')
        result=apply(home,a.apply_plan,a.expected_plan_sha256)
    else:result=setup_plan(a.harness,home,a.hooks,a.uninstall_plan)
    print(json.dumps(result));return 0
if __name__=='__main__':raise SystemExit(main())
