"""Inspect and atomically activate already-published V2 catalogue versions."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath

from .v2.publish import atomic_publish_pointer, resolve_current
from .image_download import ensure_catalogue_wal


_VERSION_ID = re.compile(r"v2-[0-9a-f]{12}\Z")


class VersionHistoryError(ValueError):
    """A stored version is not safe or complete enough to activate."""


def _version_dir(root: Path, version_id: str) -> Path:
    if not isinstance(version_id, str) or not _VERSION_ID.fullmatch(version_id):
        raise VersionHistoryError("invalid version identifier")
    versions_root = (root / "versions").resolve()
    candidate = versions_root / version_id
    if candidate.is_symlink():
        raise VersionHistoryError("version directory must not be a symbolic link")
    resolved = candidate.resolve()
    if resolved.parent != versions_root or not resolved.is_dir():
        raise VersionHistoryError("version directory not found")
    return resolved


def _edition_index(version_dir: Path) -> Path:
    editions = version_dir / "editions"
    index_path = editions / "index.json"
    if not editions.is_dir() or not index_path.is_file():
        raise VersionHistoryError("edition index missing")
    try:
        document = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VersionHistoryError(f"invalid edition index: {exc}") from exc
    if (not isinstance(document, dict) or document.get("schema_version") != "2.0.0"
            or not isinstance(document.get("editions"), list)):
        raise VersionHistoryError("unsupported or malformed edition index")
    for edition in document["editions"]:
        if not isinstance(edition, dict):
            raise VersionHistoryError("malformed edition entry")
        relative = edition.get("index_json")
        if not isinstance(relative, str) or "\\" in relative or ":" in relative or "\0" in relative:
            raise VersionHistoryError("edition index path missing")
        rel = PurePosixPath(relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise VersionHistoryError("unsafe edition index path")
        target = (editions / Path(*rel.parts)).resolve()
        if not target.is_relative_to(editions.resolve()) or not target.is_file():
            raise VersionHistoryError("edition index target missing or outside editions")
    return index_path


def _quick_check(database: Path) -> None:
    if not database.is_file():
        raise VersionHistoryError("catalogue database missing")
    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            result = connection.execute("PRAGMA quick_check").fetchall()
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            if not {"catalogue_meta", "editions", "printings", "source_records", "image_slots", "source_search"} <= tables:
                raise VersionHistoryError("unsupported catalogue schema")
            version = connection.execute(
                "SELECT value FROM catalogue_meta WHERE key='schema_version'").fetchone()
            if not version or version[0] != "2.0.0":
                raise VersionHistoryError("unsupported catalogue schema version")
    except sqlite3.Error as exc:
        raise VersionHistoryError(f"catalogue quick_check failed: {exc}") from exc
    if result != [("ok",)]:
        raise VersionHistoryError("catalogue quick_check failed")


def _directory_stats(version_dir: Path) -> tuple[int, float]:
    """Return byte count and newest mtime without reading file contents."""
    total = 0
    newest = version_dir.stat().st_mtime
    for path in version_dir.rglob("*"):
        if path.is_symlink():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            total += stat.st_size
            newest = max(newest, stat.st_mtime)
    return total, newest


def list_version_candidates(catalogue_root: Path) -> list[dict]:
    """List published version directories; structural checks only, no DB scan."""
    root = Path(catalogue_root).resolve()
    versions_root = root / "versions"
    try:
        active_id = resolve_current(root)["version_id"]
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        active_id = None
    if not versions_root.is_dir():
        return []
    candidates = []
    for path in versions_root.iterdir():
        if not _VERSION_ID.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
            continue
        item = {"version_id": path.name, "active": path.name == active_id,
                "eligible": False, "size_bytes": 0, "modified_at": None, "error": None}
        try:
            version_dir = _version_dir(root, path.name)
            database = version_dir / "catalogue.sqlite"
            if not database.is_file():
                raise VersionHistoryError("catalogue database missing")
            _edition_index(version_dir)
            size, modified = _directory_stats(version_dir)
            item.update(eligible=True, size_bytes=size, modified_at=modified)
        except (OSError, VersionHistoryError) as exc:
            item["error"] = str(exc)
        candidates.append(item)
    candidates.sort(key=lambda item: (item["modified_at"] or 0, item["version_id"]), reverse=True)
    return candidates


def prune_old_versions(catalogue_root: Path, keep_previous: int = 2) -> list[str]:
    """Keep the active catalogue and the newest published rollback versions."""
    if keep_previous < 0:
        raise ValueError("keep_previous must be nonnegative")
    root = Path(catalogue_root).resolve()
    active_id = resolve_current(root)["version_id"]
    versions_root = (root / "versions").resolve()
    candidates = []
    for path in versions_root.iterdir():
        if not _VERSION_ID.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
            continue
        if (getattr(path.lstat(), "st_file_attributes", 0) & 0x400
                or path.resolve().parent != versions_root or path.name == active_id):
            continue
        if not (path / "catalogue.sqlite").is_file():
            continue
        try:
            _edition_index(path)
        except (OSError, VersionHistoryError):
            continue
        candidates.append(path)
    candidates.sort(key=lambda path: (path.stat().st_ctime, path.name), reverse=True)
    removed = []
    for path in candidates[keep_previous:]:
        # Recheck the pointer immediately before each irreversible removal.
        if resolve_current(root)["version_id"] == path.name:
            continue
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


def activate_version(catalogue_root: Path, version_id: str) -> dict:
    """Validate a retained version and atomically make its DB and exports active."""
    root = Path(catalogue_root).resolve()
    version_dir = _version_dir(root, version_id)
    database = version_dir / "catalogue.sqlite"
    editions = version_dir / "editions"
    _edition_index(version_dir)
    _quick_check(database)
    ensure_catalogue_wal(database)
    try:
        current = resolve_current(root)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        current = None
    if current is not None and current["version_id"] == version_id:
        return {"status": "already_active", "version_id": version_id}
    document = atomic_publish_pointer(
        root, version_id,
        f"versions/{version_id}/catalogue.sqlite",
        f"versions/{version_id}/editions",
    )
    return {"status": "activated", "version_id": version_id, "pointer": document}
