"""Recover historical cards with a calibrated folder and a unique OCR title."""
from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import json
import sqlite3
from pathlib import Path

from .archive_finalize import CATALOGUE, OPS_ROOT, ROOT, _write, apply_plan
from .archive_recover_nph import _norm
from .v2.publish import resolve_current


SPECIAL = {"Phyrexian (2023)": "nph", "Kamigawa (2004)": "neo"}


def build_plan(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    current = resolve_current(CATALOGUE)
    if report.get("catalogue_version_id") != current["version_id"]:
        raise ValueError("version OCR différente")
    aliases = json.loads((OPS_ROOT / "reports" / "edition_aliases.json").read_text(encoding="utf-8"))
    folders = dict(SPECIAL)
    for alias in aliases["aliases"]:
        if alias.get("status") == "confirmed_exact_candidate" and len(alias.get("candidates", [])) == 1:
            folders[alias["folder"]] = alias["candidates"][0]["set_code"]
    rows = []
    with sqlite3.connect(current["database"]) as conn:
        titles_by_set = {}
        for folder, code in folders.items():
            set_row = conn.execute("SELECT set_id FROM editions WHERE lower(code)=lower(?)", (code,)).fetchone()
            if not set_row:
                continue
            titles = {}
            for printing, number in conn.execute("SELECT printing_id,collector_number FROM printings WHERE set_id=?", set_row):
                for provider, lang, printed, name in conn.execute("SELECT provider_card_id,lang,printed_name,name FROM source_records WHERE printing_id=? AND lang IN ('fr','en')", (printing,)):
                    title = printed or name
                    if title:
                        titles.setdefault(lang, {}).setdefault(_norm(title), []).append((printing, number, provider, title))
            titles_by_set[folder] = (code, titles)
        for record in report["rows"]:
            path = record.get("path", "")
            folder = Path(path).parts[0] if path else ""
            if folder not in titles_by_set or not record.get("ocr"):
                continue
            original = ROOT / path
            if not original.is_file():
                continue
            stem = original.stem.casefold()
            lang = "fr" if stem.endswith("_fr") or stem.startswith("fr_") else "en" if stem.endswith("_en") or stem.startswith("en_") else None
            if not lang:
                continue
            code, by_language = titles_by_set[folder]
            titles = by_language.get(lang, {})
            raw_title = record["ocr"]["text_by_region"].get("title", "")
            observed = _norm(raw_title)
            if len(observed) < 5:
                continue
            ranked = sorted(((SequenceMatcher(None, observed, key).ratio(), key) for key in titles), reverse=True)
            if not ranked or ranked[0][0] < .78 or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < .12):
                continue
            matches = titles[ranked[0][1]]
            if len({match[0] for match in matches}) != 1:
                continue
            printing, number, provider, target_title = matches[0]
            data = original.read_bytes()
            if hashlib.sha256(data).hexdigest() != record.get("sha256"):
                continue
            rows.append({"path": path, "bytes": len(data), "sha256": record["sha256"],
                         "action": "verify", "provider_card_id": provider,
                         "printing_id": printing, "face_index": 0, "set_code": code,
                         "collector_number": number, "language": lang,
                         "proof": {"kind": "folder_title", "title": target_title,
                                   "ocr_title": raw_title, "set_code": code,
                                   "report": report_path.name}})
    return {"version_id": current["version_id"], "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = build_plan(args.report)
    path = OPS_ROOT / "reports" / "folder-title-plan-current.json"
    _write(path, plan)
    print(json.dumps({"plan_rows": len(plan["rows"]),
                      "result": apply_plan(path) if args.apply else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
