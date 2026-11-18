"""Recover exact printings directly from OCR footer set, collector and title."""
from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

from .archive_finalize import CATALOGUE, OPS_ROOT, ROOT, _write, apply_plan
from .v2.publish import resolve_current


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").casefold()
    return " ".join(re.findall(r"[a-z0-9]+", "".join(c for c in text if not unicodedata.combining(c))))


def _language(path: Path) -> str:
    words = set(re.split(r"[_\-.]", path.stem.casefold()))
    return "fr" if "fr" in words else "en"


def build_plan(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    current = resolve_current(CATALOGUE)
    if report.get("catalogue_version_id") != current["version_id"]:
        raise ValueError("version OCR différente")
    rows = []
    with sqlite3.connect(current["database"]) as conn:
        sets = {code.upper(): (set_id, code) for set_id, code in conn.execute("SELECT set_id,code FROM editions")}
        for record in report["rows"]:
            path = record.get("path", "")
            original = ROOT / path
            if not original.is_file() or not record.get("ocr"):
                continue
            regions = record["ocr"]["text_by_region"]
            footer = regions.get("footer", "").upper()
            title = _norm(regions.get("title", ""))
            if not title:
                continue
            observed_codes = [code for code in sets if re.search(r"(?<![A-Z0-9])" + re.escape(code) + r"(?![A-Z0-9])", footer)]
            numbers = {str(int(value)) for value in re.findall(r"(?<!\d)0*(\d{1,4})/\d{1,4}", footer)}
            for value in re.findall(r"(?<![A-Z0-9])[A-Z]\s+0*(\d{3,4})(?![A-Z0-9])", footer):
                numbers.add(str(int(value)))
            if not numbers or not observed_codes:
                continue
            matches = []
            lang = _language(original)
            for code in observed_codes:
                set_id, raw_code = sets[code]
                for number in numbers:
                    printings = conn.execute("SELECT printing_id FROM printings INDEXED BY printings_set_number_idx WHERE set_id=? AND collector_number=?",
                                             (set_id, number)).fetchall()
                    for (printing,) in printings:
                        sources = conn.execute("SELECT provider_card_id,name,printed_name,raw_json FROM source_records WHERE printing_id=? AND lang=?",
                                               (printing, lang)).fetchall()
                        for provider, name, printed, raw in sources:
                            candidates = [(0, printed or name or "")]
                            faces = json.loads(raw).get("card_faces") or []
                            for index, face in enumerate(faces):
                                candidates.append((index, face.get("printed_name") or face.get("name") or ""))
                            for face_index, target_title in candidates:
                                norm_title = _norm(target_title)
                                score = SequenceMatcher(None, norm_title, title).ratio() if norm_title else 0.0
                                if norm_title and len(norm_title) >= 3 and (norm_title in title or score >= .72):
                                    slot = conn.execute("SELECT 1 FROM image_slots WHERE printing_id=? AND face_index=?",
                                                        (printing, face_index)).fetchone()
                                    if slot:
                                        matches.append((printing, provider, face_index, raw_code, number, target_title))
            keys = {(m[0], m[2]) for m in matches}
            if len(keys) != 1:
                continue
            printing, provider, face_index, code, number, target_title = matches[0]
            data = original.read_bytes()
            if hashlib.sha256(data).hexdigest() != record.get("sha256"):
                continue
            rows.append({"path": path, "bytes": len(data), "sha256": record["sha256"],
                         "action": "verify", "provider_card_id": provider,
                         "printing_id": printing, "face_index": face_index, "set_code": code,
                         "collector_number": number, "language": lang,
                         "proof": {"kind": "ocr_face" if face_index else "ocr", "title": target_title,
                                   "ocr_title": regions.get("title", ""),
                                   "set_code": code, "collector_number": number,
                                   "ocr_raw": "\n".join(regions.values()), "report": report_path.name}})
    return {"version_id": current["version_id"], "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = build_plan(args.report)
    path = OPS_ROOT / "reports" / "ocr-footer-plan-current.json"
    _write(path, plan)
    print(json.dumps({"plan_rows": len(plan["rows"]),
                      "result": apply_plan(path) if args.apply else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
