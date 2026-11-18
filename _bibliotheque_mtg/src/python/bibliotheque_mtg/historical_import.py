"""Resume verified historical images into the active catalogue without moving originals."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .image_download import import_local_image
from .paths import catalogue_root
from .v2.publish import resolve_current

OPS_ROOT = Path(__file__).resolve().parents[3]
CATALOGUE_ROOT = catalogue_root(OPS_ROOT)
LIBRARY_ROOT = OPS_ROOT.parent / "Cartes Magic"
STATE_PATH = OPS_ROOT / "state" / "historical-archive-resolved.json"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _checked_source(row: dict) -> bytes:
    relative = Path(row["path"])
    root = LIBRARY_ROOT.resolve()
    source = root / relative
    if source.is_symlink():
        raise ValueError("chemin d'archive invalide")
    path = source.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("chemin d'archive invalide")
    size = path.stat().st_size
    if size != row["bytes"] or size > 20 * 1024 * 1024:
        raise ValueError("taille d'archive modifiée ou excessive")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError("SHA-256 d'archive modifié")
    return data


def apply_plan(plan_path: Path, *, dry_run: bool = False, limit: int = 0) -> dict:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    current = resolve_current(CATALOGUE_ROOT)
    if plan.get("version_id") != current["version_id"]:
        raise ValueError("le plan ne correspond pas à la version active")
    if STATE_PATH.is_file():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if state.get("version_id") != current["version_id"]:
            # Resolved archive paths stay resolved across catalogue publications.
            # New target identities are checked against the active database below.
            previous = state.get("previous_version_ids", [])
            old = state.get("version_id")
            if old and old not in previous:
                previous.append(old)
            state["previous_version_ids"] = previous
            state["version_id"] = current["version_id"]
    else:
        state = {"version_id": current["version_id"], "entries": {}}
    inventory_name = plan.get("inventory_report")
    if inventory_name:
        report_root = (OPS_ROOT / "reports").resolve()
        inventory_path = (report_root / inventory_name).resolve()
        if not inventory_path.is_relative_to(report_root) or inventory_path.suffix != ".json":
            raise ValueError("rapport d'inventaire invalide")
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        originals = {r["path"]: r for r in inventory["files"]}
    else:
        originals = {r["relative_path"]: r for r in json.loads(
            (OPS_ROOT / "reports" / "v2-t4.1-historical-unidentified-index.json").read_text(encoding="utf-8"))["entries"]}
    counts = {"imported": 0, "already_ready": 0, "already_resolved": 0, "rejected": 0}
    selected = plan["rows"][:limit or None]
    for row in selected:
        path = row["path"]
        if path in state["entries"]:
            counts["already_resolved"] += 1
            continue
        try:
            if path not in originals or originals[path]["sha256"] != row["sha256"]:
                raise ValueError("archive absente de l'index de référence")
            if row.get("language") not in {"fr", "en"} or not row.get("provider_card_id"):
                raise ValueError("langue ou identifiant cible absent")
            if not set(row.get("methods") or []) & {"printed_code_title", "ocr_collector_title_folder", "confirmed_folder_ocr_title"}:
                raise ValueError("preuve d'identité insuffisante")
            data = _checked_source(row)
            with sqlite3.connect(current["database"]) as conn:
                target = conn.execute("""SELECT p.printing_id,e.code,p.collector_number,s.status
                    FROM source_records sr JOIN printings p USING(printing_id)
                    JOIN editions e USING(set_id)
                    JOIN image_slots s ON s.printing_id=p.printing_id AND s.face_index=0
                    WHERE sr.provider_card_id=?""", (row["provider_card_id"],)).fetchone()
            if not target or tuple(map(str, target[:3])) != tuple(map(str, (row["printing_id"], row["set_code"], row["collector_number"]))):
                raise ValueError("l'identité de la cible a changé")
            status = "already_ready" if target[3] == "ready" else "imported"
            if not dry_run and status == "imported":
                import_local_image(row["provider_card_id"], 0, data,
                    catalogue_root=CATALOGUE_ROOT, language=row["language"],
                    source_provider="historical_import", only_missing=True,
                    source_evidence="SHA-256 original " + row["sha256"] + "; " + ",".join(row["methods"]))
            counts[status] += 1
            if not dry_run:
                state["entries"][path] = {"sha256": row["sha256"], "printing_id": row["printing_id"],
                    "face_index": 0, "status": status, "methods": row["methods"]}
                if (counts["imported"] + counts["already_ready"]) % 25 == 0:
                    _atomic_json(STATE_PATH, state)
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            counts["rejected"] += 1
            print(f"REJECTED {path}: {exc}", flush=True)
        if sum(counts.values()) % 50 == 0:
            print(f"PROGRESS {sum(counts.values())}/{len(selected)} {counts}", flush=True)
    if not dry_run:
        _atomic_json(STATE_PATH, state)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--apply", action="store_true", help="Publish validated missing images")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(apply_plan(args.plan, dry_run=not args.apply, limit=args.limit), ensure_ascii=False))


if __name__ == "__main__":
    main()
