"""Local media enrichment and safe image export for preconstructed decks."""
from __future__ import annotations

import re
import json
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from PIL import Image


SECTIONS = ("mainboard", "sideboard", "commander", "display_commander",
            "planar_deck", "scheme_deck", "tokens")
_WINDOWS_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


NON_PLAYABLE_SECTIONS = frozenset({"tokens", "display_commander", "planar_deck", "scheme_deck"})


def composition_colors(cards, catalog_db=None):
    """Collect actual card colors, looking up older snapshots without colors."""
    colors = set()
    missing = set()
    known = False
    for card in cards:
        values = card.get("colors_key")
        if values is None:
            values = (card.get("raw") or {}).get("colors")
        if values is not None:
            known = True
            colors.update(values)
        elif catalog_db and card.get("provider_card_id"):
            missing.add(card["provider_card_id"])
    if missing:
        conn, _ = _catalog(catalog_db)
        try:
            for identifier in missing:
                row = conn.execute("SELECT colors_key FROM source_records WHERE provider_card_id=?",
                                   (identifier,)).fetchone()
                if row and row["colors_key"] is not None:
                    known = True
                    colors.update(row["colors_key"])
        finally:
            conn.close()
    return [color for color in "WUBRG" if color in colors] if known else None


def _catalog(catalog_db: Path):
    catalog_db = Path(catalog_db).resolve()
    if not catalog_db.is_file():
        raise FileNotFoundError(f"Catalogue introuvable : {catalog_db}")
    conn = sqlite3.connect(f"file:{catalog_db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn, catalog_db.parent


def _printing(conn: sqlite3.Connection, provider_id: str, expected_set: str | None = None) -> dict[str, Any] | None:
    row = conn.execute("""SELECT p.printing_id,p.layout,e.code AS set_code,sr.mana_value,sr.colors_key,
        COALESCE(fr.printed_name,fr.name) AS printed_name
        FROM source_records sr JOIN printings p USING(printing_id)
        JOIN editions e USING(set_id)
        LEFT JOIN source_records fr ON fr.printing_id=p.printing_id AND fr.lang='fr'
        WHERE sr.provider_card_id=? AND e.eligibility='eligible'
          AND (? IS NULL OR upper(e.code)=upper(?))
        ORDER BY CASE WHEN fr.provider_card_id IS NOT NULL THEN 0 ELSE 1 END,sr.provider_card_id LIMIT 1""",
        (str(provider_id), expected_set, expected_set)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["faces"] = [dict(item) for item in conn.execute("""SELECT f.face_index,s.status,a.relative_path
        FROM card_faces f LEFT JOIN image_slots s USING(printing_id,face_index)
        LEFT JOIN image_assets a USING(asset_id) WHERE f.printing_id=? AND f.physical_image=1
        ORDER BY f.face_index""", (row["printing_id"],))]
    return result


def _valid_asset(version: Path, relative: str | None) -> bool:
    if not relative:
        return False
    candidate = (version / relative).resolve()
    try:
        candidate.relative_to(version.resolve())
    except ValueError:
        return False
    if not candidate.is_file():
        return False
    try:
        with Image.open(candidate) as im:
            im.verify()
        return True
    except (OSError, ValueError):
        return False


def enrich_deck_media(deck: dict[str, Any], catalog_db: Path) -> dict[str, Any]:
    """Return a copied deck with identity-bound French text, mana and image state."""
    enriched = {**deck}
    sections = {key: [dict(card) for card in cards]
                for key, cards in (deck.get("sections") or {}).items() if isinstance(cards, list)}
    if "tokens" not in sections and isinstance(deck.get("tokens"), list):
        sections["tokens"] = [dict(card) if isinstance(card, dict) else {"name": str(card)} for card in deck["tokens"]]
    all_cards = [card for cards in sections.values() for card in cards]
    conn, version = _catalog(catalog_db)
    try:
        for card in all_cards:
            printing = _printing(conn, card.get("provider_card_id"), card.get("set_code")) if card.get("provider_card_id") else None
            card["image_path"] = None
            card["image_ready"] = False
            card["image_status"] = "unresolved" if not printing else "missing"
            card["image_faces"] = []
            card["mana_value"] = None
            card["printed_name"] = None
            card["printed_name_status"] = "unavailable"
            if not printing:
                continue
            card["mana_value"] = printing["mana_value"]
            card["colors_key"] = printing["colors_key"]
            if printing["printed_name"]:
                card["printed_name"] = printing["printed_name"]
                card["printed_name_status"] = "available"
            faces = printing["faces"]
            valid_faces = []
            for face in faces:
                ready = face["status"] == "ready" and _valid_asset(version, face["relative_path"])
                valid_faces.append(ready)
                card["image_faces"].append({"face_index": face["face_index"],
                    "status": "ready" if ready else "missing",
                    "path": face["relative_path"] if ready else None})
            card["image_ready"] = bool(valid_faces) and all(valid_faces)
            card["image_status"] = "ready" if card["image_ready"] else "missing"
            if faces and valid_faces[0]:
                card["image_path"] = faces[0]["relative_path"]
    finally:
        conn.close()
    enriched["sections"] = sections
    return enriched


def _safe_name(value: Any) -> str:
    text = _WINDOWS_BAD.sub("_", str(value or "Carte")).strip().rstrip(".")
    text = re.sub(r"\s+", " ", text)
    text = text or "Carte"
    # Windows reserves these device names even when they have an extension.
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", text):
        text = "_" + text
    return text


def _unique_path(folder: Path, stem: str) -> Path:
    candidate = folder / f"{stem}.png"
    index = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({index}).png"
        index += 1
    return candidate


def _deck_title(deck: dict[str, Any]) -> str:
    return str(deck.get("name_fr") or deck.get("name") or "Deck")


def double_faced_export_name(conn: sqlite3.Connection, provider_id: str, fallback: str) -> str:
    """Prefer the printed French front-face name of this exact printing."""
    row = conn.execute("""SELECT json_extract(fr.raw_json, '$.card_faces[0].printed_name'),
        fr.printed_name, sr.name
        FROM source_records sr LEFT JOIN source_records fr
        ON fr.printing_id=sr.printing_id AND fr.lang='fr'
        WHERE sr.provider_card_id=?
        ORDER BY fr.provider_card_id LIMIT 1""", (provider_id,)).fetchone()
    name = (row[0] or row[1] or row[2]) if row else fallback
    return (name or fallback).split(" // ", 1)[0]


def export_face_suffix(face_index: int, count: int) -> str:
    if count == 2:
        return " (recto)" if face_index == 0 else " (verso)"
    return f" (face {face_index + 1})" if count > 1 else ""


def export_deck_to_desktop(deck: dict[str, Any], catalog_db: Path,
                           desktop: Path | None = None, *, proxy_lookup=None) -> dict[str, Any]:
    """Export all physical faces as PNG, refusing incomplete decks before writing."""
    if desktop is None:
        desktop = Path.home() / "Desktop"
    desktop = Path(desktop).expanduser().resolve()
    enriched = enrich_deck_media(deck, catalog_db)
    conn, version = _catalog(catalog_db)
    todo: list[tuple[Path, Path, str, int]] = []
    try:
        for section, cards in enriched.get("sections", {}).items():
            token_section = section.casefold() == "tokens"
            for card in cards:
                provider_id = card.get("provider_card_id")
                linked = proxy_lookup(provider_id) if proxy_lookup and card.get("set_code") == "proxy" else None
                if linked:
                    current, path = linked
                    card.update(current)
                    if not _valid_asset(path.parent, path.name):
                        raise ValueError(f"Image du proxy indisponible pour {card.get('name', 'carte')}")
                    sources = [(0, path)]
                else:
                    printing = _printing(conn, provider_id, card.get("set_code")) if provider_id else None
                    if not printing:
                        raise ValueError(f"Impression introuvable pour {card.get('name', 'carte')}")
                    faces = printing["faces"]
                    if not faces or any(face["status"] != "ready" or not _valid_asset(version, face["relative_path"]) for face in faces):
                        raise ValueError(f"Images incomplètes pour {card.get('printed_name') or card.get('name')}: toutes les faces physiques doivent être prêtes")
                    sources = [(face["face_index"], (version / face["relative_path"]).resolve()) for face in faces]
                name = str(card.get("printed_name") or card.get("name") or "Carte")
                if len(sources) == 2:
                    name = double_faced_export_name(conn, provider_id, name)
                mana = 0 if token_section else card.get("mana_value")
                mana_label = str(int(mana)) if isinstance(mana, (int, float)) and float(mana).is_integer() else str(mana if mana is not None else 0)
                quantity = int(card.get("count", card.get("quantity", 1)) or 1)
                if quantity < 1:
                    raise ValueError(f"Quantité invalide pour {name}")
                for copy_index in range(1, quantity + 1):
                    for face_index, source in sources:
                        suffix = export_face_suffix(face_index, len(sources))
                        if quantity > 1:
                            suffix += f" ({copy_index})"
                        stem = _safe_name(f"{mana_label} - {name}{suffix}")
                        output_subdir = Path("Jetons") if token_section else Path()
                        todo.append((source, output_subdir, stem, face_index))
    finally:
        conn.close()
    if not todo:
        raise ValueError("Le deck ne contient aucune impression à exporter")
    desktop.mkdir(parents=True, exist_ok=True)
    folder_base = _safe_name(_deck_title(deck))
    folder = desktop / folder_base
    index = 2
    while folder.exists():
        folder = desktop / f"{folder_base} ({index})"
        index += 1
    folder.mkdir()
    files: list[str] = []
    try:
        for source, subdir, stem, _face_index in todo:
            destination_dir = folder / subdir
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = _unique_path(destination_dir, stem)
            with Image.open(source) as image:
                image.convert("RGBA").save(destination, format="PNG")
            files.append(str(destination))
    except Exception:
        # The new folder is exclusively ours; do not leave a partial export behind.
        import shutil
        shutil.rmtree(folder)
        raise
    return {"folder": str(folder), "files": files, "total": len(files)}


def deck_cover_paths(deck_ids: list[str], deck_db: Path,
                     catalog_db: Path) -> dict[str, str | None]:
    """Map deck IDs to the first commander's ready face-0 relative image path."""
    ids = list(dict.fromkeys(str(deck_id) for deck_id in deck_ids))
    result: dict[str, str | None] = {deck_id: None for deck_id in ids}
    if not ids or not Path(deck_db).is_file():
        return result

    placeholders = ",".join("?" for _ in ids)
    deck_uri = f"file:{Path(deck_db).resolve().as_posix()}?mode=ro"
    with sqlite3.connect(deck_uri, uri=True) as decks:
        commanders = decks.execute(f"""SELECT c.deck_id,c.raw_json
            FROM deck_cards c JOIN (
                SELECT deck_id,MIN(ordinal) AS ordinal FROM deck_cards
                WHERE section='commander' AND deck_id IN ({placeholders}) GROUP BY deck_id
            ) first ON first.deck_id=c.deck_id AND first.ordinal=c.ordinal
            WHERE c.section='commander'""", ids).fetchall()

    if not commanders:
        return result
    conn, version = _catalog(catalog_db)
    try:
        for deck_id, raw_json in commanders:
            try:
                card = json.loads(raw_json)
            except (TypeError, json.JSONDecodeError):
                continue
            provider_id = card.get("provider_card_id") or card.get("providerCardId")
            identifiers = card.get("identifiers") or {}
            scryfall_id = card.get("scryfall_id") or identifiers.get("scryfallId")
            set_code = card.get("setCode") or card.get("set_code") or card.get("set")
            matches = []
            if provider_id:
                matches = conn.execute("""SELECT p.printing_id,sr.provider_card_id,sr.lang
                    FROM source_records sr JOIN printings p USING(printing_id)
                    JOIN editions e USING(set_id)
                    WHERE sr.provider_card_id=? AND e.eligibility='eligible'
                      AND (? IS NULL OR upper(e.code)=upper(?))""",
                    (str(provider_id), set_code, set_code)).fetchall()
            elif scryfall_id:
                matches = conn.execute("""SELECT p.printing_id,sr.provider_card_id,sr.lang
                    FROM source_records sr JOIN printings p USING(printing_id)
                    JOIN editions e USING(set_id)
                    WHERE sr.provider_card_id=? AND e.eligibility='eligible'
                      AND (? IS NULL OR upper(e.code)=upper(?))
                    ORDER BY CASE sr.lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 ELSE 2 END,sr.provider_card_id""",
                    (str(scryfall_id), set_code, set_code)).fetchall()
            if not matches:
                number = card.get("number") or card.get("collector_number")
                if set_code and number:
                    matches = conn.execute("""SELECT p.printing_id,sr.provider_card_id,sr.lang
                        FROM source_records sr JOIN printings p USING(printing_id)
                        JOIN editions e USING(set_id)
                        WHERE upper(e.code)=upper(?) AND p.collector_number=?
                          AND e.eligibility='eligible'
                        ORDER BY CASE sr.lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 ELSE 2 END,sr.provider_card_id""",
                        (str(set_code), str(number))).fetchall()
            printing_ids = {row[0] for row in matches}
            if len(printing_ids) != 1 or not matches:
                continue
            printing = _printing(conn, str(matches[0][1]))
            if printing and printing["faces"]:
                face = printing["faces"][0]
                if face["face_index"] == 0 and face["status"] == "ready" and _valid_asset(version, face["relative_path"]):
                    result[str(deck_id)] = face["relative_path"]
    finally:
        conn.close()
    return result
