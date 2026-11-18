"""Recover the mislabelled New Phyrexia historical folder by printed title."""
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


def build_plan(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    current = resolve_current(CATALOGUE)
    if report.get("catalogue_version_id") != current["version_id"]:
        raise ValueError("version OCR différente")
    rows = []
    with sqlite3.connect(current["database"]) as conn:
        set_id = conn.execute("SELECT set_id FROM editions WHERE code='nph'").fetchone()[0]
        printings = conn.execute("SELECT printing_id,collector_number FROM printings WHERE set_id=?", (set_id,)).fetchall()
        titles = {}
        for printing, number in printings:
            for provider, printed, name in conn.execute("SELECT provider_card_id,printed_name,name FROM source_records WHERE printing_id=? AND lang='fr'", (printing,)):
                title = printed or name
                if title:
                    titles.setdefault(_norm(title), []).append((printing, number, provider, title))
        for record in report["rows"]:
            path = record.get("path", "")
            if not path.startswith("Phyrexian (2023)/") or not record.get("ocr"):
                continue
            original = ROOT / path
            if not original.is_file() or not original.name.casefold().endswith("_fr.jpg"):
                continue
            raw_title = record["ocr"]["text_by_region"].get("title", "")
            observed = _norm(raw_title)
            if len(observed) < 5:
                continue
            ranked = sorted(((SequenceMatcher(None, observed, key).ratio(), key) for key in titles), reverse=True)
            if not ranked or ranked[0][0] < .78 or (len(ranked)>1 and ranked[0][0]-ranked[1][0] < .12):
                continue
            matches = titles[ranked[0][1]]
            if len({m[0] for m in matches}) != 1:
                continue
            printing, number, provider, target_title = matches[0]
            data = original.read_bytes()
            if hashlib.sha256(data).hexdigest() != record.get("sha256"):
                continue
            rows.append({"path": path, "bytes": len(data), "sha256": record["sha256"],
                         "action": "verify", "provider_card_id": provider,
                         "printing_id": printing, "face_index": 0, "set_code": "nph",
                         "collector_number": number, "language": "fr",
                         "proof": {"kind": "folder_title", "title": target_title,
                                   "ocr_title": raw_title, "set_code": "nph",
                                   "report": report_path.name}})
    return {"version_id": current["version_id"], "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = build_plan(args.report)
    path = OPS_ROOT / "reports" / "nph-title-plan-current.json"
    _write(path, plan)
    print(json.dumps({"plan_rows": len(plan["rows"]),
                      "result": apply_plan(path) if args.apply else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
