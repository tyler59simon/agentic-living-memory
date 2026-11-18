"""Install the Agentic Living Memory package into its own per-user folder, independent of any harness.

Windows: %LOCALAPPDATA%/agentic-living-memory/app. Elsewhere: ~/.local/lib/agentic-living-memory/app.
Plans, journals and the ownership record live beside app/ in that folder. Each harness (Claude Code, Codex,
Copilot, Factory) is wired afterwards with integrations/setup.py HARNESS --hooks, run from the installed copy.
Older releases installed into CODEX_HOME/agentic-living-memory; --retire-legacy-plan removes that copy once
no harness points at it.

The plan also puts an `alm` launcher in ~/.local/bin and, when that folder is not on PATH, adds it (summary
keys `launcher` and `path_change`). Apply checks both are unchanged since the plan, then writes them."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from core.installer_lifecycle import build_plan as build_lifecycle_plan

SOURCE = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
HARNESSES = ("claude", "codex", "copilot", "factory")


def _install_home() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        # Not ~/.local/share/agentic-living-memory: older releases kept memory data there.
        base = Path.home() / ".local" / "lib"
    return (base.expanduser() / "agentic-living-memory").resolve()


INSTALL_HOME = _install_home()
INSTALL_DIR = INSTALL_HOME / "app"
LEGACY_DIR = CODEX_HOME / "agentic-living-memory"
LAUNCHER_DIR = Path.home() / ".local" / "bin"
PROFILE_LINE = 'export PATH="$HOME/.local/bin:$PATH"  # agentic-living-memory'


def harness_home(harness: str) -> Path:
    return Path(os.environ.get(harness.upper() + "_HOME", Path.home() / ("." + harness))).expanduser()


CONFIG_NAMES = {"claude": "settings.json", "codex": "hooks.json", "copilot": "hooks/agentic-living-memory.json", "factory": "hooks.json"}


def harness_configs(extra_homes=()) -> list:
    """(harness, hook configuration file) for each default harness home, plus homes given with --home."""
    homes = [(h, harness_home(h)) for h in HARNESSES] + [(h, Path(p).expanduser()) for h, p in extra_homes]
    found = [(h, home / CONFIG_NAMES[h]) for h, home in homes]
    return list(dict.fromkeys((h, path) for h, path in found if path.is_file()))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _lead() -> str:
    """PowerShell runs a command that starts with a quoted executable only after `&`."""
    return '& ' if os.name == 'nt' else ''


def _unwire_command(harness: str, path: Path) -> str:
    home = path.parents[len(Path(CONFIG_NAMES[harness]).parts) - 1]
    custom = '' if home.resolve() == harness_home(harness).resolve() else f' --home "{home}"'
    return f'{_lead()}"{sys.executable}" "{INSTALL_DIR / "integrations" / "setup.py"}" {harness} --uninstall-plan{custom}'


def legacy_references(extra_homes=(), folder=None, unwire=False) -> list:
    """Harnesses whose hook configuration still runs scripts from the legacy package folder (or from `folder`).
    With unwire, each entry ends with the command that unwires that harness."""
    marker = os.path.normcase(str(folder or LEGACY_DIR) + os.sep)
    found = []
    for harness, path in harness_configs(extra_homes):
        fix = f'; unwire it with: {_unwire_command(harness, path)}' if unwire else ''
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            found.append(f"{harness} ({path}: unreadable, so it cannot be checked{fix})")
            continue
        if any(marker in os.path.normcase(text) for text in _strings(config)):
            found.append(f"{harness} ({path}{fix})")
    return found


def next_steps() -> list:
    setup = INSTALL_DIR / "integrations" / "setup.py"
    homes = [h for h in HARNESSES if harness_home(h).is_dir()]
    return [f'"{sys.executable}" "{setup}" {h} --hooks' for h in homes]


def print_next_steps(root, plan_id) -> None:
    """After an install, the wiring commands from the applied plan, ready to paste into PowerShell, on stderr (stdout stays the JSON result)."""
    from core.repository import read_json
    steps = ((read_json(Path(root) / '.plans' / f'{plan_id}.json') or {}).get('summary') or {}).get('next_steps') or []
    if steps:
        print('Installed. Next, wire each harness: run these, then review and apply each plan they print (--apply-plan ID --expected-plan-sha256 DIGEST).\n' +
              '\n'.join('  ' + _lead() + step for step in steps), file=sys.stderr)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--location', type=Path, help='Shared memory folder for unattended first-time setup')
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--apply-plan')
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--uninstall-plan', action='store_true', help='Remove the package (unwire each harness first)')
    parser.add_argument('--retire-legacy-plan', action='store_true', help=f'Remove the old copy in {LEGACY_DIR}')
    parser.add_argument('--home', action='append', default=[], metavar='HARNESS=PATH',
                        help='A harness home set up with setup.py --home PATH; uninstall and retirement check it too (repeatable)')
    args = parser.parse_args()
    homes = []
    for value in args.home:
        harness, sep, path = value.partition('=')
        if not sep or harness not in HARNESSES or not path:
            parser.error('--home takes HARNESS=PATH, with HARNESS one of ' + ', '.join(HARNESSES))
        homes.append((harness, path))
    if not (args.uninstall_plan or args.apply_plan or args.retire_legacy_plan):
        from core.storage import ensure_setup
        ensure_setup(args.location)
    if args.apply_plan:
        if not args.expected_plan_sha256:
            parser.error('--expected-plan-sha256 is required')
        # Package plans live in INSTALL_HOME; legacy retirement plans in CODEX_HOME.
        root = INSTALL_HOME if (INSTALL_HOME / '.plans' / f'{args.apply_plan}.json').is_file() else CODEX_HOME
        result = apply_plan(root, args.apply_plan, args.expected_plan_sha256, homes)
        print_next_steps(root, args.apply_plan)
    elif args.retire_legacy_plan:
        result = plan_retire(homes)
    else:
        proposed = plan_install(args.uninstall_plan, homes)
        result = proposed if args.plan or args.uninstall_plan else apply_plan(INSTALL_HOME, proposed['plan_id'], proposed['digest'])
        if result is not proposed:
            print_next_steps(INSTALL_HOME, proposed['plan_id'])
    print(json.dumps(result, separators=(',', ':')))
    return 0


def _refuse_if_in_use(extra_homes=()):
    still = legacy_references(extra_homes, INSTALL_DIR, True)
    if still:
        raise ValueError('The package is still in use by: ' + '; '.join(still) + '. Apply each unwire plan, then retry the uninstall.')


def plan_install(uninstall=False, extra_homes=()):
    from core.repository import store_lock
    from core.governance import plan
    if uninstall:
        _refuse_if_in_use(extra_homes)
    INSTALL_HOME.mkdir(parents=True, exist_ok=True)
    with store_lock(INSTALL_HOME):
        changes = _package_changes(INSTALL_DIR, uninstall)
        summary = {'package': str(INSTALL_DIR), 'files': len(changes), 'interpreter': sys.executable}
        summary['launcher'] = _launcher_plan(uninstall)
        if uninstall:
            summary['homes'] = [[h, str(Path(p).expanduser().resolve())] for h, p in extra_homes]   # absolute: apply may run from another folder
            summary['before'] = 'Unwire each harness first: setup.py HARNESS --uninstall-plan from the installed copy.'
            summary['path'] = f'A PATH entry for {LAUNCHER_DIR} is left in place: other programs may live in that folder.'
        else:
            summary['next_steps'] = next_steps()
            change = _path_plan() if summary['launcher']['action'] in ('create', 'replace', 'keep') else None
            if change:
                summary['path_change'] = change
        return plan(INSTALL_HOME, changes, 'package_uninstall' if uninstall else 'package_install', summary=summary)


def _sha(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _launcher_state(path: Path):
    """What is at the launcher path now: None (nothing), 'symlink' (also any non-file), or the sha256 of the file."""
    if not os.path.lexists(path):
        return None
    return 'symlink' if path.is_symlink() or not path.is_file() else _sha(path.read_bytes())


def _norm_dir(text):
    return os.path.normcase(os.path.normpath(os.path.expandvars(text)))


def _on_path(folder, path_value, sep):
    return any(_norm_dir(e) == _norm_dir(str(folder)) for e in path_value.split(sep) if e)


def _launcher_plan(uninstall):
    """The `alm` launcher this plan creates, replaces, keeps or removes. Only a file carrying our marker is ever touched."""
    from core import launcher
    path = LAUNCHER_DIR / launcher.FILE
    script = INSTALL_DIR / 'living_memory.py'
    state = _launcher_state(path)
    record = {'path': str(path), 'action': 'none', 'before': state, 'after': None, 'python': sys.executable, 'script': str(script),
              'on_path': _on_path(LAUNCHER_DIR, os.environ.get('PATH', ''), os.pathsep)}
    if state is not None and not launcher.ours(path):
        record['reason'] = 'a file named ' + launcher.FILE + ' that is not ours is already there; left alone'
    elif uninstall:
        if state is not None:
            record['action'] = 'remove'
        else:
            record['reason'] = 'no launcher installed'
    else:
        want = launcher.build(sys.executable, script)
        if want is None:
            record['reason'] = 'no launcher stub found (needs pip: run `py -m ensurepip`); agents keep using the full command'
        else:
            record['after'] = _sha(want)
            record['action'] = 'create' if state is None else 'keep' if state == record['after'] else 'replace'
    return record


def _registry_path():
    """(value, type) of the user Path value under HKCU\\Environment; ('', REG_EXPAND_SZ) when there is none."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
        try:
            return winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            return '', winreg.REG_EXPAND_SZ


def _profile():
    shell = os.path.basename(os.environ.get('SHELL', ''))
    home = Path.home()
    return home / '.zprofile' if shell == 'zsh' else home / '.bash_profile' if shell == 'bash' and (home / '.bash_profile').exists() else home / '.profile'


def _path_plan():
    """The PATH change that makes the launcher folder reachable, or None when it already is (this process's PATH, the user's registry value or profile)."""
    if _on_path(LAUNCHER_DIR, os.environ.get('PATH', ''), os.pathsep):
        return None
    if os.name == 'nt':
        value, kind = _registry_path()
        if _on_path(LAUNCHER_DIR, value, ';'):
            return None
        return {'kind': 'registry', 'target': 'HKCU\\Environment\\Path', 'before': value, 'before_type': kind, 'add': str(LAUNCHER_DIR)}
    profile = _profile()
    text = profile.read_text(encoding='utf-8') if profile.is_file() else None
    if text is not None and PROFILE_LINE in text:
        return None
    return {'kind': 'profile', 'target': str(profile), 'before': _sha(text.encode()) if text is not None else None, 'add': PROFILE_LINE}


def _path_stale(change):
    if change['kind'] == 'registry':
        return list(_registry_path()) != [change['before'], change['before_type']]
    profile = Path(change['target'])
    return (_sha(profile.read_bytes()) if profile.is_file() else None) != change['before']


def _add_to_path_windows(change):
    import ctypes
    import winreg
    value = change['before'].rstrip(';')
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, 'Path', 0, change['before_type'], (value + ';' if value else '') + change['add'])
    # Tell running programs (Explorer, new terminals) the environment changed.
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, 'Environment', 0x2, 5000, ctypes.byref(ctypes.c_ulong()))


def _add_to_path_posix(change):
    profile = Path(change['target'])
    text = profile.read_text(encoding='utf-8') if profile.is_file() else ''
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(text + ('' if not text or text.endswith('\n') else '\n') + change['add'] + '\n', encoding='utf-8')


def _path_done(change):
    """True when the PATH entry this change adds is already there (a retry after it was written)."""
    if change['kind'] == 'registry':
        return _on_path(change['add'], _registry_path()[0], ';')
    profile = Path(change['target'])
    return profile.is_file() and change['add'] in profile.read_text(encoding='utf-8')


def _launcher_stale(record):
    if record['python'] != sys.executable:
        return True
    # A retry of this plan may find its own launcher step already done: that state is expected too.
    done = {'create': record['after'], 'replace': record['after'], 'remove': None}.get(record['action'], record['before'])
    if _launcher_state(Path(record['path'])) not in (record['before'], done):
        return True
    if record['action'] in ('create', 'replace', 'keep'):
        from core import launcher
        return _sha(launcher.build(record['python'], record['script'])) != record['after']
    return False


def _write_launcher(record):
    """Write (create, replace) or remove the launcher, then probe it. A launcher that does not run is removed. Returns a warning or None."""
    from core import launcher
    path = Path(record['path'])
    if record['action'] == 'remove':
        if launcher.ours(path):
            path.unlink()
        return None
    if record['action'] not in ('create', 'replace'):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = path.with_name(path.name + '.new')
    fresh.write_bytes(launcher.build(record['python'], record['script']))
    fresh.chmod(0o755)
    try:
        os.replace(fresh, path)
    except PermissionError:   # Windows: a running exe cannot be overwritten but can be renamed
        old = path.with_name(path.name + '.old')
        os.replace(path, old)
        os.replace(fresh, path)
        try:
            old.unlink()
        except OSError:
            pass
    try:
        probe = subprocess.run([str(path), '--help'], capture_output=True, timeout=60)
        if probe.returncode == 0 and probe.stdout.startswith(b'usage: alm'):
            return None
        reason = f'exit {probe.returncode}: ' + probe.stderr.decode('utf-8', 'replace').strip()[:300]
    except (OSError, subprocess.SubprocessError) as exc:
        reason = str(exc)
    path.unlink()
    return f'The launcher {path} did not run ({reason}) and was removed; agents keep using the full command.'


def apply_plan(root, plan_id, digest, extra_homes=()):
    """governance.apply, plus the launcher and PATH change a package plan records. Both are checked before anything is written.
    A package uninstall re-checks that no harness runs the package: the --home folders it was planned with and extra_homes."""
    from core.governance import apply
    from core.repository import canonical_json, read_json
    value = read_json(Path(root) / '.plans' / f'{plan_id}.json') or {}
    unsigned = {k: v for k, v in value.items() if k != 'digest'}
    summary = value.get('summary') or {}
    if value.get('kind') == 'package_uninstall':
        _refuse_if_in_use([tuple(h) for h in summary.get('homes') or []] + list(extra_homes))
    if value.get('digest') != digest or hashlib.sha256(canonical_json(unsigned).encode()).hexdigest() != digest or 'launcher' not in summary:
        return apply(root, plan_id, digest)   # unverified or not a package plan: governance decides
    record, change = summary['launcher'], summary.get('path_change')
    if _launcher_stale(record) or (change and _path_stale(change) and not _path_done(change)):
        raise ValueError('stale plan: the launcher or PATH changed since this plan was made; plan again')
    receipt = apply(root, plan_id, digest)
    warning = _write_launcher(record)
    receipt['launcher'] = {'path': record['path'], 'action': record['action'] if not warning else 'failed'}
    if warning:
        receipt.setdefault('warnings', []).append(warning)
    elif change:
        if not _path_done(change):
            (_add_to_path_windows if change['kind'] == 'registry' else _add_to_path_posix)(change)
        receipt['path_change'] = change['add']
        receipt['notice'] = 'Open a new terminal (and restart running agent sessions) so `alm` is found.'
    return receipt


def plan_retire(extra_homes=()):
    """Remove the files the legacy CODEX_HOME copy owns, once nothing runs from it. Harness homes outside
    the defaults (setup.py --home PATH) are checked only when passed here, so the plan lists what it checked."""
    from core.repository import store_lock
    from core.governance import plan
    if not (LEGACY_DIR / '.core-ownership-v1.json').is_file():
        raise ValueError(f'No legacy package at {LEGACY_DIR}')
    if not (INSTALL_DIR / '.core-ownership-v1.json').is_file():
        raise ValueError(f'Install the package at {INSTALL_DIR} before retiring the legacy copy')
    still = legacy_references(extra_homes)
    if still:
        raise ValueError('Legacy package still in use by: ' + '; '.join(still) +
                         '. Run integrations/setup.py HARNESS --hooks from the new copy for each, apply, then retry.')
    with store_lock(CODEX_HOME):
        changes = _package_changes(LEGACY_DIR, True)
        removed = {k for k, v in changes.items() if v is None}
        kept = sorted(p.relative_to(LEGACY_DIR).as_posix() for p in LEGACY_DIR.rglob('*')
                      if p.is_file() and (Path(LEGACY_DIR.name) / p.relative_to(LEGACY_DIR)).as_posix() not in removed)
        untouched = [str(p) for p in (CODEX_HOME / 'agentic-living-memory-data', CODEX_HOME / '.agentic-living-memory.lifecycle') if p.exists()]
        summary = {'retire': str(LEGACY_DIR), 'files': len(removed), 'checked': [str(p) for _, p in harness_configs(extra_homes)],
                   'not_owned_left_in_place': kept[:50],
                   'untouched': untouched,
                   'note': 'Only files the old install owns and that are unchanged are removed. Harness homes set up with '
                           '--home PATH are checked only if passed as --home HARNESS=PATH.'}
        return plan(CODEX_HOME, changes, 'legacy_package_retire', summary=summary)


def _package_changes(target: Path, uninstall: bool) -> dict:
    """Package files and the ownership record, keyed relative to target.parent (the plan root)."""
    from core import installer_lifecycle as lifecycle
    from core.repository import canonical_json, now
    import uuid
    proposal = build_lifecycle_plan(target, SOURCE, uninstall)
    if proposal['blockers']:
        raise RuntimeError('Existing files preserved; ownership conflicts: ' + json.dumps(proposal['blockers']))
    changes = {}
    entries = {}
    for action in proposal['actions']:
        rel = action['path']
        dest = (Path(target.name) / rel).as_posix()
        if action['action'] in {'create', 'replace'}:
            changes[dest] = lifecycle._read_source(SOURCE, rel, action['sourceSha256']).decode('utf-8')
        elif action['action'] == 'remove':
            changes[dest] = None
        if not uninstall and action['action'] in {'create', 'replace', 'keep', 'adopt'}:
            entry = proposal['sourceInventory'][rel]
            entries[rel] = {'state': 'owned', 'sourceSha256': entry['sha256'], 'currentSha256': entry['sha256'],
                            'mode': action['expected'].get('mode') or (0o666 if os.name == 'nt' else 0o600),
                            'operationId': proposal['operationId']}
    ownership_rel = target.name + '/.core-ownership-v1.json'
    if uninstall:
        changes[ownership_rel] = None
    else:
        record = {'schema': lifecycle.OWNERSHIP_SCHEMA,
                  'installationId': (proposal['ownership'] or {}).get('installationId', uuid.uuid4().hex),
                  'installedRelease': lifecycle.CORE_RELEASE, 'source': str(SOURCE), 'entries': entries, 'updatedAt': now()}
        stamp = lifecycle.source_stamp(SOURCE)
        if stamp:
            record.update(sourceCommit=stamp['commit'], sourceDirty=stamp['dirty'])
        record['recordDigest'] = lifecycle._digest(record)
        changes[ownership_rel] = canonical_json(record) + '\n'
    return changes


if __name__ == "__main__":
    raise SystemExit(main())
