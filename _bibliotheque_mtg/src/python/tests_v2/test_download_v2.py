import hashlib
import io
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from PIL import Image, ImageDraw

import bibliotheque_mtg.image_download as image_download
from bibliotheque_mtg.image_download import (_allowed_url, _encode_webp, _fetch, _fetch_live_fr,
                                             _manual_candidates, _ordered_uris, download_printing,
                                             preview_printing_candidate, publish_preview_candidate,
                                             refresh_printing_deep)


def patterned_image(size):
    image = Image.new("RGB", size, "#b22")
    draw = ImageDraw.Draw(image)
    draw.rectangle((size[0] // 3, 0, size[0] * 2 // 3, size[1]), fill="#294")
    draw.line((0, size[1] // 2, size[0], size[1] // 2), fill="#eef", width=max(2, size[0] // 100))
    return image


class ImageFixtureHandler(BaseHTTPRequestHandler):
    counts = {}
    image_bytes = b""

    def do_GET(self):
        self.counts[self.path] = self.counts.get(self.path, 0) + 1
        if self.path == "/timeout":
            time.sleep(.4)
            self.send_response(200); self.end_headers(); return
        if self.path == "/403":
            self.send_response(403); self.end_headers(); return
        if self.path == "/429":
            self.send_response(429); self.send_header("Retry-After", "120"); self.end_headers(); return
        if self.path == "/redirect":
            self.send_response(302); self.send_header("Location", "/image"); self.end_headers(); return
        if self.path == "/html":
            data, typ = b"<script>alert(1)</script>", "text/html"
        elif self.path == "/small":
            small = Image.new("RGB", (200, 300), "#3a7")
            buf = io.BytesIO(); small.save(buf, "PNG"); data, typ = buf.getvalue(), "image/png"
        elif self.path == "/medium":
            medium = patterned_image((420, 650))
            buf = io.BytesIO(); medium.save(buf, "PNG"); data, typ = buf.getvalue(), "image/png"
        elif self.path == "/high":
            high = patterned_image((900, 1200))
            buf = io.BytesIO(); high.save(buf, "PNG"); data, typ = buf.getvalue(), "image/png"
        elif self.path == "/corrupt":
            data, typ = b"not an image", "image/png"
        else:
            data, typ = self.image_bytes, "image/png"
        self.send_response(200); self.send_header("Content-Type", typ); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def log_message(self, *_):
        pass


@pytest.fixture(scope="module")
def image_server():
    ImageFixtureHandler.counts = {}
    image = patterned_image((600, 800))
    buf = io.BytesIO(); image.save(buf, "PNG"); ImageFixtureHandler.image_bytes = buf.getvalue()
    server = ThreadingHTTPServer(("127.0.0.1", 0), ImageFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.fixture(autouse=True)
def avoid_live_scryfall_calls(monkeypatch):
    monkeypatch.setattr(image_download, "_fetch_live_fr", lambda *args, **kwargs: (None, "not_found", None))


def make_catalogue(tmp_path, base_url, *, fr_path="/image", en_path="/image", face_count=1):
    ops = tmp_path / "ops"; version = ops / "versions" / "demo"; editions = version / "editions" / "set-demo"
    editions.mkdir(parents=True); (version / "assets").mkdir()
    db = version / "catalogue.sqlite"
    schema = Path(__file__).parents[1] / "bibliotheque_mtg" / "v2" / "schema.sql"
    conn = sqlite3.connect(db); conn.executescript(schema.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO editions VALUES(?,?,?,?,?,?,?,?,?)", ("set-id", "tst", "Test Edition", "core", "2020-01-01", 0, 1, "eligible", None))
    conn.execute("INSERT INTO printings VALUES(?,?,?,?,?,?,?,?,?)", ("printing-demo", "set-id", "1a", None, "transform" if face_count == 2 else "normal", None, "confirmed_multilang", "fixture exact identity", "{}"))
    for lang, path, name in (("fr", fr_path, "Carte test"), ("en", en_path, "Test Card")):
        uris = {"png": base_url + path}
        raw = {"id": f"provider-{lang}", "name": name, "lang": lang, "image_uris": uris}
        if face_count == 2:
            raw.pop("image_uris")
            raw["card_faces"] = [{"name": name + " A", "image_uris": uris}, {"name": name + " B", "image_uris": uris}]
        conn.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"provider-{lang}", "printing-demo", lang, "fixture", name, name if lang == "fr" else None,
                      "Artifact", None, "{1}", 1, "[]", "[]", "", "", "rare", "[]", "fixture rule", "fixture text", json.dumps(raw)))
        conn.execute("INSERT INTO printing_languages VALUES(?,?,?)", ("printing-demo", lang, f"provider-{lang}"))
        conn.execute("INSERT INTO source_search VALUES(?,?,?,?,?,?,?,?,?)", (f"provider-{lang}", "tst", lang, name, name, "Artifact", "", "fixture rule", "fixture text"))
    for i in range(face_count):
        conn.execute("INSERT INTO card_faces VALUES(?,?,?)", ("printing-demo", i, 1))
        conn.execute("INSERT INTO image_slots VALUES(?,?,?,?,?,?,?)", ("printing-demo", i, "fr", None, "missing", None, "{}"))
    conn.commit(); conn.close()
    (ops / "current.json").write_text(json.dumps({"contract_version":"2.0.0", "version_id":"demo", "database":"versions/demo/catalogue.sqlite", "editions":"versions/demo/editions"}), encoding="utf-8")
    (version / "editions" / "index.json").write_text(json.dumps({"editions":[{"id":"set-id","images_json":"set-demo/images.json"}]}), encoding="utf-8")
    manifest = editions / "images.json"
    manifest.write_text(json.dumps({"edition_id":"set-id","schema_version":"2.0.0","slots":[{"printing_id":"printing-demo","face_index":i,"preferred_language":"fr","selected_language":None,"status":"missing","path":None} for i in range(face_count)]}), encoding="utf-8")
    return ops, version, db, manifest


def test_provider_responses_block_or_reject_without_following_redirects(image_server):
    host = {"127.0.0.1"}; session = requests.Session()
    assert _fetch(image_server + "/403", session, host)[1] == "acces_refuse_403"
    data, error, delay = _fetch(image_server + "/429", session, host)
    assert data is None and error == "limite_debit_429" and delay == 120
    assert _fetch(image_server + "/html", session, host)[1] == "contenu_non_image"
    assert _fetch(image_server + "/redirect", session, host)[1] == "redirection_non_autorisee"
    assert _fetch("https://example.org/image.png", session)[1] == "url_non_autorisee"
    assert _fetch(image_server + "/timeout", session, host, timeout=(.05, .05))[1].startswith("erreur_reseau:")


def test_phyrexian_only_printing_downloads_its_exact_image(tmp_path, image_server):
    ops, version, db, manifest = make_catalogue(tmp_path, image_server, en_path='/high')
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM source_records WHERE provider_card_id='provider-fr'")
        conn.execute("DELETE FROM source_search WHERE provider_card_id='provider-fr'")
        conn.execute("DELETE FROM printing_languages WHERE provider_card_id='provider-fr'")
        conn.execute("UPDATE source_records SET provider_card_id='provider-ph', lang='ph', "
                     "name='Vorinclex, Voice of Hunger', printed_name=NULL, "
                     "raw_json=json_set(raw_json,'$.id','provider-ph','$.lang','ph',"
                     "'$.name','Vorinclex, Voice of Hunger','$.printed_name','|lornScs') "
                     "WHERE provider_card_id='provider-en'")
        conn.execute("UPDATE source_search SET provider_card_id='provider-ph', lang='ph', "
                     "name='Vorinclex, Voice of Hunger', printed_name='' "
                     "WHERE provider_card_id='provider-en'")
        conn.execute("UPDATE printing_languages SET provider_card_id='provider-ph', lang='ph' "
                     "WHERE provider_card_id='provider-en'")
    preview = preview_printing_candidate('provider-ph', catalogue_root=ops,
                                         ledger_path=ops / 'ledger.json', face_index=0,
                                         test_hosts={'127.0.0.1'})
    assert preview['status'] == 'candidate'
    assert preview['candidates'][0]['language'] == 'ph'
    report = download_printing('provider-ph', catalogue_root=ops,
                               ledger_path=ops / 'ledger.json', test_hosts={'127.0.0.1'})
    assert report['results'][0]['status'] == 'ready'
    assert report['results'][0]['language'] == 'ph'
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status,selected_language FROM image_slots").fetchone() == ('ready', 'ph')
        assert conn.execute("SELECT name,printed_name FROM source_records WHERE lang='ph'").fetchone() == ('Vorinclex, Voice of Hunger', None)


def test_download_prefers_hd_french_then_publishes_one_face_only(tmp_path, image_server):
    ops, version, db, manifest = make_catalogue(tmp_path, image_server, fr_path="/corrupt", en_path="/image", face_count=2)
    before_index = hashlib.sha256((version / "editions" / "index.json").read_bytes()).hexdigest()
    before_other = json.loads(manifest.read_text(encoding="utf-8"))
    report = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                               test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert report["results"] == [{"face_index": 0, "status": "ready", "language": "en", "relative_path": report["results"][0]["relative_path"], "width": 531, "height": 738, "source_width": 600, "source_height": 800}]
    assert ImageFixtureHandler.counts["/corrupt"] == 1 and ImageFixtureHandler.counts["/image"] == 1
    after = json.loads(manifest.read_text(encoding="utf-8"))
    assert after["slots"][0]["status"] == "ready" and after["slots"][0]["selected_language"] == "en"
    assert after["slots"][1] == before_other["slots"][1]
    assert hashlib.sha256((version / "editions" / "index.json").read_bytes()).hexdigest() == before_index
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT status,selected_language FROM image_slots WHERE face_index=0").fetchone() == ("ready", "en")
    assert conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0] == 1
    conn.close()
    # Simulate a valid WebP from the previous V2 behavior (original dimensions).
    legacy = patterned_image((600, 800)); legacy_bytes = io.BytesIO(); legacy.save(legacy_bytes, "WEBP")
    legacy_payload = legacy_bytes.getvalue(); legacy_hash = hashlib.sha256(legacy_payload).hexdigest()
    legacy_relative = f"assets/webp/en/{legacy_hash}.webp"
    (version / legacy_relative).parent.mkdir(parents=True, exist_ok=True)
    (version / legacy_relative).write_bytes(legacy_payload)
    provenance = json.dumps({"source_url": "https://cards.scryfall.io/legacy.webp",
                             "source_dimensions": [600, 800], "selected_language": "en"})
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO image_assets VALUES(?,?,?,?,?,?,?,?)",
                 (legacy_hash, legacy_relative, "image/webp", len(legacy_payload), 600, 800, "en", provenance))
    conn.execute("UPDATE image_slots SET asset_id=? WHERE printing_id='printing-demo' AND face_index=0", (legacy_hash,))
    conn.commit(); conn.close()
    doc = json.loads(manifest.read_text(encoding="utf-8")); doc["slots"][0]["path"] = legacy_relative
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    second = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                               test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert second["results"][0]["status"] == "normalized_existing"
    assert (second["results"][0]["width"], second["results"][0]["height"]) == (531, 738)
    assert ImageFixtureHandler.counts["/image"] == 1
    asset = version / second["results"][0]["relative_path"]
    with Image.open(asset) as image: assert image.size == (531, 738)
    third = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                              test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert third["results"][0]["status"] == "already_ready"
    asset.write_bytes(b"corrupt")
    check = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                              test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert check["results"][0]["status"] == "corrupt_requires_repair"
    fixed = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                              test_hosts={"127.0.0.1"}, face_indexes=[0], repair=True)
    assert fixed["results"][0]["status"] == "ready"
    asset = version / fixed["results"][0]["relative_path"]
    with Image.open(asset) as image:
        assert image.format == "WEBP" and image.size == (531, 738)
        image.verify()


def test_download_does_not_propagate_image_between_editions(tmp_path, image_server):
    ops, version, db, first_manifest = make_catalogue(tmp_path, image_server, fr_path="/image", en_path="/image")
    second_dir = version / "editions" / "set-other"
    second_dir.mkdir()
    second_manifest = second_dir / "images.json"
    second_manifest.write_text(json.dumps({"edition_id": "set-other", "schema_version": "2.0.0",
                                           "slots": [{"printing_id": "printing-other", "face_index": 0,
                                                      "preferred_language": "fr", "selected_language": None,
                                                      "status": "missing", "path": None}]}), encoding="utf-8")
    index_path = version / "editions" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["editions"].append({"id": "set-other", "images_json": "set-other/images.json"})
    index_path.write_text(json.dumps(index), encoding="utf-8")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO editions VALUES(?,?,?,?,?,?,?,?,?)", ("set-other", "oth", "Other Edition", "core", "2021-01-01", 0, 1, "eligible", None))
    conn.execute("INSERT INTO printings VALUES(?,?,?,?,?,?,?,?,?)", ("printing-other", "set-other", "1a", None, "normal", None, "single_language", "exact fixture identity", "{}"))
    raw = {"id": "provider-other", "name": "Other Card", "lang": "en",
           "image_uris": {"png": image_server + "/image"}}
    conn.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("provider-other", "printing-other", "en", "fixture", "Other Card", None,
                  "Artifact", None, "{1}", 1, "[]", "[]", "", "", "rare", "[]", "fixture", "fixture", json.dumps(raw)))
    conn.execute("INSERT INTO printing_languages VALUES(?,?,?)", ("printing-other", "en", "provider-other"))
    conn.execute("INSERT INTO source_search VALUES(?,?,?,?,?,?,?,?,?)", ("provider-other", "oth", "en", "Other Card", "Other Card", "Artifact", "", "fixture", "fixture"))
    conn.execute("INSERT INTO card_faces VALUES(?,?,?)", ("printing-other", 0, 1))
    conn.execute("INSERT INTO image_slots VALUES(?,?,?,?,?,?,?)", ("printing-other", 0, "fr", None, "missing", None, "{}"))
    conn.commit(); conn.close()
    old_second_manifest = hashlib.sha256(second_manifest.read_bytes()).hexdigest()

    result = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                               test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert result["printing_id"] == "printing-demo" and result["edition"] == "tst"
    assert json.loads(first_manifest.read_text(encoding="utf-8"))["slots"][0]["status"] == "ready"
    assert hashlib.sha256(second_manifest.read_bytes()).hexdigest() == old_second_manifest
    conn = sqlite3.connect(db)
    second_slot = conn.execute("SELECT status,asset_id FROM image_slots WHERE printing_id='printing-other' AND face_index=0").fetchone()
    conn.close()
    assert second_slot == ("missing", None)


def test_split_layout_uses_root_image_uri_when_face_uris_absent():
    raw = {"layout": "split", "image_uris": {"png": "https://cards.scryfall.io/card.png"},
           "card_faces": [{"name": "Front"}, {"name": "Back"}]}
    assert _ordered_uris(raw, 0) == ["https://cards.scryfall.io/card.png"]


def test_manual_alternates_are_exact_and_hosts_are_scoped_to_each_entry():
    lotus = _manual_candidates("WHO", "77", 0)
    assert len(lotus) == 1 and lotus[0]["source"] == "Lotus Noir"
    assert _allowed_url(lotus[0]["url"], allowed_hosts={lotus[0]["allow_host"]})
    assert not _allowed_url(lotus[0]["url"])
    assert _manual_candidates("who", "77", 1) == []
    assert _manual_candidates("other", "77", 0) == []


def test_deep_refresh_uses_exact_manual_french_source_and_keeps_better_asset(tmp_path, image_server, monkeypatch):
    def manual_candidate(width_path):
        return {"set_code": "tst", "collector_number": "1a", "face_index": 0, "language": "fr",
                "url": image_server + width_path, "allow_host": "127.0.0.1",
                "source": "Manual test source", "evidence": "Exact fixture edition and number."}

    ops, _, db, _ = make_catalogue(tmp_path / "same", image_server, fr_path="/corrupt", en_path="/image")
    monkeypatch.setattr(image_download, "_manual_candidates", lambda set_code, number, face: [manual_candidate("/high")] if (set_code, number, face) == ("tst", "1a", 0) else [])
    first = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                              test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert first["results"][0]["language"] == "en"
    organized = list((ops / "Images" / "TST - Test Edition").glob("*.webp"))
    assert len(organized) == 1
    assert organized[0].samefile(db.parent / first["results"][0]["relative_path"])
    refreshed = refresh_printing_deep("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                                      test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert refreshed["results"][0]["language"] == "fr"
    assert list((ops / "Images" / "TST - Test Edition").glob("*.webp")) == organized
    assert organized[0].samefile(db.parent / refreshed["results"][0]["relative_path"])
    internal = db.parent / refreshed["results"][0]["relative_path"]
    internal.unlink()
    from bibliotheque_mtg.organized_images import synchronize_organized_images
    assert synchronize_organized_images(ops) == {"ready": 1, "organized": 1, "missing": 0}
    assert internal.samefile(organized[0])
    conn = sqlite3.connect(db)
    selected = conn.execute("SELECT s.selected_language,a.provenance_json FROM image_slots s JOIN image_assets a USING(asset_id)").fetchone()
    conn.close()
    assert selected[0] == "fr"
    provenance = json.loads(selected[1])
    assert provenance["provider"] == "Manual test source"
    assert provenance["source_evidence"] == "Exact fixture edition and number."

    ops2, _, db2, _ = make_catalogue(tmp_path / "lower", image_server, fr_path="/corrupt", en_path="/image")
    monkeypatch.setattr(image_download, "_manual_candidates", lambda set_code, number, face: [manual_candidate("/medium")] if (set_code, number, face) == ("tst", "1a", 0) else [])
    initial = download_printing("provider-fr", catalogue_root=ops2, ledger_path=ops2 / "ledger.json",
                               test_hosts={"127.0.0.1"}, face_indexes=[0])
    prior_path = initial["results"][0]["relative_path"]
    retained = refresh_printing_deep("provider-fr", catalogue_root=ops2, ledger_path=ops2 / "ledger.json",
                                     test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert retained["results"][0]["status"] == "lower_quality_kept_existing"
    conn = sqlite3.connect(db2)
    lang, asset = conn.execute("SELECT s.selected_language,a.relative_path FROM image_slots s JOIN image_assets a USING(asset_id)").fetchone()
    conn.close()
    assert lang == "en" and asset == prior_path


def test_more_than_400px_french_scan_is_accepted_and_normalized(tmp_path, image_server):
    ops, version, db, _ = make_catalogue(tmp_path, image_server, fr_path="/medium", en_path="/image")
    result = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                               test_hosts={"127.0.0.1"}, face_indexes=[0])
    card = result["results"][0]
    assert card["status"] == "ready" and card["language"] == "fr"
    assert (card["source_width"], card["source_height"]) == (420, 650)
    assert (card["width"], card["height"]) == (531, 738)
    with Image.open(version / card["relative_path"]) as image:
        assert image.format == "WEBP" and image.size == (531, 738)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT width,height FROM image_assets").fetchone() == (531, 738)
    conn.close()


def test_deep_refresh_replaces_exact_printing_with_best_french_candidate(tmp_path, image_server):
    ops, version, db, manifest = make_catalogue(tmp_path, image_server, fr_path="/medium", en_path="/image")
    conn = sqlite3.connect(db)
    raw = json.loads(conn.execute("SELECT raw_json FROM source_records WHERE lang='fr'").fetchone()[0])
    raw["image_uris"] = {"png": image_server + "/medium", "large": image_server + "/image",
                         "normal": image_server + "/small"}
    conn.execute("UPDATE source_records SET raw_json=? WHERE lang='fr'", (json.dumps(raw),))
    conn.commit(); conn.close()

    first = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                              test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert first["results"][0]["language"] == "fr"
    assert first["results"][0]["source_width"] == 420
    previous_path = first["results"][0]["relative_path"]

    refreshed = refresh_printing_deep("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                                      test_hosts={"127.0.0.1"}, face_indexes=[0])
    card = refreshed["results"][0]
    assert refreshed["printing_id"] == "printing-demo" and refreshed["edition"] == "tst"
    assert refreshed["mode"] == "deep_refresh"
    assert card["status"] == "ready" and card["language"] == "fr"
    assert card["source_width"] == 600
    assert card["relative_path"] != previous_path
    assert json.loads(manifest.read_text(encoding="utf-8"))["slots"][0]["path"] == card["relative_path"]
    conn = sqlite3.connect(db)
    asset = conn.execute("SELECT provenance_json FROM image_assets WHERE asset_id=(SELECT asset_id FROM image_slots WHERE printing_id='printing-demo' AND face_index=0)").fetchone()[0]
    conn.close()
    assert json.loads(asset)["acquisition_mode"] == "deep_refresh"


def test_deep_refresh_retains_ready_asset_if_candidates_fail(tmp_path, image_server):
    ops, version, db, _ = make_catalogue(tmp_path, image_server, fr_path="/medium", en_path="/image")
    first = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                              test_hosts={"127.0.0.1"}, face_indexes=[0])
    old_path = first["results"][0]["relative_path"]
    conn = sqlite3.connect(db)
    raw = json.loads(conn.execute("SELECT raw_json FROM source_records WHERE lang='fr'").fetchone()[0])
    raw["image_uris"] = {"png": image_server + "/corrupt"}
    raw_en = json.loads(conn.execute("SELECT raw_json FROM source_records WHERE lang='en'").fetchone()[0])
    raw_en["image_uris"] = {"png": image_server + "/corrupt"}
    conn.execute("UPDATE source_records SET raw_json=? WHERE lang='fr'", (json.dumps(raw),))
    conn.execute("UPDATE source_records SET raw_json=? WHERE lang='en'", (json.dumps(raw_en),))
    conn.commit(); conn.close()
    result = refresh_printing_deep("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                                   test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert result["results"][0]["status"] == "refresh_failed_kept_existing"
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT status,asset_id FROM image_slots WHERE printing_id='printing-demo' AND face_index=0").fetchone()
    conn.close()
    assert row == ("ready", hashlib.sha256((version / old_path).read_bytes()).hexdigest())


def test_preview_candidate_does_not_publish_until_explicit_commit(tmp_path, image_server):
    ops, _, db, manifest = make_catalogue(tmp_path, image_server, fr_path="/high", en_path="/image")
    before_manifest = manifest.read_bytes()
    conn = sqlite3.connect(db)
    before_slot = conn.execute("SELECT status,asset_id,selected_language,provenance_json FROM image_slots").fetchone()
    before_assets = conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0]
    conn.close()

    preview = preview_printing_candidate("provider-fr", catalogue_root=ops,
                                         ledger_path=ops / "ledger.json", face_index=0,
                                         test_hosts={"127.0.0.1"})
    assert preview["status"] == "candidate"
    candidate = preview["candidates"][0]
    assert {item["language"] for item in preview["candidates"]} == {"fr", "en"}
    assert candidate["provider_card_id"] == "provider-fr"
    assert candidate["printing_id"] == "printing-demo" and candidate["version_id"] == "demo"
    assert candidate["face_index"] == 0 and candidate["language"] == "fr"
    assert candidate["source_data"].startswith(b"\x89PNG")
    assert candidate["mime_type"] == "image/png"
    assert (candidate["source_width"], candidate["source_height"]) == (900, 1200)
    assert candidate["old_asset_id"] is None and candidate["old_status"] == "missing"

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status,asset_id,selected_language,provenance_json FROM image_slots").fetchone() == before_slot
    assert conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0] == before_assets
    conn.close()
    assert manifest.read_bytes() == before_manifest

    published = publish_preview_candidate(candidate, catalogue_root=ops,
                                          provider_card_id="provider-fr", face_index=0)
    assert published["status"] == "ready" and published["language"] == "fr"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status,selected_language,asset_id FROM image_slots WHERE printing_id='printing-demo' AND face_index=0").fetchone() == ("ready", "fr", hashlib.sha256((ops / "versions" / "demo" / published["relative_path"]).read_bytes()).hexdigest())
    conn.close()
    assert json.loads(manifest.read_text(encoding="utf-8"))["slots"][0]["path"] == published["relative_path"]


def test_preview_excludes_current_source_and_keeps_other_language(tmp_path, image_server):
    ops, _, _, _ = make_catalogue(tmp_path, image_server, fr_path="/high", en_path="/image")
    first = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                              test_hosts={"127.0.0.1"})
    assert first["results"][0]["status"] == "ready"
    preview = preview_printing_candidate("provider-fr", catalogue_root=ops,
                                         ledger_path=ops / "ledger.json", face_index=0,
                                         test_hosts={"127.0.0.1"})
    assert preview["status"] == "candidate"
    assert all(item["source_url"] != first["results"][0].get("source_url") for item in preview["candidates"])
    assert {item["language"] for item in preview["candidates"]} == {"en"}


def test_official_gallery_matches_exact_french_name(monkeypatch):
    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def iter_content(self, size):
            yield ('<img alt="Effort collectif" '
                   'src="http://media.wizards.com/2016/test/fr_card.png">'
                   '<img alt="Autre carte" src="https://media.wizards.com/other.png">').encode()

        def close(self):
            pass

    class Session:
        def get(self, url, **kwargs):
            assert url.endswith("/fr/products/eldritch-moon/card-image-gallery")
            return Response()

    monkeypatch.setattr(image_download, "_GALLERY_CACHE", {})
    found = image_download._official_gallery_images("Eldritch Moon", "Effort collectif", Session())
    assert len(found) == 1
    assert found[0]["url"] == "https://media.wizards.com/2016/test/fr_card.png"
    assert image_download._official_gallery_images("Eldritch Moon", "Autre nom", Session()) == []


def test_publish_preview_candidate_rejects_stale_version_identity(tmp_path, image_server):
    ops, _, _, _ = make_catalogue(tmp_path, image_server, fr_path="/image", en_path="/image")
    preview = preview_printing_candidate("provider-fr", catalogue_root=ops,
                                         ledger_path=ops / "ledger.json", face_index=0,
                                         test_hosts={"127.0.0.1"})
    assert preview["status"] == "candidate"
    current = json.loads((ops / "current.json").read_text(encoding="utf-8"))
    current["version_id"] = "replacement"
    (ops / "current.json").write_text(json.dumps(current), encoding="utf-8")
    with pytest.raises(ValueError, match="version active a changé"):
        publish_preview_candidate(preview["candidates"][0], catalogue_root=ops,
                                  provider_card_id="provider-fr", face_index=0)


def test_live_fr_metadata_lookup_uses_exact_path_and_bounded_headers(monkeypatch):
    class JsonResponse:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        def iter_content(self, chunk_size):
            yield b'{"set":"who","collector_number":"77","lang":"fr"}'
        def close(self): pass

    class Session:
        def __init__(self): self.calls = []
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs)); return JsonResponse()

    session = Session()
    monkeypatch.setattr(image_download, "_SCRYFALL_API_MIN_INTERVAL", 0)
    raw, status, retry = _fetch_live_fr("who", "77", session)
    assert status == "candidate_found" and retry is None
    assert raw["collector_number"] == "77"
    url, options = session.calls[0]
    assert url == "https://api.scryfall.com/cards/who/77/fr"
    assert options["allow_redirects"] is False and options["timeout"] == (8, 12)
    assert options["headers"]["User-Agent"] == "BibliothequeMagicLocal/2.0"


def test_deep_refresh_uses_new_exact_french_api_record(tmp_path, image_server, monkeypatch):
    ops, _, db, _ = make_catalogue(tmp_path, image_server, fr_path="/corrupt", en_path="/image")
    conn = sqlite3.connect(db)
    expected = json.loads(conn.execute("SELECT raw_json FROM source_records WHERE lang='en'").fetchone()[0])
    expected.update({"oracle_id": "oracle-fixture", "illustration_id": "illustration-fixture"})
    conn.execute("UPDATE source_records SET raw_json=? WHERE lang='en'", (json.dumps(expected),))
    conn.commit(); conn.close()
    live = {"id": "live-fr-provider", "set": "tst", "set_id": "set-id", "collector_number": "1a", "lang": "fr",
            "layout": "normal", "oracle_id": "oracle-fixture", "illustration_id": "illustration-fixture",
            "image_uris": {"png": image_server + "/high"}}
    calls = []
    def live_lookup(set_code, collector_number, session, cancel_event=None):
        calls.append((set_code, collector_number))
        return live, "candidate_found", None
    monkeypatch.setattr(image_download, "_fetch_live_fr", live_lookup)

    report = refresh_printing_deep("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                                   test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert calls == [("tst", "1a")]
    assert report["results"][0]["status"] == "ready"
    assert report["results"][0]["language"] == "fr"
    assert any(item.get("status") == "candidate_identity_verified" for item in report["attempts"])
    conn = sqlite3.connect(db)
    provenance = conn.execute("SELECT provenance_json FROM image_slots WHERE printing_id='printing-demo'").fetchone()[0]
    source_rows = conn.execute("SELECT COUNT(*) FROM source_records WHERE lang='fr'").fetchone()[0]
    conn.close()
    assert json.loads(provenance)["source_url"] == image_server + "/high"
    assert json.loads(provenance)["provider"] == "scryfall_api"
    assert json.loads(provenance)["source_record_id"] == "live-fr-provider"
    assert source_rows == 1


def test_deep_refresh_rejects_live_french_identity_mismatch(tmp_path, image_server, monkeypatch):
    ops, _, _, _ = make_catalogue(tmp_path, image_server, fr_path="/corrupt", en_path="/image")
    live = {"set": "tst", "set_id": "different-set", "collector_number": "1a", "lang": "fr",
            "layout": "normal", "oracle_id": "oracle-fixture", "image_uris": {"png": image_server + "/high"}}
    monkeypatch.setattr(image_download, "_fetch_live_fr", lambda *args, **kwargs: (live, "candidate_found", None))
    report = refresh_printing_deep("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                                   test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert report["results"][0]["language"] == "en"
    assert any(item.get("status") == "candidate_identity_rejected" for item in report["attempts"])


def test_encoder_normalizes_small_source_without_distortion_or_crop():
    source = patterned_image((420, 650))
    raw = io.BytesIO(); source.save(raw, "PNG")
    encoded, width, height = _encode_webp(raw.getvalue())
    assert (width, height) == (531, 738)
    with Image.open(io.BytesIO(encoded)) as result:
        assert result.format == "WEBP" and result.size == (531, 738)


def test_429_persists_retry_budget_and_html_corruption_never_publish(tmp_path, image_server):
    ops, _, db, _ = make_catalogue(tmp_path, image_server, fr_path="/429", en_path="/image")
    ledger = ops / "ledger.json"
    result = download_printing("provider-fr", catalogue_root=ops, ledger_path=ledger, test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert result["results"][0]["status"] == "retry_later"
    count = ImageFixtureHandler.counts["/429"]
    result2 = download_printing("provider-fr", catalogue_root=ops, ledger_path=ledger, test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert result2["results"][0]["status"] == "retry_later" and ImageFixtureHandler.counts["/429"] == count
    conn = sqlite3.connect(db); assert conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0] == 0; conn.close()
    ops2, _, db2, _ = make_catalogue(tmp_path / "htmlcase", image_server, fr_path="/html", en_path="/corrupt")
    failed = download_printing("provider-fr", catalogue_root=ops2, ledger_path=ops2 / "ledger.json", test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert failed["results"][0]["status"] == "missing"
    conn = sqlite3.connect(db2); assert conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0] == 0; conn.close()


def test_403_stops_provider_and_low_resolution_french_falls_back_to_hd_english(tmp_path, image_server):
    en_before = ImageFixtureHandler.counts.get("/image", 0)
    blocked_ops, _, _, _ = make_catalogue(tmp_path / "blocked", image_server, fr_path="/403", en_path="/image")
    blocked = download_printing("provider-fr", catalogue_root=blocked_ops, ledger_path=blocked_ops / "ledger.json",
                                test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert blocked["results"][0]["status"] == "blocked"
    assert ImageFixtureHandler.counts.get("/image", 0) == en_before
    ops, version, _, manifest = make_catalogue(tmp_path / "quality", image_server, fr_path="/small", en_path="/image")
    result = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                               test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert result["results"][0]["language"] == "en"
    assert result["results"][0]["source_width"] == 600
    assert json.loads(manifest.read_text(encoding="utf-8"))["slots"][0]["selected_language"] == "en"
    assert (version / result["results"][0]["relative_path"]).is_file()


def test_empty_urls_and_three_get_budget_per_language_face(tmp_path, image_server):
    ops, _, db, _ = make_catalogue(tmp_path / "empty", image_server, fr_path="/403", en_path="/image")
    conn = sqlite3.connect(db)
    raw = json.loads(conn.execute("SELECT raw_json FROM source_records WHERE lang='fr'").fetchone()[0])
    raw["image_uris"] = {"png": "", "large": None}
    conn.execute("UPDATE source_records SET raw_json=? WHERE lang='fr'", (json.dumps(raw),)); conn.commit(); conn.close()
    result = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json", test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert result["results"][0]["language"] == "en"

    ops2, _, db2, _ = make_catalogue(tmp_path / "budget", image_server, fr_path="/corrupt", en_path="/image")
    conn = sqlite3.connect(db2)
    raw = json.loads(conn.execute("SELECT raw_json FROM source_records WHERE lang='fr'").fetchone()[0])
    raw["image_uris"] = {"png": image_server+"/corrupt", "large": image_server+"/corrupt", "jpeg": image_server+"/corrupt", "normal": image_server+"/corrupt"}
    conn.execute("UPDATE source_records SET raw_json=? WHERE lang='fr'", (json.dumps(raw),)); conn.commit(); conn.close()
    before = ImageFixtureHandler.counts.get("/corrupt", 0)
    report = download_printing("provider-fr", catalogue_root=ops2, ledger_path=ops2 / "ledger.json", test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert report["results"][0]["language"] == "en"
    assert ImageFixtureHandler.counts["/corrupt"] - before == 3
    ledger = json.loads((ops2 / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["faces"]["printing-demo:0:fr"]["gets"] == 3


def test_interrupted_attempt_resumes_and_double_click_deduplicates(tmp_path, image_server):
    ops, _, db, _ = make_catalogue(tmp_path / "resume", image_server, fr_path="/image", en_path="/image")
    conn = sqlite3.connect(db)
    raw = json.loads(conn.execute("SELECT raw_json FROM source_records WHERE lang='en'").fetchone()[0])
    raw.pop("image_uris", None)
    conn.execute("UPDATE source_records SET raw_json=? WHERE lang='en'", (json.dumps(raw),)); conn.commit(); conn.close()

    class OneInterruption:
        def __init__(self): self.first = True; self.session = requests.Session()
        def get(self, *args, **kwargs):
            if self.first:
                self.first = False
                raise requests.Timeout("simulated interruption before response")
            return self.session.get(*args, **kwargs)

    ledger = ops / "ledger.json"; session = OneInterruption()
    interrupted = download_printing("provider-fr", catalogue_root=ops, ledger_path=ledger, session=session,
                                    test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert interrupted["results"][0]["status"] == "missing"
    resumed = download_printing("provider-fr", catalogue_root=ops, ledger_path=ledger, session=session,
                                test_hosts={"127.0.0.1"}, face_indexes=[0])
    assert resumed["results"][0]["status"] == "ready"

    ops2, _, _, _ = make_catalogue(tmp_path / "double", image_server, fr_path="/image", en_path="/image")
    before = ImageFixtureHandler.counts.get("/image", 0)
    def go():
        return download_printing("provider-fr", catalogue_root=ops2, ledger_path=ops2 / "ledger.json",
                                test_hosts={"127.0.0.1"}, face_indexes=[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: go(), range(2)))
    assert sorted(r["results"][0]["status"] for r in reports) == ["already_ready", "ready"]
    assert ImageFixtureHandler.counts.get("/image", 0) - before == 1


def test_manual_import_wins_over_batch_fetch_in_progress(tmp_path, image_server, monkeypatch):
    ops, _, db, _ = make_catalogue(tmp_path, image_server)
    original_fetch = image_download._fetch
    manual = io.BytesIO()
    patterned_image((700, 900)).save(manual, "PNG")
    imported = False

    def fetch_after_import(*args, **kwargs):
        nonlocal imported
        if not imported:
            imported = True
            image_download.import_local_image("provider-fr", 0, manual.getvalue(), catalogue_root=ops)
        return original_fetch(*args, **kwargs)

    monkeypatch.setattr(image_download, "_fetch", fetch_after_import)
    report = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                               test_hosts={"127.0.0.1"}, parallel_fetch=True)
    assert report["results"][0]["status"] == "already_ready"
    with sqlite3.connect(db) as conn:
        provenance = json.loads(conn.execute("SELECT provenance_json FROM image_slots").fetchone()[0])
    assert provenance["provider"] == "manual_import"


def test_active_download_can_be_cancelled_without_publishing(tmp_path, image_server):
    ops, _, db, _ = make_catalogue(tmp_path, image_server, fr_path="/image", en_path="/image")
    event = threading.Event()

    class Response:
        status_code = 200
        headers = {"Content-Type": "image/png", "Content-Length": "100"}
        def iter_content(self, chunk_size): yield b"x" * 100
        def close(self): pass

    class CancellingSession:
        def get(self, *args, **kwargs): event.set(); return Response()

    report = download_printing("provider-fr", catalogue_root=ops, ledger_path=ops / "ledger.json",
                               session=CancellingSession(), test_hosts={"127.0.0.1"}, face_indexes=[0], cancel_event=event)
    assert report["results"][0]["status"] == "cancelled"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM image_slots").fetchone()[0] == "missing"
    assert conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0] == 0
    conn.close()
