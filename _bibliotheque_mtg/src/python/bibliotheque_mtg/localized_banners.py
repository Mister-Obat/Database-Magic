"""Detect Scryfall's localized-image banner and replace only confirmed slots."""

import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import requests
from PIL import Image, ImageOps

from .image_download import (
    _MAX_GETS,
    _allowed_url,
    _convertible,
    _fetch,
    _image_mime_type,
    _ordered_uris,
    publish_preview_candidate,
)
from .v2.publish import resolve_current
from .v2.query import connect


# Light glyph strokes and dark gaps in “Localized Image / Not Available”,
# sampled every two pixels from real Scryfall placeholders at 265 × 369.
# Dots are locations where the artwork or antialiasing makes contrast variable.
_GLYPHS = (
    "-.---------------...-----------..--.-------------------",
    "-+---------------...-----------+.--+-------------------",
    "-+---.++..++..++...+.+++-++.-+++.--+-++++++-+++-+++..++",
    "-+---+--++--....+..+--+-+..+..-+.--+-+-..-+.-.+..-+.+..",
    "-+---+--++--.+..+..+-+.-+.....-+.--+-+-..-+...+..-+.+..",
    "-+++.+++..++.++++..+.+++.+.+.+.+.--+-+-..-+.+++-+.+.+.+",
    "-----------------------------------------------..-+----",
    "------------------------------------------------.+.----",
    "-------------------------------------------------------",
    "-------------------------------------------------------",
    "-------------------------------------------------------",
    "-----------.+--+-----..----+.---------+-+-----+---+----",
    "----------..+.-+--...+.---..+-.--.-..-.-+-...-+..-+--..",
    "--------.-...+-+-+.+...---+-+-...++...+-+-+.+-+...+-+.+",
    "------.---.....+..-.+..---+++.-+...++.+-+-.++-+--++-+++",
    ".-...-.......-++-+-....--..--+-++-+-..+-+..-+-+-.++-+-.",
    ".....-........-+-.+.-.+--+---....-.+.+.-+-++...++-.-.++",
)
_NORMALIZED_SIZE = (265, 369)
_REGION = (92, 127, 205, 164)
_WIDTH = _REGION[2] - _REGION[0]
_HEIGHT = _REGION[3] - _REGION[1]
_ORIGIN = (2, 2)
_LIGHT = tuple(
    tuple((2 * y + _ORIGIN[1]) * _WIDTH + 2 * x + _ORIGIN[0]
          for y in rows for x, mark in enumerate(_GLYPHS[y]) if mark == "+")
    for rows in (range(8), range(11, len(_GLYPHS)))
)
_GAPS = tuple(
    (2 * y + _ORIGIN[1]) * _WIDTH + 2 * x + _ORIGIN[0]
    for y in (0, 8, 9, 10) for x, mark in enumerate(_GLYPHS[y]) if mark == "-"
)


def has_localized_banner(image: Image.Image) -> bool:
    if image.getexif().get(274, 1) != 1:
        image = ImageOps.exif_transpose(image)
    source_box = tuple(edge * image.size[i % 2] / _NORMALIZED_SIZE[i % 2]
                       for i, edge in enumerate(_REGION))
    pixels = image.resize((_WIDTH, _HEIGHT), Image.Resampling.BICUBIC,
                          box=source_box).convert("L").tobytes()
    # A small offset covers differences in provider image renditions and padding.
    for dy in (0, -1, 1, -2, 2):
        for dx in (0, -1, 1, -2, 2):
            shift = dy * _WIDTH + dx
            if (all(sum(pixels[p + shift] >= 143 for p in line) >= .68 * len(line) for line in _LIGHT)
                    and sum(pixels[p + shift] <= 80 for p in _GAPS) >= .86 * len(_GAPS)):
                return True
    return False


def _english_sources(conn, printing_id, face_index):
    for row in conn.execute(
            "SELECT provider_card_id,raw_json FROM source_records "
            "WHERE printing_id=? AND lang='en' ORDER BY provider_card_id", (printing_id,)):
        raw = json.loads(row["raw_json"])
        if raw.get("image_status") in {"missing", "placeholder"}:
            continue
        # An image for face zero can live at the root. Later physical faces
        # must have their own image; never substitute the recto for the verso.
        if face_index:
            faces = raw.get("card_faces") or []
            face = faces[face_index] if len(faces) > face_index else None
            if not isinstance(face, dict) or not face.get("image_uris"):
                continue
        urls = list(dict.fromkeys(url for url in _ordered_uris(
            raw, face_index, full_card_only=True, compact=True) if _allowed_url(url)))
        if urls:
            return row["provider_card_id"], urls[:_MAX_GETS]
    return None


def _stored_banner(version, relative_path):
    try:
        path = (version / relative_path).resolve()
        if not path.is_relative_to(version):
            return None
        with Image.open(path) as image:
            return has_localized_banner(image)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def analyze_localized_banners(current, cancel_event, progress):
    db, version = current["database"], current["database"].parent.resolve()
    counts = {"scanned": 0, "detected": 0, "replaceable": 0, "unreadable": 0}
    matches = []
    with closing(connect(db)) as conn, ThreadPoolExecutor(max_workers=8) as pool:
        total = conn.execute("SELECT count(*) FROM image_slots WHERE status='ready'").fetchone()[0]
        rows = conn.execute("""
            SELECT s.printing_id,s.face_index,s.asset_id,a.relative_path,e.eligibility,
                   e.code,p.collector_number
            FROM image_slots s JOIN image_assets a USING(asset_id)
            JOIN printings p USING(printing_id) JOIN editions e USING(set_id)
            WHERE s.status='ready'
        """)
        progress(processed=0, total=total, detected=0)
        while not cancel_event.is_set() and (batch := rows.fetchmany(64)):
            checks = pool.map(lambda row: _stored_banner(version, row["relative_path"]), batch)
            for row, banner in zip(batch, checks):
                if cancel_event.is_set():
                    break
                counts["scanned"] += 1
                if banner is None:
                    counts["unreadable"] += 1
                elif banner:
                    counts["detected"] += 1
                    english = (_english_sources(conn, row["printing_id"], row["face_index"])
                               if row["eligibility"] == "eligible" else None)
                    if english:
                        counts["replaceable"] += 1
                    name = conn.execute("""
                        SELECT coalesce(printed_name,name) FROM source_records
                        WHERE printing_id=? ORDER BY lang not in ('fr','en'), lang!='fr', provider_card_id
                        LIMIT 1
                    """, (row["printing_id"],)).fetchone()
                    matches.append({
                        "printing_id": row["printing_id"], "face_index": row["face_index"],
                        "asset_id": row["asset_id"], "english": english,
                        "name": name[0] if name else row["printing_id"],
                        "edition": row["code"], "collector_number": row["collector_number"],
                        "relative_path": row["relative_path"],
                    })
            progress(processed=counts["scanned"], total=total, detected=counts["detected"])
    matches.sort(key=lambda item: (item["edition"], item["collector_number"], item["face_index"]))
    return counts, matches


def replace_localized_banners(current, matches, catalogue_root, cancel_event, progress, *, on_replaced=None):
    db, version_id = current["database"], current["version_id"]
    version = db.parent.resolve()
    counts = {"replaced": 0, "failed": 0, "skipped": 0, "total": len(matches)}
    progress(processed=0, **counts)
    with requests.Session() as session, closing(connect(db)) as conn:
        for processed, match in enumerate(matches, 1):
            if cancel_event.is_set():
                break
            if resolve_current(catalogue_root)["version_id"] != version_id:
                raise ValueError("La version active a changé. Relancez la détection.")
            printing_id, face_index = match["printing_id"], match["face_index"]
            old_asset_id = match["asset_id"]
            slot = conn.execute("SELECT s.asset_id,a.relative_path FROM image_slots s "
                                "JOIN image_assets a USING(asset_id) "
                                "WHERE s.printing_id=? AND s.face_index=? AND s.status='ready'",
                                (printing_id, face_index)).fetchone()
            if (not slot or slot["asset_id"] != old_asset_id
                    or not _stored_banner(version, slot["relative_path"])):
                counts["skipped"] += 1
            else:
                provider_card_id, urls = match["english"]
                outcome = "failed"
                for url in urls:
                    data, error, _ = _fetch(url, session, cancel_event=cancel_event, batch_rate_limit=True)
                    if error in {"acces_refuse_403", "limite_debit_429"}:
                        counts["failed"] += 1
                        progress(processed=processed, **counts)
                        detail = ("Scryfall a refusé l’accès." if error == "acces_refuse_403" else
                                  "Scryfall a limité le débit. Relancez après le délai d’attente.")
                        return {**counts, "detail": detail}
                    if error:
                        continue
                    try:
                        width, height = _convertible(data)
                        with Image.open(io.BytesIO(data)) as image:
                            if has_localized_banner(image):
                                continue
                    except (OSError, ValueError, Image.DecompressionBombError):
                        continue
                    if cancel_event.is_set():
                        break
                    candidate = {
                        "version_id": version_id, "printing_id": printing_id,
                        "provider_card_id": provider_card_id, "face_index": face_index,
                        "old_status": "ready", "old_asset_id": old_asset_id,
                        "language": "en", "source_url": url, "source_data": data,
                        "source_width": width, "source_height": height,
                        "mime_type": _image_mime_type(data), "source_record_id": provider_card_id,
                        "source_evidence": "replacement_localized_image_banner",
                    }
                    try:
                        published = publish_preview_candidate(
                            candidate, catalogue_root=catalogue_root,
                            provider_card_id=provider_card_id, face_index=face_index)
                    except ValueError as exc:
                        outcome = "skipped" if str(exc).startswith("candidate périmée") else "failed"
                    except (OSError, sqlite3.Error):
                        outcome = "failed"
                    else:
                        outcome = "replaced"
                        if on_replaced is not None:
                            on_replaced({
                                **{key: match[key] for key in
                                   ("name", "edition", "collector_number", "face_index", "printing_id")},
                                "provider_card_id": provider_card_id,
                                "old_path": slot["relative_path"],
                                "new_path": published["relative_path"],
                            })
                    break
                if cancel_event.is_set() and outcome == "failed":
                    break
                counts[outcome] += 1
            progress(processed=processed, **counts)
    return counts
