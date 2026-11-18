import gzip
import hashlib
import json
import sqlite3

from bibliotheque_mtg import sources


def test_additions_only_fetches_only_growing_set(monkeypatch, tmp_path):
    ops = tmp_path
    config = ops / "config.json"
    config.write_text("{}", encoding="utf-8")
    current = ops / "current.sqlite"
    with sqlite3.connect(current) as conn:
        conn.execute("CREATE TABLE editions(set_id TEXT, source_card_count INTEGER, eligibility TEXT, released_at TEXT)")
        conn.executemany("INSERT INTO editions VALUES(?,?,?,?)", [("old", 1, "eligible", "2020-01-01"), ("new", 0, "deferred", "2026-11-01")])
        conn.execute("CREATE TABLE printings(printing_id TEXT, set_id TEXT)")
        conn.execute("CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT)")
        conn.execute("INSERT INTO printings VALUES('print-old','old')")
        conn.execute("INSERT INTO source_records VALUES('old-card','print-old')")
    monkeypatch.setattr(sources, "_load_config", lambda *_: {"network": {
        "user_agent": "test", "minimum_scryfall_interval_ms": 0}})
    monkeypatch.setattr(sources, "_set_reason", lambda item, *_: ("eligible", None))
    monkeypatch.setattr(sources, "_fetch_set_pages", lambda *_: ([
        {"id": "old", "code": "OLD", "card_count": 1},
        {"id": "new", "code": "NEW", "card_count": 1}],
        [{"sha256": "page"}]))
    requested = []

    def pages(_client, url):
        requested.append(url)
        yield url, {"data": [{"id": "new-card", "set_id": "new", "lang": "fr"}], "has_more": False}

    monkeypatch.setattr(sources, "iter_api_pages", pages)
    result = sources.run_additions_only(config, current)
    assert result["import"]["new_card_records"] == 1
    assert len(requested) == 1 and "e%3ANEW" in requested[0]
    with gzip.open(result["sources"]["scryfall_all_cards"]["archive_path"], "rb") as stream:
        assert {json.loads(line)["id"] for line in stream} == {"new-card"}


def test_additions_only_skips_archive_when_counts_unchanged(monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    current = tmp_path / "current.sqlite"
    with sqlite3.connect(current) as conn:
        conn.execute("CREATE TABLE editions(set_id TEXT, source_card_count INTEGER, eligibility TEXT, released_at TEXT)")
        conn.execute("INSERT INTO editions VALUES('old', 1, 'eligible', '2020-01-01')")
    monkeypatch.setattr(sources, "_load_config", lambda *_: {"network": {
        "user_agent": "test", "minimum_scryfall_interval_ms": 0}})
    monkeypatch.setattr(sources, "_set_reason", lambda item, *_: ("eligible", None))
    monkeypatch.setattr(sources, "_fetch_set_pages", lambda *_: ([
        {"id": "old", "code": "OLD", "card_count": 1}], [{"sha256": "page"}]))
    existing_catalogue = tmp_path / "sources" / "wizards" / "gallery_catalogue.json"
    existing_catalogue.parent.mkdir(parents=True)
    existing_catalogue.write_text(json.dumps({"articles": [
        {"url": "https://magic.wizards.com/fr/news/card-image-gallery/old"}],
        "unique_gallery_articles": 1}), encoding="utf-8")
    calls = []
    archive_html = b'<html><a href="/fr/products/new-set/card-image-gallery">New Set</a></html>'

    def fetch_index(_client, url, path, *, expected):
        calls.append((url, path, expected))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(archive_html)
        return archive_html, {"sha256": hashlib.sha256(archive_html).hexdigest()}, True

    monkeypatch.setattr(sources, "fetch_cached", fetch_index)
    result = sources.run_additions_only(config, current)
    assert result["status"] == "no_change"
    assert result["wizards_gallery_index"]["new_gallery_links"] == 1
    assert len(calls) == 1 and calls[0][0] == sources.WIZARDS_PRODUCT_ARCHIVE
    updated = json.loads(existing_catalogue.read_text(encoding="utf-8"))
    assert {row["url"] for row in updated["articles"]} == {
        "https://magic.wizards.com/fr/news/card-image-gallery/old",
        "https://magic.wizards.com/fr/products/new-set/card-image-gallery",
    }
