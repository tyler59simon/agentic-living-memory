"""Install Agentic Living Memory directly into the user's Codex setup."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from core.installer_lifecycle import apply_plan as apply_lifecycle_plan
from core.installer_lifecycle import build_plan as build_lifecycle_plan

SOURCE = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
INSTALL_DIR = CODEX_HOME / "agentic-living-memory"
HOOKS_FILE = CODEX_HOME / "hooks.json"


def hook_command(script: Path, action: str) -> dict:
    script_arg = f'"{script}"'
    if os.name == "nt":
        # A quoted executable alone is a string expression in PowerShell.
        # The standard Windows launcher has no spaces and works in both shells.
        launcher = shutil.which("py")
        if launcher and not any(c.isspace() for c in launcher):
            windows_command = f'{launcher} -{sys.version_info.major}.{sys.version_info.minor} -X utf8 {script_arg} {action}'
        else:
            # Explicit PowerShell invocation also handles Python paths with spaces.
            import base64
            expression = '& "' + str(sys.executable).replace('"', '`"') + '" -X utf8 ' + script_arg + ' ' + action
            encoded = base64.b64encode(expression.encode('utf-16-le')).decode('ascii')
            windows_command = 'powershell.exe -NoProfile -NonInteractive -EncodedCommand ' + encoded
        return {
            "type": "command",
            "command": f'python3 {script_arg} {action}',
            "commandWindows": windows_command,
        }
    return {"type": "command", "command": f'"{sys.executable}" {script_arg} {action}'}


def hook_groups(script: Path) -> dict:
    def handler(action: str, status: str, limit: int | None = None) -> dict:
        item = hook_command(script, action)
        item["statusMessage"] = status
        item["timeout"] = 30
        if limit is not None:
            item["additionalContextLimit"] = limit
        return item

    return {
        "SessionStart": [{
            "matcher": "startup|resume|clear|compact|fork",
            "hooks": [handler("session-start", "Loading project memory", 4000)],
        }],
        "UserPromptSubmit": [{
            "hooks": [handler("user-prompt", "Searching project memory", 4000)],
        }],
        "SubagentStart": [{
            "hooks": [handler("subagent-start", "Applying memory access rules", 1000)],
        }],
        "PreToolUse": [{
            "hooks": [handler("pre-tool-use", "Checking protected memory paths")],
        }],
        "Stop": [{"hooks": [handler("stop", "Checking durable capture completion")]}],
    }


def is_our_handler(handler: dict) -> bool:
    command = str(handler.get("command", "")) + " " + str(handler.get("commandWindows", ""))
    return "agentic-living-memory" in command and "codex_hooks.py" in command


def merge_hooks(existing: dict, managed: dict) -> dict:
    result = dict(existing)
    all_hooks = dict(result.get("hooks", {}))
    for event, new_groups in managed.items():
        groups = []
        for group in all_hooks.get(event, []):
            if not isinstance(group, dict):
                groups.append(group)
                continue
            kept = [item for item in group.get("hooks", []) if not (isinstance(item, dict) and is_our_handler(item))]
            if kept:
                updated = dict(group)
                updated["hooks"] = kept
                groups.append(updated)
        groups.extend(new_groups)
        all_hooks[event] = groups
    result["hooks"] = all_hooks
    result.setdefault("description", "Codex user-level lifecycle hooks.")
    return result


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def install_hooks() -> None:
    if HOOKS_FILE.exists():
        try:
            current = json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot safely merge existing Codex hooks file: {HOOKS_FILE}: {exc}") from exc
        if not isinstance(current, dict) or not isinstance(current.get("hooks", {}), dict):
            raise RuntimeError(f"Unexpected Codex hooks format in {HOOKS_FILE}; file left untouched")
    else:
        current = {}
    atomic_json(HOOKS_FILE, merge_hooks(current, hook_groups(INSTALL_DIR / "hooks" / "codex_hooks.py")))


def main() -> int:
    import argparse
    from core.governance import apply
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--location',type=Path,help='Shared memory folder for unattended first-time setup')
    parser.add_argument('--plan',action='store_true');parser.add_argument('--apply-plan');parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--uninstall-plan',action='store_true');args=parser.parse_args()
    if not args.uninstall_plan and not args.apply_plan:
        from core.storage import ensure_setup
        ensure_setup(args.location)
    CODEX_HOME.mkdir(parents=True,exist_ok=True)
    if args.apply_plan:
        if not args.expected_plan_sha256:parser.error('--expected-plan-sha256 is required')
        result=apply(CODEX_HOME,args.apply_plan,args.expected_plan_sha256)
    else:
        proposed=plan_install(args.uninstall_plan)
        if args.plan or args.uninstall_plan:result=proposed
        else:result=apply(CODEX_HOME,proposed['plan_id'],proposed['digest'])
    print(json.dumps(result,separators=(',',':')))
    return 0


def plan_install(uninstall=False):
    from core.repository import store_lock
    with store_lock(CODEX_HOME):return _plan_install(uninstall)


def _plan_install(uninstall=False):
    """Package and owned hook changes form a single durable transaction."""
    from core import installer_lifecycle as lifecycle
    from core.repository import read_json, canonical_json, now
    from core.governance import plan
    import uuid
    proposal=build_lifecycle_plan(INSTALL_DIR,SOURCE,uninstall)
    if proposal['blockers']:
        raise RuntimeError('Existing files preserved; ownership conflicts: '+json.dumps(proposal['blockers']))
    changes={};entries={}
    for action in proposal['actions']:
        rel=action['path'];dest=(Path(INSTALL_DIR.name)/rel).as_posix()
        if action['action'] in {'create','replace'}:changes[dest]=lifecycle._read_source(SOURCE,rel,action['sourceSha256']).decode('utf-8')
        elif action['action']=='remove':changes[dest]=None
        if not uninstall and action['action'] in {'create','replace','keep','adopt'}:
            entry=proposal['sourceInventory'][rel]
            entries[rel]={'state':'owned','sourceSha256':entry['sha256'],'currentSha256':entry['sha256'],
                          'mode':action['expected'].get('mode') or (0o666 if os.name=='nt' else 0o600),'operationId':proposal['operationId']}
    ownership_rel=INSTALL_DIR.name+'/.core-ownership-v1.json'
    if uninstall:changes[ownership_rel]=None
    else:
        record={'schema':lifecycle.OWNERSHIP_SCHEMA,'installationId':(proposal['ownership'] or {}).get('installationId',uuid.uuid4().hex),
                'installedRelease':lifecycle.CORE_RELEASE,'source':str(SOURCE),'entries':entries,'updatedAt':now()}
        record['recordDigest']=lifecycle._digest(record);changes[ownership_rel]=canonical_json(record)+'\n'
    current=read_json(HOOKS_FILE,{})
    if not isinstance(current,dict) or not isinstance(current.get('hooks',{}),dict):raise ValueError('Existing hooks configuration preserved: invalid format')
    managed=hook_groups(INSTALL_DIR/'hooks/codex_hooks.py')
    if uninstall:
        cleaned=dict(current);events=dict(cleaned.get('hooks',{}))
        for event in managed:
            groups=[]
            for group in events.get(event,[]):
                kept=[h for h in group.get('hooks',[]) if not is_our_handler(h)]
                if kept:groups.append({**group,'hooks':kept})
            if groups:events[event]=groups
            else:events.pop(event,None)
        cleaned['hooks']=events;updated=cleaned
    else:updated=merge_hooks(current,managed)
    changes['hooks.json']=canonical_json(updated)+'\n'
    changes.update(_instruction_changes(uninstall))
    return plan(CODEX_HOME,changes,'codex_uninstall' if uninstall else 'codex_install',summary={'files':len(changes),'hooks':list(managed),'interpreter':sys.executable})


def _instruction_changes(uninstall=False):
    """Managed Living Memory block in Codex's global AGENTS.md, owned by hash like the other harnesses."""
    from core.instructions import instruction_block, merge_block
    from core.repository import read_json, canonical_json, digest
    agents=CODEX_HOME/'AGENTS.md';ownership=CODEX_HOME/'.living-memory-ownership.json'
    state=read_json(ownership,{}) or {}
    original=agents.read_text(encoding='utf-8') if agents.exists() else ''
    block=instruction_block('codex')
    text=merge_block(original,block,state.get('instruction_hash'),digest,uninstall)
    changes={}
    if text!=original:changes['AGENTS.md']=text
    if uninstall:
        if ownership.exists():changes['.living-memory-ownership.json']=None
    else:changes['.living-memory-ownership.json']=canonical_json({'schema':2,'instruction_hash':digest(block.encode())})
    return changes


if __name__ == "__main__":
    raise SystemExit(main())
