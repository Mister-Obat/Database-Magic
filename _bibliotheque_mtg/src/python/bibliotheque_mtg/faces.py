from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable


PHYSICAL_MULTIFACE_LAYOUTS = {
    "transform", "modal_dfc", "double_faced_token", "reversible_card"
}
SINGLE_PHYSICAL_FACE_LAYOUTS = {"split", "adventure", "meld"}


def _decode_json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _attributes(raw: dict) -> str:
    values = {}
    for key in ("illustration_id", "finishes", "frame_effects", "border_color", "variation", "promo"):
        value = raw.get(key)
        if isinstance(value, list):
            value = sorted(value)
        if value is not None:
            values[key] = value
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _group_key(record: dict) -> tuple:
    set_id = record.get("set_id")
    collector = str(record.get("collector_number") or "")
    oracle_id = record.get("oracle_id")
    layout = str(record.get("layout") or "unknown")
    raw = _decode_json(record.get("raw_json"))
    if set_id and collector and (oracle_id or raw.get("illustration_id")):
        return ("print", set_id, collector, oracle_id, layout, _attributes(raw))
    return ("provider", str(record.get("id") or "missing-provider-id"))


def _stable_id(identity: tuple, provider_id: str | None = None) -> str:
    payload = {"identity": identity, "provider_id": provider_id}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    return "prn_" + digest[:24]


@dataclass(frozen=True)
class PrintingGroup:
    printing_id: str
    set_id: str | None
    set_code: str | None
    collector_number: str | None
    oracle_id: str | None
    layout: str
    identity_status: str
    members: dict[str, dict]

    @property
    def provider_ids(self) -> dict[str, str]:
        return {language: record["id"] for language, record in self.members.items()}


@dataclass(frozen=True)
class PhysicalFace:
    printing_id: str
    face_index: int
    layout: str
    names: dict[str, str | None]
    image_uris: dict[str, dict]
    set_id: str | None
    set_code: str | None
    collector_number: str | None
    oracle_id: str | None
    provider_ids: dict[str, str]
    identity_status: str


def _rows_from_sqlite(conn: sqlite3.Connection, set_id: str | None = None):
    sql = ("SELECT id, oracle_id, lang, set_id, set_code, collector_number, layout, "
           "image_uris_json, raw_json FROM cards")
    args = ()
    if set_id is not None:
        sql += " WHERE set_id=?"
        args = (set_id,)
    sql += " ORDER BY set_id, collector_number, id"
    for row in conn.execute(sql, args):
        yield {"id": row[0], "oracle_id": row[1], "lang": row[2], "set_id": row[3],
               "set_code": row[4], "collector_number": str(row[5] or ""), "layout": row[6],
               "image_uris_json": row[7], "raw_json": row[8]}


def build_printing_groups(records: Iterable[dict]) -> list[PrintingGroup]:
    grouped: dict[tuple, list[dict]] = {}
    for source in records:
        record = dict(source)
        language = str(record.get("lang") or "").casefold()
        if language not in ("fr", "en"):
            continue
        record["lang"] = language
        record["id"] = str(record.get("id") or "")
        if not record["id"]:
            raise ValueError("Each printing-language record needs a provider card id")
        grouped.setdefault(_group_key(record), []).append(record)
    result = []
    for key, rows in sorted(grouped.items(), key=lambda item: repr(item[0])):
        by_language = {language: [row for row in rows if row["lang"] == language]
                       for language in ("fr", "en")}
        unique_pair = (len(rows) == 2 and len(by_language["fr"]) == 1 and len(by_language["en"]) == 1)
        if unique_pair:
            pair = {language: by_language[language][0] for language in ("fr", "en")}
            layouts_match = (pair["fr"].get("layout") or "unknown") == (pair["en"].get("layout") or "unknown")
            fr_faces = _decode_json(pair["fr"].get("raw_json")).get("card_faces") or []
            en_faces = _decode_json(pair["en"].get("raw_json")).get("card_faces") or []
            faces_match = len(fr_faces) == len(en_faces)
            if layouts_match and faces_match:
                result.append(_make_group(key, pair, "confirmed_multilang"))
                continue
        if len(rows) == 1:
            row = rows[0]
            result.append(_make_group(key, {row["lang"]: row}, "single_language"))
            continue
        for row in sorted(rows, key=lambda item: (item["lang"], item["id"])):
            result.append(_make_group(key, {row["lang"]: row}, "unresolved_identity",
                                      provider_id=row["id"]))
    return sorted(result, key=lambda group: (group.set_id or "", group.collector_number or "",
                                             group.printing_id))


def _make_group(key: tuple, members: dict[str, dict], status: str,
                provider_id: str | None = None) -> PrintingGroup:
    first = next(iter(members.values()))
    return PrintingGroup(
        printing_id=_stable_id(key, provider_id), set_id=first.get("set_id"),
        set_code=first.get("set_code"), collector_number=str(first.get("collector_number") or ""),
        oracle_id=first.get("oracle_id"), layout=str(first.get("layout") or "unknown"),
        identity_status=status, members=members)


def printing_groups_from_catalogue(catalogue_path, set_id: str | None = None) -> list[PrintingGroup]:
    conn = sqlite3.connect(catalogue_path)
    try:
        return build_printing_groups(_rows_from_sqlite(conn, set_id))
    finally:
        conn.close()


def physical_faces(group: PrintingGroup) -> list[PhysicalFace]:
    raws = {language: _decode_json(record.get("raw_json"))
            for language, record in group.members.items()}
    if group.layout in PHYSICAL_MULTIFACE_LAYOUTS:
        face_counts = [len(raw.get("card_faces") or []) for raw in raws.values()]
        count = max([2, *face_counts])
    else:
        count = 1
    result = []
    for index in range(count):
        names, urls = {}, {}
        for language, record in group.members.items():
            raw = raws[language]
            if group.layout in PHYSICAL_MULTIFACE_LAYOUTS:
                faces = raw.get("card_faces") or []
                face = faces[index] if index < len(faces) and isinstance(faces[index], dict) else {}
                name = face.get("printed_name") if language == "fr" else (face.get("printed_name") or face.get("name"))
                image_uris = face.get("image_uris") or {}
            else:
                name = raw.get("printed_name") if language == "fr" else (raw.get("printed_name") or raw.get("name"))
                image_uris = raw.get("image_uris") or {}
                if not image_uris:
                    image_uris = _decode_json(record.get("image_uris_json"))
            image_uris = {key: value for key, value in image_uris.items()
                          if isinstance(value, str) and value.strip()}
            names[language] = name if isinstance(name, str) else None
            urls[language] = image_uris if isinstance(image_uris, dict) else {}
        for language in ("fr", "en"):
            names.setdefault(language, None)
            urls.setdefault(language, {})
        result.append(PhysicalFace(group.printing_id, index, group.layout, names, urls,
                                   group.set_id, group.set_code, group.collector_number,
                                   group.oracle_id, group.provider_ids, group.identity_status))
    return result

