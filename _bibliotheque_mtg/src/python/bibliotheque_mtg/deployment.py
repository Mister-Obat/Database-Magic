from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

from .indexing import sha256_bytes
from .publication import (FileLock, InjectedFailure, InvalidPublication,
                          PublicationError, StorageFailure, atomic_write,
                          safe_relative_path)


class DeploymentError(PublicationError):
    """A validated generation could not be safely deployed to the library."""


def _target_path(root: Path, relative: str) -> Path:
    """Resolve a safe relative target while rejecting symlinks and escapes."""
    relative = safe_relative_path(relative)
    target = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    if current.is_symlink():
        raise InvalidPublication(f"Library root must not be a symlink: {root}")
    for component in PurePosixPath(relative).parts:
        current = current / component
        if current.is_symlink():
            raise InvalidPublication(f"Deployment path contains a symlink: {relative}")
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise InvalidPublication(f"Deployment path escapes the library: {relative}")
    if len(str(resolved)) > 240:
        raise InvalidPublication(f"Deployment path exceeds the Windows path budget: {relative}")
    return target


def _read_marker(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise DeploymentError(f"Library publication marker is not a file: {path}")
    try:
        raw = path.read_bytes()
        marker = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"Existing library publication marker is unreadable: {path}: {exc}") from exc
    if (not isinstance(marker, dict) or marker.get("deployed_by") != "bibliotheque_mtg"
            or not isinstance(marker.get("generation_id"), str)
            or not isinstance(marker.get("manifest_sha256"), str)):
        raise DeploymentError(f"Refusing to replace an unrecognized library marker: {path}")
    return raw


def _verify_image(relative: str, payload: bytes) -> None:
    if PurePosixPath(relative).suffix.casefold() != ".png":
        return
    try:
        from io import BytesIO
        with Image.open(BytesIO(payload)) as image:
            image.verify()
        with Image.open(BytesIO(payload)) as image:
            if image.format != "PNG" or image.size != (531, 738):
                raise DeploymentError(
                    f"Managed PNG must be 531x738: {relative} ({image.format}, {image.size})")
    except (UnidentifiedImageError, OSError) as exc:
        raise DeploymentError(f"Managed PNG cannot be decoded: {relative}: {exc}") from exc


def deploy_generation(publisher, library_root: str | Path, ops_root: str | Path,
                      generation_id: str | None = None,
                      fail_after_first_write: bool = False) -> dict:
    """Deploy only a fully validated immutable generation to the historical library.

    Existing originals are never overwritten. Existing index documents can be replaced
    after being backed up. Conflicting managed images fail preflight. The marker is the
    final write and a failed deployment is rolled back when its outputs are still intact.
    ``fail_after_first_write`` exists only for deterministic rollback tests.
    """
    library = Path(library_root).resolve()
    operations = Path(ops_root).resolve()
    if not library.is_dir() or not operations.is_dir():
        raise DeploymentError("Library and operations roots must already exist")
    lock_path = operations / "locks" / "library-deploy.lock"
    marker_path = _target_path(library, "publication.json")
    with FileLock(lock_path):
        validated = publisher.validate_current(check_images=True)
        active_id = validated["generation_id"]
        if generation_id is not None and active_id != generation_id:
            raise DeploymentError(
                f"Requested generation {generation_id!r} is not the validated active generation {active_id!r}")
        manifest = validated["manifest"]
        generation_dir = (publisher.generations / safe_relative_path(active_id)).resolve()
        if not generation_dir.is_relative_to(publisher.generations.resolve()):
            raise InvalidPublication("Generation path escapes the staging root")

        payloads: list[tuple[str, str, bytes, Path]] = []
        seen: set[str] = set()
        for row in manifest["files"]:
            relative = safe_relative_path(row.get("path", ""))
            if relative.casefold() == "publication.json" or relative.casefold() in seen:
                raise InvalidPublication(f"Duplicate or reserved deployment path: {relative}")
            seen.add(relative.casefold())
            source = (generation_dir / relative).resolve()
            if not source.is_relative_to(generation_dir) or not source.is_file():
                raise InvalidPublication(f"Staged deployment file is missing or unsafe: {relative}")
            payload = source.read_bytes()
            if len(payload) != row.get("bytes") or sha256_bytes(payload) != row.get("sha256"):
                raise InvalidPublication(f"Staged deployment hash differs: {relative}")
            _verify_image(relative, payload)
            target = _target_path(library, relative)
            payloads.append((relative, row.get("kind", ""), payload, target))

        old_marker = _read_marker(marker_path)
        previous: dict[Path, bytes | None] = {}
        identical: set[str] = set()
        for relative, kind, payload, target in payloads:
            if target.exists():
                if not target.is_file():
                    raise DeploymentError(f"Deployment target is not a regular file: {relative}")
                old = target.read_bytes()
                if old == payload:
                    identical.add(relative)
                    continue
                if kind != "json" and kind != "markdown":
                    raise DeploymentError(f"Conflicting non-index library file; refusing overwrite: {relative}")
                if PurePosixPath(relative).name.casefold() not in {"index.json", "index.md"}:
                    raise DeploymentError(f"Conflicting non-index document; refusing overwrite: {relative}")
                previous[target] = old
            else:
                previous[target] = None
        previous[marker_path] = old_marker

        backup_root = operations / "backups" / "library-deploy" / f"{active_id}-{uuid.uuid4().hex[:8]}"
        backup_entries = []
        old_files = [(path, data) for path, data in previous.items() if data is not None]
        if old_files:
            if backup_root.exists():
                raise DeploymentError(f"Backup directory already exists: {backup_root}")
            backup_root.mkdir(parents=True, exist_ok=False)
            for target, data in old_files:
                relative = target.relative_to(library).as_posix()
                backup_path = _target_path(backup_root, relative)
                atomic_write(backup_path, data)
                backup_entries.append({"path": relative, "sha256": sha256_bytes(data), "bytes": len(data)})
            backup_manifest = {"generation_id": active_id, "created_at_utc":
                               datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                               "files": backup_entries}
            atomic_write(backup_root / "backup.json",
                         (json.dumps(backup_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))

        root_indexes_last = {"index.md", "index.json"}
        ordered = sorted(payloads, key=lambda item: (
            2 if item[0].casefold() in root_indexes_last else (0 if item[1] == "artifact" else 1),
            item[0].casefold()))
        written: list[tuple[Path, bytes | None, bytes]] = []
        marker_payload: bytes | None = None
        try:
            for relative, _kind, payload, target in ordered:
                if relative in identical:
                    continue
                old = previous[target]
                if target.exists() and target.read_bytes() != old:
                    raise DeploymentError(f"Target changed after preflight; refusing write: {relative}")
                if not target.exists() and old is not None:
                    raise DeploymentError(f"Target disappeared after preflight: {relative}")
                atomic_write(target, payload)
                written.append((target, old, payload))
                if fail_after_first_write:
                    fail_after_first_write = False
                    raise InjectedFailure("injected failure after first library file")

            for relative, _kind, payload, target in payloads:
                if not target.is_file() or target.read_bytes() != payload:
                    raise StorageFailure(f"Deployed file failed hash/content verification: {relative}")
            index_records = {}
            for relative, _kind, payload, _target in payloads:
                if PurePosixPath(relative).name.casefold() in {"index.json", "index.md"}:
                    index_records[relative] = {"sha256": sha256_bytes(payload), "bytes": len(payload)}
            marker = {"schema_version": "1.0.0", "deployed_by": "bibliotheque_mtg",
                      "generation_id": active_id, "manifest_sha256": validated["manifest_sha256"],
                      "deployed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                      "indexes": index_records, "managed_files": len(payloads)}
            marker_payload = (json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if marker_path.exists() and marker_path.read_bytes() != old_marker:
                raise DeploymentError("Library marker changed after preflight")
            if not marker_path.exists() and old_marker is not None:
                raise DeploymentError("Library marker disappeared after preflight")
            atomic_write(marker_path, marker_payload)
            if marker_path.read_bytes() != marker_payload:
                raise StorageFailure("Library publication marker failed verification")
        except BaseException as exc:
            rollback_errors = []
            for target, old, deployed in reversed(written):
                try:
                    if not target.is_file() or target.read_bytes() != deployed:
                        rollback_errors.append(f"changed during rollback: {target}")
                    elif old is None:
                        target.unlink()
                    else:
                        atomic_write(target, old)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{target}: {rollback_exc}")
            try:
                if marker_payload is not None and marker_path.is_file() and marker_path.read_bytes() == marker_payload:
                    if old_marker is None:
                        marker_path.unlink()
                    else:
                        atomic_write(marker_path, old_marker)
            except OSError as rollback_exc:
                rollback_errors.append(f"{marker_path}: {rollback_exc}")
            if rollback_errors:
                raise DeploymentError(f"Deployment failed ({exc}); rollback incomplete: {rollback_errors}") from exc
            raise

        return {"status": "deployed", "generation_id": active_id,
                "manifest_sha256": validated["manifest_sha256"], "library_root": str(library),
                "managed_files": len(payloads), "new_or_replaced": len(written),
                "identical_skipped": len(identical), "backed_up": len(backup_entries),
                "backup_root": str(backup_root) if backup_entries else None,
                "marker_path": str(marker_path)}
