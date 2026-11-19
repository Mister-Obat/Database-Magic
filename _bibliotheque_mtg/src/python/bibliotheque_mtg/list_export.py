"""Export a completed pasted list as one desktop folder of card PNGs."""
from __future__ import annotations

import io
import shutil
from pathlib import Path

from PIL import Image, ImageOps

from .deck_media import _safe_name, _valid_asset
from .images import OUTPUT_SIZE
from .v2.query import connect


def _mana_label(value) -> str:
    number = float(value or 0)
    return str(int(number)) if number.is_integer() else str(number)


def _png(source: Path | None, *, normalize=False) -> bytes:
    output = io.BytesIO()
    if source is None:
        image = Image.new("RGB", (531, 738), "white")
    else:
        with Image.open(source) as original:
            image = ImageOps.exif_transpose(original).convert("RGBA") if normalize else original.convert("RGBA")
    try:
        if normalize:
            with ImageOps.pad(image, OUTPUT_SIZE, method=Image.Resampling.LANCZOS, color=(0, 0, 0, 0)) as canvas:
                canvas.save(output, format="PNG")
        else:
            image.save(output, format="PNG")
        return output.getvalue()
    finally:
        image.close()


def export_list_to_desktop(items: list[dict], catalog_db: Path,
                           desktop: Path | None = None) -> dict:
    if not items:
        raise ValueError("Aucune carte traitée à exporter.")
    desktop = Path(desktop) if desktop is not None else Path.home() / "Desktop"
    desktop = desktop.expanduser().resolve()
    version = Path(catalog_db).resolve().parent
    prepared = []
    with connect(catalog_db) as conn:
        for item in items:
            name, mana, faces = item["input"], 0, [(0, None)]
            provider_id = item.get("provider_card_id")
            if provider_id:
                row = conn.execute("""SELECT sr.printing_id,sr.mana_value,sr.name,
                    COALESCE(fr.printed_name,fr.name) AS french_name
                    FROM source_records sr LEFT JOIN source_records fr
                    ON fr.printing_id=sr.printing_id AND fr.lang='fr'
                    WHERE sr.provider_card_id=? ORDER BY fr.provider_card_id LIMIT 1""",
                    (provider_id,)).fetchone()
                if row:
                    name = row["french_name"] or row["name"] or name
                    mana = row["mana_value"]
                    slots = conn.execute("""SELECT s.face_index,s.status,a.relative_path
                        FROM image_slots s LEFT JOIN image_assets a USING(asset_id)
                        WHERE s.printing_id=? ORDER BY s.face_index""", (row["printing_id"],)).fetchall()
                    if slots:
                        faces = [(slot["face_index"], version / slot["relative_path"]
                                  if slot["status"] == "ready" and _valid_asset(version, slot["relative_path"])
                                  else None) for slot in slots]
            prepared.append((name, mana, faces))
    desktop.mkdir(parents=True, exist_ok=True)
    target = desktop / "Export cartes Magic"
    index = 2
    while target.exists():
        target = desktop / f"Export cartes Magic ({index})"
        index += 1
    count = blanks = 0
    used = set()
    target.mkdir()
    try:
        for name, mana, faces in prepared:
            for face_index, source in faces:
                suffix = f" (face {face_index + 1})" if len(faces) > 1 else ""
                stem = _safe_name(f"{_mana_label(mana)} - {name}{suffix}")
                candidate = stem + ".png"
                duplicate = 2
                while candidate.casefold() in used:
                    candidate = f"{stem} ({duplicate}).png"
                    duplicate += 1
                used.add(candidate.casefold())
                (target / candidate).write_bytes(_png(source))
                count += 1
                blanks += source is None
    except Exception:
        shutil.rmtree(target)
        raise
    return {"folder": str(target), "total": count, "blank": blanks}


def export_card_to_desktop(provider_card_id: str, face_index: int, catalog_db: Path,
                           desktop: Path | None = None) -> dict:
    """Copy one ready face to the persistent desktop export folder."""
    version = Path(catalog_db).resolve().parent
    with connect(catalog_db) as conn:
        row = conn.execute("""SELECT sr.printing_id,sr.mana_value,sr.name,
            COALESCE(fr.printed_name,fr.name) AS french_name
            FROM source_records sr LEFT JOIN source_records fr
            ON fr.printing_id=sr.printing_id AND fr.lang='fr'
            WHERE sr.provider_card_id=? ORDER BY fr.provider_card_id LIMIT 1""",
            (provider_card_id,)).fetchone()
        if row is None:
            raise ValueError("Carte introuvable dans le catalogue.")
        slots = conn.execute("""SELECT s.face_index,s.status,a.relative_path
            FROM image_slots s LEFT JOIN image_assets a USING(asset_id)
            WHERE s.printing_id=? ORDER BY s.face_index""", (row["printing_id"],)).fetchall()
    slot = next((item for item in slots if item["face_index"] == face_index), None)
    if slot is None or slot["status"] != "ready" or not _valid_asset(version, slot["relative_path"]):
        raise ValueError("Image locale indisponible pour cette face.")
    suffix = f" (face {face_index + 1})" if len(slots) > 1 else ""
    return export_local_card_image(version / slot["relative_path"],
                                   (row["french_name"] or row["name"]) + suffix, row["mana_value"], desktop)


def export_local_card_image(image_path: Path, name: str, mana_value, desktop: Path | None = None,
                            *, normalize=False) -> dict:
    desktop = Path(desktop) if desktop is not None else Path.home() / "Desktop"
    target = desktop.expanduser().resolve() / "Cartes exportées"
    target.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(f"{_mana_label(mana_value)} - {name}")
    candidate = target / f"{stem}.png"
    duplicate = 2
    while candidate.exists():
        candidate = target / f"{stem} ({duplicate}).png"
        duplicate += 1
    candidate.write_bytes(_png(image_path, normalize=normalize))
    return {"path": str(candidate), "folder": str(target)}
