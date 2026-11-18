"""Recover printed second faces from OCR with exact set and collector evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

from .archive_finalize import CATALOGUE, OPS_ROOT, ROOT, _write, apply_plan
from .v2.publish import resolve_current


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return " ".join(re.findall(r"[a-z0-9]+", "".join(c for c in value if not unicodedata.combining(c))))


def build_plan(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    current = resolve_current(CATALOGUE)
    if report.get("catalogue_version_id") != current["version_id"]:
        raise ValueError("version OCR différente")
    aliases_doc = json.loads((OPS_ROOT / "reports" / "edition_aliases.json").read_text(encoding="utf-8"))
    aliases = {entry["folder"]: entry for entry in aliases_doc["aliases"]}
    rows = []
    with sqlite3.connect(current["database"]) as conn:
        set_ids = {code: set_id for set_id, code in conn.execute("SELECT set_id,code FROM editions")}
        for record in report["rows"]:
            path = record.get("path", "")
            original = ROOT / path
            if not original.is_file() or not record.get("ocr"):
                continue
            folder = path.split("/", 1)[0]
            alias = aliases.get(folder, {})
            if alias.get("status") != "confirmed_exact_candidate" or len(alias.get("candidates", [])) != 1:
                continue
            code = alias["candidates"][0]["set_code"]
            set_id = set_ids.get(code)
            if not set_id:
                continue
            regions = record["ocr"]["text_by_region"]
            title = _norm(regions.get("title", ""))
            footer = regions.get("footer", "")
            numbers = {str(int(value)) for value in re.findall(r"(?<!\d)0*(\d{1,4})/\d{1,4}", footer)}
            if not numbers or not title:
                continue
            language = "fr" if re.search(r"(?<![a-z])fr(?![a-z])", original.stem.casefold()) else "en"
            matches = []
            for number in numbers:
                printings = conn.execute("""SELECT printing_id FROM printings INDEXED BY printings_set_number_idx
                    WHERE set_id=? AND collector_number=?""", (set_id, number)).fetchall()
                for (printing,) in printings:
                    slot = conn.execute("SELECT 1 FROM image_slots WHERE printing_id=? AND face_index=1",
                                        (printing,)).fetchone()
                    if not slot:
                        continue
                    sources = conn.execute("SELECT provider_card_id,raw_json FROM source_records WHERE printing_id=? AND lang=?",
                                           (printing, language)).fetchall()
                    for provider, raw in sources:
                        faces = json.loads(raw).get("card_faces") or []
                        if len(faces) < 2:
                            continue
                        face_title = faces[1].get("printed_name") or faces[1].get("name") or ""
                        normalized = _norm(face_title)
                        if normalized and normalized in title:
                            matches.append((printing, provider, number, face_title))
            if len({m[0] for m in matches}) != 1:
                continue
            printing, provider, number, face_title = matches[0]
            data = original.read_bytes()
            if hashlib.sha256(data).hexdigest() != record.get("sha256"):
                continue
            rows.append({"path": path, "bytes": len(data), "sha256": record["sha256"],
                         "action": "verify", "provider_card_id": provider,
                         "printing_id": printing, "face_index": 1, "set_code": code,
                         "collector_number": number, "language": language,
                         "proof": {"kind": "ocr_face", "title": face_title,
                                   "set_code": code, "collector_number": number,
                                   "ocr_raw": "\n".join(regions.values()), "report": report_path.name}})
    return {"version_id": current["version_id"], "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = build_plan(args.report)
    path = OPS_ROOT / "reports" / "ocr-face-plan-current.json"
    _write(path, plan)
    print(json.dumps({"plan_rows": len(plan["rows"]),
                      "result": apply_plan(path) if args.apply else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
