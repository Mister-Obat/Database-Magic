"""Detect Scryfall's localized-image banner and replace only confirmed slots."""

import io
import json
import sqlite3
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from threading import Event

import requests
from PIL import Image, ImageOps

from .image_download import (
    BATCH_IMAGE_WORKERS,
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
# Increment when the visual detector changes to invalidate prior verdicts.
_DETECTOR_VERSION = 1
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


def _banner_path(version, relative_path):
    path = (version / relative_path).resolve()
    if not path.is_relative_to(version):
        raise ValueError("asset outside catalogue version")
    return path


def _stored_banner(version, relative_path):
    try:
        with Image.open(_banner_path(version, relative_path)) as image:
            return has_localized_banner(image)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _file_signature(path):
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"


def _check_banner(version, relative_path, cached):
    try:
        path = _banner_path(version, relative_path)
        signature = _file_signature(path)
        entry = cached.get(relative_path)
        if entry and entry[0] == signature:
            return bool(entry[1]), None
        with Image.open(path) as image:
            banner = has_localized_banner(image)
        if _file_signature(path) != signature:
            return None, None
        return banner, (relative_path, _DETECTOR_VERSION, signature, int(banner))
    except (OSError, ValueError, Image.DecompressionBombError):
        return None, None


def analyze_localized_banners(current, cancel_event, progress, cache_path):
    db, version = current["database"], current["database"].parent.resolve()
    counts = {"scanned": 0, "detected": 0, "replaceable": 0, "unreadable": 0}
    matches = []
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with (closing(connect(db)) as conn, closing(sqlite3.connect(cache_path)) as cache,
          ThreadPoolExecutor(max_workers=8) as pool):
        cache.execute("""
            CREATE TABLE IF NOT EXISTS banner_verdicts (
                relative_path TEXT PRIMARY KEY, detector_version INTEGER NOT NULL,
                signature TEXT NOT NULL, banner INTEGER NOT NULL
            ) WITHOUT ROWID
        """)
        total = conn.execute("SELECT count(*) FROM image_slots WHERE status='ready'").fetchone()[0]
        rows = conn.execute("""
            SELECT s.printing_id,s.face_index,s.asset_id,a.relative_path,e.eligibility,
                   e.code,p.collector_number
            FROM image_slots s JOIN image_assets a USING(asset_id)
            JOIN printings p USING(printing_id) JOIN editions e USING(set_id)
            WHERE s.status='ready'
        """)
        progress(processed=0, total=total, detected=0)
        while not cancel_event.is_set() and (batch := rows.fetchmany(256)):
            paths = [row["relative_path"] for row in batch]
            placeholders = ",".join("?" for _ in paths)
            cached = {path: (signature, banner) for path, signature, banner in cache.execute(
                f"SELECT relative_path,signature,banner FROM banner_verdicts "
                f"WHERE detector_version=? AND relative_path IN ({placeholders})",
                (_DETECTOR_VERSION, *paths),
            )}
            checks = pool.map(lambda path: _check_banner(version, path, cached), paths)
            updates = []
            for row, (banner, update) in zip(batch, checks):
                if cancel_event.is_set():
                    break
                if update:
                    updates.append(update)
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
            if updates:
                with cache:
                    cache.executemany("INSERT OR REPLACE INTO banner_verdicts VALUES (?,?,?,?)", updates)
            progress(processed=counts["scanned"], total=total, detected=counts["detected"])
    matches.sort(key=lambda item: (item["edition"], item["collector_number"], item["face_index"]))
    return counts, matches


_PROVIDER_STOP_ERRORS = {
    "acces_refuse_403": "Scryfall a refusé l’accès.",
    "limite_debit_429": "Scryfall a limité le débit. Relancez après le délai d’attente.",
}


def _fetch_replacement(match, version_id, stop):
    provider_card_id, urls = match["english"]
    with requests.Session() as session:
        for url in urls:
            data, error, _ = _fetch(url, session, cancel_event=stop, batch_rate_limit=True)
            if error in _PROVIDER_STOP_ERRORS:
                stop.set()
                return None, error
            if stop.is_set():
                return None, "annule"
            if error:
                continue
            try:
                width, height = _convertible(data)
                with Image.open(io.BytesIO(data)) as image:
                    if has_localized_banner(image):
                        continue
            except (OSError, ValueError, Image.DecompressionBombError):
                continue
            return {
                "version_id": version_id, "printing_id": match["printing_id"],
                "provider_card_id": provider_card_id, "face_index": match["face_index"],
                "old_status": "ready", "old_asset_id": match["asset_id"],
                "language": "en", "source_url": url, "source_data": data,
                "source_width": width, "source_height": height,
                "mime_type": _image_mime_type(data), "source_record_id": provider_card_id,
                "source_evidence": "replacement_localized_image_banner",
            }, None
    return None, None


def _replacement_slot(conn, match):
    return conn.execute("""
        SELECT s.asset_id,a.relative_path FROM image_slots s JOIN image_assets a USING(asset_id)
        WHERE s.printing_id=? AND s.face_index=? AND s.status='ready' AND s.asset_id=?
    """, (match["printing_id"], match["face_index"], match["asset_id"])).fetchone()


def _publish_replacement(conn, version, match, candidate, catalogue_root, on_replaced):
    slot = _replacement_slot(conn, match)
    if not slot or not _stored_banner(version, slot["relative_path"]):
        return "skipped"
    provider_card_id = candidate["provider_card_id"]
    try:
        published = publish_preview_candidate(
            candidate, catalogue_root=catalogue_root,
            provider_card_id=provider_card_id, face_index=match["face_index"])
    except ValueError as exc:
        return "skipped" if str(exc).startswith("candidate périmée") else "failed"
    except (OSError, sqlite3.Error):
        return "failed"
    if on_replaced is not None:
        on_replaced({
            **{key: match[key] for key in
               ("name", "edition", "collector_number", "face_index", "printing_id")},
            "provider_card_id": provider_card_id,
            "old_path": slot["relative_path"],
            "new_path": published["relative_path"],
        })
    return "replaced"


def replace_localized_banners(current, matches, catalogue_root, cancel_event, progress, *, on_replaced=None):
    db, version_id = current["database"], current["version_id"]
    version = db.parent.resolve()
    counts = {"replaced": 0, "failed": 0, "skipped": 0, "total": len(matches)}
    processed = 0
    progress(processed=processed, **counts)
    fetch_stop = Event()
    remaining = iter(matches)
    pending = {}
    detail = None

    def record(outcome):
        nonlocal processed
        counts[outcome] += 1
        processed += 1
        progress(processed=processed, **counts)

    with (closing(connect(db)) as conn,
          ThreadPoolExecutor(max_workers=BATCH_IMAGE_WORKERS, thread_name_prefix="magic-banner") as pool):
        try:
            while True:
                if cancel_event.is_set():
                    break
                while not fetch_stop.is_set() and len(pending) < BATCH_IMAGE_WORKERS:
                    if cancel_event.is_set():
                        break
                    match = next(remaining, None)
                    if match is None:
                        break
                    if resolve_current(catalogue_root)["version_id"] != version_id:
                        raise ValueError("La version active a changé. Relancez la détection.")
                    slot = _replacement_slot(conn, match)
                    if not slot or not _stored_banner(version, slot["relative_path"]):
                        record("skipped")
                        continue
                    pending[pool.submit(_fetch_replacement, match, version_id, fetch_stop)] = match
                if not pending:
                    break
                done, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                for future in done:
                    if cancel_event.is_set():
                        break
                    match = pending.pop(future)
                    candidate, error = future.result()
                    if error in _PROVIDER_STOP_ERRORS:
                        detail = detail or _PROVIDER_STOP_ERRORS[error]
                        record("failed")
                    elif error == "annule":
                        continue
                    elif candidate is None:
                        record("failed")
                    else:
                        record(_publish_replacement(conn, version, match, candidate, catalogue_root, on_replaced))
        finally:
            fetch_stop.set()
    return {**counts, "detail": detail} if detail else counts
