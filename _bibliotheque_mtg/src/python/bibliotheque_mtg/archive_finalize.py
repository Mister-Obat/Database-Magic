"""Finalize individually verified historical cards and record unresolved originals."""
from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import json
import os
import re
import shutil
import sqlite3
import unicodedata
from pathlib import Path

from PIL import Image

from .image_download import import_local_image
from .organized_images import mirror_printing_face
from .paths import catalogue_root
from .v2.publish import resolve_current

OPS_ROOT = Path(__file__).resolve().parents[3]
ROOT = OPS_ROOT.parent / "Cartes Magic"
CATALOGUE = catalogue_root(OPS_ROOT)
LOG = OPS_ROOT / "state" / "archive-finalization.json"
RESOLVED = OPS_ROOT / "state" / "historical-archive-resolved.json"
IMPOSSIBLE = ROOT / "Impossible"


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in text if not unicodedata.combining(c)).casefold().strip()


def _source(row: dict) -> tuple[Path, bytes]:
    relative = Path(row["path"])
    root = ROOT.resolve()
    source = root / relative
    if (relative.is_absolute() or ".." in relative.parts or source.is_symlink()
            or (relative.parts and relative.parts[0].casefold() == "impossible")):
        raise ValueError("chemin d'archive invalide")
    path = source.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("chemin d'archive invalide")
    data = path.read_bytes()
    if len(data) != row["bytes"] or len(data) > 20 * 1024 * 1024:
        raise ValueError("taille d'archive invalide")
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError("SHA-256 d'archive modifié")
    return path, data


def _target(conn: sqlite3.Connection, row: dict) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    target = conn.execute("""SELECT p.printing_id,p.collector_number,e.code,
        s.face_index,s.status,s.asset_id,s.selected_language,
        sr.provider_card_id,sr.printed_name,sr.name,sr.lang,sr.raw_json
        FROM source_records sr JOIN printings p USING(printing_id)
        JOIN editions e USING(set_id)
        JOIN image_slots s ON s.printing_id=p.printing_id AND s.face_index=?
        WHERE sr.provider_card_id=?""",
        (row.get("face_index", 0), row["provider_card_id"])).fetchone()
    if target is None or target["printing_id"] != row["printing_id"]:
        raise ValueError("impression ou face absente du catalogue actif")
    if target["code"].casefold() != row["set_code"].casefold() or str(target["collector_number"]) != str(row["collector_number"]):
        raise ValueError("code ou numéro différent de la cible")
    return target


def _proof(conn: sqlite3.Connection, row: dict, target: sqlite3.Row) -> None:
    proof = row.get("proof") or {}
    kind = proof.get("kind")
    if kind in {"ocr", "ocr_face"}:
        target_title = target["printed_name"] or target["name"]
        faces = json.loads(target["raw_json"]).get("card_faces") or []
        if kind == "ocr" and faces and target["face_index"] == 0:
            face_title = faces[0].get("printed_name") or faces[0].get("name")
            if _norm(proof.get("title", "")) == _norm(face_title):
                target_title = face_title
        if kind == "ocr_face":
            if target["face_index"] >= len(faces):
                raise ValueError("face absente de la source")
            target_title = faces[target["face_index"]].get("printed_name") or faces[target["face_index"]].get("name")
        if _norm(proof.get("title", "")) != _norm(target_title):
            raise ValueError("titre OCR différent de la cible")
        if str(proof.get("collector_number", "")) != str(target["collector_number"]):
            raise ValueError("numéro OCR différent de la cible")
        if proof.get("set_code", "").casefold() != target["code"].casefold():
            raise ValueError("code OCR différent de la cible")
        if row.get("language") not in {"fr", "en"} or target["lang"] != row["language"]:
            raise ValueError("langue OCR différente de la source")
        raw = _norm(proof.get("ocr_raw", ""))
        code = target["code"].casefold()
        code_seen = bool(re.search(r"(?<![a-z0-9])" + re.escape(code) + r"(?![a-z0-9])", raw))
        if kind == "ocr_face" and not code_seen:
            aliases = json.loads((OPS_ROOT / "reports" / "edition_aliases.json").read_text(encoding="utf-8"))
            folder = Path(row["path"]).parts[0]
            alias = next((item for item in aliases["aliases"] if item["folder"] == folder), None)
            code_seen = bool(alias and alias.get("status") == "confirmed_exact_candidate"
                             and len(alias.get("candidates", [])) == 1
                             and alias["candidates"][0]["set_code"].casefold() == code)
        if target["code"].casefold().startswith("t") and re.search(
                r"(?<![a-z0-9])" + re.escape(code[1:]) + r"(?![a-z0-9])", raw):
            if re.search(r"(?<![A-Z0-9])T\s*0*\d{1,4}(?![A-Z0-9])", proof.get("ocr_raw", "").upper()):
                code_seen = True
        numbers = {str(int(match)) for match in re.findall(r"(?<!\d)(\d{1,4})(?!\d)", raw)}
        ocr_title = _norm(proof.get("ocr_title", ""))
        title_score = SequenceMatcher(None, _norm(proof.get("title", "")), ocr_title).ratio() if ocr_title else 0.0
        title_seen = (title_score >= .72 or _norm(proof.get("title", "")) in raw or
                      (kind == "ocr_face" and re.sub(r"[^a-z0-9]", "", _norm(proof.get("title", "")))
                       in re.sub(r"[^a-z0-9]", "", raw)))
        if (not title_seen
                or not code_seen
                or str(target["collector_number"]) not in numbers):
            raise ValueError("texte OCR brut ne prouve pas titre/code/numéro")
    elif kind == "ocr_code_title":
        footer = proof.get("ocr_footer", "").upper()
        code = target["code"].upper()
        if not re.search(r"(?<![A-Z0-9])" + re.escape(code) + r"(?![A-Z0-9])", footer):
            raise ValueError("code d'édition absent du pied de carte")
        if target["face_index"] != 0 or target["lang"] != row.get("language"):
            raise ValueError("face ou langue invalide")
        target_title = _norm(target["printed_name"] or target["name"])
        if _norm(proof.get("title", "")) != target_title:
            raise ValueError("titre cible différent")
        if SequenceMatcher(None, target_title, _norm(proof.get("ocr_title", ""))).ratio() < .78:
            raise ValueError("titre OCR insuffisant")
        count = conn.execute("""SELECT COUNT(DISTINCT p.printing_id) FROM printings p
            JOIN editions e USING(set_id) JOIN source_records sr USING(printing_id)
            WHERE e.code=? AND sr.lang=?
            AND COALESCE(NULLIF(sr.printed_name,''),sr.name)=?""",
            (target["code"], row["language"], target["printed_name"] or target["name"])).fetchone()[0]
        if count != 1:
            raise ValueError("titre non unique dans l'édition")
    elif kind == "folder_title":
        folder = Path(row["path"]).parts[0]
        special = {"Phyrexian (2023)": "nph", "Kamigawa (2004)": "neo"}
        if folder in special:
            allowed_code = special[folder]
        else:
            aliases = json.loads((OPS_ROOT / "reports" / "edition_aliases.json").read_text(encoding="utf-8"))
            alias = next((item for item in aliases["aliases"] if item["folder"] == folder), None)
            allowed_code = (alias["candidates"][0]["set_code"] if alias
                            and alias.get("status") == "confirmed_exact_candidate"
                            and len(alias.get("candidates", [])) == 1 else None)
        if not allowed_code or target["code"].casefold() != allowed_code.casefold():
            raise ValueError("lot historique non calibré")
        if target["face_index"] != 0 or target["lang"] != row.get("language") or row.get("language") not in {"fr", "en"}:
            raise ValueError("face ou langue invalide")
        target_title = _norm(target["printed_name"] or target["name"])
        if _norm(proof.get("title", "")) != target_title:
            raise ValueError("titre cible différent")
        ocr_title = _norm(proof.get("ocr_title", ""))
        if SequenceMatcher(None, target_title, ocr_title).ratio() < .78:
            raise ValueError("titre OCR insuffisant")
        count = conn.execute("""SELECT COUNT(DISTINCT p.printing_id) FROM printings p
            JOIN editions e USING(set_id) JOIN source_records sr USING(printing_id)
            WHERE e.code=? AND sr.lang=?
            AND COALESCE(NULLIF(sr.printed_name,''),sr.name)=?""",
            (allowed_code, row["language"], target["printed_name"] or target["name"])).fetchone()[0]
        if count != 1:
            raise ValueError("titre non unique dans l'édition")
    elif kind == "ocr_artist":
        aliases = json.loads((OPS_ROOT / "reports" / "edition_aliases.json").read_text(encoding="utf-8"))
        folder = Path(row["path"]).parts[0]
        alias = next((item for item in aliases["aliases"] if item["folder"] == folder), None)
        if (not proof.get("folder_confirmed") or not alias
                or alias.get("status") != "confirmed_exact_candidate"
                or len(alias.get("candidates", [])) != 1
                or alias["candidates"][0]["set_code"].casefold() != target["code"].casefold()):
            raise ValueError("dossier d'édition non confirmé")
        if _norm(proof.get("title", "")) != _norm(target["printed_name"] or target["name"]):
            raise ValueError("titre OCR différent de la cible")
        if proof.get("set_code", "").casefold() != target["code"].casefold():
            raise ValueError("édition différente de la cible")
        if row.get("language") not in {"fr", "en"} or target["lang"] != row["language"]:
            raise ValueError("langue OCR différente de la source")
        artist = str(json.loads(target["raw_json"]).get("artist") or "")
        if not artist or _norm(proof.get("artist", "")) != _norm(artist):
            raise ValueError("artiste OCR différent de la cible")
        raw = _norm(proof.get("ocr_raw", ""))
        if _norm(proof.get("title", "")) not in raw or _norm(artist) not in raw:
            raise ValueError("texte OCR brut ne prouve pas titre/artiste")
        count = conn.execute("""SELECT COUNT(DISTINCT p.printing_id)
            FROM source_records sr JOIN printings p USING(printing_id)
            JOIN editions e USING(set_id)
            WHERE e.code=? AND COALESCE(NULLIF(sr.printed_name,''),sr.name)=?
            AND json_extract(sr.raw_json,'$.artist')=?""",
            (target["code"], target["printed_name"] or target["name"], artist)).fetchone()[0]
        if count != 1:
            raise ValueError("titre/édition/artiste non uniques")
    elif kind == "visual":
        if target["status"] != "ready" or proof.get("asset_id") != target["asset_id"]:
            raise ValueError("l'asset de référence n'est plus prêt")
        if proof.get("report") not in {"visual-verified-20.json", "visual-reviewed-next-interim-20.json"}:
            raise ValueError("correspondance visuelle non corroborée par revue d'identité")
        if not proof.get("whole_match") or not proof.get("identity_region_match") or not proof.get("unique"):
            raise ValueError("preuve visuelle incomplète")
    else:
        raise ValueError("type de preuve absent")


def _ready(db: Path, target: sqlite3.Row) -> dict:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        slot = conn.execute("""SELECT s.status,s.asset_id,a.relative_path,a.width,a.height
            FROM image_slots s LEFT JOIN image_assets a USING(asset_id)
            WHERE s.printing_id=? AND s.face_index=?""",
            (target["printing_id"], target["face_index"])).fetchone()
    if not slot or slot["status"] != "ready" or not slot["asset_id"] or (slot["width"], slot["height"]) != (531, 738):
        raise ValueError("image organisée non prête")
    mirror = mirror_printing_face(db, db.parent, target["printing_id"], target["face_index"])
    data = mirror.read_bytes()
    if hashlib.sha256(data).hexdigest() != slot["asset_id"].rsplit("_", 1)[-1]:
        raise ValueError("hash du WebP organisé invalide")
    with Image.open(mirror) as image:
        image.load()
        if image.format != "WEBP" or image.size != (531, 738):
            raise ValueError("WebP organisé invalide")
    return {"asset_id": slot["asset_id"], "organized": str(mirror), "asset_sha256": hashlib.sha256(data).hexdigest()}


def apply_plan(plan_path: Path) -> dict:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    current = resolve_current(CATALOGUE)
    if plan.get("version_id") != current["version_id"]:
        raise ValueError("plan établi sur une autre version du catalogue")
    log = json.loads(LOG.read_text(encoding="utf-8")) if LOG.is_file() else {"entries": {}}
    resolved = json.loads(RESOLVED.read_text(encoding="utf-8")) if RESOLVED.is_file() else {"version_id": current["version_id"], "entries": {}}
    results = {"verified_removed": 0, "impossible_moved": 0, "already_done": 0, "rejected": 0}
    for row in plan["rows"]:
        key = row["path"].replace("\\", "/")
        prior = log["entries"].get(key)
        if prior and prior.get("status") in {"verified_removed", "impossible"}:
            results["already_done"] += 1
            continue
        try:
            if prior and prior.get("status") in {"ready_verified", "pending_impossible"}:
                destination = ROOT / prior.get("destination", "") if prior.get("destination") else None
                if not (ROOT / key).exists() and (prior["status"] == "ready_verified" or
                        (destination and destination.is_file() and
                         hashlib.sha256(destination.read_bytes()).hexdigest() == row["sha256"])):
                    prior["status"] = "verified_removed" if prior["status"] == "ready_verified" else "impossible"
                    _write(LOG, log)
                    resolved["entries"][key] = {"sha256": row["sha256"], "status": prior["status"],
                                                  "printing_id": prior.get("printing_id"), "face_index": prior.get("face_index")}
                    resolved["version_id"] = current["version_id"]
                    _write(RESOLVED, resolved)
                    results["already_done"] += 1
                    continue
            source, data = _source(row)
            if row.get("action") == "impossible":
                reason = row.get("reason", "").strip()
                if not reason or not row.get("examined"):
                    raise ValueError("motif ou examen absent")
                destination = IMPOSSIBLE / key
                if destination.exists():
                    raise ValueError("destination Impossible déjà occupée")
                destination.parent.mkdir(parents=True, exist_ok=True)
                entry = {"status": "pending_impossible", "sha256": row["sha256"],
                         "destination": str(destination.relative_to(ROOT)).replace("\\", "/"), "reason": reason}
                log["entries"][key] = entry
                _write(LOG, log)
                shutil.move(str(source), str(destination))
                entry["status"] = "impossible"
                results["impossible_moved"] += 1
            elif row.get("action") == "verify":
                with sqlite3.connect(current["database"]) as conn:
                    target = _target(conn, row)
                    _proof(conn, row, target)
                if target["status"] != "ready":
                    if row["proof"]["kind"] not in {"ocr", "ocr_face", "ocr_artist", "folder_title", "ocr_code_title"}:
                        raise ValueError("import sans preuve OCR")
                    import_local_image(row["provider_card_id"], target["face_index"], data,
                        catalogue_root=CATALOGUE, language=row["language"],
                        source_provider="historical_import", only_missing=True,
                        source_evidence="SHA-256 original " + row["sha256"] + "; OCR code/numéro/titre")
                ready = _ready(current["database"], target)
                # Record the verified publication before removing its only original.
                entry = {"status": "ready_verified", "sha256": row["sha256"],
                         "printing_id": target["printing_id"], "face_index": target["face_index"],
                         "proof": row["proof"], **ready}
                log["entries"][key] = entry
                _write(LOG, log)
                source.unlink()
                entry["status"] = "verified_removed"
                results["verified_removed"] += 1
            else:
                raise ValueError("action inconnue")
            log["entries"][key] = entry
            _write(LOG, log)
            resolved["entries"][key] = {"sha256": row["sha256"], "status": entry["status"],
                                          "printing_id": entry.get("printing_id"), "face_index": entry.get("face_index")}
            resolved["version_id"] = current["version_id"]
            _write(RESOLVED, resolved)
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            print(f"REJECTED {key}: {exc}", flush=True)
            results["rejected"] += 1
    impossible_entries = {path: entry for path, entry in log["entries"].items()
                          if entry.get("status") == "impossible"}
    if impossible_entries:
        _write(IMPOSSIBLE / "index.json", {"entries": impossible_entries})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    print(json.dumps(apply_plan(args.plan), ensure_ascii=False))


if __name__ == "__main__":
    main()
