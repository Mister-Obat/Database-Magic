"""Conservative local visual proposals for unresolved archive images.

Visual similarity is evidence for review, never an identity on its own. A result
is ``visual_candidate_for_review`` only when one ready slot passes all pixel and
card-zone limits and has a clear lead over every other candidate. Only files
whose content hash agrees with the active catalogue's asset_id are indexed.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError

from .organized_images import _safe_name


# Strict pixel error limits after orientation correction and same-size resize.
# These are deliberately much tighter than perceptual-hash duplicate thresholds.
MAX_GLOBAL_MAE = 0.035
MAX_ZONE_MAE = 0.055
MIN_LEAD = 0.008
MAX_DHASH_DISTANCE = 14
# A second, explicitly non-identity tier for human review only.
SUGGEST_MAX_GLOBAL_MAE = 0.050
SUGGEST_MAX_ZONE_MAE = 0.080
SUGGEST_MIN_LEAD = 0.008
SUGGEST_MAX_DHASH_DISTANCE = 18
_SIZE = (128, 178)
_ZONES = {
    "title": (0.035, 0.055, 0.22, 0.31),
    "set_symbol": (0.72, 0.78, 0.94, 0.91),
    "collector_number": (0.69, 0.92, 0.98, 0.985),
}


def archive_relative_path(path: Path, archive_root: Path) -> str:
    """Return a stable UTF-8 JSON-safe relative archive path."""
    resolved_root = Path(archive_root).resolve(strict=True)
    resolved_path = Path(path).resolve(strict=True)
    if not resolved_path.is_relative_to(resolved_root) or not resolved_path.is_file():
        raise ValueError("archive path is outside the archive root")
    return resolved_path.relative_to(resolved_root).as_posix()


@dataclass(frozen=True)
class _Feature:
    pixels: Image.Image
    dhash: int


def _feature(path: Path) -> _Feature:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA").convert("RGB")
        # The catalog and archive contain card scans; reject incompatible shapes
        # instead of stretching a landscape or cropped image into a card.
        ratio = image.width / image.height
        if not 0.60 <= ratio <= 0.82:
            raise ValueError("image shape is outside the portrait card range")
        pixels = image.resize(_SIZE, Image.Resampling.LANCZOS)
    gray = pixels.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    values = list(gray.get_flattened_data())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (values[row * 9 + col] > values[row * 9 + col + 1])
    return _Feature(pixels, bits)


def _mae(left: Image.Image, right: Image.Image, box=None) -> float:
    if box:
        left, right = left.crop(box), right.crop(box)
    diff = ImageChops.difference(left, right).convert("L")
    histogram = diff.histogram()
    total = sum(value * count for value, count in enumerate(histogram))
    return total / (255 * left.width * left.height)


def _metrics(left: Image.Image, right: Image.Image) -> dict[str, float]:
    result = {"global_mae": _mae(left, right)}
    for name, (x0, y0, x1, y1) in _ZONES.items():
        box = (round(x0 * _SIZE[0]), round(y0 * _SIZE[1]),
               round(x1 * _SIZE[0]), round(y1 * _SIZE[1]))
        result[f"{name}_mae"] = _mae(left, right, box)
    return result


def _active_rows(db: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("""
            SELECT p.printing_id,p.collector_number,p.set_id,e.code,e.name AS set_name,
                   s.face_index,a.asset_id,a.relative_path,
                   (SELECT sr.provider_card_id FROM source_records sr
                     WHERE sr.printing_id=p.printing_id
                     ORDER BY CASE sr.lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 ELSE 2 END,
                              sr.provider_card_id LIMIT 1) AS provider_card_id,
                   COALESCE((SELECT COALESCE(NULLIF(sr.printed_name,''),sr.name)
                     FROM source_records sr WHERE sr.printing_id=p.printing_id
                     ORDER BY CASE sr.lang WHEN 'fr' THEN 0 ELSE 1 END LIMIT 1),'Carte') AS card_name
            FROM printings p JOIN editions e USING(set_id)
            JOIN image_slots s USING(printing_id) JOIN image_assets a USING(asset_id)
            WHERE s.status='ready' ORDER BY p.printing_id,s.face_index
        """)]


def _organized_path(root: Path, row: dict[str, Any]) -> Path:
    folder = _safe_name(f'{row["code"].upper()} - {row["set_name"]}', 100)
    stem = _safe_name(f'{row["collector_number"]} - {row["card_name"]}', 110)
    identity = hashlib.sha256(row["printing_id"].encode("utf-8")).hexdigest()[:16]
    return root / folder / f'{stem} - face {row["face_index"] + 1} - {identity}.webp'


class VisualIndex:
    """Reusable in-memory feature index; instantiate once per archive batch."""

    def __init__(self, current_db: Path, organized_root: Path):
        self.current_db = Path(current_db)
        self.organized_root = Path(organized_root)
        self.rows = _active_rows(self.current_db)
        self.features: list[tuple[dict[str, Any], _Feature]] = []
        self.skipped = 0
        for row in self.rows:
            path = _organized_path(self.organized_root, row)
            try:
                data_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                # Current V2 assets include raw SHA-256 IDs as well as the
                # language-prefixed form used by older builds.
                if row["asset_id"] != data_hash and not row["asset_id"].endswith("_" + data_hash):
                    self.skipped += 1
                    continue
                self.features.append((row, _feature(path)))
            except (OSError, ValueError, UnidentifiedImageError):
                self.skipped += 1

    def propose(self, archive_path: Path) -> dict[str, Any]:
        """Return a unique, ambiguous, or none proposal with comparison evidence."""
        try:
            query = _feature(Path(archive_path))
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            return {"status": "none", "reason": f"unreadable_or_unsupported: {exc}",
                    "indexed_assets": len(self.features), "skipped_assets": self.skipped,
                    "candidates": []}

        shortlist = []
        for row, feature in self.features:
            distance = (query.dhash ^ feature.dhash).bit_count()
            if distance <= MAX_DHASH_DISTANCE:
                shortlist.append((distance, row, feature))

        ranked = []
        for distance, row, feature in shortlist:
            metrics = _metrics(query.pixels, feature.pixels)
            ranked.append({**{key: row[key] for key in (
                "printing_id", "face_index", "asset_id", "code", "set_name",
                "collector_number", "card_name", "provider_card_id")}, **metrics,
                "dhash_distance": distance})
        ranked.sort(key=lambda item: (item["global_mae"], item["dhash_distance"],
                                      item["printing_id"], item["face_index"]))

        passing = [candidate for candidate in ranked
                   if candidate["global_mae"] <= MAX_GLOBAL_MAE
                   and all(candidate[f"{zone}_mae"] <= MAX_ZONE_MAE for zone in _ZONES)]
        best = passing[0] if passing else None
        second = passing[1] if len(passing) > 1 else None
        if best and (second is None or second["global_mae"] - best["global_mae"] >= MIN_LEAD):
            status = "visual_candidate_for_review"
        elif best:
            status = "ambiguous_visual_candidates_for_review"
        else:
            status = "none"
        return {"status": status, "reason": None if best else "no_candidate_passed_limits",
                "indexed_assets": len(self.features), "skipped_assets": self.skipped,
                "thresholds": {"global_mae_max": MAX_GLOBAL_MAE,
                               "zone_mae_max": MAX_ZONE_MAE,
                               "unique_lead_min": MIN_LEAD,
                               "dhash_distance_max": MAX_DHASH_DISTANCE},
                "candidates": ranked[:5]}

    def suggest(self, archive_path: Path) -> dict[str, Any]:
        """Return relaxed visual leads, always labelled for human review only."""
        try:
            query = _feature(Path(archive_path))
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            return {"status": "none", "reason": f"unreadable_or_unsupported: {exc}",
                    "candidates": []}

        ranked = []
        for row, feature in self.features:
            distance = (query.dhash ^ feature.dhash).bit_count()
            if distance > SUGGEST_MAX_DHASH_DISTANCE:
                continue
            metrics = _metrics(query.pixels, feature.pixels)
            ranked.append({**{key: row[key] for key in (
                "printing_id", "face_index", "asset_id", "code", "set_name",
                "collector_number", "card_name", "provider_card_id")}, **metrics,
                "dhash_distance": distance})
        ranked.sort(key=lambda item: (item["global_mae"], item["dhash_distance"],
                                      item["printing_id"], item["face_index"]))
        strict_passing = [candidate for candidate in ranked
                          if candidate["dhash_distance"] <= MAX_DHASH_DISTANCE
                          and candidate["global_mae"] <= MAX_GLOBAL_MAE
                          and all(candidate[f"{zone}_mae"] <= MAX_ZONE_MAE for zone in _ZONES)]
        if strict_passing:
            if len(strict_passing) == 1 or \
                    strict_passing[1]["global_mae"] - strict_passing[0]["global_mae"] >= MIN_LEAD:
                strict_status = "visual_candidate_for_review"
            else:
                strict_status = "ambiguous_visual_candidates_for_review"
            return {"status": "strict_candidate_exists", "strict_status": strict_status,
                    "candidates": strict_passing[:5]}
        passing = [candidate for candidate in ranked
                   if candidate["global_mae"] <= SUGGEST_MAX_GLOBAL_MAE
                   and all(candidate[f"{zone}_mae"] <= SUGGEST_MAX_ZONE_MAE for zone in _ZONES)]
        if not passing:
            status = "none"
        elif len(passing) == 1 or passing[1]["global_mae"] - passing[0]["global_mae"] >= SUGGEST_MIN_LEAD:
            status = "suggestion_unique_for_review"
        else:
            status = "suggestion_ambiguous_for_review"
        return {"status": status, "reason": None if passing else "no_candidate_passed_suggestion_limits",
                "tier": "suggestion_only",
                "passing_candidate_count": len(passing),
                "thresholds": {"global_mae_max": SUGGEST_MAX_GLOBAL_MAE,
                               "zone_mae_max": SUGGEST_MAX_ZONE_MAE,
                               "lead_min": SUGGEST_MIN_LEAD,
                               "dhash_distance_max": SUGGEST_MAX_DHASH_DISTANCE},
                "candidates": passing[:5]}


def propose_visual(path: Path, current_db: Path, organized_root: Path,
                   cache: VisualIndex | None = None) -> dict[str, Any]:
    """Public single-file API; pass a ``VisualIndex`` as cache for archive batches."""
    index = cache or VisualIndex(current_db, organized_root)
    if index.current_db.resolve() != Path(current_db).resolve() or \
            index.organized_root.resolve() != Path(organized_root).resolve():
        raise ValueError("visual index belongs to different catalogue paths")
    return index.propose(path)
