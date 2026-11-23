import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from bibliotheque_mtg import deck_media
from bibliotheque_mtg.deck_media import enrich_deck_media, export_deck_to_desktop


@pytest.fixture
def catalog(tmp_path):
    version = tmp_path / "version"
    db = version / "catalogue.sqlite"
    (version / "assets" / "webp" / "fr").mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE editions(set_id TEXT, code TEXT, eligibility TEXT);
    CREATE TABLE printings(printing_id TEXT, set_id TEXT, layout TEXT);
    CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT, lang TEXT,
      printed_name TEXT, name TEXT, mana_value REAL, colors_key TEXT);
    CREATE TABLE card_faces(printing_id TEXT, face_index INTEGER, physical_image INTEGER);
    CREATE TABLE image_assets(asset_id TEXT, relative_path TEXT);
    CREATE TABLE image_slots(printing_id TEXT, face_index INTEGER, status TEXT, asset_id TEXT);
    """)
    for printing, provider, en, fr, mana, faces in [
        ("p1", "id1", "Fire // Ice", "Feu // Glace", 2.0, 2),
        ("p2", "id2", "Token Goblin", None, 0.0, 1),
    ]:
        conn.execute("INSERT INTO editions VALUES(?,?,?)", ("set", "TST", "eligible"))
        conn.execute("INSERT INTO printings VALUES(?,?,?)", (printing, "set", "modal_dfc" if faces > 1 else "normal"))
        conn.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?)", (provider, printing, "en", None, en, mana, "R"))
        if fr:
            conn.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?)", (provider + "fr", printing, "fr", fr, fr, mana, "R"))
        for face in range(faces):
            conn.execute("INSERT INTO card_faces VALUES(?,?,1)", (printing, face))
            rel = f"assets/webp/fr/{printing}-{face}.webp"
            asset = f"{printing}-{face}"
            conn.execute("INSERT INTO image_assets VALUES(?,?)", (asset, rel))
            conn.execute("INSERT INTO image_slots VALUES(?,?,?,?)", (printing, face, "ready", asset))
            Image.new("RGB", (10, 10), (face * 80, 20, 30)).save(version / rel, "WEBP")
    conn.commit()
    conn.close()
    return db


def test_enrichment_requires_each_physical_face_and_preserves_french(catalog, tmp_path):
    deck = {"name": "Deck", "sections": {"mainboard": [
        {"name": "wrong name cannot resolve", "provider_card_id": "id1"}]}}
    enriched = enrich_deck_media(deck, catalog)
    card = enriched["sections"]["mainboard"][0]
    assert card["printed_name"] == "Feu // Glace"
    assert card["mana_value"] == 2
    assert card["image_ready"] is True
    assert card["image_path"] == "assets/webp/fr/p1-0.webp"
    (catalog.parent / "assets/webp/fr/p1-1.webp").unlink()
    card = enrich_deck_media(deck, catalog)["sections"]["mainboard"][0]
    assert card["image_path"] == "assets/webp/fr/p1-0.webp"
    assert card["image_ready"] is False


def test_media_never_uses_another_edition_even_with_exact_provider_id(catalog, tmp_path):
    card = {"name": "Copy", "set_code": "OTHER", "provider_card_id": "id1"}
    deck = {"name": "Other deck", "sections": {"tokens": [card]}}
    enriched = enrich_deck_media(deck, catalog)["sections"]["tokens"][0]
    assert enriched["image_status"] == "unresolved"
    assert enriched["image_path"] is None
    with pytest.raises(ValueError, match="introuvable"):
        export_deck_to_desktop(deck, catalog, tmp_path / "Desktop")
    assert not (tmp_path / "Desktop").exists()


def test_export_png_faces_tokens_french_and_collision(catalog, tmp_path):
    deck = {"name": "Deck / test", "sections": {
        "mainboard": [{"name": "Fire // Ice", "provider_card_id": "id1", "count": 2}],
        "tokens": [{"name": "Token Goblin", "provider_card_id": "id2", "count": 1}],
    }}
    desktop = tmp_path / "Desktop"
    first = export_deck_to_desktop(deck, catalog, desktop)
    second = export_deck_to_desktop(deck, catalog, desktop)
    assert Path(first["folder"]).name == "Deck _ test"
    assert Path(second["folder"]).name == "Deck _ test (2)"
    assert first["total"] == 5
    assert (Path(first["folder"]) / "Jetons" / "0 - Token Goblin.png").is_file()
    assert any("Feu __ Glace" in Path(file).name for file in first["files"])
    assert all(Image.open(file).format == "PNG" for file in first["files"])


def test_export_incomplete_deck_creates_no_folder(catalog, tmp_path):
    (catalog.parent / "assets/webp/fr/p1-1.webp").unlink()
    deck = {"name": "Incomplete", "sections": {"mainboard": [
        {"name": "Card", "provider_card_id": "id1"}]}}
    with pytest.raises(ValueError, match="incomplètes"):
        export_deck_to_desktop(deck, catalog, tmp_path / "Desktop")
    assert not list((tmp_path / "Desktop").glob("*"))


def test_cover_paths_batches_decks_and_uses_one_catalog_connection(catalog, tmp_path, monkeypatch):
    deck_db = tmp_path / "decks.sqlite"
    conn = sqlite3.connect(deck_db)
    conn.execute("CREATE TABLE deck_cards(deck_id TEXT, section TEXT, ordinal INTEGER, raw_json TEXT)")
    for index in range(50):
        deck_id = f"deck-{index}"
        conn.execute("INSERT INTO deck_cards VALUES(?,?,?,?)",
                     (deck_id, "commander", 0, '{"identifiers":{"scryfallId":"id1"}}'))
        # Later commanders must never replace the first commander image.
        conn.execute("INSERT INTO deck_cards VALUES(?,?,?,?)",
                     (deck_id, "commander", 1, '{"identifiers":{"scryfallId":"missing"}}'))
    conn.commit()
    conn.close()
    opened = 0
    original_catalog = deck_media._catalog

    def counting_catalog(path):
        nonlocal opened
        opened += 1
        return original_catalog(path)

    monkeypatch.setattr(deck_media, "_catalog", counting_catalog)
    ids = [f"deck-{index}" for index in range(50)]
    result = deck_media.deck_cover_paths(ids, deck_db, catalog)
    assert len(result) == 50
    assert set(result.values()) == {"assets/webp/fr/p1-0.webp"}
    assert opened == 1

    with sqlite3.connect(deck_db) as conn:
        conn.execute("UPDATE deck_cards SET raw_json=? WHERE deck_id='deck-0' AND ordinal=0",
                     ('{"setCode":"OTHER","identifiers":{"scryfallId":"id1"}}',))
    changed = deck_media.deck_cover_paths(["deck-0", "deck-1"], deck_db, catalog)
    assert changed["deck-0"] is None
    assert changed["deck-1"] == "assets/webp/fr/p1-0.webp"
