from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from jsonschema import Draft202012Validator, FormatChecker

from .faces import PHYSICAL_MULTIFACE_LAYOUTS, physical_faces, printing_groups_from_catalogue


SCHEMA_VERSION = "1.0.0"
SUPER_TYPES = {"Basic", "Legendary", "Snow", "World", "Ongoing"}
CARD_TYPES = {"Artifact", "Battle", "Conspiracy", "Creature", "Dungeon", "Enchantment",
              "Instant", "Kindred", "Land", "Phenomenon", "Plane", "Planeswalker",
              "Scheme", "Sorcery", "Vanguard"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value, default):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def _slug(value: str, limit: int = 36) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip(" .-_ ")[:limit].rstrip(" .-_ ")
    return normalized or "sans-nom"


def safe_edition_folders(set_rows: list[dict]) -> dict[str, str]:
    by_code = {}
    for row in set_rows:
        by_code.setdefault(str(row["code"]).casefold(), []).append(row)
    folders = {}
    used = set()
    for row in sorted(set_rows, key=lambda r: (str(r["code"]).casefold(), str(r["id"]))):
        code = _slug(row["code"], 16)
        stem = f"{code} - {_slug(row['name'], 36)}"
        if len(by_code[str(row["code"]).casefold()]) > 1:
            stem = f"{stem[:48].rstrip()} - {_slug(str(row['id'])[:8], 8)}"
        reserved = stem.split(".", 1)[0].upper()
        if reserved in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"COM[1-9]|LPT[1-9]", reserved):
            stem = "_" + stem
        folder = stem
        if folder.casefold() in used:
            folder = f"{stem[:52].rstrip()} - {_slug(str(row['id'])[:8], 8)}"
        used.add(folder.casefold())
        folders[str(row["id"])] = folder
    return folders


def source_snapshots(ops_root: str | Path) -> list[dict]:
    ops_root = Path(ops_root).resolve()
    source_path = ops_root / "reports" / "sources.json"
    scope_path = ops_root / "reports" / "catalogue_scope.json"
    if not source_path.is_file() or not scope_path.is_file():
        return []
    sources = json.loads(source_path.read_text(encoding="utf-8-sig"))
    scope = json.loads(scope_path.read_text(encoding="utf-8-sig"))
    retrieved = sources.get("generated_at_utc") or f"{scope.get('as_of_date', date.today().isoformat())}T00:00:00Z"
    snapshots = []
    bulk = sources.get("sources", {}).get("scryfall_all_cards", {})
    bulk_path = Path(bulk.get("archive_path", ""))
    if bulk.get("archive_sha256") and bulk_path.is_file():
        try:
            relative = bulk_path.resolve().relative_to(ops_root).as_posix()
        except ValueError:
            relative = str(bulk_path.resolve())
        snapshots.append({"provider": "scryfall_all_cards", "retrieved_at_utc": retrieved,
                          "sha256": bulk["archive_sha256"], "path": relative})
    sets_source = sources.get("sources", {}).get("scryfall_sets", {})
    for page in sets_source.get("pages", []):
        page_path = Path(page.get("path", ""))
        if not page.get("sha256") or not page_path.is_file():
            continue
        try:
            relative = page_path.resolve().relative_to(ops_root).as_posix()
        except ValueError:
            relative = str(page_path.resolve())
        snapshots.append({"provider": "scryfall_sets", "retrieved_at_utc": retrieved,
                          "sha256": page["sha256"], "path": relative})
    return snapshots


def _split_type_line(type_line: str | None) -> tuple[list[str], list[str], list[str]]:
    if not type_line:
        return [], [], []
    parts = re.split(r"\s+[—\-]\s+", type_line, maxsplit=1)
    left = parts[0].split()
    supertypes = [item for item in left if item in SUPER_TYPES]
    types = [item for item in left if item in CARD_TYPES]
    subtypes = parts[1].split() if len(parts) > 1 else []
    return types, subtypes, supertypes


def _raw_face(record: dict, layout: str, index: int) -> dict:
    raw = _json(record.get("raw_json"), {})
    if layout in PHYSICAL_MULTIFACE_LAYOUTS:
        faces = raw.get("card_faces") or []
        if index < len(faces) and isinstance(faces[index], dict):
            return faces[index]
        return {}
    return raw


def _image_metadata(result: dict | None) -> dict:
    result = result or {}
    attempts = result.get("attempts") or {}
    fr = attempts.get("fr") or {}
    en = attempts.get("en") or {}
    status = result.get("status", "missing")
    if status not in {"ready", "missing", "blocked", "unsupported_language"}:
        status = "missing"
    selected = result.get("selected_language") if status == "ready" else None
    candidate = next((item for item in result.get("candidates", [])
                      if item.get("provider") == result.get("provider")
                      and item.get("language") == selected
                      and item.get("url") == result.get("source_url")), {})
    image = {
        "status": status,
        "path": result.get("path") if status == "ready" else None,
        "selected_language": selected,
        "provider": result.get("provider") if status == "ready" else None,
        "source_url": result.get("source_url") if status == "ready" else None,
        "provider_quality": candidate.get("declared_quality"),
        "source_width": result.get("source_width") if status == "ready" else None,
        "source_height": result.get("source_height") if status == "ready" else None,
        "source_format": result.get("source_format") if status == "ready" else None,
        "source_bytes": result.get("source_bytes") if status == "ready" else None,
        "final_width": result.get("final_width") if status == "ready" else None,
        "final_height": result.get("final_height") if status == "ready" else None,
        "sha256": result.get("output_sha256") if status == "ready" else None,
        "quality": result.get("quality", "missing") if status == "ready" else "missing",
        "choice_reason": result.get("choice_reason"),
        "selected_at_utc": result.get("selected_at_utc") if status == "ready" else None,
        "attempts": {"fr_candidates": int(fr.get("candidates", 0)),
                     "en_candidates": int(en.get("candidates", 0)),
                     "fr_gets": int(fr.get("gets", 0)),
                     "en_gets": int(en.get("gets", 0))},
    }
    if status == "blocked":
        image["choice_reason"] = result.get("choice_reason") or "source_blocked"
    return image


def _face_project(group, face_index: int, image_result: dict | None) -> dict:
    members = group.members
    en_record = members.get("en") or next(iter(members.values()))
    fr_record = members.get("fr")
    en_face = _raw_face(en_record, group.layout, face_index)
    fr_face = _raw_face(fr_record, group.layout, face_index) if fr_record else {}
    type_line = en_face.get("type_line") or fr_face.get("type_line")
    printed_type = (fr_face.get("printed_type_line") or fr_face.get("printed_type")
                    or en_face.get("printed_type_line") or en_face.get("printed_type"))
    types, subtypes, supertypes = _split_type_line(type_line)
    raw_en = _json(en_record.get("raw_json"), {})
    raw_fr = _json(fr_record.get("raw_json"), {}) if fr_record else {}
    colors = en_face.get("colors", raw_en.get("colors", fr_face.get("colors", raw_fr.get("colors", [])))) or []
    color_identity = raw_en.get("color_identity", raw_fr.get("color_identity", [])) or []
    mana_cost = en_face.get("mana_cost")
    if mana_cost is None:
        mana_cost = fr_face.get("mana_cost")
    mana_value = raw_en.get("cmc", raw_fr.get("cmc"))
    oracle_text = en_face.get("oracle_text") or raw_en.get("oracle_text")
    printed_text = fr_face.get("printed_text") or fr_face.get("oracle_text") or raw_fr.get("printed_text")
    flavor_text = en_face.get("flavor_text") or raw_en.get("flavor_text")
    keywords = en_face.get("keywords", raw_en.get("keywords", [])) or []
    names = {
        "en": en_face.get("name") if isinstance(en_face.get("name"), str) else None,
        "fr": fr_face.get("printed_name") if isinstance(fr_face.get("printed_name"), str) else None,
    }
    if group.layout not in PHYSICAL_MULTIFACE_LAYOUTS:
        names["en"] = raw_en.get("name") if isinstance(raw_en.get("name"), str) else names["en"]
        names["fr"] = raw_fr.get("printed_name") if isinstance(raw_fr.get("printed_name"), str) else None
    return {
        "face_index": face_index,
        "is_primary": face_index == 0,
        "names": names,
        "mana_cost": mana_cost if isinstance(mana_cost, str) else None,
        "mana_value": mana_value if isinstance(mana_value, (int, float)) else None,
        "colors": sorted({c for c in colors if c in {"W", "U", "B", "R", "G"}}),
        "color_identity": sorted({c for c in color_identity if c in {"W", "U", "B", "R", "G"}}),
        "types": types,
        "subtypes": subtypes,
        "supertypes": supertypes,
        "type_line": type_line if isinstance(type_line, str) else None,
        "printed_type": printed_type if isinstance(printed_type, str) else None,
        "oracle_text": oracle_text if isinstance(oracle_text, str) else None,
        "printed_text": printed_text if isinstance(printed_text, str) else None,
        "flavor_text": flavor_text if isinstance(flavor_text, str) else None,
        "keywords": [str(item) for item in keywords if isinstance(item, str)],
        "power": str(en_face.get("power")) if en_face.get("power") is not None else None,
        "toughness": str(en_face.get("toughness")) if en_face.get("toughness") is not None else None,
        "loyalty": str(en_face.get("loyalty")) if en_face.get("loyalty") is not None else None,
        "defense": str(en_face.get("defense")) if en_face.get("defense") is not None else None,
        "image": _image_metadata(image_result),
    }


def load_image_results(state_path: str | Path, cycle: str | None = None) -> dict[tuple[str, int], dict]:
    path = Path(state_path)
    if not path.is_file():
        return {}
    conn = sqlite3.connect(path)
    try:
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='image_results'").fetchone()
        if not table:
            return {}
        query = "SELECT task_key,result_json FROM image_results ORDER BY updated_utc"
        args = ()
        if cycle is not None:
            query += " WHERE cycle=?"; args = (cycle,)
        results = {}
        for task_key, value in conn.execute(query, args):
            match = re.fullmatch(r"(.+):face:(\d+)", task_key)
            if not match:
                continue
            try:
                results[(match.group(1), int(match.group(2)))] = json.loads(value)
            except json.JSONDecodeError:
                continue
        return results
    finally:
        conn.close()


def project_edition_index(catalogue_path: str | Path, set_id: str, generation_id: str,
                          as_of_date: str, source_rows: list[dict] | None = None,
                          image_results: dict[tuple[str, int], dict] | None = None,
                          folder_name: str | None = None,
                          local_assets: list[dict] | None = None) -> dict:
    catalogue_path = Path(catalogue_path)
    conn = sqlite3.connect(catalogue_path)
    conn.row_factory = sqlite3.Row
    try:
        set_row = conn.execute("SELECT * FROM sets WHERE id=?", (set_id,)).fetchone()
        if not set_row:
            raise ValueError(f"Unknown set id: {set_id}")
        set_raw = _json(set_row["raw_json"], {})
        groups = printing_groups_from_catalogue(catalogue_path, set_id)
        images = image_results or {}
        provider_to_printing = {}
        for group in groups:
            for provider_id in group.provider_ids.values():
                provider_to_printing[provider_id] = group.printing_id
        cards = []
        gaps = []
        faces_expected = valid_images = 0
        unresolved = 0
        for group in groups:
            face_jobs = physical_faces(group)
            for face in face_jobs:
                faces_expected += 1
                image_result = images.get((group.printing_id, face.face_index))
                image_obj = _image_metadata(image_result)
                valid_images += image_obj["status"] == "ready"
                if image_obj["status"] != "ready":
                    kind = "source_blocked" if image_obj["status"] == "blocked" else "missing_image"
                    gaps.append({"kind": kind, "object_id": f"{group.printing_id}:face:{face.face_index}",
                                 "reason": image_obj["choice_reason"] or "No valid image candidate",
                                 "last_attempt_utc": (image_result or {}).get("selected_at_utc"),
                                 "next_action": "Réessayer au prochain cycle avec le budget restant ou une source validée."})
            first = next(iter(group.members.values()))
            raw = _json(first.get("raw_json"), {})
            raw_fr = _json(group.members.get("fr", {}).get("raw_json"), {})
            frame_effects = raw.get("frame_effects") or ["standard"]
            if isinstance(frame_effects, str):
                frame_effects = [frame_effects]
            finishes = raw.get("finishes") or []
            if isinstance(finishes, str):
                finishes = [finishes]
            variants = [{
                "variant_id": "variant-0",
                "illustration_id": raw.get("illustration_id"),
                "treatments": sorted(str(value) for value in frame_effects),
                "finishes": sorted(str(value) for value in finishes),
                "promo": bool(raw.get("promo") or raw.get("promo_types")),
            }]
            legalities = raw.get("legalities") or raw_fr.get("legalities") or {}
            legalities = {str(key): value if value in {"legal", "not_legal", "restricted", "banned"} else "unknown"
                          for key, value in legalities.items()}
            all_parts = raw.get("all_parts") or raw_fr.get("all_parts") or []
            relationships, seen_relationships = [], set()
            for part in all_parts:
                target = provider_to_printing.get(part.get("id"))
                if target and target != group.printing_id:
                    relationship = (part.get("component") or "related", target)
                    if relationship not in seen_relationships:
                        seen_relationships.add(relationship)
                        relationships.append({"kind": relationship[0], "target_printing_id": target})
            if group.identity_status == "unresolved_identity":
                unresolved += 1
                gaps.append({"kind": "unresolved_identity", "object_id": group.printing_id,
                             "reason": "Plusieurs impressions localisées ne peuvent pas être liées sans ambiguïté.",
                             "last_attempt_utc": None, "next_action": "Garder les sources séparées jusqu’à preuve exacte."})
            local_matches = []
            for asset in local_assets or []:
                if asset.get("printing_id") != group.printing_id:
                    continue
                local_matches.append({"path": asset["path"], "sha256": asset["sha256"],
                                      "method": "; ".join(asset.get("evidence", [])) or "reconciliation locale",
                                      "confidence": "high" if asset.get("classification") == "matched_card" else "unresolved",
                                      "decision_id": None})
            artists = set()
            for record in group.members.values():
                artist = _json(record.get("raw_json"), {}).get("artist")
                if isinstance(artist, str):
                    artists.add(artist)
                elif isinstance(artist, list):
                    artists.update(str(value) for value in artist if isinstance(value, str))
            cards.append({
                "printing_id": group.printing_id,
                "provider_ids": {f"scryfall_{language}": value
                                 for language, value in group.provider_ids.items()},
                "oracle_id": group.oracle_id,
                "collector_number": str(group.collector_number or ""),
                "language_codes": sorted(group.members),
                "layout": group.layout,
                "rarity": raw.get("rarity") or raw_fr.get("rarity"),
                "artist": sorted(artists),
                "variants": variants,
                "faces": [_face_project(group, face.face_index,
                                         images.get((group.printing_id, face.face_index))) for face in face_jobs],
                "legalities": legalities,
                "relationships": relationships,
                "local_matches": local_matches,
            })
        cards.sort(key=lambda card: (card["collector_number"].casefold(), card["printing_id"]))
        expected = set_row["card_count"] if set_row["card_count"] is not None else len(cards)
        metadata_complete = len(cards) == int(expected) and unresolved == 0
        image_complete = valid_images == faces_expected
        release_date = set_row["released_at"]
        try:
            publication_status = "future" if release_date and release_date > as_of_date else "published"
        except TypeError:
            publication_status = "unknown"
        local_id = f"scryfall-set-{set_id}"
        names = {"en": str(set_row["name"])}
        folder = folder_name or f"{_slug(set_row['code'], 16)} - {_slug(set_row['name'], 36)}"
        return {
            "schema_version": SCHEMA_VERSION,
            "generation_id": generation_id,
            "generated_at_utc": utc_now(),
            "source_snapshots": source_rows or [],
            "scope": {"as_of_date": as_of_date, "eligible": set_row["eligibility"] == "eligible",
                      "reason": set_row["exclusion_reason"]},
            "edition": {"local_id": local_id, "provider_ids": {"scryfall": set_id},
                        "code": str(set_row["code"]), "names": names,
                        "release_date": release_date, "category": str(set_row["set_type"]),
                        "digital": bool(set_row["digital"]), "paper": not bool(set_row["digital"]),
                        "publication_status": publication_status, "parent_local_id": None,
                        "folder": folder},
            "completeness": {"metadata_state": "complete" if metadata_complete else "incomplete",
                             "image_state": "complete" if image_complete else "incomplete",
                             "cards_expected": int(expected), "cards_indexed": len(cards),
                             "faces_expected": faces_expected, "faces_with_valid_image": valid_images,
                             "missing_metadata": max(0, int(expected) - len(cards)),
                             "missing_images": faces_expected - valid_images,
                             "unresolved_identities": unresolved},
            "cards": cards,
            "gaps": gaps,
            "local_assets": local_assets or [],
        }
    finally:
        conn.close()


def project_catalogue_index(edition_indexes: list[dict], generation_id: str,
                            as_of_date: str, eligible_set_count: int | None = None,
                            deferred_set_count: int = 0,
                            explicit_exclusions: list[dict] | None = None,
                            source_rows: list[dict] | None = None,
                            groups: list[dict] | None = None,
                            selection: str = "selected") -> dict:
    editions = []
    cards_expected = cards_indexed = faces_expected = faces_valid = unresolved = 0
    all_gaps = []
    for index in edition_indexes:
        meta = index["edition"]
        folder = meta["folder"]
        editions.append({"local_id": meta["local_id"], "code": meta["code"],
                         "names": meta["names"], "release_date": meta["release_date"],
                         "category": meta["category"], "index_json": f"{folder}/index.json",
                         "index_md": f"{folder}/index.md",
                         "metadata_state": index["completeness"]["metadata_state"],
                         "image_state": index["completeness"]["image_state"]})
        counts = index["completeness"]
        cards_expected += counts["cards_expected"]
        cards_indexed += counts["cards_indexed"]
        faces_expected += counts["faces_expected"]
        faces_valid += counts["faces_with_valid_image"]
        unresolved += counts["unresolved_identities"]
        for gap in index.get("gaps", []):
            all_gaps.append({key: gap[key] for key in ("kind", "object_id", "reason", "next_action")})
    expected_sets = len(edition_indexes) if eligible_set_count is None else eligible_set_count
    metadata_state = "complete" if len(edition_indexes) == expected_sets and all(
        item["metadata_state"] == "complete" for item in editions) else "incomplete"
    image_state = "complete" if len(edition_indexes) == expected_sets and all(
        item["image_state"] == "complete" for item in editions) else "incomplete"
    return {"schema_version": SCHEMA_VERSION, "generation_id": generation_id,
            "generated_at_utc": utc_now(), "source_snapshots": source_rows or [],
            "scope": {"as_of_date": as_of_date, "selection": selection,
                      "eligible_set_count": expected_sets,
                      "deferred_set_count": deferred_set_count,
                      "explicit_exclusions": explicit_exclusions or []},
            "completeness": {"metadata_state": metadata_state, "image_state": image_state,
                             "editions_expected": expected_sets, "editions_indexed": len(editions),
                             "cards_expected": cards_expected, "cards_indexed": cards_indexed,
                             "faces_expected": faces_expected, "faces_with_valid_images": faces_valid,
                             "unresolved_tasks": len(all_gaps)},
            "editions": editions, "groups": groups or [], "gaps": all_gaps}


def render_edition_markdown(index: dict) -> str:
    edition = index["edition"]
    names = edition["names"]
    title = names.get("fr") or names.get("en") or edition["code"]
    out = [f"# {title}", "", f"**Édition :** {edition['code']}  ",
           f"**Nom EN :** {names.get('en', '')}  ",
           f"**Sortie :** {edition.get('release_date') or 'inconnue'}  ",
           f"**Génération :** `{index['generation_id']}`", "",
           "## Complétude", "",
           f"- Cartes indexées : {index['completeness']['cards_indexed']} / {index['completeness']['cards_expected']}",
           f"- Faces avec image valide : {index['completeness']['faces_with_valid_image']} / {index['completeness']['faces_expected']}",
           f"- État images : {index['completeness']['image_state']}", "", "## Cartes", ""]
    for card in index["cards"]:
        anchor = "card-" + re.sub(r"[^A-Za-z0-9_-]", "-", card["printing_id"])
        face0 = card["faces"][0]
        card_name = face0["names"].get("fr") or face0["names"].get("en") or card["printing_id"]
        out.extend([f'<a id="{anchor}"></a>', f"### {card['collector_number']} — {card_name}",
                    f"", f"- Identifiant d’impression : `{card['printing_id']}`",
                    f"- Type : {face0.get('printed_type') or face0.get('type_line') or 'inconnu'}",
                    f"- Coût : {face0.get('mana_cost') or '—'} ; valeur : {face0.get('mana_value') if face0.get('mana_value') is not None else 'inconnue'}"])
        for face in card["faces"]:
            image = face["image"]
            out.append(f"- Face {face['face_index']} : EN {face['names'].get('en') or '—'} ; FR {face['names'].get('fr') or '—'}")
            if face.get("printed_text"):
                out.append(f"  - Texte imprimé : {face['printed_text'].replace(chr(10), ' ')}")
            if face.get("oracle_text"):
                out.append(f"  - Texte Oracle : {face['oracle_text'].replace(chr(10), ' ')}")
            if image.get("status") == "ready" and image.get("path"):
                out.append(f"  - Image {image['selected_language']} : [{image['path']}]({quote(image['path'], safe='/._-')})")
            else:
                out.append("  - Image : manquante")
        out.append("")
    out.extend(["## Lacunes", ""])
    if index["gaps"]:
        for gap in index["gaps"]:
            out.append(f"- `{gap['kind']}` — `{gap['object_id']}` : {gap['reason']} (suite : {gap['next_action']})")
    else:
        out.append("Aucune.")
    out.append("")
    return "\n".join(out)


def render_catalogue_markdown(index: dict) -> str:
    out = ["# Catalogue de la bibliothèque Magic", "",
           f"**Génération :** `{index['generation_id']}`",
           f"**Sélection :** `{index['scope'].get('selection', 'selected')}`", "",
           f"Éditions : {index['completeness']['editions_indexed']} / {index['completeness']['editions_expected']}",
           "", "## Éditions", ""]
    for edition in index["editions"]:
        name = edition["names"].get("fr") or edition["names"].get("en") or edition["code"]
        out.append(f"- [{edition['code']} — {name}]({quote(edition['index_md'], safe='/._-')}) — "
                   f"métadonnées {edition['metadata_state']}, images {edition['image_state']}")
    out += ["", "## Lacunes", ""]
    out += ([f"- `{gap['kind']}` `{gap['object_id']}` : {gap['reason']}" for gap in index["gaps"]]
            or ["Aucune."])
    out.append("")
    return "\n".join(out)


def render_folder_markdown(index: dict) -> str:
    folder = index["folder"]
    out = [f"# {folder['display_name']}", "", f"**Génération :** `{index['generation_id']}`",
           f"**État d’indexation :** {index['completeness']['state']}",
           f"**Motif :** {index['completeness'].get('reason') or 'aucun'}", "",
           f"Fichiers : {index['completeness']['files_indexed']} / {index['completeness']['files_expected']}", "",
           "## Éditions liées", ""]
    out += ([f"- [{link['code']}]({quote(link['index_md'], safe='/._-')})" for link in index["edition_links"]]
            or ["Aucune."])
    out += ["", "## Fichiers originaux", ""]
    for asset in index["assets"]:
        link = quote(asset["path"], safe="/._-")
        out.append(f"- [{asset['path']}]({link}) — {asset['kind']}; identité {asset['identity_status']}; "
                   f"langue {asset['language'] or 'inconnue'}; SHA-256 `{asset['sha256']}`")
    out.append("")
    return "\n".join(out)


def validate_json_document(value: dict, schema_path: str | Path) -> list[str]:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8-sig"))
    Draft202012Validator.check_schema(schema)
    errors = Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value)
    return [f"{'/'.join(map(str, error.absolute_path))}: {error.message}" for error in errors]


def check_recomputed_counts(index: dict) -> list[str]:
    errors = []
    cards = index.get("cards", [])
    expected_faces = sum(len(card.get("faces", [])) for card in cards)
    ready_faces = sum(face.get("image", {}).get("status") == "ready"
                      for card in cards for face in card.get("faces", []))
    missing_images = sum(face.get("image", {}).get("status") != "ready"
                         for card in cards for face in card.get("faces", []))
    if index["completeness"]["cards_indexed"] != len(cards):
        errors.append("cards_indexed differs from cards array length")
    if index["completeness"]["faces_expected"] != expected_faces:
        errors.append("faces_expected differs from cards' faces")
    if index["completeness"]["faces_with_valid_image"] != ready_faces:
        errors.append("faces_with_valid_image differs from ready image count")
    if index["completeness"]["missing_images"] != missing_images:
        errors.append("missing_images differs from non-ready image count")
    return errors


def markdown_link_targets(markdown: str) -> list[str]:
    return re.findall(r"\]\(([^)]+)\)", markdown)
