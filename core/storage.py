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
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from .common import atomic_write, file_lock
from .paths import is_os_junk


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


# bind starts a store in an existing empty folder and the restore commands rebuild one. The journal commands also run on
# the other journal roots RECOVERY.md names (install folder, harness homes, CODEX_HOME, an Obsidian vault), which hold no bindings.
STORE_CHECK_SKIPPED = {'bind', 'restore-backup', 'version', 'history', 'diff', 'recover', 'undo'}


def require_store(root, command):
    """An explicit --root must name an existing folder, and a store unless the command is in STORE_CHECK_SKIPPED, so a
    mistyped path fails instead of quietly creating one (STO-2)."""
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f'no_store: the --root folder does not exist: {root}')
    if command not in STORE_CHECK_SKIPPED and not (root / 'bindings.json').is_file():
        raise ValueError(f'no_store: the --root folder is not a Living Memory store (no bindings.json): {root}')


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
        raise ValueError('Automatic maintenance is running or has an unfinished run; wait for it (or run maintenance status), then run setup --location again')


DERIVED_INDEX = re.compile(r'(?:search|sources|vectors|docs)\.sqlite3(?:-wal|-shm|-journal)?')


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
        # Derived indexes with their SQLite side files; the destination rebuilds them (docs.sqlite3 is the notebook index).
        if len(rel.parts) <= 3 and (DERIVED_INDEX.fullmatch(path.name) or path.name.startswith(('.sources-', '.vectors-', '.docs-'))):
            continue
        if is_os_junk(path):   # Finder files: not store content
            continue
        if path.name.startswith('.') and path.name.endswith('.relocation-copy'):   # the copy loop's temporary files; an attachment may end so
            continue
        if len(rel.parts) == 3 and rel.parts[0] == 'domains' and path.name in ('INDEX.md', 'ARCHIVE.md', '.derived-dirty'):
            continue
        result[rel.as_posix()] = _hash(path)
    return result


def _relocations():
    return config_path().parent / 'relocations'


def _pending_relocation(source, destination):
    """The ID of an interrupted relocation from source (None: any source) to destination, which a rerun resumes, or None."""
    for path in sorted(_relocations().glob('*.relocation.json')) if _relocations().is_dir() else []:
        record = _read(path)
        if not isinstance(record, dict) or (source is not None and not _same(record.get('source'), source)) or not _same(record.get('destination'), destination):
            continue
        journal = _read(path.with_name(record['id'] + '.journal.json'))
        if journal and journal.get('state') not in ('committed', 'abandoned'):
            return record['id']
    return None


def _same(recorded, path):
    """True when the recorded folder is path. Folders that exist are compared by identity, so a case-sensitive APFS volume
    keeps Store and store apart; when only one exists they differ; when neither does, the names are compared as the disk would."""
    from .paths import same_path
    if not isinstance(recorded, str):
        return False
    try:
        here, there = Path(recorded).exists(), Path(path).exists()
        if here and there:
            return os.path.samefile(recorded, path)
        if here != there:
            return False
    except OSError:
        pass
    return same_path(recorded, path, resolve=False)


def _finish(journal_path, journal, state):
    """Write a terminal state (committed or abandoned) with the time it was reached, so the newest one wins."""
    _write(journal_path, {**journal, 'state': state, 'completed_ns': time.time_ns()})


def _abandoned_relocation(destination):
    """True when the newest finished relocation to destination was abandoned (its source or the configuration changed during
    the copy): a folder that became the store since is never called a stale copy, and a later abandonment is not hidden by an
    older commit. Journals written before completed_ns are ordered by their file time."""
    latest = None
    for path in _relocations().glob('*.relocation.json') if _relocations().is_dir() else []:
        record = _read(path)
        if not isinstance(record, dict) or not _same(record.get('destination'), destination):
            continue
        journal_path = path.with_name(record['id'] + '.journal.json')
        journal = _read(journal_path)
        if not isinstance(journal, dict) or journal.get('state') not in ('committed', 'abandoned'):
            continue
        at = journal.get('completed_ns')
        if not isinstance(at, int):
            try:
                at = journal_path.stat().st_mtime_ns
            except OSError:
                continue
        if latest is None or at > latest[0]:
            latest = (at, journal['state'])
    return latest is not None and latest[1] == 'abandoned'


def relocate(source, destination, config_revision=False):
    """Move the shared store from source to destination in one direct command: copy, verify, then point the configuration at the
    copy. The source is kept, marked as relocated. Rerunning the same command resumes an interrupted relocation.
    config_revision: the configuration's hash when the caller chose source from it, so a setup that changed it meanwhile wins."""
    from .repository import store_lock
    source, destination = local_path(source), local_path(destination)
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Source and destination must be separate, non-nested folders')
    resumed = _pending_relocation(source, destination)
    if resumed:
        return _relocate(resumed)
    if destination.exists() and (not destination.is_dir() or any(p.name != '.store.lock' and not is_os_junk(p) for p in destination.iterdir())):
        raise ValueError('Relocation requires an empty destination; stores are never merged')
    with store_lock(source):
        validate_store(source)
        manifest = _manifest(source)
    value = {'version': 1, 'id': uuid.uuid4().hex, 'source': str(source), 'destination': str(destination),
             'config_path': str(config_path()), 'config_revision': _hash(config_path()) if config_revision is False else config_revision, 'files': manifest,
             'notes': sum(1 for p in manifest if p.endswith('.md') and len(p.split('/')) > 3 and p.split('/')[0] == 'domains' and p.split('/')[2] in ('notes', 'archive'))}
    value['digest'] = _digest(value)
    destination.mkdir(parents=True, exist_ok=True)
    _write(_relocations() / (value['id'] + '.relocation.json'), value)
    result = _relocate(value['id'])
    return {**result, 'notes': value['notes'], 'files': len(manifest), 'next': 'Restart running harness sessions so they use the new folder.'}


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


def _maintenance_matches(source, destination, recorded_hash):
    """The destination's maintenance.json is the recorded source configuration with only `enabled` set to false, or an exact copy
    when the source's is not a valid configuration."""
    from . import maintenance_state as ms
    from .repository import read_json
    src, dest = source / MAINTENANCE_CONFIG, destination / MAINTENANCE_CONFIG
    if recorded_hash is None:
        return not dest.exists()
    if not src.is_file() or _hash(src) != recorded_hash or not dest.is_file():
        return False
    try:
        want = {**ms.validate(read_json(src)), 'enabled': False}
    except ValueError:
        return _hash(dest) == recorded_hash
    try:
        return ms.validate(read_json(dest)) == want
    except ValueError:
        return False


def _same_files(actual, recorded, source, destination):
    """Manifests match; the destination's maintenance.json must be the source configuration, disabled."""
    skip = lambda m: {k: v for k, v in m.items() if k != MAINTENANCE_CONFIG}
    return skip(actual) == skip(recorded) and (MAINTENANCE_CONFIG in actual) == (MAINTENANCE_CONFIG in recorded) and         _maintenance_matches(source, destination, recorded.get(MAINTENANCE_CONFIG))


def _relocate(identity):
    """Copy, verify and publish one recorded relocation; the pointer changes last, so a stop anywhere leaves the old store in use."""
    from .repository import store_lock, reconcile
    directory = _relocations()
    record = _read(directory / (identity + '.relocation.json'))
    if not record:
        raise ValueError('Relocation record unavailable')
    if _digest({k: v for k, v in record.items() if k != 'digest'}) != record['digest']:
        raise ValueError('Relocation record damaged; start the relocation again')
    if record['config_path'] != str(config_path()):
        raise ValueError('Relocation belongs to another configuration location')
    source, destination = local_path(record['source']), local_path(record['destination'])
    journal_path = directory / (identity + '.journal.json')
    with file_lock(config_path().parent / '.setup.lock', timeout=30):
        with ExitStack() as stack:
            for root in sorted((source, destination), key=str):
                stack.enter_context(store_lock(root, allow_retired=(root == source)))
            journal = _read(journal_path)
            target_config = {'version': 1, 'data_root': str(destination)}
            if journal and journal.get('state') == 'committed':
                return {'ok': True, 'status': 'committed', 'root': str(destination), 'source_retained': str(source)}
            # A source or configuration that changed since the record was made: this relocation can never finish, so it is
            # abandoned and the next run to this destination starts a new one.
            if _hash(config_path()) != record['config_revision'] and _read(config_path()) != target_config:
                _finish(journal_path, journal or {'relocation_id': identity}, 'abandoned')
                raise ValueError('The shared configuration changed during the relocation; nothing was published. Delete the partial copy in the destination and run setup --location again')
            if _manifest(source) != record['files']:
                _finish(journal_path, journal or {'relocation_id': identity}, 'abandoned')
                raise ValueError('The source store changed during the relocation; nothing was published. Delete the partial copy in the destination and run setup --location again')
            if not journal:
                if any(p.name != '.store.lock' and not is_os_junk(p) for p in destination.iterdir()):
                    raise ValueError('Destination conflict: no files were overwritten')
                journal = {'relocation_id': identity, 'state': 'copying'}
                _write(journal_path, journal)
            if journal['state'] == 'copying':
                # On resume, only files in the recorded manifest may be present.
                unexpected = set(_manifest(destination)) - set(record['files'])
                if unexpected:
                    raise ValueError(f'Destination conflict: {sorted(unexpected)}')
                for rel, expected_hash in record['files'].items():
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
                if not _same_files(_manifest(destination), record['files'], source, destination):
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
            for rel, expected_hash in record['files'].items():
                # Catalogs are regenerated; all other durable bytes must still match.
                if rel == MAINTENANCE_CONFIG:
                    if not _maintenance_matches(source, destination, expected_hash):
                        raise ValueError(f'Destination verification failed: {rel}')
                elif _hash(destination / rel) != expected_hash:
                    raise ValueError(f'Destination verification failed: {rel}')
            # The retired marker is durable before publishing the new pointer.
            marker = {'destination': str(destination), 'relocation_id': identity}
            existing = _read(source / '.relocated.json')
            if existing and existing != marker:
                raise ValueError('Source already relocated by another operation')
            _write(source / '.relocated.json', marker)
            _write(config_path(), target_config)
            _finish(journal_path, journal, 'committed')
    return {'ok': True, 'status': 'committed', 'root': str(destination), 'source_retained': str(source)}


def setup(location=None, non_interactive=False, source=None):
    from .migration import migrate_schema
    from .repository import store_lock
    current = None
    seen = _hash(config_path())   # before the store is chosen from it: a relocation from that store refuses a configuration changed since
    try:
        current = resolve_root()[0]
    except ValueError:
        pass  # Explicit setup may repair broken configuration; never used by runtime discovery.
    if location is None:
        if non_interactive or not sys.stdin.isatty():
            raise ValueError('Unattended setup requires --location with an absolute local folder')
        default, missing = current, None
        if default is None:
            configured = _read(config_path())
            try:
                missing = local_path(configured['data_root']) if isinstance(configured, dict) and isinstance(configured.get('data_root'), str) else None
            except ValueError:
                missing = None
            default = missing or Path.home() / 'LivingMemory'
        status = current or (f'none (the configured folder {missing} is missing; restore it or pick a location)' if missing else 'none')
        print(f'Current store: {status}\nSuggested location: {default}', file=sys.stderr)
        location = input('Memory folder (Enter accepts suggestion): ').strip() or str(default)
    destination = local_path(location)
    if source:
        return relocate(source, destination)
    pending = _pending_relocation(None, destination)
    if pending:
        return _relocate(pending)   # an interrupted relocation to this folder: finish it, never publish its partial copy
    try:
        configured = _configured()   # only the folder the shared configuration names is exempt, never an environment override
    except ValueError:
        configured = None
    if (configured is None or not _same(str(configured), destination)) and _abandoned_relocation(destination) and destination.exists() and any(p.name != '.store.lock' and not is_os_junk(p) for p in destination.iterdir()):
        raise ValueError(f'{destination} holds the partial copy of an abandoned relocation; delete its contents and run setup --location again')
    if destination.exists() and any(p.name != '.store.lock' and not is_os_junk(p) for p in destination.iterdir()):
        with store_lock(destination):
            validate_store(destination)
    else:
        if current and current != destination:
            return relocate(current, destination, seen)
        migrate_schema(destination)
    with file_lock(config_path().parent / '.setup.lock', timeout=30):
        _write(config_path(), {'version': 1, 'data_root': str(destination)})
    return {'ok': True, 'status': 'configured', 'root': str(destination), 'config_path': str(config_path()),
            'next': 'Install a harness adapter, bind a project, then run doctor --cwd PROJECT.'}


def refuse_agent_setup():
    """The refusals `alm setup` makes (core/cli.py), for install.py and integrations/setup.py run directly: not from an agent in a
    Hivemind terminal, a subagent or a viewer."""
    if os.environ.get('HIVEMIND_SESSION'):
        from .runtime import HIVEMIND_DENY
        raise SystemExit('hivemind: ' + HIVEMIND_DENY)
    actor = os.environ.get('LIVING_MEMORY_ACTOR')
    if actor in ('subagent', 'viewer'):
        raise SystemExit(f'{actor.capitalize()}s are read-only')


def ensure_setup(location=None):
    """Installer entry: explicit selection or existing store; otherwise prompt."""
    if location:
        return Path(setup(location, non_interactive=True)['root'])
    try:
        root, selected_by = resolve_root()
        if selected_by != 'legacy':
            return root
    except ValueError:
        # Do not replace invalid shared config as a side effect of adapter installation.
        if config_path().exists():
            raise
    return Path(setup()['root'])
