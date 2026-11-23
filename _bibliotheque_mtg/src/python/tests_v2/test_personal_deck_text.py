import sqlite3

import pytest

from bibliotheque_mtg.personal_deck_text import export_text, import_text
from bibliotheque_mtg.personal_decks import get_deck


@pytest.fixture
def catalog(tmp_path):
    db = tmp_path / "catalogue.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE editions(set_id TEXT, code TEXT, eligibility TEXT, released_at TEXT);
            CREATE TABLE printings(printing_id TEXT, set_id TEXT, collector_number TEXT);
            CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT,
                name TEXT, printed_name TEXT, lang TEXT, type_line TEXT, mana_value REAL,
                color_identity_json TEXT, colors_key TEXT);
            CREATE VIRTUAL TABLE source_search USING fts5(
                provider_card_id UNINDEXED, name, printed_name);
            INSERT INTO editions VALUES('old','old','eligible','2001-01-01'),
                ('new','new','eligible','2015-01-01');
            INSERT INTO printings VALUES('p1','old','1'), ('p2','new','99'),
                ('p3','new','22a'), ('p4','new','561'), ('p5','new','17');
            INSERT INTO source_records VALUES
                ('old1','p1','Lightning Bolt','Foudre','fr','Instant',1,'["R"]','R'),
                ('new1','p2','Lightning Bolt',NULL,'en','Instant',1,'["R"]','R'),
                ('new2','p3','Fire // Ice',NULL,'en','Instant',2,'["U","R"]','UR'),
                ('new3','p4','Xande, Dark Mage',NULL,'en','Creature',4,'["U","B"]','UB'),
                ('new4','p5','X',NULL,'en','Creature',2,'["U","B"]','UB');
            INSERT INTO source_search SELECT provider_card_id,name,printed_name FROM source_records;
        """)
    return db


def test_arena_keeps_exact_printings_quantities_sections_and_unresolved(catalog, tmp_path):
    db = tmp_path / "decks.sqlite"
    result = import_text(db, catalog, """\ufeffAbout
Name Nom: test
Commander
1 Fire (NEW) 22a
Deck
2x Foudre (OLD) 1 *F*
3 Lightning Bolt
Sideboard
1 Lightning Bolt (OLD) 777
1 Uncatalogued Card
nonsense here
0 Invalid quantity
""", "ignored.txt")
    deck = result["deck"]
    assert deck["name"] == "Nom: test"
    assert deck["colors"] == ["U", "R"]
    assert result["imported"] == 8
    assert [item["line"] for item in result["unresolved"]] == [9, 10, 11, 12]
    assert deck["revision"] == 1
    assert get_deck(db, deck["id"]) == deck
    commander, main, sideboard = deck["sections"]
    assert [section["source_section"] for section in deck["sections"]] == [
        "commander", "mainboard", "sideboard"
    ]
    assert commander["cards"][0]["printing_id"] == "p3"
    assert [(card["printing_id"], card["count"]) for card in main["cards"]] == [
        ("p1", 2), ("p2", 3)
    ]
    assert [(card["name"], card.get("collector_number")) for card in sideboard["cards"]] == [
        ("Lightning Bolt", "777"), ("Uncatalogued Card", None)
    ]


def test_ignores_display_commander_as_heading_metadata_and_card_tag(catalog, tmp_path):
    result = import_text(tmp_path / "decks.sqlite", catalog, """Name Deck
Commander
1 Lightning Bolt (OLD) 1
Deck
2 Lightning Bolt (NEW) 99
1 X [Display Commander]
Commandant de présentation
1 Not a playable card
// Section: ["Vitrine", "display_commander"]
1 Fire // Ice (NEW) 22a
""")
    assert result["imported"] == 3
    assert not result["unresolved"]
    deck = result["deck"]
    assert deck["colors"] == ["R"]
    assert [section["source_section"] for section in deck["sections"]] == ["commander", "mainboard"]


def test_dec_filename_comment_and_sb_prefix(catalog, tmp_path):
    db = tmp_path / "decks.sqlite"
    result = import_text(db, catalog, """// Lands
2 Lightning Bolt [OLD]
SB: 1 [NEW:22a] Fire // Ice
1 lightning bolt
""", r"C:\fakepath\Mon deck.dec")
    assert result["deck"]["name"] == "Mon deck"
    main, sideboard = result["deck"]["sections"]
    assert [card["printing_id"] for card in main["cards"]] == ["p1", "p2"]
    assert sideboard["cards"][0]["printing_id"] == "p3"
    assert not result["unresolved"]
    assert import_text(db, catalog, "//NAME: Test\n1 Lightning Bolt")["deck"]["name"] == "Test"


def test_export_non_overwriting_utf8_and_round_trip(catalog, tmp_path):
    db = tmp_path / "decks.sqlite"
    deck = import_text(db, catalog, """About
Name Éclairs
// Colors:
// Section: ["Favoris", null]
2 Lightning Bolt (OLD) 1
// Section: ["Favoris", null]
Sideboard
1 Uncatalogued Card
""")["deck"]
    desktop = tmp_path / "Desktop"
    first = export_text(db, deck["id"], desktop)
    second = export_text(db, deck["id"], desktop)
    assert first == {"file": str(desktop / "Éclairs.txt"), "total": 3}
    assert second == {"file": str(desktop / "Éclairs (2).txt"), "total": 3}
    value = (desktop / "Éclairs.txt").read_text(encoding="utf-8")
    assert "2 Lightning Bolt (OLD) 1" in value
    copy = import_text(db, catalog, value)["deck"]
    assert copy["name"] == deck["name"]
    assert copy["colors"] == deck["colors"] == []
    assert [section["name"] for section in copy["sections"]] == ["Favoris", "Favoris", "Réserve"]
    assert [len(section["cards"]) for section in copy["sections"]] == [1, 0, 1]


def test_reject_invalid_and_oversized_documents(catalog, tmp_path):
    db = tmp_path / "decks.sqlite"
    for value in ("", "This is random text.", "É" * 102_401):
        with pytest.raises(ValueError):
            import_text(db, catalog, value)
    assert not db.exists()


def test_x_card_names_survive_export_and_reimport(catalog, tmp_path):
    db = tmp_path / "decks.sqlite"
    initial = import_text(db, catalog, """Deck
1 Xande, Dark Mage (NEW) 561
1 X (NEW) 17
2 x Lightning Bolt (OLD) 1
3xLightning Bolt (NEW) 99
""", "Blue-Black.txt")
    exported = export_text(db, initial["deck"]["id"], tmp_path)
    copy = import_text(db, catalog, (tmp_path / "Blue-Black.txt").read_text(encoding="utf-8"))
    assert exported["total"] == 7
    assert copy["unresolved"] == initial["unresolved"] == []
    assert [(card["printing_id"], card["count"]) for card in copy["deck"]["sections"][0]["cards"]] == [
        ("p4", 1), ("p5", 1), ("p1", 2), ("p2", 3),
    ]


def test_inline_commander_markers_and_tags(catalog, tmp_path):
    deck = import_text(tmp_path / "decks.sqlite", catalog, """Deck
1 Lightning Bolt
1 Fire // Ice (NEW) 22a *CMDR* *F* #combo
1 Lightning Bolt (OLD) 1 *E* *CMDR*
1 Lightning Bolt (OLD) 1 [Sideboard{noDeck}] ^Have,#37d67a^
""")
    assert not deck["unresolved"]
    commander, main, sideboard = deck["deck"]["sections"]
    assert [card["printing_id"] for card in commander["cards"]] == ["p3", "p1"]
    assert main["cards"][0]["printing_id"] == "p2"
    assert sideboard["cards"][0]["printing_id"] == "p1"
