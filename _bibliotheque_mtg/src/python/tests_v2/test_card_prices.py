import json
import sqlite3
from contextlib import closing
from threading import Event

import pytest

from bibliotheque_mtg import card_prices
from bibliotheque_mtg import local_app
from bibliotheque_mtg.maintenance import MaintenanceCancelled


class GuideResponse:
    def __init__(self, rows=(), *, status=200, etag='"test-guide"'):
        self.status_code = status
        self.headers = {"ETag": etag}
        self.body = json.dumps({"createdAt": "2026-11-23T02:45:33+0100", "priceGuides": rows}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, *, chunk_size):
        yield self.body


@pytest.fixture
def catalogue(tmp_path):
    db = tmp_path / "catalogue.sqlite"
    records = [
        ("Nature's Lore", {"cardmarket_id": 1}),
        ("Nature's Lore", {"cardmarket_id": 2}),
        ("Nature's Lore", {"cardmarket_id": 3, "printed_name": "Savoir de la nature"}),
        ("Wear // Tear", {"cardmarket_id": 4}),
        ("Black Lotus", {"cardmarket_id": 5}),
        ("Mystery", {}),
    ]
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE source_records (name TEXT, raw_json TEXT)")
        conn.executemany("INSERT INTO source_records VALUES (?, ?)",
                         ((name, json.dumps(raw)) for name, raw in records))
    return {"database": db, "version_id": "v2-test"}


def test_imports_deduplicated_cardmarket_prices_and_revalidates(tmp_path, catalogue, monkeypatch):
    assert card_prices.read_card_prices(tmp_path) == {}
    calls = []
    rows = [
        {"idProduct": 1, "trend": 0.65},
        {"idProduct": 2, "trend": 0.47},
        {"idProduct": 3, "trend": 0, "trend-foil": 0.1},
        {"idProduct": 4, "trend": None, "trend-foil": 1.23},
        {"idProduct": 5, "trend": None, "trend-foil": None},
        {"idProduct": 999, "trend": 2000},
    ]

    def fetch(url, *, headers, **_):
        assert url == card_prices._PRICE_GUIDE
        calls.append(headers)
        return GuideResponse(rows) if not headers else GuideResponse(status=304)

    monkeypatch.setattr(card_prices.requests, "get", fetch)
    report = card_prices.refresh_card_prices(
        tmp_path, catalogue, progress=lambda *_: None, cancel_event=Event())
    assert report == {"status": "updated", "cards": 2, "as_of": "2026-11-23T02:45:33+0100"}
    assert card_prices.read_card_prices(tmp_path) == {"Nature's Lore": 47, "Wear // Tear": 123}

    unchanged = card_prices.refresh_card_prices(
        tmp_path, catalogue, progress=lambda *_: None, cancel_event=Event())
    assert unchanged == report | {"status": "no_change"}
    card_prices.refresh_card_prices(
        tmp_path, catalogue | {"version_id": "v2-other"}, progress=lambda *_: None, cancel_event=Event())
    assert calls == [{}, {"If-None-Match": '"test-guide"'}, {}]


def test_keeps_existing_prices_after_invalid_guide_or_cancellation(tmp_path, catalogue, monkeypatch):
    rows = [{"idProduct": 1, "trend": 0.65}]
    monkeypatch.setattr(card_prices.requests, "get", lambda *_, **__: GuideResponse(rows))

    def refresh(progress=lambda *_: None):
        return card_prices.refresh_card_prices(tmp_path, catalogue, progress=progress, cancel_event=cancel)

    cancel = Event()
    refresh()
    rows.clear()
    with pytest.raises(ValueError, match="Aucun prix"):
        refresh()
    assert card_prices.read_card_prices(tmp_path) == {"Nature's Lore": 65}

    rows.append({"idProduct": 1, "trend": 0.50})

    def cancel_at_save(fraction, _detail):
        if fraction == 0.95:
            cancel.set()

    with pytest.raises(MaintenanceCancelled):
        refresh(cancel_at_save)
    assert card_prices.read_card_prices(tmp_path) == {"Nature's Lore": 65}


def test_maintenance_refreshes_prices_after_no_catalogue_change(tmp_path, catalogue, monkeypatch):
    monkeypatch.setattr(local_app, "OPS_ROOT", tmp_path)
    monkeypatch.setattr(local_app, "_MAINTENANCE_CANCEL", Event())
    monkeypatch.setattr(local_app, "_MAINTENANCE_TASK", {
        "task_id": "test-run", "steps": local_app._maintenance_steps_template(),
    })
    monkeypatch.setattr(local_app, "resolve_current", lambda _: catalogue)
    monkeypatch.setattr(local_app, "refresh_v2", lambda *_, **__: {"status": "no_change"})
    monkeypatch.setattr(local_app, "_synchronize_library_images", lambda: 0)
    monkeypatch.setattr(local_app, "refresh_deck_index", lambda *_, **__: {"decks": 0})
    monkeypatch.setattr(card_prices.requests, "get",
                        lambda *_, **__: GuideResponse([{"idProduct": 1, "trend": 0.65}]))

    local_app._run_maintenance_task("test-run")

    task = local_app._maintenance_snapshot()
    assert task["status"] == "complete"
    assert task["result"]["prices"]["status"] == "updated"
    assert card_prices.read_card_prices(tmp_path) == {"Nature's Lore": 65}
