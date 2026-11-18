from bibliotheque_mtg.list_import import parse_list, resolve_list


def test_parse_list_accepts_names_and_edhrec_links():
    assert parse_list("Foudre\n[Tour](https://edhrec.com/cards/command-tower)\n") == [
        ("Foudre", None), ("command tower", "https://edhrec.com/cards/command-tower")]


def test_parse_list_rejects_other_hosts():
    import pytest
    with pytest.raises(ValueError):
        parse_list("https://example.com/cards/command-tower")


def test_resolve_exact_printing(tmp_path):
    import sqlite3
    db = tmp_path / "catalogue.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE editions(set_id TEXT, code TEXT, name TEXT, released_at TEXT, eligibility TEXT);
        CREATE TABLE printings(printing_id TEXT, set_id TEXT, collector_number TEXT);
        CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT, name TEXT, printed_name TEXT, lang TEXT);
        CREATE VIRTUAL TABLE source_search USING fts5(provider_card_id UNINDEXED, name, printed_name);
        INSERT INTO editions VALUES('old','old','Old','2020-01-01','eligible'),('new','new','New','2025-01-01','eligible');
        INSERT INTO printings VALUES('p1','old','1'),('p2','new','2');
        INSERT INTO source_records VALUES('en1','p1','Lightning Bolt',NULL,'en'),('en2','p2','Lightning Bolt',NULL,'en'),('fr2','p2','Lightning Bolt','Foudre','fr');
        INSERT INTO source_search VALUES('en1','Lightning Bolt',NULL),('en2','Lightning Bolt',NULL),('fr2','Lightning Bolt','Foudre');
    """)
    conn.close()
    result = resolve_list(db, "Foudre\nLightning Bolt")
    assert [item["provider_card_id"] for item in result] == ["fr2", "fr2"]
    assert all(item["set_code"] == "new" for item in result)
