"""Harness-neutral store discovery, setup and recoverable approved relocation.

Configuration is user-owned, outside both the data store and harness packages.
No discovery path creates a store. Relocation never removes the source.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path

from .common import atomic_write, file_lock


def config_path():
    return Path.home() / '.config' / 'agentic-living-memory' / 'config.json'


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f'Invalid configuration preserved: {path}: {exc}') from exc


def _write(path, value):
    atomic_write(path, json.dumps(value, sort_keys=True, indent=2) + '\n')


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def local_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or str(path).startswith(('\\\\', '//')):
        raise ValueError('Choose an absolute local folder, not a relative or UNC path')
    return path.resolve()


def legacy_roots():
    candidates = [Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'agentic-living-memory-data',
                  Path.home() / '.local' / 'share' / 'agentic-living-memory']
    return list(dict.fromkeys(p.expanduser().resolve() for p in candidates))


def check_root(root):
    root = Path(root).resolve()
    retired = _read(root / '.relocated.json')
    if retired:
        raise ValueError(f'Store retired; use {retired["destination"]}. Run setup or remove stale overrides.')
    return root


def _configured():
    value = _read(config_path())
    if value is None:
        return None
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('data_root'), str):
        raise ValueError(f'Invalid shared configuration preserved: {config_path()}')
    root = local_path(value['data_root'])
    if not root.is_dir():
        raise ValueError(f'Selected memory folder unavailable: {root}; restore it or run setup')
    check_root(root)
    return root


def resolve_root(explicit=None):
    if explicit is not None:
        return check_root(Path(explicit).expanduser()), 'argument'
    for name in ('LIVING_MEMORY_ROOT', 'LIVING_MEMORY_HOME'):
        if os.environ.get(name):
            return check_root(Path(os.environ[name]).expanduser()), name
    configured = _configured()
    if configured is not None:
        return configured, 'shared_config'
    roots = [p for p in legacy_roots() if (p / 'bindings.json').is_file() and not (p / '.relocated.json').exists()]
    if len(roots) > 1:
        raise ValueError('Multiple legacy stores found; run setup --location to choose one')
    if roots:
        return roots[0], 'legacy'
    raise ValueError('Living Memory setup required: run living_memory.py setup')


def diagnostics(explicit=None):
    root, source = resolve_root(explicit)
    configured = None
    warning = None
    try:
        configured = _configured()
    except ValueError as exc:
        warning = str(exc)
    overrides = {name: str(Path(os.environ[name]).expanduser().resolve())
                 for name in ('LIVING_MEMORY_ROOT', 'LIVING_MEMORY_HOME') if os.environ.get(name)}
    conflicts = {k: v for k, v in overrides.items() if Path(v) != (configured or root)}
    if explicit is not None and configured is not None and root != configured:
        conflicts['--root'] = str(root)
    return {'config_path': str(config_path()), 'selection_source': source,
            'configured_root': str(configured) if configured else None,
            'conflicting_overrides': conflicts, 'configuration_warning': warning}


def validate_store(root):
    from .validate import validate_domain
    check_root(root)
    bindings = _read(root / 'bindings.json')
    taxonomy = _read(root / 'tag_taxonomy.json')
    if not isinstance(bindings, dict) or not isinstance(bindings.get('domains'), dict) or not taxonomy:
        raise ValueError(f'Not a valid Living Memory store: {root}')
    for domain in (root / 'domains').iterdir() if (root / 'domains').exists() else []:
        if domain.is_dir():
            issues = validate_domain(domain.name, root)
            if issues:
                raise ValueError(f'Invalid store domain {domain.name}: {issues}')
    for journal in (root / '.operations').glob('*/journal.json'):
        if _read(journal).get('state') not in ('committed', 'rolled_back', 'abandoned'):
            raise ValueError('Pending operation recovery required before relocation')
    from . import maintenance_state as ms
    if ms.active_runs(root) or ((root / 'maintenance').is_dir() and ms.running(root)):
        raise ValueError('Automatic maintenance is running or has an unfinished run; wait for it (or run maintenance status), then plan the relocation again')


def _manifest(root):
    """Preserve durable state and unknown files; omit only known transient data."""
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError(f'Links in a store require manual review: {path}')
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts[0] in ('.turns', 'hooks', 'logs', '.cache', '.views', '__pycache__', 'sources-cache') or path.name in ('.store.lock', '.relocated.json') or rel.as_posix() == 'maintenance/worker.lock':
            continue
        if path.name in ('search.sqlite3', 'search.sqlite3-wal', 'search.sqlite3-shm', 'sources.sqlite3', 'vectors.sqlite3', 'vectors.sqlite3-journal') or path.name.startswith(('.sources-', '.vectors-')):
            continue
        if path.name.endswith('.relocation-copy'):
            continue
        if len(rel.parts) == 3 and rel.parts[0] == 'domains' and path.name in ('INDEX.md', 'ARCHIVE.md', '.derived-dirty'):
            continue
        result[rel.as_posix()] = _hash(path)
    return result


def relocation_plan(source, destination):
    from .repository import store_lock
    source, destination = local_path(source), local_path(destination)
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Source and destination must be separate, non-nested folders')
    if destination.exists() and (not destination.is_dir() or any(p.name != '.store.lock' for p in destination.iterdir())):
        raise ValueError('Relocation requires an empty destination; stores are never merged')
    with store_lock(source):
        validate_store(source)
        manifest = _manifest(source)
    value = {'version': 1, 'id': uuid.uuid4().hex, 'source': str(source), 'destination': str(destination),
             'config_path': str(config_path()), 'config_revision': _hash(config_path()), 'files': manifest,
             'notes': sum(1 for p in manifest if p.startswith('domains/') and p.endswith('.md') and ('/notes/' in p or '/archive/' in p))}
    value['digest'] = _digest(value)
    _write(config_path().parent / 'relocations' / (value['id'] + '.plan.json'), value)
    return {'ok': True, 'status': 'approval_required', 'plan_id': value['id'], 'digest': value['digest'],
            'source': str(source), 'destination': str(destination), 'notes': value['notes'], 'files': len(manifest),
            'instruction': 'Stop harness sessions, then run setup --apply-plan ID --expected-plan-sha256 DIGEST. Source is retained.'}


def inspect_relocation(identity):
    """Report approval freshness without exposing note contents or changing stores."""
    if not re.fullmatch('[a-f0-9]{32}', identity or ''):
        raise ValueError('Invalid relocation plan identity')
    plan = _read(config_path().parent / 'relocations' / (identity + '.plan.json'))
    if not plan:
        raise ValueError('Relocation plan unavailable')
    if _digest({k: v for k, v in plan.items() if k != 'digest'}) != plan['digest']:
        raise ValueError('Plan digest mismatch')
    source = local_path(plan['source'])
    from .repository import store_lock
    with store_lock(source, allow_retired=True):
        current = _manifest(source)
    previous = plan['files']
    return {'ok': True, 'plan_id': identity, 'digest': plan['digest'],
            'source': plan['source'], 'destination': plan['destination'],
            'config_changed': _hash(config_path()) != plan['config_revision'],
            'added': sorted(set(current) - set(previous)),
            'removed': sorted(set(previous) - set(current)),
            'changed': sorted(p for p in set(previous) & set(current) if previous[p] != current[p])}


MAINTENANCE_CONFIG = 'maintenance.json'


def _disable_maintenance(destination):
    """The destination never inherits enabled maintenance: paid work starts only after an explicit `maintenance enable` there."""
    from . import maintenance_state as ms
    from .repository import canonical_json
    from .repository import read_json
    path = destination / MAINTENANCE_CONFIG
    if path.is_file():
        try:
            cfg = ms.validate(read_json(path))   # read_json accepts a BOM, as ms.config does
        except ValueError:
            return  # a malformed configuration already reads as disabled; _maintenance_matches then requires an exact copy
        if cfg['enabled']:
            atomic_write(path, canonical_json({**cfg, 'enabled': False}))


def _maintenance_matches(source, destination, planned_hash):
    """The destination's maintenance.json is the approved source configuration with only `enabled` set to false, or an exact copy
    when the source's is not a valid configuration."""
    from . import maintenance_state as ms
    from .repository import read_json
    src, dest = source / MAINTENANCE_CONFIG, destination / MAINTENANCE_CONFIG
    if planned_hash is None:
        return not dest.exists()
    if not src.is_file() or _hash(src) != planned_hash or not dest.is_file():
        return False
    try:
        want = {**ms.validate(read_json(src)), 'enabled': False}
    except ValueError:
        return _hash(dest) == planned_hash
    try:
        return ms.validate(read_json(dest)) == want
    except ValueError:
        return False


def _same_files(actual, planned, source, destination):
    """Manifests match; the destination's maintenance.json must be the source configuration, disabled."""
    skip = lambda m: {k: v for k, v in m.items() if k != MAINTENANCE_CONFIG}
    return skip(actual) == skip(planned) and (MAINTENANCE_CONFIG in actual) == (MAINTENANCE_CONFIG in planned) and         _maintenance_matches(source, destination, planned.get(MAINTENANCE_CONFIG))


def apply_relocation(identity, expected):
    """Resume by rerunning the same approved command; pointer changes last."""
    from .repository import store_lock, reconcile
    if not re.fullmatch('[a-f0-9]{32}', identity or ''):
        raise ValueError('Invalid relocation plan identity')
    directory = config_path().parent / 'relocations'
    plan = _read(directory / (identity + '.plan.json'))
    if not plan:
        raise ValueError('Relocation plan unavailable')
    unsigned = {k: v for k, v in plan.items() if k != 'digest'}
    if _digest(unsigned) != expected or plan['digest'] != expected:
        raise ValueError('Plan digest mismatch')
    if plan['config_path'] != str(config_path()):
        raise ValueError('Plan belongs to another configuration location')
    source, destination = local_path(plan['source']), local_path(plan['destination'])
    journal_path = directory / (identity + '.journal.json')
    with file_lock(config_path().parent / '.setup.lock', timeout=30):
        with ExitStack() as stack:
            for root in sorted((source, destination), key=str):
                stack.enter_context(store_lock(root, allow_retired=(root == source)))
            journal = _read(journal_path)
            target_config = {'version': 1, 'data_root': str(destination)}
            if journal and journal.get('state') == 'committed':
                return {'ok': True, 'status': 'committed', 'root': str(destination), 'source_retained': str(source)}
            if _hash(config_path()) != plan['config_revision'] and _read(config_path()) != target_config:
                raise ValueError('Stale shared configuration; create a new relocation plan')
            if _manifest(source) != plan['files']:
                raise ValueError('Stale source store; create a new relocation plan')
            if not journal:
                if any(p.name != '.store.lock' for p in destination.iterdir()):
                    raise ValueError('Destination conflict: no files were overwritten')
                journal = {'plan_id': identity, 'state': 'copying'}
                _write(journal_path, journal)
            if journal['state'] == 'copying':
                # On resume, only files in the approved manifest may be present.
                unexpected = set(_manifest(destination)) - set(plan['files'])
                if unexpected:
                    raise ValueError(f'Destination conflict: {sorted(unexpected)}')
                for rel, expected_hash in plan['files'].items():
                    dest = destination / rel
                    if dest.exists():
                        if _hash(dest) != expected_hash and not (rel == MAINTENANCE_CONFIG and _maintenance_matches(source, destination, expected_hash)):
                            raise ValueError(f'Destination conflict: {rel}')
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # Copy via replace so a killed process cannot leave a partial canonical file.
                    temp = dest.with_name('.' + dest.name + '.relocation-copy')
                    shutil.copyfile(source / rel, temp)
                    os.replace(temp, dest)
                for domain in (source / 'domains').iterdir() if (source / 'domains').exists() else []:
                    if domain.is_dir():
                        for name in ('notes', 'archive'):
                            (destination / 'domains' / domain.name / name).mkdir(parents=True, exist_ok=True)
                _disable_maintenance(destination)
                if not _same_files(_manifest(destination), plan['files'], source, destination):
                    raise ValueError('Destination verification failed; shared location unchanged')
                validate_store(destination)
                for log in (source / 'logs').glob('*') if (source / 'logs').is_dir() else []:
                    try:
                        if log.is_file() and not log.is_symlink() and not (destination / 'logs' / log.name).exists():
                            (destination / 'logs').mkdir(exist_ok=True)
                            shutil.copyfile(log, destination / 'logs' / log.name)
                    except OSError:
                        pass  # Log history is best-effort; it never fails a relocation.
                journal['state'] = 'verified'
                _write(journal_path, journal)
            if journal['state'] == 'verified':
                for domain in (destination / 'domains').iterdir() if (destination / 'domains').exists() else []:
                    if domain.is_dir():
                        reconcile(domain.name, destination, sync_overlay=False)
                journal['state'] = 'ready'
                _write(journal_path, journal)
            for rel, expected_hash in plan['files'].items():
                # Catalogs are regenerated; all other durable bytes must still match.
                if rel == MAINTENANCE_CONFIG:
                    if not _maintenance_matches(source, destination, expected_hash):
                        raise ValueError(f'Destination verification failed: {rel}')
                elif _hash(destination / rel) != expected_hash:
                    raise ValueError(f'Destination verification failed: {rel}')
            # The retired marker is durable before publishing the new pointer.
            marker = {'destination': str(destination), 'plan_id': identity}
            existing = _read(source / '.relocated.json')
            if existing and existing != marker:
                raise ValueError('Source already relocated by another operation')
            _write(source / '.relocated.json', marker)
            _write(config_path(), target_config)
            journal['state'] = 'committed'
            _write(journal_path, journal)
    return {'ok': True, 'status': 'committed', 'root': str(destination), 'source_retained': str(source)}


def setup(location=None, non_interactive=False, source=None):
    from .migration import migration_plan
    from .governance import apply
    from .repository import store_lock
    current = None
    try:
        current = resolve_root()[0]
    except ValueError:
        pass  # Explicit setup may repair broken configuration; never used by runtime discovery.
    if location is None:
        if non_interactive or not sys.stdin.isatty():
            raise ValueError('Unattended setup requires --location with an absolute local folder')
        default = current if _read(config_path()) else Path.home() / 'LivingMemory'
        print(f'Current store: {current or "none"}\nSuggested location: {default}', file=sys.stderr)
        location = input('Memory folder (Enter accepts suggestion): ').strip() or str(default)
    destination = local_path(location)
    if source:
        return relocation_plan(source, destination)
    if destination.exists() and any(p.name != '.store.lock' for p in destination.iterdir()):
        with store_lock(destination):
            validate_store(destination)
    else:
        if current and current != destination:
            return relocation_plan(current, destination)
        with store_lock(destination):
            proposal = migration_plan(destination)
            apply(destination, proposal['plan_id'], proposal['digest'])
    with file_lock(config_path().parent / '.setup.lock', timeout=30):
        _write(config_path(), {'version': 1, 'data_root': str(destination)})
    return {'ok': True, 'status': 'configured', 'root': str(destination), 'config_path': str(config_path()),
            'next': 'Install a harness adapter, bind a project, then run doctor --cwd PROJECT.'}


def ensure_setup(location=None):
    """Installer entry: explicit selection or existing store; otherwise prompt."""
    if location:
        result = setup(location, non_interactive=True)
        if result['status'] == 'approval_required':
            raise ValueError('Store relocation requires approval first: ' + json.dumps(result))
        return Path(result['root'])
    try:
        root, selected_by = resolve_root()
        if selected_by != 'legacy':
            return root
    except ValueError:
        # Do not replace invalid shared config as a side effect of adapter installation.
        if config_path().exists():
            raise
    result = setup()
    if result['status'] == 'approval_required':
        raise ValueError('Run setup and approve the relocation before installing an adapter: ' + json.dumps(result))
    return Path(result['root'])
