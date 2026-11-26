"""Transactional core installer.

The planner is deliberately conservative: it only overwrites files that it
created on an earlier run or files whose current hash is an explicit
recognized predecessor. Every apply produces a receipt and content backups;
rollback checks the post-apply state before restoring anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from .common import file_lock, memory_root
except ImportError:  # Permit `py core/installer_lifecycle.py ...` from the repo root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.common import file_lock, memory_root

PROTOCOL_VERSION = 1
CORE_RELEASE = "1.0.0"
OWNERSHIP_SCHEMA = "living-memory-core-ownership-v1"
PLAN_SCHEMA = "living-memory-core-plan-v1"
RECEIPT_SCHEMA = "living-memory-core-install-receipt-v1"
OPERATION_RE = re.compile(r"^[0-9a-f]{32}$")
SOFTWARE_ROOTS = ("core", "scripts", "hooks", "integrations", "adapters", "assets")
ROOT_DOCUMENTS = (
    "README.md", "INSTALLATION.md", "INTEGRATION.md", "MAINTENANCE.md",
    "OBSIDIAN.md", "PDF.md", "TAGGING.md", "VALIDATION.md", "AGENTS.md",
    "bindings.example.json", "tag_taxonomy.example.json", "install.py",
    "new_note.py", "resolve_note.py", "living_memory.py", "PROTOCOL.md",
    "ARCHITECTURE.md", "SCRIPT_REFERENCE.md", "RECOVERY.md", "NOTE_TYPES.md", "NOTE_MAINTENANCE.md", "NOTEBOOKS.md",
    "WRITING.md", "templates/CLAUDE.md", "templates/AGENTS.md", "templates/GEMINI.md",
)
SKIP_NAMES = {".git", ".agents", ".codex-plugin", "__pycache__", ".DS_Store"}
SKIP_SUFFIXES = {".pyc", ".db", ".sqlite", ".sqlite3", ".lock"}
MAX_FILES = 256
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024


class InstallError(ValueError):
    code = "invalid"
    exit_code = 2

    def __init__(self, message: str, operation_id: str | None = None):
        super().__init__(message)
        self.operation_id = operation_id


class PathSafetyError(InstallError):
    code = "unsafe-path"
    exit_code = 3


class ConflictError(InstallError):
    code = "conflict"
    exit_code = 5


class StalePlanError(InstallError):
    code = "stale-plan"
    exit_code = 6
    retryable = True


class RecoveryRequiredError(InstallError):
    code = "recovery-required"
    exit_code = 7
    retryable = True


def _git(root: str | Path, *args: str) -> str | None:
    """stdout of `git -C root ARGS`, or None when git is missing or the command fails."""
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def source_stamp(source: str | Path) -> dict[str, Any]:
    """{'commit', 'dirty'} of a source checkout (dirty is None when git status fails), or {} when it is not a git checkout of its own or git is missing."""
    top = _git(source, "rev-parse", "--show-toplevel")
    commit = _git(source, "rev-parse", "HEAD")
    if not top or not commit or os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(source)):
        return {}
    status = _git(source, "status", "--porcelain")
    return {"commit": commit, "dirty": None if status is None else bool(status)}


def installed_stamp(root: str | Path | None = None) -> dict[str, Any] | None:
    """What the installed copy at root (default: this code's folder) records about itself, or None for a copy with no ownership record."""
    try:
        record = json.loads((Path(root or Path(__file__).resolve().parents[1]) / ".core-ownership-v1.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return {"release": record.get("installedRelease"), "commit": record.get("sourceCommit"), "dirty": record.get("sourceDirty"), "source": record.get("source"), "interpreter": record.get("interpreter")}


def source_warning(stamp: dict[str, Any] | None) -> str | None:
    """A doctor warning when the source checkout's HEAD is newer than the commit the install was made from."""
    if not stamp or not stamp.get("commit") or not stamp.get("source"):
        return None
    head = source_stamp(stamp["source"]).get("commit")
    if not head or head == stamp["commit"] or _git(stamp["source"], "merge-base", "--is-ancestor", stamp["commit"], head) is None:
        return None
    return f"install: the source checkout {stamp['source']} is at {head[:12]}, newer than the installed copy ({stamp['commit'][:12]}); run alm upgrade (or install.py from it) to update"


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_relative(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or raw.startswith("/"):
        raise PathSafetyError("Lifecycle path is not a confined relative path")
    p = PurePosixPath(raw)
    if any(part in {"", ".", ".."} for part in p.parts) or p.is_absolute():
        raise PathSafetyError("Lifecycle path is not a confined relative path")
    return p.as_posix()


def _path(root: Path, relative: str) -> Path:
    relative = _safe_relative(relative)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    absolute_root = Path(os.path.abspath(root))
    absolute_candidate = Path(os.path.abspath(candidate))
    try:
        absolute_candidate.relative_to(absolute_root)
    except ValueError as exc:
        raise PathSafetyError("Lifecycle path escapes its target root") from exc
    return absolute_candidate


def _read_regular(path: Path, expected: os.stat_result | None = None) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PathSafetyError(f"Lifecycle file is unsafe or unavailable: {path.name}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise PathSafetyError(f"Lifecycle file is not regular and singly linked: {path.name}")
        if expected is not None and (before.st_dev, before.st_ino, before.st_size, stat.S_IMODE(before.st_mode)) != (
            expected.st_dev, expected.st_ino, expected.st_size, stat.S_IMODE(expected.st_mode)
        ):
            raise StalePlanError("Lifecycle file changed during inspection")
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise StalePlanError("Lifecycle file changed while being read")
        return b"".join(chunks), after
    finally:
        os.close(fd)


def _check_ancestors(path: Path, stop: Path) -> None:
    current = Path(os.path.abspath(path))
    stop = Path(os.path.abspath(stop))
    chain = []
    while current != stop:
        chain.append(current)
        if current.parent == current:
            raise PathSafetyError("Lifecycle path has an unsafe ancestor")
        current = current.parent
    for item in reversed(chain):
        try:
            mode = item.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise PathSafetyError("Lifecycle path has a symbolic-link ancestor")
        if item != path and not stat.S_ISDIR(mode):
            raise PathSafetyError("Lifecycle path has a non-directory ancestor")


def _snapshot(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"exists": False, "type": None, "size": None, "sha256": None, "mode": None}
    if stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1:
        raise PathSafetyError(f"Lifecycle target is not a regular, singly-linked file: {path.name}")
    if not stat.S_ISREG(metadata.st_mode):
        raise PathSafetyError(f"Lifecycle target is not a regular file: {path.name}")
    data, after = _read_regular(path, metadata)
    if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise StalePlanError("Lifecycle file changed during inspection")
    return {
        "exists": True,
        "type": "file",
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "links": metadata.st_nlink,
        "size": metadata.st_size,
        "sha256": _sha(data),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _same_snapshot(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    if not expected.get("exists"):
        return not actual.get("exists")
    fields = ("exists", "type", "device", "inode", "links", "size", "sha256", "mode")
    return all(expected.get(k) == actual.get(k) for k in fields)


def _payload(source: Path) -> dict[str, dict[str, Any]]:
    source = source.resolve(strict=True)
    paths: list[Path] = []
    for name in ROOT_DOCUMENTS:
        p = source / name
        if p.is_file():
            paths.append(p)
    for root_name in SOFTWARE_ROOTS:
        top = source / root_name
        if not top.exists():
            continue
        if top.is_symlink():
            raise PathSafetyError(f"Source directory is a symbolic link: {root_name}")
        paths.extend(p for p in top.rglob("*") if p.is_file())
    result: dict[str, dict[str, Any]] = {}
    total = 0
    for p in sorted(set(paths)):
        rel = p.relative_to(source).as_posix()
        if any(part in SKIP_NAMES for part in PurePosixPath(rel).parts) or p.suffix.lower() in SKIP_SUFFIXES:
            continue
        if p.is_symlink():
            raise PathSafetyError(f"Source file is a symbolic link: {rel}")
        meta = p.stat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
            raise PathSafetyError(f"Source file is unsafe: {rel}")
        if meta.st_size > MAX_FILE_BYTES:
            raise InstallError(f"Source file exceeds the 4 MiB limit: {rel}")
        content, after = _read_regular(p, meta)
        if (meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ) or len(content) != meta.st_size:
            raise StalePlanError(f"Source file changed while reading: {rel}")
        result[rel] = {"sha256": _sha(content), "size": len(content), "mode": stat.S_IMODE(meta.st_mode)}
        total += len(content)
    if not result or len(result) > MAX_FILES or total > MAX_PAYLOAD_BYTES:
        raise InstallError("Core payload exceeds file-count or total-size limits")
    return result


def _operation_root(target: Path, operation_id: str) -> Path:
    if not OPERATION_RE.fullmatch(operation_id):
        raise InstallError("Operation ID is invalid")
    return target.parent / f".{target.name}.lifecycle" / "operations" / operation_id


def _ownership_path(target: Path) -> Path:
    return target / ".core-ownership-v1.json"


def _read_ownership(target: Path) -> dict[str, Any] | None:
    path = _ownership_path(target)
    if not path.exists():
        return None
    snapshot = _snapshot(path)
    if not snapshot["exists"] or snapshot["size"] > 1024 * 1024:
        raise ConflictError("Core ownership manifest is unavailable")
    try:
        content, _ = _read_regular(path)
        record = json.loads(content.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConflictError("Core ownership manifest is invalid") from exc
    if not isinstance(record, dict):
        raise ConflictError("Core ownership manifest failed integrity validation")
    unsigned = {k: v for k, v in record.items() if k != "recordDigest"}
    if (record.get("schema") != OWNERSHIP_SCHEMA
            or record.get("recordDigest") != _digest(unsigned)
            or not isinstance(record.get("entries"), dict)):
        raise ConflictError("Core ownership manifest failed integrity validation")
    return record


def _classify(target: Path, source_files: dict[str, dict[str, Any]], ownership: dict[str, Any] | None,
              uninstall: bool) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    old_entries = ownership.get("entries", {}) if ownership else {}
    names = sorted(set(old_entries) if uninstall else set(source_files) | set(old_entries))
    actions: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    for rel in names:
        path = _path(target, rel)
        _check_ancestors(path, target)
        current = _snapshot(path)
        source = source_files.get(rel)
        old = old_entries.get(rel)
        action = "preserve"
        reason = None
        if uninstall:
            if old and old.get("state") == "owned":
                if not current.get("exists"):
                    action = "forget"
                elif current.get("sha256") == old.get("currentSha256") and current.get("mode") == old.get("mode"):
                    action = "remove"
                else:
                    action, reason = "conflict", "owned-file-changed"
        elif not current["exists"]:
            action = "create" if source is not None else "forget" if old else "preserve"
        elif old and old.get("state") == "owned" and current.get("sha256") == old.get("currentSha256"):
            if source is None:
                action = "remove"
            else:
                action = "keep" if current.get("sha256") == source["sha256"] else "replace"
        elif source and current.get("sha256") == source["sha256"]:
            action = "adopt"
        elif old:
            action, reason = "conflict", "existing-target-is-not-owned"
        elif source:
            action, reason = "conflict", "existing-target-would-be-overwritten"
        item = {
            "path": rel, "action": action, "expected": current,
            "sourceSha256": None if source is None else source["sha256"],
            "mode": None if source is None else source["mode"],
            "ownershipState": None if old is None else old.get("state"),
        }
        actions.append(item)
        if reason:
            blockers.append({"path": rel, "code": reason})
    return actions, blockers


def build_plan(target: str | Path, source: str | Path | None = None, uninstall: bool = False) -> dict[str, Any]:
    source_root = Path(source or Path(__file__).resolve().parents[1]).resolve(strict=True)
    requested_target = Path(os.path.abspath(Path(target).expanduser()))
    # Ancestors resolved once (macOS /var and /tmp are links to /private/...); the target itself stays as named, so a link there is refused.
    target_root = Path(os.path.realpath(requested_target.parent)) / requested_target.name
    if os.path.islink(target_root) or getattr(os.path, "isjunction", lambda p: False)(target_root):
        raise PathSafetyError("Installation target is a symbolic link")
    # Overlap checks use the target's real path: an existing leaf may be a short (8.3) or other alias of the store.
    target_real = Path(os.path.realpath(target_root))
    source_abs = source_root.resolve()
    try:
        source_abs.relative_to(target_real)
        overlaps = True
    except ValueError:
        try:
            target_real.relative_to(source_abs)
            overlaps = True
        except ValueError:
            overlaps = False
    if overlaps:
        raise PathSafetyError("Installation target overlaps the source tree")
    try:
        live_store = Path(os.path.realpath(memory_root()))
    except ValueError:
        live_store = None  # No store configured yet: nothing the target could alias.
    if live_store is not None and (target_real == live_store or target_real in live_store.parents or live_store in target_real.parents):
        raise PathSafetyError("Installation target aliases or overlaps the live memory store")
    if target_root.exists() and target_root.is_symlink():
        raise PathSafetyError("Installation target is a symbolic link")
    if not target_root.parent.exists():
        raise PathSafetyError("Installation target parent must already exist")
    _check_ancestors(target_root, target_root.anchor and Path(target_root.anchor) or target_root.parent)
    ownership = _read_ownership(target_root)
    source_files = {} if uninstall else _payload(source_root)
    actions, blockers = _classify(target_root, source_files, ownership, uninstall)
    provisional = {
        "schema": PLAN_SCHEMA,
        "protocolVersion": PROTOCOL_VERSION,
        "operation": "uninstall" if uninstall else "install",
        "target": str(target_root),
        "source": str(source_root),
        "sourceInventory": source_files,
        "ownershipExpected": _snapshot(_ownership_path(target_root)),
        "ownership": ownership,
        "actions": actions,
        "blockers": blockers,
    }
    operation_basis = {k: v for k, v in provisional.items() if k != "operationId"}
    provisional["operationId"] = uuid.uuid5(
        uuid.NAMESPACE_URL,
        "living-memory-core:" + _digest(operation_basis),
    ).hex
    provisional["planSha256"] = _digest(provisional)
    return provisional


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:
            os.chmod(temp, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _read_source(source_root: Path, rel: str, expected_hash: str) -> bytes:
    path = _path(source_root, rel)
    _check_ancestors(path, source_root)
    content, _ = _read_regular(path)
    if _sha(content) != expected_hash:
        raise StalePlanError(f"Reviewed source changed: {rel}")
    return content


def _load_receipt(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if metadata.st_size > 1024 * 1024:
            raise InstallError("Lifecycle receipt exceeds its size limit")
        raw, _ = _read_regular(path, metadata)
        receipt = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError("Lifecycle receipt is unavailable or invalid") from exc
    unsigned = {k: v for k, v in receipt.items() if k != "receiptDigest"}
    if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("receiptDigest") != _digest(unsigned):
        raise InstallError("Lifecycle receipt failed integrity validation")
    return receipt


def status(target: str | Path, operation_id: str) -> dict[str, Any]:
    target = Path(target).expanduser().resolve()
    root = _operation_root(target, operation_id)
    receipt_path = root / "receipt.json"
    if receipt_path.exists():
        receipt = _load_receipt(receipt_path)
        return {"operationId": operation_id, "state": receipt["state"],
                "receiptDigest": receipt["receiptDigest"], "receipt": str(receipt_path)}
    journal = root / "operation.json"
    if journal.exists():
        value = json.loads(journal.read_text(encoding="utf-8"))
        return {"operationId": operation_id, "state": value.get("state", "unknown"), "receipt": None}
    return {"operationId": operation_id, "state": "unknown", "receipt": None}


def _restore_effects(target: Path, operation_root: Path, effects: list[dict[str, Any]], require_after: bool = True) -> None:
    # Preflight the entire rollback before performing the first restoration.
    for effect in effects:
        path = _path(target, effect["path"])
        _check_ancestors(path, target)
        if require_after and effect.get("after") is not None and not _same_snapshot(effect["after"], _snapshot(path)):
            raise StalePlanError(f"Rollback is stale for {effect['path']}")
        if effect.get("before", {}).get("exists"):
            if not effect.get("backup"):
                raise InstallError("Rollback backup is missing")
            data, _ = _read_regular(_path(operation_root, effect["backup"]))
            if _sha(data) != effect["before"]["sha256"]:
                raise InstallError("Rollback backup failed integrity validation")
    for effect in reversed(effects):
        rel = _safe_relative(effect["path"])
        path = _path(target, rel)
        _check_ancestors(path, target)
        current = _snapshot(path)
        after = effect.get("after")
        if require_after and after is not None and not _same_snapshot(after, current):
            raise StalePlanError(f"Rollback is stale for {rel}")
        if not effect.get("before", {}).get("exists"):
            if current.get("exists"):
                path.unlink()
            continue
        backup_rel = effect.get("backup")
        if not backup_rel:
            raise InstallError(f"Rollback backup is missing for {rel}")
        backup = _path(operation_root, backup_rel)
        data, _ = _read_regular(backup)
        before = effect["before"]
        if _sha(data) != before.get("sha256"):
            raise InstallError(f"Rollback backup failed integrity validation for {rel}")
        _atomic_write(path, data, before.get("mode") or 0o600)


def rollback(receipt_path: str | Path) -> dict[str, Any]:
    receipt_path = Path(receipt_path).expanduser().resolve(strict=True)
    receipt = _load_receipt(receipt_path)
    target = Path(receipt["target"])
    operation_root = receipt_path.parent
    with file_lock(operation_root.parent / ".lifecycle.lock"):
        if receipt.get("state") == "rolled-back":
            return {"operationId": receipt["operationId"], "state": "rolled-back", "alreadyRolledBack": True}
        if receipt.get("state") != "committed":
            raise InstallError("Lifecycle receipt state is not rollback-eligible")
        from core.repository import transaction, recover
        rollback_id = "rollback-" + receipt["operationId"]
        durable = target.parent / ".operations" / rollback_id / "journal.json"
        if durable.exists():
            recover(target.parent, rollback_id)
        else:
            changes = {}
            expected = {}
            for effect in receipt["effects"]:
                path = _path(target, effect["path"])
                if not _same_snapshot(effect["after"], _snapshot(path)):
                    raise StalePlanError(f"Rollback is stale for {effect['path']}")
                relative = path.relative_to(target.parent).as_posix()
                expected[relative] = effect["after"].get("sha256")
                data = None
                if effect["before"]["exists"]:
                    data, _ = _read_regular(_path(operation_root, effect["backup"]))
                    if _sha(data) != effect["before"]["sha256"]:
                        raise InstallError("Rollback backup failed integrity validation")
                changes[relative] = data
            transaction(target.parent, changes, "package_rollback", expected, rollback_id)
        receipt["state"] = "rolled-back"
        receipt["rolledBackAt"] = _timestamp()
        receipt.pop("receiptDigest", None)
        receipt["receiptDigest"] = _digest(receipt)
        _atomic_write(receipt_path, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
        return {"operationId": receipt["operationId"], "state": "rolled-back", "receiptDigest": receipt["receiptDigest"]}


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps({"protocolVersion": PROTOCOL_VERSION, "ok": True, "data": value}, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    # Only for receipts older installs left beside the legacy copy (RECOVERY.md); install.py installs and uninstalls.
    parser = argparse.ArgumentParser(description="Show or roll back a lifecycle receipt left by an older Living Memory install")
    parser.add_argument("--target", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true")
    group.add_argument("--rollback", type=Path)
    parser.add_argument("--operation-id")
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            result = rollback(args.rollback)
        elif args.status:
            if not args.operation_id:
                raise InstallError("--operation-id is required for status")
            result = status(args.target, args.operation_id)
        _emit(result)
        return 0
    except InstallError as exc:
        error = {"code": exc.code, "message": str(exc), "retryable": bool(getattr(exc, "retryable", False))}
        if exc.operation_id:
            error["operationId"] = exc.operation_id
        print(json.dumps({"protocolVersion": PROTOCOL_VERSION, "ok": False, "error": error}, ensure_ascii=False))
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
