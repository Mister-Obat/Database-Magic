from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from PIL import Image, UnidentifiedImageError


class InventoryError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normal(value: str) -> str:
    value = value.casefold().replace("æ", "ae").replace("œ", "oe")
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def _folder_label(folder: str) -> str:
    label = re.sub(r"^\s*\d+\s+", "", folder)
    return re.sub(r"\s*\(\d{4}\)\s*$", "", label).strip()


def _image_info(path: Path) -> dict:
    with Image.open(path) as image:
        image_format = image.format
        width, height = image.size
        mode = image.mode
        image.verify()
    with Image.open(path) as image:
        image.load()
    short, long = sorted((width, height))
    ratio = short / long if long else 0
    portrait_card = 0.60 <= ratio <= 0.82
    landscape_card = 0.60 <= ratio <= 0.82 and width > height
    # A rotated card keeps the same aspect ratio; orientation is recorded separately.
    card_like = portrait_card or landscape_card
    category = "image_card_like" if card_like else "image_review_non_card_shape"
    return {"format": image_format, "width": width, "height": height, "mode": mode,
            "card_like_shape": card_like, "category": category}


def inventory_library(library_root: Path, catalogue_path: Path, gallery_path: Path,
                      report_path: Path) -> dict:
    root = library_root.resolve()
    if not root.is_dir():
        raise InventoryError(f"Library root does not exist: {root}")
    rows = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(current) / d).is_symlink()
                         and not (Path(current) == root and d.casefold() == "impossible"))
        for filename in sorted(files):
            path = Path(current) / filename
            relative = path.relative_to(root).as_posix()
            suffix = path.suffix.casefold()
            row = {"path": relative, "size_bytes": path.stat().st_size,
                   "sha256": _sha256(path), "extension": suffix or None,
                   "category": None, "image": None}
            if path.is_symlink():
                row["category"] = "symlink_review"
            else:
                try:
                    row["image"] = _image_info(path)
                    row["category"] = row["image"]["category"]
                except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
                    if suffix == ".ini":
                        row["category"] = "sidecar_ini"
                    elif suffix == ".db":
                        row["category"] = "sidecar_database"
                    elif suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
                        if _all_zero_bytes(path):
                            row["category"] = "corrupt_zero_payload"
                            row["image_error"] = "AllZeroPayload: image file contains only null bytes"
                        else:
                            row["category"] = "corrupt_or_unsupported_image"
                            row["image_error"] = f"{type(exc).__name__}: {exc}"
                    else:
                        row["category"] = "non_image_file"
            row["folder"] = relative.split("/", 1)[0] if "/" in relative else ""
            rows.append(row)
    rows.sort(key=lambda item: item["path"].casefold())
    paths_by_sha = defaultdict(list)
    for row in rows:
        paths_by_sha[row["sha256"]].append(row["path"])
    duplicate_groups = {digest: paths for digest, paths in paths_by_sha.items() if len(paths) > 1}
    for row in rows:
        duplicates = duplicate_groups.get(row["sha256"])
        if duplicates:
            row["duplicate_count"] = len(duplicates)
            row["duplicate_paths"] = duplicates
    counts = Counter(row["category"] for row in rows)
    aliases = build_edition_aliases(root, catalogue_path)
    gallery = json.loads(gallery_path.read_text(encoding="utf-8"))
    assets = []
    for article in gallery.get("sample_articles", []):
        for asset in article.get("assets", []):
            assets.append({**asset, "article_url": article.get("url"),
                           "article_title": article.get("title")})
    by_name = defaultdict(list)
    for asset in assets:
        by_name[asset.get("filename", "").casefold()].append(asset)
    file_by_path = {row["path"]: row for row in rows}
    origin_files = []
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        for candidate in folder.iterdir():
            if candidate.is_file() and candidate.name.casefold() in by_name:
                origin_files.append(candidate.relative_to(root).as_posix())
    origin_matches = []
    for relative in sorted(origin_files, key=str.casefold):
        source_assets = by_name[Path(relative).name.casefold()]
        origin_matches.append({"path": relative, "sha256": file_by_path[relative]["sha256"],
                               "asset_count": len(source_assets),
                               "asset_filenames": sorted({a.get("filename") for a in source_assets})})
    report = {"schema_version": "1.0.0", "library_root": str(root),
              "file_count": len(rows), "category_counts": dict(sorted(counts.items())),
              "files": rows, "gallery_asset_count": len(assets),
              "gallery_local_filename_matches": origin_matches,
              "gallery_local_filename_match_count": len(origin_matches),
              "duplicate_content_group_count": len(duplicate_groups),
              "duplicate_content_file_count": sum(len(paths) for paths in duplicate_groups.values()),
              "edition_aliases": aliases,
              "read_only": True}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(report_path, report)
    alias_path = report_path.parent / "edition_aliases.json"
    _atomic_json(alias_path, {"schema_version": "1.0.0", "aliases": aliases})
    return {"file_count": len(rows), "category_counts": report["category_counts"],
            "gallery_asset_count": len(assets),
            "gallery_local_filename_match_count": len(origin_matches),
            "duplicate_content_group_count": len(duplicate_groups),
            "duplicate_content_file_count": sum(len(paths) for paths in duplicate_groups.values()),
            "inventory_report": str(report_path), "edition_aliases_report": str(alias_path)}


def _all_zero_bytes(path: Path) -> bool:
    found_nonzero = False
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if any(block):
                found_nonzero = True
                break
    return not found_nonzero


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".part")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def build_edition_aliases(library_root: Path, catalogue_path: Path) -> list[dict]:
    conn = sqlite3.connect(catalogue_path)
    try:
        sets = conn.execute("SELECT id, code, name, set_type, released_at FROM sets ORDER BY code").fetchall()
    finally:
        conn.close()
    exact_by_name = defaultdict(list)
    by_code = defaultdict(list)
    for row in sets:
        exact_by_name[_normal(row[2])].append(row)
        by_code[row[1].casefold()].append(row)
    aliases = []
    explicit_candidates = {
        "horizons du modern 3": ("mh3", "translated folder title; candidate only"),
        "la lune hermetique": ("emn", "French-title candidate; confirmation required"),
    }
    for folder in sorted((p.name for p in library_root.iterdir()
                          if p.is_dir() and p.name.casefold() != "impossible"), key=str.casefold):
        label = _folder_label(folder)
        normalized = _normal(label)
        candidates = []
        evidence = []
        code_match = re.search(r"\bcore m(\d+)\b", normalized)
        if code_match:
            code = "m" + code_match.group(1)
            candidates = list(by_code.get(code, []))
            evidence.append("explicit Core set code in folder label; year ignored")
        elif normalized in explicit_candidates:
            code, reason = explicit_candidates[normalized]
            candidates = list(by_code.get(code, []))
            evidence.append(reason)
        elif exact_by_name.get(normalized):
            candidates = list(exact_by_name[normalized])
            evidence.append("normalized exact match to catalogue set name; year ignored")
        elif normalized == "lotr" or "lord of the rings" in normalized:
            candidates = [s for s in sets if "lord of the rings" in _normal(s[2])]
            evidence.append("broad franchise label; deliberately not reduced to one product")
        elif "kamigawa" in normalized:
            candidates = [s for s in sets if "kamigawa" in _normal(s[2])]
            evidence.append("broad world label; multiple Kamigawa editions remain candidates")
        elif "warhammer" in normalized:
            candidates = [s for s in sets if "warhammer" in _normal(s[2])]
            evidence.append("product family label; candidate set not inferred from year")
        elif "phyrexian" in normalized:
            candidates = [s for s in sets if "phyrexia" in _normal(s[2])]
            evidence.append("theme label; candidate editions not inferred from folder year")
        candidate_rows = [{"set_id": row[0], "set_code": row[1], "set_name": row[2],
                           "set_type": row[3], "released_at": row[4]} for row in candidates]
        if len(candidate_rows) == 1 and evidence and "confirmation required" not in evidence[0] and "candidate only" not in evidence[0]:
            status = "confirmed_exact_candidate"
        elif len(candidate_rows) > 1:
            status = "ambiguous_candidates"
        elif len(candidate_rows) == 1:
            status = "candidate_requires_confirmation"
        else:
            status = "unresolved_identity"
        aliases.append({"folder": folder, "normalized_label": normalized, "status": status,
                        "candidates": candidate_rows, "evidence": evidence,
                        "year_used_as_identity": False})
    return aliases


def _candidate_for_asset(conn: sqlite3.Connection, asset: dict, set_code: str,
                         record_cache: dict | None = None) -> list[dict]:
    printed = _normal(asset.get("printed_name") or "")
    language = (asset.get("language") or "unknown").casefold()
    if not printed or language not in ("fr", "en"):
        return []
    cache_key = (set_code.casefold(), language)
    if record_cache is not None and cache_key in record_cache:
        records = record_cache[cache_key]
    else:
        set_ids = [row[0] for row in conn.execute("SELECT id FROM sets WHERE lower(code)=?", (set_code.casefold(),))]
        card_columns = {row[1] for row in conn.execute("PRAGMA table_info(cards)")}
        raw_column = ", raw_json" if "raw_json" in card_columns else ""
        records = []
        for set_id in set_ids:
            records.extend(conn.execute(
                "SELECT id, name, layout, faces_json" + raw_column + " FROM cards WHERE set_id=? AND lang=?",
                (set_id, language)).fetchall())
        if record_cache is not None:
            record_cache[cache_key] = records
    matches = []
    for record in records:
        card_id, name, layout, faces_json = record[:4]
        raw_json = record[4] if len(record) > 4 else None
        raw = None
        try:
            raw = json.loads(raw_json) if raw_json else None
        except (TypeError, json.JSONDecodeError):
            raw = None
        try:
            faces = json.loads(faces_json) if faces_json else []
        except (TypeError, json.JSONDecodeError):
            faces = []
        if isinstance(raw, dict) and isinstance(raw.get("card_faces"), list):
            faces = raw["card_faces"]
        if isinstance(raw, dict):
            top_name = raw.get("printed_name") if language == "fr" else (raw.get("printed_name") or raw.get("name"))
            if top_name and _normal(top_name) == printed:
                matches.append({"target_id": card_id, "face_index": None,
                                "layout": layout, "matched_name": top_name})
            for face_index, face in enumerate(faces):
                face_name = face.get("printed_name") if language == "fr" else (face.get("printed_name") or face.get("name"))
                if face_name and _normal(face_name) == printed:
                    matches.append({"target_id": card_id, "face_index": face_index,
                                    "layout": layout, "matched_name": face_name})
        elif faces:
            for face_index, face in enumerate(faces):
                face_name = face.get("printed_name") or face.get("name")
                if face_name and _normal(face_name) == printed:
                    matches.append({"target_id": card_id, "face_index": face_index,
                                    "layout": layout, "matched_name": face_name})
        elif (language == "en" or raw is None) and _normal(name or "") == printed:
            matches.append({"target_id": card_id, "face_index": None,
                            "layout": layout, "matched_name": name})
    unique = {(item["target_id"], item["face_index"]): item for item in matches}
    return list(unique.values())


def reconcile_sample(library_root: Path, catalogue_path: Path, gallery_path: Path,
                     aliases_path: Path, sample_path: Path, overrides_path: Path,
                     report_path: Path, dry_run: bool = True,
                     inventory_path: Path | None = None) -> dict:
    if not dry_run:
        raise InventoryError("Step 4 reconciliation is read-only; use --dry-run")
    root = library_root.resolve()
    inventory_source = Path(inventory_path) if inventory_path else aliases_path.with_name("inventory.json")
    inventory = json.loads(inventory_source.read_text(encoding="utf-8"))
    alias_map = {row["folder"]: row for row in inventory.get("edition_aliases", [])}
    files = {row["path"]: row for row in inventory.get("files", [])}
    gallery = json.loads(gallery_path.read_text(encoding="utf-8"))
    assets_by_name = defaultdict(list)
    for article in gallery.get("sample_articles", []):
        for asset in article.get("assets", []):
            assets_by_name[asset.get("filename", "").casefold()].append({**asset, "article_url": article.get("url")})
    overrides_doc = {"schema_version": "1.0.0", "overrides": []}
    if overrides_path.exists():
        overrides_doc = json.loads(overrides_path.read_text(encoding="utf-8"))
    override_rows = overrides_doc.get("overrides", [])
    conn = sqlite3.connect(catalogue_path)
    try:
        valid_ids = {row[0] for row in conn.execute("SELECT id FROM cards")}
        sample = json.loads(sample_path.read_text(encoding="utf-8"))
        results = []
        record_cache = {}
        for item in sample.get("files", []):
            relative = Path(item["path"]).as_posix()
            row = files.get(relative)
            if row is None:
                results.append({"path": relative, "status": "missing_from_inventory",
                                "expected_status": item.get("expected_status")})
                continue
            result = {"path": relative, "sha256": row["sha256"], "file_category": row["category"],
                      "status": "unresolved_identity", "match_method": None, "target_id": None,
                      "face_index": None, "candidate_count": 0}
            try:
                current_path = (root / Path(relative)).resolve(strict=True)
            except OSError:
                result.update(status="missing_file", match_method="sample_path_missing")
                results.append(result)
                continue
            if not current_path.is_relative_to(root):
                result.update(status="unsafe_path_rejected", match_method="path_escapes_library_root")
                results.append(result)
                continue
            if not current_path.is_file():
                result.update(status="missing_file", match_method="sample_path_not_a_file")
                results.append(result)
                continue
            current_size = current_path.stat().st_size
            current_sha = _sha256(current_path)
            if current_sha != row["sha256"] or current_size != row["size_bytes"]:
                result.update(status="inventory_stale", match_method="file_changed_since_audit",
                              current_size_bytes=current_size, current_sha256=current_sha)
                results.append(result)
                continue
            if row["category"] == "image_review_non_card_shape":
                result["status"] = "non_card_review"
            elif row["category"] != "image_card_like":
                result["status"] = row["category"]
            else:
                matching_overrides = [o for o in override_rows if Path(o.get("file", "")).as_posix() == relative]
                if matching_overrides:
                    override = matching_overrides[-1]
                    if override.get("sha256") != row["sha256"]:
                        result.update(status="stale_override_rejected", match_method="override_hash_mismatch")
                    elif override.get("target_id") not in valid_ids or not override.get("evidence") or not override.get("date"):
                        result.update(status="invalid_override_rejected", match_method="override_validation")
                    else:
                        result.update(status="matched", target_id=override["target_id"],
                                      face_index=override.get("face_index"), match_method="manual_override",
                                      evidence={"language": override.get("language"),
                                                "set_code": override.get("set_code"),
                                                "manual_evidence": override.get("evidence"),
                                                "image_url": None})
                else:
                    basename = Path(relative).name.casefold()
                    matched_assets = assets_by_name.get(basename, [])
                    folder = relative.split("/", 1)[0]
                    alias = alias_map.get(folder, {})
                    if len(matched_assets) == 1 and alias.get("status") == "confirmed_exact_candidate" and len(alias.get("candidates", [])) == 1:
                        set_code = alias["candidates"][0]["set_code"]
                        candidates = _candidate_for_asset(conn, matched_assets[0], set_code, record_cache)
                        result["candidate_count"] = len(candidates)
                        result["evidence"] = {"gallery_filename_exact": matched_assets[0].get("filename"),
                                               "printed_name_exact": matched_assets[0].get("printed_name"),
                                               "language": matched_assets[0].get("language"),
                                               "image_url": matched_assets[0].get("url"),
                                               "set_code": set_code, "article_url": matched_assets[0].get("article_url")}
                        if len(candidates) == 1:
                            result.update(status="matched", target_id=candidates[0]["target_id"],
                                          face_index=candidates[0]["face_index"],
                                          target_layout=candidates[0]["layout"],
                                          match_method="exact_gallery_filename_name_language_set")
                        else:
                            reason = ("multiple_catalogue_printings_share_name" if len(candidates) > 1
                                      else "gallery_name_not_present_as_localized_catalogue_print")
                            result.update(status="unresolved_identity", match_method=reason)
                    else:
                        result["candidate_count"] = len(matched_assets)
                        result["match_method"] = "no_unique_exact_gallery_context"
            expected_status = item.get("expected_status")
            expected_target = item.get("expected_target_id")
            expected_face = item.get("expected_face_index")
            result["expected_status"] = expected_status
            result["expected_target_id"] = expected_target
            result["expected_face_index"] = expected_face
            result["truth_pass"] = (result["status"] == expected_status and
                                    (expected_target is None or result.get("target_id") == expected_target) and
                                    (expected_face is None or result.get("face_index") == expected_face)) if expected_status else None
            results.append(result)
    finally:
        conn.close()
    truth = [row for row in results if row.get("truth_pass") is not None]
    report = {"schema_version": "1.0.0", "read_only": True, "dry_run": dry_run,
              "sample_path": str(sample_path), "sample_count": len(results),
              "matched_count": sum(row["status"] == "matched" for row in results),
              "unresolved_count": sum(row["status"] == "unresolved_identity" for row in results),
              "truth_checks": len(truth), "truth_passed": sum(bool(row["truth_pass"]) for row in truth),
              "truth_failed": sum(row.get("truth_pass") is False for row in truth),
              "status_counts": dict(sorted(Counter(row["status"] for row in results).items())),
              "results": results}
    _atomic_json(report_path, report)
    return {k: v for k, v in report.items() if k != "results"}


def reconcile_all_existing(library_root: Path, catalogue_path: Path, gallery_path: Path,
                           aliases_path: Path, inventory_path: Path, overrides_path: Path,
                           report_path: Path, dry_run: bool = True) -> dict:
    """Reconcile every file in a saved audit without changing the library originals."""
    inventory_path = Path(inventory_path)
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryError(f"Cannot load production inventory {inventory_path}: {exc}") from exc
    if not isinstance(inventory.get("files"), list):
        raise InventoryError("Production inventory has no files list")
    with tempfile.TemporaryDirectory(prefix="magic-reconcile-") as temp_dir:
        sample_path = Path(temp_dir) / "all-existing.json"
        sample_path.write_text(json.dumps({"files": [{"path": row["path"]}
                                                       for row in inventory["files"]]},
                                          ensure_ascii=False), encoding="utf-8")
        result = reconcile_sample(library_root, catalogue_path, gallery_path, aliases_path,
                                  sample_path, overrides_path, report_path, dry_run=True,
                                  inventory_path=inventory_path)
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    report["scope"] = "all_existing"
    report["dry_run"] = bool(dry_run)
    report["sample_path"] = str(inventory_path.resolve())
    report["inventory_sha256"] = _sha256(inventory_path)
    _atomic_json(Path(report_path), report)
    result.update(scope="all_existing", dry_run=bool(dry_run),
                  inventory_path=str(inventory_path.resolve()),
                  sample_path=str(inventory_path.resolve()),
                  inventory_sha256=report["inventory_sha256"],
                  report_path=str(Path(report_path).resolve()))
    return result

