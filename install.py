"""Install the Agentic Living Memory package into its own per-user folder, independent of any harness.

Windows: %LOCALAPPDATA%/agentic-living-memory/app. Elsewhere: ~/.local/lib/agentic-living-memory/app.
Plans, journals and the ownership record live beside app/ in that folder. Each harness (Claude Code, Codex,
Copilot, Factory) is wired afterwards with integrations/setup.py HARNESS --hooks, run from the installed copy.
Older releases installed into CODEX_HOME/agentic-living-memory; --retire-legacy-plan removes that copy once
no harness points at it."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from core.installer_lifecycle import apply_plan as apply_lifecycle_plan
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


def harness_home(harness: str) -> Path:
    return Path(os.environ.get(harness.upper() + "_HOME", Path.home() / ("." + harness))).expanduser()


def harness_configs() -> dict:
    """Each harness's hook configuration file, where it exists."""
    names = {"claude": "settings.json", "codex": "hooks.json", "copilot": "hooks/agentic-living-memory.json", "factory": "hooks.json"}
    return {h: harness_home(h) / name for h, name in names.items() if (harness_home(h) / name).is_file()}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def legacy_references() -> list:
    """Harnesses whose hook configuration still runs scripts from the legacy package folder."""
    marker = os.path.normcase(str(LEGACY_DIR) + os.sep)
    found = []
    for harness, path in harness_configs().items():
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            found.append(f"{harness} ({path}: unreadable, so it cannot be checked)")
            continue
        if any(marker in os.path.normcase(text) for text in _strings(config)):
            found.append(f"{harness} ({path})")
    return found


def next_steps() -> list:
    setup = INSTALL_DIR / "integrations" / "setup.py"
    homes = [h for h in HARNESSES if harness_home(h).is_dir()]
    return [f'"{sys.executable}" "{setup}" {h} --hooks' for h in homes]


def main() -> int:
    import argparse
    from core.governance import apply
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--location', type=Path, help='Shared memory folder for unattended first-time setup')
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--apply-plan')
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--uninstall-plan', action='store_true', help='Remove the package (unwire each harness first)')
    parser.add_argument('--retire-legacy-plan', action='store_true', help=f'Remove the old copy in {LEGACY_DIR}')
    args = parser.parse_args()
    if not (args.uninstall_plan or args.apply_plan or args.retire_legacy_plan):
        from core.storage import ensure_setup
        ensure_setup(args.location)
    if args.apply_plan:
        if not args.expected_plan_sha256:
            parser.error('--expected-plan-sha256 is required')
        # Package plans live in INSTALL_HOME; legacy retirement plans in CODEX_HOME.
        root = INSTALL_HOME if (INSTALL_HOME / '.plans' / f'{args.apply_plan}.json').is_file() else CODEX_HOME
        result = apply(root, args.apply_plan, args.expected_plan_sha256)
    elif args.retire_legacy_plan:
        result = plan_retire()
    else:
        proposed = plan_install(args.uninstall_plan)
        result = proposed if args.plan or args.uninstall_plan else apply(INSTALL_HOME, proposed['plan_id'], proposed['digest'])
    print(json.dumps(result, separators=(',', ':')))
    return 0


def plan_install(uninstall=False):
    from core.repository import store_lock
    from core.governance import plan
    INSTALL_HOME.mkdir(parents=True, exist_ok=True)
    with store_lock(INSTALL_HOME):
        changes = _package_changes(INSTALL_DIR, uninstall)
        summary = {'package': str(INSTALL_DIR), 'files': len(changes), 'interpreter': sys.executable}
        if uninstall:
            summary['before'] = 'Unwire each harness first: setup.py HARNESS --uninstall-plan from the installed copy.'
        else:
            summary['next_steps'] = next_steps()
        return plan(INSTALL_HOME, changes, 'package_uninstall' if uninstall else 'package_install', summary=summary)


def plan_retire():
    """Remove the files the legacy CODEX_HOME copy owns, once nothing runs from it."""
    from core.repository import store_lock
    from core.governance import plan
    if not (LEGACY_DIR / '.core-ownership-v1.json').is_file():
        raise ValueError(f'No legacy package at {LEGACY_DIR}')
    if not (INSTALL_DIR / '.core-ownership-v1.json').is_file():
        raise ValueError(f'Install the package at {INSTALL_DIR} before retiring the legacy copy')
    still = legacy_references()
    if still:
        raise ValueError('Legacy package still in use by: ' + '; '.join(still) +
                         '. Run integrations/setup.py HARNESS --hooks from the new copy for each, apply, then retry.')
    with store_lock(CODEX_HOME):
        changes = _package_changes(LEGACY_DIR, True)
        removed = {k for k, v in changes.items() if v is None}
        kept = sorted(p.relative_to(LEGACY_DIR).as_posix() for p in LEGACY_DIR.rglob('*')
                      if p.is_file() and (Path(LEGACY_DIR.name) / p.relative_to(LEGACY_DIR)).as_posix() not in removed)
        untouched = [str(p) for p in (CODEX_HOME / 'agentic-living-memory-data', CODEX_HOME / '.agentic-living-memory.lifecycle') if p.exists()]
        summary = {'retire': str(LEGACY_DIR), 'files': len(removed), 'not_owned_left_in_place': kept[:50],
                   'untouched': untouched, 'note': 'Only files the old install owns and that are unchanged are removed.'}
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
        record['recordDigest'] = lifecycle._digest(record)
        changes[ownership_rel] = canonical_json(record) + '\n'
    return changes


if __name__ == "__main__":
    raise SystemExit(main())
