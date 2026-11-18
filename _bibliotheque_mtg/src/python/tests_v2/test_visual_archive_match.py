import hashlib
import json
import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from bibliotheque_mtg.visual_archive_match import VisualIndex, archive_relative_path, propose_visual


def _card(path: Path, color=(30, 70, 120), quality=95) -> None:
    image = Image.new("RGB", (300, 420), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 12, 288, 408), outline=(230, 210, 160), width=8)
    draw.rectangle((20, 26, 280, 62), fill=(190, 150, 80))
    draw.ellipse((220, 330, 260, 370), fill=(200, 50, 30))
    draw.text((22, 28), "Example Card", fill=(0, 0, 0))
    image.save(path, format="WEBP", quality=quality, method=4)


def _catalogue(tmp_path: Path, count=1):
    db = tmp_path / "catalogue.sqlite"
    root = tmp_path / "Images"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE printings(printing_id TEXT,set_id TEXT,collector_number TEXT);
            CREATE TABLE editions(set_id TEXT,code TEXT,name TEXT);
            CREATE TABLE image_slots(printing_id TEXT,face_index INTEGER,status TEXT,asset_id TEXT);
            CREATE TABLE image_assets(asset_id TEXT,relative_path TEXT);
            CREATE TABLE source_records(printing_id TEXT,provider_card_id TEXT,lang TEXT,printed_name TEXT,name TEXT);
        """)
        for i in range(count):
            printing_id = f"printing-{i}"
            identity = hashlib.sha256(printing_id.encode()).hexdigest()[:16]
            folder = root / "TST - Test Set"
            folder.mkdir(parents=True, exist_ok=True)
            image_path = folder / f"1 - Example Card - face 1 - {identity}.webp"
            _card(image_path, quality=95 - i)
            digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
            conn.execute("INSERT INTO printings VALUES(?,?,?)", (printing_id, "set-1", "1"))
            if i == 0:
                conn.execute("INSERT INTO editions VALUES(?,?,?)", ("set-1", "tst", "Test Set"))
            conn.execute("INSERT INTO image_slots VALUES(?,?,?,?)", (printing_id, 0, "ready", f"asset_{digest}"))
            conn.execute("INSERT INTO image_assets VALUES(?,?)", (f"asset_{digest}", f"assets/{i}.webp"))
            conn.execute("INSERT INTO source_records VALUES(?,?,?,?,?)", (printing_id, f"provider-{i}", "fr", "Example Card", "Example Card"))
    return db, root


def test_unique_visual_match_returns_active_slot_identity(tmp_path):
    db, root = _catalogue(tmp_path)
    archive = tmp_path / "archive.webp"
    _card(archive)
    index = VisualIndex(db, root)

    result = propose_visual(archive, db, root, cache=index)

    assert result["status"] == "visual_candidate_for_review"
    assert result["candidates"][0]["printing_id"] == "printing-0"
    assert result["candidates"][0]["face_index"] == 0
    assert result["candidates"][0]["asset_id"].startswith("asset_")
    assert result["candidates"][0]["title_mae"] == 0
    assert result["candidates"][0]["set_symbol_mae"] == 0
    assert result["candidates"][0]["collector_number_mae"] == 0


def test_equal_visual_matches_are_rejected_as_ambiguous(tmp_path):
    db, root = _catalogue(tmp_path, count=2)
    archive = tmp_path / "archive.webp"
    _card(archive)

    result = VisualIndex(db, root).propose(archive)

    assert result["status"] == "ambiguous_visual_candidates_for_review"
    assert len(result["candidates"]) == 2


def test_corrupt_catalogue_asset_is_not_indexed(tmp_path):
    db, root = _catalogue(tmp_path)
    path = next(root.rglob("*.webp"))
    path.write_bytes(b"changed")

    result = VisualIndex(db, root).propose(path)

    assert result["status"] == "none"
    assert result["indexed_assets"] == 0
    assert result["skipped_assets"] == 1


def test_unicode_archive_path_round_trips_through_json(tmp_path):
    archive_root = tmp_path / "Cartes Magic"
    archive_root.mkdir()
    source = archive_root / "Inconnu" / "Jambières d'éclair.png"
    source.parent.mkdir()
    source.write_bytes(b"archive")

    relative = archive_relative_path(source, archive_root)
    serialized = json.dumps({"archive_path": relative}, ensure_ascii=True)
    restored = json.loads(serialized)["archive_path"]

    assert restored == "Inconnu/Jambières d'éclair.png"
    assert "Ã" not in restored


def test_relaxed_tier_is_explicitly_review_only(tmp_path):
    db, root = _catalogue(tmp_path)
    original = next(root.rglob("*.webp"))
    archive = tmp_path / "archive-brightened.webp"
    with Image.open(original) as image:
        ImageEnhance.Brightness(image.convert("RGB")).enhance(1.12).save(
            archive, format="WEBP", quality=95
        )

    result = VisualIndex(db, root).suggest(archive)

    assert result["tier"] == "suggestion_only"
    assert result["status"] in {"suggestion_unique_for_review", "suggestion_ambiguous_for_review"}
    assert result["candidates"]
    assert result["candidates"][0]["printing_id"] == "printing-0"
