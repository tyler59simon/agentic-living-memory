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
        from .background import in_cooldown,session_gate,touch_cooldown
        config=setting(bindings)
        if config is None:return 'disabled'
        blocked=session_gate(subagent=subagent,plan=plan,compact=compact)
        if blocked:return blocked
        if not SCRIPT.is_file():return 'no_script'
        marker=Path(root)/'hooks'/MARKER
        if in_cooldown(marker,EVERY,json_key='at'):return 'cooldown'
        touch_cooldown(marker,json_key='at')
        _spawn(root,config)
        return 'spawned'
    except Exception as exc:
        from .logs import swallowed
        swallowed('digest_refresh.schedule',exc,root)
        return 'error'

def argv(root,config):
    return [sys.executable,str(SCRIPT),'--root',str(root),'--every-domain','--stale-only',
            '--model',config['model'],'--budget-usd',f"{config['daily_budget_usd']:.2f}"]

def _spawn(root,config):
    """Detached via core/background.py: output goes to the script's run logs in the store's logs/ folder."""
    from .background import spawn_detached
    spawn_detached(argv(root,config))
