"""Automatic note maintenance: configuration, allowance accounting, run records and recovery (no model work here).

`maintenance.json` is the configuration. Each run is one record under `maintenance/runs/YYYY-MM-DD/`, and
`maintenance/state.json` is a checkpoint of the day's accounting. The checkpoint is never trusted alone: the
allowance in force is the element-wise maximum of it and the figures summed from the run records, so deleting
the checkpoint (or `.cache`) never creates a new allowance. Money is integer microdollars; records hold metadata only."""
from __future__ import annotations
import hashlib
import re
import uuid
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from .common import file_lock, global_domains, safe_domain
from .repository import canonical_json, digest, read_json, store_lock, transaction

CONFIG = 'maintenance.json'
STATE = 'maintenance/state.json'
RUNS = 'maintenance/runs'
POLICY_VERSION = 1
RETRY_DAYS = 7
MICRO = 1_000_000
DEFAULTS = {'version': 1, 'enabled': False, 'domains': [], 'policy_version': POLICY_VERSION, 'model': 'sonnet', 'max_notes_per_day': 10,
            'timeout_seconds': 300, 'daily_budget_usd': '1.00', 'min_interval_seconds': 86400, 'enabled_at': None}
STATES = ('reserved', 'running', 'applying', 'completed', 'dry_run', 'failed', 'cancelled', 'interrupted')
TRANSITIONS = {'reserved': {'running', 'failed', 'cancelled', 'interrupted'},
               'running': {'applying', 'completed', 'dry_run', 'failed', 'cancelled', 'interrupted'},
               'applying': {'completed', 'failed', 'cancelled', 'interrupted'}}
FINISH_STATES = {'completed', 'dry_run', 'failed', 'cancelled'}
COUNTERS = ('reviewed', 'reserved_notes', 'charged_micro', 'reserved_micro')


class MaintenanceSkip(Exception):
    """A launch is refused for a reason code (see eligibility); not an error."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Unavailable(Exception):
    """Run records or the checkpoint cannot be read, so the allowance is unknown."""


# ---- time and money

def _aware(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    return now.astimezone(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds')


def _parse(value):
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def parse_usd(value, name='daily_budget_usd'):
    """Decimal string such as "1.00" -> microdollars. Only plain positive decimals (at most 6 places) are accepted."""
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,9}(\.\d{1,6})?', value) or Decimal(value) <= 0:
        raise ValueError(f'Invalid {name}: a positive decimal string such as "1.00"')
    return int(Decimal(value) * MICRO)


def format_usd(micro):
    whole, frac = divmod(int(micro), MICRO)
    return f'{whole}.{f"{frac:06d}".rstrip("0").ljust(2, "0")}'


def micro_from_provider(value):
    """A provider's float cost -> microdollars (rounded up), or None when missing or not a finite non-negative number."""
    try:
        if isinstance(value, bool) or value is None:
            return None
        cost = Decimal(str(value))
        return int((cost * MICRO).to_integral_value(rounding=ROUND_CEILING)) if cost.is_finite() and cost >= 0 else None
    except ArithmeticError:
        return None


# ---- configuration

def _int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f'Invalid {name}: a positive integer')
    return value


def validate(cfg, root=None, allow_global=False):
    """Normalised copy of a configuration. With `root`, each domain must exist and not be global unless allow_global."""
    if not isinstance(cfg, dict):
        raise ValueError('Invalid maintenance configuration')
    unknown = sorted(set(cfg) - set(DEFAULTS))
    if unknown:
        raise ValueError('Unknown maintenance configuration key: ' + ', '.join(map(str, unknown)))
    c = {**DEFAULTS, **cfg}
    if c['version'] != 1 or type(c['version']) is not int:
        raise ValueError('Unsupported maintenance configuration version')
    if type(c['enabled']) is not bool:
        raise ValueError('Invalid enabled: true or false')
    for key in ('policy_version', 'max_notes_per_day', 'timeout_seconds', 'min_interval_seconds'):
        _int(c[key], key)
    parse_usd(c['daily_budget_usd'])
    if not isinstance(c['model'], str) or not re.fullmatch(r'[a-z0-9.-]{1,40}', c['model']):
        raise ValueError('Invalid model: a lowercase alias such as "sonnet"')
    if not isinstance(c['domains'], list) or len(set(map(str, c['domains']))) != len(c['domains']):
        raise ValueError('Invalid domains: a list of distinct domain names')
    for d in c['domains']:
        try:
            safe_domain(d)  # plain names only, so './_global' cannot slip past the global-domain check below
        except ValueError:
            raise ValueError(f'Invalid domain name: {d!r}') from None
    if c['enabled_at'] is not None:
        try:
            _parse(c['enabled_at'])
        except (TypeError, ValueError):
            raise ValueError('Invalid enabled_at: an ISO timestamp or null') from None
    if root is not None:
        globals_ = global_domains(root)
        for d in c['domains']:
            if not (Path(root) / 'domains' / d).is_dir():
                raise ValueError(f'Unknown domain: {d}')
            if d in globals_ and not allow_global:
                raise ValueError(f'{d} is a global domain; name it explicitly to include it')
    return c


def config(root):
    """The configuration (disabled defaults when the file is missing); a malformed file raises ValueError."""
    value = read_json(Path(root) / CONFIG, None)
    return validate(DEFAULTS if value is None else value)


def config_revision(cfg):
    return digest(canonical_json(cfg).encode())


def set_config(root, cfg, expected_revision=None, allow_global=False):
    cfg = validate(cfg, root, allow_global)
    with store_lock(root):
        path = Path(root) / CONFIG
        before = path.read_bytes() if path.exists() else None
        if expected_revision is not None and config_revision(config(root)) != expected_revision:
            raise ValueError(f'Stale revision: {CONFIG}')
        result = transaction(root, {CONFIG: canonical_json(cfg)}, 'maintenance_config', expected={CONFIG: digest(before)})
    return dict(result, config=cfg, revision=config_revision(cfg))


# ---- records and accounting

def _blank(day):
    return {'version': 1, 'day': day, 'reviewed': 0, 'reserved_notes': 0, 'charged_micro': 0, 'reserved_micro': 0, 'cost_complete': True,
            'last_attempt_at': None, 'last_attempt_config_revision': None, 'last_failure': None, 'blocked': None, 'last_run': None,
            'domain_cursor': None, 'retry': {}}


def _nonneg(value):
    return type(value) is int and value >= 0


def _check_run(run, day=None):
    ok = (isinstance(run, dict) and isinstance(run.get('id'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run['id'])
          and run.get('state') in STATES and all(_nonneg(run.get(k)) for k in ('reserved_notes', 'reserved_micro', 'charged_micro', 'budget_passed_micro'))
          and isinstance(run.get('attempted_at'), str) and isinstance(run.get('day'), str) and (day is None or run['day'] == day)
          and isinstance(run.get('decisions'), dict) and isinstance(run.get('selected'), list))
    if not ok:
        raise ValueError('Invalid maintenance run record')
    _parse(run['attempted_at'])
    return run


def _read_run(path, day=None):
    try:
        return _check_run(read_json(path), day)
    except (ValueError, TypeError) as exc:
        raise Unavailable(f'unreadable run record: {path.name}') from exc


def _run_path(run):
    return f'{RUNS}/{run["day"]}/{run["id"]}.json'


def _checkpoint(root):
    """The checkpoint, or None when it is missing or malformed (it is then rebuilt from the run records)."""
    try:
        value = read_json(Path(root) / STATE)
        if not isinstance(value, dict) or not isinstance(value.get('day'), str) or not isinstance(value.get('retry'), dict) or not isinstance(value.get('cost_complete'), bool):
            return None
        date.fromisoformat(value['day'])
        if not all(_nonneg(value.get(k)) for k in COUNTERS):
            return None
        if value.get('last_attempt_at') is not None:
            _parse(value['last_attempt_at'])
        return {**_blank(value['day']), **value}
    except (ValueError, TypeError):
        return None


def _scan(root, since):
    """Run records of every day directory on or after `since`; any damage raises Unavailable."""
    base = Path(root) / RUNS
    if not base.exists():
        return []
    runs = []
    try:
        for folder in sorted(base.iterdir()):
            try:
                day = date.fromisoformat(folder.name)
            except ValueError:
                continue
            if day >= since:
                if not folder.is_dir():
                    raise Unavailable('damaged runs directory')
                runs += [_read_run(p, folder.name) for p in sorted(folder.glob('*.json'))]
    except OSError as exc:
        raise Unavailable('damaged runs directory') from exc
    return runs


def _contrib(run):
    n, m, s = run['reserved_notes'], run['reserved_micro'], run['state']
    if run.get('released'):
        return {}
    if s == 'reserved':
        return {'reserved_notes': n, 'reserved_micro': m}
    if s in ('running', 'applying'):
        return {'reviewed': n, 'reserved_micro': m}
    return {'reviewed': n, 'charged_micro': run['charged_micro']}


def accounting(root, now=None):
    """(allowance state in force, effective now). Raises Unavailable when the run records cannot be trusted."""
    now = _aware(now)
    cp = _checkpoint(root)
    eff = now
    if cp and cp['last_attempt_at']:
        eff = max(eff, _parse(cp['last_attempt_at']))
    # Without a checkpoint, the interval and a provider block may rest on runs of any age, so read every record.
    runs = _scan(root, eff.date() - timedelta(days=1) if cp else date.min)
    latest = max(runs, key=lambda r: _parse(r['attempted_at']), default=None)
    if latest and _parse(latest['attempted_at']) > eff:
        eff = _parse(latest['attempted_at'])
    day = eff.date().isoformat()
    base = cp if cp and cp['day'] == day else _blank(day)
    out = {**(cp or _blank(day)), **{k: base[k] for k in COUNTERS}, 'day': day, 'cost_complete': base['cost_complete']}
    sums = {k: 0 for k in COUNTERS}
    for run in (r for r in runs if r['day'] == day):
        for k, v in _contrib(run).items():
            sums[k] += v
        if run['state'] in ('interrupted', 'completed', 'failed', 'dry_run', 'cancelled') and run.get('cost_complete') is False:
            out['cost_complete'] = False
    for k in COUNTERS:
        out[k] = max(out[k], sums[k])
    if latest and (not cp or not cp['last_attempt_at'] or _parse(latest['attempted_at']) > _parse(cp['last_attempt_at'])):
        out['last_attempt_at'], out['last_attempt_config_revision'], out['last_run'] = latest['attempted_at'], latest['config_revision'], latest['id']
        # Only a failure before launch (released) may be retried early after a config change.
        out['last_failure'] = ({'at': latest['finished_at'], 'reason_code': latest['reason_code'], 'run_id': latest['id']}
                               if latest.get('released') else None)
    if not out['blocked']:
        over = [r for r in runs if r.get('reason_code') == 'provider_budget']
        if over:
            out['blocked'] = {'reason': 'provider_budget', 'config_revision': over[-1]['config_revision']}
    return out, eff


def state(root, now=None):
    return accounting(root, now)[0]


def _midnight(eff):
    return datetime(eff.year, eff.month, eff.day, tzinfo=timezone.utc) + timedelta(days=1)


def eligibility(root, now=None, cfg=None):
    """{eligible, skip_reason, next_eligible_at}; the reason is one of the spec's codes."""
    def no(reason, when=None):
        return {'eligible': False, 'skip_reason': reason, 'next_eligible_at': _iso(when) if when else None}
    try:
        cfg = validate(cfg) if cfg is not None else config(root)
    except ValueError:
        return no('disabled')
    if not cfg['enabled']:
        return no('disabled')
    if cfg['policy_version'] != POLICY_VERSION:
        return no('policy_unknown')
    try:
        acc, eff = accounting(root, now)
    except Unavailable:
        return no('accounting_unavailable')
    revision = config_revision(cfg)
    if acc['blocked'] and acc['blocked'].get('config_revision') == revision:
        return no('provider_budget')
    if acc['last_attempt_at']:
        due = _parse(acc['last_attempt_at']) + timedelta(seconds=cfg['min_interval_seconds'])
        # Early retry only when the latest attempt itself failed before launch; an older run released later does not count.
        corrected = (acc['last_failure'] and acc['last_failure'].get('run_id') == acc['last_run']
                     and acc['last_attempt_config_revision'] != revision)
        if due > eff and not corrected:
            return no('interval', due)
    if acc['reviewed'] + acc['reserved_notes'] >= cfg['max_notes_per_day']:
        return no('daily_note_limit', _midnight(eff))
    if acc['charged_micro'] + acc['reserved_micro'] >= parse_usd(cfg['daily_budget_usd']):
        return no('daily_budget', _midnight(eff))
    return {'eligible': True, 'skip_reason': None, 'next_eligible_at': None}


# ---- transitions

def _file_digest(root, rel):
    path = Path(root) / rel
    return digest(path.read_bytes()) if path.exists() else None


def _commit(root, run, acc, expected_run, expected_state, op=None):
    changes = {_run_path(run): canonical_json(run), STATE: canonical_json(acc)}
    expected = {_run_path(run): expected_run, STATE: expected_state}
    return transaction(root, changes, 'maintenance_run', expected=expected, operation_id=op)


def reserve(root, cfg, n_candidates, now=None, selected=None):
    """Durably reserve notes and money for one launch, or raise MaintenanceSkip(reason). Re-checks under the store lock."""
    with store_lock(root):
        cfg = validate(cfg)
        revision = config_revision(cfg)
        if revision != config_revision(config(root)):
            raise MaintenanceSkip('config_changed')
        verdict = eligibility(root, now, cfg)
        if not verdict['eligible']:
            raise MaintenanceSkip(verdict['skip_reason'])
        acc, eff = accounting(root, now)
        notes = min(int(n_candidates), cfg['max_notes_per_day'] - acc['reviewed'] - acc['reserved_notes'])
        if notes <= 0:
            raise MaintenanceSkip('no_candidates')
        micro = parse_usd(cfg['daily_budget_usd']) - acc['charged_micro'] - acc['reserved_micro']
        day = eff.date().isoformat()
        run = {'id': f'{eff.strftime("%Y%m%dT%H%M%S")}-{uuid.uuid4().hex[:8]}', 'day': day, 'config_revision': revision, 'model': cfg['model'],
               'reserved_notes': notes, 'reserved_micro': micro, 'budget_passed_micro': micro, 'selected': list((selected or [])[:notes]),
               'state': 'reserved', 'charged_micro': 0, 'cost_complete': True, 'attempted_at': _iso(eff), 'launched_at': None, 'finished_at': None,
               'reason_code': None, 'released': False, 'decisions': {}}
        before = _file_digest(root, STATE)
        acc.update(reserved_notes=acc['reserved_notes'] + notes, reserved_micro=acc['reserved_micro'] + micro, last_attempt_at=run['attempted_at'],
                   last_attempt_config_revision=revision, last_run=run['id'], last_failure=None)
        _commit(root, run, acc, None, before)
        return run


def _current(root, run):
    """The stored record of `run` (callers may hold an older copy)."""
    return _read_run(Path(root) / _run_path(run), run['day'])


def _move(root, run, to, now, apply):
    """One transition: check it, let `apply(run, acc)` edit the record and the day's accounting, commit both."""
    with store_lock(root):
        stored = _current(root, run)
        if to not in TRANSITIONS.get(stored['state'], ()):
            raise ValueError(f'Invalid maintenance transition: {stored["state"]} -> {to}')
        acc, eff = accounting(root, now)
        same_day = acc['day'] == stored['day']
        before_state, before_run = _file_digest(root, STATE), _file_digest(root, _run_path(stored))
        new = dict(stored, state=to)
        # A run from an earlier day leaves today's counters alone, but its block and failure still reach the checkpoint.
        target = acc if same_day else {**acc, **{k: 0 for k in COUNTERS}, 'cost_complete': True}
        apply(new, target, _iso(eff))
        if not same_day:
            acc['blocked'], acc['last_failure'] = target['blocked'], target['last_failure']
        _commit(root, new, acc, before_run, before_state)
        return new


def _free(run, acc, notes=True):
    acc['reserved_micro'] = max(0, acc['reserved_micro'] - run['reserved_micro'])
    if notes:
        acc['reserved_notes'] = max(0, acc['reserved_notes'] - run['reserved_notes'])


def _failed(acc, run, at, reason):
    # Only the latest attempt's failure is kept: releasing an older reservation must not replace or invent it.
    if acc['last_run'] in (None, run['id']):
        acc['last_failure'] = {'at': at, 'reason_code': reason, 'run_id': run['id']}


def launch(root, run, now=None):
    def apply(new, acc, at):
        acc['reserved_notes'] = max(0, acc['reserved_notes'] - new['reserved_notes'])
        acc['reviewed'] += new['reserved_notes']
        new['launched_at'] = at
    return _move(root, run, 'running', now, apply)


def applying(root, run, now=None):
    return _move(root, run, 'applying', now, lambda new, acc, at: None)


def release(root, run, reason, now=None):
    """A known failure before launch: free the money and notes. The attempt and its interval stay recorded."""
    def apply(new, acc, at):
        _free(new, acc)
        new.update(released=True, reason_code=reason, finished_at=at)
        _failed(acc, new, at, reason)
    return _move(root, run, 'failed', now, apply)


def finish(root, run, usage_micro, outcome, now=None, reason=None):
    """End a launched run. Known usage is charged as it is; unknown usage (None) charges the whole reservation."""
    if outcome not in FINISH_STATES:
        raise ValueError(f'Invalid finish state: {outcome}')
    def apply(new, acc, at):
        known = usage_micro is not None
        _free(new, acc, notes=False)
        new.update(charged_micro=usage_micro if known else new['reserved_micro'], cost_complete=known, finished_at=at, reason_code=reason)
        acc['charged_micro'] += new['charged_micro']
        acc['cost_complete'] = acc['cost_complete'] and known
        if known and usage_micro > new['budget_passed_micro']:
            new['reason_code'] = 'provider_budget'
            acc['blocked'] = {'reason': 'provider_budget', 'config_revision': new['config_revision']}
    return _move(root, run, outcome, now, apply)


def operation_id(run_id, ref, action):
    return 'm-' + hashlib.sha256(f'{run_id}|{ref}|{action}'.encode()).hexdigest()[:40]


def decision_changes(root, run, ref, result):
    """(changes, expected, op id, updated run) recording one decision. `result` holds action, result and reason_code; the
    caller adds these to the note transaction so the decision and the note change commit together."""
    action = result['action']
    op = operation_id(run['id'], ref, action)
    stored = _current(root, run)
    new = dict(stored, decisions={**stored['decisions'], ref: {'action': action, 'result': result.get('result'), 'operation_id': op,
                                                                'reason_code': result.get('reason_code')}})
    path = _run_path(new)
    return {path: canonical_json(new)}, {path: _file_digest(root, path)}, op, new


def recover_runs(root, now=None):
    """Settle runs a dead worker left behind; call it holding the worker lock. Returns the recovered run IDs."""
    done = []
    with store_lock(root):
        base = Path(root) / RUNS
        for path in sorted(base.glob('*/*.json')) if base.exists() else []:
            try:
                run = _read_run(path)
            except Unavailable:
                continue
            if run['state'] not in ('reserved', 'running', 'applying'):
                continue
            was_reserved = run['state'] == 'reserved'
            def apply(new, acc, at, was_reserved=was_reserved):
                _free(new, acc, notes=was_reserved)
                new.update(finished_at=at, reason_code='interrupted')
                if was_reserved:
                    new['released'] = True
                    _failed(acc, new, at, 'interrupted')
                else:
                    new.update(charged_micro=new['reserved_micro'], cost_complete=False)
                    acc['charged_micro'] += new['reserved_micro']
                    acc['cost_complete'] = False
            _move(root, run, 'interrupted', now, apply)
            done.append(run['id'])
    return done


# ---- worker lock

@contextmanager
def worker_lock(root):
    with ExitStack() as stack:
        try:
            stack.enter_context(file_lock(Path(root) / 'maintenance' / 'worker.lock', timeout=0))
        except (TimeoutError, OSError):
            raise MaintenanceSkip('already_running') from None
        yield


def running(root):
    """True while another process holds the worker lock; the lock file's existence proves nothing."""
    try:
        with worker_lock(root):
            return False
    except MaintenanceSkip:
        return True


# ---- retry checkpoints

def retry_hold(st, ref, revision, fingerprint, now=None):
    """The retry entry while it still holds (same revision and fingerprint, not yet due), else None."""
    entry = (st.get('retry') or {}).get(ref)
    if (entry and entry.get('revision') == revision and entry.get('fingerprint') == fingerprint
            and _parse(entry['retry_after']) > _aware(now)):
        return entry
    return None


def set_retry(root, ref, revision, fingerprint, result, now=None, days=RETRY_DAYS):
    """Hold a note's review until `days` pass or its revision or fingerprint changes. Never touches `checked`."""
    with store_lock(root):
        now = _aware(now)
        acc, _ = accounting(root, now)
        before = _file_digest(root, STATE)
        acc['retry'] = {k: v for k, v in acc['retry'].items() if _parse(v['retry_after']) > now}
        acc['retry'][ref] = {'revision': revision, 'fingerprint': fingerprint, 'retry_after': _iso(now + timedelta(days=days)), 'result': result}
        return transaction(root, {STATE: canonical_json(acc)}, 'maintenance_run', expected={STATE: before})
