"""Build a published snapshot by replacing only the editions changed by Scryfall."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import date
from pathlib import Path

from ..sources import _load_config, build_catalogue_db
from .build_catalogue import _search_field, build_catalogue


def _link_or_copy(source: str, target: str) -> str:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return target


def _copy_active_database(source: Path, target: Path, progress=None) -> None:
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as old:
        with sqlite3.connect(target) as new:
            pages = old.execute("PRAGMA page_count").fetchone()[0]

            def copied(_status, remaining, total):
                if progress and total:
                    progress(0.12 + 0.30 * (total - remaining) / total,
                             f"Copie SQLite : {total - remaining} / {total} pages")

            old.backup(new, pages=2048, progress=copied, sleep=0.05)


def _upgrade_source_search(conn: sqlite3.Connection, progress=None) -> None:
    old_columns = [row[1] for row in conn.execute("PRAGMA main.table_info(source_search)")]
    if "artist" in old_columns:
        return
    if progress:
        progress(0.55, "Mise à niveau de l’index de recherche…")

    # The delta was just built with the current schema. Reuse its FTS definition
    # and preserve the indexed text of editions outside this update.
    definition = conn.execute(
        "SELECT sql FROM delta.sqlite_schema WHERE name='source_search'").fetchone()[0]
    conn.execute("ALTER TABLE main.source_search RENAME TO previous_source_search")
    conn.execute(definition)
    conn.create_function("search_artist", 1, lambda raw: _search_field(json.loads(raw), "artist"))
    conn.execute(
        """INSERT INTO main.source_search(
               provider_card_id,set_code,lang,name,printed_name,type_line,
               printed_type_line,oracle_text,printed_text,artist)
           SELECT f.provider_card_id,f.set_code,f.lang,f.name,f.printed_name,f.type_line,
                  f.printed_type_line,f.oracle_text,f.printed_text,search_artist(s.raw_json)
           FROM main.previous_source_search f
           JOIN main.source_records s USING(provider_card_id)""")
    conn.execute("DROP TABLE main.previous_source_search")
    conn.execute("DROP TABLE IF EXISTS main.artist_search")


def _merge_edition_rows(target: Path, delta: Path, set_ids: list[str], source_sha: str,
                        source_as_of: str, progress=None) -> None:
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ATTACH DATABASE ? AS delta", (str(delta),))
        conn.execute("CREATE TABLE IF NOT EXISTS printing_added (printing_id TEXT PRIMARY KEY REFERENCES printings(printing_id), added_at TEXT NOT NULL) WITHOUT ROWID")
        baseline = date.today().isoformat() + "T00:00:00.000Z"
        conn.execute("INSERT OR IGNORE INTO printing_added SELECT printing_id,? FROM printings", (baseline,))
        conn.execute("CREATE TEMP TABLE changed_sets(set_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO changed_sets VALUES(?)", [(item,) for item in set_ids])
        conn.execute("CREATE TEMP TABLE old_ready AS SELECT s.* FROM image_slots s JOIN printings p USING(printing_id) "
                     "WHERE p.set_id IN (SELECT set_id FROM changed_sets) AND s.status='ready'")
        conn.execute("CREATE UNIQUE INDEX temp.old_ready_slot ON old_ready(printing_id,face_index)")
        conn.execute("CREATE TEMP TABLE touched_oracles(oracle_id TEXT PRIMARY KEY)")
        conn.execute("INSERT OR IGNORE INTO touched_oracles SELECT oracle_id FROM printings "
                     "WHERE set_id IN (SELECT set_id FROM changed_sets) AND oracle_id IS NOT NULL")
        conn.execute("INSERT OR IGNORE INTO touched_oracles SELECT oracle_id FROM delta.printings WHERE oracle_id IS NOT NULL")
        printing_ids = "SELECT printing_id FROM printings WHERE set_id IN (SELECT set_id FROM changed_sets)"
        provider_ids = f"SELECT provider_card_id FROM source_records WHERE printing_id IN ({printing_ids})"
        conn.execute(f"DELETE FROM source_search WHERE provider_card_id IN ({provider_ids})")
        conn.execute(f"DELETE FROM identity_exceptions WHERE provider_card_id IN ({provider_ids})")
        conn.execute(f"DELETE FROM printing_languages WHERE printing_id IN ({printing_ids})")
        conn.execute(f"DELETE FROM source_records WHERE printing_id IN ({printing_ids})")
        conn.execute(f"DELETE FROM image_slots WHERE printing_id IN ({printing_ids})")
        conn.execute(f"DELETE FROM card_faces WHERE printing_id IN ({printing_ids})")
        conn.execute("DELETE FROM printings WHERE set_id IN (SELECT set_id FROM changed_sets)")
        conn.execute("DELETE FROM editions WHERE set_id IN (SELECT set_id FROM changed_sets)")
        for table in ("editions", "oracle_cards", "printings", "source_records", "printing_languages",
                      "card_faces", "image_slots", "identity_exceptions"):
            clause = "OR IGNORE " if table == "oracle_cards" else ""
            conn.execute(f"INSERT {clause}INTO {table} SELECT * FROM delta.{table}")
        _upgrade_source_search(conn, progress)
        conn.execute(
            """INSERT INTO main.source_search(
                   provider_card_id,set_code,lang,name,printed_name,type_line,
                   printed_type_line,oracle_text,printed_text,artist)
               SELECT provider_card_id,set_code,lang,name,printed_name,type_line,
                      printed_type_line,oracle_text,printed_text,artist
               FROM delta.source_search""")
        conn.execute("INSERT OR IGNORE INTO printing_added SELECT printing_id,added_at FROM delta.printing_added")
        conn.execute("DELETE FROM printing_added WHERE printing_id NOT IN (SELECT printing_id FROM printings)")
        conn.execute("UPDATE image_slots SET selected_language=(SELECT selected_language FROM old_ready r "
                     "WHERE r.printing_id=image_slots.printing_id AND r.face_index=image_slots.face_index), "
                     "status='ready', asset_id=(SELECT asset_id FROM old_ready r WHERE r.printing_id=image_slots.printing_id "
                     "AND r.face_index=image_slots.face_index), provenance_json=(SELECT provenance_json FROM old_ready r "
                     "WHERE r.printing_id=image_slots.printing_id AND r.face_index=image_slots.face_index) "
                     "WHERE EXISTS(SELECT 1 FROM old_ready r WHERE r.printing_id=image_slots.printing_id "
                     "AND r.face_index=image_slots.face_index)")
        conn.execute("UPDATE legacy_aliases SET v2_printing_id=NULL,status='unmatched' "
                     "WHERE v2_printing_id IS NOT NULL AND NOT EXISTS "
                     "(SELECT 1 FROM printings p WHERE p.printing_id=legacy_aliases.v2_printing_id)")
        conn.execute("UPDATE oracle_cards SET source_record_count=(SELECT COUNT(*) FROM source_records sr "
                     "JOIN printings p USING(printing_id) WHERE p.oracle_id=oracle_cards.oracle_id) "
                     "WHERE oracle_id IN (SELECT oracle_id FROM touched_oracles)")
        conn.execute("DELETE FROM oracle_cards WHERE source_record_count=0")
        conn.execute("DELETE FROM catalogue_meta WHERE key='source_database_sha256'")
        values = {"source_archive_sha256": source_sha, "source_as_of": source_as_of,
                  "source_strategy": "edition_delta"}
        for key, table in (("source_records", "source_records"), ("printings", "printings"),
                           ("identity_exceptions", "identity_exceptions"), ("faces", "card_faces")):
            values[key] = str(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        values["eligible_editions"] = str(conn.execute(
            "SELECT COUNT(*) FROM editions WHERE eligibility='eligible'").fetchone()[0])
        values["deferred_editions"] = str(conn.execute(
            "SELECT COUNT(*) FROM editions WHERE eligibility='deferred'").fetchone()[0])
        conn.executemany("INSERT OR REPLACE INTO catalogue_meta VALUES(?,?)", values.items())
        conn.commit()
        if progress:
            progress(0.80, "Éditions ciblées intégrées")
        broken = conn.execute("PRAGMA foreign_key_check").fetchone()
        if broken:
            raise sqlite3.IntegrityError(f"foreign key check failed: {broken}")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise sqlite3.IntegrityError("SQLite quick_check failed")
        if progress:
            progress(0.92, "Base SQLite vérifiée")
    finally:
        conn.close()


def _merge_exports(old_root: Path, delta_root: Path, target_root: Path,
                   source_sha: str, source_as_of: str) -> None:
    shutil.copytree(old_root, target_root, copy_function=_link_or_copy)
    old_doc = json.loads((old_root / "index.json").read_text(encoding="utf-8"))
    delta_doc = json.loads((delta_root / "index.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in old_doc["editions"]}
    for item in delta_doc["editions"]:
        previous = by_id.get(item["id"])
        if previous and previous["folder"] != item["folder"]:
            shutil.rmtree(target_root / previous["folder"])
        target = target_root / item["folder"]
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(delta_root / item["folder"], target, copy_function=_link_or_copy)
        by_id[item["id"]] = item
    items = sorted(by_id.values(), key=lambda item: item["id"])
    old_doc["editions"] = items
    old_doc["edition_count"] = len(items)
    old_doc["source"] = {"name": "Scryfall targeted API", "as_of": source_as_of,
                         "sha256": source_sha}
    for name, content in (("index.json", json.dumps(old_doc, ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":")) + "\n"),
                          ("index.md", "# Catalogue Magic\n\n" + "".join(
                              f"- [{e['name']} ({e['code']})]({e['index_md']}) — {e['card_records']} enregistrements\n"
                              for e in items))):
        path = target_root / name
        path.unlink(missing_ok=True)
        path.write_text(content, encoding="utf-8")


def build_incremental_version(config_path: Path, current_db: Path, old_version: Path,
                              target_version: Path, source_report: dict, previous_sha: str,
                              *, progress=None) -> dict:
    """Build only changed editions, then merge them into a SQLite backup of the active version."""
    config_path = Path(config_path).resolve()
    ops_root = config_path.parent
    archive = Path(source_report["sources"]["scryfall_all_cards"]["archive_path"])
    delta_sha = source_report["sources"]["scryfall_all_cards"]["archive_sha256"]
    set_items = source_report["set_items"]
    set_ids = [item["id"] for item in set_items]
    source_as_of = source_report["generated_at_utc"]
    source_sha = hashlib.sha256((previous_sha + delta_sha).encode()).hexdigest()
    config = _load_config(config_path, ops_root)
    source_db = target_version / ".delta-source.sqlite"
    delta_db = target_version / ".delta-catalogue.sqlite"
    delta_editions = target_version / ".delta-editions"
    target_version.mkdir(parents=True, exist_ok=True)
    bulk_meta = {"updated_at": source_as_of, "sha256": delta_sha,
                 "validation": {"rows": source_report["sources"]["scryfall_all_cards"]["rows"]}}
    try:
        build_catalogue_db(archive, set_items, source_db, config, bulk_meta,
                           source_report["sources"]["scryfall_sets"]["sha256"], date.today(),
                           progress=(lambda fraction, detail="": progress("catalogue", 0.04 * fraction, detail)) if progress else None)
        build_catalogue(source_db, archive, delta_db, archive_sha256=delta_sha,
                        archive_as_of=source_as_of, edition_root=delta_editions,
                        source_name="Scryfall targeted API",
                        total_records=bulk_meta["validation"]["rows"],
                        progress=(lambda fraction, detail="": progress("catalogue", 0.04 + 0.08 * fraction, detail)) if progress else None)
        target_db = target_version / "catalogue.sqlite"
        _copy_active_database(current_db, target_db,
                              (lambda fraction, detail="": progress("catalogue", fraction, detail)) if progress else None)
        _merge_edition_rows(target_db, delta_db, set_ids, source_sha, source_as_of,
                            (lambda fraction, detail="": progress("catalogue", fraction, detail)) if progress else None)
        _merge_exports(old_version / "editions", delta_editions, target_version / "editions",
                       source_sha, source_as_of)
        if progress:
            progress("exports", 1.0, "Exports des éditions inchangées réutilisés")
        if (old_version / "assets").exists():
            shutil.copytree(old_version / "assets", target_version / "assets", copy_function=_link_or_copy)
        if progress:
            progress("images", 1.0, "Images existantes conservées")
        from ..maintenance import _refresh_manifests
        _refresh_manifests(target_db, target_version, edition_ids=set(set_ids),
                           progress=(lambda fraction, detail="": progress("manifests", fraction, detail)) if progress else None)
        if progress:
            progress("catalogue", 1.0, f"{len(set_ids)} éditions actualisées")
        return {"source_sha256": source_sha, "changed_set_ids": set_ids,
                "new_card_records": source_report["import"]["new_card_records"]}
    finally:
        source_db.unlink(missing_ok=True)
        delta_db.unlink(missing_ok=True)
        shutil.rmtree(delta_editions, ignore_errors=True)
