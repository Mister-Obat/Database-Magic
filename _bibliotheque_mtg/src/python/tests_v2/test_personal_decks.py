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
            CREATE TABLE printings(printing_id TEXT, set_id TEXT, layout TEXT, collector_number TEXT,
                oracle_id TEXT);
            CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT, lang TEXT,
                printed_name TEXT, name TEXT, mana_value REAL, type_line TEXT, colors_key TEXT, raw_json TEXT);
            CREATE TABLE card_faces(printing_id TEXT, face_index INTEGER, physical_image INTEGER);
            CREATE TABLE image_assets(asset_id TEXT, relative_path TEXT);
            CREATE TABLE image_slots(printing_id TEXT, face_index INTEGER, status TEXT, asset_id TEXT);
            INSERT INTO editions VALUES('set','TST','eligible');
        """)
        for printing, provider, name, french, mana, faces, kind in [
            ("p1", "id1", "Fire // Ice", "Feu // Glace", 2.0, 2, "Creature"),
            ("p2", "id2", "Token Goblin", None, 0.0, 1, "Token Creature"),
        ]:
            conn.execute("INSERT INTO printings VALUES(?,?,?,?,?)",
                         (printing, "set", "modal_dfc" if faces > 1 else "normal", provider, f"oracle-{printing}"))
            conn.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?,NULL)",
                         (provider, printing, "en", None, name, mana, kind, "UR" if faces > 1 else "R"))
            if french:
                conn.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?,NULL)",
                             (provider + "fr", printing, "fr", french, french, mana, kind, "UR"))
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


def test_format_templates_and_current_bans(cards, tmp_path):
    db = tmp_path / "personal.sqlite"
    with sqlite3.connect(cards) as conn:
        conn.execute("UPDATE source_records SET raw_json=? WHERE provider_card_id='id1'", (
            json.dumps({"legalities": {"modern": "banned", "commander": "legal", "vintage": "restricted"}}),
        ))
    deck = personal.create_deck(db, "Formats", [], deck_format="modern")
    assert any(section.get("source_section") == "sideboard" for section in deck["sections"])
    assert not any(section.get("source_section") == "commander" for section in deck["sections"])
    commander = personal.create_deck(db, "Commander", [], deck_format="commander")
    assert commander["sections"][0]["source_section"] == "commander"
    assert not any(section.get("source_section") == "sideboard" for section in commander["sections"])
    deck = personal.add_card(db, deck["id"], cards, "id1fr")
    assert personal.with_media(deck, cards)["cover_card"]["banned_formats"] == ["modern"]
    draft = personal.with_media(deck, cards)
    draft["format"] = "commander"
    saved = personal.save_deck(db, draft)
    assert saved["format"] == "commander"
    assert saved["sections"] == deck["sections"]
    assert personal.duplicate_deck(db, deck["id"], "Copie", saved["revision"])["format"] == "commander"
    assert personal._read_document(json.dumps({k: v for k, v in commander.items() if k != "format"}))[
        "format"] == "commander"
    draft = personal.with_media(saved, cards)
    draft["format"] = "inventé"
    with pytest.raises(ValueError, match="Format"):
        personal.save_deck(db, draft)


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


@pytest.mark.parametrize("design", [
    "standard", "obsidian", "plains", "island", "swamp", "mountain", "forest",
    "silver", "copper", "indigo", "braise", "abyssal", "canopy", "glacier", "amethyst",
    "orbital", "reactor", "biotech", "hyperspace", "cryovault",
])
def test_box_design_defaults_and_survives_save_and_duplicate(tmp_path, design):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Ancien deck", [])
    assert deck.pop("box_design") == "standard"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE personal_decks SET document=? WHERE id=?", (json.dumps(deck), deck["id"]))
    assert personal.get_deck(db, deck["id"])["box_design"] == "standard"

    deck = personal.save_deck(db, {**deck, "box_design": design})
    assert personal.summary_with_media(deck, None)["box_design"] == design
    with pytest.raises(ValueError, match="Modèle"):
        personal.save_deck(db, {**deck, "box_design": None})
    del deck["box_design"]
    deck = personal.save_deck(db, deck)
    assert personal.get_deck(db, deck["id"])["box_design"] == design
    copied = personal.duplicate_deck(db, deck["id"], "Copie", deck["revision"])
    assert copied["box_design"] == design


def test_retired_eclipse_deck_can_still_be_saved(tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Ancien deck", [])
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE personal_decks SET document=? WHERE id=?",
                     (json.dumps({**deck, "box_design": "eclipse"}), deck["id"]))
    opened = personal.get_deck(db, deck["id"])
    assert opened["box_design"] == "standard"
    saved = personal.save_deck(db, opened)
    assert saved["box_design"] == "standard"
    with pytest.raises(ValueError, match="Modèle de boîte"):
        personal.save_deck(db, {**saved, "box_design": "eclipse"})


def test_deck_groups_migrate_a_legacy_database_and_preserve_compositions(tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Ancien deck", ["G"])
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE personal_deck_group_assignments")
        conn.execute("DROP TABLE personal_deck_groups")
    assert personal.list_deck_groups(db) == []
    assert personal.list_decks(db, include_groups=True) == [{**deck, "group_id": None}]

    first = personal.create_deck_group(db, "Favoris")
    second = personal.create_deck_group(db, "À tester")
    assert personal.list_deck_groups(db) == [second, first]
    assert personal.rename_deck_group(db, second["id"], "  Projets  ") == {**second, "name": "Projets"}
    assert personal.move_deck_to_group(db, deck["id"], first["id"]) == {
        "id": deck["id"], "group_id": first["id"]}
    personal.move_deck_to_group(db, deck["id"], second["id"])
    with pytest.raises(KeyError, match="Section"):
        personal.move_deck_to_group(db, deck["id"], "absent")
    current = personal.list_decks(db, include_groups=True)[0]
    assert current == {**deck, "group_id": second["id"]}
    assert personal.summary_with_media(current, None)["group_id"] == second["id"]

    backup = tmp_path / "personal-decks-backups/latest.sqlite"
    restored = personal.restore_backup(backup, tmp_path / "restored.sqlite")
    assert personal.list_decks(restored, include_groups=True) == [current]
    assert personal.list_deck_groups(restored) == [{**second, "name": "Projets"}, first]

    personal.delete_deck_group(db, second["id"])
    assert personal.list_deck_groups(db) == [first]
    assert personal.list_decks(db, include_groups=True) == [{**deck, "group_id": None}]
    personal.move_deck_to_group(db, deck["id"], first["id"])
    personal.move_deck_to_group(db, deck["id"], None)
    assert personal.list_decks(db, include_groups=True) == [{**deck, "group_id": None}]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM personal_deck_revisions").fetchone()[0] == 1


def test_manual_deck_order_survives_reopening_and_section_moves(tmp_path):
    db = tmp_path / "personal.sqlite"
    a, b, c, d, e = [personal.create_deck(db, name, [])["id"] for name in "ABCDE"]
    group = personal.create_deck_group(db, "Favoris")["id"]
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE personal_decks DROP COLUMN list_position")

    def ids(path, group_id):
        return [deck["id"] for deck in personal.list_decks(path, include_groups=True)
                if deck["group_id"] == group_id]

    assert ids(db, None) == [e, d, c, b, a]
    personal.move_deck_to_group(db, e, None, before_deck_id=b)
    assert ids(db, None) == [d, c, e, b, a]
    personal.move_deck_to_group(db, d, group, before_deck_id=None)
    personal.move_deck_to_group(db, c, group, before_deck_id=d)
    personal.move_deck_to_group(db, a, group, before_deck_id=d)
    assert ids(db, group) == [c, a, d]
    with pytest.raises(ValueError, match="destination"):
        personal.move_deck_to_group(db, a, None, before_deck_id=c)
    assert ids(db, group) == [c, a, d]
    personal.move_deck_to_group(db, a, None, before_deck_id=b)
    assert ids(db, None) == [e, a, b]
    personal.move_deck_to_group(db, c, group, before_deck_id=None)
    assert ids(db, group) == [d, c]

    backup = tmp_path / "personal-decks-backups/latest.sqlite"
    reopened = personal.restore_backup(backup, tmp_path / "reopened.sqlite")
    assert ids(reopened, group) == [d, c]
    assert ids(reopened, None) == [e, a, b]
    assert personal.get_deck(reopened, c)["revision"] == 1
    personal.delete_deck_group(reopened, group)
    assert ids(reopened, None) == [e, a, b, d, c]


def test_create_from_text_preserves_unresolved_cards_and_empty_sections(tmp_path):
    db = tmp_path / "personal.sqlite"
    card = {"name": "Carte inconnue", "count": 2, "set_code": "TST", "collector_number": "42"}
    sections = [{"name": "Commandant", "source_section": "commander", "cards": []},
                {"name": "Deck principal", "source_section": "mainboard", "cards": [card, card]}]
    deck = personal.create_deck_from_text(db, "Import", [], sections)
    assert personal.get_deck(db, deck["id"]) == deck
    assert deck["revision"] == 1
    assert deck["sections"][0]["source_section"] == "commander"
    assert deck["sections"][0]["cards"] == []
    cards = deck["sections"][1]["cards"]
    assert [{key: value for key, value in saved.items() if key != "id"} for saved in cards] == [card, card]
    assert cards[0]["id"] != cards[1]["id"]
    with pytest.raises(ValueError, match="Quantité"):
        personal.create_deck_from_text(db, "Invalide", [], [{"name": "Deck", "cards": [{**card, "count": 0}]}])
    assert personal.list_decks(db) == [deck]


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


def test_legacy_display_commander_filtered_without_rewriting_history(tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Legacy", [], commander=True)
    commander = {"id": str(uuid.uuid4()), "count": 1, "name": "Commander"}
    deck["sections"][0]["cards"].append(commander)
    deck["sections"].append({
        "id": str(uuid.uuid4()), "name": "Renamed display", "source_section": "display_commander",
        "cards": [{**commander, "id": str(uuid.uuid4())}],
    })
    document = json.dumps(deck)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE personal_decks SET document=? WHERE id=?", (document, deck["id"]))
        conn.execute("UPDATE personal_deck_revisions SET document=? WHERE deck_id=?", (document, deck["id"]))
    visible = personal.get_deck(db, deck["id"])
    assert visible["sections"] == deck["sections"][:-1]
    assert personal.list_decks(db) == [visible]
    assert personal.save_deck(db, visible)["sections"] == visible["sections"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT document FROM personal_deck_revisions WHERE revision=1").fetchone() == (document,)


def test_import_keeps_sections_except_display_commander_and_png_export(cards, tmp_path):
    db = tmp_path / "personal.sqlite"
    source = {"id": "precon", "name": "Deck", "colors": "UR", "sections": {
        "commander": [{"name": "Commander", "provider_card_id": "id1", "count": 1}],
        "display_commander": [{"name": "Commander", "provider_card_id": None, "count": 1}],
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
    assert card["colors_key"] == "UR"
    assert card["image_status"] == "unresolved"


def test_cover_follows_first_card_across_sections(cards, tmp_path):
    db = tmp_path / "personal.sqlite"
    deck = personal.create_deck(db, "Partners", [], commander=True)
    command = deck["sections"][0]
    assert command["source_section"] == "commander"
    deck = personal.add_card(db, deck["id"], cards, "id1", section_id=command["id"])
    deck = personal.add_card(db, deck["id"], cards, "id2", section_id=command["id"])
    assert personal.with_media(deck, cards)["cover_card"]["provider_card_id"] == "id1"
    # Moving the command zone after empty sections preserves its first card.
    command = deck["sections"].pop(0)
    command["name"] = "Commandement"
    deck["sections"].append(command)
    command["cards"].reverse()
    deck = personal.save_deck(db, deck)
    media = personal.with_media(deck, cards)
    assert media["cover_card"]["provider_card_id"] == "id2"
    assert media["cover_image_path"] == "assets/webp/fr/p2-0.webp"
    # A nonempty section ahead of the command zone now supplies the cover.
    deck = personal.add_card(db, deck["id"], cards, "id1", section_id=deck["sections"][1]["id"])
    assert personal.with_media(deck, cards)["cover_card"]["provider_card_id"] == "id1"
    # Leading empty sections and an empty command zone do not hide that card.
    deck["sections"][-1]["cards"].clear()
    deck = personal.save_deck(db, deck)
    assert personal.with_media(deck, cards)["cover_card"]["provider_card_id"] == "id1"
    assert personal.summary_with_media(deck, cards)["cover_card"]["provider_card_id"] == "id1"
    deck["sections"][1]["cards"].clear()
    assert personal.summary_with_media(deck, cards)["cover_card"] is None


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
    with sqlite3.connect(cards) as conn:
        conn.executescript("""
            ALTER TABLE editions ADD COLUMN released_at TEXT;
            ALTER TABLE source_records ADD COLUMN color_identity_json TEXT NOT NULL DEFAULT '[]';
        """)
    export = app.personal_deck_text.export_text
    desktop = tmp_path / "test-desktop"
    monkeypatch.setattr(app.personal_deck_text, "export_text",
                        lambda db, deck_id: export(db, deck_id, desktop))
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
        opened = request("/api/v2/personal-deck?id=" + deck["id"])[1]["deck"]
        assert opened["revision"] == 2 and opened["box_design"] == "standard"
        opened["box_design"] = "obsidian"
        assert request("/api/v2/personal-decks/save", {"deck": opened})[0] == 200
        for design in personal.PANORAMIC_BOX_DESIGNS:
            with urllib.request.urlopen(base + f"/deck-box-{design}.webp", timeout=5) as texture:
                assert texture.headers.get_content_type() == "image/webp"
                assert texture.read(12)[8:] == b"WEBP"
        for design in personal.BOX_DESIGNS:
            with urllib.request.urlopen(base + f"/deck-box-preview-{design}.webp", timeout=5) as texture:
                assert texture.headers.get_content_type() == "image/webp"
                assert texture.read(12)[8:] == b"WEBP"
        assert request("/api/v2/recent-activity")[1]["personal_decks"][0]["box_design"] == "obsidian"
        assert request("/api/v2/personal-decks?summary=1")[1]["items"][0]["box_design"] == "obsidian"
        assert len(request("/api/v2/personal-decks?q=HTTP&colors=W")[1]["items"]) == 1
        assert request("/api/v2/personal-decks/create", {"name": "bad", "colors": [1]})[0] == 400
        delete_url = "/api/v2/personal-decks/delete"
        delete_request = {"deck_id": deck["id"], "revision": 1}
        assert request(delete_url, delete_request, "https://evil.test")[0] == 403
        assert request(delete_url, delete_request)[0] == 409
        delete_request["revision"] = 3
        assert request(delete_url, delete_request) == (
            200, {"deleted": True, "deck_id": deck["id"], "revision": 4})
        assert request("/api/v2/personal-decks")[1]["items"] == []
        assert request("/api/v2/personal-deck?id=" + deck["id"])[0] == 404
        assert request("/api/v2/personal-decks/save", {"deck": deck})[0] == 404
        assert request("/api/v2/personal-decks/add", {"deck_id": deck["id"], "provider_card_id": "id1"})[0] == 404
        assert request("/api/v2/personal-decks/export", {"deck_id": deck["id"]})[0] == 404
        assert request(delete_url, delete_request)[0] == 404

        status, imported = request("/api/v2/personal-decks/import-text", {
            "filename": "Liste importée.txt", "text": "Deck\n2 Fire // Ice (TST) id1",
        })
        assert status == 200 and imported["imported"] == 2 and imported["unresolved"] == []
        imported_deck = imported["deck"]
        assert imported_deck["name"] == "Liste importée" and imported_deck["cover_card"]["printing_id"] == "p1"
        deck_id = imported_deck["id"]
        status, result = request("/api/v2/personal-decks/group-create", {"name": "Favoris"})
        assert status == 200
        group_id = result["group"]["id"]
        assert request("/api/v2/personal-decks/group-move", {"deck_id": deck_id, "group_id": group_id}) == (
            200, {"id": deck_id, "group_id": group_id})
        listing = request("/api/v2/personal-decks?summary=1")[1]
        assert listing["groups"] == [{"id": group_id, "name": "Favoris"}]
        assert listing["items"][0]["group_id"] == group_id
        assert request("/api/v2/personal-decks/group-move", {
            "deck_id": deck_id, "group_id": None, "before_deck_id": None,
        }) == (200, {"id": deck_id, "group_id": None})
        status, result = request("/api/v2/personal-decks/export-text", {"deck_id": deck_id})
        assert status == 200 and result["total"] == 2
        target = Path(result["path"])
        assert target.parent == desktop
        assert "2 Fire // Ice (TST) id1" in target.read_text(encoding="utf-8")
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
