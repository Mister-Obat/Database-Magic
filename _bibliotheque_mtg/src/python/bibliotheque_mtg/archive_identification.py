"""Read-only helpers for searching and manually identifying archive images.

Search results are suggestions only. A human must inspect the source image and
choose the exact printing and face before an import can be prepared.
"""
from __future__ import annotations

import copy
import json
import hashlib
import os
import re
import sqlite3
import threading
import uuid
import urllib.parse
from collections import OrderedDict
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .v2 import api
from .v2.query import connect
from . import ocr_candidates
from .image_download import import_local_image
from .organized_images import mirror_printing_face
from .v2.publish import resolve_current

MAX_QUERY_LENGTH = 200
MAX_CANDIDATES = 100
SUPPORTED_LANGUAGES = {"fr", "en"}
_OCR_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_OCR_CACHE_LOCK = threading.Lock()
_OCR_CACHE_LIMIT = 256
_RESOLVED_STATE_LOCK = threading.RLock()
_OCR_STATE_ROOT = Path(__file__).resolve().parents[3] / "state" / "archive-ocr"
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


class ArchiveConflictError(ValueError):
    """The selected archive item conflicts with an existing image or duplicate."""


def _oracle_language_expansion(database: Path, items: list[dict]) -> list[dict]:
    """Suggest FR/EN printings of the same oracle that a French name could not match.

    A French card name matches FR source rows only. Its English editions are
    reached through the shared oracle identity; each edition not already in the
    results keeps one row, FR first, then EN. Suggestions stay advisory.
    Each lookup stays a simple indexed one-table query: the catalogue has no
    ANALYZE stats and SQLite flips multi-table joins onto a full
    source_records scan depending on the IN-list shape.
    """
    printing_ids = [str(item["printing_id"]) for item in items if item.get("printing_id")]
    if not printing_ids:
        return []
    conn = connect(database)
    try:
        marks = ",".join("?" for _ in printing_ids)
        oracle_ids = [row[0] for row in conn.execute(
            f"SELECT DISTINCT oracle_id FROM printings "
            f"WHERE printing_id IN ({marks}) AND oracle_id IS NOT NULL", printing_ids)]
        rows = []
        for oracle_id in oracle_ids:
            # Step 1: the printings of this oracle (indexed, one-table).
            printing_rows = conn.execute("""SELECT printing_id,oracle_id,collector_number,set_id
                FROM printings WHERE oracle_id=?""", (oracle_id,)).fetchall()
            step_ids = [row["printing_id"] for row in printing_rows]
            if not step_ids:
                continue
            # Step 2: their FR/EN source rows (printing_id index, one-table).
            step_marks = ",".join("?" for _ in step_ids)
            source_rows = conn.execute(f"""SELECT printing_id,provider_card_id,lang,name,printed_name
                FROM source_records WHERE printing_id IN ({step_marks}) AND lang IN ('fr','en')""",
                step_ids).fetchall()
            sources: dict[str, list] = {}
            for source_row in source_rows:
                sources.setdefault(source_row["printing_id"], []).append(source_row)
            # Step 3: the editions of the touched sets (small table).
            set_ids = {row["set_id"] for row in printing_rows}
            set_marks = ",".join("?" for _ in set_ids)
            edition_rows = conn.execute(f"""SELECT set_id,code,name AS edition,released_at
                FROM editions WHERE eligibility='eligible' AND set_id IN ({set_marks})""",
                tuple(set_ids)).fetchall()
            editions = {row["set_id"]: row for row in edition_rows}
            for printing_row in printing_rows:
                edition = editions.get(printing_row["set_id"])
                if edition is None:
                    continue
                for source_row in sources.get(printing_row["printing_id"], []):
                    rows.append({"printing_id": printing_row["printing_id"],
                                 "oracle_id": oracle_id,
                                 "collector_number": printing_row["collector_number"],
                                 "code": edition["code"], "edition": edition["edition"],
                                 "released_at": edition["released_at"],
                                 "provider_card_id": source_row["provider_card_id"],
                                 "lang": source_row["lang"], "name": source_row["name"],
                                 "printed_name": source_row["printed_name"]})
    finally:
        conn.close()
    def _number_value(row):
        number = str(row["collector_number"] or "")
        match = re.match(r"\d*", number)
        return int(match.group() or 0), number
    # Mirror the intended order: released_at desc, code asc, collector asc.
    # Stable sorts keep each pass independent.
    rows.sort(key=lambda row: _number_value(row))
    rows.sort(key=lambda row: str(row["code"] or ""))
    rows.sort(key=lambda row: str(row["released_at"] or ""), reverse=True)
    base_sets = {str(item.get("set_code") or "").casefold() for item in items}
    base_printings = set(printing_ids)
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        code = str(row["code"]).casefold()
        if code in base_sets:
            continue
        key = (str(row["oracle_id"]), code)
        current = grouped.get(key)
        if current is None:
            grouped[key] = {"row": row, "lang": row["lang"]}
        elif current["lang"] != "fr" and row["lang"] == "fr":
            grouped[key] = {"row": row, "lang": "fr"}
    expanded = []
    for pick in grouped.values():
        row = pick["row"]
        if str(row["printing_id"]) in base_printings:
            continue
        expanded.append({"printing_id": row["printing_id"],
            "provider_card_id": row["provider_card_id"], "set_code": row["code"],
            "edition": row["edition"], "lang": row["lang"],
            "name": row["name"], "printed_name": row["printed_name"],
            "collector_number": row["collector_number"],
            "match_source": "oracle_language_expansion"})
    return expanded


MAX_EXPANSION_ITEMS = 30


def _archive_display_name(file_name: str) -> str:
    """Mirror the UI tile name derived from the archive file name."""
    stem = re.sub(r"\.[^.]+$", "", str(file_name))
    stripped = re.sub(r"^\d+\s*-\s*", "", stem).strip()
    return stripped or str(file_name)


def _name_search_terms(text: str) -> str:
    """Build a text-search key from a card name: alphanumerics only.

    Punctuation and isolated letters break the FTS prefix query (each word
    is an AND), so tokens of a single character are dropped.
    """
    tokens = re.findall(r"[^\W_]+", str(text), flags=re.UNICODE)
    return " ".join(token for token in tokens if len(token) >= 2)


def _name_findable_in_catalogue(database: Path, name: str) -> bool:
    """A coherent name is one the active catalogue can find by text search."""
    terms = _name_search_terms(name)
    if not terms:
        return False
    try:
        result = api.dispatch(database, "/api/v2/search", {"q": terms, "limit": 1})
    except (ValueError, sqlite3.Error):
        return False
    return int(result.get("total") or 0) > 0


def _write_resolved_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(state_path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, state_path)


def _read_display_name(resolved_state_path: Path, path_posix: str) -> str:
    """Return the previously recorded archive display name, if any."""
    state_path = Path(resolved_state_path)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return ""
    display_names = state.get("display_names") if isinstance(state.get("display_names"), dict) else {}
    value = display_names.get(path_posix)
    return value.strip() if isinstance(value, str) else ""


def _record_display_name(resolved_state_path: Path, path_posix: str, name: str) -> None:
    """Persist the manually or automatically derived archive display name."""
    state_path = Path(resolved_state_path)
    with _RESOLVED_STATE_LOCK:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        display_names = state.get("display_names") if isinstance(state.get("display_names"), dict) else {}
        if display_names.get(path_posix) == name:
            return
        display_names[path_posix] = name
        state["display_names"] = display_names
        _write_resolved_state(state_path, state)


def _canonical_catalogue_name(database: Path, candidate: str) -> str:
    """Match the OCR title against the catalogue to borrow its clean name.

    The catalogue name is adopted only for a strong similarity: the OCR text
    stays advisory and a wrong match must remain easy to spot.
    """
    try:
        terms = _name_search_terms(candidate)
        if not terms:
            return ""
        result = api.dispatch(database, "/api/v2/search", {"q": terms, "limit": 3})
    except (ValueError, sqlite3.Error):
        return ""
    items = result.get("items") or []
    if not items:
        return ""
    canonical = str(items[0].get("printed_name") or items[0].get("name") or "").strip()
    if len(canonical) < 2 or not any(char.isalpha() for char in canonical):
        return ""
    ratio = SequenceMatcher(None, ocr_candidates._normal(candidate),
                            ocr_candidates._normal(canonical)).ratio()
    return canonical if ratio >= 0.6 else ""


def _decide_display_name(database: Path, file_name: str, proposal: dict,
                         resolved_state_path: Path, path_posix: str) -> dict:
    """Choose the archive tile name: keep it when the catalogue knows it.

    Priority for the replacement: the catalogue title of a unique OCR
    proposal, then the catalogue name closest to the OCR title, then the
    first readable line of the OCR title region.
    """
    evidence = proposal.get("evidence") or {}
    target = proposal.get("target") or {}
    existing = _read_display_name(resolved_state_path, path_posix)
    current = existing or _archive_display_name(file_name)
    if _name_findable_in_catalogue(database, current):
        return {"status": "kept", "name": current}
    candidate = str(target.get("title") or "").strip()
    if not candidate:
        ocr_title = str(evidence.get("ocr_title") or "")
        candidate = next((line.strip() for line in ocr_title.splitlines() if line.strip()), "")
    candidate = candidate.strip(" \t|()[]{}«»\"'“”‘’:;,.-–—!?*_/<>=+~\\")
    sanitized = _name_search_terms(candidate)
    if len(sanitized) >= 2 and any(char.isalpha() for char in sanitized):
        candidate = _canonical_catalogue_name(database, sanitized) or sanitized
    if (len(candidate) < 2 or not any(char.isalpha() for char in candidate)
            or ocr_candidates._normal(candidate) == ocr_candidates._normal(current)):
        return {"status": "unnamed", "name": current,
                "reason": "Aucun titre lisible n’a été trouvé par l’OCR"}
    _record_display_name(resolved_state_path, path_posix, candidate)
    return {"status": "renamed", "name": candidate, "previous": current}


def archive_auto_name(library_root: Path, relative_path: str, database: Path, *,
                      aliases_path: Path | None = None,
                      resolved_state_path: Path,
                      ocr_result: dict | None = None) -> dict:
    """Derive a real card name for one archive file from local OCR.

    Never imports or removes the file: only the display name can change,
    and only when the current name is not findable in the catalogue.
    """
    if ocr_result is None:
        ocr_result = propose_archive_ocr(library_root, relative_path, database,
                                         aliases_path=aliases_path)
    relative = Path(str(relative_path).replace("\\", "/"))
    decision = _decide_display_name(database, relative.name,
                                    ocr_result.get("identity_proposal") or {},
                                    resolved_state_path, relative.as_posix())
    decision["ocr_cached"] = bool(ocr_result.get("cached"))
    return decision


def search_archive_printings(database: Path, query: str, *, set_code: str | None = None,
                             language: str | None = None, limit: int = 30,
                             offset: int = 0,
                             source_database: Path | None = None) -> dict:
    """Search active printings for a manual archive match; never asserts identity."""
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= MAX_QUERY_LENGTH:
        raise ValueError("Saisissez entre 2 et 200 caractères pour rechercher")
    if language is not None and language not in SUPPORTED_LANGUAGES:
        raise ValueError("Langue d'archive non prise en charge")
    try:
        limit = max(1, min(MAX_CANDIDATES, int(limit)))
        offset = max(0, int(offset))
    except (TypeError, ValueError) as exc:
        raise ValueError("Pagination invalide") from exc
    params = {"q": query.strip(), "limit": limit, "offset": offset}
    if set_code:
        params["set"] = set_code
    if language:
        params["lang"] = language
    result = api.dispatch(database, "/api/v2/search", params)
    # A French name cannot text-match English-only printings; reach them through
    # the oracle identity shared with the matched French rows.
    if language != "en" and not set_code:
        expanded = _oracle_language_expansion(database, result.get("items", []))
        if expanded:
            result["items"] = list(result.get("items", [])) + expanded[:MAX_EXPANSION_ITEMS]
            result["language_expanded"] = True
    if source_database is None:
        source_database = Path(__file__).resolve().parents[3] / "catalogue.sqlite"
    icons = _edition_icon_urls({str(item.get("set_code") or "").lower()
                               for item in result.get("items", [])}, source_database)
    for item in result.get("items", []):
        item["edition_icon_url"] = icons.get(str(item.get("set_code") or "").lower())
    return {**result, "identity_proven": False,
            "warning": "Résultats de recherche : vérifier visuellement l’impression et la face."}


def _edition_icon_urls(codes: set[str], source_database: Path) -> dict[str, str]:
    """Read Scryfall set symbols already archived in the local source DB."""
    codes = {code for code in codes if code and len(code) <= 12 and code.isalnum()}
    if not codes or not Path(source_database).is_file():
        return {}
    try:
        connection = sqlite3.connect(Path(source_database).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        placeholders = ",".join("?" for _ in codes)
        rows = connection.execute(
            f"SELECT code,raw_json FROM sets WHERE lower(code) IN ({placeholders})", tuple(codes))
        icons = {}
        for code, raw_json in rows:
            try:
                metadata = json.loads(raw_json)
                url = metadata.get("icon_svg_uri") if isinstance(metadata, dict) else None
                if not isinstance(url, str):
                    continue
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme == "https" and parsed.hostname == "svgs.scryfall.io" and parsed.path.endswith(".svg"):
                    icons[str(code).lower()] = url
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return icons
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


def describe_archive_target(database: Path, *, provider_card_id: str,
                            printing_id: str, face_index: int,
                            language: str) -> dict:
    """Validate a manually selected target against the active database."""
    if not isinstance(provider_card_id, str) or not provider_card_id or len(provider_card_id) > 200:
        raise ValueError("Identifiant de carte invalide")
    if not isinstance(printing_id, str) or not printing_id or len(printing_id) > 200:
        raise ValueError("Identifiant d’impression invalide")
    if type(face_index) is not int or not 0 <= face_index <= 5:
        raise ValueError("Face invalide")
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError("Langue d’archive non prise en charge")

    conn = connect(database)
    try:
        row = conn.execute("""SELECT sr.provider_card_id,sr.printing_id,sr.lang,sr.name,
                sr.printed_name,sr.raw_json,p.collector_number,e.code AS set_code,
                e.name AS edition,s.status
            FROM source_records sr JOIN printings p USING(printing_id)
            JOIN editions e USING(set_id)
            JOIN image_slots s ON s.printing_id=p.printing_id AND s.face_index=?
            WHERE sr.provider_card_id=? AND sr.printing_id=? AND e.eligibility='eligible'""",
            (face_index, provider_card_id, printing_id)).fetchone()
        if row is None or row["lang"] != language:
            raise ValueError("La carte, l’impression, la face ou la langue n’existe pas dans le catalogue actif")
        raw = json.loads(row["raw_json"] or "{}")
        faces = raw.get("card_faces") or []
        if faces:
            if face_index >= len(faces):
                raise ValueError("Face absente des données de la carte")
            face = faces[face_index]
            title = face.get("printed_name") or face.get("name") or row["printed_name"] or row["name"]
        else:
            if face_index != 0:
                raise ValueError("Cette carte ne possède pas cette face")
            title = row["printed_name"] or row["name"]
        return {"provider_card_id": row["provider_card_id"],
                "printing_id": row["printing_id"], "set_code": row["set_code"],
                "edition": row["edition"], "collector_number": row["collector_number"],
                "face_index": face_index, "language": row["lang"], "title": title,
                "image_status": row["status"], "manual_review_required": True}
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise ValueError("Impossible de valider la cible dans le catalogue actif") from exc
    finally:
        conn.close()


def propose_archive_ocr(library_root: Path, relative_path: str,
                        database: Path, *, aliases_path: Path | None = None,
                        cache_root: Path | None = None) -> dict:
    """Run local OCR for one safe archive file and return a review proposal.

    The result is advisory and does not import or modify the archive image.
    """
    root = Path(library_root).resolve(strict=True)
    relative = Path(str(relative_path).replace("\\", "/"))
    if (relative.is_absolute() or ".." in relative.parts or not relative.parts
            or relative.parts[0].casefold() == "impossible"):
        raise ValueError("Chemin d’archive invalide")
    if relative.suffix.casefold() not in _IMAGE_EXTENSIONS:
        raise ValueError("Format d’image d’archive non pris en charge")
    source = root.joinpath(*relative.parts)
    if source.is_symlink():
        raise ValueError("Les liens symboliques ne sont pas acceptés")
    path = source.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Fichier d’archive introuvable")
    source_stat = path.stat()
    size = source_stat.st_size
    if size <= 0 or size > 20 * 1024 * 1024:
        raise ValueError("Taille d’image d’archive invalide")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    database = Path(database).resolve(strict=True)
    folder_alias = {}
    if aliases_path is not None and Path(aliases_path).is_file():
        aliases = json.loads(Path(aliases_path).read_text(encoding="utf-8")).get("aliases", [])
        folder_alias = next((item for item in aliases if item.get("folder") == path.parent.name), {})
    alias_key = json.dumps(folder_alias, ensure_ascii=False, sort_keys=True)
    cache_key = (str(path), digest, str(database),
                 ocr_candidates.OCR_ALGORITHM_VERSION, alias_key)
    state_root = Path(cache_root) if cache_root is not None else _OCR_STATE_ROOT
    state_path = state_root / (hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest() + ".json")
    with _OCR_CACHE_LOCK:
        cached = _OCR_CACHE.get(cache_key)
        if cached is not None:
            _OCR_CACHE.move_to_end(cache_key)
            result = copy.deepcopy(cached)
            result["cached"] = True
            return result
    try:
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        if saved.get("cache_key") == list(cache_key) and isinstance(saved.get("result"), dict):
            result = saved["result"]
            result["cached"] = True
            return result
    except (OSError, ValueError, TypeError):
        pass
    conn = connect(database)
    try:
        proposal = ocr_candidates.propose_ocr(path, conn, folder_alias)
    finally:
        conn.close()
    result = {"path": relative.as_posix(), "sha256": digest, "database": str(database),
              "identity_proposal": proposal, "read_only": True,
              "manual_review_required": True, "cached": False}
    state_root.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(state_path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps({"cache_key": cache_key, "source_size": size,
            "source_mtime_ns": source_stat.st_mtime_ns, "result": result},
            ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, state_path)
    finally:
        temporary.unlink(missing_ok=True)
    with _OCR_CACHE_LOCK:
        _OCR_CACHE[cache_key] = copy.deepcopy(result)
        _OCR_CACHE.move_to_end(cache_key)
        while len(_OCR_CACHE) > _OCR_CACHE_LIMIT:
            _OCR_CACHE.popitem(last=False)
    return result


def cached_archive_ocr(library_root: Path, relative_path: str, database: Path,
                       *, aliases_path: Path | None = None,
                       cache_root: Path | None = None) -> dict | None:
    """Return a saved result for listing without rereading or hashing the image."""
    relative = Path(relative_path)
    path = Path(library_root) / relative
    state_root = Path(cache_root) if cache_root is not None else _OCR_STATE_ROOT
    state_path = state_root / (hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest() + ".json")
    try:
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        source_stat = path.stat()
        if not Path(database).is_file():
            return None
        folder_alias = {}
        if aliases_path is not None and Path(aliases_path).is_file():
            aliases = json.loads(Path(aliases_path).read_text(encoding="utf-8")).get("aliases", [])
            folder_alias = next((item for item in aliases if item.get("folder") == path.parent.name), {})
        key = (str(path.resolve()), saved.get("result", {}).get("sha256"),
               str(Path(database).resolve()), ocr_candidates.OCR_ALGORITHM_VERSION,
               json.dumps(folder_alias, ensure_ascii=False, sort_keys=True))
        if (saved.get("source_size") == source_stat.st_size
                and saved.get("source_mtime_ns") == source_stat.st_mtime_ns
                and saved.get("cache_key") == list(key)):
            return {**saved["result"], "cached": True}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def auto_import_archive_ocr(library_root: Path, relative_path: str,
                            catalogue_root: Path, ocr_result: dict,
                            *, resolved_state_path: Path) -> dict:
    """Publish only an unambiguous printed code, number, title and language."""
    proposal = ocr_result.get("identity_proposal") or {}
    evidence = proposal.get("evidence") or {}
    target = proposal.get("target") or {}
    if (proposal.get("status") != "unique"
            or evidence.get("identity_method") != "printed_code_number_title"
            or evidence.get("title_crop_score", 0) < .9
            or evidence.get("language_proven") is not True):
        return {"status": "review_required", "reason": "Preuve OCR insuffisante ou ambiguë"}
    if (target.get("set_code") not in evidence.get("set_codes", [])
            or ocr_candidates._collector_key(str(target.get("collector_number", ""))) not in
            {ocr_candidates._collector_key(str(number)) for number in evidence.get("collector_numbers", [])}):
        return {"status": "review_required", "reason": "Code ou numéro non confirmé par l’OCR"}
    current = resolve_current(catalogue_root)
    if Path(current["database"]).resolve() != Path(ocr_result.get("database", current["database"])).resolve():
        return {"status": "review_required", "reason": "La version du catalogue a changé"}
    try:
        described = describe_archive_target(current["database"],
            provider_card_id=target["provider_card_id"], printing_id=target["printing_id"],
            face_index=target["face_index"], language=target["language"])
        if (described["set_code"] != target["set_code"]
                or ocr_candidates._collector_key(described["collector_number"]) !=
                ocr_candidates._collector_key(target["collector_number"])):
            return {"status": "review_required", "reason": "Impression différente du catalogue actif"}
        ocr_title = (evidence.get("ocr_title") or "").strip()
        if (not ocr_title or not described.get("title")
                or ocr_candidates._normal(ocr_title) != ocr_candidates._normal(described["title"])):
            return {"status": "review_required", "reason": "Titre de face à vérifier manuellement"}
        if described["image_status"] != "missing":
            return {"status": "review_required", "reason": "Image déjà présente : comparaison manuelle nécessaire"}
        imported = import_manual_archive_choice(library_root, relative_path, catalogue_root,
            sha256=ocr_result["sha256"], provider_card_id=target["provider_card_id"],
            printing_id=target["printing_id"], face_index=target["face_index"],
            language=target["language"], resolved_state_path=resolved_state_path,
            verification_method="ocr_printed_code_number_title_language")
        return {"status": "imported", "result": imported}
    except (ArchiveConflictError, ValueError, OSError, KeyError) as exc:
        return {"status": "review_required", "reason": str(exc)}


def import_manual_archive_choice(library_root: Path, relative_path: str,
                                 catalogue_root: Path, *, sha256: str,
                                 provider_card_id: str, printing_id: str,
                                 face_index: int, language: str,
                                 inventory_path: Path | None = None,
                                 resolved_state_path: Path,
                                 action: str = "archive",
                                 verification_method: str = "manual_visual_confirmation") -> dict:
    """Import an image after manual confirmation or strict OCR proof.

    The live archive file is rehashed at import time. Its original is removed
    only after the organized WebP has been verified successfully.
    """
    root = Path(library_root).resolve(strict=True)
    relative = Path(str(relative_path).replace("\\", "/"))
    if (relative.is_absolute() or ".." in relative.parts or not relative.parts
            or relative.parts[0].casefold() == "impossible"):
        raise ValueError("Chemin d’archive invalide")
    path = root.joinpath(*relative.parts)
    if path.is_symlink():
        raise ValueError("Les liens symboliques ne sont pas acceptés")
    path = path.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Fichier d’archive introuvable")
    data = path.read_bytes()
    if len(data) <= 0 or len(data) > 20 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError("Image d’archive modifiée ou trop volumineuse")
    if _live_duplicate_paths(root, relative.as_posix(), sha256, len(data)):
        raise ArchiveConflictError("Hash présent sur plusieurs fichiers d’archive; originaux conservés")
    target = describe_archive_target(resolve_current(catalogue_root)["database"],
        provider_card_id=provider_card_id, printing_id=printing_id,
        face_index=face_index, language=language)
    if action not in {"archive", "already_indexed", "replace_indexed"}:
        raise ValueError("Action d’import d’archive invalide")
    if action == "archive":
        if target["image_status"] != "missing":
            raise ArchiveConflictError("Le slot est déjà prêt; choisissez ‘already_indexed’ après contrôle visuel")
        imported = import_local_image(provider_card_id, face_index, data,
            catalogue_root=catalogue_root, language=language,
            source_provider="historical_import",
            source_evidence=f"{verification_method}; path={relative.as_posix()}; sha256={sha256}",
            only_missing=True)
        if imported.get("status") != "ready":
            raise ValueError("L’image choisie n’a pas été publiée dans le catalogue")
    elif action == "replace_indexed":
        if target["image_status"] != "ready":
            raise ArchiveConflictError("Le slot n’est pas prêt et ne peut pas être remplacé")
        imported = import_local_image(provider_card_id, face_index, data,
            catalogue_root=catalogue_root, language=language,
            source_provider="historical_import",
            source_evidence=f"manual archive replacement; path={relative.as_posix()}; sha256={sha256}",
            only_missing=False)
        if imported.get("status") != "ready":
            raise ValueError("L’image d’archive n’a pas remplacé le slot existant")
    else:
        if target["image_status"] != "ready":
            raise ArchiveConflictError("Aucune image prête n’existe pour ce slot")
        imported = {"status": "already_ready"}

    current = resolve_current(catalogue_root)
    verified_asset = _verify_organized_image(current["database"], current["database"].parent,
                                            printing_id, face_index)
    target["image_status"] = "ready"
    # Deleting a source is allowed only after a ready 531×738 WebP has been
    # decoded and its organized copy has been verified against the asset hash.
    path.unlink()
    state_path = Path(resolved_state_path)
    with _RESOLVED_STATE_LOCK:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        entries = state.get("entries") if isinstance(state.get("entries"), dict) else {}
        entries[relative.as_posix()] = {"sha256": sha256, "printing_id": printing_id,
            "face_index": face_index, "status": "verified_removed",
            "method": verification_method}
        previous = list(state.get("previous_version_ids") or [])
        old_version = state.get("version_id")
        if old_version and old_version != current["version_id"] and old_version not in previous:
            previous.append(old_version)
        state["version_id"] = current["version_id"]
        state["entries"] = entries
        if previous:
            state["previous_version_ids"] = previous
        display_names = state.get("display_names")
        if isinstance(display_names, dict):
            display_names.pop(relative.as_posix(), None)
        _write_resolved_state(state_path, state)
    result_status = {"archive": "imported", "already_indexed": "already_indexed",
                     "replace_indexed": "replaced_indexed"}[action]
    return {"status": result_status,
            "target": target, "verified_asset": verified_asset, "result": imported,
            "archive_path": relative.as_posix(), "sha256": sha256}


def delete_archive_file(library_root: Path, relative_path: str, *,
                        resolved_state_path: Path) -> dict:
    """Remove one archive image on explicit user request, after validation.

    The removal is recorded in the resolved state with the status
    ``discarded``; no import, slot or catalogue data is touched.
    """
    root = Path(library_root).resolve(strict=True)
    relative = Path(str(relative_path).replace("\\", "/"))
    if (relative.is_absolute() or ".." in relative.parts or not relative.parts
            or relative.parts[0].casefold() == "impossible"):
        raise ValueError("Chemin d’archive invalide")
    path = root.joinpath(*relative.parts)
    if path.is_symlink():
        raise ValueError("Les liens symboliques ne sont pas acceptés")
    try:
        path = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("Fichier d’archive introuvable") from exc
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Fichier d’archive introuvable")
    if path.suffix.casefold() not in _IMAGE_EXTENSIONS:
        raise ValueError("Format d’image d’archive non pris en charge")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Image d’archive trop volumineuse")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.unlink()
    state_path = Path(resolved_state_path)
    with _RESOLVED_STATE_LOCK:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        entries = state.get("entries") if isinstance(state.get("entries"), dict) else {}
        entries[relative.as_posix()] = {"sha256": digest, "status": "discarded",
                                        "method": "manual_archive_delete"}
        state["entries"] = entries
        display_names = state.get("display_names")
        if isinstance(display_names, dict):
            display_names.pop(relative.as_posix(), None)
        _write_resolved_state(state_path, state)
    return {"status": "deleted", "archive_path": relative.as_posix(), "sha256": digest}


def _live_duplicate_paths(root: Path, current_relative: str, digest: str, size: int) -> list[str]:
    duplicates = []
    for candidate in root.rglob("*"):
        try:
            relative = candidate.relative_to(root)
            if ("impossible" in {part.casefold() for part in relative.parts}
                    or candidate.is_symlink() or candidate.suffix.casefold() not in _IMAGE_EXTENSIONS
                    or not candidate.is_file()):
                continue
            if relative.as_posix() == current_relative or candidate.stat().st_size != size:
                continue
            if hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
                duplicates.append(relative.as_posix())
        except OSError as exc:
            raise ArchiveConflictError("Impossible de vérifier les doublons dans l’archive") from exc
    return duplicates


def _verify_organized_image(database: Path, version: Path,
                            printing_id: str, face_index: int) -> dict:
    """Require a decoded active and organized WebP before archive cleanup."""
    conn = connect(database)
    try:
        row = conn.execute("""SELECT s.status,a.asset_id,a.width,a.height,a.mime_type
            FROM image_slots s JOIN image_assets a USING(asset_id)
            WHERE s.printing_id=? AND s.face_index=?""", (printing_id, face_index)).fetchone()
    finally:
        conn.close()
    if (row is None or row["status"] != "ready" or row["width"] != 531
            or row["height"] != 738 or row["mime_type"] != "image/webp"):
        raise ValueError("Aucune image WebP prête et normalisée pour cette face")
    organized = mirror_printing_face(database, version, printing_id, face_index)
    expected = row["asset_id"].rsplit("_", 1)[-1]
    data = organized.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError("Hash du WebP organisé différent du catalogue")
    try:
        with Image.open(organized) as image:
            image.load()
            if image.format != "WEBP" or image.size != (531, 738):
                raise ValueError("Le fichier organisé n’est pas un WebP 531×738")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Le WebP organisé est illisible") from exc
    return {"printing_id": printing_id, "face_index": face_index,
            "asset_id": row["asset_id"], "organized_path": str(organized),
            "status": "ready", "width": 531, "height": 738}
