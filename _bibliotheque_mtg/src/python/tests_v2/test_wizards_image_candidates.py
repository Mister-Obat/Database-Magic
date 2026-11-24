import json
import sqlite3
import time
import threading
import urllib.error
import urllib.request
from io import BytesIO

from bibliotheque_mtg import image_download, local_app
from PIL import Image


def test_batch_allows_image_preview_and_choice_but_defers_other_faces(monkeypatch, tmp_path):
    db = tmp_path / "catalogue.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE source_records(provider_card_id, name, lang)")
        conn.execute("INSERT INTO source_records VALUES ('en-1', 'Card', 'en')")
    monkeypatch.setattr(local_app.LibraryHandler, "db", property(lambda self: db))
    monkeypatch.setattr(local_app, "_BATCH_TASK", {"status": "running"})
    monkeypatch.setattr(local_app, "_MAINTENANCE_TASK", None)
    monkeypatch.setattr(local_app, "_STARTUP_IMAGE_RESTORE", threading.Event())
    monkeypatch.setattr(local_app, "_IMAGE_TASKS", {})
    monkeypatch.setattr(local_app, "_start_image_task", lambda *args, **kwargs: {"task_id": "preview"})
    candidate = {"provider_card_id": "en-1", "face_index": 0}
    monkeypatch.setattr(local_app, "_image_candidate", lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(local_app, "publish_preview_candidate",
                        lambda candidate, **kwargs: {"face_index": candidate["face_index"]})
    server = local_app.LocalServer(("127.0.0.1", 0), local_app.LibraryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path, payload):
        request = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    try:
        image = {"provider_card_id": "en-1", "face_index": 0}
        choice = {"candidate_id": "test", "choice": "new", "index": 0}
        status, body = post("/api/v2/download", {**image, "face_index": 1})
        assert (status, body["error"]) == (409, "download_busy")
        assert post("/api/v2/image/preview", image) == (202, {"task_id": "preview"})
        assert post("/api/v2/image/candidate/choose", choice) == (
            200, {"status": "ready", "result": {"face_index": 0}})

        monkeypatch.setattr(local_app, "_BATCH_TASK", None)
        monkeypatch.setattr(local_app, "_MAINTENANCE_TASK", {"status": "running"})
        assert post("/api/v2/download", {**image, "face_index": 1}) == (
            409, {"error": "maintenance_in_progress"})
        assert post("/api/v2/image/candidate/choose", choice) == (
            409, {"error": "maintenance_in_progress"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_japanese_visual_accepts_same_name_french_text_reference(monkeypatch, tmp_path):
    visual = {"provider_card_id": "jp", "printing_id": "jp-print", "face_index": 0,
              "language": "ja", "source_data": b"visual"}
    french = {"provider_card_id": "fr", "printing_id": "fr-print", "face_index": 0,
              "language": "fr", "source_data": b"text", "text_reference_only": True}
    monkeypatch.setattr(local_app, "_image_candidate", lambda _id: [visual, french])
    monkeypatch.setattr(local_app, "_same_name_text_reference", lambda *_args: True)
    calls = []
    monkeypatch.setattr(local_app, "_start_image_task", lambda *args, **kwargs:
                        calls.append((args, kwargs)) or {"task_id": "test", "status": "queued"})
    monkeypatch.setattr(local_app.LibraryHandler, "db", property(lambda self: tmp_path / "catalogue.sqlite"))
    server = local_app.LocalServer(("127.0.0.1", 0), local_app.LibraryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = {"candidate_id": "test", "visual_index": 0, "text_index": 1, "text_mode": "image"}
        request = urllib.request.Request(base + "/api/v2/image/ai/generate",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "Origin": base})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _Response:
    status_code = 200
    headers = {"Content-Type": "text/html"}

    def __init__(self, body):
        self.body = body

    def iter_content(self, _size):
        yield self.body

    def close(self):
        pass


class _Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append((url, _kwargs))
        if url not in self.responses:
            response = _Response(b"")
            response.status_code = 404
            return response
        return _Response(self.responses[url])


def test_news_gallery_keeps_distinct_french_card_images(tmp_path, monkeypatch):
    url = "https://magic.wizards.com/fr/news/card-image-gallery/warhammer-40000-commander-decks"
    images = ["https://media.wizards.com/2022/40k/fr_first.png",
              "https://media.wizards.com/2022/40k/fr_second.png"]
    index = tmp_path / "gallery_catalogue.json"
    index.write_text(json.dumps({"articles": [{"url": url}]}), encoding="utf-8")
    monkeypatch.setattr(image_download, "_WIZARDS_INDEX_PATH", index)
    image_download._GALLERY_CACHE.clear()
    html = "<html>" + "".join(f'<img alt="Le Maître des Essaims" src="{image}">' for image in images) + "</html>"
    result = image_download._official_gallery_images(
        "Warhammer 40,000 Commander", "Le Maître des Essaims", _Session({url: html.encode()}))
    assert [candidate["url"] for candidate in result] == images


def test_product_gallery_matches_french_name_and_exact_collector(tmp_path, monkeypatch):
    page = "https://magic.wizards.com/fr/products/modern-horizons-3/card-image-gallery"
    base = "https://cdn.contentful.com/spaces/space/environments/master/entries/"
    index = tmp_path / "gallery_catalogue.json"
    index.write_text(json.dumps({"articles": [{"url": page}]}), encoding="utf-8")
    monkeypatch.setattr(image_download, "_WIZARDS_INDEX_PATH", index)
    monkeypatch.setattr(image_download, "_CONTENTFUL_CLIENT_CACHE", (time.monotonic(), "space", "token"))
    image_download._GALLERY_CACHE.clear()
    image_download._PRODUCT_GALLERY_CACHE.clear()
    image_download._CONTENTFUL_ENTRY_CACHE.clear()
    responses = {
        page: b'<html><div data-cig-v="1" data-ctf-id="gallery"></div></html>',
        base + "gallery": json.dumps({"sys": {"contentType": {"sys": {"id": "cardImageGallery"}}},
            "fields": {"cardList": {"body": "Psychic Frog [cardone]\nPsychic Frog [cardtwo]"}}}).encode(),
        base + "cardone": json.dumps({"sys": {"contentType": {"sys": {"id": "magicCard"}}},
            "fields": {"name": "Grenouille psychique", "collectorNumber": "199",
                       "face": "https://media.wizards.com/2024/mh3/fr_cardone.png"}}).encode(),
        base + "cardtwo": json.dumps({"sys": {"contentType": {"sys": {"id": "magicCard"}}},
            "fields": {"name": "Grenouille psychique", "collectorNumber": "344",
                       "face": "https://media.wizards.com/2024/mh3/fr_cardtwo.png"}}).encode(),
    }
    result = image_download._official_gallery_images(
        "Modern Horizons 3", "Grenouille psychique", _Session(responses),
        english_name="Psychic Frog", collector_number="199", exact_only=True)
    assert [candidate["url"] for candidate in result] == [
        "https://media.wizards.com/2024/mh3/fr_cardone.png"]


def test_reviewed_wizards_image_requires_exact_provider_identity(tmp_path, monkeypatch):
    db = tmp_path / "catalogue.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE official_french_text (provider_card_id TEXT, oracle_id TEXT, "
                     "set_code TEXT, collector_number TEXT, language TEXT, fields_json TEXT, "
                     "source_url TEXT, source_image_url TEXT)")
        conn.execute("INSERT INTO official_french_text VALUES (?,?,?,?,?,?,?,?)", (
            "card-id", "oracle-id", "40k", "176", "fr",
            json.dumps({"printed_name": "Le Maître des Essaims"}),
            "https://magic.wizards.com/fr/news/card-image-gallery/warhammer-40000-commander-decks",
            "https://media.wizards.com/2022/40k/fr_card.png"))
    monkeypatch.setattr(image_download, "_SOURCE_CATALOGUE_PATH", db)
    printing = {"oracle_id": "oracle-id", "code": "40K", "collector_number": "176"}
    assert image_download._reviewed_wizards_reference("card-id", printing)["url"].endswith("fr_card.png")
    assert image_download._reviewed_wizards_reference("another-id", printing) is None
    assert image_download._reviewed_wizards_reference("card-id", {**printing, "collector_number": "4"}) is None


def test_contentful_direct_lookup_uses_exact_set_name_and_number(monkeypatch):
    monkeypatch.setattr(image_download, "_CONTENTFUL_CLIENT_CACHE", (time.monotonic(), "space", "token"))
    image_download._CONTENTFUL_DIRECT_CACHE.clear()
    url = "https://cdn.contentful.com/spaces/space/environments/master/entries"
    session = _Session({url: json.dumps({"items": [{
        "sys": {"id": "cardone", "contentType": {"sys": {"id": "magicCard"}}},
        "fields": {"identifier": "psychic-frog", "name": "Grenouille psychique",
                   "collectorNumber": "199", "face": "https://media.wizards.com/2024/mh3/fr_card.png"}}]}).encode()})
    result = image_download._contentful_direct_images("MH3", "Psychic Frog", "", "199", session)
    assert [item["url"] for item in result] == ["https://media.wizards.com/2024/mh3/fr_card.png"]
    params = session.calls[0][1]["params"]
    assert params["fields.foundInSet.fields.abbreviation"] == "mh3"
    assert params["fields.identifier"] == "psychic-frog"
    assert params["fields.collectorNumber"] == "199"


def test_distinct_wizards_cards_overlap_while_shared_gallery_loads_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    page = "https://magic.wizards.com/fr/products/test-set/card-image-gallery"
    url = "https://cdn.contentful.com/spaces/space/environments/master/entries"
    index = tmp_path / "gallery_catalogue.json"
    index.write_text(json.dumps({"articles": [{"url": page}]}), encoding="utf-8")
    monkeypatch.setattr(image_download, "_WIZARDS_INDEX_PATH", index)
    monkeypatch.setattr(image_download, "_GALLERY_CACHE", {})
    monkeypatch.setattr(image_download, "_CONTENTFUL_DIRECT_CACHE", {})
    monkeypatch.setattr(image_download, "_CONTENTFUL_CLIENT_CACHE", (time.monotonic(), "space", "token"))
    queries = threading.Barrier(2)
    calls = []

    class Session:
        def get(self, requested, *, params=None, **_kwargs):
            calls.append(requested)
            if requested == page:
                return _Response(b"<html></html>")
            assert requested == url
            number = params["fields.collectorNumber"]
            queries.wait(5)
            return _Response(json.dumps({"items": [{
                "sys": {"id": number, "contentType": {"sys": {"id": "magicCard"}}},
                "fields": {"identifier": f"card-{number}", "name": f"Carte {number}",
                           "collectorNumber": number, "face": f"https://media.wizards.com/fr_{number}.png"},
            }]}).encode())

    def lookup(number):
        return image_download._gallery_for_printing(
            {"name": "Test Set", "code": "tst", "collector_number": number},
            f"Carte {number}", f"Card {number}", Session(), exact_only=True, cancel_event=None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lookup, ("1", "2")))
    assert calls.count(page) == 1 and calls.count(url) == 2
    assert [result[0]["url"] for result in results] == [
        "https://media.wizards.com/fr_1.png", "https://media.wizards.com/fr_2.png"]


def test_normal_missing_image_download_can_use_exact_wizards_reference(tmp_path, monkeypatch):
    db = tmp_path / "catalogue.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE editions(set_id TEXT,code TEXT,name TEXT,eligibility TEXT)")
        conn.execute("CREATE TABLE printings(printing_id TEXT,set_id TEXT,collector_number TEXT,layout TEXT,oracle_id TEXT)")
        conn.execute("CREATE TABLE source_records(provider_card_id TEXT,printing_id TEXT,lang TEXT,raw_json TEXT,printed_name TEXT)")
        conn.execute("CREATE TABLE image_slots(printing_id TEXT,face_index INTEGER,status TEXT,selected_language TEXT,asset_id TEXT)")
        conn.execute("CREATE TABLE image_assets(asset_id TEXT,relative_path TEXT)")
        conn.execute("INSERT INTO editions VALUES('set','40K','Warhammer 40,000 Commander','eligible')")
        conn.execute("INSERT INTO printings VALUES('print','set','176','normal','oracle')")
        conn.execute("INSERT INTO source_records VALUES('card','print','en',?,NULL)", (
            json.dumps({"name": "The Swarmlord", "image_uris": {"normal": "https://cards.scryfall.io/card.jpg"}}),))
        conn.execute("INSERT INTO image_slots VALUES('print',0,'missing',NULL,NULL)")
    monkeypatch.setattr(image_download, "resolve_current", lambda _root: {
        "database": db, "version_id": "test"})
    monkeypatch.setattr(image_download, "ensure_catalogue_wal", lambda _db: None)
    official = "https://media.wizards.com/2022/40k/fr_card.png"
    monkeypatch.setattr(image_download, "_reviewed_wizards_reference", lambda *_: {
        "name": "Le Maître des Essaims", "url": official, "language": "fr",
        "source": "wizards_official_reviewed", "allow_host": "media.wizards.com",
        "evidence": "https://magic.wizards.com/fr/news/card-image-gallery/warhammer-40000-commander-decks"})
    image = BytesIO()
    Image.new("RGB", (265, 370), "white").save(image, format="PNG")
    fetched = []
    monkeypatch.setattr(image_download, "_fetch", lambda url, *_args, **_kwargs: (
        fetched.append(url) or image.getvalue(), None, None))
    monkeypatch.setattr(image_download, "_convertible", lambda *_args, **_kwargs: (265, 370))
    published = []
    monkeypatch.setattr(image_download, "_publish_face", lambda *_args, **kwargs: (
        published.append(kwargs) or {"status": "ready"}))

    result = image_download.download_printing(
        "card", catalogue_root=tmp_path, ledger_path=tmp_path / "ledger.json")
    assert result["results"][0]["status"] == "ready"
    assert fetched == [official]
    assert published[0]["source_provider"] == "wizards_official_reviewed"
