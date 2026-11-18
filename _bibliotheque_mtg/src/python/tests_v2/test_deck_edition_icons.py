import json
import sqlite3
import threading
import urllib.request
from contextlib import closing

from bibliotheque_mtg import local_app


def test_deck_listing_read_set_icons_from_local_metadata(tmp_path, monkeypatch):
    icon = "https://svgs.scryfall.io/sets/who.svg?test=1"
    source_db = tmp_path / "catalogue.sqlite"
    with closing(sqlite3.connect(source_db)) as conn:
        conn.execute("CREATE TABLE sets(code TEXT, raw_json TEXT)")
        conn.executemany("INSERT INTO sets VALUES(?,?)", [
            ("who", json.dumps({"icon_svg_uri": icon})),
            ("fake", json.dumps({"icon_svg_uri": "https://unknown.example/sets/fake.svg"})),
            ("invalid", json.dumps([])),
        ])
        conn.commit()

    monkeypatch.setattr(local_app, "OPS_ROOT", tmp_path)
    monkeypatch.setattr(local_app.LibraryHandler, "db", property(lambda self: tmp_path / "active.sqlite"))
    monkeypatch.setattr(local_app, "ensure_deck_index", lambda path: None)
    monkeypatch.setattr(local_app, "_deck_edition_names", lambda path: {"WHO": "Doctor Who"})
    monkeypatch.setattr(local_app, "_deck_list_media", lambda deck_id, path: {"complete": None})
    monkeypatch.setattr(local_app, "list_decks", lambda path, **kwargs: {"items": [
        {"id": "1", "set_code": "WHO"},
        {"id": "2", "set_code": "fake"},
        {"id": "3", "set_code": "invalid"},
        {"id": "4", "set_code": "unknown"},
        {"id": "5", "set_code": None},
    ], "total": 5})

    with local_app.LocalServer(("127.0.0.1", 0), local_app.LibraryHandler) as server:
        base = f"http://127.0.0.1:{server.server_port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(base + "/api/v2/decks", timeout=5) as response:
                result = json.load(response)
            assert result["total"] == 5
            assert result["items"][0]["set_name"] == "Doctor Who"
            assert [item["edition_icon_url"] for item in result["items"]] == [icon, None, None, None, None]
            source_db.unlink()
            with urllib.request.urlopen(base + "/api/v2/decks", timeout=5) as response:
                assert all(item["edition_icon_url"] is None for item in json.load(response)["items"])
        finally:
            server.shutdown()
            thread.join(timeout=2)
