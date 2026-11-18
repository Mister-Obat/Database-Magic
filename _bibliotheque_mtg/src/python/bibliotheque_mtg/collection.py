from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image, UnidentifiedImageError

from .faces import PhysicalFace, physical_faces, printing_groups_from_catalogue
from .images import (ImageCandidate, ImageStateStore, ImageTransport, candidates_for_face,
                     collect_face, verified_gallery_candidates, verified_local_candidates)
from .indexing import safe_edition_folders
from .pipeline import PipelineStorageError, load_valid_config
from .publication import FileLock, LockedPublication, Publisher
from .sources import SourceFormatError


class CollectionDataError(SourceFormatError):
    pass


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionDataError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CollectionDataError(f"Expected a JSON object in {path}")
    return value


def _load_selection(path: Path, schema_path: Path) -> dict:
    value = _json(path)
    try:
        schema = _json(schema_path)
        Draft202012Validator.check_schema(schema)
        errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise CollectionDataError(f"Pilot selection schema cannot be loaded: {exc}") from exc
    if errors:
        detail = "; ".join(f"/{'/'.join(map(str, error.absolute_path))}: {error.message}"
                           for error in errors[:12])
        raise CollectionDataError(f"Invalid pilot selection: {detail}")
    return value


def _source_sha256(catalogue_path: Path) -> str:
    try:
        with closing(sqlite3.connect(catalogue_path)) as conn:
            row = conn.execute("SELECT value FROM source_meta WHERE key='bulk_sha256'").fetchone()
    except sqlite3.Error as exc:
        raise PipelineStorageError(f"Cannot read catalogue source metadata: {exc}") from exc
    if not row or not row[0]:
        raise CollectionDataError("Catalogue does not record its source archive SHA-256")
    return str(row[0])


def _set_presence_map(aliases_path: Path) -> dict[str, str]:
    aliases = _json(aliases_path).get("aliases", [])
    result = {}
    for row in aliases:
        folder = row.get("folder")
        candidates = row.get("candidates") or []
        for candidate in candidates:
            set_id = candidate.get("set_id")
            if set_id and folder:
                # Ambiguous aliases still prove that a matching historical folder exists;
                # reconciliation quality is handled independently.
                result.setdefault(str(set_id), str(folder))
    return result


def _identity_for_face(face: PhysicalFace, group, catalogue_path: Path) -> dict:
    raw_by_language = {lang: json.loads(record.get("raw_json") or "{}")
                       for lang, record in group.members.items()}
    illustrations = [raw.get("illustration_id") for raw in raw_by_language.values()
                     if raw.get("illustration_id")]
    artists = sorted({str(raw["artist"]) for raw in raw_by_language.values()
                      if isinstance(raw.get("artist"), str) and raw["artist"].strip()})
    return {
        "set_code": face.set_code,
        "collector_number": str(face.collector_number or ""),
        "layout": face.layout,
        "provider_ids": face.provider_ids,
        "language_codes": sorted(face.provider_ids),
        "oracle_id": face.oracle_id,
        "face_names": {"fr": face.names.get("fr"), "en": face.names.get("en")},
        "illustration_id": illustrations[0] if illustrations else None,
        "artist": artists,
    }


def _verify_local_asset(item: dict, library_root: Path) -> dict:
    local = item.get("expected", {}).get("local_asset")
    if local is None:
        return {"matched": False, "reason": "no local asset declared"}
    relative = Path(local["path"])
    try:
        path = (library_root / relative).resolve(strict=True)
        root = library_root.resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise OSError("local asset escapes the library root or is not a file")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        with Image.open(path) as image:
            image.load()
            dimensions = image.size
    except (OSError, UnidentifiedImageError) as exc:
        raise CollectionDataError(f"Cannot verify frozen local asset {relative}: {exc}") from exc
    if digest != local["sha256"]:
        raise CollectionDataError(f"Frozen local asset SHA-256 changed: {relative}")
    if dimensions != (local["width"], local["height"]):
        raise CollectionDataError(f"Frozen local asset dimensions changed: {relative}")
    return {"matched": True, "path": str(path), "sha256": digest,
            "width": dimensions[0], "height": dimensions[1], "bytes": len(data)}


def _load_faces(selection: dict, config: dict, ops_root: Path, selection_path: Path,
                max_faces: int, max_image_downloads: int) -> list[dict]:
    if not 1 <= max_faces <= 20:
        raise CollectionDataError("--max-faces must be between 1 and 20")
    if not 0 <= max_image_downloads <= 120:
        raise CollectionDataError("--max-image-downloads must be between 0 and 120")
    if max_faces > int(selection["max_faces"]):
        raise CollectionDataError("--max-faces exceeds the frozen selection budget")
    if max_image_downloads > int(selection["max_image_downloads"]):
        raise CollectionDataError("--max-image-downloads exceeds the frozen selection budget")

    catalogue = ops_root / "catalogue.sqlite"
    actual_sha = _source_sha256(catalogue)
    if selection["catalogue_sha256"] != actual_sha:
        raise CollectionDataError("Frozen selection catalogue SHA-256 does not match the normalized catalogue")

    aliases = _set_presence_map(ops_root / "reports" / "edition_aliases.json")
    sets_by_id = {}
    try:
        with closing(sqlite3.connect(catalogue)) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT id,code,name,eligibility FROM sets"):
                sets_by_id[row["id"]] = dict(row)
    except sqlite3.Error as exc:
        raise PipelineStorageError(f"Cannot read catalogue set list: {exc}") from exc

    selected_sets = {row["set_id"]: row for row in selection["sets"]}
    if len(selected_sets) != len(selection["sets"]):
        raise CollectionDataError("Pilot selection repeats a set_id")
    for set_id, frozen in selected_sets.items():
        actual = sets_by_id.get(set_id)
        if not actual or actual["eligibility"] != "eligible":
            raise CollectionDataError(f"Pilot set is absent or outside eligible scope: {set_id}")
        if (actual["code"], actual["name"]) != (frozen["code"], frozen["name"]):
            raise CollectionDataError(f"Frozen set identity changed for {set_id}")
        present = set_id in aliases
        if present != frozen["local_folder_present"]:
            raise CollectionDataError(f"Local folder presence changed for frozen set {frozen['code']}")
        expected_status = "existing_partial" if present else "new_edition_partial_pilot"
        if frozen["publication_status"] != expected_status:
            raise CollectionDataError(f"Invalid partial-pilot status for set {frozen['code']}")

    group_by_id = {}
    for set_id in selected_sets:
        for group in printing_groups_from_catalogue(catalogue, set_id):
            group_by_id[group.printing_id] = group

    library_root = Path(config["paths"]["library_root"])
    face_entries = []
    seen = set()
    for item in selection["faces"]:
        key = (item["printing_id"], int(item["face_index"]))
        if key in seen:
            raise CollectionDataError(f"Pilot selection repeats physical face {key}")
        seen.add(key)
        if item["set_id"] not in selected_sets:
            raise CollectionDataError(f"Pilot face references an undeclared set: {item['set_id']}")
        group = group_by_id.get(item["printing_id"])
        if group is None or group.set_id != item["set_id"]:
            raise CollectionDataError(f"Frozen printing identity is absent or changed: {item['printing_id']}")
        physical = next((face for face in physical_faces(group) if face.face_index == item["face_index"]), None)
        if physical is None:
            raise CollectionDataError(f"Frozen physical face does not exist: {key}")
        actual = _identity_for_face(physical, group, catalogue)
        expected = item["expected"]
        if any(expected.get(name) != value for name, value in actual.items()):
            mismatches = [name for name, value in actual.items() if expected.get(name) != value]
            raise CollectionDataError(f"Frozen face identity differs for {key}: {', '.join(mismatches)}")
        local_asset = _verify_local_asset(item, library_root)
        face_entries.append({"selection": item, "face": physical, "group": group,
                             "local_asset": local_asset})

    if len(face_entries) != len(selection["faces"]):
        raise CollectionDataError("Not every frozen pilot face was verified")
    if len(face_entries) > 20:
        raise CollectionDataError("Frozen pilot contains more than 20 physical faces")

    report_path = ops_root / "reports" / "pilot-reconciliation.json"
    if not report_path.is_file():
        raise CollectionDataError(f"Exact pilot reconciliation report is missing: {report_path}")
    gallery_path = ops_root / "sources" / "wizards" / "gallery_catalogue.json"
    gallery = _json(gallery_path)
    reconciliation = _json(report_path)
    candidates_per_language = int(config["image_policy"]["max_candidates_per_language"])
    prepared = []
    for entry in face_entries[:max_faces]:
        face = entry["face"]
        verified = verified_local_candidates(face, library_root, reconciliation)
        verified.extend(verified_gallery_candidates(face, gallery, reconciliation))
        candidates = candidates_for_face(face, verified)
        limited = []
        lang_counts = {"fr": 0, "en": 0, "unknown": 0}
        for candidate in candidates:
            lang = candidate.language if candidate.language in {"fr", "en"} else "unknown"
            if lang in {"fr", "en"} and lang_counts[lang] >= candidates_per_language:
                continue
            if lang in {"fr", "en"}:
                lang_counts[lang] += 1
            limited.append(candidate)
        prepared.append({**entry, "candidates": limited, "candidate_counts": lang_counts})
    return prepared


def _candidate_plan(candidate: ImageCandidate) -> dict:
    return {"provider": candidate.provider, "language": candidate.language,
            "candidate_key": candidate.candidate_key,
            "url": candidate.url, "local_path": candidate.local_path,
            "declared_quality": candidate.declared_quality,
            "exact_identity": candidate.exact_identity,
            "identity_evidence": candidate.identity_evidence}


def collect_pilot(config_path: str | Path, selection_path: str | Path, *,
                  max_faces: int = 20, max_image_downloads: int = 120,
                  dry_run: bool = False) -> dict:
    config_path, selection_path = Path(config_path).resolve(), Path(selection_path).resolve()
    config, ops_root = load_valid_config(config_path)
    if Path(config["paths"]["staging_root"]).resolve() == Path(config["paths"]["library_root"]).resolve():
        raise CollectionDataError("Pilot staging root must be separate from the production library")
    if not Path(config["paths"]["staging_root"]).resolve().is_relative_to(ops_root.resolve()):
        raise CollectionDataError("Pilot staging root must remain under the operations directory")
    selection = _load_selection(selection_path, ops_root / "schemas" / "pilot-selection.schema.json")
    if len(selection["faces"]) > int(selection["max_faces"]):
        raise CollectionDataError("Frozen selection face count exceeds its declared max_faces")
    prepared = _load_faces(selection, config, ops_root, selection_path, max_faces, max_image_downloads)
    root = Path(config["paths"]["staging_root"]) / "pilot"
    cycle = selection["selection_id"]
    get_count_bound = sum(sum(1 for candidate in row["candidates"] if candidate.url)
                          * (1 + int(config["network"].get("max_redirects", 2))) for row in prepared)
    estimated_cache = min(max_image_downloads, get_count_bound) * int(config["network"]["max_image_bytes"])
    final_uncompressed = len(prepared) * int(config["image_policy"]["output_width"]) * int(
        config["image_policy"]["output_height"]) * 4
    reserve = int((estimated_cache + final_uncompressed * 3 + int(config["network"]["max_image_bytes"])) * 1.2)
    reserve += 5 * 1024 ** 3
    disk = shutil.disk_usage(root.parent if root.parent.exists() else root.anchor)
    if not dry_run and disk.free < reserve:
        raise PipelineStorageError(f"Insufficient disk reserve for bounded pilot: free={disk.free}, required={reserve}")

    plan = {
        "schema_version": "1.0.0", "selection_id": cycle,
        "selection_sha256": hashlib.sha256(selection_path.read_bytes()).hexdigest(),
        "catalogue_sha256": selection["catalogue_sha256"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "dry_run" if dry_run else "collection",
        "max_faces": max_faces, "max_image_downloads": max_image_downloads,
        "selected_set_ids": list(dict.fromkeys(row["selection"]["set_id"] for row in prepared)),
        "selected_face_count": len(prepared),
        "selection_face_count": len(selection["faces"]),
        "candidate_get_upper_bound_including_redirects": get_count_bound,
        "max_redirects_per_image_get": int(config["network"].get("max_redirects", 2)),
        "staging_root": str(root.resolve()), "production_library_root": str(Path(config["paths"]["library_root"]).resolve()),
        "disk_free_bytes_before": disk.free, "disk_reserve_required_bytes": reserve,
        "candidate_sources": {"scryfall": 0, "wizards_gallery": 0, "local_exact": 0,
                              "gatherer": 0, "smfcorp": 0, "magiccorporation": 0},
        "faces": []
    }
    for entry in prepared:
        for candidate in entry["candidates"]:
            plan["candidate_sources"][candidate.provider] = plan["candidate_sources"].get(candidate.provider, 0) + 1
        plan["faces"].append({
            "set_code": entry["face"].set_code,
            "printing_id": entry["face"].printing_id,
            "collector_number": entry["face"].collector_number,
            "face_index": entry["face"].face_index,
            "purpose": entry["selection"]["purpose"],
            "expected_identity": entry["selection"]["expected"],
            "local_asset": entry["local_asset"],
            "candidate_counts": entry["candidate_counts"],
            "candidates": [_candidate_plan(candidate) for candidate in entry["candidates"]],
        })

    report_dir = ops_root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"pilot-collection-{stamp}.json"
    if dry_run:
        plan["status"] = "planned"
        plan["image_gets_executed"] = 0
        report_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        plan["report_path"] = str(report_path)
        return plan

    root.mkdir(parents=True, exist_ok=True)
    image_staging = root / "image_staging"
    cache_root = root / "cache"
    state_path = root / "state.sqlite"
    started_utc = datetime.now(timezone.utc)
    started = time.monotonic()
    before_cache = _tree_bytes(cache_root)
    before_staging = _tree_bytes(image_staging)
    outcomes = []
    interrupted = False
    with FileLock(root / ".publish.lock"):
        with ImageStateStore(state_path) as state:
            cycle_gets_before = state.total_get_count(cycle)
            event_id_before = int(state.conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM image_get_events").fetchone()[0])
            transport = ImageTransport(state, cache_root, config)
            transport.max_total_gets = max_image_downloads
            set_rows = [{"id": row["set_id"], "code": row["code"], "name": row["name"]}
                        for row in selection["sets"]]
            edition_folders = safe_edition_folders(set_rows)
            current_generation_id = None
            try:
                current_publication = Publisher(root, ops_root / "schemas", config["paths"]["library_root"],
                                                config["publication"]["keep_valid_generations"])
                current_generation_id = current_publication.validate_current(check_images=True)["generation_id"]
            except Exception:
                current_generation_id = None
            for index, entry in enumerate(prepared):
                face = entry["face"]
                task_start = time.monotonic()
                try:
                    published_image = None
                    prior = state.conn.execute(
                        "SELECT result_json FROM image_results WHERE task_key=? AND cycle=?",
                        (f"{face.printing_id}:face:{face.face_index}", cycle),
                    ).fetchone()
                    if prior and current_generation_id:
                        prior_result = json.loads(prior["result_json"])
                        if prior_result.get("status") == "ready" and prior_result.get("selected_language") in {"fr", "en"}:
                            lang_folder = "images_fr" if prior_result["selected_language"] == "fr" else "cartes anglaises"
                            published_image = (root / "generations" / current_generation_id /
                                               edition_folders[entry["selection"]["set_id"]] /
                                               lang_folder / Path(prior_result["path"]).name)
                    result = collect_face(entry["candidates"], image_staging, state, transport,
                                          cycle=cycle,
                                          max_candidates=int(config["image_policy"]["max_candidates_per_language"]),
                                          max_gets=int(config["image_policy"]["max_image_gets_per_language"]),
                                          printing_id=face.printing_id, face_index=face.face_index,
                                          published_image=published_image)
                except KeyboardInterrupt:
                    interrupted = True
                    break
                outcomes.append({"set_code": face.set_code, "printing_id": face.printing_id,
                                 "collector_number": face.collector_number,
                                 "face_index": face.face_index, "purpose": entry["selection"]["purpose"],
                                 "duration_seconds": round(time.monotonic() - task_start, 3),
                                 "result": result.to_dict()})
                if state.total_get_count(cycle) >= max_image_downloads and index + 1 < len(prepared):
                    break

            total_gets = state.total_get_count(cycle)
            attempt_rows = [dict(row) for row in state.conn.execute(
                "SELECT task_key,language,provider,status,reason,get_count FROM image_attempts "
                "WHERE cycle=? ORDER BY task_key,language,candidate_key", (cycle,))]
            event_rows = [dict(row) for row in state.conn.execute(
                "SELECT id,task_key,language,candidate_key,url,status,created_utc FROM image_get_events "
                "WHERE cycle=? ORDER BY id", (cycle,))]
            result_rows = [dict(row) for row in state.conn.execute(
                "SELECT task_key,result_json,updated_utc FROM image_results WHERE cycle=? ORDER BY task_key", (cycle,))]
            transport.session.close()

    all_ready = (len(prepared) == len(selection["faces"])
                 and len(outcomes) == len(prepared)
                 and all(row["result"]["status"] == "ready" for row in outcomes))
    plan.update({
        "status": "interrupted" if interrupted else ("complete" if all_ready else "incomplete"),
        "started_at_utc": started_utc.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round(time.monotonic() - started, 3),
        "image_gets_executed": total_gets - cycle_gets_before,
        "cycle_total_gets": total_gets,
        "raw_cache_bytes_before": before_cache,
        "raw_cache_bytes_after": _tree_bytes(cache_root),
        "image_staging_bytes_before": before_staging,
        "image_staging_bytes_after": _tree_bytes(image_staging),
        "state_database_bytes": state_path.stat().st_size if state_path.exists() else 0,
        "journal_bytes": (state_path.with_name(state_path.name + "-wal").stat().st_size
                          if state_path.with_name(state_path.name + "-wal").exists() else 0),
        "outcomes": outcomes,
        "attempts": attempt_rows,
        "get_events": event_rows,
        "get_events_this_run": [row for row in event_rows if row["id"] > event_id_before],
        "persisted_results": [{**row, "result_json": json.loads(row["result_json"])} for row in result_rows],
        "faces_not_processed_this_run": max(0, len(prepared) - len(outcomes)),
    })
    plan["report_path"] = str(report_path)
    serialized = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    plan["report_bytes"] = len(serialized.encode("utf-8"))
    report_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    plan["network_get_limit_respected"] = total_gets <= max_image_downloads
    if not plan["network_get_limit_respected"]:
        raise PipelineStorageError("Image GET ledger exceeded the configured global budget")
    return plan


def _tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total
