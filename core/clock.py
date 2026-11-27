"""The record clock. Headless runs only (`claude -p`, the SDK, LIVING_MEMORY_HEADLESS=1) may shift it with
LIVING_MEMORY_CLOCK_OFFSET_SECONDS (a float, negative allowed), so a simulation can age notes without waiting.
Everything that stamps or compares a record time reads now()/today(); duration timers and lock ages stay on real time."""
from __future__ import annotations
import math,os
from datetime import datetime,timedelta,timezone

OFFSET_ENV='LIVING_MEMORY_CLOCK_OFFSET_SECONDS'
_reported=set()

def headless_env():
    """True in a non-interactive run, where the caller receives only the final message."""
    env=os.environ
    return env.get('LIVING_MEMORY_HEADLESS')=='1' or env.get('CLAUDE_CODE_SESSION_ATTENDED')=='0' or env.get('CLAUDE_CODE_ENTRYPOINT','').startswith('sdk')

def offset_seconds():
    """The offset in force: 0.0 unless the run is headless and the variable holds a finite number."""
    raw=os.environ.get(OFFSET_ENV)
    if raw is None or not raw.strip() or not headless_env():return 0.0
    try:
        value=float(raw)
        if not math.isfinite(value):raise ValueError('not finite')
        timedelta(seconds=value)
        return value
    except (ValueError,OverflowError) as exc:
        if raw not in _reported:
            _reported.add(raw)
            try:
                from .logs import swallowed
                swallowed('clock.offset',exc)
            except Exception:pass
        return 0.0

def simulated():
    return offset_seconds()!=0.0

def now():
    """Timezone-aware UTC datetime, shifted by the offset."""
    moment=datetime.now(timezone.utc)
    offset=offset_seconds()
    if not offset:return moment
    try:return moment+timedelta(seconds=offset)
    except OverflowError:return moment

def today():
    """The local calendar date of now(), as date.today() gave it."""
    return now().astimezone().date()
