from __future__ import annotations

from pathlib import Path

from bibliotheque_mtg import local_app


def test_recalculate_deck_search_visits_all_matching_pages(monkeypatch):
    pages = []
    monkeypatch.setattr(local_app, "_deck_media_context", lambda _db: ("test-search",))

    def list_page(_db, **kwargs):
        pages.append(kwargs["offset"])
        offset = kwargs["offset"]
        items = [{"id": f"deck-{index}"} for index in range(offset, min(offset + 100, 205))]
        return {"total": 205, "items": items}

    seen = []
    monkeypatch.setattr(local_app, "list_decks", list_page)
    monkeypatch.setattr(local_app, "_deck_list_media",
                        lambda deck_id, _catalog: seen.append(deck_id) or
                        {"complete": None if deck_id == "deck-204" else True})

    result = local_app._recalculate_deck_search({"q": "Commander", "set_code": "WHO"}, Path("catalog.sqlite"))

    assert pages == [0, 100, 200]
    assert len(seen) == 205
    assert result == {"total": 205, "recalculated": 204, "unloaded": 1}


def test_deck_list_media_keeps_commander_cover_when_tokens_are_unloaded(monkeypatch):
    payload = {"found": True, "deck": {"loaded": True, "tokens_loaded": False,
                                        "cover_image_path": "assets/commander.webp"}}
    monkeypatch.setattr(local_app, "_deck_media_context", lambda _db: ("test-cover",))
    monkeypatch.setattr(local_app, "get_deck", lambda *args, **kwargs: payload)
    monkeypatch.setattr(local_app, "_deck_with_media", lambda value, _catalog: value)

    result = local_app._deck_list_media("deck-id", Path("catalog.sqlite"))

    assert result == {"cover_image_path": "assets/commander.webp",
                      "media_summary": None, "complete": None}
