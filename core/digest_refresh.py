"""The daily digest refresh: SessionStart starts `scripts/ingest_run.py --every-domain --stale-only` detached, at most once a day, so digests
whose files changed are refreshed by a headless agent (confirmed, updated in place or replaced) instead of piling up in the review queue (USE-1, 2026-11-27).

Off by default. It spends money, so the user turns it on with a daily budget:
    alm config set digest_refresh '{"daily_budget_usd": "1.00", "model": "haiku"}'
and off with `alm config set digest_refresh null`. The script stops starting batches once the agents' reported cost reaches the budget, so a
day can go over by up to one batch. Only digests already in the store are refreshed; files never digested are left to a manual ingest run."""
from __future__ import annotations
import json,os,subprocess,sys,tempfile,time
from pathlib import Path

EVERY=86400   # seconds between scheduled refreshes
SCRIPT=Path(__file__).resolve().parents[1]/'scripts'/'ingest_run.py'
MARKER='last-digest-refresh.json'

def setting(bindings):
    """{daily_budget_usd: float, model: str} when the refresh is on, else None."""
    value=bindings.get('digest_refresh')
    if not isinstance(value,dict):return None
    try:budget=float(value.get('daily_budget_usd'))
    except (TypeError,ValueError):return None
    return {'daily_budget_usd':budget,'model':str(value.get('model') or 'haiku')} if budget>0 else None

def schedule(root,bindings,*,subagent=False,plan=False,compact=False):
    """The SessionStart check: start the refresh when it is on and the last one started EVERY seconds ago or more. Returns 'spawned' or why
    not; never raises, never waits. Headless sessions (the refresh's own agents among them) never start one."""
    try:
        from .runtime import headless
        config=setting(bindings)
        if config is None:return 'disabled'
        if subagent:return 'subagent'
        if headless():return 'headless'
        if plan:return 'plan_mode'   # the user asked for no changes
        if compact:return 'compact'
        if os.environ.get('LIVING_MEMORY_NO_BACKGROUND'):return 'background_disabled'
        if os.environ.get('LIVING_MEMORY_MAINTENANCE_CHILD'):return 'maintenance_child'
        if not SCRIPT.is_file():return 'no_script'
        marker=Path(root)/'hooks'/MARKER
        try:
            if time.time()-float(json.loads(marker.read_text(encoding='utf-8'))['at'])<EVERY:return 'cooldown'
        except (OSError,ValueError,KeyError,TypeError):pass
        from .common import atomic_write
        atomic_write(marker,json.dumps({'at':time.time()}))
        _spawn(root,config)
        return 'spawned'
    except Exception as exc:
        from .logs import swallowed
        swallowed('digest_refresh.schedule',exc,root);return 'error'

def argv(root,config):
    return [sys.executable,str(SCRIPT),'--root',str(root),'--every-domain','--stale-only',
            '--model',config['model'],'--budget-usd',f"{config['daily_budget_usd']:.2f}"]

def _spawn(root,config):
    """Detached, as rebaseline._spawn: output goes to the script's run logs in the store's logs/ folder."""
    options={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL,'close_fds':True,'cwd':tempfile.gettempdir()}
    if os.name=='nt':
        flags=0x00000008|0x00000200
        try:subprocess.Popen(argv(root,config),creationflags=flags|0x01000000,**options)
        except OSError:subprocess.Popen(argv(root,config),creationflags=flags,**options)
    else:subprocess.Popen(argv(root,config),start_new_session=True,**options)
