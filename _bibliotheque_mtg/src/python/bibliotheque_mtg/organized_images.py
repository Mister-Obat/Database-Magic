"""Browseable, durable edition folders for the active catalogue's ready images."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
from pathlib import Path

from .paths import catalogue_root
from .v2.publish import resolve_current


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


_READY_ROW_SQL = """SELECT p.printing_id,p.collector_number,e.code,e.name,
        s.face_index,a.asset_id,a.relative_path,
        COALESCE((SELECT COALESCE(NULLIF(sr.printed_name,''),sr.name) FROM source_records sr
                  WHERE sr.printing_id=p.printing_id ORDER BY CASE sr.lang WHEN 'fr' THEN 0 ELSE 1 END LIMIT 1),'Carte') AS card_name
        FROM printings p JOIN editions e USING(set_id)
        JOIN image_slots s USING(printing_id) JOIN image_assets a USING(asset_id)
        WHERE s.status='ready'"""


def _row(conn: sqlite3.Connection, printing_id: str, face_index: int):
    return conn.execute(_READY_ROW_SQL + " AND p.printing_id=? AND s.face_index=?",
                        (printing_id, face_index)).fetchone()


def _mirror_row(version: Path, row: sqlite3.Row) -> Path:
    relative = Path(row["relative_path"])
    source = (version / relative).resolve()
    if not source.is_relative_to(version.resolve()):
        raise ValueError(f"Chemin d'image invalide : {relative}")
    folder = _safe_name(f'{row["code"].upper()} - {row["name"]}', 100)
    stem = _safe_name(f'{row["collector_number"]} - {row["card_name"]}', 110)
    identity = hashlib.sha256(row["printing_id"].encode("utf-8")).hexdigest()[:16]
    destination = _images_root(version) / folder / f'{stem} - face {row["face_index"] + 1} - {identity}.webp'
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
    import json
    root = catalogue_root(Path(__file__).resolve().parents[3])
    print(json.dumps(synchronize_organized_images(root), ensure_ascii=False))
