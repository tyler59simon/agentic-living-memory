"""PreToolUse fast path for Read, Glob and Grep: answer without loading the runtime when the session state says
every repo the call reads needs nothing more (its map hint was given, or it already counts as explored).

Read calls are the most frequent hook, so this imports only the standard library and never writes or logs.
Anything uncertain returns None and the full hook runs. It mirrors runtime._pointer, common.git_root and
runtime._target_repos; tests/test_maps.py checks that they agree."""
import hashlib,json,os
from pathlib import Path

FAST_TOOLS={'read','glob','grep'}
PATH_KEYS=('file_path','path','notebook_path','filepath')

def _root():
    for name in ('LIVING_MEMORY_ROOT','LIVING_MEMORY_HOME'):
        if os.environ.get(name):return Path(os.environ[name]).expanduser().resolve()
    config=json.loads((Path.home()/'.config'/'agentic-living-memory'/'config.json').read_text(encoding='utf-8'))
    return Path(os.path.expandvars(config['data_root'])).expanduser().resolve()

def _git_root(path):
    here=Path(path).resolve()
    return next((p for p in (here,*here.parents) if (p/'.git').exists()),None)

def quick(payload):
    """{} when the full hook would add nothing for this read call, else None."""
    try:
        name=str(payload.get('tool_name') or payload.get('toolName') or '').lower()
        session=str(payload.get('session_id') or payload.get('sessionId') or '')
        if name not in FAST_TOOLS or not session:return None
        root=_root()
        if (root/'.relocated.json').exists():return None
        state=json.loads((root/'.turns'/(hashlib.sha256(session.encode()).hexdigest()+'.current.json')).read_text(encoding='utf-8'))
        repos=state.get('repos') if isinstance(state,dict) else None
        if not isinstance(repos,dict):return None
        cwd=payload.get('cwd') or payload.get('workingDirectory') or os.getcwd()
        args=payload.get('tool_input')
        args=args if isinstance(args,dict) else {}
        found=[]
        for key in PATH_KEYS:
            value=args.get(key)
            if isinstance(value,str) and value.strip():
                path=Path(os.path.expandvars(value.strip())).expanduser()
                found.append(_git_root(path if path.is_absolute() else Path(cwd)/path))
        if not found:found.append(_git_root(cwd))
        # A store that has had private notebooks (NS-2A): a read inside the store, or a search above it, goes to the full hook, which checks them.
        if (root/'notebook-privacy.json').exists():
            for key in PATH_KEYS+('pattern',):
                value=args.get(key)
                if key=='pattern':value=value.split('*')[0].split('?')[0].split('[')[0].split('{')[0] if name=='glob' and isinstance(value,str) else None
                if not isinstance(value,str) or not value.strip():continue
                path=Path(os.path.expandvars(value.strip())).expanduser()
                path=(path if path.is_absolute() else Path(cwd)/path).resolve()
                if path.is_relative_to(root) or (name!='read' and root.is_relative_to(path)):return None
            here=Path(cwd).resolve()
            if name!='read' and not any(args.get(k) for k in PATH_KEYS) and (here.is_relative_to(root) or root.is_relative_to(here)):return None
        agent=str(payload.get('agent_id') or payload.get('agentId') or '')
        for repo in dict.fromkeys(r for r in found if r):
            done=(repos.get(repo.as_posix()) or {}).get('done') or []
            if '*' not in done and agent not in done:return None
        return {}
    except Exception:  # missing state, an unreadable config, a bad path: the full hook decides
        return None
