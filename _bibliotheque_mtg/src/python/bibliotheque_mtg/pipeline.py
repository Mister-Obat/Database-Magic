from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .indexing import (load_image_results, project_catalogue_index, project_edition_index,
                       render_catalogue_markdown, render_edition_markdown,
                       render_folder_markdown, safe_edition_folders, source_snapshots)
from .faces import physical_faces, printing_groups_from_catalogue
from .images import ImageCandidate, image_relative_path, normalize_png
from .publication import InvalidPublication, Publisher, render_report_markdown
from .sources import SourceFormatError, _load_config, validate_catalogue


class PipelineStorageError(RuntimeError):
    pass


def load_valid_config(config_path: str | Path) -> tuple[dict, Path]:
    path = Path(config_path).resolve()
    ops_root = path.parent
    return _load_config(path, ops_root), ops_root


def _catalogue_sets(catalogue_path: Path) -> list[dict]:
    if not catalogue_path.is_file():
        raise PipelineStorageError(f"Normalized catalogue is missing: {catalogue_path}")
    try:
        conn = sqlite3.connect(catalogue_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT id,code,name,set_type,released_at,digital,card_count,eligibility,exclusion_reason "
                                "FROM sets ORDER BY COALESCE(released_at,''),code,id").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SourceFormatError(f"Cannot read normalized catalogue: {exc}") from exc
    return [dict(row) for row in rows]


def _verified_staged_image(staging_root: Path, result: dict,
                           published_image: Path | None = None) -> bytes | None:
    relative = result.get("path")
    if not isinstance(relative, str) or not relative:
        return None
    # Result paths are produced by image_relative_path and must stay in this
    # configured image staging root before they can be copied into a generation.
    candidates = [(staging_root, staging_root / Path(relative))]
    if published_image is not None:
        candidates.append((published_image.parent, published_image))
    for root, path in candidates:
        candidate = path.resolve()
        if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
            continue
        payload = candidate.read_bytes()
        if hashlib.sha256(payload).hexdigest() != result.get("output_sha256"):
            continue
        try:
            with Image.open(candidate) as image:
                image.load()
                if image.format == "PNG" and image.size == (531, 738):
                    return payload
        except (OSError, UnidentifiedImageError):
            continue
    return None


def _existing_group_indexes(library_root: Path, inventory: dict, reconciliation: dict,
                            selected_sets: list[dict], set_folders: dict[str, str],
                            generation_id: str) -> tuple[dict[str, dict | str], dict[str, str], list[dict]]:
    """Build a complete read-only inventory index for every historical top-level folder."""
    selected_by_id = {row["id"]: row for row in selected_sets}
    groups_by_folder: dict[str, list[dict]] = {}
    for item in inventory.get("files", []):
        folder = item.get("folder")
        if folder:
            groups_by_folder.setdefault(folder, []).append(item)
    reconciliation_by_path = {row["path"]: row for row in reconciliation.get("results", [])}
    alias_by_folder = {row["folder"]: row for row in inventory.get("edition_aliases", [])}
    docs: dict[str, dict | str] = {}
    schema_map: dict[str, str] = {}
    root_groups = []
    for folder in sorted(groups_by_folder, key=str.casefold):
        alias = alias_by_folder.get(folder, {})
        set_ids = [row["set_id"] for row in alias.get("candidates", [])
                   if row.get("set_id") in selected_by_id]
        if alias.get("status") != "confirmed_exact_candidate":
            set_ids = []
        links = [{"edition_id": f"scryfall-set-{set_id}", "code": selected_by_id[set_id]["code"],
                  "index_json": f"../{set_folders[set_id]}/index.json",
                  "index_md": f"../{set_folders[set_id]}/index.md"}
                 for set_id in set_ids]
        assets = []
        unresolved = 0
        for row in sorted(groups_by_folder[folder], key=lambda item: item["path"].casefold()):
            result = reconciliation_by_path.get(row["path"], {})
            category = row.get("category") or "unknown"
            identity_status = "not_applicable"
            printing_id = None
            face_index = None
            language = None
            if result.get("status") == "matched":
                identity_status = "matched"
                printing_id = result.get("printing_id")
                face_index = result.get("resolved_face_index")
                language = (result.get("evidence") or {}).get("language")
            elif category == "image_card_like":
                if result.get("candidate_count", 0) or alias.get("status") == "ambiguous_candidates":
                    identity_status = "ambiguous"
                else:
                    identity_status = "unresolved"
                unresolved += 1
            elif category == "image_review_non_card_shape":
                identity_status = "unresolved"
                unresolved += 1
            elif category.startswith("corrupt"):
                identity_status = "unresolved"
                unresolved += 1
            evidence = []
            if identity_status == "matched":
                details = result.get("evidence") or {}
                if result.get("match_method") == "manual_override":
                    evidence = [f"Décision manuelle par empreinte SHA-256 : {details.get('manual_evidence')}",
                                f"Langue confirmée visuellement : {language}",
                                f"Édition vérifiée : {details.get('set_code')}; cible fournisseur {result.get('target_id')}"]
                else:
                    evidence = [f"Correspondance exacte de nom d’actif Wizards : {details.get('gallery_filename_exact')}",
                                f"Nom imprimé exact : {details.get('printed_name_exact')}",
                                f"Langue confirmée par galerie : {language}",
                                f"Édition confirmée : {details.get('set_code')}; méthode {result.get('match_method')}"]
            elif category == "image_card_like":
                evidence = [f"Image décodable, ratio de carte; identité non publiée comme certaine ({result.get('match_method') or alias.get('status') or 'aucune preuve unique'}).",
                            f"Nombre de candidats observés : {result.get('candidate_count', 0)}"]
            elif category == "image_review_non_card_shape":
                evidence = ["Format/dimensions à revue manuelle; non classé comme carte ni rejeté comme non-carte."]
            elif category.startswith("corrupt"):
                evidence = ["invalid_original : contenu illisible conservé sans modification.",
                            row.get("image_error") or category]
            else:
                evidence = [f"Fichier annexe classé {category}; non importé comme carte."]
            if row.get("duplicate_count", 0) > 1:
                evidence.append(f"Doublon de contenu : {row['duplicate_count']} fichiers; chemins {', '.join(row.get('duplicate_paths', []))}.")
            relative = Path(row["path"])
            relative_in_folder = relative.relative_to(Path(folder)).as_posix()
            if category == "image_card_like":
                kind = "card_image"
            elif category.startswith("corrupt"):
                kind = "corrupt"
            elif category == "image_review_non_card_shape":
                kind = "unknown"
            else:
                kind = "non_card"
            assets.append({"path": relative_in_folder, "sha256": row["sha256"],
                           "bytes": row["size_bytes"], "kind": kind,
                           "identity_status": identity_status, "printing_id": printing_id,
                           "face_index": face_index, "language": language,
                           "evidence": [item for item in evidence if item]})
        candidate_names = [f"{row['set_code']} — {row['set_name']}" for row in alias.get("candidates", [])]
        if alias.get("status") == "confirmed_exact_candidate":
            reason = "Alias de dossier confirmé; les identités individuelles non uniques restent explicitement signalées."
        elif candidate_names:
            reason = f"Groupe historique non assigné à une seule édition ({alias.get('status')}); candidats : {'; '.join(candidate_names)}."
        else:
            reason = f"Édition historique non résolue ({alias.get('status', 'unresolved_identity')}); aucune identité inférée depuis le nom de dossier."
        index = {"schema_version": "1.0.0", "generation_id": generation_id,
                 "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                 "folder": {"folder_id": "legacy-" + hashlib.sha256(folder.encode("utf-8")).hexdigest()[:12],
                            "kind": "historical_group", "display_name": folder, "path": folder,
                            "language": None},
                 "completeness": {"state": "complete" if unresolved == 0 else "incomplete",
                                  "files_expected": len(groups_by_folder[folder]),
                                  "files_indexed": len(assets), "unresolved": unresolved,
                                  "reason": reason},
                 "edition_links": links, "assets": assets}
        index_json = f"{folder}/index.json"
        docs[index_json] = index
        docs[f"{folder}/index.md"] = render_folder_markdown(index)
        schema_map[index_json] = "folder.schema.json"
        root_groups.append({"folder_id": index["folder"]["folder_id"],
                            "display_name": folder, "index_json": index_json,
                            "index_md": f"{folder}/index.md",
                            "edition_ids": [link["edition_id"] for link in links]})
    return docs, schema_map, root_groups


def build_existing_publication(config_path: str | Path) -> tuple[Publisher, dict, dict[str, dict | str], dict[str, bytes], dict[str, str]]:
    """Build the audited historical-folder scope, including only proven source identities."""
    config, ops_root = load_valid_config(config_path)
    library_root = Path(config["paths"]["library_root"]).resolve()
    catalogue_path = ops_root / "catalogue.sqlite"
    validate_catalogue(ops_root)
    inventory_path = ops_root / "reports" / "inventory-production.json"
    reconciliation_path = ops_root / "reports" / "reconciliation-existing.json"
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineStorageError(f"Production audit/reconciliation is missing or invalid: {exc}") from exc
    inventory_sha = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    if reconciliation.get("inventory_sha256") != inventory_sha or reconciliation.get("sample_count") != inventory.get("file_count"):
        raise PipelineStorageError("Production inventory changed after all-existing reconciliation; rerun audit and reconcile")
    unsafe_statuses = {"inventory_stale", "missing_file", "missing_from_inventory", "unsafe_path_rejected"}
    if any(row.get("status") in unsafe_statuses for row in reconciliation.get("results", [])):
        raise PipelineStorageError("Reconciliation contains stale, missing, or unsafe original files")
    all_sets = _catalogue_sets(catalogue_path)
    by_id = {row["id"]: row for row in all_sets}
    aliases = inventory.get("edition_aliases", [])
    selected_ids = sorted({candidate["set_id"]
                           for alias in aliases if alias.get("status") == "confirmed_exact_candidate"
                           for candidate in alias.get("candidates", [])
                           if candidate.get("set_id") in by_id})
    if not selected_ids:
        raise PipelineStorageError("No historically confirmed edition can be published")
    selected = [by_id[set_id] for set_id in selected_ids]
    set_folders = safe_edition_folders(selected)
    generation_id = f"g-existing-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    snapshots = source_snapshots(ops_root)
    as_of = date.today().isoformat()
    publisher = Publisher(Path(config["paths"]["staging_root"]) / "existing",
                          ops_root / "schemas", library_root,
                          config["publication"]["keep_valid_generations"])
    provider_groups = {}
    set_groups = {}
    for set_row in selected:
        groups = printing_groups_from_catalogue(catalogue_path, set_row["id"])
        set_groups[set_row["id"]] = groups
        for group in groups:
            for provider_id in group.provider_ids.values():
                provider_groups[provider_id] = group
    files_by_path = {row["path"]: row for row in inventory.get("files", [])}
    alias_by_folder = {row["folder"]: row for row in inventory.get("edition_aliases", [])}
    image_rows: dict[tuple[str, int], dict] = {}
    image_sources: dict[tuple[str, int], tuple[dict, dict]] = {}
    local_assets_by_set: dict[str, list[dict]] = {set_id: [] for set_id in selected_ids}
    results_by_path = {row["path"]: row for row in reconciliation.get("results", [])}
    for row in reconciliation.get("results", []):
        if row.get("status") != "matched":
            continue
        group = provider_groups.get(row.get("target_id"))
        if group is None:
            raise PipelineStorageError(f"Matched local file targets a card outside the confirmed editions: {row.get('path')}")
        face_index = row.get("face_index") if row.get("face_index") is not None else 0
        if face_index < 0 or face_index >= len(physical_faces(group)):
            raise PipelineStorageError(f"Matched local file references an invalid face: {row.get('path')}")
        source = files_by_path.get(row["path"])
        if not source or source.get("category") != "image_card_like":
            raise PipelineStorageError(f"Matched local file has no valid card-like inventory row: {row.get('path')}")
        if row.get("target_id") not in group.provider_ids.values():
            raise PipelineStorageError(f"Matched provider ID does not belong to its printing group: {row.get('path')}")
        evidence = row.get("evidence") or {}
        alias = alias_by_folder.get(Path(row["path"]).parts[0], {})
        confirmed_codes = {item.get("set_code") for item in alias.get("candidates", [])}
        if (alias.get("status") != "confirmed_exact_candidate" or group.set_code not in confirmed_codes
                or evidence.get("set_code") not in {None, group.set_code}):
            raise PipelineStorageError(f"Matched card edition conflicts with its historical-folder evidence: {row.get('path')}")
        if (group.printing_id, face_index) in image_sources:
            previous = image_sources[(group.printing_id, face_index)][0]
            raise PipelineStorageError(f"Two local files claim the same printing face: {previous['path']} and {row['path']}")
        image_sources[(group.printing_id, face_index)] = (row, source)
        row["printing_id"] = group.printing_id
        row["resolved_face_index"] = face_index
        local_assets_by_set[group.set_id].append({
            "path": row["path"], "sha256": source["sha256"], "bytes": source["size_bytes"],
            "classification": "matched_card", "printing_id": group.printing_id,
            "evidence": [row.get("match_method", "exact reconciliation"),
                         f"provider card id {row['target_id']}",
                         f"physical face {face_index}"]})
    artifacts: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="magic-existing-png-") as temp_dir:
        for (printing_id, face_index), (match, source) in sorted(image_sources.items()):
            original = (library_root / Path(match["path"])).resolve(strict=True)
            if not original.is_relative_to(library_root) or not original.is_file():
                raise PipelineStorageError(f"Matched source escapes or is missing: {match['path']}")
            payload = original.read_bytes()
            if len(payload) != source["size_bytes"] or hashlib.sha256(payload).hexdigest() != source["sha256"]:
                raise PipelineStorageError(f"Original changed after reconciliation: {match['path']}")
            try:
                with Image.open(io.BytesIO(payload)) as source_image:
                    source_image.load()
                    source_width, source_height = source_image.size
                    source_format = source_image.format or "unknown"
            except (OSError, UnidentifiedImageError) as exc:
                raise PipelineStorageError(f"Matched image no longer decodes: {match['path']}: {exc}") from exc
            group = provider_groups[match["target_id"]]
            details = match.get("evidence") or {}
            language = details.get("language")
            if language not in {"fr", "en"}:
                raise PipelineStorageError(f"Matched local asset has no confirmed language: {match['path']}")
            source_url = details.get("image_url")
            candidate = ImageCandidate(
                printing_id=printing_id, face_index=face_index, language=language,
                provider="local_exact", url=source_url,
                local_path=None if source_url else str(original),
                declared_quality="target" if min(source_width, source_height) >= 531 and max(source_width, source_height) >= 738 else "lowres",
                declared_status="lowres" if min(source_width, source_height) < 531 or max(source_width, source_height) < 738 else None,
                exact_identity=True, expected_set_id=group.set_id, source_set_id=group.set_id,
                expected_collector_number=group.collector_number,
                source_collector_number=group.collector_number,
                identity_evidence="exact Wizards gallery filename, localized printed name, language, and edition")
            temp_output = Path(temp_dir) / f"{printing_id}-{face_index}.png"
            normalize_png(payload, temp_output)
            normalized = temp_output.read_bytes()
            output_sha = hashlib.sha256(normalized).hexdigest()
            relative_image = image_relative_path(candidate, output_sha)
            edition_folder = set_folders[group.set_id]
            artifact_path = f"{edition_folder}/{relative_image}"
            if artifact_path in artifacts:
                raise PipelineStorageError(f"Normalized image path collision: {artifact_path}")
            artifacts[artifact_path] = normalized
            attempts = {"fr": {"candidates": 1 if language == "fr" else 0, "gets": 0},
                        "en": {"candidates": 1 if language == "en" else 0, "gets": 0}}
            image_rows[(printing_id, face_index)] = {
                "status": "ready", "path": relative_image, "selected_language": language,
                "provider": "local_exact", "source_url": source_url,
                "source_width": source_width, "source_height": source_height,
                "source_format": source_format, "source_bytes": len(payload),
                "output_sha256": output_sha, "final_width": 531, "final_height": 738,
                "quality": "target" if candidate.declared_status is None else "low_resolution_fallback",
                "choice_reason": (f"Fichier local identifié par galerie officielle; source {source_width}x{source_height}; "
                                  "retenu en repli basse résolution, aucun GET réseau." if candidate.declared_status
                                  else "Fichier local identifié par galerie officielle et conforme au seuil dimensionnel."),
                "selected_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "attempts": attempts,
                "candidates": [{"provider": "local_exact", "language": language,
                                "url": source_url, "declared_quality": candidate.declared_quality}],
            }
    edition_indexes = []
    documents: dict[str, dict | str] = {}
    schema_map: dict[str, str] = {}
    for set_row in selected:
        set_id = set_row["id"]
        folder = set_folders[set_id]
        printing_ids = {group.printing_id for group in set_groups[set_id]}
        edition_images = {key: value for key, value in image_rows.items() if key[0] in printing_ids}
        edition = project_edition_index(catalogue_path, set_id, generation_id, as_of,
                                        source_rows=snapshots, image_results=edition_images,
                                        folder_name=folder,
                                        local_assets=local_assets_by_set[set_id])
        edition_indexes.append(edition)
        documents[f"{folder}/index.json"] = edition
        documents[f"{folder}/index.md"] = render_edition_markdown(edition)
        schema_map[f"{folder}/index.json"] = "edition.schema.json"
        language_assets = []
        for (printing_id, face_index), result in edition_images.items():
            relative = result["path"]
            filename = Path(relative).name
            payload = artifacts[f"{folder}/{relative}"]
            language_assets.append({"path": filename, "sha256": hashlib.sha256(payload).hexdigest(),
                                    "bytes": len(payload), "kind": "card_image",
                                    "identity_status": "matched", "printing_id": printing_id,
                                    "face_index": face_index, "language": result["selected_language"],
                                    "evidence": [result["choice_reason"]]})
        if language_assets:
            language = language_assets[0]["language"]
            language_dir = "images_fr" if language == "fr" else "cartes anglaises"
            folder_index = {"schema_version": "1.0.0", "generation_id": generation_id,
                            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            "folder": {"folder_id": f"{edition['edition']['local_id']}-{language}",
                                       "kind": "language",
                                       "display_name": "Images françaises" if language == "fr" else "Cartes anglaises",
                                       "path": f"{folder}/{language_dir}", "language": language},
                            "completeness": {"state": "complete", "files_expected": len(language_assets),
                                             "files_indexed": len(language_assets), "unresolved": 0,
                                             "reason": "Images normalisées des originaux identifiés; qualité source détaillée par face."},
                            "edition_links": [],
                            "assets": sorted(language_assets, key=lambda item: item["path"].casefold())}
            folder_json = f"{folder}/{language_dir}/index.json"
            documents[folder_json] = folder_index
            documents[f"{folder}/{language_dir}/index.md"] = render_folder_markdown(folder_index)
            schema_map[folder_json] = "folder.schema.json"
    group_docs, group_schemas, root_groups = _existing_group_indexes(
        library_root, inventory, reconciliation, selected, set_folders, generation_id)
    documents.update(group_docs)
    schema_map.update(group_schemas)
    exclusions = [{"set_id": row["id"], "reason": row["exclusion_reason"] or "explicitly excluded"}
                  for row in all_sets if row["eligibility"] == "explicitly_excluded"]
    catalogue = project_catalogue_index(
        edition_indexes, generation_id, as_of, eligible_set_count=len(selected),
        deferred_set_count=sum(row["eligibility"] == "deferred" for row in all_sets),
        explicit_exclusions=exclusions, source_rows=snapshots, groups=root_groups,
        selection="identified_existing_folders")
    documents["index.json"] = catalogue
    documents["index.md"] = render_catalogue_markdown(catalogue)
    schema_map["index.json"] = "catalogue.schema.json"
    return publisher, catalogue, documents, artifacts, schema_map


def build_publication(config_path: str | Path, scope: str,
                      selected_set_ids: list[str] | None = None) -> tuple[Publisher, dict, dict[str, dict | str], dict[str, bytes], dict[str, str]]:
    if scope == "existing":
        return build_existing_publication(config_path)
    config, ops_root = load_valid_config(config_path)
    catalogue_path = ops_root / "catalogue.sqlite"
    validate_catalogue(ops_root)
    all_sets = _catalogue_sets(catalogue_path)
    eligible_sets = [row for row in all_sets if row["eligibility"] == "eligible"]
    if scope == "pilot":
        if selected_set_ids:
            by_id = {row["id"]: row for row in eligible_sets}
            unknown = [set_id for set_id in selected_set_ids if set_id not in by_id]
            if unknown:
                raise SourceFormatError(f"Pilot set IDs are missing or outside eligible scope: {unknown}")
            selected = [by_id[set_id] for set_id in dict.fromkeys(selected_set_ids)]
        else:
            selection_path = ops_root / "pilot_selection.json"
            if selection_path.is_file():
                try:
                    selection = json.loads(selection_path.read_text(encoding="utf-8-sig"))
                    frozen_ids = [str(row["set_id"]) for row in selection["sets"]]
                except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise SourceFormatError(f"Frozen pilot selection is invalid: {exc}") from exc
                by_id = {row["id"]: row for row in eligible_sets}
                unknown = [set_id for set_id in frozen_ids if set_id not in by_id]
                if unknown:
                    raise SourceFormatError(f"Pilot set IDs are missing or outside eligible scope: {unknown}")
                selected = [by_id[set_id] for set_id in dict.fromkeys(frozen_ids)]
            else:
                # Stage 6 provided an offline one-edition publication smoke test.
                selected = eligible_sets[:1]
        expected_sets = len(selected)
    elif scope in {"library", "existing", "edition"}:
        if scope == "edition" and not selected_set_ids:
            raise SourceFormatError("--scope edition requires at least one --set-id")
        if selected_set_ids:
            by_id = {row["id"]: row for row in eligible_sets}
            unknown = [set_id for set_id in selected_set_ids if set_id not in by_id]
            if unknown:
                raise SourceFormatError(f"Set IDs are missing or outside eligible scope: {unknown}")
            selected = [by_id[set_id] for set_id in dict.fromkeys(selected_set_ids)]
        else:
            selected = eligible_sets
        expected_sets = len(eligible_sets) if scope == "library" else len(selected)
    else:
        raise SourceFormatError(f"Unsupported publication scope: {scope}")

    if scope == "pilot":
        root = Path(config["paths"]["staging_root"]) / "pilot"
    elif scope in {"existing", "edition"}:
        root = Path(config["paths"]["staging_root"]) / scope
    else:
        root = Path(config["paths"]["staging_root"]) / "library"
    publisher = Publisher(root, ops_root / "schemas", config["paths"]["library_root"],
                          config["publication"]["keep_valid_generations"])
    image_results = load_image_results(publisher.state_path)
    image_staging = root / "image_staging"
    current = None
    try:
        current = publisher.validate_current(check_images=True)
    except Exception:
        current = None
    set_folders = safe_edition_folders(selected)
    generation_id = f"g-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    snapshots = source_snapshots(ops_root)
    as_of = date.today().isoformat()
    documents: dict[str, dict | str] = {}
    artifacts: dict[str, bytes] = {}
    schema_map: dict[str, str] = {}
    edition_indexes = []
    root_groups = []

    if scope == "library":
        inventory_path = ops_root / "reports" / "inventory-production.json"
        reconciliation_path = ops_root / "reports" / "reconciliation-existing.json"
        try:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineStorageError(f"Production audit/reconciliation is missing or invalid: {exc}") from exc
        inventory_sha = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
        if (reconciliation.get("inventory_sha256") != inventory_sha
                or reconciliation.get("sample_count") != inventory.get("file_count")):
            raise PipelineStorageError(
                "Production inventory changed after all-existing reconciliation; rerun audit and reconcile")
        unsafe_statuses = {"inventory_stale", "missing_file", "missing_from_inventory", "unsafe_path_rejected"}
        if any(row.get("status") in unsafe_statuses for row in reconciliation.get("results", [])):
            raise PipelineStorageError("Reconciliation contains stale, missing, or unsafe original files")

        catalogue_sets_by_id = {row["id"]: row for row in all_sets}
        identity_set_ids = set(set_folders)
        for alias in inventory.get("edition_aliases", []):
            if alias.get("status") != "confirmed_exact_candidate":
                continue
            identity_set_ids.update(candidate.get("set_id") for candidate in alias.get("candidates", [])
                                    if candidate.get("set_id") in catalogue_sets_by_id)
        provider_groups = {}
        for identity_set_id in sorted(identity_set_ids):
            for group in printing_groups_from_catalogue(catalogue_path, identity_set_id):
                for provider_id in group.provider_ids.values():
                    previous = provider_groups.get(provider_id)
                    if previous is not None and previous.printing_id != group.printing_id:
                        raise PipelineStorageError(f"Provider id maps to multiple printings: {provider_id}")
                    provider_groups[provider_id] = group
        for row in reconciliation.get("results", []):
            if row.get("status") != "matched":
                continue
            face_index = row.get("resolved_face_index", row.get("face_index"))
            if row.get("printing_id") and face_index is not None:
                continue
            group = provider_groups.get(row.get("target_id"))
            if group is None:
                raise PipelineStorageError(
                    f"Matched historical asset has no confirmed printing identity: {row.get('path')}")
            try:
                face_index = int(face_index if face_index is not None else 0)
            except (TypeError, ValueError) as exc:
                raise PipelineStorageError(
                    f"Matched historical asset has invalid face identity: {row.get('path')}") from exc
            if face_index < 0 or face_index >= len(physical_faces(group)):
                raise PipelineStorageError(
                    f"Matched historical asset references an invalid face: {row.get('path')}")
            row["printing_id"] = group.printing_id
            row["resolved_face_index"] = face_index
        group_docs, group_schemas, root_groups = _existing_group_indexes(
            Path(config["paths"]["library_root"]).resolve(), inventory, reconciliation,
            selected, set_folders, generation_id)
        documents.update(group_docs)
        schema_map.update(group_schemas)

    for set_row in selected:
        folder = set_folders[set_row["id"]]
        printing_ids = {group.printing_id for group in printing_groups_from_catalogue(catalogue_path, set_row["id"])}
        edition_images = {}
        language_assets: dict[str, list[dict]] = {"fr": [], "en": []}
        for key, stored_result in image_results.items():
            printing_id, face_index = key
            if printing_id not in printing_ids:
                continue
            result = dict(stored_result)
            language = result.get("selected_language")
            if result.get("status") != "ready" or language not in {"fr", "en"}:
                continue
            folder_relative = Path(folder) / ("images_fr" if language == "fr" else "cartes anglaises") / Path(str(result.get("path", ""))).name
            published_image = ((publisher.generations / current["generation_id"] / folder_relative)
                               if current else None)
            payload = _verified_staged_image(image_staging, result, published_image)
            if published_image is not None and payload is not None:
                # A committed PNG can be used as the source when image staging was pruned.
                result["path"] = str(published_image.relative_to(published_image.parents[1])).replace("\\", "/")
            if payload is None:
                result["status"] = "missing"
                result["choice_reason"] = "staged_image_missing_or_corrupt"
                continue
            filename = Path(result["path"]).name
            language_dir = "images_fr" if language == "fr" else "cartes anglaises"
            relative = f"{language_dir}/{filename}"
            result["path"] = relative
            edition_images[key] = result
            artifact_path = f"{folder}/{relative}"
            artifacts[artifact_path] = payload
            language_assets[language].append({
                "path": filename, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
                "kind": "card_image", "identity_status": "matched", "printing_id": printing_id,
                "face_index": face_index, "language": language,
                "evidence": ["output SHA-256 and PNG dimensions verified from staging"],
            })

        edition = project_edition_index(catalogue_path, set_row["id"], generation_id, as_of,
                                        source_rows=snapshots, image_results=edition_images,
                                        folder_name=folder)
        edition_indexes.append(edition)
        documents[f"{folder}/index.json"] = edition
        documents[f"{folder}/index.md"] = render_edition_markdown(edition)
        schema_map[f"{folder}/index.json"] = "edition.schema.json"
        for language, assets in language_assets.items():
            if not assets:
                continue
            language_dir = "images_fr" if language == "fr" else "cartes anglaises"
            folder_index = {
                "schema_version": "1.0.0", "generation_id": generation_id,
                "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "folder": {"folder_id": f"{edition['edition']['local_id']}-{language}",
                           "kind": "language",
                           "display_name": "Images françaises" if language == "fr" else "Cartes anglaises",
                           "path": f"{folder}/{language_dir}", "language": language},
                "completeness": {"state": "complete", "files_expected": len(assets),
                                 "files_indexed": len(assets), "unresolved": 0, "reason": None},
                "edition_links": [], "assets": sorted(assets, key=lambda asset: asset["path"].casefold()),
            }
            folder_json = f"{folder}/{language_dir}/index.json"
            documents[folder_json] = folder_index
            documents[f"{folder}/{language_dir}/index.md"] = render_folder_markdown(folder_index)
            schema_map[folder_json] = "folder.schema.json"

    exclusions = [{"set_id": row["id"], "reason": row["exclusion_reason"] or "explicitly excluded"}
                  for row in all_sets if row["eligibility"] == "explicitly_excluded"]
    catalogue = project_catalogue_index(edition_indexes, generation_id, as_of,
                                        eligible_set_count=expected_sets,
                                        deferred_set_count=sum(row["eligibility"] == "deferred" for row in all_sets),
                                        explicit_exclusions=exclusions, source_rows=snapshots,
                                        groups=root_groups, selection=scope)
    documents["index.json"] = catalogue
    documents["index.md"] = render_catalogue_markdown(catalogue)
    schema_map["index.json"] = "catalogue.schema.json"
    return publisher, catalogue, documents, artifacts, schema_map


def create_report(config_path: str | Path, scope: str, output: str | Path | None = None,
                  markdown: bool = False) -> tuple[dict, Path]:
    config, ops_root = load_valid_config(config_path)
    root = Path(config["paths"]["staging_root"]) / ("library" if scope == "library" else scope)
    publisher = Publisher(root, ops_root / "schemas", config["paths"]["library_root"],
                          config["publication"]["keep_valid_generations"])
    report = publisher.report()
    if output:
        path = Path(output).resolve()
    else:
        path = ops_root / "reports" / f"report-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.{'md' if markdown else 'json'}"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = render_report_markdown(report) if markdown else json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    return report, path


def update_dry_run(config_path: str | Path, strict_completeness: bool = False) -> dict:
    config, ops_root = load_valid_config(config_path)
    if not (ops_root / "catalogue.sqlite").is_file():
        raise PipelineStorageError(f"Normalized catalogue is missing: {ops_root / 'catalogue.sqlite'}")
    validate_catalogue(ops_root)
    publisher = Publisher(Path(config["paths"]["staging_root"]) / "library", ops_root / "schemas",
                          config["paths"]["library_root"], config["publication"]["keep_valid_generations"])
    try:
        report = publisher.report()
    except (InvalidPublication, OSError):
        report = None
    incomplete = report["editions_incomplete"] if report else None
    log_path = ops_root / "reports" / f"update-preview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    result = {"status": "dry_run", "network_calls": 0, "publication_exists": report is not None,
              "editions_incomplete": incomplete,
              "catalogue": str(ops_root / "catalogue.sqlite"),
              "publication_root": str(publisher.root), "next_action": "refresh catalogue delta and collect only eligible missing faces"}
    result.update(progress={"phase": "local catalogue and publication check", "completed": 1, "total": 1},
                  last_card=None, summary="Prévisualisation locale terminée ; aucun appel réseau ni changement de bibliothèque.",
                  log_path=str(log_path))
    if strict_completeness and (report is None or report["editions_incomplete"]):
        result["status"] = "incomplete"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
