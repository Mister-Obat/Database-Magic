import pytest

from bibliotheque_mtg import local_app


def test_lens_ticket_is_single_use_and_expires(monkeypatch):
    monkeypatch.setattr(local_app, "_archive_lens_jpeg", lambda path: b"jpeg-image")
    with local_app._LENS_TICKETS_LOCK:
        local_app._LENS_TICKETS.clear()
    ticket = local_app._store_lens_ticket("archive.png")
    assert local_app._take_lens_ticket(ticket) == b"jpeg-image"
    with pytest.raises(ValueError, match="Ticket"):
        local_app._take_lens_ticket(ticket)
    ticket = local_app._store_lens_ticket("archive.png")
    with local_app._LENS_TICKETS_LOCK:
        created, image = local_app._LENS_TICKETS[ticket]
        local_app._LENS_TICKETS[ticket] = (created - 91, image)
    with pytest.raises(ValueError, match="expiré"):
        local_app._take_lens_ticket(ticket)


def test_lens_bridge_submits_file_to_google():
    page = local_app._archive_lens_bridge(b"jpeg-image").decode("utf-8")
    assert "action=https://www.google.com/searchbyimage/upload" in page
    assert "name=encoded_image" in page
    assert "new File([bytes],'carte-magic.jpg'" in page
