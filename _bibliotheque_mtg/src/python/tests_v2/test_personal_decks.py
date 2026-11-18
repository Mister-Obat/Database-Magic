import copy
import json
import sqlite3
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from bibliotheque_mtg import personal_decks as personal


@pytest.fixture
def cards(tmp_path):
    """Small complete local catalogue; no dependency on other test modules."""
    version = tmp_path / "version"
    db = version / "catalogue.sqlite"
    (version / "assets" / "webp" / "fr").mkdir(parents=True)
    conn = sqlite3.connect(db)
    try:
        conn.executescript("""
            CREATE TABLE editions(set_id TEXT, code TEXT, eligibility TEXT);
            CREATE TABLE printings(printing_id TEXT, set_id TEXT, layout TEXT, collector_number TEXT);
            CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT, lang TEXT,
                printed_name TEXT, name TEXT, mana_value REAL, type_line TEXT);
            CREATE TABLE card_faces(printing_id TEXT, face_index INTEGER, physical_image INTEGER);
            CREATE TABLE image_assets(asset_id TEXT, relative_path TEXT);
            CREATE TABLE image_slots(printing_id TEXT, face_index INTEGER, status TEXT, asset_id TEXT);
            INSERT INTO editions VALUES('set','TST','eligible');
        """)
        for printing, provider, name, french, mana, faces, kind in [
            ("p1", "id1", "Fire // Ice", "Feu // Glace", 2.0, 2, "Creature"),
            ("p2", "id2", "Token Goblin", None, 0.0, 1, "Token Creature"),
        ]:
            conn.execute("INSERT INTO printings VALUES(?,?,?,?)",
                         (printing, "set", "modal_dfc" if faces > 1 else "normal", provider))
            conn.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?)",
                         (provider, printing, "en", None, name, mana, kind))
            if french:
                conn.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?)",
                             (provider + "fr", printing, "fr", french, french, mana, kind))
            for face in range(faces):
                asset = f"{printing}-{face}"
                relative = f"assets/webp/fr/{asset}.webp"
                conn.execute("INSERT INTO card_faces VALUES(?,?,1)", (printing, face))
                conn.execute("INSERT INTO image_assets VALUES(?,?)", (asset, relative))
                conn.execute("INSERT INTO image_slots VALUES(?,?,'ready',?)", (printing, face, asset))
                with Image.new("RGB", (10, 10), (face * 80, 20, 30)) as image:
                    image.save(version / relative, "WEBP")
        conn.commit()
    finally:
        conn.close()
    return db


def test_durable_revisions_backup_and_conflict(tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Premier", ["R", "U"], True)
    stale = copy.deepcopy(deck)
    deck["name"] = "Renommé"
    saved = personal.save_deck(db, deck)
    assert saved["revision"] == 2
    assert personal.get_deck(db, saved["id"])["name"] == "Renommé"
    with pytest.raises(personal.RevisionConflict):
        personal.save_deck(db, stale)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM personal_deck_revisions").fetchone()[0] == 2
    backup = tmp_path / "personal-decks-backups/latest.sqlite"
    restored = personal.restore_backup(backup, tmp_path / "restored.sqlite")
    assert personal.get_deck(restored, saved["id"])["name"] == "Renommé"
    with pytest.raises(FileExistsError):
        personal.restore_backup(backup, db)


def test_exact_identity_quantities_moves_and_nonempty_section_removal(cards, tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Deck", [])
    with pytest.raises(ValueError, match="exacte"):
        personal.add_card(db, deck["id"], cards, "id1", "wrong-printing")
    deck = personal.add_card(db, deck["id"], cards, "id1", "p1")
    deck = personal.add_card(db, deck["id"], cards, "id1", "p1")
    card = deck["sections"][0]["cards"][0]
    assert card["count"] == 2
    card["provider_card_id"] = "id2"  # Client metadata cannot change the exact identity.
    target = {"id": str(uuid.uuid4()), "name": "Favoris", "cards": [card]}
    deck["sections"][0]["cards"] = []
    deck["sections"].append(target)
    saved = personal.save_deck(db, deck)
    assert saved["sections"][-1]["cards"][0]["provider_card_id"] == "id1"
    saved["sections"].pop()
    saved = personal.save_deck(db, saved)
    assert saved["sections"][0]["cards"][0]["count"] == 2
    assert personal.with_media(saved, cards)["cover_image_path"]


def test_import_keeps_all_sections_unresolved_counts_and_png_export(cards, tmp_path):
    db = tmp_path / "personal.sqlite"
    source = {"id": "precon", "name": "Deck", "colors": "UR", "sections": {
        "commander": [{"name": "Commander", "provider_card_id": "id1", "count": 1}],
        "mainboard": [{"name": "Fire", "provider_card_id": "id1", "count": 2}],
        "sideboard": [{"name": "Unknown", "provider_card_id": None, "count": 3}],
        "planar_deck": [{"name": "Plane", "provider_card_id": None, "count": 1}],
        "tokens": [{"name": "Goblin", "provider_card_id": "id2", "count": 2}]}}
    deck = personal.import_preconstructed(db, source, cards)
    assert deck["sections"][0]["name"] == "Commandant"
    assert sum(c["count"] for s in deck["sections"] for c in s["cards"]) == 9
    assert {s["name"] for s in deck["sections"]} >= {"Réserve", "Deck planaire", "Terrains", "Créatures"}
    assert next(s for s in deck["sections"] if s["name"] == "Créatures")["cards"][0]["count"] == 2
    with pytest.raises(ValueError, match="introuvable"):
        personal.export_deck(db, deck["id"], cards, tmp_path / "Desktop")
    for section in deck["sections"]:
        section["cards"] = [c for c in section["cards"] if c.get("provider_card_id")]
        if section.get("source_section") == "tokens":
            section["name"] = "Mes jetons"
    saved = personal.save_deck(db, deck)
    first = personal.export_deck(db, saved["id"], cards, tmp_path / "Desktop")
    second = personal.export_deck(db, saved["id"], cards, tmp_path / "Desktop")
    assert first["total"] == 8
    assert first["folder"] != second["folder"]
    assert len(list((Path(first["folder"]) / "Jetons").glob("*.png"))) == 2


def test_parallel_additions_do_not_lose_updates(cards, tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Concurrent", [])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: personal.add_card(db, deck["id"], cards, "id1"), range(8)))
    saved = personal.get_deck(db, deck["id"])
    assert saved["revision"] == 9
    assert saved["sections"][0]["cards"][0]["count"] == 8
    restored = personal.restore_backup(tmp_path / "personal-decks-backups/latest.sqlite", tmp_path / "copy.sqlite")
    assert personal.get_deck(restored, deck["id"])["revision"] == 9


def test_snapshot_survives_catalogue_card_disappearing(cards, tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Snapshot", [])
    deck = personal.add_card(db, deck["id"], cards, "id1")
    with sqlite3.connect(cards) as conn:
        conn.execute("DELETE FROM source_records WHERE printing_id='p1'")
    card = personal.with_media(personal.get_deck(db, deck["id"]), cards)["sections"][0]["cards"][0]
    assert card["name"] == "Fire // Ice"
    assert card["mana_value"] == 2
    assert card["image_status"] == "unresolved"


def test_commander_cover_follows_first_commander_independent_of_section_order(cards, tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Partners", [], commander=True)
    command = deck["sections"][0]
    assert command["source_section"] == "commander"
    deck = personal.add_card(db, deck["id"], cards, "id1", section_id=command["id"])
    deck = personal.add_card(db, deck["id"], cards, "id2", section_id=command["id"])
    assert personal.with_media(deck, cards)["cover_card"]["provider_card_id"] == "id1"
    # Rename and move the command zone; its stable role remains authoritative.
    command = deck["sections"].pop(0)
    command["name"] = "Commandement"
    deck["sections"].append(command)
    command["cards"].reverse()
    deck = personal.save_deck(db, deck)
    media = personal.with_media(deck, cards)
    assert media["cover_card"]["provider_card_id"] == "id2"
    assert media["cover_image_path"] == "assets/webp/fr/p2-0.webp"
    command = deck["sections"][-1]
    command["cards"].clear()
    # An empty command zone has no cover, even with cards in another section.
    deck = personal.add_card(db, deck["id"], cards, "id1", section_id=deck["sections"][0]["id"])
    deck["sections"][-1]["cards"].clear()
    deck = personal.save_deck(db, deck)
    assert personal.with_media(deck, cards)["cover_card"] is None


def test_secondary_backup_failure_does_not_reject_committed_mutation(tmp_path, monkeypatch):
    db = tmp_path / "personal.sqlite"
    monkeypatch.setattr(personal.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk")))
    deck = personal.create_deck(db, "Saved", [])
    assert personal.get_deck(db, deck["id"])["name"] == "Saved"


@pytest.mark.parametrize("sections", [[None], [{"id": [], "cards": []}],
    [{"id": str(uuid.uuid4()), "name": "X", "cards": ["invalid"]}]])
def test_malformed_sections_leave_revision_unchanged(tmp_path, sections):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Validate", [])
    draft = {**deck, "sections": sections}
    with pytest.raises(ValueError):
        personal.save_deck(db, draft)
    assert personal.get_deck(db, deck["id"])["revision"] == 1


def test_restore_keeps_corrupt_original_untouched(tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Recovery", [])
    db.write_bytes(b"corrupt original")
    backup = tmp_path / "personal-decks-backups/latest.sqlite"
    restored = personal.restore_backup(backup, tmp_path / "recovered.sqlite")
    assert db.read_bytes() == b"corrupt original"
    assert personal.get_deck(restored, deck["id"])["name"] == "Recovery"


def test_restore_deleted_deck_preserves_composition_and_history(cards, tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Recovery", ["W"])
    deck = personal.add_card(db, deck["id"], cards, "id1")
    deleted = personal.delete_deck(db, deck["id"], deck["revision"])
    assert deleted["deleted_at"]
    restored = personal.restore_deleted_deck(db, deck["id"])
    assert restored["sections"] == deck["sections"]
    assert restored["revision"] == 4
    assert personal.list_decks(db) == [restored]
    with sqlite3.connect(db) as conn:
        history = [json.loads(row[0]) for row in conn.execute(
            "SELECT document FROM personal_deck_revisions WHERE deck_id=? ORDER BY revision", (deck["id"],))]
    assert [entry["revision"] for entry in history] == [1, 2, 3, 4]
    assert [entry["revision"] for entry in history if entry.get("deleted_at")] == [3]


def test_optional_section_color_persists_and_rejects_invalid_values(tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Coding", [], commander=True)
    deck["sections"][0]["color"] = "C"
    deck = personal.save_deck(db, deck)
    assert personal.get_deck(db, deck["id"])["sections"][0]["color"] == "C"
    deck["sections"][0].pop("color")
    deck = personal.save_deck(db, deck)
    assert deck["sections"][0]["color"] == "C"
    deck["sections"][0]["color"] = ["W"]
    with pytest.raises(ValueError, match="Couleur de section invalide"):
        personal.save_deck(db, deck)
    assert personal.get_deck(db, deck["id"])["revision"] == 3
    deck["sections"][0]["color"] = None
    deck = personal.save_deck(db, deck)
    assert "color" not in deck["sections"][0]
    assert deck["sections"][0]["source_section"] == "commander"


def test_http_origin_routes_and_stale_save(cards, tmp_path, monkeypatch):
    from bibliotheque_mtg import local_app as app
    monkeypatch.setattr(app, "PERSONAL_DECK_DB", tmp_path / "personal.sqlite")
    monkeypatch.setattr(app.LibraryHandler, "db", property(lambda self: cards))
    server = app.LocalServer(("127.0.0.1", 0), app.LibraryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, payload=None, origin=None):
        req = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"Content-Type": "application/json", "Origin": origin or base})
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    try:
        assert request("/api/v2/personal-decks/create", {"name": "X"}, "https://evil.test")[0] == 403
        status, result = request("/api/v2/personal-decks/create", {"name": "HTTP", "colors": ["W"]})
        assert status == 200
        deck = result["deck"]
        assert request("/api/v2/personal-decks/add", {"deck_id": deck["id"], "provider_card_id": "id1"})[0] == 200
        assert request("/api/v2/personal-decks/save", {"deck": deck})[0] == 409
        assert request("/api/v2/personal-deck?id=" + deck["id"])[1]["deck"]["revision"] == 2
        assert len(request("/api/v2/personal-decks?q=HTTP&colors=W")[1]["items"]) == 1
        assert request("/api/v2/personal-decks/create", {"name": "bad", "colors": [1]})[0] == 400
        delete_url = "/api/v2/personal-decks/delete"
        delete_request = {"deck_id": deck["id"], "revision": 1}
        assert request(delete_url, delete_request, "https://evil.test")[0] == 403
        assert request(delete_url, delete_request)[0] == 409
        delete_request["revision"] = 2
        assert request(delete_url, delete_request) == (
            200, {"deleted": True, "deck_id": deck["id"], "revision": 3})
        assert request("/api/v2/personal-decks")[1]["items"] == []
        assert request("/api/v2/personal-deck?id=" + deck["id"])[0] == 404
        assert request("/api/v2/personal-decks/save", {"deck": deck})[0] == 404
        assert request("/api/v2/personal-decks/add", {"deck_id": deck["id"], "provider_card_id": "id1"})[0] == 404
        assert request("/api/v2/personal-decks/export", {"deck_id": deck["id"]})[0] == 404
        assert request(delete_url, delete_request)[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_import_unpacks_real_preconstructed_response_and_keeps_112_cards(cards, tmp_path, monkeypatch):
    from bibliotheque_mtg import decks, local_app as app

    precon_db = tmp_path / "preconstructed.sqlite"
    personal_db = tmp_path / "personal.sqlite"
    exact = {"name": "Fire", "set_code": "TST", "number": "id1",
             "identifiers": {"scryfallId": "id1"}, "colorIdentity": ["U", "R"]}
    decks.import_decks([{"name": "Complete precon", "code": "TST", "type": "Commander Deck",
        "cards": [{**exact, "count": 99}], "commander": [{**exact, "count": 1}],
        "sideboard": [{"name": "Unresolved reserve", "count": 2}],
        "tokens": [{"name": "Goblin", "count": 10, "set_code": "TST", "number": "id2",
                    "identifiers": {"scryfallId": "id2"}}]}], precon_db)
    precon_id = decks.list_decks(precon_db)["items"][0]["id"]
    source = decks.get_deck(precon_db, precon_id, catalog_db=cards,
                            fetch_missing=False, include_tokens=True)
    assert source["found"] is True and source["deck"]["name"] == "Complete precon"
    assert sum(c["count"] for group in source["deck"]["sections"].values() for c in group) == 112
    monkeypatch.setattr(app, "DECK_DB", precon_db)
    monkeypatch.setattr(app, "PERSONAL_DECK_DB", personal_db)
    monkeypatch.setattr(app.LibraryHandler, "db", property(lambda self: cards))
    server = app.LocalServer(("127.0.0.1", 0), app.LibraryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        req = urllib.request.Request(base + "/api/v2/personal-decks/import",
                data=json.dumps({"deck_id": precon_id}).encode(),
                headers={"Content-Type": "application/json", "Origin": base})
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 200
            imported = json.load(response)["deck"]
        assert imported["card_count"] == 112
        assert imported["name"] == "Complete precon"
        assert imported["colors"] == ["U", "R"]
        assert next(s for s in imported["sections"] if s["name"] == "Réserve")["cards"][0]["count"] == 2
        assert next(s for s in imported["sections"] if s["name"] == "Jetons")["cards"][0]["count"] == 10
        assert personal.get_deck(personal_db, imported["id"])["source_deck_id"] == precon_id
        req.data = json.dumps({"deck_id": "does-not-exist"}).encode()
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req, timeout=5)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
