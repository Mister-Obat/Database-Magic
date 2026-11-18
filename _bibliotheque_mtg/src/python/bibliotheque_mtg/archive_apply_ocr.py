"""Build and apply a strictly verified historical import plan from OCR checkpoints."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .archive_finalize import CATALOGUE, LOG, OPS_ROOT, ROOT, _write, apply_plan
from .v2.publish import resolve_current


def _raw_text(row: dict) -> str:
    parts = row.get("ocr", {}).get("text_by_region", {})
    return "\n".join(str(parts.get(name) or "") for name in ("title", "header", "footer", "full"))


def build_plan(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    current = resolve_current(CATALOGUE)
    if report.get("catalogue_version_id") != current["version_id"]:
        raise ValueError("rapport OCR établi sur une autre version")
    done = json.loads(LOG.read_text(encoding="utf-8"))["entries"] if LOG.is_file() else {}
    rows = []
    seen = set()
    for candidate in report["rows"]:
        proposal = candidate.get("identity_proposal") or {}
        if proposal.get("status") != "unique":
            continue
        path = candidate["path"].replace("\\", "/")
        if path in done or path in seen or not (ROOT / path).is_file():
            continue
        seen.add(path)
        target = proposal["target"]
        evidence = proposal["evidence"]
        method = evidence.get("identity_method")
        if target["language"] not in {"fr", "en"}:
            continue
        basename = Path(path).name.casefold()
        if (basename.startswith("fr_") or basename.endswith("_fr.jpg")) and target["language"] != "fr":
            continue
        if (basename.startswith("en_") or basename.endswith("_en.jpg")) and target["language"] != "en":
            continue
        if method == "printed_code_number_title":
            proof = {"kind": "ocr", "title": evidence["catalogue_name"],
                     "set_code": target["set_code"], "collector_number": target["collector_number"]}
        elif method == "confirmed_folder_title_artist" and evidence.get("matched_artists"):
            proof = {"kind": "ocr_artist", "title": evidence["catalogue_name"],
                     "artist": evidence["matched_artists"][0], "set_code": target["set_code"],
                     "folder_confirmed": True}
        else:
            continue
        proof["ocr_raw"] = _raw_text(candidate)
        proof["report"] = report_path.name
        rows.append({"path": path, "bytes": (ROOT / path).stat().st_size,
                     "sha256": candidate["sha256"], "action": "verify", **target,
                     "proof": proof})
    return {"version_id": current["version_id"], "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = build_plan(args.report)
    plan_path = OPS_ROOT / "reports" / "ocr-import-plan-current.json"
    _write(plan_path, plan)
    if args.apply:
        print(json.dumps({"plan_rows": len(plan["rows"]), "result": apply_plan(plan_path)}, ensure_ascii=False))
    else:
        print(json.dumps({"plan_rows": len(plan["rows"]), "plan": str(plan_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
