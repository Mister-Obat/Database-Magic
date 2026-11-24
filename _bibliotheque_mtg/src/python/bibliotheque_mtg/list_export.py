"""Export a completed pasted list as one desktop folder of card PNGs."""
from __future__ import annotations

import io
import shutil
from contextlib import closing
from pathlib import Path

from PIL import Image, ImageOps

from .deck_media import _safe_name, _valid_asset, double_faced_export_name, export_face_suffix
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
    """Copy local PNGs for an exact printing to the desktop export folder."""
    version = Path(catalog_db).resolve().parent
    with closing(connect(catalog_db)) as conn:
        row = conn.execute("""SELECT sr.printing_id,sr.mana_value,sr.name,
            COALESCE(fr.printed_name,fr.name) AS french_name
            FROM source_records sr LEFT JOIN source_records fr
            ON fr.printing_id=sr.printing_id AND fr.lang='fr'
            WHERE sr.provider_card_id=? ORDER BY fr.provider_card_id LIMIT 1""",
            (provider_card_id,)).fetchone()
        if row is None:
            raise ValueError("Carte introuvable dans le catalogue.")
        slots = {slot["face_index"]: slot for slot in conn.execute("""
            SELECT f.face_index,s.status,a.relative_path FROM card_faces f
            LEFT JOIN image_slots s USING(printing_id,face_index)
            LEFT JOIN image_assets a USING(asset_id)
            WHERE f.printing_id=? AND f.physical_image=1 ORDER BY f.face_index""", (row["printing_id"],))}
        name = row["french_name"] or row["name"]
        if len(slots) == 2:
            name = double_faced_export_name(conn, provider_card_id, name)
    if face_index not in slots:
        raise ValueError("Image locale indisponible pour cette face.")
    indexes = list(slots) if len(slots) == 2 else [face_index]
    sources = []
    for index in indexes:
        slot = slots[index]
        if slot["status"] != "ready" or not _valid_asset(version, slot["relative_path"]):
            raise ValueError("Les deux images locales doivent être disponibles." if len(indexes) == 2
                             else "Image locale indisponible pour cette face.")
        sources.append((version / slot["relative_path"], name + export_face_suffix(index, len(slots))))
    result = _export_card_images(sources, row["mana_value"], desktop)
    paths = result["paths"]
    return {"path": paths[indexes.index(face_index)], **result}


def export_local_card_image(image_path: Path, name: str, mana_value, desktop: Path | None = None,
                            *, normalize=False) -> dict:
    result = _export_card_images([(image_path, name)], mana_value, desktop, normalize=normalize)
    return {"path": result["paths"][0], "folder": result["folder"]}


def _export_card_images(sources: list[tuple[Path, str]], mana_value,
                        desktop: Path | None, *, normalize=False) -> dict:
    images = [(_safe_name(f"{_mana_label(mana_value)} - {name}"), _png(path, normalize=normalize))
              for path, name in sources]
    desktop = Path(desktop) if desktop is not None else Path.home() / "Desktop"
    target = desktop.expanduser().resolve() / "Cartes exportées"
    target.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for stem, data in images:
            candidate = target / f"{stem}.png"
            duplicate = 2
            while True:
                try:
                    output = candidate.open("xb")
                    break
                except FileExistsError:
                    candidate = target / f"{stem} ({duplicate}).png"
                    duplicate += 1
            written.append(candidate)
            with output:
                output.write(data)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return {"paths": [str(path) for path in written], "folder": str(target)}
