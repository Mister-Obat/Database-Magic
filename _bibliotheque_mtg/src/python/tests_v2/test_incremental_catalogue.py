import gzip
import hashlib
import json
import sqlite3

from bibliotheque_mtg.sources import build_catalogue_db
from bibliotheque_mtg.v2.build_catalogue import build_catalogue
from bibliotheque_mtg.v2.incremental import build_incremental_version


def test_incremental_build_changes_only_target_edition(monkeypatch, tmp_path):
    import bibliotheque_mtg.v2.incremental as incremental

    config = {"scope": {"explicit_exclusions": [], "include_unreleased": False,
                        "include_digital": True, "include_paper": True,
                        "include_promos": True, "include_tokens": True}}
    monkeypatch.setattr(incremental, "_load_config", lambda *_: config)
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    sets = [{"id": "set-1", "code": "one", "name": "First", "set_type": "expansion",
             "released_at": "2020-01-01", "digital": False, "card_count": 2}]
    old_card = {"id": "card-old", "set_id": "set-1", "set": "one", "lang": "en",
                "collector_number": "1", "oracle_id": "oracle-old", "illustration_id": "art-old",
                "layout": "normal", "name": "Old Card", "type_line": "Creature", "cmc": 1,
                "colors": [], "color_identity": [], "rarity": "common"}
    new_card = {**old_card, "id": "card-new", "collector_number": "2",
                "oracle_id": "oracle-new", "illustration_id": "art-new", "name": "New Card"}
    untouched_set = {"id": "set-2", "code": "two", "name": "Untouched", "set_type": "expansion",
                     "released_at": "2019-01-01", "digital": False, "card_count": 1}
    untouched_card = {**old_card, "id": "card-untouched", "set_id": "set-2", "set": "two",
                      "oracle_id": "oracle-untouched", "illustration_id": "art-untouched",
                      "name": "Untouched Card"}

    def archive(path, cards):
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            for card in cards:
                stream.write(json.dumps(card) + "\n")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    old_version = tmp_path / "old"
    old_version.mkdir()
    old_archive = tmp_path / "old.jsonl.gz"
    old_sha = archive(old_archive, [old_card, untouched_card])
    old_source = tmp_path / "old-source.sqlite"
    build_catalogue_db(old_archive, [{**sets[0], "card_count": 1}, untouched_set], old_source, config,
                       {"updated_at": "2026-01-01", "sha256": old_sha, "validation": {"rows": 2}},
                       "sets-old", __import__("datetime").date(2026, 1, 1))
    build_catalogue(old_source, old_archive, old_version / "catalogue.sqlite",
                    archive_sha256=old_sha, archive_as_of="2026-01-01",
                    edition_root=old_version / "editions", total_records=2)
    old_index = json.loads((old_version / "editions" / "index.json").read_text())
    untouched_folder = next(item["folder"] for item in old_index["editions"] if item["id"] == "set-2")
    delta_archive = tmp_path / "delta.jsonl.gz"
    delta_sha = archive(delta_archive, [old_card, new_card])
    source_report = {"set_items": sets, "generated_at_utc": "2026-11-16T12:00:00Z",
                     "sources": {"scryfall_all_cards": {"archive_path": str(delta_archive),
                                 "archive_sha256": delta_sha, "rows": 2},
                                 "scryfall_sets": {"sha256": "sets-new"}},
                     "import": {"new_card_records": 1}}
    target = tmp_path / "new"
    result = build_incremental_version(config_path, old_version / "catalogue.sqlite",
                                       old_version, target, source_report, old_sha)
    with sqlite3.connect(target / "catalogue.sqlite") as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert {row[0] for row in conn.execute("SELECT provider_card_id FROM source_records")} == {
            "card-old", "card-new", "card-untouched"}
        assert conn.execute("SELECT COUNT(*) FROM source_search").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 2
    assert result["new_card_records"] == 1
    assert json.loads((target / "editions" / "index.json").read_text())["edition_count"] == 2
    assert (target / "editions" / untouched_folder / "index.json").read_bytes() == (
        old_version / "editions" / untouched_folder / "index.json").read_bytes()
