"""Move archived images left without a verified identity to Impossible."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .archive_finalize import CATALOGUE, LOG, OPS_ROOT, ROOT, _write, apply_plan
from .v2.publish import resolve_current

EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _image_paths() -> dict[str, Path]:
    return {path.relative_to(ROOT).as_posix(): path for path in ROOT.rglob("*")
            if path.is_file() and path.suffix.casefold() in EXTENSIONS
            and path.relative_to(ROOT).parts[0].casefold() != "impossible"}


def build_plan(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    current = resolve_current(CATALOGUE)
    if report.get("catalogue_version_id") != current["version_id"]:
        raise ValueError("rapport OCR établi sur une autre version")
    originals = _image_paths()
    by_path = {row["path"].replace("\\", "/"): row for row in report["rows"]}
    if not set(originals).issubset(by_path):
        missing = sorted(set(originals) - set(by_path))
        raise ValueError(f"OCR incomplet : {len(missing)} images absentes, ex. {missing[:3]}")
    log = json.loads(LOG.read_text(encoding="utf-8"))["entries"] if LOG.is_file() else {}
    rows = []
    for relative, path in originals.items():
        record = by_path[relative]
        if hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256"):
            raise ValueError(f"original modifié après OCR: {relative}")
        if log.get(relative, {}).get("status") == "verified_removed":
            raise ValueError(f"original encore présent malgré état nettoyé: {relative}")
        proposal = record.get("identity_proposal") or {}
        reason = ("Proposition OCR unique refusée par la vérification finale"
                  if proposal.get("status") == "unique" else
                  "Plusieurs impressions compatibles avec les preuves OCR"
                  if proposal.get("status") == "ambiguous" else
                  "Aucune impression exacte prouvée après OCR et comparaison visuelle")
        rows.append({"path": relative, "bytes": path.stat().st_size,
                     "sha256": record["sha256"], "action": "impossible", "examined": True,
                     "reason": reason, "ocr_report": report_path.name,
                     "ocr_status": record.get("status")})
    return {"version_id": current["version_id"], "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = build_plan(args.report)
    plan_path = OPS_ROOT / "reports" / "impossible-plan-current.json"
    _write(plan_path, plan)
    if args.apply:
        print(json.dumps({"plan_rows": len(plan["rows"]), "result": apply_plan(plan_path)}, ensure_ascii=False))
    else:
        print(json.dumps({"plan_rows": len(plan["rows"]), "plan": str(plan_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
