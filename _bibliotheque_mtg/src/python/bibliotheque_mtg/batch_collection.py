from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .faces import physical_faces, printing_groups_from_catalogue
from .images import (ImageCandidate, ImageStateStore, ImageTransport, candidates_for_face,
                     collect_face, verified_gallery_candidates, verified_local_candidates)
from .indexing import safe_edition_folders
from .pipeline import PipelineStorageError, load_valid_config
from .publication import FileLock, InvalidPublication, LockedPublication, atomic_write, safe_relative_path
from .collection import _identity_for_face, _source_sha256


class BatchPlanError(RuntimeError):
    pass


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchPlanError(f"Cannot read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BatchPlanError(f"Expected a JSON object in {path}")
    return value


def _sha(path: Path) -> str:
    if not path.is_file():
        raise BatchPlanError(f"Required source file is missing: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _catalogue_sets(catalogue_path: Path) -> list[dict]:
    try:
        with sqlite3.connect(catalogue_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sets)")}
            required = {"id", "code", "name", "eligibility"}
            if not required <= columns:
                raise BatchPlanError(f"Catalogue sets table is missing {sorted(required - columns)}")
            wanted = ["id", "code", "name", "eligibility", "released_at", "set_type", "digital", "card_count"]
            select = ",".join(name if name in columns else f"NULL AS {name}" for name in wanted)
            rows = conn.execute(f"SELECT {select} FROM sets ORDER BY COALESCE(released_at,''),code,id").fetchall()
    except sqlite3.Error as exc:
        raise BatchPlanError(f"Cannot read catalogue set list: {exc}") from exc
    return [dict(zip(wanted, row)) for row in rows]


def _current_edition_ids(library_root: Path) -> set[str]:
    index_path = library_root / "index.json"
    if not index_path.is_file():
        return set()
    value = _load_json(index_path)
    result = set()
    for edition in value.get("editions", []):
        local_id = str(edition.get("local_id", ""))
        if local_id.startswith("scryfall-set-"):
            result.add(local_id.removeprefix("scryfall-set-"))
    return result


def _latest_image_results(path: Path) -> dict[tuple[str, int], dict]:
    if not path.is_file():
        return {}
    try:
        with sqlite3.connect(path) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='image_results'").fetchone():
                return {}
            rows = conn.execute("SELECT task_key,result_json FROM image_results ORDER BY updated_utc,rowid").fetchall()
    except sqlite3.Error as exc:
        raise PipelineStorageError(f"Cannot inspect persistent image state {path}: {exc}") from exc
    result = {}
    for task_key, raw in rows:
        try:
            printing_id, face_part, face_index = task_key.rsplit(":", 2)
            if face_part != "face":
                continue
            result[(printing_id, int(face_index))] = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            continue
    return result


def _current_face_images(library_root: Path, set_folders: dict[str, str]) -> dict[tuple[str, int], dict]:
    result = {}
    for folder in set_folders.values():
        path = library_root / folder / "index.json"
        if not path.is_file():
            continue
        index = _load_json(path)
        for card in index.get("cards", []):
            for face in card.get("faces", []):
                result[(card["printing_id"], int(face["face_index"]))] = face.get("image", {})
    return result


def _candidate_rows(face, library_root: Path, reconciliation: dict, gallery: dict,
                    max_candidates_per_language: int) -> list[ImageCandidate]:
    candidates = verified_local_candidates(face, library_root, reconciliation)
    candidates.extend(verified_gallery_candidates(face, gallery, reconciliation))
    candidates = candidates_for_face(face, candidates)
    counts = {"fr": 0, "en": 0}
    limited = []
    for candidate in candidates:
        if candidate.language in counts:
            if counts[candidate.language] >= max_candidates_per_language:
                continue
            counts[candidate.language] += 1
        limited.append(candidate)
    return limited


def _candidate_record(candidate: ImageCandidate) -> dict:
    return {**asdict(candidate), "candidate_key": candidate.candidate_key}


def _candidate_from_record(row: dict) -> ImageCandidate:
    values = {key: value for key, value in row.items() if key in ImageCandidate.__dataclass_fields__}
    candidate = ImageCandidate(**values)
    if row.get("candidate_key") != candidate.candidate_key:
        raise BatchPlanError("Frozen candidate key does not match its URL/local path")
    return candidate


def _plan_schema_validate(value: dict, path: Path):
    schema = _load_json(path)
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    if errors:
        details = "; ".join(f"/{'/'.join(map(str, item.absolute_path))}: {item.message}"
                            for item in errors[:12])
        raise BatchPlanError(f"Collection batch plan schema is invalid: {details}")


def create_batch_plan(config_path: str | Path, mode: str, *, max_faces: int = 100,
                      max_image_downloads: int = 600, max_sets: int = 1,
                      set_ids: list[str] | None = None) -> dict:
    if mode not in {"pending_existing", "missing_sets"}:
        raise BatchPlanError(f"Unsupported collection batch mode: {mode}")
    if not 1 <= max_faces <= 100 or not 1 <= max_image_downloads <= 600 or not 1 <= max_sets <= 5:
        raise BatchPlanError("Batch limits are max_faces 1..100, max_image_downloads 1..600, max_sets 1..5")
    config, ops_root = load_valid_config(Path(config_path).resolve())
    catalogue_path = ops_root / "catalogue.sqlite"
    library = Path(config["paths"]["library_root"]).resolve()
    library_stage = Path(config["paths"]["staging_root"]) / "library"
    aliases_path = ops_root / "reports" / "inventory-production.json"
    reconciliation_path = ops_root / "reports" / "reconciliation-existing.json"
    gallery_path = ops_root / "sources" / "wizards" / "gallery_catalogue.json"
    for required in (aliases_path, reconciliation_path, gallery_path):
        if not required.is_file():
            raise BatchPlanError(f"Required reconciled source is missing: {required}")
    inventory, reconciliation, gallery = (_load_json(aliases_path), _load_json(reconciliation_path),
                                           _load_json(gallery_path))
    expected_inventory_sha = hashlib.sha256(aliases_path.read_bytes()).hexdigest()
    if reconciliation.get("inventory_sha256") != expected_inventory_sha:
        raise BatchPlanError("All-existing reconciliation is stale; rerun audit and reconciliation before planning")
    source_hashes = {"inventory": expected_inventory_sha,
                     "reconciliation": hashlib.sha256(reconciliation_path.read_bytes()).hexdigest(),
                     "gallery": hashlib.sha256(gallery_path.read_bytes()).hexdigest()}
    bulk_sha = _source_sha256(catalogue_path)
    current_set_ids = _current_edition_ids(library)
    library_marker_path = library / "publication.json"
    base_generation = None
    if library_marker_path.is_file():
        base_generation = _load_json(library_marker_path).get("generation_id")
    set_rows = [row for row in _catalogue_sets(catalogue_path) if row["eligibility"] == "eligible"]
    by_id = {row["id"]: row for row in set_rows}
    unknown = [value for value in (set_ids or []) if value not in by_id]
    if unknown:
        raise BatchPlanError(f"Requested set IDs are missing or ineligible: {unknown}")
    if set_ids:
        requested = set(dict.fromkeys(set_ids))
    elif mode == "pending_existing":
        requested = {candidate["set_id"] for alias in inventory.get("edition_aliases", [])
                     if alias.get("status") == "confirmed_exact_candidate"
                     for candidate in alias.get("candidates", []) if candidate.get("set_id") in by_id}
    else:
        requested = set(by_id) - current_set_ids
    if mode == "missing_sets" and not set_ids:
        requested -= current_set_ids
    set_rows = [row for row in set_rows if row["id"] in requested]
    if not set_rows:
        raise BatchPlanError("No eligible set is available for this collection mode")

    set_folders = safe_edition_folders(set_rows)
    state_path = library_stage / "state.sqlite"
    prior_results = _latest_image_results(state_path)
    current_images = _current_face_images(library, set_folders)
    max_candidates = int(config["image_policy"]["max_candidates_per_language"])
    set_rows.sort(key=lambda row: (str(row.get("released_at") or "9999-99-99"),
                                   str(row["code"]).casefold(), row["id"]))
    chosen_sets, tasks = [], []
    for set_row in set_rows:
        if len(chosen_sets) >= max_sets or len(tasks) >= max_faces:
            break
        pending_count = 0
        set_tasks = []
        for group in printing_groups_from_catalogue(catalogue_path, set_row["id"]):
            for face in physical_faces(group):
                key = (face.printing_id, face.face_index)
                prior = prior_results.get(key)
                current = current_images.get(key, {})
                if prior and prior.get("status") in {"ready", "missing"}:
                    continue
                if current.get("status") == "ready" and current.get("quality") == "target":
                    continue
                if mode == "pending_existing" and set_row["id"] not in current_set_ids:
                    continue
                pending_count += 1
                if len(tasks) + len(set_tasks) < max_faces:
                    candidates = _candidate_rows(face, library, reconciliation, gallery, max_candidates)
                    expected = _identity_for_face(face, group, catalogue_path)
                    purpose = ("existing_low_resolution" if current.get("status") == "ready"
                               else "missing_image" if current or prior else "new_set_image")
                    set_tasks.append({"set_id": set_row["id"], "set_code": str(set_row["code"]),
                                      "set_folder": set_folders[set_row["id"]],
                                      "printing_id": group.printing_id, "face_index": face.face_index,
                                      "collector_number": str(face.collector_number or ""),
                                      "purpose": purpose, "expected": expected,
                                      "candidates": [_candidate_record(item) for item in candidates]})
        if not pending_count:
            continue
        set_tasks.sort(key=lambda item: (item["collector_number"].casefold(),
                                         item["printing_id"], item["face_index"]))
        tasks.extend(set_tasks)
        chosen_sets.append({"set_id": set_row["id"], "code": str(set_row["code"]),
                            "name": str(set_row["name"]), "released_at": set_row.get("released_at"),
                            "face_count": pending_count, "tasks_selected": len(set_tasks),
                            "set_complete_in_this_plan": len(set_tasks) == pending_count})
        if len(tasks) >= max_faces:
            break
    if not tasks:
        return {"status": "no_work", "mode": mode, "eligible_sets_considered": len(set_rows),
                "base_generation_id": base_generation, "report_path": None}

    batch_id = f"batch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    image_get_budget = min(max_image_downloads, int(config["image_policy"]["max_image_gets_per_language"]) * 2 * len(tasks))
    image_get_budget = max(1, image_get_budget)
    free_bytes = shutil.disk_usage(library_stage.parent if library_stage.parent.exists() else ops_root).free
    raw_cache_reserve = max_image_downloads * int(config["network"]["max_image_bytes"])
    normalized_reserve = len(tasks) * int(config["image_policy"]["output_width"]) * int(
        config["image_policy"]["output_height"]) * 4
    reserve = int((raw_cache_reserve + normalized_reserve + int(config["network"]["max_image_bytes"])) * 1.2)
    reserve += int(config["storage"]["minimum_free_bytes"])
    if free_bytes < reserve:
        raise PipelineStorageError(f"Insufficient disk reserve for collection batch: free={free_bytes}, required={reserve}")
    plan = {"schema_version": "1.0.0", "batch_id": batch_id, "mode": mode,
            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "catalogue_sha256": bulk_sha, "source_hashes": source_hashes,
            "base_library_generation_id": base_generation,
            "max_faces": max_faces, "max_image_downloads": image_get_budget,
            "max_sets": max_sets, "selected_set_ids": [row["set_id"] for row in chosen_sets],
            "sets": chosen_sets,
            "faces_planned": len(tasks), "candidate_get_upper_bound": image_get_budget,
            "max_gets_per_language": int(config["image_policy"]["max_image_gets_per_language"]),
            "max_redirects_per_get": int(config["network"].get("max_redirects", 2)),
            "disk_free_bytes_before": free_bytes, "disk_reserve_required_bytes": reserve,
            "tasks": tasks}
    _plan_schema_validate(plan, ops_root / "schemas" / "collection-batch.schema.json")
    plans_root = ops_root / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)
    plan_path = plans_root / f"{batch_id}.json"
    payload = (json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if plan_path.exists():
        raise BatchPlanError(f"Refusing to overwrite immutable batch plan: {plan_path}")
    atomic_write(plan_path, payload)
    digest = hashlib.sha256(payload).hexdigest()
    atomic_write(plan_path.with_suffix(".sha256"), (digest + "  " + plan_path.name + "\n").encode("ascii"))
    report_path = ops_root / "reports" / f"plan-{batch_id}.json"
    summary = {key: value for key, value in plan.items() if key != "tasks"}
    summary.update({"status": "planned", "task_count": len(tasks), "plan_path": str(plan_path),
                    "plan_sha256": digest, "report_path": str(report_path),
                    "candidate_counts": dict(Counter(candidate["provider"]
                        for task in tasks for candidate in task["candidates"])),
                    "candidate_language_counts": dict(Counter(candidate["language"] or "unknown"
                        for task in tasks for candidate in task["candidates"]))})
    atomic_write(report_path, (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return summary


def collect_batch_plan(config_path: str | Path, plan_path: str | Path, *, dry_run: bool = False) -> dict:
    config_path = Path(config_path).resolve()
    config, ops_root = load_valid_config(config_path)
    plan_path = Path(plan_path).resolve()
    plans_root = (ops_root / "plans").resolve()
    if not plan_path.is_relative_to(plans_root) or not plan_path.is_file():
        raise BatchPlanError("Batch plan must be an existing file inside the operations plans directory")
    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes.decode("utf-8"))
    _plan_schema_validate(plan, ops_root / "schemas" / "collection-batch.schema.json")
    plan_sha = hashlib.sha256(plan_bytes).hexdigest()
    checksum_path = plan_path.with_suffix(".sha256")
    if not checksum_path.is_file() or checksum_path.read_text(encoding="ascii").split()[0] != plan_sha:
        raise BatchPlanError("Immutable plan checksum is missing or does not match its JSON")
    catalogue_path = ops_root / "catalogue.sqlite"
    if plan["catalogue_sha256"] != _source_sha256(catalogue_path):
        raise BatchPlanError("Frozen plan catalogue fingerprint is stale")
    inventory_path = ops_root / "reports" / "inventory-production.json"
    reconciliation_path = ops_root / "reports" / "reconciliation-existing.json"
    gallery_path = ops_root / "sources" / "wizards" / "gallery_catalogue.json"
    current_hashes = {"inventory": _sha(inventory_path), "reconciliation": _sha(reconciliation_path),
                      "gallery": _sha(gallery_path)}
    if current_hashes != plan["source_hashes"]:
        raise BatchPlanError("One or more frozen reconciliation/gallery sources changed after planning")
    reconciliation = _load_json(reconciliation_path)
    gallery = _load_json(gallery_path)
    library_root = Path(config["paths"]["library_root"]).resolve()
    root = Path(config["paths"]["staging_root"]) / "library"
    image_staging = root / "image_staging"
    cache_root = root / "cache"
    state_path = root / "state.sqlite"
    if dry_run:
        return {"status": "planned", "mode": plan["mode"], "batch_id": plan["batch_id"],
                "plan_sha256": plan_sha, "planned_tasks": len(plan["tasks"]),
                "network_gets_executed": 0, "staging_state_created": False}

    set_ids = set(plan["selected_set_ids"])
    selected_sets = [row for row in _catalogue_sets(catalogue_path) if row["id"] in set_ids]
    set_folders = safe_edition_folders(selected_sets)
    current_generation_root = None
    marker_path = root / "publication.json"
    if marker_path.is_file():
        marker = _load_json(marker_path)
        generation_id = marker.get("generation_id")
        if not isinstance(generation_id, str) or not generation_id:
            raise BatchPlanError("Current publication marker has no generation id")
        try:
            generation_relative = safe_relative_path(generation_id)
        except (TypeError, ValueError) as exc:
            raise BatchPlanError("Current publication generation id is unsafe") from exc
        generations_root = (root / "generations").resolve()
        current_generation_root = (generations_root / generation_relative).resolve()
        if not current_generation_root.is_relative_to(generations_root) or not current_generation_root.is_dir():
            raise BatchPlanError("Current publication generation is missing or escapes the staging root")

    groups_by_id = {}
    for set_id in set_ids:
        for group in printing_groups_from_catalogue(catalogue_path, set_id):
            groups_by_id[group.printing_id] = group
    prepared = []
    candidates_per_language = int(config["image_policy"]["max_candidates_per_language"])
    for task in plan["tasks"]:
        group = groups_by_id.get(task["printing_id"])
        if group is None or group.set_id != task["set_id"]:
            raise BatchPlanError(f"Frozen printing is missing or changed: {task['printing_id']}")
        face = next((item for item in physical_faces(group) if item.face_index == task["face_index"]), None)
        if face is None or _identity_for_face(face, group, catalogue_path) != task["expected"]:
            raise BatchPlanError(f"Frozen face identity changed: {task['printing_id']} face {task['face_index']}")
        actual_candidates = _candidate_rows(face, library_root, reconciliation, gallery, candidates_per_language)
        expected_candidates = task["candidates"]
        actual_keys = [_candidate_record(item) for item in actual_candidates]
        if actual_keys != expected_candidates:
            raise BatchPlanError(f"Frozen candidate list changed: {task['printing_id']} face {task['face_index']}")
        prepared.append((task, face, [_candidate_from_record(item) for item in expected_candidates]))

    if not root.resolve().is_relative_to(Path(config["paths"]["staging_root"]).resolve()):
        raise BatchPlanError("Collection staging root escapes configured staging directory")
    reserve = int(plan.get("disk_reserve_required_bytes", config["storage"]["minimum_free_bytes"]))
    free_before = shutil.disk_usage(root.parent if root.parent.exists() else root.anchor).free
    if free_before < reserve:
        raise PipelineStorageError(f"Insufficient disk reserve before batch: free={free_before}, required={reserve}")
    root.mkdir(parents=True, exist_ok=True)
    image_staging.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    before_cache = sum(item.stat().st_size for item in cache_root.rglob("*") if item.is_file())
    before_images = sum(item.stat().st_size for item in image_staging.rglob("*") if item.is_file())
    started = datetime.now(timezone.utc)
    cycle = plan["batch_id"]
    outcomes = []
    network_errors = 0
    consecutive_network_errors = 0
    stop_reason = None
    with FileLock(root / ".publish.lock"):
        with ImageStateStore(state_path) as state:
            gets_before = state.total_get_count(cycle)
            event_id_before = int(state.conn.execute("SELECT COALESCE(MAX(id),0) FROM image_get_events").fetchone()[0])
            prior_results = _latest_image_results(state_path)
            transport = ImageTransport(state, cache_root, config)
            transport.max_total_gets = int(plan["max_image_downloads"])
            for position, (task, face, candidates) in enumerate(prepared, 1):
                try:
                    published_image = None
                    prior = prior_results.get((face.printing_id, face.face_index))
                    if (current_generation_root is not None and prior
                            and prior.get("status") == "ready"
                            and prior.get("selected_language") in {"fr", "en"}):
                        prior_path = Path(str(prior.get("path", "")))
                        expected_language_dir = ("images_fr" if prior.get("selected_language") == "fr"
                                                 else "cartes anglaises")
                        if prior_path.parent.as_posix() == expected_language_dir and prior_path.name:
                            language_dir = expected_language_dir
                            folder = set_folders.get(task["set_id"])
                            if folder:
                                candidate_image = (current_generation_root / folder / language_dir / prior_path.name).resolve()
                                if candidate_image.is_relative_to(current_generation_root):
                                    published_image = candidate_image
                    result = collect_face(candidates, image_staging, state, transport,
                                          cycle=cycle, max_candidates=int(config["image_policy"]["max_candidates_per_language"]),
                                          max_gets=int(config["image_policy"]["max_image_gets_per_language"]),
                                          printing_id=face.printing_id, face_index=face.face_index,
                                          published_image=published_image)
                except KeyboardInterrupt:
                    stop_reason = "keyboard_interrupt"
                    break
                task_errors = [item.get("reason", "") for item in result.errors]
                for reason in task_errors:
                    is_network_error = (reason.startswith("network_error") or reason.startswith("http_5")
                                        or reason in {"http_403", "http_404", "rate_limited",
                                                      "download_time_limit_exceeded", "size_limit_exceeded"})
                    if is_network_error:
                        network_errors += 1
                        consecutive_network_errors += 1
                    elif reason not in {"provider_deferred", "global_get_budget_exhausted"}:
                        consecutive_network_errors = 0
                outcomes.append({"position": position, "set_id": task["set_id"], "set_code": task["set_code"],
                                 "printing_id": face.printing_id, "collector_number": task["collector_number"],
                                 "face_index": face.face_index, "purpose": task["purpose"],
                                 "expected": task["expected"], "result": result.to_dict()})
                total_gets_now = state.total_get_count(cycle)
                attempted = sum(1 for event in state.conn.execute(
                    "SELECT id FROM image_get_events WHERE cycle=?", (cycle,)))
                failure_ratio = network_errors / max(1, attempted)
                if consecutive_network_errors >= 5:
                    stop_reason = "five_consecutive_network_errors"
                    break
                if attempted >= 5 and failure_ratio > 0.20:
                    stop_reason = "network_error_ratio_over_20_percent"
                    break
                if total_gets_now >= int(plan["max_image_downloads"]) and position < len(prepared):
                    stop_reason = "global_image_get_budget_exhausted"
                    break
            total_gets = state.total_get_count(cycle)
            attempts = [dict(row) for row in state.conn.execute(
                "SELECT task_key,language,provider,status,reason,get_count FROM image_attempts "
                "WHERE cycle=? ORDER BY task_key,language,candidate_key", (cycle,))]
            events = [dict(row) for row in state.conn.execute(
                "SELECT id,task_key,language,candidate_key,url,status,created_utc FROM image_get_events "
                "WHERE cycle=? ORDER BY id", (cycle,))]
            transport.session.close()

    ready = sum(row["result"]["status"] == "ready" for row in outcomes)
    missing = sum(row["result"]["status"] != "ready" for row in outcomes)
    deferred_tasks = len(prepared) - len(outcomes)
    complete = len(outcomes) == len(prepared) and missing == 0
    report = {"schema_version": "1.0.0", "batch_id": cycle, "mode": plan["mode"],
              "plan_path": str(plan_path), "plan_sha256": plan_sha,
              "catalogue_sha256": plan["catalogue_sha256"], "started_at_utc": started.isoformat().replace("+00:00", "Z"),
              "finished_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
              "status": "complete" if complete else "interrupted" if stop_reason == "keyboard_interrupt" else "incomplete",
              "stop_reason": stop_reason, "sets": plan["sets"], "planned_tasks": len(prepared),
              "processed_tasks": len(outcomes), "ready_images": ready, "missing_images": missing,
              "tasks_left_for_next_plan": deferred_tasks, "image_get_budget": plan["max_image_downloads"],
              "image_gets_executed": total_gets - gets_before, "network_errors": network_errors,
              "network_error_ratio": network_errors / max(1, total_gets - gets_before),
              "raw_cache_bytes_before": before_cache,
              "raw_cache_bytes_after": sum(item.stat().st_size for item in cache_root.rglob("*") if item.is_file()),
              "image_staging_bytes_before": before_images,
              "image_staging_bytes_after": sum(item.stat().st_size for item in image_staging.rglob("*") if item.is_file()),
              "disk_free_bytes_before": free_before,
              "disk_free_bytes_after": shutil.disk_usage(root.parent).free,
              "outcomes": outcomes, "attempts": attempts,
              "get_events_this_batch": [row for row in events if row["id"] > event_id_before]}
    report_path = ops_root / "reports" / f"collection-{cycle}.json"
    atomic_write(report_path, (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    report["report_path"] = str(report_path)
    report["network_get_limit_respected"] = total_gets <= int(plan["max_image_downloads"])
    if not report["network_get_limit_respected"]:
        raise PipelineStorageError("Image GET ledger exceeded frozen batch budget")
    return report
