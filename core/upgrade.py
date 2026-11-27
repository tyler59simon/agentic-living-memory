"""`alm upgrade`: update the installed copy from the git checkout it was installed from.

It pulls the checkout (fast-forward only, on the remote's default branch, with no uncommitted changes), then runs that
checkout's install.py, which installs the package as one journaled write (undo OPERATION reverses it). install.py runs in its
own process, with the Python that made the install: the checkout's new code must do the install, and install.py refuses to run
from inside the installed copy. The result relays the install's `next_steps` (the harnesses whose wiring the new copy changes)
and `wiring_current`."""
from __future__ import annotations
import json,sys
from pathlib import Path
from .common import default_branch,head_branch
from .installer_lifecycle import _git,installed_stamp

PULL_TIMEOUT=300
INSTALL_TIMEOUT=600
RESTART='Restart running harness sessions to load the new copy.'

def _run(argv,cwd,timeout):
    """(exit code, stdout, stderr) of a command. Git never prompts for credentials (it would wait until the timeout). Tests replace it."""
    import os,subprocess
    try:done=subprocess.run(argv,cwd=str(cwd),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout,env={**os.environ,'GIT_TERMINAL_PROMPT':'0'})
    except (OSError,subprocess.SubprocessError) as exc:return 1,'',str(exc)
    return done.returncode,done.stdout,done.stderr

def _last_json(stdout):
    for line in reversed(stdout.strip().splitlines()):
        try:return json.loads(line)
        except ValueError:continue
    return None

def _install(python,source,*args):
    """The JSON result of the checkout's install.py with args. Raises ValueError with install.py's error when it fails."""
    code,out,err=_run([python,str(Path(source)/'install.py'),*args],source,INSTALL_TIMEOUT)
    result=_last_json(out)
    if code or not isinstance(result,dict):
        lines=[line for line in err.strip().splitlines() if line.strip()]
        raise ValueError(f'{" ".join(["install.py",*args])} failed in {source}: '+(lines[-1] if lines else f'exit {code}'))
    return result

def _rewire(python,steps):
    """Run each setup.py --hooks step reported by install.next_steps(); returns (rewired_harnesses, errors)."""
    import install
    return install.wire_harnesses(steps,python=python)

def run(rewire=False):
    stamp=installed_stamp()
    if not stamp or not stamp.get('source') or not stamp.get('commit'):
        raise ValueError('upgrade works from an installed copy made from a git checkout; this copy records none. Run install.py from your checkout instead.')
    source=stamp['source']
    old=stamp['commit']
    if not Path(source).is_dir():raise ValueError(f'The checkout this copy was installed from ({source}) is gone; clone it again and run install.py from it.')
    status=_git(source,'status','--porcelain')
    if status is None:raise ValueError(f'{source} is not a git checkout git can read')
    if status:raise ValueError(f'{source} has uncommitted changes; commit or stash them first')
    branch=head_branch(source)
    wanted=default_branch(source)
    if branch!=wanted:raise ValueError(f'{source} is on {branch or "a detached HEAD"}; upgrade installs only from {wanted}. Switch to {wanted} first.')
    code,out,err=_run(['git','-C',source,'pull','--ff-only'],source,PULL_TIMEOUT)
    if code:raise ValueError(f'git pull --ff-only failed in {source}: '+((err or out).strip().splitlines() or [f'exit {code}'])[-1])
    new=_git(source,'rev-parse','HEAD')
    if not new:raise ValueError(f'Cannot read HEAD in {source} after the pull')
    python=stamp.get('interpreter') if stamp.get('interpreter') and Path(stamp['interpreter']).is_file() else sys.executable
    result={'ok':True,'source':source,'branch':branch,'from':old[:12],'to':new[:12]}
    if new==old:
        import install   # the installed copy's own install.py: its next_steps() checks wiring against this copy
        steps,current=install.next_steps()
        result['next_steps'],result['wiring_current']=steps,current
        if rewire and steps:
            wired,errors=_rewire(python,steps)
            result['rewired']=wired
            if errors:result['warnings']=['rewire failed: '+e for e in errors]
            else:
                result['next_steps']=[]
                result['wiring_current']=sorted(set(current)|set(wired))
        return {**result,'status':'current','commits':0}
    count=_git(source,'rev-list','--count',f'{old}..{new}')
    result['commits']=int(count) if count and count.isdigit() else None
    receipt=_install(python,source)
    summary=receipt.get('summary') or {}
    steps=summary.get('next_steps') or []
    current=summary.get('wiring_current') or []
    result.update(files=summary.get('files'),next_steps=steps,wiring_current=current,
                  operation_id=receipt.get('operation_id'),undo=receipt.get('undo'))
    for key in ('warnings','notice'):
        if receipt.get(key):result[key]=receipt[key]
    if rewire and steps:
        wired,errors=_rewire(python,steps)
        result['rewired']=wired
        if errors:result.setdefault('warnings',[]).extend('rewire failed: '+e for e in errors)
        else:
            result['next_steps']=[]
            result['wiring_current']=sorted(set(current)|set(wired))
    return {**result,'status':'upgraded','restart':RESTART}
