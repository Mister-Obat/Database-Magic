import json
import threading
import time
import urllib.request

from bibliotheque_mtg import local_app


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
