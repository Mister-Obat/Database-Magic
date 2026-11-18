"""Read-only local OCR suggestions for unresolved Magic card images.

The output is a review aid, never an import plan: OCR text and fuzzy title
similarity do not prove a printing identity.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .paths import catalogue_root as default_catalogue_root
from .v2.publish import resolve_current

OPS_ROOT = Path(__file__).resolve().parents[3]
LIBRARY_ROOT = OPS_ROOT.parent / "Cartes Magic"
STATE_PATH = OPS_ROOT / "state" / "unknown-imported.json"
TESSERACT = Path(os.environ.get("MAGIC_LIBRARY_TESSERACT") or shutil.which("tesseract")
                 or Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                 / "Tesseract-OCR" / "tesseract.exe")
TESSDATA = OPS_ROOT / "tools" / "tessdata"
OCR_ALGORITHM_VERSION = "1.5.0"


def _normal(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_report(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _tessdata_langs(tessdata: Path) -> list[str]:
    return sorted(p.stem for p in tessdata.glob("*.traineddata")
                  if p.stem in {"eng", "fra"})


def _language_hint(path: Path) -> str | None:
    match = re.search(r"(?:^|[_\-.])(fr|en)(?:[_\-.]|$)", path.stem.casefold())
    return match.group(1) if match else None


def _collector_key(value: str) -> str:
    value = str(value).strip().upper()
    match = re.match(r"^0*(\d+)(.*)$", value)
    return (str(int(match.group(1))) + match.group(2)) if match else value


def _observed_set_codes(text: str, catalogue_codes: set[str]) -> tuple[set[str], set[str]]:
    upper = text.upper()
    explicit = {code for code in catalogue_codes if re.search(
        rf"(?<![A-Z0-9]){re.escape(code.upper())}(?![A-Z0-9])", upper)}
    inferred = set()
    # Some token prints show the parent expansion abbreviation (e.g. MH3)
    # beside a collector token prefixed with T; map only when the exact derived
    # code exists in this catalogue candidate set.
    if re.search(r"(?<![A-Z0-9])T\s*0*\d{1,4}(?![A-Z0-9])", upper):
        for abbreviation in re.findall(r"(?<![A-Z0-9])([A-Z0-9]{3,6})(?![A-Z0-9])", upper):
            if not re.search(r"[A-Z]", abbreviation) or not re.search(r"\d", abbreviation):
                continue
            derived = "t" + abbreviation.casefold()
            if derived in {code.casefold() for code in catalogue_codes}:
                inferred.update(code for code in catalogue_codes if code.casefold() == derived)
    return explicit | inferred, inferred


def _ocr_region(image: Image.Image, tesseract: Path, tessdata: Path,
                languages: list[str], timeout: int, psm: int = 6) -> tuple[str, float | None]:
    with tempfile.TemporaryDirectory(prefix="mtg-ocr-") as directory:
        input_path = Path(directory) / "region.png"
        image.save(input_path, format="PNG")
        args = [str(tesseract), str(input_path), "stdout", "--tessdata-dir",
                str(tessdata), "-l", "+".join(languages), "--psm", str(psm),
                "-c", "tessedit_create_tsv=1"]
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                check=False, encoding="utf-8", errors="replace",
                                env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Tesseract exit {result.returncode}")
    rows = csv.DictReader(io.StringIO(result.stdout), delimiter="\t")
    words, confidences = [], []
    for row in rows:
        token = (row.get("text") or "").strip()
        try:
            confidence = float(row.get("conf", "-1"))
        except ValueError:
            confidence = -1
        if token and confidence >= 0:
            words.append(token)
            confidences.append(confidence)
    return " ".join(words), round(sum(confidences) / len(confidences), 1) if confidences else None


def _ocr_image(path: Path, tesseract: Path, tessdata: Path, timeout: int) -> dict:
    if not tesseract.is_file():
        raise FileNotFoundError("Tesseract introuvable. Installez-le ou définissez MAGIC_LIBRARY_TESSERACT.")
    langs = _tessdata_langs(tessdata)
    if not langs:
        raise FileNotFoundError(f"aucun fichier eng/fra.traineddata dans {tessdata}")
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        width, height = image.size
        if width < 1 or height < 1:
            raise ValueError("image vide")
        # Two evidence-rich crops reduce decorative artwork noise while keeping
        # the whole card available for titles on rotated or unusual layouts.
        regions = {
            "full": image,
            "header": image.crop((0, 0, width, max(1, int(height * .27)))),
            "footer": image.crop((0, int(height * .76), width, height)),
            "title": image.crop((int(width * .02), 0,
                                 max(int(width * .02) + 1, int(width * .82)),
                                 max(1, int(height * .12)))),
        }
        texts, confidence = {}, {}
        for name, region in regions.items():
            if name == "title":
                scale = max(1, min(4, 1200 / region.width))
                if scale > 1:
                    region = region.resize((round(region.width * scale), round(region.height * scale)))
            elif max(region.size) < 900:
                scale = 900 / max(region.size)
                region = region.resize((round(region.width * scale), round(region.height * scale)))
            texts[name], confidence[name] = _ocr_region(region, tesseract, tessdata, langs, timeout,
                                                         psm=7 if name == "title" else 6)
    return {"language_models": langs, "text_by_region": texts,
            "confidence_by_region": confidence,
            "text": "\n".join(f"[{k}] {v}" for k, v in texts.items() if v)}


def _load_catalogue(database: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        prints = {row[0]: (row[1], row[2], row[3]) for row in conn.execute(
            "SELECT printing_id,set_id,collector_number,identity_status FROM printings")
            if row[3] in ("confirmed_multilang", "single_language")}
        sets = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT set_id,code,name FROM editions")}
        rows = []
        for printing_id, lang, name, printed_name in conn.execute(
                "SELECT printing_id,lang,name,printed_name FROM source_records"):
            printing = prints.get(printing_id)
            if not printing:
                continue
            set_id, collector_number, _identity = printing
            set_row = sets.get(set_id)
            if set_row:
                rows.append({"printing_id": printing_id, "code": set_row[0], "edition": set_row[1],
                    "collector_number": collector_number, "name": name,
                    "printed_name": printed_name, "lang": lang})
    finally:
        conn.close()
    return rows


def _load_catalogue_for_ocr(database: Path, ocr: dict, folder_code: str | None = None,
                            language_hint: str | None = None) -> list[dict]:
    """Use the catalogue FTS index to keep per-image matching bounded."""
    text = " ".join(ocr.get("text_by_region", {}).get(k, "") for k in ("title", "header", "full", "footer"))
    tokens = [token for token in dict.fromkeys(_normal(text).split()) if len(token) >= 4][:12]
    if not tokens:
        return []
    expression = " OR ".join(f'name : "{token}" OR printed_name : "{token}"' for token in tokens)
    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        known_codes = {str(row[0]).upper() for row in conn.execute("SELECT code FROM editions")}
        observed_codes, _ = _observed_set_codes(ocr.get("text_by_region", {}).get("footer", ""), known_codes)
        filter_codes = sorted(observed_codes) if observed_codes else ([folder_code.upper()] if folder_code else [])
        query = """SELECT DISTINCT p.printing_id,e.code,
            e.name AS edition,p.collector_number,s.name,s.printed_name,s.lang,s.provider_card_id,s.raw_json
            FROM source_search f JOIN source_records s ON s.provider_card_id=f.provider_card_id
            JOIN printings p USING(printing_id) JOIN editions e USING(set_id)
            WHERE source_search MATCH ? AND p.identity_status IN ('confirmed_multilang','single_language')
              AND s.lang IN ('fr','en')"""
        params: tuple = (expression,)
        if filter_codes:
            placeholders = ",".join("?" for _ in filter_codes)
            query += f" AND upper(e.code) IN ({placeholders})"
            params += tuple(filter_codes)
        preferred = language_hint if language_hint in {"fr", "en"} else "fr"
        query += " ORDER BY CASE s.lang WHEN ? THEN 0 ELSE 1 END LIMIT 5000"
        params += (preferred,)
        return [dict(row) for row in conn.execute(query, params)]
    finally:
        conn.close()


def _candidate_rows(ocr: dict, catalogue: list[dict], limit: int = 8,
                    folder_code: str | None = None) -> tuple[list[dict], dict]:
    full = ocr["text_by_region"].get("full", "")
    header = ocr["text_by_region"].get("title", "") + " " + ocr["text_by_region"].get("header", "")
    footer = ocr["text_by_region"].get("footer", "")
    combined = _normal(" ".join((full, header, footer)))
    tokens = set(combined.split())
    code_values, inferred_codes = _observed_set_codes(footer,
        {str(r["code"]).upper() for r in catalogue})
    code_matches = sorted(code_values)
    raw_numbers = set(re.findall(r"(?<![\w])\d{1,4}[A-Z]?(?![\w])", footer.upper()))
    numbers = sorted({_collector_key(number) for number in raw_numbers})
    by_printing: dict[str, dict] = {}
    for row in catalogue:
        key = row["printing_id"]
        candidate = by_printing.setdefault(key, {**row, "names": set(), "face_titles": {}})
        candidate["names"].update(n for n in (row["name"], row["printed_name"]) if n)
        try:
            faces = (json.loads(row.get("raw_json") or "{}") or {}).get("card_faces") or []
        except (TypeError, ValueError, json.JSONDecodeError):
            faces = []
        for face_index, face in enumerate(faces):
            face_title = str((face or {}).get("printed_name") or (face or {}).get("name") or "").strip()
            if face_title:
                candidate["names"].add(face_title)
                candidate["face_titles"].setdefault(_normal(face_title), face_index)
        candidate.setdefault("source_rows", []).append(row)
    ranked = []
    proven = []
    title_text = _normal(" ".join((ocr["text_by_region"].get("title", ""),
                                   ocr["text_by_region"].get("header", ""))))
    title_tokens = set(title_text.split())
    all_text = _normal(" ".join((full, header, footer)))
    artist_text = _normal(" ".join((full, footer)))
    for row in by_printing.values():
        best_title, title_score, overlap, best_face = "", 0.0, 0, 0
        code_hit = row["code"].upper() in code_matches
        number_hit = _collector_key(row["collector_number"]) in numbers
        for name in row["names"]:
            normal = _normal(name)
            name_tokens = set(normal.split())
            common = len(name_tokens & title_tokens)
            if not common:
                continue
            # Compare OCR title region, with token overlap to tolerate OCR noise.
            ratio = SequenceMatcher(None, normal, title_text).ratio()
            score = max(ratio, common / max(1, len(name_tokens)))
            if score > title_score:
                best_title, title_score, overlap = name, score, common
                best_face = row["face_titles"].get(normal, 0)
        folder_hit = bool(folder_code and row["code"].casefold() == folder_code.casefold())
        rank = title_score + (.45 if code_hit else 0) + (.35 if number_hit else 0) + (.2 if folder_hit else 0)
        if title_score >= .15 or code_hit or number_hit:
            artists = set()
            provider_ids = []
            languages = []
            for source in row["source_rows"]:
                provider_ids.append(source.get("provider_card_id"))
                languages.append(source.get("lang"))
                try:
                    data = json.loads(source.get("raw_json") or "{}")
                    artist = data.get("artist") if isinstance(data, dict) else None
                    if isinstance(artist, list):
                        artists.update(str(a.get("name", "") if isinstance(a, dict) else a) for a in artist)
                    elif artist:
                        artists.add(str(artist))
                except (TypeError, json.JSONDecodeError):
                    pass
            artist_hits = sorted(a for a in artists if _normal(a) and _normal(a) in artist_text)
            title_match = title_score >= .72 or overlap / max(1, len(_normal(best_title).split())) >= .8
            strict_proof = bool(code_hit and number_hit and title_match)
            explicit_matches = set(code_matches) - inferred_codes
            folder_proof = bool(folder_hit and title_match and artist_hits
                                and (not explicit_matches or row["code"].upper() in explicit_matches))
            identity_method = ("ocr_token_set_prefix_number_title" if strict_proof and row["code"].upper() in inferred_codes
                               else "printed_code_number_title" if strict_proof else (
                "confirmed_folder_title_artist" if folder_proof else None)
                               )
            if identity_method:
                proven.append({"printing_id": row["printing_id"], "set_code": row["code"],
                    "collector_number": str(row["collector_number"]), "catalogue_name": best_title,
                    "provider_card_id": next((p for p in provider_ids if p), None),
                    "face_index": best_face,
                    "language": next((lang for lang in languages if lang), None),
                    "identity_method": identity_method, "matched_artists": artist_hits,
                    "title_similarity": round(title_score, 3)})
            ranked.append((rank, row, best_title, title_score, code_hit, number_hit, overlap,
                           bool(artist_hits), identity_method, best_face))
    ranked.sort(key=lambda item: (-item[0], item[1]["code"], item[1]["collector_number"], item[1]["printing_id"]))
    results = []
    for rank, row, best_title, score, code_hit, number_hit, overlap, artist_hit, identity_method, best_face in ranked[:limit]:
        results.append({"printing_id": row["printing_id"], "set_code": row["code"],
            "edition": row["edition"], "collector_number": str(row["collector_number"]),
            "catalogue_name": best_title, "language": row["lang"], "face_index": best_face,
            "signals": {"title_similarity": round(score, 3), "title_token_overlap": overlap,
                        "set_code_seen_in_ocr": code_hit, "collector_number_seen_in_footer": number_hit,
                        "confirmed_folder_alias": bool(folder_code and row["code"].casefold() == folder_code.casefold()),
                        "artist_seen_in_ocr": artist_hit, "identity_method": identity_method},
            "rank_score": round(rank, 3)})
    unique_proven: dict[str, dict] = {row["printing_id"]: row for row in proven}
    proposals = list(unique_proven.values())
    evidence = {"set_codes": code_matches, "collector_number_tokens": numbers,
                "identity_proposal_status": "unique" if len(proposals) == 1 else (
                    "ambiguous" if len(proposals) > 1 else "none"),
                "identity_proposals": proposals,
                "identity_proven": len(proposals) == 1,
                "inferred_set_codes": sorted(inferred_codes),
                "warning": "Suggestions only; OCR and fuzzy name matches do not prove printing identity."}
    return results, evidence


def _title_variants(printed_name: str, name: str, raw_json: str) -> list[tuple[int, str]]:
    """Titles to match against the OCR title region, with their face index.

    The first entry is the printed source title (face 0 by convention); the
    per-face titles from ``card_faces`` let a verso scan prove the verso
    instead of always matching the front title.
    """
    variants: list[tuple[int, str]] = []
    seen: set[str] = set()
    for face_index, title in ((0, str(printed_name or name or "")), (0, str(name or ""))):
        title = title.strip()
        normal = _normal(title)
        if title and normal and normal not in seen:
            seen.add(normal)
            variants.append((face_index, title))
    try:
        faces = (json.loads(raw_json or "{}") or {}).get("card_faces") or []
    except (TypeError, ValueError, json.JSONDecodeError):
        faces = []
    for face_index, face in enumerate(faces):
        title = str((face or {}).get("printed_name") or (face or {}).get("name") or "").strip()
        normal = _normal(title)
        if title and normal and normal not in seen:
            seen.add(normal)
            variants.append((face_index, title))
    return variants


def propose_ocr(path: Path, conn: sqlite3.Connection, folder_alias: dict) -> dict:
    """Return a conservative identity proposal for a single image.

    A target requires a near-exact printed title plus either OCR set code and
    collector number, or a confirmed exact folder alias and OCR-matched artist.
    A folder alias alone is never enough to identify a printing.
    """
    ocr = _ocr_image(path, TESSERACT, TESSDATA, timeout=45)
    full = ocr["text_by_region"].get("full", "")
    header = ocr["text_by_region"].get("title", "") + " " + ocr["text_by_region"].get("header", "")
    footer = ocr["text_by_region"].get("footer", "")
    raw_text = "\n".join(x for x in (full, header, footer) if x)
    title_text = _normal(" ".join((ocr["text_by_region"].get("title", ""),
                                   ocr["text_by_region"].get("header", ""))))
    cropped_title = _normal(ocr["text_by_region"].get("title", ""))
    all_text = _normal(raw_text)
    artist_text = _normal(" ".join((full, footer)))
    set_rows = conn.execute("SELECT set_id,code,name FROM editions").fetchall()
    code_to_set = {str(row[1]).upper(): str(row[0]) for row in set_rows}
    ocr_codes, inferred_codes = _observed_set_codes(footer,
        {code.upper() for code in code_to_set})
    explicit_codes = ocr_codes - inferred_codes
    alias_code = None
    parent_name = path.parent.name
    alias_value = (folder_alias if folder_alias.get("folder") == parent_name
                   else folder_alias.get(parent_name)) if isinstance(folder_alias, dict) else None
    if isinstance(alias_value, dict) and alias_value.get("status") == "confirmed_exact_candidate":
        alias_code = str(alias_value.get("code") or "").upper()
    elif isinstance(alias_value, str) and alias_value.upper() in code_to_set:
        alias_code = alias_value.upper()
    codes = ocr_codes | ({alias_code} if alias_code in code_to_set else set())
    set_ids = sorted({code_to_set[code] for code in codes if code in code_to_set})
    numbers = {_collector_key(number) for number in
               re.findall(r"(?<![\w])\d{1,4}[A-Z]?(?![\w])", footer.upper())}
    possible = []
    ocr_tokens = set(all_text.split())
    if set_ids:
        placeholders = ",".join("?" for _ in set_ids)
        preferred = _language_hint(path) or "fr"
        for row in conn.execute(f"""SELECT p.printing_id,p.collector_number,e.code,e.name,
            s.provider_card_id,s.name,s.printed_name,s.lang,s.raw_json
            FROM printings p JOIN editions e USING(set_id)
            JOIN source_records s USING(printing_id)
            WHERE p.set_id IN ({placeholders}) AND s.lang IN ('fr','en')
            ORDER BY CASE s.lang WHEN ? THEN 0 ELSE 1 END""", (*set_ids, preferred)):
            collector = str(row[1]).upper()
            artist_names = []
            try:
                source_record = json.loads(row[8] or "{}")
                parsed_artists = source_record.get("artist", []) if isinstance(source_record, dict) else []
                if isinstance(parsed_artists, list):
                    artist_names = [str(a.get("name", "") if isinstance(a, dict) else a)
                                    for a in parsed_artists]
                elif isinstance(parsed_artists, str):
                    artist_names = [parsed_artists]
            except (TypeError, json.JSONDecodeError):
                pass
            artist_match = any((_normal(artist) and _normal(artist) in artist_text)
                               for artist in artist_names)
            number_match = _collector_key(collector) in numbers
            # The title crop helps older cards where the collector number is absent.
            # Each printed title variant carries its face index so a verso scan
            # proves the verso of the printing instead of the default front.
            best = None
            for face_index, title in _title_variants(row[6], row[5], row[8]):
                norm_name = _normal(title)
                title_score = max(SequenceMatcher(None, norm_name, title_text).ratio(),
                                  SequenceMatcher(None, norm_name, all_text).ratio())
                title_crop_score = SequenceMatcher(None, norm_name, cropped_title).ratio()
                token_coverage = len(set(norm_name.split()) & ocr_tokens) / max(1, len(norm_name.split()))
                variant = {"face_index": face_index, "name": title,
                           "title_score": round(max(title_score, token_coverage), 3),
                           "title_crop_score": round(title_crop_score, 3),
                           "title_match": title_score >= .72 or token_coverage >= .8}
                if best is None or (variant["title_crop_score"], variant["title_score"]) > \
                        (best["title_crop_score"], best["title_score"]):
                    best = variant
            if best is None:
                continue
            strict_proof = bool(str(row[2]).upper() in ocr_codes and best["title_match"] and number_match)
            folder_artist_proof = bool(alias_code and alias_code == str(row[2]).upper()
                                       and best["title_match"] and artist_match
                                       and (not explicit_codes or alias_code in explicit_codes))
            if strict_proof or folder_artist_proof:
                possible.append({"printing_id": row[0], "collector_number": str(row[1]),
                    "set_code": row[2], "edition": row[3], "provider_card_id": row[4],
                    "name": best["name"], "face_index": best["face_index"], "language": row[7],
                    "title_score": best["title_score"],
                    "title_crop_score": best["title_crop_score"],
                    "identity_method": ("ocr_token_set_prefix_number_title" if strict_proof and str(row[2]).upper() in inferred_codes
                                       else "printed_code_number_title") if strict_proof
                                       else "confirmed_folder_title_artist"})
    # Collapse multilingual source rows, but preserve distinct printings as ambiguity.
    grouped: dict[str, list[dict]] = defaultdict(list)
    for candidate in possible:
        grouped[candidate["printing_id"]].append(candidate)
    if len(grouped) == 1:
        variants = next(iter(grouped.values()))
        chosen = max(variants, key=lambda c: c["title_crop_score"])
        precise_languages = {c["language"] for c in variants if c["title_crop_score"] >= .9}
        return {"status": "unique", "target": {"provider_card_id": chosen["provider_card_id"],
            "printing_id": chosen["printing_id"], "set_code": chosen["set_code"],
            "collector_number": chosen["collector_number"],
            "face_index": chosen["face_index"], "language": chosen["language"],
            "title": chosen["name"]}, "evidence": {"ocr_text": raw_text,
            "ocr_confidence": ocr["confidence_by_region"], "set_codes": sorted(codes),
            "collector_numbers": sorted(numbers), "title_score": chosen["title_score"],
            "title_crop_score": chosen["title_crop_score"],
            "ocr_title": ocr["text_by_region"].get("title", ""),
            "language_proven": precise_languages == {chosen["language"]},
            "inferred_set_codes": sorted(inferred_codes),
            "folder_alias_used": alias_code is not None, "identity_method": chosen["identity_method"],
            "artist_match": chosen["identity_method"] == "confirmed_folder_title_artist",
            "identity_proven": True}}
    return {"status": "ambiguous" if grouped else "none", "target": None,
        "candidates": [c for values in grouped.values() for c in values],
        "evidence": {"ocr_text": raw_text, "ocr_confidence": ocr["confidence_by_region"],
            "set_codes": sorted(codes), "collector_numbers": sorted(numbers),
            "ocr_title": ocr["text_by_region"].get("title", ""),
            "folder_alias_used": alias_code is not None, "identity_proven": False}}


def generate_candidates(library_root: Path = LIBRARY_ROOT, catalogue_root: Path | None = None,
                        output_path: Path | None = None, *, tesseract: Path = TESSERACT,
                        tessdata: Path = TESSDATA, limit: int = 0,
                        timeout: int = 45, resume: bool = True, workers: int = 4) -> dict:
    """OCR every remaining image under the historical library, excluding Impossible."""
    catalogue_root = catalogue_root if catalogue_root is not None else default_catalogue_root(OPS_ROOT)
    current = resolve_current(catalogue_root)
    database = current["database"]
    version_id = current["version_id"]
    if not tesseract.is_file():
        raise FileNotFoundError(f"Tesseract introuvable: {tesseract}")
    if not database.is_file():
        raise FileNotFoundError(f"Base active introuvable: {database}")
    extensions = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
    files = sorted((p for p in library_root.rglob("*") if p.is_file()
                    and p.suffix.casefold() in extensions
                    and "impossible" not in {part.casefold() for part in p.relative_to(library_root).parts}),
                   key=lambda p: str(p).casefold())
    alias_path = OPS_ROOT / "reports" / "edition_aliases.json"
    aliases = json.loads(alias_path.read_text(encoding="utf-8")).get("aliases", []) if alias_path.is_file() else []
    aliases_by_folder = {entry.get("folder"): entry for entry in aliases if entry.get("folder")}
    file_map = {p.relative_to(library_root).as_posix(): p for p in files}
    saved_rows = []
    if resume and output_path and output_path.is_file():
        try:
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            if (saved.get("algorithm_version") == OCR_ALGORITHM_VERSION
                    and saved.get("catalogue_version_id") == version_id
                    and saved.get("library_root") == str(library_root.resolve())):
                saved_rows = [row for row in saved.get("rows", []) if row.get("path") in file_map]
        except (OSError, ValueError, TypeError):
            saved_rows = []
    saved_by_path = {row["path"]: row for row in saved_rows if isinstance(row, dict) and row.get("path")}
    for relative, row in list(saved_by_path.items()):
        path = file_map[relative]
        try:
            unchanged = row.get("sha256") and _sha256(path) == row["sha256"]
        except OSError:
            unchanged = False
        if not unchanged:
            del saved_by_path[relative]
    rows = list(saved_by_path.values())
    pending = [(relative, path) for relative, path in file_map.items() if relative not in saved_by_path]
    selected = pending[:limit] if limit else pending
    def recognize(item: tuple[str, Path]) -> tuple[str, Path, str | None, dict | None, str | None]:
        relative, path = item
        try:
            digest = _sha256(path)
        except OSError as exc:
            return relative, path, None, None, str(exc)
        try:
            return relative, path, digest, _ocr_image(path, tesseract, tessdata, timeout), None
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, UnidentifiedImageError) as exc:
            return relative, path, digest, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
      for relative, path, digest, ocr, ocr_error in pool.map(recognize, selected):
        if digest is None:
            rows.append({"path": relative, "status": "source_missing", "error": ocr_error, "candidates": []})
            if output_path and len(rows) % 50 == 0:
                _write_report(output_path, _report_object(rows, len(files), library_root, tesseract, version_id))
            continue
        row = {"path": relative, "sha256": digest, "status": "ocr_suggestions_only", "candidates": []}
        try:
            if ocr_error:
                row.update({"status": "ocr_error", "error": ocr_error})
                rows.append(row)
                if output_path and len(rows) % 50 == 0:
                    _write_report(output_path, _report_object(rows, len(files), library_root, tesseract, version_id))
                continue
            assert ocr is not None
            alias = aliases_by_folder.get(path.parent.name, {})
            candidate_codes = {c.get("set_code") for c in alias.get("candidates", [])}
            folder_code = next(iter(candidate_codes)) if alias.get("status") == "confirmed_exact_candidate" and len(candidate_codes) == 1 else None
            language_hint = _language_hint(path)
            catalogue = _load_catalogue_for_ocr(database, ocr, folder_code, language_hint)
            candidates, evidence = _candidate_rows(ocr, catalogue, folder_code=folder_code)
            evidence["folder_alias"] = {"status": alias.get("status"), "code": folder_code,
                                         "source": "edition_aliases.json" if alias else None}
            row.update({"ocr": ocr, "evidence": evidence, "candidates": candidates,
                        "status": evidence["identity_proposal_status"] if evidence["identity_proven"]
                                  else ("suggestions" if candidates else "no_candidate")})
            if evidence["identity_proposal_status"] == "unique":
                chosen = evidence["identity_proposals"][0]
                row["identity_proposal"] = {"status": "unique", "target": {
                    "provider_card_id": chosen["provider_card_id"], "printing_id": chosen["printing_id"],
                    "set_code": chosen["set_code"], "collector_number": chosen["collector_number"],
                    "face_index": int(chosen.get("face_index") or 0),
                    "language": chosen["language"]}, "evidence": chosen}
            elif evidence["identity_proposal_status"] == "ambiguous":
                row["identity_proposal"] = {"status": "ambiguous", "target": None,
                                             "candidates": evidence["identity_proposals"]}
            else:
                row["identity_proposal"] = {"status": "none", "target": None}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, UnidentifiedImageError) as exc:
            row.update({"status": "ocr_error", "error": f"{type(exc).__name__}: {exc}"})
        rows.append(row)
        if output_path and len(rows) % 50 == 0:
            _write_report(output_path, _report_object(rows, len(files), library_root, tesseract, version_id))
    result = _report_object(rows, len(files), library_root, tesseract, version_id)
    if output_path:
        _write_report(output_path, result)
    return result


def _report_object(rows: list[dict], total_files: int, library_root: Path,
                   tesseract: Path, version_id: str) -> dict:
    return {"schema_version": "1.0.0", "algorithm_version": OCR_ALGORITHM_VERSION,
        "catalogue_version_id": version_id,
        "library_root": str(library_root.resolve()), "tesseract": str(tesseract),
        "read_only": True, "identity_proven": False,
        "processed_count": len(rows), "remaining_file_count": total_files, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=LIBRARY_ROOT)
    parser.add_argument("--catalogue-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0, help="Maximum unresolved files (0 = all)")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--tesseract", type=Path, default=TESSERACT)
    parser.add_argument("--tessdata", type=Path, default=TESSDATA)
    parser.add_argument("--no-resume", action="store_true", help="Ignore an existing partial report")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent Tesseract workers (max 4)")
    args = parser.parse_args()
    result = generate_candidates(args.library, args.catalogue_root, args.output,
        tesseract=args.tesseract, tessdata=args.tessdata, limit=args.limit, timeout=args.timeout,
        resume=not args.no_resume, workers=args.workers)
    counts: dict[str, int] = defaultdict(int)
    for row in result["rows"]:
        counts[row["status"]] += 1
    print(json.dumps({"processed": result["processed_count"], "remaining": result["remaining_file_count"],
                      "statuses": dict(counts), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
