"""Browseable, durable edition folders for the active catalogue's ready images."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

from .paths import catalogue_root
from .v2.publish import resolve_current


_BACKUP_SUFFIX = re.compile(r" - face ([1-9][0-9]*) - ([0-9a-f]{16})\.webp$")
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(value: str, limit: int) -> str:
    name = _BAD.sub("_", str(value or "")).strip(" .")[:limit].rstrip(" .") or "Sans nom"
    if name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    return name


def _images_root(version: Path) -> Path:
    publication = version.parent.parent
    if publication.name != "v3-staging":
        return publication / "Images"
    ops = publication.parent
    if ops.name == "work":
        ops = ops.parent
    return ops / "Images"


_SLOT_ROW_SQL = """SELECT p.printing_id,p.set_id,p.collector_number,e.code,e.name,
        s.face_index,s.status,a.asset_id,a.relative_path,
        COALESCE((SELECT COALESCE(NULLIF(sr.printed_name,''),sr.name) FROM source_records sr
                  WHERE sr.printing_id=p.printing_id ORDER BY CASE sr.lang WHEN 'fr' THEN 0 ELSE 1 END LIMIT 1),'Carte') AS card_name
        FROM printings p JOIN editions e USING(set_id)
        JOIN image_slots s USING(printing_id) LEFT JOIN image_assets a USING(asset_id)"""
_READY_ROW_SQL = _SLOT_ROW_SQL + " WHERE s.status='ready' AND a.asset_id IS NOT NULL"


def _row(conn: sqlite3.Connection, printing_id: str, face_index: int):
    return conn.execute(_READY_ROW_SQL + " AND p.printing_id=? AND s.face_index=?",
                        (printing_id, face_index)).fetchone()


def _printing_identity(printing_id: str) -> str:
    return hashlib.sha256(printing_id.encode("utf-8")).hexdigest()[:16]


def _organized_path(root: Path, row: sqlite3.Row) -> Path:
    folder = _safe_name(f'{row["code"].upper()} - {row["name"]}', 100)
    stem = _safe_name(f'{row["collector_number"]} - {row["card_name"]}', 110)
    identity = _printing_identity(row["printing_id"])
    return root / folder / f'{stem} - face {row["face_index"] + 1} - {identity}.webp'


def _mirror_row(version: Path, row: sqlite3.Row) -> Path:
    relative = Path(row["relative_path"])
    source = (version / relative).resolve()
    if not source.is_relative_to(version.resolve()):
        raise ValueError(f"Chemin d'image invalide : {relative}")
    destination = _organized_path(_images_root(version), row)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        expected = row["asset_id"].rsplit("_", 1)[-1]
        if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Image active absente : {relative}")
        source.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(destination, source)
        except OSError:
            shutil.copy2(destination, source)
        return destination
    if destination.is_file() and os.path.samefile(source, destination):
        return destination
    expected = row["asset_id"].rsplit("_", 1)[-1]
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise ValueError(f"Image active corrompue : {relative}")
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def mirror_printing_face(db: Path, version: Path, printing_id: str, face_index: int) -> Path:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = _row(conn, printing_id, face_index)
    if row is None:
        raise ValueError("Image prête introuvable dans le catalogue")
    return _mirror_row(version, row)


def _backup_images(root: Path) -> set[Path]:
    if not root.is_dir() or root.is_symlink() or root.resolve() != root:
        return set()
    files = set()
    with os.scandir(root) as folders:
        for folder in folders:
            if not folder.is_dir(follow_symlinks=False) or Path(folder.path).resolve() != Path(folder.path):
                continue
            with os.scandir(folder.path) as entries:
                files.update(Path(entry.path) for entry in entries
                             if _BACKUP_SUFFIX.search(entry.name) and entry.is_file(follow_symlinks=False))
    return files


def _read_backup(source: Path) -> bytes | None:
    max_bytes = 20 * 1024 * 1024
    try:
        if source.resolve() == source:
            with source.open("rb") as stream:
                data = stream.read(max_bytes + 1)
            return data if len(data) <= max_bytes else None
    except (OSError, RuntimeError):
        pass
    return None


def _write_asset(output: Path, data: bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".webp.restore")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _asset_output(version: Path, relative: str) -> Path:
    output = version / relative
    if not output.resolve().is_relative_to(version):
        raise ValueError("Chemin d’image restaurée invalide")
    return output


def _adopt_backup_file(conn: sqlite3.Connection, version: Path, source: Path) -> tuple[str, str, int] | None:
    data = _read_backup(source)
    if data is None:
        return None
    try:
        with Image.open(BytesIO(data), formats=["WEBP"]) as image:
            if image.size != (531, 738) or image.n_frames != 1:
                return None
            image.load()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None

    digest = hashlib.sha256(data).hexdigest()
    asset = conn.execute("SELECT relative_path FROM image_assets WHERE asset_id=?", (digest,)).fetchone()
    relative = asset["relative_path"] if asset else f"assets/webp/und/{digest}.webp"
    output = _asset_output(version, relative)
    if not output.is_file() or hashlib.sha256(output.read_bytes()).hexdigest() != digest:
        _write_asset(output, data)
    return digest, relative, len(data)


def _restore_ready_file(version: Path, row: sqlite3.Row, source: Path) -> bool:
    relative = row["relative_path"]
    if not relative or (version / relative).is_file():
        return False
    output = _asset_output(version, relative)
    data = _read_backup(source)
    if data is not None and hashlib.sha256(data).hexdigest() == row["asset_id"].rsplit("_", 1)[-1]:
        _write_asset(output, data)
        return True
    return False


def _adopt_edition(conn: sqlite3.Connection, version: Path, manifest: Path,
                   candidates: list[tuple[sqlite3.Row, Path]]) -> int:
    original = manifest.read_bytes()
    doc = json.loads(original)
    slots = {(slot["printing_id"], slot["face_index"]): slot for slot in doc["slots"]}
    changes = []
    for row, source in candidates:
        key = row["printing_id"], row["face_index"]
        if key not in slots:
            continue
        file = _adopt_backup_file(conn, version, source)
        if file is not None:
            changes.append((key, file))
    if not changes:
        return 0

    temp = manifest.with_suffix(".json.restore")
    replaced = False
    adopted = 0
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for key, (digest, relative, size) in changes:
                provenance = json.dumps({
                    "provider": "organized_backup", "source_sha256": digest,
                    "source_dimensions": [531, 738], "output_dimensions": [531, 738],
                    "selected_language": "und", "published_at": datetime.now(timezone.utc).timestamp(),
                }, sort_keys=True)
                conn.execute("INSERT OR IGNORE INTO image_assets VALUES(?,?,?,?,?,?,?,?)",
                             (digest, relative, "image/webp", size, 531, 738, "und", provenance))
                updated = conn.execute("""UPDATE image_slots SET status='ready',asset_id=?,selected_language='und',
                                provenance_json=? WHERE printing_id=? AND face_index=? AND status='missing'""",
                             (digest, provenance, *key))
                if updated.rowcount:
                    slots[key].update(status="ready", selected_language="und", path=relative)
                    adopted += 1
            temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, manifest)
            replaced = True
    except Exception:
        if replaced:
            temp.write_bytes(original)
            os.replace(temp, manifest)
        raise
    finally:
        temp.unlink(missing_ok=True)
    return adopted


def restore_organized_images(catalogue_root: Path) -> int:
    from .image_download import _edition_images_manifest

    db = resolve_current(catalogue_root)["database"]
    version = db.parent
    root = _images_root(version)
    available = _backup_images(root)
    if not available:
        return 0
    restored = 0
    existing_assets = {}
    with closing(sqlite3.connect(db, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        backups = {}
        for path in available:
            suffix = _BACKUP_SUFFIX.search(path.name)
            code, separator, _ = path.parent.name.partition(" - ")
            if suffix and separator:
                face, identity = suffix.groups()
                backups.setdefault((code.lower(), face, identity), []).append(path)
        if not backups:
            return 0
        codes = sorted({code for code, _, _ in backups})
        placeholders = ",".join("?" for _ in codes)
        matches = {}
        for row in conn.execute(
            "SELECT p.printing_id,p.set_id,p.collector_number,s.face_index,s.status,e.code,a.asset_id,a.relative_path "
            "FROM printings p JOIN editions e USING(set_id) JOIN image_slots s USING(printing_id) "
            "LEFT JOIN image_assets a USING(asset_id) "
            f"WHERE e.eligibility='eligible' AND e.code IN ({placeholders})", codes
        ):
            key = (row["code"].lower(), str(row["face_index"] + 1), _printing_identity(row["printing_id"]))
            if key in backups:
                matches.setdefault(key, []).append(row)
        editions = {}
        for key, rows in matches.items():
            if len(rows) != 1:
                continue
            row = rows[0]
            prefix = _safe_name(row["collector_number"], 110) + " - "
            paths = [path for path in backups[key] if path.name.startswith(prefix)]
            if len(paths) != 1:
                continue
            if row["status"] == "ready" and row["relative_path"]:
                output = version / row["relative_path"]
                if output.parent not in existing_assets:
                    try:
                        with os.scandir(output.parent) as entries:
                            existing_assets[output.parent] = {entry.name for entry in entries if entry.is_file()}
                    except FileNotFoundError:
                        existing_assets[output.parent] = set()
                names = existing_assets[output.parent]
                if output.name not in names and _restore_ready_file(version, row, paths[0]):
                    names.add(output.name)
                    restored += 1
            elif row["status"] == "missing":
                editions.setdefault(row["set_id"], []).append((row, paths[0]))
        for set_id, matches in editions.items():
            try:
                manifest = _edition_images_manifest(version, set_id)
                restored += _adopt_edition(conn, version, manifest, matches)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                logging.warning("Restauration des images ignorée pour l’édition %s.", set_id, exc_info=True)
    return restored


def synchronize_organized_images(catalogue_root: Path) -> dict[str, int]:
    current = resolve_current(catalogue_root)
    db, version = current["database"], current["database"].parent
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        ready_count = conn.execute("SELECT COUNT(*) FROM image_slots WHERE status='ready'").fetchone()[0]
        rows = conn.execute(_READY_ROW_SQL + " ORDER BY p.printing_id,s.face_index").fetchall()
    counts = {"ready": ready_count, "organized": 0, "missing": ready_count - len(rows)}
    for row in rows:
        if row is None:
            counts["missing"] += 1
            continue
        try:
            _mirror_row(version, row)
            counts["organized"] += 1
        except (OSError, ValueError):
            counts["missing"] += 1
    return counts


if __name__ == "__main__":
    root = catalogue_root(Path(__file__).resolve().parents[3])
    print(json.dumps(synchronize_organized_images(root), ensure_ascii=False))
