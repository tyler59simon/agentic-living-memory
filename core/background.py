"""Shared detached background worker spawning and SessionStart cooldown guards (REL-D2)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .common import atomic_write

_WIN_DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
_WIN_BREAKAWAY = 0x01000000               # CREATE_BREAKAWAY_FROM_JOB


def spawn_detached(argv, env=None):
    """Launch argv detached from this session's process group and job object, with DEVNULL stdio and cwd=tempdir."""
    options = {
        'stdin': subprocess.DEVNULL,
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
        'close_fds': True,
        'cwd': tempfile.gettempdir(),
    }
    if env is not None:
        options['env'] = env
    if os.name == 'nt':
        try:
            subprocess.Popen(argv, creationflags=_WIN_DETACHED | _WIN_BREAKAWAY, **options)
        except OSError:
            subprocess.Popen(argv, creationflags=_WIN_DETACHED, **options)
    else:
        subprocess.Popen(argv, start_new_session=True, **options)


def session_gate(*, subagent=False, plan=False, compact=False):
    """Common SessionStart background-worker guard. Returns a skip reason string, or None when allowed."""
    from .runtime import headless
    if subagent:
        return 'subagent'
    if headless():
        return 'headless'
    if plan:
        return 'plan_mode'
    if compact:
        return 'compact'
    if os.environ.get('LIVING_MEMORY_NO_BACKGROUND'):
        return 'background_disabled'
    if os.environ.get('LIVING_MEMORY_MAINTENANCE_CHILD'):
        return 'maintenance_child'
    return None


def in_cooldown(marker, seconds, *, json_key=None, now=None):
    """True when marker was written less than `seconds` ago. Tolerates missing or malformed markers."""
    ts = time.time() if now is None else now
    try:
        raw = Path(marker).read_text(encoding='utf-8')
        stamp = float(json.loads(raw)[json_key]) if json_key else float(raw)
        return (ts - stamp) < seconds
    except (OSError, ValueError, KeyError, TypeError):
        return False


def touch_cooldown(marker, *, json_key=None, now=None):
    """Record the current timestamp in `marker` (disposable cache: fsync=False)."""
    ts = time.time() if now is None else now
    body = json.dumps({json_key: ts}) if json_key else str(ts)
    atomic_write(Path(marker), body, fsync=False)
