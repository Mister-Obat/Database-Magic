"""Detect Scryfall's localized-image banner and replace only confirmed slots."""

import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from queue import Empty, Full, Queue
from threading import Event, Lock
from time import monotonic

import requests
from PIL import Image, ImageOps

from .image_download import (
    _MAX_GETS,
    _allowed_url,
    _convertible,
    _encode_webp,
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


def _inspect_banner_file(path):
    try:
        return _file_signature(path) if path is not None else None
    except (OSError, ValueError):
        return None


def _check_banner(version, entry):
    relative_path, signature = entry
    try:
        path = _banner_path(version, relative_path)
        with Image.open(path) as image:
            banner = has_localized_banner(image)
        return banner if _file_signature(path) == signature else None
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def analyze_localized_banners(current, cancel_event, progress, cache_path):
    db, version = current["database"], current["database"].parent.resolve()
    counts = {"scanned": 0, "analyzed": 0, "detected": 0, "replaceable": 0, "unreadable": 0}
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
        rows = conn.execute("""
            SELECT s.printing_id,s.face_index,s.asset_id,a.relative_path,e.eligibility,
                   e.code,p.collector_number
            FROM image_slots s JOIN image_assets a USING(asset_id)
            JOIN printings p USING(printing_id) JOIN editions e USING(set_id)
            WHERE s.status='ready'
        """).fetchall()
        paths = list(dict.fromkeys(row["relative_path"] for row in rows))
        cached = {path: (signature, bool(banner)) for path, signature, banner in cache.execute(
            "SELECT relative_path,signature,banner FROM banner_verdicts WHERE detector_version=?",
            (_DETECTOR_VERSION,),
        )}
        verdicts, changed, directories = {}, [], {}

        def inventory_path(relative_path):
            directory, _, filename = relative_path.rpartition("/")
            if directory not in directories:
                try:
                    directories[directory] = _banner_path(version, directory)
                except (OSError, ValueError):
                    directories[directory] = None
            parent = directories[directory]
            return parent / filename if parent is not None and filename not in ("", ".", "..") else None

        progress(processed=0, total=None)
        for offset in range(0, len(paths), 256):
            if cancel_event.is_set():
                return counts, matches
            batch = paths[offset:offset + 256]
            signatures = pool.map(_inspect_banner_file, map(inventory_path, batch))
            for path, signature in zip(batch, signatures):
                if signature is None:
                    verdicts[path] = None
                    continue
                saved = cached.get(path)
                if saved and saved[0] == signature:
                    verdicts[path] = saved[1]
                else:
                    changed.append((path, signature))

        total = len(changed)
        progress(processed=0, total=total)
        for offset in range(0, total, 64):
            if cancel_event.is_set():
                return counts, matches
            batch = changed[offset:offset + 64]
            checks = pool.map(lambda entry: _check_banner(version, entry), batch)
            updates = []
            for (path, signature), banner in zip(batch, checks):
                if cancel_event.is_set():
                    break
                counts["analyzed"] += 1
                verdicts[path] = banner
                if banner is not None:
                    updates.append((path, _DETECTOR_VERSION, signature, int(banner)))
            if updates:
                with cache:
                    cache.executemany("INSERT OR REPLACE INTO banner_verdicts VALUES (?,?,?,?)", updates)
            progress(processed=counts["analyzed"], total=total)
        if cancel_event.is_set():
            return counts, matches

        counts["scanned"] = len(rows)
        for row in rows:
            banner = verdicts[row["relative_path"]]
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
    matches.sort(key=lambda item: (item["edition"], item["collector_number"], item["face_index"]))
    return counts, matches


_PROVIDER_STOP_ERRORS = {
    "acces_refuse_403": "Scryfall a refusé l’accès.",
    "limite_debit_429": "Scryfall a limité le débit. Relancez après le délai d’attente.",
}
_REPLACEMENT_WORKERS = 8
_REPLACEMENT_MIN_INTERVAL = 0.125
_STOP_POLL_INTERVAL = 0.05


class _ReplacementStop:
    def __init__(self, cancel_event):
        self.cancel_event = cancel_event
        self.fetch_stop = Event()

    def is_set(self):
        return self.fetch_stop.is_set() or self.cancel_event.is_set()

    def wait(self, timeout):
        deadline = monotonic() + timeout
        while not self.is_set():
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            self.fetch_stop.wait(min(remaining, _STOP_POLL_INTERVAL))
        return True


def _fetch_replacement(match, version_id, stop, session):
    provider_card_id, urls = match["english"]
    for url in urls:
        data, error, _ = _fetch(url, session, cancel_event=stop, batch_rate_limit=True,
                                batch_min_interval=_REPLACEMENT_MIN_INTERVAL)
        if error in _PROVIDER_STOP_ERRORS:
            stop.fetch_stop.set()
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
            # Match the historical batch encoder's faster setting at quality 95.
            encoded_webp = _encode_webp(data, method=4)
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
            "encoded_webp": encoded_webp,
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
            provider_card_id=provider_card_id, face_index=match["face_index"],
            _encoded_webp=candidate["encoded_webp"])
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
    stop = _ReplacementStop(cancel_event)
    shutdown = Event()
    remaining = iter(matches)
    remaining_lock = Lock()
    results = Queue(maxsize=_REPLACEMENT_WORKERS)
    detail = None

    def record(outcome):
        nonlocal processed
        counts[outcome] += 1
        processed += 1
        progress(processed=processed, **counts)

    def fetch_matches():
        try:
            with closing(connect(db)) as read_conn, requests.Session() as session:
                while not stop.is_set():
                    with remaining_lock:
                        match = next(remaining, None)
                    if match is None:
                        return
                    if resolve_current(catalogue_root)["version_id"] != version_id:
                        raise ValueError("La version active a changé. Relancez la détection.")
                    slot = _replacement_slot(read_conn, match)
                    if not slot or not _stored_banner(version, slot["relative_path"]):
                        candidate, error = None, "skipped"
                    else:
                        candidate, error = _fetch_replacement(match, version_id, stop, session)
                    if error == "annule":
                        return
                    # Preserve the provider's stop reason even when the queue is full.
                    while not shutdown.is_set() and not cancel_event.is_set():
                        try:
                            results.put((match, candidate, error), timeout=_STOP_POLL_INTERVAL)
                            break
                        except Full:
                            continue
        except Exception:
            stop.fetch_stop.set()
            raise

    with (closing(connect(db)) as conn,
          ThreadPoolExecutor(max_workers=_REPLACEMENT_WORKERS, thread_name_prefix="magic-banner") as pool):
        workers = [pool.submit(fetch_matches) for _ in range(min(len(matches), _REPLACEMENT_WORKERS))]
        try:
            while not cancel_event.is_set():
                try:
                    match, candidate, error = results.get(timeout=_STOP_POLL_INTERVAL)
                except Empty:
                    if not all(worker.done() for worker in workers) or not results.empty():
                        continue
                    for worker in workers:
                        worker.result()
                    break
                if cancel_event.is_set():
                    break
                if error == "skipped":
                    record("skipped")
                elif candidate is None:
                    detail = detail or _PROVIDER_STOP_ERRORS.get(error)
                    record("failed")
                else:
                    record(_publish_replacement(conn, version, match, candidate, catalogue_root, on_replaced))
        finally:
            shutdown.set()
            stop.fetch_stop.set()
    return {**counts, "detail": detail} if detail else counts
