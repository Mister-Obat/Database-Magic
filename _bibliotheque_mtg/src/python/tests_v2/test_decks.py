import pytest
import json
import sqlite3
from unittest.mock import patch

from bibliotheque_mtg.decks import get_deck, import_decks, list_decks, index_deck_colors
from bibliotheque_mtg.decks import deck_facets, _resolve_provider_ids


def test_deck_printing_resolution_excludes_deferred_editions(tmp_path):
    catalog = tmp_path / "catalogue.sqlite"
    with sqlite3.connect(catalog) as conn:
        conn.executescript("""
            CREATE TABLE editions(set_id TEXT PRIMARY KEY, code TEXT, eligibility TEXT);
            CREATE TABLE printings(printing_id TEXT PRIMARY KEY, set_id TEXT, collector_number TEXT);
            CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT, lang TEXT, printed_name TEXT);
            INSERT INTO editions VALUES('deferred','TRC','deferred'),('eligible','WHO','eligible');
            INSERT INTO printings VALUES('trc-card','deferred','84'),('who-card','eligible','3');
            INSERT INTO source_records VALUES('trc-id','trc-card','en',NULL),('who-id','who-card','en',NULL);
        """)
    rows = [
        {"raw": {"identifiers": {"scryfallId": "trc-id"}}, "set_code": "TRC", "collector_number": "84"},
        {"raw": {"identifiers": {"scryfallId": "who-id"}}, "set_code": "WHO", "collector_number": "3"},
        {"raw": {"identifiers": {"scryfallId": "trc-id"}}, "set_code": "WHO", "collector_number": "84"},
        {"raw": {"identifiers": {"scryfallId": "trc-id"}}, "set_code": "WHO", "collector_number": "3"},
    ]
    _resolve_provider_ids(rows, catalog)
    assert [row["provider_card_id"] for row in rows] == [None, "who-id", None, "who-id"]


def test_import_and_list_decks(tmp_path):
    db = tmp_path / "decks.sqlite"
    source = {
        "meta": {"date": "2026-11-15", "version": "5.3.0"},
        "data": [
            {"code": "WHO", "fileName": "TimeyWimey_WHO", "name": "Timey-Wimey",
             "releaseDate": "2023-10-13", "type": "Commander Deck"},
            {"code": "SLD", "fileName": "nonDeck_SLD", "name": "Secret Lair",
             "releaseDate": "2023-10-13", "type": "Secret Lair Drop"},
            {"code": "DMU", "fileName": "SneakSquad_DMU", "name": "Sneak Squad",
             "releaseDate": "2022-09-09", "type": "Arena Promotional Deck"},
            {"code": "SLD", "fileName": "DandanDeck_SLD", "name": "Dandan Deck",
             "releaseDate": "2022-01-01", "type": "Dandan Deck"},
            {"code": "M12", "fileName": "BoosterBattlePackBlackA_M12", "name": "Black Packet",
             "releaseDate": "2011-07-15", "type": "Booster Battle Pack Packet"},
        ],
    }
    assert import_decks(source, db) == {"decks": 5, "loaded": 0}
    page = list_decks(db, query="Timey", limit=10)
    assert page["total"] == 1
    assert page["items"][0]["id"] == "mtgjson:TimeyWimey_WHO"
    assert get_deck(db, page["items"][0]["id"], fetch_missing=False)["deck"]["loaded"] == 0
    assert list_decks(db)["total"] == 3
    assert {item["name"] for item in list_decks(db, limit=10)["items"]} == {
        "Timey-Wimey", "Sneak Squad", "Dandan Deck"
    }
    assert list_decks(db, deck_format="Commander")["items"][0]["deck_format"] == "Commander"


def test_verified_french_names_search_and_edition_filter(tmp_path):
    db = tmp_path / "decks.sqlite"
    import_decks({"meta": {"version": "test"}, "data": [
        {"code": "WHO", "fileName": "TimeyWimey_WHO", "name": "Timey-Wimey",
         "setName": "Doctor Who", "type": "Commander Deck"},
        {"code": "WHO", "fileName": "BlastFromThePast_WHO", "name": "Blast from the Past",
         "setName": "Doctor Who", "type": "Commander Deck"},
        {"code": "TST", "fileName": "Sample_TST", "name": "Sample",
         "setName": "Test Set", "translations": {"French": "Exemple"},
         "setTranslations": {"French": "Édition de test"}, "type": "Commander Deck"},
    ]}, db)

    localized = list_decks(db, query="meli-melo")
    assert localized["total"] == 1
    item = localized["items"][0]
    assert item["name"] == "Timey-Wimey"
    assert item["name_fr"] == "Méli-mélo spatio-temporel"
    assert item["set_name_fr"] == "Doctor Who"
    assert list_decks(db, query="edition de test")["total"] == 1
    assert list_decks(db, set_code="who")["total"] == 2
    assert deck_facets(db)["editions"] == [
        {"code": "TST", "name_fr": "Édition de test", "name_en": "Test Set"},
        {"code": "WHO", "name_fr": "Doctor Who", "name_en": "Doctor Who"},
    ]
    detail = get_deck(db, "mtgjson:TimeyWimey_WHO", fetch_missing=False)["deck"]
    assert detail["name_fr"] == "Méli-mélo spatio-temporel"


def test_colorless_filter_can_be_combined_with_colors(tmp_path):
    db = tmp_path / "decks.sqlite"
    import_decks([{
        "name": "Colorless", "cards": [{"name": "Artifact", "colorIdentity": []}]
    }, {
        "name": "Blue", "cards": [{"name": "Island", "colorIdentity": ["U"]}]
    }], db)

    assert list_decks(db, colors="C")["items"][0]["name"] == "Colorless"
    assert list_decks(db, colors=["C"])["total"] == 1
    assert {item["name"] for item in list_decks(db, colors="CU")["items"]} == {"Colorless", "Blue"}


def test_color_modes_match_exact_or_containing_identity(tmp_path):
    db = tmp_path / "decks.sqlite"
    import_decks([{
        "name": "Colorless", "cards": [{"name": "Artifact", "colorIdentity": []}]
    }, {
        "name": "White", "cards": [{"name": "Plains", "colorIdentity": ["W"]}]
    }, {
        "name": "Azorius", "cards": [{"name": "WU Card", "colorIdentity": ["U", "W"]}]
    }, {
        "name": "Bant", "cards": [{"name": "WUG Card", "colorIdentity": ["G", "W", "U"]}]
    }], db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE decks SET colors='UW' WHERE name='Azorius'")
        conn.commit()
    finally:
        conn.close()

    assert {item["name"] for item in list_decks(db, colors="W", color_mode="exact")["items"]} == {"White"}
    assert {item["name"] for item in list_decks(db, colors="WU", color_mode="exact")["items"]} == {"Azorius"}
    assert {item["name"] for item in list_decks(db, colors="WU")["items"]} == {"Azorius", "Bant"}
    assert {item["name"] for item in list_decks(db, colors="C", color_mode="exact")["items"]} == {"Colorless"}
    assert {item["name"] for item in list_decks(db, colors="CW")["items"]} == {"Colorless", "White", "Azorius", "Bant"}
    assert {item["name"] for item in list_decks(db, colors="CW", color_mode="exact")["items"]} == {"Colorless", "White"}
    assert list_decks(db, deck_type="-")["total"] == 0
    assert list_decks(db, deck_format="-")["total"] == 0
    assert list_decks(db, set_code="-")["total"] == 0
    with pytest.raises(ValueError, match="contains ou exact"):
        list_decks(db, colors="WU", color_mode="invalid")


def test_community_sections_and_quantities(tmp_path):
    db = tmp_path / "decks.sqlite"
    import_decks([{
        "name": "Sample", "type": "Preconstructed", "set_code": "TST",
        "cards": [{"name": "Example Card", "count": 2, "set_code": "TST", "number": "1", "colorIdentity": ["U"]}],
        "commander": [{"name": "Captain", "count": 1, "colorIdentity": ["R"]}],
    }], db)
    deck_id = list_decks(db)["items"][0]["id"]
    result = get_deck(db, deck_id, fetch_missing=False)
    assert result["deck"]["sections"]["mainboard"][0]["count"] == 2
    assert result["deck"]["sections"]["commander"][0]["name"] == "Captain"
    assert result["deck"]["sections"]["sideboard"] == []
    assert result["deck"]["colors"] == ["U", "R"]
    assert result["deck"]["deck_format"] == "Autre"
    assert list_decks(db, colors="UR")["total"] == 1
    assert list_decks(db, colors="W")["total"] == 0
    assert deck_facets(db)["color_indexed"] == 1


def test_invalid_quantity_does_not_replace_existing_snapshot(tmp_path):
    db = tmp_path / "decks.sqlite"
    import_decks([{"name": "Safe", "cards": [{"name": "Card", "count": 1}]}], db)
    with pytest.raises(ValueError, match="Quantité non positive"):
        import_decks([{"name": "Bad", "cards": [{"name": "Card", "count": 0}]}], db)
    assert list_decks(db)["items"][0]["name"] == "Safe"


def test_refreshing_one_source_preserves_other_source(tmp_path):
    db = tmp_path / "decks.sqlite"
    import_decks({"meta": {"version": "1"}, "data": [
        {"name": "MTGJSON deck", "fileName": "sample_TST", "code": "TST", "type": "Commander Deck"}
    ]}, db)
    import_decks([{"name": "Community deck", "cards": [{"name": "Card"}]}], db)
    import_decks({"meta": {"version": "2"}, "data": [
        {"name": "Updated MTGJSON deck", "fileName": "updated_TST", "code": "TST", "type": "Commander Deck"}
    ]}, db)
    names = {item["name"] for item in list_decks(db, limit=10)["items"]}
    assert names == {"Updated MTGJSON deck", "Community deck"}


def test_existing_decks_gain_verified_french_names(tmp_path):
    import sqlite3

    db = tmp_path / "decks.sqlite"
    import_decks({"data": [
        {"name": "Timey-Wimey", "fileName": "TimeyWimey_WHO", "code": "WHO", "type": "Commander Deck"},
        {"name": "Tenth Edition Deck", "fileName": "Example_10E", "code": "10E", "type": "Theme Deck"},
    ]}, db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE decks SET name_fr=NULL,name_fr_search=NULL,set_name_fr=NULL,set_name_fr_search=NULL")
    who = list_decks(db, query="meli-melo")["items"]
    assert len(who) == 1
    assert who[0]["name_fr"] == "Méli-mélo spatio-temporel"
    assert list_decks(db, query="dixieme edition")["items"][0]["set_name_fr"] == "Dixième édition"


def test_tokens_are_opt_in_and_enriched_without_replacing_loaded_cards(tmp_path):
    db = tmp_path / "decks.sqlite"
    import_decks({"meta": {}, "data": [{
        "code": "TST", "fileName": "Sample_TST", "name": "Sample", "type": "Commander Deck",
        "mainBoard": [{"name": "Existing Card", "count": 2}],
    }]}, db)
    deck_id = "mtgjson:Sample_TST"
    token = {
        "name": "Soldier", "count": 3, "setCode": "TST", "number": "42",
        "uuid": "token-uuid", "identifiers": {"scryfallId": "token-scryfall"},
    }
    payload = {"data": {"mainBoard": [{"name": "Remote Card", "count": 1}], "tokens": [token]}}
    with patch("bibliotheque_mtg.decks.urllib.request.urlopen", return_value=_Response(payload)) as fetch:
        offline = get_deck(db, deck_id, fetch_missing=False, include_tokens=True)["deck"]
        assert offline["tokens_loaded"] is False
        assert offline["sections"]["tokens"] == []
        fetch.assert_not_called()

        result = get_deck(db, deck_id, fetch_missing=True, include_tokens=True)["deck"]
    fetch.assert_called_once()
    assert result["tokens_loaded"] is True
    assert result["sections"]["mainboard"][0]["name"] == "Existing Card"
    stored = result["sections"]["tokens"][0]
    assert (stored["name"], stored["count"], stored["set_code"], stored["collector_number"], stored["mtgjson_uuid"]) == (
        "Soldier", 3, "TST", "42", "token-uuid"
    )
    assert stored["raw"]["identifiers"]["scryfallId"] == "token-scryfall"
    assert stored["provider_card_id"] is None


def test_inline_tokens_are_loaded_with_deck_composition(tmp_path):
    db = tmp_path / "decks.sqlite"
    import_decks([{"name": "Inline", "mainBoard": [{"name": "Card"}],
                  "tokens": [{"name": "Clue", "quantity": 2, "setCode": "TST", "number": "1", "uuid": "u"}]}], db)
    deck = get_deck(db, list_decks(db)["items"][0]["id"], fetch_missing=False)["deck"]
    assert deck["tokens_loaded"] is True
    assert deck["sections"]["tokens"][0]["count"] == 2
    assert deck["sections"]["tokens"][0]["mtgjson_uuid"] == "u"


def test_cached_empty_tokens_derive_all_parts_and_prefer_companion_token_set(tmp_path):
    db = tmp_path / "decks.sqlite"
    catalog = tmp_path / "catalogue.sqlite"
    with sqlite3.connect(catalog) as conn:
        conn.executescript("""
            CREATE TABLE editions(set_id TEXT PRIMARY KEY,code TEXT,eligibility TEXT);
            CREATE TABLE printings(printing_id TEXT PRIMARY KEY,set_id TEXT,oracle_id TEXT,collector_number TEXT);
            CREATE TABLE source_records(provider_card_id TEXT PRIMARY KEY,printing_id TEXT,lang TEXT,
                raw_json TEXT,name TEXT,printed_name TEXT);
            INSERT INTO editions VALUES ('trc','trc','eligible'),('ttrc','ttrc','eligible'),('ttrk','ttrk','eligible');
            INSERT INTO printings VALUES ('deck-print','trc','deck-oracle','1'),
                ('token-a-old','ttrk','oracle-a','5'),('token-a-trc','ttrc','oracle-a','10'),
                ('token-b-ttrk','ttrk','oracle-b','26');
        """)
        conn.executemany("INSERT INTO source_records VALUES(?,?,?,?,?,?)", [
            ("deck-scryfall", "deck-print", "en", json.dumps({"id": "deck-scryfall", "all_parts": [
                {"id": "token-a-old-en", "component": "token"},
                {"id": "token-b-en", "component": "token"},
            ]}), "Deck Card", None),
            ("token-a-old-en", "token-a-old", "en", json.dumps({"id": "token-a-old-en", "name": "Ape", "set": "ttrk", "collector_number": "5"}), "Ape", None),
            ("token-a-trc-en", "token-a-trc", "en", json.dumps({"id": "token-a-trc-en", "name": "Ape", "set": "ttrc", "collector_number": "10"}), "Ape", None),
            ("token-a-trc-fr", "token-a-trc", "fr", json.dumps({"id": "token-a-trc-fr", "name": "Singe", "set": "ttrc", "collector_number": "10"}), "Ape", "Singe"),
            ("token-b-en", "token-b-ttrk", "en", json.dumps({"id": "token-b-en", "name": "Officer", "set": "ttrk", "collector_number": "26"}), "Officer", None),
        ])
    import_decks({"meta": {}, "data": [{"code": "TRC", "fileName": "Sample_TRC", "name": "Sample",
        "mainBoard": [{"name": "Deck Card", "identifiers": {"scryfallId": "deck-scryfall"}}], "tokens": []}]}, db)
    # Simulate a cache written by the older build, which marked tokens: [] complete.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE decks SET tokens_loaded=1 WHERE id='mtgjson:Sample_TRC'")
        conn.execute("DELETE FROM deck_meta WHERE key='tokens_all_parts_migration'")
    with patch("bibliotheque_mtg.decks.urllib.request.urlopen") as fetch:
        deck = get_deck(db, "mtgjson:Sample_TRC", catalog_db=catalog,
                        include_tokens=True, fetch_missing=True)["deck"]
    fetch.assert_not_called()
    tokens = deck["sections"]["tokens"]
    assert [(row["name"], row["set_code"], row["collector_number"], row["provider_card_id"])
            for row in tokens] == [("Ape", "TTRC", "10", "token-a-trc-fr")]
    assert [row["printed_name"] for row in tokens] == ["Singe"]
    # A cached derived token from another edition must disappear even offline.
    foreign = {"name": "Officer", "count": 1, "setCode": "TTRK", "number": "26",
               "identifiers": {"scryfallId": "token-b-en"}, "all_parts_source": "token-b-en"}
    with sqlite3.connect(db) as conn:
        conn.execute("""INSERT INTO deck_cards
            (deck_id,section,ordinal,name,count,set_code,collector_number,mtgjson_uuid,foil,raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("mtgjson:Sample_TRC", "tokens", 1, "Officer", 1, "TTRK", "26", None, 0, json.dumps(foreign)))
        conn.execute("DELETE FROM deck_meta WHERE key='tokens_companion_edition_migration'")
    offline = get_deck(db, "mtgjson:Sample_TRC", catalog_db=catalog,
                       include_tokens=True, fetch_missing=False)["deck"]
    assert [row["name"] for row in offline["sections"]["tokens"]] == ["Ape"]


class _Response:
    status = 200
    headers = {}

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


def _make_color_catalog(path):
    import sqlite3

    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE printings(printing_id TEXT PRIMARY KEY, oracle_id TEXT);
            CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT, color_identity_json TEXT);
        """)
        conn.executemany("INSERT INTO printings VALUES(?,?)", [
            ("printing-w", "oracle-w"), ("printing-u", "oracle-u"), ("printing-r", "oracle-r")
        ])
        conn.executemany("INSERT INTO source_records VALUES(?,?,?)", [
            ("scryfall-w", "printing-w", '["W"]'),
            ("scryfall-u", "printing-u", '["U"]'),
            ("scryfall-r", "printing-r", '["R"]'),
        ])


def test_index_colors_joins_by_file_and_color_filter_contains_all_selected(tmp_path):
    db = tmp_path / "decks.sqlite"
    catalog = tmp_path / "catalogue.sqlite"
    _make_color_catalog(catalog)
    import_decks({"meta": {"version": "test"}, "data": [
        {"code": "TST", "fileName": "Azorius_TST", "name": "Azorius", "type": "Commander Deck"},
        {"code": "TST", "fileName": "Boros_TST", "name": "Boros", "type": "Commander Deck"},
        {"code": "TST", "fileName": "Esper_TST", "name": "Esper", "type": "Commander Deck"},
    ]}, db)
    payload = {"num_rows_total": 3, "rows": [
        {"row": {"file": "Azorius_TST", "cards": [
            {"oracle_id": "oracle-w"}, {"oracle_id": "oracle-u"}]}},
        {"row": {"file": "Boros_TST", "cards": [
            {"oracle_id": "oracle-w"}, {"oracle_id": "oracle-r"}]}},
        {"row": {"file": "Esper_TST", "cards": [
            {"oracle_id": "oracle-w"}, {"oracle_id": "oracle-u"}, {"oracle_id": "oracle-r"}]}},
    ]}
    progress = []
    with patch("bibliotheque_mtg.decks.urllib.request.urlopen", return_value=_Response(payload)):
        result = index_deck_colors(db, catalog, progress_callback=progress.append)

    assert result["decks"] == 3
    assert result["indexed"] == 3
    assert result["unmatched"] == 0
    assert progress[-1]["phase"] == "complete"
    assert list_decks(db, colors="WU")["total"] == 2
    assert list_decks(db, colors="WUB")["total"] == 0
    assert {tuple(item["colors"]) for item in list_decks(db, limit=10)["items"]} == {
        ("W", "U"), ("W", "R"), ("W", "U", "R")
    }


def test_failed_color_snapshot_does_not_apply_partial_updates(tmp_path):
    import sqlite3

    db = tmp_path / "decks.sqlite"
    catalog = tmp_path / "catalogue.sqlite"
    _make_color_catalog(catalog)
    import_decks({"meta": {"version": "test"}, "data": [
        {"code": "TST", "fileName": "Sample_TST", "name": "Sample", "type": "Commander Deck"},
    ]}, db)
    with patch("bibliotheque_mtg.decks.urllib.request.urlopen", side_effect=OSError("network unavailable")):
        with pytest.raises(OSError, match="network unavailable"):
            index_deck_colors(db, catalog)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT colors FROM decks WHERE file_name='Sample_TST'").fetchone()[0] is None
