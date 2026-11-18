from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from . import SCHEMA_VERSION


PHYSICAL_MULTIFACE_LAYOUTS = {
    "transform", "modal_dfc", "double_faced_token", "reversible_card"
}


def _json(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _identity_key(row: dict) -> tuple | None:
    """Use identity evidence, never language-dependent finish/treatment fields."""
    raw = _json(row.get("raw_json"), {})
    set_id = row.get("set_id")
    number = str(row.get("collector_number") or "")
    oracle_id = row.get("oracle_id")
    illustration_id = raw.get("illustration_id")
    if not set_id or not number or not (oracle_id or illustration_id):
        return None
    return (str(set_id), number, str(oracle_id or ""),
            str(row.get("layout") or "unknown"), str(illustration_id or ""))


def _stable_id(key: tuple | None, provider_id: str | None = None) -> str:
    payload = {"identity": key, "provider_id": provider_id}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return "v2prn_" + hashlib.sha256(canonical).hexdigest()[:24]


def identity_groups(records: Iterable[dict]) -> list[dict]:
    """Resolve only a unique FR/EN pair on the same set/number/oracle/art/layout.

    Records without enough evidence, or collisions with multiple records in a
    language, remain separate and carry an explicit exception reason.
    """
    grouped = defaultdict(list)
    unkeyed = []
    for source in records:
        row = dict(source)
        lang = str(row.get("lang") or "").casefold()
        if not lang:
            continue
        row["lang"] = lang
        provider_id = str(row.get("id") or "")
        if not provider_id:
            raise ValueError("Every source row must have its provider card id")
        key = _identity_key(row)
        if key is None:
            unkeyed.append((row, "missing_set_number_or_identity_evidence"))
        else:
            grouped[key].append(row)

    result = []
    for key in sorted(grouped):
        rows = sorted(grouped[key], key=lambda r: (r["lang"], str(r["id"])))
        counts = {lang: sum(r["lang"] == lang for r in rows) for lang in {r["lang"] for r in rows}}
        if len(rows) > 1 and all(count == 1 for count in counts.values()):
            face_counts = [len(_json(row.get("raw_json"), {}).get("card_faces") or []) for row in rows]
            if face_counts[0] == face_counts[1]:
                result.append({"key": key, "rows": rows, "status": "confirmed_multilang",
                               "evidence": "same set id, collector number, oracle id, layout and illustration id"})
            else:
                for row in rows:
                    result.append({"key": key, "rows": [row], "status": "unresolved_identity",
                                   "evidence": "provider id retained; FR/EN face counts differ",
                                   "exception": "language_face_structure_mismatch"})
        elif len(rows) == 1:
            result.append({"key": key, "rows": rows, "status": "single_language",
                           "evidence": "unique provider record with set/number and oracle or illustration identity"})
        else:
            for row in rows:
                result.append({"key": key, "rows": [row], "status": "unresolved_identity",
                               "evidence": "provider id retained; identity key collided within a language",
                               "exception": "ambiguous_identity_key"})
    for row, reason in sorted(unkeyed, key=lambda item: (item[0]["lang"], str(item[0]["id"]))):
        result.append({"key": None, "rows": [row], "status": "unresolved_identity",
                       "evidence": "provider id retained; no safe cross-language identity key",
                       "exception": reason})
    return result


def _faces(row: dict) -> list[dict]:
    raw = _json(row.get("raw_json"), {})
    layout = str(row.get("layout") or "unknown")
    source_faces = raw.get("card_faces") or []
    if layout in PHYSICAL_MULTIFACE_LAYOUTS:
        count = max(2, len(source_faces))
    else:
        count = 1
    return [{"face_index": i, "physical_image": 1} for i in range(count)]


def _record_values(row: dict, printing_id: str) -> tuple:
    raw = _json(row.get("raw_json"), {})
    colors = _json(row.get("colors_json"), raw.get("colors") or [])
    color_identity = _json(row.get("color_identity_json"), raw.get("color_identity") or [])
    artist = _json(row.get("artist_json"), raw.get("artist_ids") or raw.get("artist") or [])
    return (
        str(row["id"]), printing_id, row["lang"], raw.get("uri"),
        str(row.get("name") or raw.get("name") or ""), raw.get("printed_name"),
        row.get("type_line") or raw.get("type_line"), raw.get("printed_type_line"),
        row.get("mana_cost") or raw.get("mana_cost"), row.get("mana_value"),
        json.dumps(colors, ensure_ascii=False, sort_keys=True),
        json.dumps(color_identity, ensure_ascii=False, sort_keys=True),
        "".join(sorted(str(x) for x in colors)),
        "".join(sorted(str(x) for x in color_identity)),
        row.get("rarity") or raw.get("rarity"),
        json.dumps(artist, ensure_ascii=False, sort_keys=True),
        row.get("oracle_text") if row.get("oracle_text") is not None else raw.get("oracle_text"),
        row.get("printed_text") if row.get("printed_text") is not None else raw.get("printed_text"),
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _create_schema(conn: sqlite3.Connection) -> None:
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)


def migrate(source_db: str | Path, target_db: str | Path, *, archive_sha256: str,
            archive_as_of: str, eligible_only: bool = True) -> dict:
    """Build a deterministic V2 SQLite snapshot without network access.

    The destination is replaced atomically only after integrity checks pass.
    A pre-existing destination is left untouched on any error.
    """
    source_db = Path(source_db).resolve()
    target_db = Path(target_db).resolve()
    target_db.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".catalogue-v2-", suffix=".sqlite",
                                         dir=target_db.parent, delete=False) as temp:
            temp_name = temp.name
        out = sqlite3.connect(temp_name)
        out.execute("PRAGMA journal_mode=DELETE")
        out.execute("PRAGMA synchronous=FULL")
        _create_schema(out)
        sets = source.execute(
            "SELECT id,code,name,set_type,released_at,digital,card_count,eligibility,exclusion_reason "
            "FROM sets ORDER BY id").fetchall()
        out.executemany("INSERT INTO editions VALUES(?,?,?,?,?,?,?,?,?)", [tuple(r) for r in sets])
        oracle_counts = source.execute(
            "SELECT c.oracle_id,COUNT(*) FROM cards c JOIN sets s ON s.id=c.set_id "
            "WHERE c.oracle_id IS NOT NULL AND (?=0 OR s.eligibility='eligible') "
            "GROUP BY c.oracle_id ORDER BY c.oracle_id", (int(eligible_only),)).fetchall()
        out.executemany("INSERT INTO oracle_cards VALUES(?,?)", [tuple(r) for r in oracle_counts])
        sql = ("SELECT c.id,c.oracle_id,c.lang,c.set_id,c.set_code,c.collector_number,c.layout,"
               "c.name,c.type_line,c.mana_cost,c.mana_value,c.colors_json,c.color_identity_json,"
               "c.rarity,c.artist_json,c.printed_text,c.oracle_text,c.raw_json "
               "FROM cards c JOIN sets s ON s.id=c.set_id WHERE (?=0 OR s.eligibility='eligible') "
               "AND c.lang IN ('fr','en') ORDER BY c.set_id,c.collector_number,c.id")
        rows = [dict(r) for r in source.execute(sql, (int(eligible_only),))]
        groups = identity_groups(rows)
        source_record_count = 0
        face_count = 0
        exception_count = 0
        for group in groups:
            key = group["key"]
            first = group["rows"][0]
            raw = _json(first.get("raw_json"), {})
            printing_id = _stable_id(key, str(first["id"]) if key is None or group["status"] == "unresolved_identity" else None)
            if group["status"] == "unresolved_identity":
                exception_count += len(group["rows"])
            variant_attributes = {k: raw.get(k) for k in ("variation", "promo", "frame_effects", "border_color")
                                  if raw.get(k) is not None}
            out.execute("INSERT INTO printings VALUES(?,?,?,?,?,?,?,?,?)", (
                printing_id, first["set_id"], str(first.get("collector_number") or ""),
                first.get("oracle_id"), str(first.get("layout") or "unknown"),
                raw.get("illustration_id"), group["status"], group["evidence"],
                json.dumps(variant_attributes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
            for row in group["rows"]:
                vals = _record_values(row, printing_id)
                out.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
                out.execute("INSERT INTO printing_languages VALUES(?,?,?)", (printing_id, row["lang"], row["id"]))
                out.execute("INSERT INTO source_search(provider_card_id,set_code,lang,name,printed_name,type_line,printed_type_line,oracle_text,printed_text,artist) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)", (row["id"], row.get("set_code") or "", row["lang"], row.get("name") or raw.get("name") or "",
                            _json(row.get("raw_json"), {}).get("printed_name") or "", row.get("type_line") or "",
                            _json(row.get("raw_json"), {}).get("printed_type_line") or "",
                            row.get("oracle_text") or "", row.get("printed_text") or "", raw.get("artist") or ""))
                if group.get("exception"):
                    out.execute("INSERT INTO identity_exceptions VALUES(?,?,?,?)", (
                        row["id"], group["exception"], group["evidence"],
                        json.dumps({"archive_sha256": archive_sha256, "as_of": archive_as_of,
                                    "set_code": row.get("set_code"), "lang": row["lang"]}, ensure_ascii=False, sort_keys=True)))
                source_record_count += 1
            face_templates = _faces(first)
            for face in face_templates:
                out.execute("INSERT INTO card_faces VALUES(?,?,?)", (printing_id, face["face_index"], face["physical_image"]))
                face_count += 1
        out.execute("INSERT INTO printing_added SELECT printing_id,strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM printings")
        meta = {
            "schema_version": SCHEMA_VERSION,
            "source_database_sha256": _sha256(source_db),
            "source_archive_sha256": archive_sha256,
            "source_as_of": archive_as_of,
            "eligible_only": str(bool(eligible_only)).lower(),
            "source_records": str(source_record_count),
            "printings": str(len(groups)),
            "identity_exceptions": str(exception_count),
            "faces": str(face_count),
        }
        out.executemany("INSERT INTO catalogue_meta VALUES(?,?)", sorted(meta.items()))
        out.commit()
        integrity = out.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"V2 integrity check failed: {integrity}")
        checks = {
            "source_records": out.execute("SELECT COUNT(*) FROM source_records").fetchone()[0],
            "printings": out.execute("SELECT COUNT(*) FROM printings").fetchone()[0],
            "exceptions": out.execute("SELECT COUNT(*) FROM identity_exceptions").fetchone()[0],
            "faces": out.execute("SELECT COUNT(*) FROM card_faces").fetchone()[0],
            "search_rows": out.execute("SELECT COUNT(*) FROM source_search").fetchone()[0],
        }
        out.close()
        os.replace(temp_name, target_db)
        temp_name = None
        return {"status": "valid", "integrity": integrity, **checks,
                "target_db": str(target_db), "target_sha256": _sha256(target_db), "meta": meta}
    finally:
        source.close()
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def import_legacy_aliases(v2_db: str | Path, legacy_generation: str | Path) -> dict:
    """Link V1 printing IDs to V2 only when a stored provider ID proves the link."""
    v2_db = Path(v2_db).resolve()
    legacy_generation = Path(legacy_generation).resolve()
    root_index = json.loads((legacy_generation / "index.json").read_text(encoding="utf-8"))
    legacy_to_provider_ids = defaultdict(set)
    for edition in root_index.get("editions", []):
        edition_path = edition.get("index_json")
        if not edition_path:
            continue
        document = json.loads((legacy_generation / edition_path).read_text(encoding="utf-8"))
        for card in document.get("cards", []):
            legacy_id = str(card.get("printing_id") or "")
            if not legacy_id:
                continue
            ids = card.get("provider_ids") or {}
            for provider_id in ids.values():
                if provider_id:
                    legacy_to_provider_ids[legacy_id].add(str(provider_id))
            legacy_to_provider_ids.setdefault(legacy_id, set())

    conn = sqlite3.connect(v2_db)
    try:
        provider_map = dict(conn.execute("SELECT provider_card_id,printing_id FROM source_records"))
        rows = []
        for legacy_id in sorted(legacy_to_provider_ids):
            targets = {provider_map[p] for p in legacy_to_provider_ids[legacy_id] if p in provider_map}
            if len(targets) == 1:
                target, status = next(iter(targets)), "confirmed"
                evidence = "matching Scryfall provider card id in both generations"
            elif len(targets) > 1:
                target, status = None, "ambiguous"
                evidence = "V1 provider ids resolve to multiple V2 printings"
            else:
                target, status = None, "unmatched"
                evidence = "no matching provider card id; historical identity not inferred"
            rows.append((legacy_id, target, evidence, status))
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("INSERT OR REPLACE INTO legacy_aliases VALUES(?,?,?,?)", rows)
        conn.commit()
        status_counts = dict(conn.execute("SELECT status,COUNT(*) FROM legacy_aliases GROUP BY status"))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"V2 alias import integrity check failed: {integrity}")
        return {"status": "valid", "legacy_ids": len(rows), "status_counts": status_counts,
                "integrity": integrity}
    finally:
        conn.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

