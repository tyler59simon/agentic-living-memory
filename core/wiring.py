"""Whether each harness's hooks still reach this package: read-only checks for `doctor`.

Hook handlers hold absolute paths to an interpreter and an adapter script. When Python is upgraded or
the package moves, every hooked tool call fails inside the harness, where nothing from this package can
say why. `doctor` is the one place that can, so it reads each harness's hook configuration and checks
what the handlers point at. Nothing here writes."""
from __future__ import annotations
import os,re,shlex,shutil,subprocess
from pathlib import Path
from .instructions import harness_home,installed

ROOT=Path(__file__).resolve().parents[1]
COMMAND_KEYS=('command','commandWindows','bash','powershell')
SHELLS={'powershell','powershell.exe','pwsh','pwsh.exe','bash','sh','cmd','cmd.exe'}
LAUNCHER_VERSION=re.compile(r'-\d+(?:\.\d+)?')

ENCODED=re.compile(r'-EncodedCommand\s+([A-Za-z0-9+/=]+)')

def _handlers(value):
    """Every mapping in a hook configuration that carries a command."""
    if isinstance(value,dict):
        if any(isinstance(value.get(key),str) for key in COMMAND_KEYS):yield value
        for item in value.values():yield from _handlers(item)
    elif isinstance(value,list):
        for item in value:yield from _handlers(item)

def _plain(text):
    """A command line as written, with a PowerShell -EncodedCommand payload decoded (Codex on Windows without the py launcher)."""
    match=ENCODED.search(text)
    if not match:return text
    try:
        import base64
        return base64.b64decode(match[1]).decode('utf-16-le')
    except (ValueError,UnicodeError):return text

def _script(handler,harness):
    """The adapter script a handler runs (this package's hooks/<harness>_hooks.py, in any copy), else None."""
    tail=r'[\\/]hooks[\\/]'+re.escape(harness)+r'_hooks\.py'
    for argument in handler.get('args') or []:
        if re.search(tail+r'\Z',str(argument)):return str(argument)
    for key in COMMAND_KEYS:
        if not isinstance(handler.get(key),str):continue
        text=_plain(handler[key])
        if harness=='antigravity' and os.name!='nt':
            try:return next((word for word in shlex.split(text) if re.search(tail+r'\Z',word)),None)
            except ValueError:return None
        match=re.search(r'"([^"]*'+tail+r')"',text) or re.search(r"'([^']*"+tail+r")'",text) or re.search(r'(\S*'+tail+r')',text)
        if match:return match[1]
    return None

def _first_words(line):
    """The first two words of a shell command line, quotes and a leading PowerShell call operator removed."""
    line=line.strip()
    if line.startswith('&'):line=line[1:].lstrip()
    words=[]
    for match in re.finditer(r'"([^"]*)"|\'([^\']*)\'|(\S+)',line):
        words.append(next(group for group in match.groups() if group is not None))
        if len(words)==2:break
    return words+['']*(2-len(words))

def _interpreter(handler,harness=None):
    """(what the handler runs first, the word after it) on this platform."""
    if isinstance(handler.get('args'),list):return str(handler.get('command','')),''
    windows=os.name=='nt'
    line=next((handler[key] for key in (('commandWindows','powershell','command','bash') if windows else ('command','bash'))
               if isinstance(handler.get(key),str) and handler[key].strip()),'')
    if harness=='antigravity' and not windows:
        try:return (shlex.split(line)+['',''])[:2]
        except ValueError:return '',''
    first,second=_first_words(_plain(line))
    return first,second

def _runs(program,argument):
    """True when the program exists, None when it cannot be told (a shell wrapper), False when it is missing."""
    if not program:return False
    name=Path(program).name.lower()
    if name in SHELLS:return None
    found=program if Path(program).is_absolute() and Path(program).exists() else shutil.which(program)
    if not found:return False
    if name in {'py','py.exe'} and LAUNCHER_VERSION.fullmatch(argument or ''):
        # The Windows launcher is there, but the Python version the handler asks for may not be.
        try:return subprocess.run([found,argument,'-c','pass'],capture_output=True,timeout=10).returncode==0
        except (OSError,subprocess.SubprocessError):return False
    return True

def _same(a,b):
    from .paths import same_path
    try:return same_path(Path(a).resolve(),Path(b).resolve())
    except (OSError,ValueError):return False

def _current(harness,config,setup,root=None):
    """True when the managed handlers are exactly what this copy (or the copy at root) and this interpreter would write."""
    generated=setup.hooks(harness,root)
    if harness=='copilot' and not setup._copilot_file(config):return False   # Copilot would not load it
    wanted={'hooks':generated} if harness in setup.NESTED else generated
    return setup.managed_handlers(config,harness)==setup.managed_handlers(wanted,harness)

def check(harness,setup):
    """One harness's wiring, or None when it has no hook handlers of ours."""
    import json
    home=harness_home(harness)
    path=home/setup.CONFIG[harness]
    expected=harness=='antigravity' and (home/setup.ANTIGRAVITY_OWNERSHIP).is_file()
    if not path.is_file() and not expected:return None
    try:
        raw=path.read_text(encoding='utf-8-sig')
        config=json.loads(raw) if raw.strip() else {}
    except FileNotFoundError:
        config={}
    except PermissionError:
        return {'config':str(path),'wired':expected,'config_ok':False,
                'problems':[f'{path} cannot be read (permission denied)']}
    except OSError as exc:
        return {'config':str(path),'wired':expected,'config_ok':False,
                'problems':[f'{path} cannot be read ({exc})']}
    except ValueError:
        return {'config':str(path),'wired':expected,'config_ok':False,
                'problems':[f'{path} is not valid JSON, so the harness cannot load its hooks']}
    managed=setup.managed_handlers(config,harness) if harness=='antigravity' else config
    ours=[(handler,_script(handler,harness)) for handler in _handlers(managed)]
    ours=[(handler,script) for handler,script in ours if script]
    if not ours:
        if expected or (harness=='antigravity' and managed):
            return {'config':str(path),'wired':True,'config_ok':False,
                    'problems':[f'{path} has no Living Memory Antigravity commands; rerun integrations/setup.py antigravity from the installed copy']}
        return None
    handler,script=ours[0]
    program,argument=_interpreter(handler,harness)
    runs=_runs(program,argument);here=_same(script,ROOT/'hooks'/(harness+'_hooks.py'))
    result={'config':str(path),'wired':True,'handlers':len(ours),'script':script,'script_exists':Path(script).is_file(),
            'interpreter':' '.join(part for part in (program,argument if LAUNCHER_VERSION.fullmatch(argument or '') else '') if part),
            'interpreter_ok':runs,'this_copy':here,'problems':[]}
    fix=(f'run integrations/setup.py {harness} --hooks from {ROOT} with the Python you use now, then restart the harness')
    if not result['script_exists']:
        result['problems'].append(f'the hook script {script} is missing, so every hooked event fails; {fix}')
    if runs is False:
        result['problems'].append(f'the hook interpreter {result["interpreter"] or "(none)"} is missing, so every hooked event fails; {fix}')
    if harness=='copilot' and not setup._copilot_file(config):
        result['config_ok']=False
        result['problems'].append(f'{path} is not a Copilot hooks file of version 1, so Copilot may not load it and setup refuses it; set "version" to 1 (or remove it), then {fix}')
    if harness=='antigravity':
        group=managed[setup.ANTIGRAVITY_GROUP]
        if isinstance(group,dict) and group.get('enabled') is False:
            result['blocked']=[{'source':str(path),'setting':'enabled'}]
            result['problems'].append(f'the Living Memory hook group is disabled in {path}; {fix}')
    if harness=='claude':
        # Claude Code skips these handlers (without a word) under managed allowManagedHooksOnly or disableAllHooks, or the user's own disableAllHooks.
        ask='ask whoever manages this computer to deploy the hooks through managed settings (INTEGRATION.md, Managed Claude Code) or to lift the setting'
        blocked=[{'source':label,'setting':'allowManagedHooksOnly'} for label in setup._managed_hooks_lock()]+[{'source':label,'setting':'disableAllHooks'} for label in setup._hooks_disabled()]
        if isinstance(config,dict) and config.get('disableAllHooks') is True:blocked.append({'source':str(path),'setting':'disableAllHooks'})
        result['blocked']=blocked
        for row in blocked:
            if row['source']==str(path):result['problems'].append(f'{path} sets disableAllHooks, so Claude Code runs no hooks and memory is never loaded or captured in Claude sessions; remove "disableAllHooks" from it')
            elif row['setting']=='disableAllHooks':result['problems'].append(f'managed settings in {row["source"]} set disableAllHooks, so Claude Code runs no hooks and memory is never loaded or captured in Claude sessions; {ask}')
            else:result['problems'].append(f'managed settings in {row["source"]} set allowManagedHooksOnly, so Claude Code skips the hooks in {path} and memory is never loaded or captured in Claude sessions; {ask}')
    if here:
        # Only this copy can say what its own handlers and instruction block should look like.
        try:result['handlers_current']=_current(harness,config,setup)
        except Exception:result['handlers_current']=None
        result['instructions']='current' if installed(harness) else 'stale_or_missing'
        if result['handlers_current'] is False and not result['problems']:
            result['problems'].append(f"the hook handlers differ from what this copy and interpreter would write (events, matcher or timeouts changed); {fix}")
        if result.get('instructions')=='stale_or_missing':
            result['problems'].append(f'the managed instruction block is missing or out of date, so each session start injects the full protocol instead; {fix}')
    else:
        result['note']='the hooks run another copy of this package; run doctor from that copy to check its handlers and instruction block'
    return result

def needs_rewire(harness,setup,package_root):
    """Whether integrations/setup.py HARNESS, run from package_root with this interpreter, would change the harness's hook
    handlers or managed instruction block: True or False, or None when the harness was never wired (no handlers of ours
    and no managed block). Ownership and --adopt conflicts are not checked here; setup.py reports those itself."""
    from .instructions import BEGIN,INSTRUCTION_FILE
    from .repository import read_json
    home=harness_home(harness)
    path=home/setup.CONFIG[harness]
    try:config=read_json(path,{}) if path.is_file() else {}
    except ValueError:return True
    if not isinstance(config,dict):return True
    handlers=setup.managed_handlers(config,harness)
    try:text=(home/INSTRUCTION_FILE[harness]).read_text(encoding='utf-8-sig').replace('\r\n','\n')
    except (OSError,ValueError):text=''
    if not handlers and BEGIN not in text:return None
    hooked=bool(handlers) or harness!='factory'   # Factory without handlers of ours runs in instructions-only mode
    if setup.instruction_block(harness,hooked,package_root) not in text:return True
    return hooked and not _current(harness,config,setup,package_root)

def wiring():
    """{harness: check} for every harness that has our hook handlers. Never raises."""
    try:
        from integrations import setup
    except Exception as exc:
        return {'error':f'integrations/setup.py cannot be loaded ({type(exc).__name__}: {exc})'}
    found={}
    for harness in sorted(setup.CONFIG):
        try:result=check(harness,setup)
        except Exception as exc:result={'wired':None,'problems':[f'could not be checked ({type(exc).__name__}: {exc})']}
        if result:found[harness]=result
    return found

def broken(found):
    """True when a wired harness's hooks cannot run at all (a missing script or interpreter, an unloadable file, or a setting that switches them off)."""
    return any(isinstance(row,dict) and row.get('wired') and (row.get('script_exists') is False or row.get('interpreter_ok') is False or row.get('config_ok') is False or row.get('blocked')) for row in found.values())
