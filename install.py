"""Install the Agentic Living Memory package into its own per-user folder, independent of any harness.

Windows: %LOCALAPPDATA%/agentic-living-memory/app. Elsewhere: ~/.local/lib/agentic-living-memory/app.
Journals and the ownership record live beside app/ in that folder. Each run is one journaled write whose `i-` operation
ID undo takes (`alm undo i-...` puts the previous package files back). Each harness (Claude Code, Codex, Copilot, Factory,
Antigravity CLI) is wired afterwards with integrations/setup.py HARNESS --hooks, run from the installed copy.
Older releases installed into CODEX_HOME/agentic-living-memory; --retire-legacy removes that copy once
no harness points at it.

The install also puts an `alm` launcher in ~/.local/bin and, when that folder is not on PATH, adds it (receipt
keys `launcher` and `path_change`). Undo leaves both: the launcher runs whatever copy is in app/."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from core.installer_lifecycle import build_plan as build_lifecycle_plan
from core.instructions import CONFIG_FILES as CONFIG_NAMES, HARNESSES, harness_home

SOURCE = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


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
FISH_PROFILE_LINE = 'fish_add_path -g $HOME/.local/bin  # agentic-living-memory'


def default_homes() -> list:
    """(harness, home) for each harness. When CLAUDE_CONFIG_DIR chooses another Claude folder, ~/.claude is listed too:
    hooks an earlier setup left there still run the package."""
    homes = [(h, harness_home(h)) for h in HARNESSES]
    if not os.environ.get("CLAUDE_HOME") and os.environ.get("CLAUDE_CONFIG_DIR"):
        homes.append(("claude", Path.home() / ".claude"))
    return homes


def harness_configs(extra_homes=()) -> list:
    """(harness, hook configuration file) for each default harness home, plus homes given with --home."""
    homes = default_homes() + [(h, Path(p).expanduser()) for h, p in extra_homes]
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
    return f'{_lead()}"{sys.executable}" "{INSTALL_DIR / "integrations" / "setup.py"}" {harness} --uninstall{custom}'


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


def next_steps() -> tuple:
    """(commands, current): the setup.py command for each harness to wire, and the harnesses whose wiring is already current.
    A first install lists every harness found on this machine. An update lists only harnesses wired before whose handlers
    or instruction block the installed copy would change, so a harness the user never wired is left out."""
    from core.installer_lifecycle import installed_stamp
    setup_path = INSTALL_DIR / "integrations" / "setup.py"
    homes = [h for h in HARNESSES if harness_home(h).is_dir()]
    if installed_stamp(INSTALL_DIR) is None:
        return [f'"{sys.executable}" "{setup_path}" {h} --hooks' for h in homes], []
    from core.wiring import needs_rewire
    from integrations import setup
    steps, current = [], []
    for h in homes:
        try:
            state = needs_rewire(h, setup, INSTALL_DIR)
        except Exception:   # cannot tell: list it, as every update did before
            state = True
        if state:
            steps.append(f'"{sys.executable}" "{setup_path}" {h} --hooks')
        elif state is False:
            current.append(h)
    return steps, current


def print_next_steps(summary) -> None:
    """After an install, the wiring commands, ready to paste into PowerShell, on stderr (stdout stays the JSON result)."""
    steps = summary.get('next_steps') or []
    if steps:
        print('Installed. Next, wire each harness: run these.\n' + '\n'.join('  ' + _lead() + step for step in steps), file=sys.stderr)
    elif summary.get('wiring_current'):
        print(f'Installed. Harness wiring is current ({", ".join(summary["wiring_current"])}); nothing to rerun.', file=sys.stderr)


def wire_harnesses(steps, python=None) -> tuple:
    """Run each setup.py wiring step from next_steps(). Returns (wired_harnesses, errors)."""
    import shlex
    wired, errors = [], []
    setup_path = INSTALL_DIR / "integrations" / "setup.py"
    exe = python or sys.executable
    for step in steps:
        tokens = shlex.split(step, posix=os.name != 'nt')
        harness = next((t for t in tokens if t in HARNESSES), None)
        if not harness:
            continue
        argv = [exe, str(setup_path), harness, '--hooks']
        try:
            done = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
            if done.returncode == 0:
                wired.append(harness)
            else:
                msg = ((done.stderr or done.stdout).strip().splitlines() or [f'exit {done.returncode}'])[-1]
                errors.append(f'{harness}: {msg}')
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f'{harness}: {exc}')
    return wired, errors


def unwire_all(extra_homes=(), folder=None) -> tuple:
    """Unwire every harness whose config references `folder` (default INSTALL_DIR). Returns (unwired, errors)."""
    marker = os.path.normcase(str(folder or INSTALL_DIR) + os.sep)
    setup_path = (folder or INSTALL_DIR) / "integrations" / "setup.py"
    unwired, errors = [], []
    for harness, path in harness_configs(extra_homes):
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not any(marker in os.path.normcase(text) for text in _strings(config)):
            continue
        home = path.parents[len(Path(CONFIG_NAMES[harness]).parts) - 1]
        argv = [sys.executable, str(setup_path), harness, '--uninstall']
        if home.resolve() != harness_home(harness).resolve():
            argv += ['--home', str(home)]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
            if done.returncode == 0:
                unwired.append(f'{harness} ({home})')
            else:
                msg = ((done.stderr or done.stdout).strip().splitlines() or [f'exit {done.returncode}'])[-1]
                errors.append(f'{harness} ({home}): {msg}')
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f'{harness} ({home}): {exc}')
    return unwired, errors


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--location', type=Path, help='Shared memory folder for unattended first-time setup')
    parser.add_argument('--wire-all', action='store_true', help='After installing, wire every detected harness that needs wiring')
    parser.add_argument('--uninstall', action='store_true', help='Remove the package (unwire each harness first, or pass --purge)')
    parser.add_argument('--purge', action='store_true', help='With --uninstall: unwire every harness pointing at the installed package first')
    parser.add_argument('--retire-legacy', action='store_true', help=f'Remove the old copy in {LEGACY_DIR}')
    parser.add_argument('--home', action='append', default=[], metavar='HARNESS=PATH',
                        help='A harness home set up with setup.py --home PATH; uninstall and retirement check it too (repeatable)')
    # The upgrader installed before 2026-11-26 runs `--plan`, then `--apply-plan ID --expected-plan-sha256 DIGEST` (its
    # `upgrade --plan` stops after the first). For that one upgrade --plan writes nothing and names a token, and --apply-plan
    # with that token installs directly; neither is offered otherwise.
    parser.add_argument('--plan', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--apply-plan', help=argparse.SUPPRESS)
    parser.add_argument('--expected-plan-sha256', help=argparse.SUPPRESS)
    args = parser.parse_args()
    from core.storage import refuse_agent_setup
    refuse_agent_setup()
    homes = []
    for value in args.home:
        harness, sep, path = value.partition('=')
        if not sep or harness not in HARNESSES or not path:
            parser.error('--home takes HARNESS=PATH, with HARNESS one of ' + ', '.join(HARNESSES))
        homes.append((harness, path))
    if args.uninstall and args.retire_legacy:
        parser.error('--uninstall and --retire-legacy are separate runs')
    if args.purge and not args.uninstall:
        parser.error('--purge goes with --uninstall')
    if args.wire_all and (args.uninstall or args.retire_legacy):
        parser.error('--wire-all cannot be combined with --uninstall or --retire-legacy')
    if args.plan:
        steps, current = next_steps()   # read-only; the old upgrader shows these from this reply only
        summary = {'files': len(_package_changes(INSTALL_DIR, False)), 'next_steps': steps, 'wiring_current': current}
        print(json.dumps({'ok': True, 'plan_id': LEGACY_TOKEN, 'digest': LEGACY_TOKEN, 'summary': summary,
                          'note': 'Install plans were removed on 2026-11-26; nothing was written. Applying this token installs directly.'},
                         separators=(',', ':')))
        return 0
    if args.apply_plan and (args.apply_plan, args.expected_plan_sha256) != (LEGACY_TOKEN, LEGACY_TOKEN):
        parser.error('install plans were removed on 2026-11-26; run install.py with no options')
    if not (args.uninstall or args.retire_legacy):
        from core.storage import ensure_setup
        ensure_setup(args.location)
    if args.retire_legacy:
        result = retire_legacy(homes)
    else:
        unwired = []
        if args.uninstall and args.purge:
            unwired, unwire_errors = unwire_all(homes, INSTALL_DIR)
            if unwire_errors:
                raise ValueError('Failed to unwire before uninstall: ' + '; '.join(unwire_errors))
        result = install(args.uninstall, homes)
        if unwired:
            result['unwired'] = unwired
        if not args.uninstall:
            summary = result.get('summary') or {}
            if args.wire_all and summary.get('next_steps'):
                wired, wire_errors = wire_harnesses(summary['next_steps'])
                result['wired'] = wired
                if wire_errors:
                    result.setdefault('warnings', []).extend('wire failed: ' + e for e in wire_errors)
                else:
                    summary['next_steps'] = []
                    summary['wiring_current'] = sorted(set(summary.get('wiring_current') or []) | set(wired))
            print_next_steps(summary)
    print(json.dumps(result, separators=(',', ':')))
    return 0


LEGACY_TOKEN = 'direct-install'   # see the --plan transition in main()


def _refuse_if_in_use(extra_homes=()):
    still = legacy_references(extra_homes, INSTALL_DIR, True)
    if still:
        raise ValueError('The package is still in use by: ' + '; '.join(still) + '. Run each unwire command, then retry the uninstall.')


def _operation_id():
    import uuid
    return 'i-' + uuid.uuid4().hex


def install(uninstall=False, extra_homes=()):
    """Install (or with uninstall, remove) the package as one journaled write in INSTALL_HOME, then write the launcher and PATH entry."""
    from core.repository import store_lock
    from core.governance import commit
    if uninstall:
        _refuse_if_in_use(extra_homes)
    INSTALL_HOME.mkdir(parents=True, exist_ok=True)
    with store_lock(INSTALL_HOME):
        changes = _package_changes(INSTALL_DIR, uninstall)
        summary = {'package': str(INSTALL_DIR), 'files': len(changes), 'interpreter': sys.executable}
        record = _launcher_step(uninstall)
        change = None
        if uninstall:
            summary['path'] = f'A PATH entry for {LAUNCHER_DIR} is left in place: other programs may live in that folder.'
        else:
            summary['next_steps'], summary['wiring_current'] = next_steps()
            change = _path_step() if record['action'] in ('create', 'replace', 'keep') else None
        receipt = commit(INSTALL_HOME, changes, 'package_uninstall' if uninstall else 'package_install', summary=summary,
                         operation_id=_operation_id(), meta={'launcher': record, 'path_change': change})
        # Still under the install lock, so two installers never share the launcher's .new file or lose each other's PATH
        # entry. Another program can still edit PATH between the read and the write; no lock covers that.
        warning = _write_launcher(record)
        receipt['launcher'] = {'path': record['path'], 'action': record['action'] if not warning else 'failed'}
        if record.get('reason'):
            receipt['launcher']['reason'] = record['reason']
        if warning:
            receipt.setdefault('warnings', []).append(warning)
        elif change:
            if not _path_done(change):
                (_add_to_path_windows if change['kind'] == 'registry' else _add_to_path_posix)(change)
            receipt['path_change'] = change['add']
            receipt['notice'] = 'Open a new terminal (and restart running agent sessions) so `alm` is found.'
            if change['kind'] == 'profile' and sys.platform.startswith('linux') and Path(change['target']).name in ('.profile', '.bash_profile'):
                receipt['notice'] += f' Terminals that start a non-login bash read ~/.bashrc instead; if `alm` is still not found, add this line there: {change["add"]}'
    return receipt


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


def _launcher_step(uninstall):
    """The `alm` launcher this install creates, replaces, keeps or removes. Only a file carrying our marker is ever touched."""
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
    if shell == 'fish':
        xdg = os.environ.get('XDG_CONFIG_HOME', '')
        return Path(xdg if os.path.isabs(xdg) else home / '.config') / 'fish' / 'config.fish'
    zdot = os.environ.get('ZDOTDIR', '')
    return Path(zdot if os.path.isabs(zdot) else home) / '.zprofile' if shell == 'zsh' else home / '.bash_profile' if shell == 'bash' and (home / '.bash_profile').exists() else home / '.profile'


def _path_step():
    """The PATH change that makes the launcher folder reachable, or None when it already is (this process's PATH, the user's registry value or profile)."""
    if _on_path(LAUNCHER_DIR, os.environ.get('PATH', ''), os.pathsep):
        return None
    if os.name == 'nt':
        value, kind = _registry_path()
        if _on_path(LAUNCHER_DIR, value, ';'):
            return None
        return {'kind': 'registry', 'target': 'HKCU\\Environment\\Path', 'before': value, 'before_type': kind, 'add': str(LAUNCHER_DIR)}
    profile = _profile()
    line = FISH_PROFILE_LINE if profile.name == 'config.fish' else PROFILE_LINE
    data = profile.read_bytes() if profile.is_file() else None   # bytes: a CRLF or non-UTF-8 profile is read as it is
    if data is not None and line.encode() in data:
        return None
    return {'kind': 'profile', 'target': str(profile), 'before': _sha(data) if data is not None else None, 'add': line}


def _add_to_path_windows(change):
    import ctypes
    import winreg
    # The value as it is now, not as the install step read it: another installer may have added a folder since.
    value, kind = _registry_path()
    if _on_path(change['add'], value, ';'):
        return
    value = value.rstrip(';')
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, 'Path', 0, kind, (value + ';' if value else '') + change['add'])
    # Tell running programs (Explorer, new terminals) the environment changed.
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, 'Environment', 0x2, 5000, ctypes.byref(ctypes.c_ulong()))


def _add_to_path_posix(change):
    """Append the PATH line through a temp file and a rename, so a crash never leaves a half-written profile. A symlinked
    profile (dotfile managers) is written at its target, so the link stays."""
    import stat
    import tempfile
    profile = Path(change['target']).resolve()
    data = profile.read_bytes() if profile.is_file() else b''
    mode = stat.S_IMODE(profile.stat().st_mode) if profile.is_file() else 0o644
    profile.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + profile.name + '.', dir=profile.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data + (b'' if not data or data.endswith(b'\n') else b'\n') + change['add'].encode() + b'\n')
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, profile)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _path_done(change):
    """True when the PATH entry this change adds is already there (a retry after it was written)."""
    if change['kind'] == 'registry':
        return _on_path(change['add'], _registry_path()[0], ';')
    profile = Path(change['target'])
    return profile.is_file() and change['add'].encode() in profile.read_bytes()


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
    path.unlink(missing_ok=True)   # antivirus may already have removed it
    return f'The launcher {path} did not run ({reason}) and was removed; agents keep using the full command.'


def retire_legacy(extra_homes=()):
    """Remove the files the legacy CODEX_HOME copy owns, once nothing runs from it, as one journaled write in CODEX_HOME.
    Harness homes outside the defaults (setup.py --home PATH) are checked only when passed here, so the receipt lists what it checked."""
    from core.repository import store_lock
    from core.governance import commit
    if not (LEGACY_DIR / '.core-ownership-v1.json').is_file():
        raise ValueError(f'No legacy package at {LEGACY_DIR}')
    if not (INSTALL_DIR / '.core-ownership-v1.json').is_file():
        raise ValueError(f'Install the package at {INSTALL_DIR} before retiring the legacy copy')
    still = legacy_references(extra_homes)
    if still:
        raise ValueError('Legacy package still in use by: ' + '; '.join(still) +
                         '. Run integrations/setup.py HARNESS --hooks from the new copy for each, then retry.')
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
        return commit(CODEX_HOME, changes, 'legacy_package_retire', summary=summary, operation_id=_operation_id())


def _package_changes(target: Path, uninstall: bool) -> dict:
    """Package files and the ownership record, keyed relative to target.parent (the journal root)."""
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
                  'installedRelease': lifecycle.CORE_RELEASE, 'source': str(SOURCE), 'entries': entries, 'updatedAt': now(),
                  'interpreter': sys.executable}   # Hivemind starts ALM with this Python, the one the launcher and hooks use
        stamp = lifecycle.source_stamp(SOURCE)
        if stamp:
            record.update(sourceCommit=stamp['commit'], sourceDirty=stamp['dirty'])
        record['recordDigest'] = lifecycle._digest(record)
        changes[ownership_rel] = canonical_json(record) + '\n'
    return changes


if __name__ == "__main__":
    raise SystemExit(main())
