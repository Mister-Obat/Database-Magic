import json
import sqlite3
import threading
import time
import urllib.request

from bibliotheque_mtg import local_app
from bibliotheque_mtg.v2.catalogue import migrate
from .test_catalogue_v2 import source_database


def test_batch_filter_accepts_current_search_fields_and_other_modes():
    search = {"q": "", "oracle_id": "oracle-1", "printing_id": "", "unique": "1",
              "image_filter": "without", "result_sort": "date"}
    assert local_app._batch_filter_payload("search", search) == {
        "oracle_id": "oracle-1", "unique": "1", "image_filter": "without", "result_sort": "date"}
    assert local_app._batch_filter_payload("edition", {"set": "s1"}) == {"set": "s1"}
    assert local_app._batch_filter_payload("decks", {"q": "test"}) == {"q": "test"}


def test_card_usage_exposes_edition_icon_and_deck_colors(monkeypatch, tmp_path):
    source = tmp_path / "source.sqlite"
    catalogue = tmp_path / "catalogue.sqlite"
    source_database(source)
    migrate(source, catalogue, archive_sha256="d" * 64, archive_as_of="2026-10-20")
    with sqlite3.connect(catalogue) as conn:
        printing = conn.execute("SELECT printing_id FROM source_records WHERE provider_card_id='en-1'").fetchone()[0]
    decks_db = tmp_path / "decks.sqlite"
    with sqlite3.connect(decks_db) as conn:
        conn.executescript("CREATE TABLE decks(id TEXT,name TEXT,name_fr TEXT,set_code TEXT,set_name TEXT,set_name_fr TEXT,colors TEXT,loaded INTEGER); CREATE TABLE deck_cards(deck_id TEXT,set_code TEXT,collector_number TEXT,raw_json TEXT);")
        conn.execute("INSERT INTO decks VALUES(?,?,?,?,?,?,?,?)", ("deck-1", "Test deck", None, "s1", "Set One", None, "WU", 1))
        conn.execute("INSERT INTO deck_cards VALUES(?,?,?,?)", ("deck-1", "s1", "1", json.dumps({"identifiers": {"scryfallId": "en-1"}})))
    personal_db = tmp_path / "personal.sqlite"
    with sqlite3.connect(personal_db) as conn:
        conn.execute("CREATE TABLE personal_decks(document TEXT)")
        conn.execute("INSERT INTO personal_decks VALUES(?)", (json.dumps({"id": "personal-1", "name": "Personal", "colors": ["B"], "sections": [{"cards": [{"printing_id": printing}]}]}),))
    monkeypatch.setattr(local_app, "DECK_DB", decks_db)
    monkeypatch.setattr(local_app, "PERSONAL_DECK_DB", personal_db)
    monkeypatch.setattr(local_app, "_edition_icon_urls", lambda codes, db: {"s1": "https://svgs.scryfall.io/sets/s1.svg"})
    usage = local_app._card_usage(catalogue, "en-1")
    assert usage["exact"]["editions"][0]["printing_id"] == printing
    assert usage["exact"]["editions"][0]["edition_icon_url"] == "https://svgs.scryfall.io/sets/s1.svg"
    assert usage["exact"]["decks"][0]["colors"] == ["W", "U"]
    assert usage["exact"]["personal_decks"][0]["colors"] == ["B"]


def test_manual_image_import_is_allowed_during_batch_download(monkeypatch):
    monkeypatch.setattr(local_app, "_MAINTENANCE_TASK", None)
    monkeypatch.setattr(local_app, "_BATCH_TASK", {"status": "running"})
    imported = []
    monkeypatch.setattr(local_app, "import_local_image", lambda provider, face, data, **kwargs:
                        imported.append((provider, face, data)) or {"status": "ready"})
    server = local_app.LocalServer(("127.0.0.1", 0), local_app.LibraryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        request = urllib.request.Request(
            base + "/api/v2/image/import?provider_card_id=card&face_index=0",
            data=b"image", method="POST", headers={"Origin": base})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
        assert imported == [("card", 0, b"image")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_batch_notification_keeps_edition_filter(monkeypatch, tmp_path):
    monkeypatch.setattr(local_app, "_BATCH_TASK", None)
    monkeypatch.setattr(local_app, "_MAINTENANCE_TASK", None)
    monkeypatch.setattr(local_app, "_maintenance_busy_reason", lambda: None)
    monkeypatch.setattr(local_app, "_run_batch_task", lambda *args: None)
    monkeypatch.setattr(local_app, "resolve_current", lambda _root: {"database": tmp_path / "catalogue.sqlite"})
    server = local_app.LocalServer(("127.0.0.1", 0), local_app.LibraryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        request = urllib.request.Request(base + "/api/v2/batch-download", method="POST",
            data=json.dumps({"mode": "edition", "filters": {"set": "m21"}}).encode(),
            headers={"Content-Type": "application/json", "Origin": base})
        with urllib.request.urlopen(request, timeout=5) as response:
            task_id = json.load(response)["task_id"]
        with urllib.request.urlopen(base + "/api/v2/notifications", timeout=5) as response:
            items = json.load(response)["items"]
        batch = next(item for item in items if item.get("task_id") == task_id)
        assert (batch["mode"], batch["filters"]) == ("edition", {"set": "m21"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_deck_routes_and_import_use_exact_provider_ids(monkeypatch, tmp_path):
    db = tmp_path / "catalogue.sqlite"
    db.touch()
    monkeypatch.setattr(local_app, "resolve_current", lambda _: {"database": db, "version_id": "test"})
    monkeypatch.setattr(local_app, "ensure_deck_index", lambda *args, **kwargs: None)
    monkeypatch.setattr(local_app, "_deck_edition_names", lambda *args, **kwargs: {})
    monkeypatch.setattr(local_app, "_deck_list_media", lambda *args, **kwargs: {"cover_image_path": None})
    monkeypatch.setattr(local_app, "list_decks", lambda *args, **kwargs: {
        "total": 1, "limit": 50, "offset": 0,
        "items": [{"id": "timey", "name": "Timey-Wimey", "type": "Commander Deck", "set_code": "WHO"}],
    })
    monkeypatch.setattr(local_app, "deck_facets", lambda *args, **kwargs: {"types": ["Commander Deck"], "color_indexed": 1, "total": 1})
    monkeypatch.setattr(local_app, "get_deck", lambda *args, **kwargs: {
        "found": True, "deck": {"id": "timey", "name": "Timey-Wimey", "sections": {
            "commander": [{"name": "The Tenth Doctor", "count": 1, "provider_card_id": "exact-id"}],
            "mainboard": [{"name": "Missing", "count": 1, "provider_card_id": None}],
        }},
    })
    def fake_media(payload, _catalog_db):
        for cards in payload["deck"]["sections"].values():
            for card in cards:
                card["image_ready"] = False
        payload["deck"]["media_summary"] = {"total": 2, "ready": 0, "missing": 2}
        return payload
    monkeypatch.setattr(local_app, "_deck_with_media", fake_media)
    calls = []
    def fake_download(provider_id, **kwargs):
        calls.append(provider_id)
        return {"results": [{"face_index": 0, "status": "ready"}]}
    monkeypatch.setattr(local_app, "download_printing", fake_download)
    refreshed = []
    def fake_refresh(provider_id, **kwargs):
        refreshed.append((provider_id, kwargs["face_indexes"]))
        return {"results": [{"face_index": 0, "status": "lower_quality_kept_existing"}]}
    monkeypatch.setattr(local_app, "refresh_printing_deep", fake_refresh)
    monkeypatch.setattr(local_app, "_COLOR_INDEX_TASK", None)
    def fake_color_index(*args, **kwargs):
        kwargs["progress_callback"]({"phase": "join", "completed": 1, "total": 1, "indexed": 1})
        return {"completed": 1, "total": 1, "indexed": 1, "unmatched": 0, "failed": 0, "bytes_received": 100}
    monkeypatch.setattr(local_app, "index_deck_colors", fake_color_index)
    server = local_app.LocalServer(("127.0.0.1", 0), local_app.LibraryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/api/v2/decks", timeout=5) as response:
            assert json.load(response)["items"][0]["id"] == "timey"
        with urllib.request.urlopen(base + "/api/v2/decks/filters", timeout=5) as response:
            assert json.load(response)["types"] == ["Commander Deck"]
        request = urllib.request.Request(
            base + "/api/v2/decks/colors/index", method="POST", data=b"{}",
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert json.load(response)["status"] in {"queued", "running", "complete"}
        for _ in range(50):
            with urllib.request.urlopen(base + "/api/v2/decks/colors/status", timeout=5) as response:
                color_task = json.load(response)
            if color_task["status"] == "complete":
                break
            time.sleep(0.02)
        assert color_task["status"] == "complete"
        assert (color_task["indexed"], color_task["unmatched"]) == (1, 0)
        with urllib.request.urlopen(base + "/api/v2/deck?id=timey", timeout=5) as response:
            assert json.load(response)["deck"]["name"] == "Timey-Wimey"
        request = urllib.request.Request(
            base + "/api/v2/deck/download", method="POST",
            data=json.dumps({"deck_id": "timey"}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            task_id = json.load(response)["task_id"]
        for _ in range(50):
            with urllib.request.urlopen(base + "/api/v2/deck/task?task_id=" + task_id, timeout=5) as response:
                task = json.load(response)
            if task["status"] == "complete":
                break
            time.sleep(0.02)
        assert task["status"] == "complete"
        assert (task["total"], task["completed"], task["ready"], task["unavailable"], task["failed"]) == (2, 2, 1, 1, 0)
        assert calls == ["exact-id"]
        request = urllib.request.Request(
            base + "/api/v2/image/refresh", method="POST",
            data=json.dumps({"provider_card_id": "exact-id", "face_index": 0}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert json.load(response)["results"][0]["status"] == "lower_quality_kept_existing"
        assert refreshed == [("exact-id", [0])]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
