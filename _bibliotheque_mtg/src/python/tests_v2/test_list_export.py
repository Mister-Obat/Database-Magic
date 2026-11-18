import sqlite3

from PIL import Image

from bibliotheque_mtg.list_export import export_list_to_desktop


def test_export_folder_contains_ready_and_blank_cards(tmp_path):
    db = tmp_path / "catalogue.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT, name TEXT,
            printed_name TEXT, lang TEXT, mana_value REAL);
        CREATE TABLE image_assets(asset_id TEXT, relative_path TEXT);
        CREATE TABLE image_slots(printing_id TEXT, face_index INTEGER, status TEXT, asset_id TEXT);
        INSERT INTO source_records VALUES('en','p','Lightning Bolt',NULL,'en',1),
            ('fr','p','Lightning Bolt','Foudre','fr',1);
        INSERT INTO image_assets VALUES('a','ready.webp');
        INSERT INTO image_slots VALUES('p',0,'ready','a');
    """)
    conn.close()
    Image.new("RGB", (531, 738), "red").save(tmp_path / "ready.webp")
    items = [{"input": "Lightning Bolt", "provider_card_id": "en"}, {"input": "Mystery Card"}]
    result = export_list_to_desktop(items, db, tmp_path / "Desktop")
    folder = tmp_path / "Desktop" / "Export cartes Magic"
    assert result == {"folder": str(folder), "total": 2, "blank": 1}
    assert sorted(p.name for p in folder.iterdir()) == ["0 - Mystery Card.png", "1 - Foudre.png"]
    with Image.open(folder / "0 - Mystery Card.png") as image:
        assert image.size == (531, 738)
        assert image.getpixel((200, 200)) == (255, 255, 255)
    second = export_list_to_desktop(items, db, tmp_path / "Desktop")
    assert second["folder"].endswith("Export cartes Magic (2)")
