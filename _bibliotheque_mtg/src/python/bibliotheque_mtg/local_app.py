from __future__ import annotations

import json
import base64
import hashlib
import io
import logging
import sqlite3
import mimetypes
import os
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from functools import lru_cache
from collections import OrderedDict
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps

from .v2 import api
from .paths import card_candidate_root, catalogue_root
from .v2.publish import resolve_current
from .v2.query import connect
from .maintenance import MaintenanceCancelled, refresh_v2
from .version_history import VersionHistoryError, activate_version, list_version_candidates
from .image_download import (download_printing, ensure_catalogue_wal,
                             refresh_printing_deep, preview_printing_candidate,
                             publish_preview_candidate, import_local_image, is_temporary_image_error)
from .decks import (_SET_NAMES_FR, _resolve_provider_ids, deck_facets, ensure_deck_index, get_deck,
                    index_deck_colors, list_decks, refresh_deck_index,
                    run_deck_composition_index)
from .deck_media import NON_PLAYABLE_SECTIONS, composition_colors, enrich_deck_media, export_deck_to_desktop
from .list_import import resolve_list, parse_list
from .list_export import export_card_to_desktop, export_list_to_desktop, export_local_card_image
from . import personal_decks, personal_deck_text
from .v2.card_generation import (generate_card_candidate, extract_card_text_reference,
                                 CardGenerationError)
import tempfile
from .archive_identification import (ArchiveConflictError, _edition_icon_urls, archive_auto_name,
                                     auto_import_archive_ocr,
                                     delete_archive_file, describe_archive_target,
                                     cached_archive_ocr, import_manual_archive_choice, propose_archive_ocr,
                                     search_archive_printings)
from .proxies import Proxies


OPS_ROOT = Path(__file__).resolve().parents[3]
CATALOGUE_ROOT = catalogue_root(OPS_ROOT)
UI_ROOT = Path(__file__).resolve().parent / "ui"
LIBRARY_ROOT = OPS_ROOT.parent / "Cartes Magic"
HISTORICAL_INDEX = OPS_ROOT / "reports" / "v2-t4.1-historical-unidentified-index.json"
HISTORICAL_RESOLVED = OPS_ROOT / "state" / "historical-archive-resolved.json"
DECK_DB = OPS_ROOT / "state" / "preconstructed-decks.sqlite"
PERSONAL_DECK_DB = OPS_ROOT / "state" / "personal-decks.sqlite"
PROXIES = Proxies(OPS_ROOT.parent / "proxy", OPS_ROOT / "state")
PROXY_ASSOCIATE = "/api/v2/proxies/associate"
PROXY_OPEN_FOLDER = "/api/v2/proxies/open-folder"
PROXY_IMPORT = "/api/v2/proxies/import"
PROXY_DELETE = "/api/v2/proxies/delete"
PERSONAL_POST_ROUTES = {f"/api/v2/personal-decks/{action}" for action in
                        ("create", "save", "add", "import", "export", "delete", "duplicate",
                         "import-text", "export-text", "group-create", "group-rename",
                         "group-delete", "group-move")}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
APP_API_REVISION = 44
_ACTIVE_DOWNLOADS: dict[str, threading.Event] = {}
_ACTIVE_DOWNLOADS_LOCK = threading.Lock()
_BATCH_TASK: dict | None = None
_BATCH_LOCK = threading.Lock()
_BATCH_CANCEL = threading.Event()
_DECK_TASKS: dict[str, dict] = {}
_DECK_CANCELS: dict[str, threading.Event] = {}
_DECK_TASKS_LOCK = threading.Lock()
_DECK_MEDIA_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_DECK_MEDIA_CACHE_LOCK = threading.Lock()
_DECK_MEDIA_CACHE_SIZE = 5000
_DECK_COVER_CACHE: OrderedDict[tuple, bool] = OrderedDict()
_LIST_TASK: dict | None = None
_LIST_LOCK = threading.Lock()
_COLOR_INDEX_TASK: dict | None = None
_COLOR_INDEX_LOCK = threading.Lock()
_MAINTENANCE_TASK: dict | None = None
_MAINTENANCE_LOCK = threading.RLock()
_MAINTENANCE_CANCEL = threading.Event()
_MAINTENANCE_STEPS = (
    ("sources", "Vérification des éditions et cartes"),
    ("catalogue", "Construction du catalogue"),
    ("images", "Conservation des images"),
    ("manifests", "Mise à jour des manifestes"),
    ("exports", "Réutilisation des exports"),
    ("publish", "Publication de la nouvelle version"),
    ("decks", "Index des decks préconstruits"),
)
_IMAGE_CANDIDATES: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_IMAGE_CANDIDATES_LOCK = threading.Lock()
_IMAGE_CANDIDATE_TTL = 24 * 60 * 60
_IMAGE_CANDIDATE_LIMIT = 100
_IMAGE_TASKS: dict[str, dict] = {}
_IMAGE_TASKS_LOCK = threading.RLock()
_IMAGE_TASK_CANCELS: dict[str, threading.Event] = {}
_AI_CANDIDATES: dict[str, dict] = {}
_AI_CANDIDATES_LOCK = threading.Lock()
_LENS_TICKETS: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_LENS_TICKETS_LOCK = threading.Lock()


class FrenchTextUnavailable(ValueError):
    """No compatible official French text is available locally."""


def _prune_image_candidates(now: float) -> None:
    for candidate_id, (created, _) in list(_IMAGE_CANDIDATES.items()):
        if now - created >= _IMAGE_CANDIDATE_TTL:
            del _IMAGE_CANDIDATES[candidate_id]


def _image_candidate(candidate_id: str, *, remove: bool = False) -> dict | None:
    if not isinstance(candidate_id, str) or len(candidate_id) != 32 or any(c not in "0123456789abcdef" for c in candidate_id):
        return None
    with _IMAGE_CANDIDATES_LOCK:
        _prune_image_candidates(time.monotonic())
        entry = _IMAGE_CANDIDATES.pop(candidate_id, None) if remove else _IMAGE_CANDIDATES.get(candidate_id)
        return entry[1] if entry else None


def _store_image_candidate(candidate: dict) -> str:
    candidate_id = uuid.uuid4().hex
    with _IMAGE_CANDIDATES_LOCK:
        _prune_image_candidates(time.monotonic())
        while len(_IMAGE_CANDIDATES) >= _IMAGE_CANDIDATE_LIMIT:
            _IMAGE_CANDIDATES.popitem(last=False)
        _IMAGE_CANDIDATES[candidate_id] = (time.monotonic(), candidate)
    return candidate_id


def _start_image_task(kind: str, label: str, work, *, metadata: dict | None = None) -> dict:
    task_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    task = {"task_id": task_id, "kind": kind, "label": label, "status": "queued",
            "created_at": time.time(), "result": None, "detail": "", **(metadata or {})}
    with _IMAGE_TASKS_LOCK:
        _IMAGE_TASKS[task_id] = task
        _IMAGE_TASK_CANCELS[task_id] = cancel_event

    def runner():
        with _IMAGE_TASKS_LOCK:
            if not cancel_event.is_set():
                task["status"] = "running"
        try:
            result = work(cancel_event)
            with _IMAGE_TASKS_LOCK:
                review_metadata = ({key: result[key] for key in ("french_text", "text_provenance")
                                    if isinstance(result, dict) and key in result})
                task.update(status="cancelled" if cancel_event.is_set() else
                            "error" if result.get("status") == "no_candidate" else
                            "needs_review" if result.get("status") == "candidate" or kind == "ai_generation" else "ready",
                            result=result, detail=result.get("detail", ""), **review_metadata)
        except Exception as exc:
            with _IMAGE_TASKS_LOCK:
                task.update(status="cancelled" if cancel_event.is_set() else "error",
                            detail=str(exc) if isinstance(exc, (ValueError, CardGenerationError)) else "Traitement impossible.")

    threading.Thread(target=runner, daemon=True).start()
    return {"task_id": task_id, "status": "queued"}


def _local_card_text_fields(card: dict, face_index: int, physical_face_count: int,
                            *, french_printed_only: bool = True) -> dict:
    faces = card.get("card_faces") if isinstance(card.get("card_faces"), list) else []
    selected = ([faces[face_index]] if physical_face_count > 1 and face_index < len(faces)
                else faces if faces else [card])
    fields = ("name", "type_line", "mana_cost", "oracle_text", "power", "toughness",
              "loyalty", "defense", "flavor_text")
    rendered = []
    for face in selected:
        item = {}
        for key in fields:
            printed_key = {"name": "printed_name", "type_line": "printed_type_line",
                           "oracle_text": "printed_text"}.get(key)
            if french_printed_only:
                value = face.get(printed_key) if printed_key else face.get(key)
            else:
                value = face.get(key)
            if (french_printed_only and key == "oracle_text" and face.get("oracle_text")
                    and not face.get("printed_text")):
                raise ValueError("Texte français imprimé incomplet pour cette face.")
            if isinstance(value, str) and value:
                item[key] = value
        if french_printed_only and (not item.get("name") or not item.get("type_line")):
            raise ValueError("Nom ou type français imprimé indisponible pour cette face.")
        if item:
            rendered.append(item)
    if not rendered:
        raise ValueError("Aucun texte de carte local disponible pour cette face.")
    return {"card_faces": rendered} if len(rendered) > 1 else rendered[0]


def _source_physical_face_count(card: dict) -> int:
    layout = str(card.get("layout") or "").casefold()
    faces = card.get("card_faces") if isinstance(card.get("card_faces"), list) else []
    if layout in {"transform", "modal_dfc", "double_faced_token", "reversible_card", "meld"}:
        return len(faces)
    return 1


def _same_oracle_face_identity(expected: dict, candidate: dict, face_index: int,
                               expected_face_count: int, candidate_face_count: int) -> bool:
    """Require Oracle, layout and physical face compatibility; never match by name."""
    oracle_id = expected.get("oracle_id")
    layout = str(expected.get("layout") or "").casefold()
    if (not oracle_id or oracle_id != candidate.get("oracle_id") or not layout
            or layout != str(candidate.get("layout") or "").casefold()
            or expected_face_count != candidate_face_count
            or not 0 <= face_index < expected_face_count):
        return False
    expected_faces = expected.get("card_faces") or []
    candidate_faces = candidate.get("card_faces") or []
    if len(expected_faces) != len(candidate_faces):
        return False
    if expected_face_count > 1:
        if face_index >= len(expected_faces):
            return False
        face_oracle = expected_faces[face_index].get("oracle_id")
        if not face_oracle or face_oracle != candidate_faces[face_index].get("oracle_id"):
            return False
    return True


def _lookup_local_card_text(db: Path, provider_card_id: str, face_index: int,
                            cancel_event: threading.Event | None = None) -> dict:
    """Resolve only official French fields from local catalogues, without network access."""
    with connect(db) as conn:
        target = conn.execute(
            "SELECT p.printing_id,p.set_id,p.collector_number,p.layout,p.oracle_id,e.code "
            "FROM source_records sr JOIN printings p USING(printing_id) "
            "JOIN editions e USING(set_id) WHERE sr.provider_card_id=?",
            (provider_card_id,)).fetchone()
        if target is None:
            raise ValueError("Impression exacte introuvable dans le catalogue.")
        printing = dict(target)
        faces_count = conn.execute(
            "SELECT COUNT(*) FROM image_slots WHERE printing_id=?", (printing["printing_id"],)
        ).fetchone()[0]
        if not 0 <= face_index < faces_count:
            raise ValueError("Face physique invalide pour cette impression.")
        sources = [dict(row) for row in conn.execute(
            "SELECT provider_card_id,lang,raw_json FROM source_records "
            "WHERE printing_id=? ORDER BY CASE lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 ELSE 2 END,provider_card_id",
            (printing["printing_id"],))]
    exact_source = next((source for source in sources if source["provider_card_id"] == provider_card_id), None)
    if not exact_source:
        raise ValueError("Preuve d’identité fournisseur introuvable.")
    if cancel_event is not None and cancel_event.is_set():
        raise CardGenerationError("Génération annulée.")
    oracle_id = printing.get("oracle_id") or json.loads(exact_source["raw_json"]).get("oracle_id")
    expected_raw = json.loads(exact_source["raw_json"])
    if not oracle_id:
        raise FrenchTextUnavailable("Identifiant Oracle introuvable; aucune traduction française officielle ne peut être résolue dans la base locale.")

    rows = [
        {"id": source["provider_card_id"], "oracle_id": oracle_id,
         "lang": "fr", "set_code": printing["code"],
         "collector_number": printing["collector_number"],
         "layout": printing["layout"], "raw_json": source["raw_json"]}
        for source in sources if source["lang"] == "fr"
    ]
    source_db = OPS_ROOT / "catalogue.sqlite"
    if source_db.is_file():
        source_conn = sqlite3.connect(source_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        source_conn.row_factory = sqlite3.Row
        try:
            if source_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='official_french_text'"
            ).fetchone():
                official = source_conn.execute(
                    "SELECT oracle_id,set_code,collector_number,fields_json,source,source_url,"
                    "source_image_url FROM official_french_text WHERE provider_card_id=? AND language='fr'",
                    (provider_card_id,),
                ).fetchone()
                if (official is not None and faces_count == 1 and official["oracle_id"] == oracle_id
                        and str(official["set_code"]).casefold() == str(printing["code"]).casefold()
                        and str(official["collector_number"]) == str(printing["collector_number"])):
                    printed = json.loads(official["fields_json"])
                    if all(isinstance(printed.get(key), str) and printed[key]
                           for key in ("printed_name", "printed_type_line", "printed_text")):
                        fields = {"name": printed["printed_name"],
                                  "type_line": printed["printed_type_line"],
                                  "oracle_text": printed["printed_text"]}
                        fields.update({key: printed[key] for key in
                                       ("mana_cost", "power", "toughness", "loyalty", "defense", "flavor_text")
                                       if isinstance(printed.get(key), str) and printed[key]})
                        return {"fields": fields, "source": official["source"],
                                "source_url": official["source_url"],
                                "source_image_url": official["source_image_url"],
                                "printing_id": printing["printing_id"],
                                "source_card_ids": [provider_card_id],
                                "matched_set_collectors": [[printing["code"], printing["collector_number"]]],
                                "face_index": face_index, "provider_card_id": provider_card_id,
                                "requested_provider_card_id": provider_card_id,
                                "language": "fr", "verified": True}
            rows.extend(dict(row) for row in source_conn.execute(
                "SELECT id,oracle_id,lang,set_id,set_code,collector_number,layout,raw_json "
                "FROM cards WHERE lang='fr' AND oracle_id=? "
                "ORDER BY set_code,collector_number,id", (oracle_id,)))
        finally:
            source_conn.close()

    exact_matches, oracle_matches = {}, {}
    for source in rows:
        raw = json.loads(source["raw_json"])
        if str(source.get("set_code") or "").casefold() == str(printing["code"]).casefold() \
                and str(source.get("collector_number") or "") == str(printing["collector_number"]):
            exact_source_print = True
        else:
            exact_source_print = False
        source_faces = _source_physical_face_count(raw)
        if exact_source_print:
            if (str(source.get("layout") or "").casefold() != str(printing.get("layout") or "").casefold()
                    or source_faces != faces_count or not 0 <= face_index < source_faces):
                continue
        elif not _same_oracle_face_identity(expected_raw, raw, face_index, faces_count, source_faces):
            continue
        try:
            fields = _local_card_text_fields(raw, face_index, source_faces, french_printed_only=True)
        except ValueError:
            continue
        bucket = exact_matches if exact_source_print else oracle_matches
        bucket.setdefault(json.dumps(fields, ensure_ascii=False, sort_keys=True),
                          {"fields": fields, "sources": []})["sources"].append(source)
    matches = exact_matches or oracle_matches
    if not matches:
        card_name = str(expected_raw.get("name") or "Cette carte")
        raise FrenchTextUnavailable(
            f"{card_name} ({printing['code'].upper()} nº {printing['collector_number']}) : "
            "aucun texte français officiel n'est présent dans les bases locales. "
            "La mise à jour du catalogue doit signaler cette traduction manquante.")
    match = next(iter(matches.values()))
    source_ids = sorted({str(source["id"]) for source in match["sources"]})
    matched_sets = sorted({(str(source["set_code"]), str(source["collector_number"]))
                           for source in match["sources"]})
    exact_match = bool(exact_matches)
    return {"fields": match["fields"],
            "source": "catalogue_local" if exact_match else "catalogue_oracle_fr",
            "available_text_variants": len(matches),
            "source_url": None, "printing_id": printing["printing_id"],
            "source_card_ids": source_ids, "matched_set_collectors": matched_sets,
            "face_index": face_index, "provider_card_id": source_ids[0],
            "requested_provider_card_id": provider_card_id,
            "language": "fr", "verified": True}


def _same_name_french_image_references(db: Path, provider_card_id: str,
                                       face_index: int) -> list[dict]:
    """Return ready local French images for same-name or same-Oracle printings."""
    with connect(db) as conn:
        target = conn.execute(
            "SELECT p.printing_id,p.oracle_id,p.layout,sr.lang,sr.name,sr.printed_name "
            "FROM source_records sr JOIN printings p USING(printing_id) "
            "WHERE sr.provider_card_id=?", (provider_card_id,)).fetchone()
        if target is None:
            return []
        face_count = conn.execute(
            "SELECT COUNT(*) FROM card_faces WHERE printing_id=? AND physical_image=1",
            (target["printing_id"],)).fetchone()[0]
        if not face_count or not 0 <= face_index < face_count:
            return []

        target_names = set()
        rows = conn.execute(
            "SELECT name,printed_name FROM source_records WHERE printing_id=? AND lang='fr'",
            (target["printing_id"],)).fetchall()
        if not rows and target["oracle_id"]:
            rows = conn.execute(
                "SELECT sr.name,sr.printed_name FROM source_records sr "
                "JOIN printings p USING(printing_id) WHERE p.oracle_id=? AND sr.lang='fr' "
                "ORDER BY sr.provider_card_id LIMIT 1", (target["oracle_id"],)).fetchall()
        for row in rows:
            target_names.update(str(row[key]).strip() for key in ("name", "printed_name")
                                if row[key] and str(row[key]).strip())

        clauses, parameters = [], []
        if target["oracle_id"]:
            clauses.append("p.oracle_id=?")
            parameters.append(target["oracle_id"])
        if target_names:
            placeholders = ",".join("?" for _ in target_names)
            clauses.append(f"(sr.name COLLATE NOCASE IN ({placeholders}) "
                           f"OR sr.printed_name COLLATE NOCASE IN ({placeholders}))")
            parameters.extend(sorted(target_names))
            parameters.extend(sorted(target_names))
        if not clauses:
            return []
        query = (
            "SELECT DISTINCT sr.provider_card_id,sr.printing_id,sr.name,sr.printed_name,"
            "p.oracle_id,p.layout,e.name AS edition_name,e.code AS set_code,p.collector_number,"
            "s.face_index,a.relative_path,a.mime_type,a.width,a.height "
            "FROM source_records sr JOIN printings p USING(printing_id) "
            "JOIN editions e USING(set_id) "
            "JOIN image_slots s ON s.printing_id=p.printing_id AND s.face_index=? "
            "JOIN image_assets a USING(asset_id) "
            "WHERE sr.lang='fr' AND p.printing_id<>? AND p.layout=? "
            "AND (" + " OR ".join(clauses) + ") "
            "AND s.status='ready' AND a.language='fr' "
            "AND EXISTS (SELECT 1 FROM card_faces cf WHERE cf.printing_id=p.printing_id "
            "AND cf.face_index=? AND cf.physical_image=1) "
            "AND (SELECT COUNT(*) FROM card_faces cf WHERE cf.printing_id=p.printing_id "
            "AND cf.physical_image=1)=? "
            "ORDER BY e.name,p.collector_number,p.printing_id LIMIT 48")
        return [dict(row) for row in conn.execute(
            query, (face_index, target["printing_id"], target["layout"],
                    *parameters, face_index, face_count))]


def _same_name_text_reference(db: Path, visual_provider_id: str,
                              text_provider_id: str, face_index: int) -> bool:
    """Verify a cross-printing French reference has the same name/card identity."""
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT sr.provider_card_id,sr.printing_id,sr.lang,sr.name,sr.printed_name,"
            "p.oracle_id,p.layout FROM source_records sr JOIN printings p USING(printing_id) "
            "WHERE sr.provider_card_id IN (?,?)",
            (visual_provider_id, text_provider_id)).fetchall()
        by_id = {row["provider_card_id"]: row for row in rows}
        visual, reference = by_id.get(visual_provider_id), by_id.get(text_provider_id)
        if not visual or not reference or reference["lang"] != "fr":
            return False
        if visual["layout"] != reference["layout"]:
            return False
        counts = conn.execute(
            "SELECT printing_id,COUNT(*) AS faces FROM card_faces WHERE physical_image=1 "
            "AND printing_id IN (?,?) GROUP BY printing_id",
            (visual["printing_id"], reference["printing_id"])).fetchall()
        face_counts = {row["printing_id"]: row["faces"] for row in counts}
        if (face_counts.get(visual["printing_id"]) != face_counts.get(reference["printing_id"])
                or face_index >= face_counts.get(reference["printing_id"], 0)):
            return False
        if visual["oracle_id"] and visual["oracle_id"] == reference["oracle_id"]:
            return True
        target_rows = conn.execute(
            "SELECT name,printed_name FROM source_records WHERE printing_id=? AND lang='fr'",
            (visual["printing_id"],)).fetchall()
        if not target_rows and visual["oracle_id"]:
            target_rows = conn.execute(
                "SELECT sr.name,sr.printed_name FROM source_records sr JOIN printings p USING(printing_id) "
                "WHERE p.oracle_id=? AND sr.lang='fr' LIMIT 1", (visual["oracle_id"],)).fetchall()
        target_names = {str(row[key]).strip().casefold() for row in target_rows
                        for key in ("name", "printed_name") if row[key] and str(row[key]).strip()}
        reference_names = {str(reference[key]).strip().casefold()
                           for key in ("name", "printed_name")
                           if reference[key] and str(reference[key]).strip()}
        return bool(target_names & reference_names)


@lru_cache(maxsize=4)
def _deck_edition_names(catalog_db: Path) -> dict[str, str]:
    """Fill abbreviated deck set metadata from the installed card catalogue."""
    conn = connect(catalog_db)
    try:
        return {str(row[0]).upper(): row[1] for row in conn.execute(
            "SELECT code,name FROM editions WHERE name IS NOT NULL AND name<>''"
        )}
    finally:
        conn.close()


def _with_deck_edition_names(payload: dict, catalog_db: Path) -> dict:
    names = _deck_edition_names(catalog_db)
    entries = payload.get("editions") if "editions" in payload else payload.get("items", [])
    for entry in entries:
        code = str(entry.get("code") or entry.get("set_code") or "").upper()
        if code:
            if "name_en" in entry:
                entry["name_en"] = entry.get("name_en") or names.get(code)
            else:
                entry["set_name"] = entry.get("set_name") or names.get(code)
    if "editions" in payload:
        dates = _deck_edition_release_dates(catalog_db, (entry.get("code") for entry in entries))
        for entry in entries:
            entry["released_at"] = dates.get(str(entry.get("code") or "").upper())
        # Most recent edition first, as in the main editions selector.
        entries.sort(key=lambda entry: str(entry.get("code") or "").upper())
        entries.sort(key=lambda entry: str(entry.get("released_at") or ""), reverse=True)
    return payload


def _deck_edition_release_dates(catalog_db: Path, codes) -> dict[str, str | None]:
    wanted = sorted({str(code).upper() for code in codes if code})
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    conn = connect(catalog_db)
    try:
        return {str(row[0]): row[1] for row in conn.execute(
            f"SELECT upper(code),released_at FROM editions WHERE upper(code) IN ({placeholders})",
            wanted,
        )}
    finally:
        conn.close()


def _with_deck_detail_edition_name(payload: dict, catalog_db: Path) -> dict:
    deck = payload.get("deck")
    if deck and not deck.get("set_name"):
        deck["set_name"] = _deck_edition_names(catalog_db).get(str(deck.get("set_code") or "").upper())
    return payload


def _library_config_path() -> Path:
    configured = os.environ.get("MAGIC_LIBRARY_CONFIG")
    return Path(configured) if configured else OPS_ROOT / "config.json"


def _maintenance_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _set_maintenance_task(task_id: str, **changes) -> None:
    with _MAINTENANCE_LOCK:
        if _MAINTENANCE_TASK is None or _MAINTENANCE_TASK.get("task_id") != task_id:
            return
        _MAINTENANCE_TASK.update(changes)
        _MAINTENANCE_TASK["updated_at"] = _maintenance_now()


def _maintenance_snapshot() -> dict:
    with _MAINTENANCE_LOCK:
        return dict(_MAINTENANCE_TASK) if _MAINTENANCE_TASK else {"status": "idle"}


def _maintenance_steps_template() -> list[dict]:
    return [{"key": key, "label": label, "status": "pending", "progress": 0, "detail": ""}
            for key, label in _MAINTENANCE_STEPS]


def _maintenance_progress(task_id: str, step: str, fraction: float, detail: str = "") -> None:
    if _MAINTENANCE_CANCEL.is_set():
        raise MaintenanceCancelled("Mise à jour annulée")
    percent = max(0, min(100, int(round(float(fraction) * 100))))
    with _MAINTENANCE_LOCK:
        task = _MAINTENANCE_TASK
        if task is None or task.get("task_id") != task_id:
            return
        label = step
        for item in task.get("steps", []):
            if item.get("key") == step:
                item["status"] = "complete" if percent >= 100 else "running"
                item["progress"] = percent
                if detail:
                    item["detail"] = detail
                label = item["label"]
                break
        task["stage"] = step
        task["message"] = detail or label
        task["updated_at"] = _maintenance_now()


def _mark_maintenance_steps(task_id: str, keys, status: str, *, progress: int | None = None, detail: str = "") -> None:
    with _MAINTENANCE_LOCK:
        task = _MAINTENANCE_TASK
        if task is None or task.get("task_id") != task_id:
            return
        for item in task.get("steps", []):
            if item.get("key") in keys:
                item["status"] = status
                if progress is not None:
                    item["progress"] = max(0, min(100, int(progress)))
                elif status in {"complete", "skipped"}:
                    item["progress"] = 100
                if detail:
                    item["detail"] = detail
        task["updated_at"] = _maintenance_now()


def _maintenance_stage(task_id: str) -> str:
    with _MAINTENANCE_LOCK:
        if _MAINTENANCE_TASK and _MAINTENANCE_TASK.get("task_id") == task_id:
            return str(_MAINTENANCE_TASK.get("stage") or "sources")
    return "sources"


def _catalogue_update_message(result: dict) -> str:
    status = str(result.get("status") or "")
    if status == "updated":
        message = f"Nouvelle version publiée : {result.get('version_id') or 'inconnue'}."
    elif status == "no_change":
        message = "Aucune nouvelle édition ni liste de cartes détectée."
    else:
        message = f"Catalogue : {status or 'terminé'}."
    coverage = result.get("official_french_text_coverage")
    if isinstance(coverage, dict) and coverage.get("status") == "incomplete":
        covered = coverage.get("oracle_ids_with_official_french_text")
        total = coverage.get("english_oracle_ids")
        if isinstance(covered, int) and isinstance(total, int):
            message += (f" Texte officiel français complet pour {covered} identités de carte"
                        f" sur {total} ; couverture incomplète.")
    return message


def _decks_update_message(result: dict) -> str:
    return f"Index des decks actualisé : {int(result.get('decks') or 0)} listes disponibles."


def _maintenance_busy_reason() -> str | None:
    with _BATCH_LOCK:
        if _BATCH_TASK and _BATCH_TASK.get("status") in {"queued", "running"}:
            return "Un téléchargement groupé est en cours. Attendez sa fin avant la mise à jour."
    with _LIST_LOCK:
        if _LIST_TASK and _LIST_TASK.get("status") in {"queued", "running"}:
            return "Un import de cartes est en cours. Attendez sa fin avant la mise à jour."
    with _ACTIVE_DOWNLOADS_LOCK:
        if _ACTIVE_DOWNLOADS:
            return "Des téléchargements d’images sont en cours. Attendez leur fin avant de lancer la mise à jour."
    with _DECK_TASKS_LOCK:
        if any(task.get("status") in {"queued", "running"} for task in _DECK_TASKS.values()):
            return "Un téléchargement de deck est en cours. Attendez sa fin avant de lancer la mise à jour."
    return None


def _batch_is_active() -> bool:
    with _BATCH_LOCK:
        return bool(_BATCH_TASK and _BATCH_TASK.get("status") in {"queued", "running"})


def _maintenance_is_active() -> bool:
    return bool(_MAINTENANCE_TASK and _MAINTENANCE_TASK.get("status") in {"queued", "running", "cancelling"})


def _run_maintenance_task(task_id: str) -> None:
    started = time.perf_counter()
    steps: dict[str, dict] = {}
    errors: list[dict[str, str]] = []
    try:
        _set_maintenance_task(task_id, status="running", stage="sources",
                              message="Vérification des éditions et cartes…")
        _mark_maintenance_steps(task_id, {"sources"}, "running", progress=0, detail="Préparation…")
        try:
            steps["catalogue"] = refresh_v2(
                _library_config_path(),
                additions_only=True,
                progress=lambda step, fraction, detail="": _maintenance_progress(task_id, step, fraction, detail))
        except MaintenanceCancelled:
            raise
        except Exception as exc:
            errors.append({"step": "catalogue", "error": str(exc) or type(exc).__name__})
            _mark_maintenance_steps(task_id, {_maintenance_stage(task_id)}, "failed", detail=str(exc))
        else:
            result = steps["catalogue"]
            _mark_maintenance_steps(task_id, {"sources"}, "complete", detail="Sources vérifiées")
            if result.get("status") == "no_change":
                _mark_maintenance_steps(
                    task_id, {"catalogue", "images", "manifests", "exports", "publish"},
                    "skipped", detail="Aucun changement détecté")
            else:
                _mark_maintenance_steps(
                    task_id, {"catalogue", "images", "manifests", "exports", "publish"},
                    "complete", detail="Terminé")
        if _MAINTENANCE_CANCEL.is_set():
            raise MaintenanceCancelled("Mise à jour annulée")
        _mark_maintenance_steps(task_id, {"decks"}, "running", progress=0,
                                detail="Téléchargement de l'index des decks…")
        try:
            steps["decks"] = refresh_deck_index(
                DECK_DB,
                progress=lambda fraction, detail="": _maintenance_progress(task_id, "decks", fraction, detail))
        except MaintenanceCancelled:
            raise
        except Exception as exc:
            errors.append({"step": "decks", "error": str(exc) or type(exc).__name__})
            _mark_maintenance_steps(task_id, {"decks"}, "failed", detail=str(exc))
        else:
            _mark_maintenance_steps(task_id, {"decks"}, "complete",
                                    detail=f"{int(steps['decks'].get('decks') or 0)} decks disponibles")
        messages = []
        if "catalogue" in steps:
            messages.append(_catalogue_update_message(steps["catalogue"]))
        if "decks" in steps:
            messages.append(_decks_update_message(steps["decks"]))
        summary = ("Mise à jour incomplète. " if errors else "") + " ".join(messages)
        _set_maintenance_task(
            task_id, status="failed" if errors else "complete", stage="done",
            message=summary, result=steps, errors=errors,
            seconds=round(time.perf_counter() - started, 1))
    except MaintenanceCancelled:
        _mark_maintenance_steps(task_id, {_maintenance_stage(task_id)}, "cancelled", detail="Annulée")
        _set_maintenance_task(task_id, status="cancelled", stage="done",
                              message="Mise à jour annulée.", result=steps, errors=errors,
                              seconds=round(time.perf_counter() - started, 1))
    except Exception as exc:
        _set_maintenance_task(
            task_id, status="failed", stage="done",
            message=f"Mise à jour impossible : {exc}", result=steps,
            errors=errors + [{"step": "maintenance", "error": str(exc)}],
            seconds=round(time.perf_counter() - started, 1))


def _start_maintenance_task() -> dict:
    global _MAINTENANCE_TASK
    with _MAINTENANCE_LOCK:
        if _MAINTENANCE_TASK and _MAINTENANCE_TASK.get("status") in {"queued", "running", "cancelling"}:
            return dict(_MAINTENANCE_TASK)
        _MAINTENANCE_CANCEL.clear()
        task_id = uuid.uuid4().hex
        now = _maintenance_now()
        _MAINTENANCE_TASK = {"task_id": task_id, "status": "queued", "stage": "queued",
                             "message": "Mise à jour en attente…", "started_at": now,
                             "updated_at": now, "seconds": 0, "result": {}, "errors": [],
                             "steps": _maintenance_steps_template()}
        task = dict(_MAINTENANCE_TASK)
    threading.Thread(target=_run_maintenance_task, args=(task_id,), daemon=True).start()
    return task


def _cancel_maintenance_task(task_id: str) -> dict | None:
    with _MAINTENANCE_LOCK:
        if not _MAINTENANCE_TASK or _MAINTENANCE_TASK.get("task_id") != task_id:
            return None
        if _MAINTENANCE_TASK["status"] in {"queued", "running", "cancelling"}:
            _MAINTENANCE_CANCEL.set()
            _MAINTENANCE_TASK["status"] = "cancelling"
            _MAINTENANCE_TASK["message"] = "Annulation en cours…"
            _MAINTENANCE_TASK["updated_at"] = _maintenance_now()
        return dict(_MAINTENANCE_TASK)


def _deck_entries(deck: dict) -> list[dict]:
    """One image download per exact printing, including deck tokens."""
    entries = []
    seen = set()
    for cards in (deck.get("sections") or {}).values():
        for card in cards:
            key = card.get("provider_card_id") or (
                card.get("name"), card.get("set_code"), card.get("collector_number"))
            if key not in seen:
                seen.add(key)
                entries.append(card)
    return entries


def _deck_with_media(payload: dict, catalog_db: Path) -> dict:
    if payload.get("found") and payload.get("deck"):
        deck = enrich_deck_media(payload["deck"], catalog_db)
        entries = _deck_entries(deck)
        ready = sum(bool(card.get("image_ready")) for card in entries)
        deck["media_summary"] = {"total": len(entries), "ready": ready,
                                 "missing": len(entries) - ready}
        sections = deck.get("sections") or {}
        ordered = ("commander", *(key for key in sections if key not in {"commander", "tokens"}), "tokens")
        cover = next((sections[key][0] for key in ordered if sections.get(key)), None)
        deck["cover_image_path"] = cover.get("image_path") if cover else None
        deck["composition_colors"] = composition_colors(
            card for key, cards in sections.items() if key not in NON_PLAYABLE_SECTIONS for card in cards)
        payload["deck"] = deck
    return payload


def _deck_media_context(catalog_db: Path) -> tuple:
    def fingerprint(path: Path) -> tuple:
        values = []
        for candidate in (path, Path(str(path) + "-wal")):
            try:
                stat = candidate.stat()
                values.extend((stat.st_mtime_ns, stat.st_size))
            except OSError:
                values.extend((None, None))
        return tuple(values)

    current = resolve_current(CATALOGUE_ROOT)
    return (current["version_id"], fingerprint(Path(catalog_db)), fingerprint(DECK_DB))


def _deck_cover_ready(deck_id: str, catalog_db: Path, context: tuple | None = None) -> bool:
    """Resolve the first card cover without deriving or fetching deck tokens."""
    context = context or _deck_media_context(catalog_db)
    cache_key = (str(deck_id), context)
    with _DECK_MEDIA_CACHE_LOCK:
        if cache_key in _DECK_COVER_CACHE:
            _DECK_COVER_CACHE.move_to_end(cache_key)
            return _DECK_COVER_CACHE[cache_key]
    payload = get_deck(DECK_DB, deck_id, catalog_db=catalog_db,
                       fetch_missing=False, include_tokens=False)
    payload = _deck_with_media(payload, catalog_db)
    deck = payload.get("deck") if payload.get("found") else None
    ready = bool(deck and deck.get("cover_image_path"))
    with _DECK_MEDIA_CACHE_LOCK:
        _DECK_COVER_CACHE[cache_key] = ready
        _DECK_COVER_CACHE.move_to_end(cache_key)
        while len(_DECK_COVER_CACHE) > _DECK_MEDIA_CACHE_SIZE:
            _DECK_COVER_CACHE.popitem(last=False)
    return ready


def _deck_list_media(deck_id: str, catalog_db: Path) -> dict:
    """Return current local cover/completeness without fetching deck data."""
    cache_key = (str(deck_id), _deck_media_context(catalog_db))
    with _DECK_MEDIA_CACHE_LOCK:
        cached = _DECK_MEDIA_CACHE.get(cache_key)
        if cached is not None:
            _DECK_MEDIA_CACHE.move_to_end(cache_key)
            return dict(cached)
    payload = get_deck(DECK_DB, deck_id, catalog_db=catalog_db,
                       fetch_missing=False, include_tokens=True)
    payload = _deck_with_media(payload, catalog_db)
    deck = payload.get("deck") if payload.get("found") else None
    if not deck:
        result = {"cover_image_path": None, "media_summary": None, "complete": None}
        return _cache_deck_media(cache_key, result)
    result = {"cover_image_path": deck.get("cover_image_path"),
              "composition_colors": deck.get("composition_colors"),
              "media_summary": None, "complete": None}
    if deck.get("loaded") and deck.get("tokens_loaded"):
        summary = deck.get("media_summary") or {"total": 0, "ready": 0, "missing": 0}
        result.update(media_summary=summary, complete=bool(summary["total"] and not summary["missing"]))
    return _cache_deck_media(cache_key, result)


def _cache_deck_media(key: tuple, value: dict) -> dict:
    with _DECK_MEDIA_CACHE_LOCK:
        _DECK_MEDIA_CACHE[key] = dict(value)
        _DECK_MEDIA_CACHE.move_to_end(key)
        while len(_DECK_MEDIA_CACHE) > _DECK_MEDIA_CACHE_SIZE:
            _DECK_MEDIA_CACHE.popitem(last=False)
    return value


def _recalculate_deck_search(filters: dict, catalog_db: Path) -> dict:
    allowed = {"q", "deck_format", "deck_type", "set_code", "colors",
               "color_mode", "sort", "order", "image_filter"}
    if set(filters) - allowed:
        raise ValueError("Filtres de decks invalides")
    if filters.get("image_filter", "all") not in {"all", "with", "without"}:
        raise ValueError("Filtre d'image invalide")
    common = {"query": filters.get("q") or None, "name_only": True,
              "deck_format": filters.get("deck_format") or None,
              "deck_type": filters.get("deck_type") or None,
              "set_code": filters.get("set_code") or None,
              "colors": filters.get("colors") or None,
              "color_mode": filters.get("color_mode") or "contains",
              "sort": filters.get("sort") or "date",
              "order": filters.get("order") or "desc"}
    offset = 0
    total = recalculated = unloaded = 0
    media_context = _deck_media_context(catalog_db)
    while True:
        page = list_decks(DECK_DB, **common, limit=100, offset=offset)
        total = page["total"]
        if not page["items"]:
            break
        for item in page["items"]:
            image_filter = filters.get("image_filter") or "all"
            if image_filter != "all" and _deck_cover_ready(item["id"], catalog_db, media_context) != (image_filter == "with"):
                continue
            state = _deck_list_media(item["id"], catalog_db)
            if state["complete"] is None:
                unloaded += 1
            else:
                recalculated += 1
        offset += len(page["items"])
        if offset >= total:
            break
    return {"total": total, "recalculated": recalculated, "unloaded": unloaded}


def _run_color_index_task(task_id: str) -> None:
    global _COLOR_INDEX_TASK
    def progress(update: dict) -> None:
        with _COLOR_INDEX_LOCK:
            if _COLOR_INDEX_TASK and _COLOR_INDEX_TASK["task_id"] == task_id:
                _COLOR_INDEX_TASK.update(update)
                _COLOR_INDEX_TASK["status"] = "running"
    try:
        catalog_db = resolve_current(CATALOGUE_ROOT)["database"]
        result = index_deck_colors(DECK_DB, catalog_db=catalog_db, progress_callback=progress)
        with _COLOR_INDEX_LOCK:
            if _COLOR_INDEX_TASK and _COLOR_INDEX_TASK["task_id"] == task_id:
                _COLOR_INDEX_TASK.update(result)
                _COLOR_INDEX_TASK["status"] = "complete"
    except Exception as exc:
        with _COLOR_INDEX_LOCK:
            if _COLOR_INDEX_TASK and _COLOR_INDEX_TASK["task_id"] == task_id:
                _COLOR_INDEX_TASK.update({"status": "failed", "error": str(exc)})


def _run_list_task(task_id: str, value: str) -> None:
    global _LIST_TASK
    try:
        with _LIST_LOCK:
            _LIST_TASK["status"] = "running"
        matches = resolve_list(resolve_current(CATALOGUE_ROOT)["database"], value)
        with _LIST_LOCK:
            _LIST_TASK["total"] = len(matches)
        for item in matches:
            if item["status"] == "matched":
                try:
                    report = download_printing(
                        item["provider_card_id"], catalogue_root=CATALOGUE_ROOT,
                        ledger_path=OPS_ROOT / "state" / "v2-image-download-ledger.json")
                    item["download"] = report
                    item["status"] = "ready" if report.get("results") and all(
                        face.get("status") in {"ready", "already_ready"} for face in report["results"]) else "failed"
                except Exception as exc:
                    item["status"] = "failed"
                    item["detail"] = str(exc)
            with _LIST_LOCK:
                _LIST_TASK["items"].append(item)
                _LIST_TASK["completed"] += 1
        with _LIST_LOCK:
            _LIST_TASK["status"] = "complete"
    except Exception as exc:
        with _LIST_LOCK:
            _LIST_TASK["status"] = "failed"
            _LIST_TASK["error"] = str(exc)


def _run_deck_task(task_id: str, deck_id: str, cancel: threading.Event) -> None:
    """Download exact printings in a selected precon into the active catalogue."""
    with _DECK_TASKS_LOCK:
        _DECK_TASKS[task_id]["status"] = "running"
    try:
        catalog_db = resolve_current(CATALOGUE_ROOT)["database"]
        deck_result = _deck_with_media(get_deck(DECK_DB, deck_id, catalog_db=catalog_db, include_tokens=True), catalog_db)
        if not deck_result.get("found"):
            raise ValueError("deck introuvable")
        deck = deck_result["deck"]
        entries = [card for card in _deck_entries(deck) if not card.get("image_ready")]
        with _DECK_TASKS_LOCK:
            _DECK_TASKS[task_id]["total"] = len(entries)
        if entries and not any(card.get("provider_card_id") for card in entries):
            with _DECK_TASKS_LOCK:
                task = _DECK_TASKS[task_id]
                task["status"] = "catalogue_incomplet"
                task["current"] = ""
                task["unavailable"] = len(entries)
                task["errors"].append("Aucune impression de ce deck n'est dans le catalogue actif. Actualisez le catalogue avant de télécharger les images.")
            return
        for card in entries:
            if cancel.is_set():
                break
            name = str(card.get("name") or "Carte sans nom")
            with _DECK_TASKS_LOCK:
                _DECK_TASKS[task_id]["current"] = name
            provider_id = card.get("provider_card_id")
            if provider_id:
                try:
                    report = download_printing(
                        provider_id, catalogue_root=CATALOGUE_ROOT,
                        ledger_path=OPS_ROOT / "state" / "v2-image-download-ledger.json",
                        cancel_event=cancel,
                    )
                    statuses = [result.get("status") for result in report.get("results", [])]
                    success = bool(statuses) and all(status in {"ready", "already_ready"} for status in statuses)
                    detail = ", ".join(statuses) if statuses else "aucun emplacement image"
                except Exception as exc:
                    success, detail = False, str(exc)
            else:
                with _DECK_TASKS_LOCK:
                    task = _DECK_TASKS[task_id]
                    task["completed"] += 1
                    task["unavailable"] += 1
                    task["unavailable_cards"].append(name)
                continue
            with _DECK_TASKS_LOCK:
                task = _DECK_TASKS[task_id]
                task["completed"] += 1
                task["ready" if success else "failed"] += 1
                if not success:
                    task["errors"].append(f"{name} : {detail}")
        with _DECK_TASKS_LOCK:
            task = _DECK_TASKS[task_id]
            task["status"] = "cancelled" if cancel.is_set() else "complete"
            task["current"] = ""
    except Exception as exc:
        with _DECK_TASKS_LOCK:
            task = _DECK_TASKS[task_id]
            task["status"] = "failed"
            task["current"] = ""
            task["errors"].append(str(exc))
    finally:
        with _DECK_TASKS_LOCK:
            _DECK_CANCELS.pop(task_id, None)


def _batch_filter_payload(mode: str, filters: dict) -> dict:
    search_keys = {"q", "set", "lang", "rarity", "color", "color_mode", "mana_value", "type",
                   "card_type", "image_filter", "result_sort", "mana_min", "mana_max", "power_min", "power_max",
                   "toughness_min", "toughness_max", "oracle_id", "printing_id", "unique"}
    deck_keys = {"q", "deck_format", "deck_type", "set_code", "colors", "color_mode", "sort", "order", "image_filter"}
    if not isinstance(mode, str) or mode not in {"search", "edition", "decks"} or not isinstance(filters, dict):
        raise ValueError("Filtres de téléchargement invalides")
    allowed = search_keys if mode == "search" else {"set"} if mode == "edition" else deck_keys
    if set(filters) - allowed or any(not isinstance(value, (str, int, float)) or len(str(value)) > 500 for value in filters.values()):
        raise ValueError("Filtres de téléchargement invalides")
    if mode == "edition" and not filters.get("set"):
        raise ValueError("Sélectionnez une édition")
    if mode == "decks" and filters.get("image_filter", "all") not in {"all", "with", "without"}:
        raise ValueError("Filtre d'image invalide")
    if mode == "decks" and not any(filters.get(key) for key in ("q", "deck_format", "deck_type", "set_code", "colors")):
        raise ValueError("Filtrez les decks avant de télécharger")
    return {key: value for key, value in filters.items() if value not in (None, "")}


def _batch_printings(mode: str, filters: dict, catalog_db: Path, cancel: threading.Event) -> tuple[list[tuple[str, str]], int]:
    cards: list[dict] = []
    unavailable = 0
    if mode in {"search", "edition"}:
        route = "/api/v2/search" if mode == "search" else "/api/v2/edition"
        offset = 0
        while True:
            page = api.dispatch(catalog_db, route, {**filters, "limit": 100, "offset": offset})
            cards.extend(page["items"])
            offset += len(page["items"])
            if cancel.is_set() or not page["items"] or offset >= page["total"]:
                break
    else:
        media_context = _deck_media_context(catalog_db)
        offset = 0
        while True:
            page = list_decks(DECK_DB, query=filters.get("q"), name_only=True,
                              deck_format=filters.get("deck_format"), deck_type=filters.get("deck_type"),
                              set_code=filters.get("set_code"), colors=filters.get("colors"),
                              color_mode=filters.get("color_mode") or "contains",
                              sort=filters.get("sort") or "date", order=filters.get("order") or "desc",
                              limit=100, offset=offset)
            for deck in page["items"]:
                if cancel.is_set(): break
                image_filter = filters.get("image_filter") or "all"
                if image_filter != "all" and _deck_cover_ready(deck["id"], catalog_db, media_context) != (image_filter == "with"):
                    continue
                detail = get_deck(DECK_DB, deck["id"], catalog_db=catalog_db, include_tokens=True)
                if detail.get("found"):
                    cards.extend(_deck_entries(detail["deck"]))
            offset += len(page["items"])
            if cancel.is_set() or not page["items"] or offset >= page["total"]:
                break
    selected: list[tuple[str, str]] = []
    seen = set()
    conn = connect(catalog_db)
    try:
        for card in cards:
            provider_id = card.get("provider_card_id")
            if not provider_id:
                unavailable += 1
                continue
            row = conn.execute("SELECT p.printing_id FROM source_records sr JOIN printings p USING(printing_id) "
                               "JOIN editions e USING(set_id) WHERE sr.provider_card_id=? AND e.eligibility='eligible'",
                               (provider_id,)).fetchone()
            if row is None:
                unavailable += 1
                continue
            printing_id = row[0]
            if printing_id in seen:
                continue
            seen.add(printing_id)
            slots = conn.execute("SELECT status FROM image_slots WHERE printing_id=?", (printing_id,)).fetchall()
            if slots and all(slot[0] == "ready" for slot in slots):
                continue
            selected.append((provider_id, str(card.get("printed_name") or card.get("name") or "Carte")))
    finally:
        conn.close()
    return selected, unavailable


def _run_batch_task(task_id: str, mode: str, filters: dict, catalog_db: Path) -> None:
    global _BATCH_TASK
    with _BATCH_LOCK:
        _BATCH_TASK["status"] = "running"
    try:
        selected, unavailable = _batch_printings(mode, filters, catalog_db, _BATCH_CANCEL)
        with _BATCH_LOCK:
            _BATCH_TASK.update(total=len(selected), unavailable=unavailable,
                               processing_started_at=time.time())
        def download_one(provider_id: str) -> tuple[bool, str, bool]:
            if _BATCH_CANCEL.is_set():
                return False, "annulé", False
            if resolve_current(CATALOGUE_ROOT)["database"] != catalog_db:
                raise RuntimeError("La version du catalogue a changé pendant le téléchargement")
            try:
                report = download_printing(provider_id, catalogue_root=CATALOGUE_ROOT,
                    ledger_path=OPS_ROOT / "state" / "v2-image-download-ledger.json",
                    cancel_event=_BATCH_CANCEL, parallel_fetch=True)
                results = report.get("results", [])
                success = bool(results) and all(item["status"] in {"ready", "already_ready"} for item in results)
                reasons = [item.get("reason") or item["status"] for item in results]
                detail = ", ".join(dict.fromkeys(reasons)) or "aucun emplacement image"
                return success, detail, any(is_temporary_image_error(reason) for reason in reasons)
            except Exception as exc:
                return False, str(exc), False

        remaining = iter(selected)
        consecutive_temporary_errors = 0
        stopped_for_network = False
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="magic-image-batch") as executor:
            pending = {}
            def enqueue() -> None:
                if _BATCH_CANCEL.is_set():
                    return
                try:
                    provider_id, name = next(remaining)
                except StopIteration:
                    return
                pending[executor.submit(download_one, provider_id)] = name

            for _ in range(4):
                enqueue()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    name = pending.pop(future)
                    success, detail, temporary_error = future.result()
                    consecutive_temporary_errors = consecutive_temporary_errors + 1 if temporary_error else 0
                    if consecutive_temporary_errors >= 8 and not _BATCH_CANCEL.is_set():
                        stopped_for_network = True
                        _BATCH_CANCEL.set()
                    with _BATCH_LOCK:
                        _BATCH_TASK["completed"] += 1
                        _BATCH_TASK["last_progress_at"] = time.time()
                        _BATCH_TASK["ready" if success else "failed"] += 1
                        _BATCH_TASK["current"] = name
                        if not success and len(_BATCH_TASK["errors"]) < 30:
                            _BATCH_TASK["errors"].append(f"{name} : {detail}")
                    enqueue()
        with _BATCH_LOCK:
            if stopped_for_network:
                _BATCH_TASK["errors"].insert(0, "Lot arrêté après huit échecs réseau ou serveur consécutifs. Relancez après rétablissement de la connexion.")
            status = "failed" if stopped_for_network else "cancelled" if _BATCH_CANCEL.is_set() else "complete"
            _BATCH_TASK.update(status=status, current="")
    except Exception as exc:
        with _BATCH_LOCK:
            _BATCH_TASK.update(status="failed", current="", errors=[*_BATCH_TASK["errors"], str(exc)])


def _safe_child(root: Path, relative: str) -> Path:
    """Resolve a URL path strictly inside a known local root."""
    rel = PurePosixPath(str(relative).replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(p in {"", ".", ".."} for p in rel.parts):
        raise ValueError("unsafe relative path")
    if any(":" in p or p.startswith("~") for p in rel.parts):
        raise ValueError("unsafe path component")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    for part in rel.parts:
        if part != part.rstrip(" .") or part.split(".", 1)[0].casefold() in reserved:
            raise ValueError("Windows reserved path component")
    root = root.resolve()
    target = (root / Path(*rel.parts)).resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("path outside allowed root")
    return target


def _archive_lens_jpeg(relative_path: str) -> bytes:
    path = _safe_child(LIBRARY_ROOT, relative_path)
    if (path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
            or path.stat().st_size > 20 * 1024 * 1024):
        raise ValueError("Image d’archive invalide")
    with Image.open(path) as source:
        picture = ImageOps.exif_transpose(source).convert("RGB")
        picture.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        picture.save(output, format="JPEG", quality=90)
    return output.getvalue()


def _store_lens_ticket(relative_path: str) -> str:
    image = _archive_lens_jpeg(relative_path)
    ticket = uuid.uuid4().hex
    with _LENS_TICKETS_LOCK:
        now = time.monotonic()
        for key, (created, _) in list(_LENS_TICKETS.items()):
            if now - created > 90:
                del _LENS_TICKETS[key]
        while len(_LENS_TICKETS) >= 5:
            _LENS_TICKETS.popitem(last=False)
        _LENS_TICKETS[ticket] = (now, image)
    return ticket


def _take_lens_ticket(ticket: str) -> bytes:
    if len(ticket) != 32 or any(c not in "0123456789abcdef" for c in ticket):
        raise ValueError("Ticket Google invalide")
    with _LENS_TICKETS_LOCK:
        entry = _LENS_TICKETS.pop(ticket, None)
    if entry is None or time.monotonic() - entry[0] > 90:
        raise ValueError("Ticket Google expiré")
    return entry[1]


def _archive_lens_bridge(image: bytes) -> bytes:
    encoded = base64.b64encode(image).decode("ascii")
    return ("<!doctype html><meta charset=utf-8>"
            "<form method=POST enctype=multipart/form-data "
            "action=https://www.google.com/searchbyimage/upload>"
            "<input type=file name=encoded_image>"
            "<input type=hidden name=sbisrc value='Google Chrome'></form>"
            "<script>"
            f"const bytes=Uint8Array.from(atob('{encoded}'),c=>c.charCodeAt(0));"
            "const file=new File([bytes],'carte-magic.jpg',{type:'image/jpeg'});"
            "const transfer=new DataTransfer();transfer.items.add(file);"
            "document.querySelector('input[type=file]').files=transfer.files;"
            "document.querySelector('form').submit();"
            "</script>").encode("utf-8")


def _params(query: str) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=40)
    return {key: values[-1] for key, values in parsed.items()}


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _enrich_result(db: Path, result: dict) -> dict:
    items = result.get("items")
    if not items:
        return result
    ids = sorted({str(item["printing_id"]) for item in items if item.get("printing_id")})
    if not ids:
        return result
    placeholders = ",".join("?" for _ in ids)
    with connect(db) as conn:
        rows = conn.execute(
            f"SELECT s.printing_id,s.face_index,s.preferred_language,s.selected_language,s.status,s.asset_id,"
            f"a.relative_path,a.width,a.height,a.language AS asset_language,a.provenance_json "
            f"FROM image_slots s LEFT JOIN image_assets a USING(asset_id) WHERE s.printing_id IN ({placeholders}) "
            "ORDER BY s.printing_id,s.face_index",
            ids,
        ).fetchall()
    by_printing: dict[str, list[dict]] = {}
    for row in rows:
        value = dict(row)
        if value.get("provenance_json"):
            try:
                value["provenance"] = json.loads(value.pop("provenance_json"))
            except json.JSONDecodeError:
                value["provenance"] = {"error": "invalid provenance JSON"}
        else:
            value.pop("provenance_json", None)
        by_printing.setdefault(value["printing_id"], []).append(value)
    for item in items:
        item["image_slots"] = by_printing.get(str(item.get("printing_id")), [])
    return result


def _image_slots_for_printings(db: Path, printing_ids: list[str]) -> dict[str, list[dict]]:
    if not printing_ids:
        return {}
    placeholders = ",".join("?" for _ in printing_ids)
    with connect(db) as conn:
        rows = conn.execute(
            f"SELECT s.printing_id,s.face_index,s.preferred_language,s.selected_language,s.status,s.asset_id,"
            f"a.relative_path,a.width,a.height,a.language AS asset_language,a.provenance_json "
            f"FROM image_slots s LEFT JOIN image_assets a USING(asset_id) "
            f"WHERE s.printing_id IN ({placeholders}) ORDER BY s.printing_id,s.face_index",
            printing_ids,
        ).fetchall()
    by_printing: dict[str, list[dict]] = {printing_id: [] for printing_id in printing_ids}
    for row in rows:
        value = dict(row)
        if value.get("provenance_json"):
            try:
                value["provenance"] = json.loads(value.pop("provenance_json"))
            except json.JSONDecodeError:
                value["provenance"] = {"error": "invalid provenance JSON"}
        else:
            value.pop("provenance_json", None)
        by_printing.setdefault(value["printing_id"], []).append(value)
    return by_printing


def _count_complete_decks(db: Path, deck_db: Path, deck_ids: list[str]) -> int:
    resolved = {}
    readiness = {}
    complete = 0
    for deck_id in deck_ids:
        deck = get_deck(deck_db, deck_id, fetch_missing=False, include_tokens=True).get("deck")
        if not deck:
            continue
        unresolved = {}
        cards_and_keys = []
        for section in deck["sections"].values():
            for card in section:
                raw = card.get("raw") or {}
                scryfall_id = raw.get("scryfall_id") or raw.get("identifiers", {}).get("scryfallId")
                key = (str(scryfall_id) if scryfall_id else None, card.get("set_code"), card.get("collector_number"))
                cards_and_keys.append((card, key))
                if key not in resolved:
                    unresolved.setdefault(key, card)
        if unresolved:
            _resolve_provider_ids(list(unresolved.values()), db)
            resolved.update((key, card["provider_card_id"]) for key, card in unresolved.items())
        for card, key in cards_and_keys:
            card["provider_card_id"] = resolved[key]
        entries = _deck_entries(deck)
        if not entries or any(not card["provider_card_id"] for card in entries):
            continue
        media_cards = {(card["provider_card_id"], card.get("set_code")): card for card in entries}
        if any(readiness.get(key) is False for key in media_cards):
            continue
        unchecked = {key: card for key, card in media_cards.items() if key not in readiness}
        if unchecked:
            media = enrich_deck_media({"sections": {"stats": list(unchecked.values())}}, db)
            readiness.update((key, item["image_ready"])
                             for key, item in zip(unchecked, media["sections"]["stats"]))
        complete += all(readiness[key] for key in media_cards)
    return complete


_STATS_LOCK = threading.Lock()
_STATS_CACHE: tuple[Path, float, dict] | None = None
_DECK_STATS_CONDITION = threading.Condition()
_DECK_STATS_BUSY = False
_DECK_STATS_CACHE: tuple[tuple[Path, Path], frozenset[str], float, int] | None = None


def _cached_catalogue_stats(db: Path) -> dict:
    global _STATS_CACHE
    with _STATS_LOCK:
        if _STATS_CACHE and _STATS_CACHE[0] == db and time.monotonic() - _STATS_CACHE[1] < 15:
            return _STATS_CACHE[2]
        result = _catalogue_stats(db)
        _STATS_CACHE = (db, time.monotonic(), result)
        return result


def _calculate_complete_decks(key: tuple[Path, Path], deck_ids: list[str]) -> int:
    global _DECK_STATS_BUSY, _DECK_STATS_CACHE
    try:
        count = _count_complete_decks(*key, deck_ids)
        with _DECK_STATS_CONDITION:
            _DECK_STATS_CACHE = (key, frozenset(deck_ids), time.monotonic(), count)
        return count
    finally:
        with _DECK_STATS_CONDITION:
            _DECK_STATS_BUSY = False
            _DECK_STATS_CONDITION.notify_all()


def _refresh_complete_decks(key: tuple[Path, Path], deck_ids: list[str]) -> None:
    try:
        _calculate_complete_decks(key, deck_ids)
    except Exception:
        logging.exception("Calcul du nombre de decks complets impossible")


def _cached_complete_decks(db: Path, deck_db: Path, deck_ids: list[str], *, background: bool) -> int | None:
    global _DECK_STATS_BUSY
    if not deck_ids:
        return 0
    current_ids = frozenset(deck_ids)
    key = (db, deck_db)
    with _DECK_STATS_CONDITION:
        while True:
            cached = _DECK_STATS_CACHE
            stale = None
            if cached and cached[0] == key and cached[1].issubset(current_ids):
                if time.monotonic() - cached[2] < 300:
                    return cached[3]
                stale = cached[3]
            if not _DECK_STATS_BUSY:
                _DECK_STATS_BUSY = True
                break
            if background:
                return stale
            _DECK_STATS_CONDITION.wait()
    if not background:
        return _calculate_complete_decks(key, deck_ids)
    thread = threading.Thread(target=_refresh_complete_decks, args=(key, deck_ids),
                              name="magic-home-deck-stats", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        with _DECK_STATS_CONDITION:
            _DECK_STATS_BUSY = False
            _DECK_STATS_CONDITION.notify_all()
        raise
    return stale


def _catalogue_stats(db: Path) -> dict:
    with closing(connect(db)) as conn:
        editions = conn.execute(
            "SELECT eligibility,COUNT(*) n FROM editions GROUP BY eligibility ORDER BY eligibility"
        ).fetchall()
        records = conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]
        cards = conn.execute("SELECT COUNT(*) FROM printings").fetchone()[0]
        complete_cards = conn.execute("""SELECT COUNT(*) FROM printings p
            WHERE EXISTS (SELECT 1 FROM image_slots s WHERE s.printing_id=p.printing_id)
            AND NOT EXISTS (SELECT 1 FROM image_slots s
                            WHERE s.printing_id=p.printing_id AND s.status!='ready')""").fetchone()[0]
        slots = conn.execute("SELECT status,COUNT(*) n FROM image_slots GROUP BY status").fetchall()
        assets = conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0]
        meta = dict(conn.execute("SELECT key,value FROM catalogue_meta").fetchall())
        complete_editions = conn.execute("""SELECT COUNT(*) FROM editions e
            WHERE e.eligibility='eligible' AND EXISTS
            (SELECT 1 FROM printings p WHERE p.set_id=e.set_id)
            AND NOT EXISTS (SELECT 1 FROM printings p JOIN image_slots s USING(printing_id)
            WHERE p.set_id=e.set_id AND s.status!='ready')""").fetchone()[0]
    status_counts = {str(row[0]): row[1] for row in slots}
    return {
        "version_id": resolve_current(CATALOGUE_ROOT)["version_id"],
        "catalogue_date": meta.get("generated_at_utc") or meta.get("snapshot_date"),
        "editions": {str(row[0]): row[1] for row in editions},
        "source_records": records,
        "printings": cards,
        "complete_cards": complete_cards,
        "image_slots": status_counts,
        "image_assets": assets,
        "complete_editions": complete_editions,
        "missing_images": sum(n for status, n in status_counts.items() if status != "ready"),
        "downloaded_images": status_counts.get("ready", 0),
    }


def _stats(db: Path, *, background_decks: bool = False) -> dict:
    catalogue = _cached_catalogue_stats(db)
    deck_db = DECK_DB
    decks_total = 0
    deck_ids = []
    if deck_db.is_file():
        with closing(sqlite3.connect(deck_db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            decks_total = conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0]
            deck_ids = [row[0] for row in conn.execute(
                "SELECT id FROM decks WHERE loaded=1 AND tokens_loaded=1")]
    complete_decks = _cached_complete_decks(db, deck_db, deck_ids, background=background_decks)
    return {**catalogue, "complete_decks": complete_decks,
            "loaded_decks": len(deck_ids), "decks_total": decks_total}


def _deferred(db: Path) -> list[dict]:
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT code,name,set_type,released_at,eligibility,exclusion_reason FROM editions "
            "WHERE eligibility!='eligible' ORDER BY released_at DESC,code"
        ).fetchall()
    return [dict(row) for row in rows]


def _prepare_image_preview(provider_id: str, face_index: int, catalog_db: Path, cancel_event: threading.Event) -> dict:
    if cancel_event.is_set():
        return {"status": "cancelled"}
    report = preview_printing_candidate(
        provider_id, catalogue_root=CATALOGUE_ROOT,
        ledger_path=OPS_ROOT / "state" / "v2-image-download-ledger.json",
        face_index=face_index, cancel_event=cancel_event)
    if report["status"] != "candidate":
        candidates = []
    else:
        candidates = report["candidates"]
    with connect(catalog_db) as conn:
        old = conn.execute(
            "SELECT p.printing_id,s.status,a.relative_path,a.mime_type,a.width,a.height,a.language "
            "FROM source_records sr JOIN printings p USING(printing_id) "
            "JOIN image_slots s ON s.printing_id=p.printing_id AND s.face_index=? "
            "LEFT JOIN image_assets a USING(asset_id) WHERE sr.provider_card_id=?",
            (face_index, provider_id)).fetchone()
    old_path = old["relative_path"] if old else None
    for reference in _same_name_french_image_references(catalog_db, provider_id, face_index):
        try:
            reference_path = _safe_child(Path(catalog_db).parent, reference["relative_path"])
            if reference_path.stat().st_size > 20 * 1024 * 1024:
                continue
            reference_data = reference_path.read_bytes()
            with Image.open(io.BytesIO(reference_data)) as reference_image:
                reference_image.load()
                reference_width, reference_height = reference_image.size
        except (OSError, ValueError):
            continue
        reference_hash = hashlib.sha256(reference_data).hexdigest()
        if any(hashlib.sha256(item["source_data"]).hexdigest() == reference_hash
               for item in candidates):
            continue
        candidates.append({
            "provider_card_id": reference["provider_card_id"],
            "printing_id": reference["printing_id"],
            "face_index": face_index, "language": "fr",
            "source_data": reference_data, "mime_type": reference["mime_type"],
            "source_width": reference_width, "source_height": reference_height,
            "source_url": "local-same-name-reference", "visual_only": True,
            "text_reference_only": True,
            "edition_label": f"{reference['edition_name']} · {reference['set_code']} · nº {reference['collector_number']}"})
    if old and old["status"] == "ready" and old_path and (old["language"] or "fr") in {"fr", "en"}:
        image_path = _safe_child(CATALOGUE_ROOT / "versions" /
                                 resolve_current(CATALOGUE_ROOT)["version_id"], old_path)
        if image_path.is_file() and image_path.stat().st_size <= 20 * 1024 * 1024:
            image_bytes = image_path.read_bytes()
            try:
                with Image.open(io.BytesIO(image_bytes)) as old_image:
                    old_image.load()
                    width, height = old_image.size
            except (OSError, ValueError):
                image_bytes = b""
            known_hashes = {hashlib.sha256(item["source_data"]).hexdigest() for item in candidates}
            local_hash = hashlib.sha256(image_bytes).hexdigest() if image_bytes else None
            same_content = next((item for item in candidates
                                 if hashlib.sha256(item["source_data"]).hexdigest() == local_hash), None)
            if same_content and same_content["printing_id"] == old["printing_id"]:
                same_content["local_original_reference"] = True
            elif image_bytes:
                candidates.append({"provider_card_id": provider_id,
                    "printing_id": old["printing_id"], "face_index": face_index,
                    "language": old["language"] or "fr", "source_data": image_bytes,
                    "mime_type": old["mime_type"], "source_width": width,
                    "source_height": height, "source_url": "local-current-asset",
                    "visual_only": True, "local_original_reference": True})
    if not candidates:
        return {"status": "no_candidate", "detail": "Aucune image de référence disponible."}
    if cancel_event.is_set():
        return {"status": "cancelled"}
    candidate_id = _store_image_candidate(candidates)
    return {"status": "candidate", "candidate_id": candidate_id,
                "candidates": [{"index": index, "preview_url": f"/api/v2/image/candidate?id={candidate_id}&index={index}",
                                "language": candidate["language"], "width": candidate["source_width"],
                                "height": candidate["source_height"],
                                "printing_id": candidate["printing_id"], "face_index": candidate["face_index"],
                                "visual_only": bool(candidate.get("visual_only")),
                                "text_reference_only": bool(candidate.get("text_reference_only")),
                                "local_original_reference": bool(candidate.get("local_original_reference")),
                                "edition_label": candidate.get("edition_label")}
                               for index, candidate in enumerate(candidates)],
                "old_image_path": old_path}


def _card_usage(catalog_db: Path, provider_id: str) -> dict:
    provider_id = str(provider_id)
    """Find confirmed local uses at printing and oracle levels, without network access."""
    def blank():
        return {"editions": [], "decks": [], "personal_decks": []}

    exact, global_usage = blank(), blank()
    result = {"found": False, "exact": exact, "global": global_usage,
              **global_usage, "unloaded_decks": 0}
    proxy = PROXIES.linked(catalog_db, provider_id) if provider_id.startswith("proxy-") else None
    if provider_id.startswith("proxy-") and not proxy:
        return result
    source_id = proxy[0]["proxy_source_id"] if proxy else provider_id
    with connect(catalog_db) as conn:
        card = conn.execute("SELECT p.printing_id,p.oracle_id FROM source_records sr JOIN printings p USING(printing_id) WHERE sr.provider_card_id=?", (source_id,)).fetchone()
        if not card:
            return result
        oracle, printing = card["oracle_id"], card["printing_id"]
        rows = conn.execute("""SELECT p.printing_id,e.code,e.name,e.released_at,p.collector_number
            FROM printings p JOIN editions e USING(set_id)
            WHERE e.eligibility='eligible' AND (p.oracle_id=? OR p.printing_id=?)
            AND EXISTS (SELECT 1 FROM source_records sr WHERE sr.printing_id=p.printing_id)
            ORDER BY p.printing_id<>?, CAST(p.collector_number AS INTEGER),p.collector_number,p.printing_id
            """, (oracle, printing, printing)).fetchall()
        printings = {row["printing_id"] for row in rows} | {printing}
        proxy_printings = PROXIES.printing_ids(catalog_db, oracle)
        if proxy:
            proxy_printings.add(proxy[0]["printing_id"])
        id_to_printing = {r[0]: r[1] for r in conn.execute(
            "SELECT sr.provider_card_id,p.printing_id FROM source_records sr JOIN printings p USING(printing_id) WHERE p.oracle_id=? OR p.printing_id=?", (oracle, printing))}
        set_numbers = {}
        for row in rows:
            code, number = row["code"], row["collector_number"]
            count = conn.execute("SELECT count(*) FROM printings p JOIN editions e USING(set_id) WHERE upper(e.code)=upper(?) AND p.collector_number=? AND e.eligibility='eligible'", (code, number)).fetchone()[0]
            if count == 1 and code and number:
                set_numbers[(code.upper(), number)] = row["printing_id"]
        for usage, selected in ((exact, [r for r in rows if r["printing_id"] == printing]), (global_usage, rows)):
            editions = {}
            for row in selected:
                code = row["code"]
                editions.setdefault(code, {"code": code, "name": row["name"], "name_fr": _SET_NAMES_FR.get(code.upper()), "released_at": row["released_at"], "printing_id": row["printing_id"]})
            usage["editions"] = sorted(editions.values(), key=lambda x: (x["released_at"] or "", x["code"]), reverse=True)
        icons = _edition_icon_urls({row["code"].lower() for row in rows}, OPS_ROOT / "catalogue.sqlite")
        for usage in (exact, global_usage):
            for edition in usage["editions"]:
                edition["edition_icon_url"] = icons.get(edition["code"].lower())
        if proxy_printings:
            proxy_target = proxy[0]["printing_id"] if proxy else min(proxy_printings)
            proxy_edition = {"code": "proxy", "name": "Proxy", "name_fr": "Proxy",
                             "released_at": None, "printing_id": proxy_target}
            global_usage["editions"].insert(0, proxy_edition)
        if proxy:
            exact["editions"] = [proxy_edition]

        matches = {"exact": {}, "global": {}}
        if DECK_DB.exists():
            with sqlite3.connect(f"{DECK_DB.resolve().as_uri()}?mode=ro", uri=True) as deck_conn:
                deck_conn.row_factory = sqlite3.Row
                result["unloaded_decks"] = deck_conn.execute("SELECT count(*) FROM decks WHERE loaded=0").fetchone()[0]
                for row in deck_conn.execute("""SELECT d.id,d.name,d.name_fr,d.set_code,d.set_name,d.set_name_fr,d.colors,
                        c.set_code AS card_set,c.collector_number,c.raw_json
                        FROM decks d JOIN deck_cards c ON c.deck_id=d.id WHERE d.loaded=1"""):
                    try:
                        raw = json.loads(row["raw_json"])
                        ids = raw.get("identifiers") or {}
                        source_id = raw.get("scryfall_id") or ids.get("scryfallId")
                        explicit_oracle = raw.get("oracle_id") or ids.get("scryfallOracleId") or ids.get("oracleId")
                        resolved = id_to_printing.get(source_id)
                        set_match = set_numbers.get((str(row["card_set"] or "").upper(), row["collector_number"]))
                        if not resolved and not set_match and not (oracle and explicit_oracle == oracle):
                            continue
                        if source_id and not resolved:
                            known = conn.execute("SELECT printing_id FROM source_records WHERE provider_card_id=?", (source_id,)).fetchone()
                            if known:
                                continue
                        if resolved and row["card_set"]:
                            valid_set = conn.execute("SELECT 1 FROM printings p JOIN editions e USING(set_id) WHERE p.printing_id=? AND upper(e.code)=upper(?) AND e.eligibility='eligible'", (resolved, row["card_set"])).fetchone()
                            if not valid_set:
                                continue
                        if not resolved:
                            resolved = set_match
                        if resolved and explicit_oracle and oracle and explicit_oracle != oracle:
                            continue
                        is_exact = resolved == printing
                        is_global = resolved in printings or (bool(oracle) and explicit_oracle == oracle)
                    except (ValueError, TypeError, AttributeError):
                        continue
                    item = {key: row[key] for key in ("id", "name", "name_fr", "set_code", "set_name", "set_name_fr")}
                    item["colors"] = list(row["colors"]) if row["colors"] is not None else None
                    if is_exact and not proxy:
                        matches["exact"][row["id"]] = item
                    if is_global:
                        matches["global"][row["id"]] = item
        for key, usage in (("exact", exact), ("global", global_usage)):
            usage["decks"] = sorted(matches[key].values(), key=lambda d: (d["name_fr"] or d["name"]).casefold())
        if PERSONAL_DECK_DB.exists():
            with sqlite3.connect(f"{PERSONAL_DECK_DB.resolve().as_uri()}?mode=ro", uri=True) as personal_conn:
                for (document,) in personal_conn.execute("SELECT document FROM personal_decks"):
                    deck = json.loads(document)
                    if deck.get("deleted_at"):
                        continue
                    cards = [c for section in personal_decks.composition_sections(deck) for c in section.get("cards", [])]
                    resolved = {c.get("printing_id") or id_to_printing.get(c.get("provider_card_id")) for c in cards}
                    item = {"id": deck["id"], "name": deck["name"], "colors": deck.get("colors")}
                    if (proxy[0]["printing_id"] if proxy else printing) in resolved:
                        exact["personal_decks"].append(item)
                    if resolved & (printings | proxy_printings):
                        global_usage["personal_decks"].append(item)
        for usage in (exact, global_usage):
            usage["personal_decks"].sort(key=lambda d: d["name"].casefold())
    result.update(found=True, **global_usage)
    return result


class LibraryHandler(BaseHTTPRequestHandler):
    server_version = "MagicLibrary/2"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Keep the local console useful without logging card text or user queries.
        if self.path.startswith("/api/v2/health"):
            return
        super().log_message(fmt, *args)

    @property
    def db(self) -> Path:
        return resolve_current(CATALOGUE_ROOT)["database"]

    def _send(self, status: int, body: bytes, content_type: str, *, extra_headers=None, csp=None, cache_control="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp or "default-src 'self'; img-src 'self' data: https://svgs.scryfall.io; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, status=200):
        self._send(status, _json_bytes(value), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        params = _params(parsed.query)
        route = parsed.path
        try:
            if route == "/api/v2/card/usage":
                self._json(_card_usage(self.db, params.get("id", "")))
                return
            if route == "/api/v2/recent-activity":
                database = self.db
                self._json({"personal_decks": [
                    personal_decks.summary_with_media(deck, database, lambda identifier: PROXIES.linked(database, identifier))
                    for deck in personal_decks.recent_decks(PERSONAL_DECK_DB, 7)]})
                return
            if route == "/api/v2/personal-decks":
                items = personal_decks.list_decks(PERSONAL_DECK_DB, params.get("q", ""),
                                                  list(params.get("colors", "")), include_groups=True,
                                                  include_creation_order=True)
                database = self.db
                with_media = (personal_decks.summary_with_media if params.get("summary") == "1"
                              else personal_decks.with_media)
                self._json({"items": [with_media(
                    deck, database, lambda identifier: PROXIES.linked(database, identifier)) for deck in items],
                    "groups": personal_decks.list_deck_groups(PERSONAL_DECK_DB)})
                return
            if route == "/api/v2/personal-deck":
                deck = personal_decks.get_deck(PERSONAL_DECK_DB, params.get("id", ""))
                database = self.db
                self._json({"deck": personal_decks.with_media(
                    deck, database, lambda identifier: PROXIES.linked(database, identifier))})
                return
            if route == "/api/v2/notifications":
                with _IMAGE_TASKS_LOCK:
                    items = []
                    for item in _IMAGE_TASKS.values():
                        if item["kind"] == "ai_generation" and item["status"] == "cancelled":
                            continue
                        summary = {key: value for key, value in item.items() if key != "result"}
                        if isinstance(item.get("result"), dict):
                            summary["candidate_id"] = item["result"].get("id")
                            summary["preview_url"] = item["result"].get("preview_url")
                        items.append(summary)
                with _BATCH_LOCK:
                    if _BATCH_TASK:
                        items.append({**_BATCH_TASK, "kind": "batch_download", "label": "Images d’édition"})
                with _DECK_TASKS_LOCK:
                    items.extend({**item, "kind": "deck_download", "label": "Images de deck"}
                                 for item in _DECK_TASKS.values())
                maintenance = _maintenance_snapshot()
                if maintenance.get("status") not in {None, "idle"}:
                    items.append({**maintenance, "kind": "maintenance", "label": "Mise à jour du catalogue"})
                self._json({"items": items})
                return
            if route == "/api/v2/image/task":
                task_id = params.get("task_id", "")
                with _IMAGE_TASKS_LOCK:
                    item = _IMAGE_TASKS.get(task_id)
                    result = dict(item) if item else None
                self._json(result if result else {"error": "task_not_found"}, 200 if result else 404)
                return
            if route in {"/", "/index.html", "/1", "/2"}:
                file = _safe_child(UI_ROOT, "index.html")
                self._send(200, file.read_bytes(), "text/html; charset=utf-8")
            elif route == "/api/v2/demo-images":
                with sqlite3.connect(self.db) as connection:
                    connection.row_factory = sqlite3.Row
                    rows = connection.execute("""SELECT sr.provider_card_id, sr.printing_id, p.oracle_id, sr.lang,
                        ia.relative_path, ia.width, ia.height, ia.language AS image_language, sr.name
                        FROM image_slots s
                        JOIN printings p ON p.printing_id = s.printing_id
                        JOIN source_records sr ON sr.printing_id = p.printing_id
                        JOIN image_assets ia ON ia.asset_id = s.asset_id
                        WHERE s.face_index = 0 AND s.status = 'ready' AND p.oracle_id IS NOT NULL
                        AND sr.lang IN ('fr', 'en')
                        LIMIT 1000""").fetchall()
                    pair = next(((a, b) for a in rows for b in rows
                                 if a["oracle_id"] == b["oracle_id"] and a["printing_id"] != b["printing_id"]
                                 and a["relative_path"] != b["relative_path"]), None)
                    if params.get("mode") == "ai":
                        originals = connection.execute("""SELECT sr.provider_card_id, sr.printing_id, p.oracle_id,
                            sr.lang, ia.relative_path, ia.width, ia.height, ia.language AS image_language, sr.name
                            FROM source_records sr JOIN printings p ON p.printing_id = sr.printing_id
                            JOIN image_slots s ON s.printing_id = p.printing_id AND s.face_index = 0
                            JOIN image_assets ia ON ia.asset_id = s.asset_id
                            WHERE sr.provider_card_id = 'f42ae0a0-f030-43d4-bddf-77d4b07a6c80'
                            AND sr.lang = 'en' AND ia.language = 'en'
                            LIMIT 1""").fetchone()
                        if originals:
                            pair = (originals, originals)
                if pair is None:
                    self._json({"error": "Aucune paire d'images locales disponible."}, 404)
                else:
                    current, alternative = pair
                    self._json({"provider_card_id": current["provider_card_id"],
                                "candidate": {"status": "candidate", "candidate_id": "simulation",
                                              "candidates": [{"index": 0, "language": alternative["lang"],
                                                "printing_id": alternative["printing_id"], "face_index": 0,
                                                "width": alternative["width"], "height": alternative["height"],
                                                "preview_url": "/assets?" + urllib.parse.urlencode({"path": alternative["relative_path"]})},
                                                {"index": 1, "language": current["lang"],
                                                 "printing_id": current["printing_id"], "face_index": 0,
                                                 "width": current["width"], "height": current["height"],
                                                 "preview_url": "/assets?" + urllib.parse.urlencode({"path": current["relative_path"]}),
                                                 "local_original_reference": True, "visual_only": True}]}})
            elif route == "/api/v2/demo-ai-image":
                candidates = sorted(card_candidate_root(OPS_ROOT).glob("*.png"))
                if candidates:
                    self._send(200, candidates[0].read_bytes(), "image/png")
                else:
                    self._json({"error": "Aucune image IA locale disponible."}, 404)
            elif route == "/deck-box.js":
                self._send(200, (UI_ROOT / "deck-box.js").read_bytes(), "text/javascript; charset=utf-8")
            elif route == "/deck-box-mesh.json":
                self._send(200, (UI_ROOT / "assets/deck-box-mesh.json").read_bytes(), "application/json")
            elif route == "/deck-box-grain.png":
                self._send(200, (UI_ROOT / "assets/deck-box-grain.png").read_bytes(), "image/png")
            elif route == "/deck-box-fallback.webp":
                self._send(200, (UI_ROOT / "assets/deck-box-fallback.webp").read_bytes(), "image/webp")
            elif (route.startswith("/deck-box-preview-") and route.endswith(".webp")
                  and route[len("/deck-box-preview-"):-len(".webp")] in personal_decks.BOX_DESIGNS):
                self._send(200, (UI_ROOT / "assets" / route[1:]).read_bytes(), "image/webp")
            elif (route.startswith("/deck-box-") and route.endswith(".webp")
                  and route[len("/deck-box-"):-len(".webp")] in personal_decks.PANORAMIC_BOX_DESIGNS):
                self._send(200, (UI_ROOT / "assets" / route[1:]).read_bytes(), "image/webp")
            elif route == "/app.js":
                file = _safe_child(UI_ROOT, "app.js")
                self._send(200, file.read_bytes(), "text/javascript; charset=utf-8")
            elif route == "/selects.js":
                file = _safe_child(UI_ROOT, "selects.js")
                self._send(200, file.read_bytes(), "text/javascript; charset=utf-8")
            elif route == "/styles.css":
                file = _safe_child(UI_ROOT, "styles.css")
                self._send(200, file.read_bytes(), "text/css; charset=utf-8")
            elif route == "/mana.css":
                file = _safe_child(UI_ROOT, "mana.css")
                self._send(200, file.read_bytes(), "text/css; charset=utf-8")
            elif route == "/mana.woff2":
                file = _safe_child(UI_ROOT, "assets/mana.woff2")
                self._send(200, file.read_bytes(), "font/woff2")
            elif route == "/logo.png":
                self._send(200, (OPS_ROOT.parent / "Logo.png").read_bytes(), "image/png")
            elif route == "/favicon.ico":
                self._send(200, (OPS_ROOT.parent / "Logo.ico").read_bytes(), "image/x-icon")
            elif route == "/api/v2/health":
                self._json({"status": "ok", "version_id": resolve_current(CATALOGUE_ROOT)["version_id"],
                            "app_api_revision": APP_API_REVISION})
            elif route == "/api/v2/stats":
                self._json(_stats(self.db, background_decks=params.get("background_decks") == "1"))
            elif route == "/api/v2/maintenance/update":
                self._json(_maintenance_snapshot())
            elif route == "/api/v2/maintenance/versions":
                self._json({"versions": list_version_candidates(CATALOGUE_ROOT)})
            elif route == "/api/v2/list-import":
                with _LIST_LOCK:
                    task = {**_LIST_TASK, "items": [dict(item) for item in _LIST_TASK["items"]]} if _LIST_TASK else {"status": "idle"}
                self._json(_enrich_result(self.db, task))
            elif route == "/api/v2/batch-download/task":
                with _BATCH_LOCK:
                    task = dict(_BATCH_TASK) if _BATCH_TASK and params.get("task_id") == _BATCH_TASK["task_id"] else None
                self._json(task if task else {"error": "task_not_found"}, 200 if task else 404)
            elif route == "/api/v2/deferred":
                self._json({"items": _deferred(self.db)})
            elif route == "/api/v2/decks":
                ensure_deck_index(DECK_DB)
                limit = max(1, min(200, int(params.get("limit", 50))))
                offset = max(0, int(params.get("offset", 0)))
                image_filter = params.get("image_filter") or "all"
                if image_filter not in {"all", "with", "without"}:
                    raise ValueError("Filtre d'image invalide")
                common = dict(query=params.get("q") or None, name_only=True,
                              deck_format=params.get("deck_format") or None,
                              deck_type=params.get("deck_type") or None,
                              set_code=params.get("set_code") or None,
                              colors=params.get("colors") or None,
                              color_mode=params.get("color_mode") or "contains",
                              sort=params.get("sort") or "date",
                              order=params.get("order") or "desc")
                if image_filter == "all":
                    result = list_decks(DECK_DB, **common, limit=limit, offset=offset)
                else:
                    matches = []
                    cursor = 0
                    media_context = _deck_media_context(self.db)
                    while True:
                        page = list_decks(DECK_DB, **common, limit=100, offset=cursor)
                        if cursor == 0:
                            total_candidates = page["total"]
                        for candidate in page["items"]:
                            has_cover = _deck_cover_ready(candidate["id"], self.db, media_context)
                            if has_cover == (image_filter == "with"):
                                matches.append(candidate)
                        cursor += len(page["items"])
                        if not page["items"] or cursor >= total_candidates:
                            break
                    result = {"contract_version": "1.0.0", "total": len(matches), "limit": limit,
                              "offset": offset, "items": matches[offset:offset + limit]}
                result = _with_deck_edition_names(result, self.db)
                items = result.get("items", [])
                edition_codes = [str(item.get("set_code") or "").lower() for item in items]
                icons = _edition_icon_urls(set(edition_codes), OPS_ROOT / "catalogue.sqlite")
                for item, code in zip(items, edition_codes):
                    item["edition_icon_url"] = icons.get(code)
                if items:
                    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="magic-deck-list") as executor:
                        media = executor.map(lambda item: _deck_list_media(item["id"], self.db), items)
                        for item, details in zip(items, media):
                            item.update(details)
                self._json(result)
            elif route == "/api/v2/decks/filters":
                ensure_deck_index(DECK_DB)
                self._json(_with_deck_edition_names(deck_facets(DECK_DB), self.db))
            elif route == "/api/v2/decks/colors/status":
                with _COLOR_INDEX_LOCK:
                    task = dict(_COLOR_INDEX_TASK) if _COLOR_INDEX_TASK else {"status": "idle"}
                self._json(task)
            elif route == "/api/v2/deck":
                ensure_deck_index(DECK_DB)
                deck_id = params.get("id", "")
                if not deck_id or len(deck_id) > 200:
                    raise ValueError("invalid deck id")
                result = get_deck(DECK_DB, deck_id, catalog_db=self.db, include_tokens=True)
                self._json(_with_deck_detail_edition_name(_deck_with_media(result, self.db), self.db))
            elif route == "/api/v2/deck/task":
                task_id = params.get("task_id", "")
                with _DECK_TASKS_LOCK:
                    task = dict(_DECK_TASKS[task_id]) if task_id in _DECK_TASKS else None
                self._json(task if task is not None else {"error": "task_not_found"}, 200 if task is not None else 404)
            elif route == "/api/v2/archive":
                limit = max(1, min(100, int(params.get("limit", 50))))
                offset = max(0, int(params.get("offset", 0)))
                status = params.get("status")
                entries = []
                for path in LIBRARY_ROOT.rglob("*"):
                    if (not path.is_file() or path.is_symlink()
                            or path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}):
                        continue
                    relative = path.relative_to(LIBRARY_ROOT)
                    if relative.parts[0].casefold() == "impossible":
                        continue
                    entries.append({"relative_path": relative.as_posix(), "file_name": path.name,
                                    "historical_folder": str(relative.parent), "identity_status": "unresolved",
                                    "format": path.suffix.lstrip(".").upper(),
                                    "display_as_card": False})
                entries.sort(key=lambda item: item["relative_path"].casefold())
                if HISTORICAL_RESOLVED.is_file():
                    resolved = json.loads(HISTORICAL_RESOLVED.read_text(encoding="utf-8"))
                    completed = resolved.get("entries", {})
                    display_names = resolved.get("display_names", {})
                    entries = [item for item in entries
                               if item["relative_path"].replace("\\", "/") not in completed]
                else:
                    display_names = {}
                if status in {"unresolved", "ambiguous"}:
                    entries = [x for x in entries if x.get("identity_status") == status]
                page = entries[offset:offset+limit]
                for entry in page:
                    name = display_names.get(entry["relative_path"]) if isinstance(display_names, dict) else None
                    if isinstance(name, str) and name.strip():
                        entry["display_name"] = name.strip()
                    entry["ocr_result"] = cached_archive_ocr(LIBRARY_ROOT, entry["relative_path"],
                        self.db, aliases_path=OPS_ROOT / "reports" / "edition_aliases.json")
                self._json({"total": len(entries), "limit": limit, "offset": offset, "items": page})
            elif route == "/api/v2/archive-image":
                path = _safe_child(LIBRARY_ROOT, params.get("path", ""))
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self._send(200, path.read_bytes(), content_type)
            elif route == "/api/v2/archive-lens-bridge":
                body = _archive_lens_bridge(_take_lens_ticket(params.get("ticket", "")))
                self._send(200, body, "text/html; charset=utf-8",
                           extra_headers={"Referrer-Policy": "no-referrer"},
                           csp="sandbox allow-scripts allow-forms; default-src 'none'; script-src 'unsafe-inline'; form-action https://www.google.com; frame-ancestors 'none'; base-uri 'none'")
            elif route == "/api/v2/image/candidate":
                candidates = _image_candidate(params.get("id", ""))
                try:
                    index = int(params.get("index", "0"))
                except ValueError:
                    index = -1
                if candidates is None or not 0 <= index < len(candidates):
                    self._json({"error": "candidate_expired"}, 404)
                else:
                    candidate = candidates[index]
                    self._send(200, candidate["source_data"], candidate["mime_type"])
            elif route == "/api/v2/image/ai/candidate":
                ai_id = params.get("id", "")
                with _AI_CANDIDATES_LOCK:
                    item = _AI_CANDIDATES.get(ai_id)
                if item is None:
                    self._json({"error": "candidate_expired"}, 404)
                else:
                    self._send(200, Path(item["path"]).read_bytes(), "image/png")
            elif route == "/assets":
                path = _safe_child(CATALOGUE_ROOT / "versions" / resolve_current(CATALOGUE_ROOT)["version_id"], params.get("path", ""))
                with connect(self.db) as conn:
                    asset = conn.execute("SELECT mime_type,asset_id FROM image_assets WHERE relative_path=?", (params.get("path", ""),)).fetchone()
                if asset is None:
                    self._json({"error": "asset_not_found"}, 404)
                else:
                    immutable = asset[1] in path.name
                    self._send(200, path.read_bytes(), asset[0], cache_control="private, max-age=31536000, immutable" if immutable else "no-store")
            elif route == "/api/v2/proxies/image":
                try:
                    data, content_type = PROXIES.image(params.get("id"))
                except FileNotFoundError:
                    self._json({"error": "proxy_not_found"}, 404)
                else:
                    self._send(200, data, content_type)
            elif route == "/api/v2/search" and params.get("snapshot") == "1":
                self._json(PROXIES.start_search(self.db, params, _enrich_result))
            elif route == "/api/v2/search/page":
                result = PROXIES.search_page(self.db, params, _enrich_result)
                if result is None:
                    self._json({"error": "search_expired", "detail": "Cette recherche a expiré. Cliquez sur Rechercher."}, 410)
                else:
                    self._json(result)
            elif route in {"/api/v2/proxies", "/api/v2/editions", "/api/v2/search", "/api/v2/card", "/api/v2/edition"}:
                if route != "/api/v2/card" or str(params.get("id", "")).startswith("proxy-"):
                    result = PROXIES.dispatch(self.db, route, params, _enrich_result)
                else:
                    result = api.dispatch(self.db, route, params)
                if route == "/api/v2/editions":
                    editions = result.get("items", [])
                    icons = _edition_icon_urls({edition["code"].lower() for edition in editions}, OPS_ROOT / "catalogue.sqlite")
                    for edition in editions:
                        edition["name_fr"] = _SET_NAMES_FR.get(edition["code"].upper())
                        edition["edition_icon_url"] = icons.get(edition["code"].lower())
                if route == "/api/v2/card" and result.get("found") and not result["card"].get("proxy"):
                    card = result["card"]
                    code = (card.get("set_code") or "").lower()
                    card["edition_icon_url"] = _edition_icon_urls({code}, OPS_ROOT / "catalogue.sqlite").get(code)
                    with connect(self.db) as conn:
                        slots = conn.execute(
                            "SELECT s.face_index,s.preferred_language,s.selected_language,s.status,s.asset_id,"
                            "a.relative_path,a.width,a.height,a.language AS asset_language,a.provenance_json "
                            "FROM image_slots s LEFT JOIN image_assets a USING(asset_id) WHERE s.printing_id=? ORDER BY s.face_index",
                            (card["printing_id"],),
                        ).fetchall()
                    card["image_slots"] = []
                    for row in slots:
                        entry = dict(row)
                        if entry.get("provenance_json"):
                            try:
                                entry["provenance"] = json.loads(entry.pop("provenance_json"))
                            except json.JSONDecodeError:
                                entry["provenance"] = {"error": "invalid provenance JSON"}
                        else:
                            entry.pop("provenance_json", None)
                        card["image_slots"].append(entry)
                    # Keep physical face details tied to this exact printing. Prefer
                    # the French source record when it exists, then the requested
                    # record; never infer a face from a similarly named printing.
                    face_row = None
                    with connect(self.db) as conn:
                        face_row = conn.execute(
                            "SELECT raw_json,lang FROM source_records WHERE printing_id=? "
                            "ORDER BY CASE lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 ELSE 2 END,provider_card_id LIMIT 1",
                            (card["printing_id"],),
                        ).fetchone()
                    faces = []
                    if face_row:
                        try:
                            raw = json.loads(face_row["raw_json"])
                            for index, face in enumerate(raw.get("card_faces") or []):
                                if isinstance(face, dict):
                                    faces.append({
                                        "face_index": index,
                                        "name": face.get("printed_name") or face.get("name"),
                                        "type_line": face.get("printed_type_line") or face.get("type_line"),
                                        "mana_cost": face.get("mana_cost"),
                                        "oracle_text": face.get("printed_text") or face.get("oracle_text"),
                                        "language": face_row["lang"],
                                    })
                        except (TypeError, json.JSONDecodeError):
                            faces = []
                    card["faces"] = faces
                self._json(result)
            else:
                self._json({"error": "route_not_found"}, 404)
        except KeyError as exc:
            self._json({"error": "deck_not_found", "detail": str(exc)}, 404)
        except (ValueError, FileNotFoundError, json.JSONDecodeError):
            self._json({"error": "invalid_or_missing_resource"}, 400)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._json({"error": "request_failed", "detail": str(exc)}, 500)

    def do_POST(self):
        global _LIST_TASK, _BATCH_TASK
        route = urllib.parse.urlsplit(self.path).path
        if route not in PERSONAL_POST_ROUTES and route not in {PROXY_ASSOCIATE, PROXY_OPEN_FOLDER, PROXY_IMPORT, PROXY_DELETE, "/api/v2/search/image-slots", "/api/v2/app/shutdown", "/api/v2/list-import", "/api/v2/list-import/export", "/api/v2/image/export", "/api/v2/download", "/api/v2/download/cancel", "/api/v2/image/refresh", "/api/v2/image/preview", "/api/v2/image/candidate/choose", "/api/v2/image/ai/generate", "/api/v2/image/ai/choose", "/api/v2/image/task/cancel", "/api/v2/image/import", "/api/v2/deck/download", "/api/v2/deck/export", "/api/v2/deck/cancel", "/api/v2/batch-download", "/api/v2/batch-download/cancel", "/api/v2/decks/refresh", "/api/v2/decks/recalculate", "/api/v2/decks/colors/index", "/api/v2/maintenance/update", "/api/v2/maintenance/update/cancel", "/api/v2/maintenance/activate", "/api/v2/archive/ocr", "/api/v2/archive/hash", "/api/v2/archive/lens", "/api/v2/archive/delete", "/api/v2/archive/auto-name", "/api/v2/archive/search", "/api/v2/archive/target", "/api/v2/archive/import"}:
            self._json({"error": "route_not_found"}, 404)
            return
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        local_origin = f"http://localhost:{self.server.server_port}"
        origin = self.headers.get("Origin")
        if origin and origin not in {expected_origin, local_origin}:
            self._json({"error": "origin_rejected"}, 403)
            return
        if route in {"/api/v2/app/shutdown", PROXY_OPEN_FOLDER, PROXY_IMPORT, PROXY_DELETE} and origin not in {expected_origin, local_origin}:
            self._json({"error": "origin_required"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if route == PROXY_IMPORT:
                if not 0 < length <= 20 * 1024 * 1024:
                    raise ValueError("L’image proxy doit peser au plus 20 Mio.")
                filename = _params(urllib.parse.urlsplit(self.path).query).get("filename", "")
                self._json(PROXIES.import_image(filename, self.rfile.read(length)), 201)
                return
            if route == "/api/v2/image/import":
                params = _params(urllib.parse.urlsplit(self.path).query)
                provider_id = params.get("provider_card_id", "")
                face_index = params.get("face_index", "")
                if (not isinstance(provider_id, str) or not 0 < len(provider_id) <= 100
                        or not str(face_index).isdigit() or not 0 <= int(face_index) < 10
                        or not 0 < length <= 20 * 1024 * 1024):
                    self._json({"error": "invalid_import_request"}, 400)
                    return
                image_bytes = self.rfile.read(length)
                if provider_id.startswith("proxy-"):
                    if int(face_index) != 0:
                        raise ValueError("Un proxy possède une seule image locale.")
                    result = PROXIES.replace_image(self.db, provider_id, image_bytes)
                    self._json({"status": "ready", "result": result})
                    return
                with _MAINTENANCE_LOCK:
                    if _maintenance_is_active():
                        self._json({"error": "maintenance_in_progress"}, 409)
                        return
                    result = import_local_image(provider_id, int(face_index), image_bytes,
                                                catalogue_root=CATALOGUE_ROOT)
                self._json({"status": "ready", "result": result})
                return
            maximum = 2 * 1024 * 1024 if route in PERSONAL_POST_ROUTES else (32768 if route in {"/api/v2/list-import", "/api/v2/search/image-slots"} else 4096)
            if length < 1 or length > maximum:
                self._json({"error": "invalid_request_size"}, 400)
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                self._json({"error": "json_object_required"}, 400)
                return
            if route == PROXY_OPEN_FOLDER:
                if payload:
                    raise ValueError("La demande d’ouverture du dossier proxy doit être vide.")
                PROXIES.root.mkdir(parents=True, exist_ok=True)
                os.startfile(PROXIES.root)
                self._json({"opened": True})
                return
            if route == PROXY_ASSOCIATE:
                self._json(PROXIES.associate(self.db, payload.get("provider_card_id"), payload.get("source_id")))
                return
            if route == PROXY_DELETE:
                if set(payload) != {"provider_card_id", "confirmed"} or payload["confirmed"] is not True:
                    raise ValueError("La suppression du fichier proxy doit être confirmée.")
                try:
                    result = PROXIES.delete(payload["provider_card_id"])
                except FileNotFoundError:
                    self._json({"error": "proxy_not_found", "detail": "Proxy introuvable dans le dossier."}, 404)
                    return
                except OSError as exc:
                    raise ValueError("Le fichier du proxy n’a pas pu être supprimé.") from exc
                self._json(result)
                return
            if route in PERSONAL_POST_ROUTES:
                action = route.rsplit("/", 1)[1]
                if action == "group-create":
                    self._json({"group": personal_decks.create_deck_group(PERSONAL_DECK_DB, payload.get("name"))})
                    return
                if action == "group-rename":
                    self._json({"group": personal_decks.rename_deck_group(
                        PERSONAL_DECK_DB, payload.get("group_id"), payload.get("name"), payload.get("move", 0))})
                    return
                if action == "group-delete":
                    self._json({"group": personal_decks.delete_deck_group(PERSONAL_DECK_DB, payload.get("group_id"))})
                    return
                if action == "group-move":
                    fields = {"deck_id", "group_id"}
                    if not fields <= payload.keys() or payload.keys() - fields - {"before_deck_id"}:
                        raise ValueError("Le deck et sa section doivent être indiqués.")
                    self._json(personal_decks.move_deck_to_group(PERSONAL_DECK_DB, **payload))
                    return
                if action == "export-text":
                    result = personal_deck_text.export_text(PERSONAL_DECK_DB, payload.get("deck_id"))
                    self._json({**result, "path": result["file"]})
                    return
                database = self.db
                def proxy_lookup(identifier):
                    return PROXIES.linked(database, identifier)
                if action == "import-text":
                    result = personal_deck_text.import_text(
                        PERSONAL_DECK_DB, database, payload.get("text"), payload.get("filename", ""))
                    result["deck"] = personal_decks.summary_with_media(result["deck"], database, proxy_lookup)
                    self._json(result)
                    return
                if action == "create":
                    deck = personal_decks.create_deck(PERSONAL_DECK_DB, payload.get("name"),
                            payload.get("colors", []), payload.get("commander", False))
                elif action == "save":
                    deck = personal_decks.save_deck(PERSONAL_DECK_DB, payload.get("deck"))
                elif action == "duplicate":
                    deck = personal_decks.duplicate_deck(PERSONAL_DECK_DB, payload.get("deck_id"),
                            payload.get("name"), payload.get("revision"))
                elif action == "delete":
                    deleted = personal_decks.delete_deck(PERSONAL_DECK_DB, payload.get("deck_id"),
                                                         payload.get("revision"))
                    self._json({"deleted": True, "deck_id": deleted["id"], "revision": deleted["revision"]})
                    return
                elif action == "add":
                    provider_id = payload.get("provider_card_id")
                    if isinstance(provider_id, str) and provider_id.startswith("proxy-"):
                        with PROXIES.lock:
                            if not proxy_lookup(provider_id):
                                raise ValueError("Proxy associé introuvable.")
                            deck = personal_decks.add_card(PERSONAL_DECK_DB, payload.get("deck_id"), PROXIES.database,
                                    provider_id, payload.get("printing_id"), payload.get("section_id"))
                    else:
                        deck = personal_decks.add_card(PERSONAL_DECK_DB, payload.get("deck_id"), database,
                                provider_id, payload.get("printing_id"), payload.get("section_id"))
                elif action == "import":
                    source_result = get_deck(DECK_DB, payload.get("deck_id"), catalog_db=self.db,
                                             fetch_missing=False, include_tokens=True)
                    if not source_result.get("found") or not source_result.get("deck"):
                        raise ValueError("Préconstruit introuvable.")
                    source = source_result["deck"]
                    if not any(source.get("sections", {}).values()):
                        raise ValueError("Ouvrez le préconstruit pour charger sa composition avant de le copier.")
                    deck = personal_decks.import_preconstructed(PERSONAL_DECK_DB, source, self.db)
                else:
                    self._json(personal_decks.export_deck(PERSONAL_DECK_DB, payload.get("deck_id"), database,
                                                        proxy_lookup=proxy_lookup))
                    return
                self._json({"deck": personal_decks.with_media(deck, database, proxy_lookup)})
                return
            if route == "/api/v2/search/image-slots":
                printing_ids = payload.get("printing_ids")
                if (set(payload) != {"printing_ids"} or not isinstance(printing_ids, list)
                        or not 1 <= len(printing_ids) <= 200
                        or any(not isinstance(value, str) or not 0 < len(value) <= 160
                               for value in printing_ids)):
                    self._json({"error": "invalid_image_slot_request"}, 400)
                    return
                ids = list(dict.fromkeys(printing_ids))
                self._json({"image_slots": _image_slots_for_printings(self.db, ids)})
                return
            if route == "/api/v2/app/shutdown":
                if payload:
                    raise ValueError("Aucun paramètre attendu.")
                self._json({"status": "stopping"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if route == "/api/v2/image/task/cancel":
                task_id = payload.get("task_id")
                if set(payload) != {"task_id"} or not isinstance(task_id, str):
                    self._json({"error": "invalid_cancel_request"}, 400)
                    return
                with _IMAGE_TASKS_LOCK:
                    task = _IMAGE_TASKS.get(task_id)
                    cancel_event = _IMAGE_TASK_CANCELS.get(task_id)
                    if task is None:
                        self._json({"error": "task_not_found"}, 404)
                        return
                    if task["status"] in {"queued", "running"} and cancel_event:
                        cancel_event.set()
                        task["status"] = "cancelling"
                        self._json({"task_id": task_id, "status": "cancelling"}, 202)
                    elif task["kind"] == "ai_generation" and task["status"] == "error":
                        task.update(status="cancelled", detail="")
                        self._json({"task_id": task_id, "status": "cancelled"})
                    else:
                        self._json({"task_id": task_id, "status": task["status"], "cancel_requested": False})
                return
            if route == "/api/v2/decks/recalculate":
                self._json(_recalculate_deck_search(payload, self.db))
                return
            if route == "/api/v2/image/ai/generate":
                source_id = payload.get("candidate_id")
                visual_index, text_index = payload.get("visual_index"), payload.get("text_index")
                text_mode = payload.get("text_mode", "image")
                online_text = text_mode == "online"
                correction = payload.get("correction_prompt", "")
                previous_id = payload.get("previous_id")
                candidates = _image_candidate(source_id)
                if (candidates is None or text_mode not in {"image", "online", "translate"}
                        or type(visual_index) is not int or not 0 <= visual_index < len(candidates)
                        or (text_mode == "image" and (type(text_index) is not int
                            or not 0 <= text_index < len(candidates)))
                        or (text_mode != "image" and text_index is not None)
                        or not isinstance(correction, str) or len(correction) > 4000):
                    self._json({"error": "invalid_ai_request"}, 400)
                    return
                with _AI_CANDIDATES_LOCK:
                    previous = _AI_CANDIDATES.get(previous_id) if previous_id else None
                if previous_id and (previous is None or previous["source_id"] != source_id):
                    self._json({"error": "candidate_expired"}, 404)
                    return
                visual = candidates[visual_index]
                french = candidates[text_index] if text_mode == "image" else None
                text_reference = None
                if text_mode != "image" and (visual.get("printing_id") is None or visual.get("face_index") is None):
                    self._json({"error": "reference_identity_missing"}, 400)
                    return
                if text_mode == "online":
                    try:
                        text_reference = _lookup_local_card_text(
                            self.db, visual["provider_card_id"], visual["face_index"])
                    except FrenchTextUnavailable as exc:
                        self._json({"status": "translation_unavailable", "detail": str(exc)})
                        return
                    except ValueError as exc:
                        self._json({"error": str(exc)}, 409)
                        return
                if text_mode == "image":
                    same_printing = (visual["printing_id"] == french["printing_id"]
                                     and visual["face_index"] == french["face_index"])
                    same_name_reference = bool(
                        french.get("text_reference_only")
                        and _same_name_text_reference(self.db, visual["provider_card_id"],
                                                      french["provider_card_id"], visual["face_index"]))
                    local_original = bool(french.get("local_original_reference") and same_printing)
                    if not same_printing and not same_name_reference:
                        self._json({"error": "reference_mismatch"}, 400)
                        return
                    if french["language"] != "fr" and not local_original:
                        self._json({"error": "reference_language_mismatch"}, 400)
                        return
                prior_path = Path(previous["path"]) if previous else None
                def generate(cancel_event):
                    reuse_text = (previous and previous.get("text_mode") == text_mode
                                  and previous.get("visual_index") == visual_index
                                  and previous.get("text_index") == text_index)
                    current_text_reference = (previous.get("french_text_reference")
                                              if reuse_text else text_reference)
                    with tempfile.TemporaryDirectory(prefix="magic-ai-input-") as temp:
                        visual_path = Path(temp) / "visual.png"
                        visual_path.write_bytes(visual["source_data"])
                        if french is not None and current_text_reference is None:
                            text_path = Path(temp) / "text-reference.png"
                            text_path.write_bytes(french["source_data"])
                            fields = extract_card_text_reference(
                                text_path, translate_to_french=french["language"] != "fr",
                                cancel_event=cancel_event)
                            current_text_reference = {
                                "fields": fields, "source": "codex_text_reference",
                                "verified": False, "language": "fr",
                                "provider_card_id": french["provider_card_id"],
                                "source_card_ids": [french["provider_card_id"]],
                                "printing_id": french["printing_id"],
                                "face_index": french["face_index"],
                            }
                        result = generate_card_candidate(visual_path, None,
                            card_candidate_root(OPS_ROOT), correction,
                            previous_path=prior_path,
                            french_text=current_text_reference["fields"] if current_text_reference else None,
                            translate_text=text_mode == "translate",
                            cancel_event=cancel_event)
                    if cancel_event.is_set():
                        Path(result["path"]).unlink(missing_ok=True)
                        raise CardGenerationError("Génération annulée.")
                    ai_id = result["candidate_id"]
                    with _AI_CANDIDATES_LOCK:
                        _AI_CANDIDATES[ai_id] = {**result, "source_id": source_id,
                            "provider_card_id": visual["provider_card_id"], "face_index": visual["face_index"],
                            "printing_id": visual["printing_id"], "french_text_reference": current_text_reference,
                            "visual_index": visual_index, "text_index": text_index,
                            "text_mode": text_mode, "correction_prompt": correction}
                    return {"id": ai_id, "preview_url": f"/api/v2/image/ai/candidate?id={ai_id}",
                            "source_candidate_id": source_id, "visual_index": visual_index,
                            "text_index": text_index, "text_mode": text_mode,
                            "correction_prompt": correction,
                            "provider_card_id": visual["provider_card_id"],
                            "face_index": visual["face_index"], "printing_id": visual["printing_id"],
                            "french_text": current_text_reference["fields"] if current_text_reference else None,
                            "text_provenance": ({key: value for key, value in current_text_reference.items()
                                                  if key != "fields"} if current_text_reference else None)}
                task = _start_image_task("ai_generation", "Génération d’image IA", generate,
                    metadata={"source_candidate_id": source_id, "visual_index": visual_index,
                              "text_index": text_index, "text_mode": text_mode,
                              "correction_prompt": correction,
                              "visual_preview_url": f"/api/v2/image/candidate?id={source_id}&index={visual_index}",
                              "text_preview_url": f"/api/v2/image/candidate?id={source_id}&index={text_index}",
                              "provider_card_id": visual["provider_card_id"],
                              "face_index": visual["face_index"], "printing_id": visual["printing_id"]})
                self._json(task, 202)
                return
            if route == "/api/v2/image/ai/choose":
                ai_id, choice = payload.get("id"), payload.get("choice")
                if choice not in {"old", "new"} or not isinstance(ai_id, str):
                    self._json({"error": "invalid_ai_choice"}, 400)
                    return
                with _AI_CANDIDATES_LOCK:
                    item = _AI_CANDIDATES.get(ai_id)
                if item is None:
                    self._json({"error": "candidate_expired"}, 404)
                    return
                if choice == "old":
                    with _AI_CANDIDATES_LOCK:
                        _AI_CANDIDATES.pop(ai_id, None)
                    with _IMAGE_TASKS_LOCK:
                        for task in _IMAGE_TASKS.values():
                            if (task.get("kind") == "ai_generation" and (task.get("result") or {}).get("id") == ai_id):
                                task.update(status="ready", result={**task["result"], "decision": "kept_existing"})
                    self._json({"status": "kept_existing"})
                    return
                with _MAINTENANCE_LOCK:
                    if _maintenance_is_active():
                        self._json({"error": "maintenance_in_progress"}, 409)
                        return
                    result = import_local_image(item["provider_card_id"], item["face_index"],
                        Path(item["path"]).read_bytes(), catalogue_root=CATALOGUE_ROOT,
                        language="fr", source_evidence=f"codex-imagegen:{item['sha256']}")
                with _AI_CANDIDATES_LOCK:
                    _AI_CANDIDATES.pop(ai_id, None)
                source_id = item.get("source_id")
                if source_id:
                    _image_candidate(source_id, remove=True)
                with _IMAGE_TASKS_LOCK:
                    for task in _IMAGE_TASKS.values():
                        if (task.get("kind") == "ai_generation" and (task.get("result") or {}).get("id") == ai_id):
                            task.update(status="ready", result={**task["result"], "decision": "published"})
                        elif (source_id and task.get("kind") == "image_preview"
                              and (task.get("result") or {}).get("candidate_id") == source_id):
                            task["status"] = "complete"
                self._json({"status": "ready", "result": result})
                return
            if route == "/api/v2/archive/search":
                allowed = {"query", "set_code", "language", "limit", "offset"}
                if set(payload) - allowed or "query" not in payload:
                    raise ValueError("Paramètres de recherche d’archive invalides")
                result = _enrich_result(self.db, search_archive_printings(self.db, payload["query"],
                    set_code=payload.get("set_code"), language=payload.get("language"),
                    limit=payload.get("limit", 30), offset=payload.get("offset", 0)))
                self._json(result)
                return
            if route == "/api/v2/archive/target":
                if set(payload) != {"provider_card_id", "printing_id", "face_index", "language"}:
                    raise ValueError("Cible d’archive invalide")
                result = describe_archive_target(self.db, **payload)
                self._json({"target": result, "manual_review_required": True})
                return
            if route == "/api/v2/archive/ocr":
                if set(payload) != {"path"} or not isinstance(payload["path"], str):
                    raise ValueError("Chemin d’archive attendu")
                result = propose_archive_ocr(LIBRARY_ROOT, payload["path"], self.db,
                    aliases_path=OPS_ROOT / "reports" / "edition_aliases.json")
                with _MAINTENANCE_LOCK:
                    if _maintenance_is_active() or _batch_is_active():
                        result["automation"] = {"status": "review_required",
                            "reason": "Opération de maintenance ou téléchargement en cours"}
                    else:
                        result["automation"] = auto_import_archive_ocr(LIBRARY_ROOT,
                            payload["path"], CATALOGUE_ROOT, result,
                            resolved_state_path=HISTORICAL_RESOLVED)
                if result["automation"]["status"] == "imported":
                    result["read_only"] = False
                    result["manual_review_required"] = False
                else:
                    result["display_name"] = archive_auto_name(LIBRARY_ROOT, payload["path"],
                        self.db, aliases_path=OPS_ROOT / "reports" / "edition_aliases.json",
                        resolved_state_path=HISTORICAL_RESOLVED, ocr_result=result)
                self._json(result)
                return
            if route == "/api/v2/archive/auto-name":
                if set(payload) != {"path"} or not isinstance(payload["path"], str):
                    raise ValueError("Chemin d’archive attendu")
                with _MAINTENANCE_LOCK:
                    if _maintenance_is_active() or _batch_is_active():
                        self._json({"error": "maintenance_in_progress"}, 409)
                        return
                    result = archive_auto_name(LIBRARY_ROOT, payload["path"], self.db,
                        aliases_path=OPS_ROOT / "reports" / "edition_aliases.json",
                        resolved_state_path=HISTORICAL_RESOLVED)
                self._json(result)
                return
            if route == "/api/v2/archive/hash":
                if set(payload) != {"path"} or not isinstance(payload["path"], str):
                    raise ValueError("Chemin d’archive attendu")
                path = _safe_child(LIBRARY_ROOT, payload["path"])
                if (path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
                        or path.stat().st_size > 20 * 1024 * 1024):
                    raise ValueError("Image d’archive invalide")
                self._json({"sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                return
            if route == "/api/v2/archive/lens":
                if set(payload) != {"path"} or not isinstance(payload["path"], str):
                    raise ValueError("Chemin d’archive attendu")
                self._json({"ticket": _store_lens_ticket(payload["path"])})
                return
            if route == "/api/v2/archive/delete":
                if set(payload) != {"path"} or not isinstance(payload["path"], str):
                    raise ValueError("Chemin d’archive attendu")
                with _MAINTENANCE_LOCK:
                    if _maintenance_is_active() or _batch_is_active():
                        self._json({"error": "maintenance_in_progress"}, 409)
                        return
                    result = delete_archive_file(LIBRARY_ROOT, payload["path"],
                                                 resolved_state_path=HISTORICAL_RESOLVED)
                self._json(result)
                return
            if route == "/api/v2/archive/import":
                required = {"path", "sha256", "provider_card_id", "printing_id",
                            "face_index", "language", "action"}
                if set(payload) != required:
                    raise ValueError("Paramètres d’import d’archive invalides")
                with _MAINTENANCE_LOCK:
                    if _maintenance_is_active() or _batch_is_active():
                        self._json({"error": "maintenance_in_progress"}, 409)
                        return
                    result = import_manual_archive_choice(
                        LIBRARY_ROOT, payload["path"], CATALOGUE_ROOT,
                        sha256=payload["sha256"], provider_card_id=payload["provider_card_id"],
                        printing_id=payload["printing_id"], face_index=payload["face_index"],
                        language=payload["language"], action=payload["action"],
                        inventory_path=HISTORICAL_INDEX,
                        resolved_state_path=HISTORICAL_RESOLVED)
                self._json(result)
                return
            if route == "/api/v2/batch-download/cancel":
                with _BATCH_LOCK:
                    found = bool(_BATCH_TASK and payload.get("task_id") == _BATCH_TASK["task_id"]
                                 and _BATCH_TASK["status"] in {"queued", "running"})
                    if found: _BATCH_CANCEL.set()
                self._json({"cancel_requested": found})
                return
            if route == "/api/v2/batch-download":
                if set(payload) != {"mode", "filters"}:
                    raise ValueError("Mode et filtres attendus")
                mode = payload["mode"]
                filters = _batch_filter_payload(mode, payload["filters"])
                with _MAINTENANCE_LOCK:
                    if _maintenance_is_active() or _maintenance_busy_reason():
                        self._json({"error": "download_busy", "detail": "Une autre opération est en cours."}, 409)
                        return
                    with _BATCH_LOCK:
                        task_id = uuid.uuid4().hex
                        _BATCH_CANCEL.clear()
                        _BATCH_TASK = {"task_id": task_id, "mode": mode, "filters": filters,
                                       "status": "queued", "total": 0,
                                       "completed": 0, "ready": 0, "failed": 0, "unavailable": 0,
                                       "current": "", "errors": []}
                        catalog_db = resolve_current(CATALOGUE_ROOT)["database"]
                        threading.Thread(target=_run_batch_task,
                            args=(task_id, mode, filters, catalog_db), daemon=True).start()
                self._json({"task_id": task_id, "status": "queued"}, 202)
                return
            if route == "/api/v2/image/export":
                provider_id = payload.get("provider_card_id")
                face_index = payload.get("face_index")
                if (set(payload) != {"provider_card_id", "face_index"}
                        or not isinstance(provider_id, str) or not 0 < len(provider_id) <= 100
                        or type(face_index) is not int or not 0 <= face_index < 10):
                    raise ValueError("Carte et face attendues.")
                if provider_id.startswith("proxy-"):
                    linked = PROXIES.linked(self.db, provider_id)
                    if face_index != 0 or not linked:
                        raise ValueError("Image du proxy associé indisponible.")
                    card, path = linked
                    fr = next((record for record in card["translations"] if record["lang"] == "fr"), {})
                    self._json(export_local_card_image(
                        path, fr.get("printed_name") or card.get("printed_name") or card["name"],
                        card["mana_value"], normalize=True))
                else:
                    self._json(export_card_to_desktop(provider_id, face_index, self.db))
                return
            if route == "/api/v2/list-import/export":
                if payload:
                    raise ValueError("Aucun paramètre d'export attendu.")
                with _LIST_LOCK:
                    if not _LIST_TASK or _LIST_TASK["status"] != "complete" or not _LIST_TASK["items"]:
                        self._json({"error": "list_not_complete"}, 409)
                        return
                    items = [dict(item) for item in _LIST_TASK["items"]]
                self._json(export_list_to_desktop(items, self.db))
                return
            if route == "/api/v2/list-import":
                value = payload.get("cards")
                if set(payload) != {"cards"} or not isinstance(value, str):
                    raise ValueError("Liste de cartes attendue.")
                parse_list(value)
                if _maintenance_snapshot().get("status") in {"queued", "running", "cancelling"}:
                    self._json({"error": "maintenance_in_progress"}, 409)
                    return
                with _MAINTENANCE_LOCK:
                    if _maintenance_is_active() or _batch_is_active():
                        self._json({"error": "maintenance_in_progress"}, 409)
                        return
                    with _LIST_LOCK:
                        if _LIST_TASK and _LIST_TASK["status"] in {"queued", "running"}:
                            self._json({"error": "import_in_progress"}, 409)
                            return
                        _LIST_TASK = {"task_id": uuid.uuid4().hex, "status": "queued", "total": 0,
                                      "completed": 0, "items": []}
                        task = dict(_LIST_TASK)
                threading.Thread(target=_run_list_task, args=(task["task_id"], value), daemon=True).start()
                self._json(task, 202)
                return
            if route == "/api/v2/image/preview":
                provider_id = payload.get("provider_card_id")
                face_index = payload.get("face_index")
                if (set(payload) != {"provider_card_id", "face_index"}
                        or not isinstance(provider_id, str) or not 0 < len(provider_id) <= 100
                        or type(face_index) is not int or not 0 <= face_index < 10):
                    self._json({"error": "invalid_preview_request"}, 400)
                    return
                catalog_db = self.db
                with connect(catalog_db) as conn:
                    card_name = conn.execute(
                        "SELECT name FROM source_records WHERE provider_card_id=? "
                        "ORDER BY lang='fr' DESC LIMIT 1", (provider_id,)).fetchone()
                label = f"Recherche d’image · {card_name['name']}" if card_name else "Recherche d’image"
                task = _start_image_task("image_preview", label,
                    lambda cancel_event: _prepare_image_preview(provider_id, face_index, catalog_db, cancel_event),
                    metadata={"provider_card_id": provider_id, "face_index": face_index})
                self._json(task, 202)
                return
            if route == "/api/v2/image/candidate/choose":
                candidate_id = payload.get("candidate_id")
                choice = payload.get("choice")
                index = payload.get("index")
                if (set(payload) != {"candidate_id", "choice", "index"} or choice not in {"old", "new"}
                        or type(index) is not int):
                    self._json({"error": "invalid_candidate_choice"}, 400)
                    return
                candidates = _image_candidate(candidate_id)
                if candidates is None or not 0 <= index < len(candidates):
                    self._json({"error": "candidate_expired"}, 404)
                    return
                candidate = candidates[index]
                if candidate.get("visual_only") and choice == "new":
                    self._json({"error": "visual_reference_only"}, 409)
                    return
                if choice == "old":
                    _image_candidate(candidate_id, remove=True)
                    with _IMAGE_TASKS_LOCK:
                        for task in _IMAGE_TASKS.values():
                            if task["kind"] == "image_preview" and (task.get("result") or {}).get("candidate_id") == candidate_id:
                                task["status"] = "complete"
                    self._json({"status": "kept_existing"})
                    return
                try:
                    with _MAINTENANCE_LOCK:
                        if _maintenance_is_active():
                            self._json({"error": "maintenance_in_progress"}, 409)
                            return
                        result = publish_preview_candidate(
                            candidate, catalogue_root=CATALOGUE_ROOT,
                            provider_card_id=candidate["provider_card_id"],
                            face_index=candidate["face_index"])
                except ValueError as exc:
                    self._json({"error": "candidate_stale", "detail": str(exc)}, 409)
                    return
                _image_candidate(candidate_id, remove=True)
                with _IMAGE_TASKS_LOCK:
                    for task in _IMAGE_TASKS.values():
                        if task["kind"] == "image_preview" and (task.get("result") or {}).get("candidate_id") == candidate_id:
                            task["status"] = "complete"
                self._json({"status": "ready", "result": result})
                return
            if route == "/api/v2/decks/refresh":
                if payload:
                    self._json({"error": "unexpected_request_field"}, 400)
                    return
                ensure_deck_index(DECK_DB, refresh=True)
                self._json({"status": "refreshed"})
                return
            if route == "/api/v2/maintenance/update/cancel":
                if set(payload) != {"task_id"} or not isinstance(payload["task_id"], str):
                    self._json({"error": "invalid_cancel_request"}, 400)
                    return
                task = _cancel_maintenance_task(payload["task_id"])
                self._json(task if task else {"error": "task_not_found"}, 202 if task else 404)
                return
            if route == "/api/v2/maintenance/activate":
                if set(payload) != {"version_id"} or not isinstance(payload["version_id"], str):
                    self._json({"error": "invalid_activation_request"}, 400)
                    return
                with _MAINTENANCE_LOCK:
                    if _MAINTENANCE_TASK and _MAINTENANCE_TASK.get("status") in {"queued", "running", "cancelling"}:
                        self._json({"error": "maintenance_in_progress"}, 409)
                        return
                    reason = _maintenance_busy_reason()
                    if reason:
                        self._json({"error": "activation_blocked", "detail": reason}, 409)
                        return
                    try:
                        result = activate_version(CATALOGUE_ROOT, payload["version_id"])
                    except VersionHistoryError as exc:
                        self._json({"error": "invalid_version", "detail": str(exc)}, 400)
                        return
                self._json(result)
                return
            if route == "/api/v2/maintenance/update":
                if payload:
                    self._json({"error": "unexpected_request_field"}, 400)
                    return
                with _MAINTENANCE_LOCK:
                    existing = _maintenance_snapshot()
                    if existing.get("status") in {"queued", "running", "cancelling"}:
                        self._json(existing, 202)
                        return
                    reason = _maintenance_busy_reason()
                    if reason:
                        self._json({"error": "maintenance_blocked", "detail": reason}, 409)
                        return
                    task = _start_maintenance_task()
                self._json(task, 202)
                return
            if route == "/api/v2/decks/colors/index":
                if payload:
                    self._json({"error": "unexpected_request_field"}, 400)
                    return
                ensure_deck_index(DECK_DB)
                with _COLOR_INDEX_LOCK:
                    global _COLOR_INDEX_TASK
                    if _COLOR_INDEX_TASK and _COLOR_INDEX_TASK["status"] in {"queued", "running"}:
                        task = dict(_COLOR_INDEX_TASK)
                    else:
                        task_id = uuid.uuid4().hex
                        _COLOR_INDEX_TASK = {"task_id": task_id, "status": "queued", "phase": "preparation", "completed": 0, "total": 0, "indexed": 0, "unmatched": 0, "failed": 0, "bytes_received": 0}
                        task = dict(_COLOR_INDEX_TASK)
                        threading.Thread(target=_run_color_index_task, args=(task_id,), daemon=True).start()
                self._json(task, 202)
                return
            if route == "/api/v2/deck/cancel":
                task_id = payload.get("task_id")
                with _DECK_TASKS_LOCK:
                    event = _DECK_CANCELS.get(task_id) if isinstance(task_id, str) else None
                    if event: event.set()
                self._json({"task_id": task_id, "cancel_requested": event is not None})
                return
            if route == "/api/v2/deck/download":
                if set(payload) != {"deck_id"} or not isinstance(payload.get("deck_id"), str) or not payload["deck_id"] or len(payload["deck_id"]) > 200:
                    self._json({"error": "deck_id_required"}, 400)
                    return
                ensure_deck_index(DECK_DB)
                deck_id = payload["deck_id"]
                deck_result = _deck_with_media(get_deck(DECK_DB, deck_id, catalog_db=self.db, include_tokens=True), self.db)
                if not deck_result.get("found"):
                    self._json({"error": "deck_not_found", "detail": "Deck introuvable."}, 404)
                    return
                missing_cards = [card for card in _deck_entries(deck_result["deck"]) if not card.get("image_ready")]
                if not missing_cards:
                    self._json({"error": "deck_complete", "detail": "Toutes les images du deck sont déjà disponibles."}, 409)
                    return
                if not any(card.get("provider_card_id") for card in missing_cards):
                    self._json({"error": "catalogue_incomplete", "detail": "Aucune impression de ce deck n'est dans le catalogue actif. Actualisez le catalogue de la Bibliothèque Magic avant de télécharger les images."}, 409)
                    return
                task_id = uuid.uuid4().hex
                cancel = threading.Event()
                with _MAINTENANCE_LOCK:
                    if _maintenance_is_active() or _batch_is_active():
                        self._json({"error": "maintenance_in_progress"}, 409)
                        return
                    with _DECK_TASKS_LOCK:
                        if any(task["status"] in {"queued", "running"} for task in _DECK_TASKS.values()):
                            self._json({"error": "deck_download_in_progress"}, 409)
                            return
                        _DECK_TASKS[task_id] = {"task_id": task_id, "deck_id": deck_id, "status": "queued", "total": 0, "completed": 0, "ready": 0, "failed": 0, "unavailable": 0, "unavailable_cards": [], "current": "", "errors": []}
                        _DECK_CANCELS[task_id] = cancel
                threading.Thread(target=_run_deck_task, args=(task_id, deck_id, cancel), daemon=True).start()
                self._json({"task_id": task_id, "status": "queued"}, 202)
                return
            if route == "/api/v2/deck/export":
                if set(payload) != {"deck_id"} or not isinstance(payload.get("deck_id"), str) or not payload["deck_id"] or len(payload["deck_id"]) > 200:
                    self._json({"error": "deck_id_required"}, 400)
                    return
                ensure_deck_index(DECK_DB)
                deck_result = get_deck(DECK_DB, payload["deck_id"], catalog_db=self.db, include_tokens=True)
                if not deck_result.get("found"):
                    self._json({"error": "deck_not_found", "detail": "Deck introuvable."}, 404)
                    return
                deck = _deck_with_media(deck_result, self.db)["deck"]
                if deck["media_summary"]["missing"]:
                    self._json({"error": "deck_incomplete", "detail": "Téléchargez les images manquantes avant l'export."}, 409)
                    return
                self._json(export_deck_to_desktop(deck, self.db))
                return
            if route == "/api/v2/download/cancel":
                task_id = payload.get("task_id")
                with _ACTIVE_DOWNLOADS_LOCK:
                    event = _ACTIVE_DOWNLOADS.get(task_id) if isinstance(task_id, str) else None
                    if event: event.set()
                self._json({"task_id": task_id, "cancel_requested": event is not None})
                return
            if set(payload) - {"provider_card_id", "face_index", "task_id", "repair"}:
                self._json({"error": "unexpected_request_field"}, 400)
                return
            provider_id = payload.get("provider_card_id") if isinstance(payload, dict) else None
            if not isinstance(provider_id, str) or len(provider_id) > 100:
                self._json({"error": "provider_card_id_required"}, 400)
                return
            task_id = payload.get("task_id") or uuid.uuid4().hex
            if not isinstance(task_id, str) or not task_id or len(task_id) > 80:
                self._json({"error": "invalid_task_id"}, 400)
                return
            cancel = threading.Event()
            with _MAINTENANCE_LOCK:
                if _maintenance_is_active() or _batch_is_active():
                    self._json({"error": "maintenance_in_progress"}, 409)
                    return
                with _ACTIVE_DOWNLOADS_LOCK:
                    _ACTIVE_DOWNLOADS[task_id] = cancel
            try:
                kwargs = {
                    "catalogue_root": CATALOGUE_ROOT,
                    "ledger_path": OPS_ROOT / "state" / "v2-image-download-ledger.json",
                    "face_indexes": [payload["face_index"]] if "face_index" in payload else None,
                    "cancel_event": cancel,
                }
                if route == "/api/v2/image/refresh":
                    report = refresh_printing_deep(provider_id, **kwargs)
                else:
                    report = download_printing(provider_id, **kwargs, repair=payload.get("repair") is True)
            finally:
                with _ACTIVE_DOWNLOADS_LOCK: _ACTIVE_DOWNLOADS.pop(task_id, None)
            report["task_id"] = task_id
            self._json(report)
        except personal_decks.RevisionConflict as exc:
            self._json({"error": "revision_conflict", "detail": str(exc)}, 409)
        except KeyError as exc:
            self._json({"error": "deck_not_found", "detail": str(exc)}, 404)
        except ArchiveConflictError as exc:
            self._json({"error": "archive_conflict", "detail": str(exc)}, 409)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json({"error": "download_rejected", "detail": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": "download_failed", "detail": str(exc)}, 502)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def open_existing_or_start(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, open_browser: bool = True):
    url = f"http://{host}:{port}/"
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/v2/health", timeout=1) as response:
            data = json.loads(response.read().decode("utf-8"))
            if response.status == 200 and data.get("status") == "ok" and data.get("app_api_revision") == APP_API_REVISION:
                if open_browser:
                    webbrowser.open(url)
                return None
            if response.status == 200 and data.get("status") == "ok":
                raise RuntimeError("Une ancienne version du serveur Magic utilise ce port. Fermez-la avant de relancer la bibliothèque.")
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        pass
    ensure_catalogue_wal(resolve_current(CATALOGUE_ROOT)["database"])
    server = LocalServer((host, port), LibraryHandler)
    server.app_url = url
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    return server


def _deck_index_network_busy() -> bool:
    with _COLOR_INDEX_LOCK:
        colors_running = bool(_COLOR_INDEX_TASK and _COLOR_INDEX_TASK.get("status") in {"queued", "running"})
    return colors_running or _maintenance_is_active() or bool(_maintenance_busy_reason())


def _refresh_deck_box_previews():
    generator = OPS_ROOT / "tools" / "generate_deck_box_previews.py"
    try:
        subprocess.run([sys.executable, str(generator), "--refresh"], check=True)
    except (OSError, subprocess.CalledProcessError):
        logging.warning("La mise à jour des aperçus de boîtes de deck a échoué.", exc_info=True)


def main():
    host = DEFAULT_HOST
    port = int(os.environ.get("MAGIC_LIBRARY_PORT", DEFAULT_PORT))
    if not (CATALOGUE_ROOT / "current.json").is_file():
        raise SystemExit("Catalogue absent. Lancez Initialiser_Bibliotheque_Magic.bat "
                         "ou restaurez un catalogue avant de démarrer.")
    server = open_existing_or_start(host, port, open_browser=False)
    if server is None:
        print(f"Bibliothèque Magic déjà active : http://{host}:{port}/")
        webbrowser.open(f"http://{host}:{port}/")
        return
    print(f"Bibliothèque Magic : http://{host}:{port}/")
    print("Serveur local lié uniquement à 127.0.0.1. Ctrl+C pour arrêter.")
    stop_deck_index = threading.Event()
    try:
        _refresh_deck_box_previews()
        threading.Timer(0.5, webbrowser.open, args=(server.app_url,)).start()
        threading.Thread(
            name="deck-composition-index", target=run_deck_composition_index, daemon=True,
            args=(stop_deck_index, DECK_DB), kwargs={
                "catalog_db_provider": lambda: resolve_current(CATALOGUE_ROOT)["database"],
                "network_busy": _deck_index_network_busy,
            },
        ).start()
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_deck_index.set()
        server.server_close()


if __name__ == "__main__":
    main()
