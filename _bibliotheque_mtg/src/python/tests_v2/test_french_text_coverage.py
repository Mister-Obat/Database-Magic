import json
import sqlite3
from pathlib import Path

import pytest

from bibliotheque_mtg import sources


def _source_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE cards (
        id TEXT PRIMARY KEY, oracle_id TEXT, lang TEXT NOT NULL, set_id TEXT,
        set_code TEXT NOT NULL, collector_number TEXT NOT NULL, layout TEXT,
        name TEXT NOT NULL, printed_text TEXT, oracle_text TEXT, faces_json TEXT, raw_json TEXT NOT NULL)""")
    conn.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
        "scryfall-en", "oracle-1", "en", "set-1", "40k", "176", "normal",
        "The Swarmlord", "Rapid Regeneration", "Rapid Regeneration", "[]",
        json.dumps({"oracle_id": "oracle-1", "name": "The Swarmlord",
        "oracle_text": "Rapid Regeneration", "layout": "normal"})))
    conn.commit()
    conn.close()


def _override(provider_id="scryfall-en"):
    return {"schema_version": 1, "entries": [{
        "provider_card_id": provider_id, "oracle_id": "oracle-1", "set_code": "40k",
        "collector_number": "176", "fields": {"printed_name": "Le Maître des Essaims",
            "printed_type_line": "Créature légendaire : tyranide", "mana_cost": "{3}{G}{U}{R}",
            "printed_text": "Régénération rapide — texte officiel", "power": "5", "toughness": "5"},
        "source": "wizards_official_manual_review",
        "source_url": "https://magic.wizards.com/fr/news/card-image-gallery/warhammer-40000-commander-decks",
        "source_image_url": "https://media.wizards.com/2022/40k/fr_QjfUnmccbR.png",
        "reviewed_at": "2026-11-16"}]}


def test_official_french_override_is_exact_idempotent_and_counts_for_coverage(tmp_path, monkeypatch):
    db = tmp_path / "catalogue.sqlite"
    _source_db(db)
    sidecar = tmp_path / "overrides.json"
    sidecar.write_text(json.dumps(_override(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sources, "OFFICIAL_FRENCH_OVERRIDES", sidecar)

    first = sources.apply_official_french_text_overrides(db)
    second = sources.apply_official_french_text_overrides(db)
    assert first["inserted"] == 1 and second["inserted"] == second["updated"] == 0

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT oracle_id,set_code,collector_number,language,fields_json,source_image_url "
                       "FROM official_french_text").fetchone()
    conn.close()
    assert row[:4] == ("oracle-1", "40k", "176", "fr")
    assert json.loads(row[4])["printed_name"] == "Le Maître des Essaims"
    assert row[5].endswith("fr_QjfUnmccbR.png")
    coverage = sources.official_french_text_coverage(db)
    assert coverage["status"] == "complete"
    assert coverage["oracle_ids_with_official_french_text"] == 1
    assert coverage["curated_official_french_records"] == 1
    assert coverage["english_printings_with_exact_set_number_french_record"] == 1
    assert coverage["english_printings_with_curated_official_french_text"] == 1


def test_override_rejects_identity_mismatch(tmp_path, monkeypatch):
    db = tmp_path / "catalogue.sqlite"
    _source_db(db)
    sidecar = tmp_path / "overrides.json"
    sidecar.write_text(json.dumps(_override("wrong-id")), encoding="utf-8")
    monkeypatch.setattr(sources, "OFFICIAL_FRENCH_OVERRIDES", sidecar)
    with pytest.raises(sources.SourceFormatError, match="does not match one exact local source card"):
        sources.apply_official_french_text_overrides(db)


def test_coverage_reports_incomplete_without_french_source(tmp_path):
    db = tmp_path / "catalogue.sqlite"
    _source_db(db)
    coverage = sources.official_french_text_coverage(db)
    assert coverage["status"] == "incomplete"
    assert coverage["english_oracle_ids"] == 1
    assert coverage["oracle_ids_missing_official_french_text"] == 1
