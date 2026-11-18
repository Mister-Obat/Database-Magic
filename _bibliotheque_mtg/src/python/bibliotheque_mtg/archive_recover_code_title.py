"""Recover cards by OCR footer set code and a unique printed title."""
from __future__ import annotations

import argparse
from collections import defaultdict
from difflib import SequenceMatcher
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .archive_finalize import CATALOGUE, OPS_ROOT, ROOT, _write, apply_plan
from .archive_recover_nph import _norm
from .v2.publish import resolve_current


def build_plan(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    current = resolve_current(CATALOGUE)
    if report.get("catalogue_version_id") != current["version_id"]:
        raise ValueError("version OCR différente")
    rows = []
    with sqlite3.connect(current["database"]) as conn:
        target_codes = {"LTR", "LTC", "MH3", "M3C", "TMH3", "TM3C", "NEO", "NPH",
                        "M19", "XLN", "DOM", "M12", "M13", "ORI", "THS", "EMN", "DKA"}
        known = {str(code).upper() for (code,) in conn.execute("SELECT code FROM editions")} & target_codes
        observed = set()
        for record in report["rows"]:
            path = record.get("path", "")
            if not path or not (ROOT / path).is_file():
                continue
            footer = record.get("ocr", {}).get("text_by_region", {}).get("footer", "").upper()
            observed |= known & set(re.findall(r"(?<![A-Z0-9])[A-Z0-9]{3,5}(?![A-Z0-9])", footer))
        catalog = defaultdict(lambda: defaultdict(dict))
        for code in observed:
            query = """SELECT p.printing_id,p.collector_number,sr.provider_card_id,
                sr.lang,COALESCE(NULLIF(sr.printed_name,''),sr.name)
                FROM editions e JOIN printings p USING(set_id)
                JOIN source_records sr USING(printing_id)
                WHERE upper(e.code)=? AND sr.lang IN ('fr','en')"""
            for printing, number, provider, lang, title in conn.execute(query, (code,)):
                if title:
                    catalog[code][lang].setdefault(_norm(title), []).append((printing, number, provider, title))
        for record in report["rows"]:
            path = record.get("path", "")
            original = ROOT / path
            if not original.is_file() or not record.get("ocr"):
                continue
            stem = original.stem.casefold()
            lang = "fr" if stem.startswith("fr_") or stem.endswith("_fr") else "en" if stem.startswith("en_") or stem.endswith("_en") else None
            if not lang:
                continue
            regions = record["ocr"].get("text_by_region", {})
            footer = regions.get("footer", "")
            codes = observed & set(re.findall(r"(?<![A-Z0-9])[A-Z0-9]{3,5}(?![A-Z0-9])", footer.upper()))
            raw_title = regions.get("title", "")
            title = _norm(raw_title)
            if title.split() and len(title.split()[0]) == 1 and len(title.split()) > 1:
                title = " ".join(title.split()[1:])
            if len(title) < 5:
                continue
            candidates = []
            for code in codes:
                titles = catalog[code][lang]
                ranked = []
                for key in titles:
                    if key[:2] != title[:2]:
                        continue
                    if abs(len(title) - len(key)) > .36 * max(len(title), len(key)):
                        continue
                    matcher = SequenceMatcher(None, title, key)
                    if matcher.quick_ratio() < .66:
                        continue
                    ranked.append((matcher.ratio(), key))
                ranked.sort(reverse=True)
                if not ranked or ranked[0][0] < .78 or (len(ranked) > 1 and ranked[0][0]-ranked[1][0] < .12):
                    continue
                matches = titles[ranked[0][1]]
                if len({match[0] for match in matches}) != 1:
                    continue
                candidates.append((code, matches[0], ranked[0][0]))
            if len(candidates) != 1:
                continue
            code, (printing, number, provider, target_title), score = candidates[0]
            data = original.read_bytes()
            if hashlib.sha256(data).hexdigest() != record.get("sha256"):
                continue
            rows.append({"path": path, "bytes": len(data), "sha256": record["sha256"],
                "action": "verify", "provider_card_id": provider, "printing_id": printing,
                "face_index": 0, "set_code": code, "collector_number": number,
                "language": lang, "proof": {"kind": "ocr_code_title", "title": target_title,
                "ocr_title": raw_title, "ocr_footer": footer, "report": report_path.name}})
    return {"version_id": current["version_id"], "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = build_plan(args.report)
    path = OPS_ROOT / "reports" / "ocr-code-title-plan-current.json"
    _write(path, plan)
    print(json.dumps({"plan_rows": len(plan["rows"]),
                      "result": apply_plan(path) if args.apply else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
