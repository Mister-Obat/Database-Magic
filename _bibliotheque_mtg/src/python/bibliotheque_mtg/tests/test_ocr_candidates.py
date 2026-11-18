import sqlite3
import json

from bibliotheque_mtg import ocr_candidates as ocr


def _connection(rows):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE editions(set_id TEXT, code TEXT, name TEXT);
        CREATE TABLE printings(printing_id TEXT, set_id TEXT, collector_number TEXT);
        CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT,
            name TEXT, printed_name TEXT, lang TEXT, raw_json TEXT);
    """)
    conn.executemany("INSERT INTO editions VALUES(?,?,?)", [("s1", "ABC", "Alpha Beta")])
    conn.executemany("INSERT INTO printings VALUES(?,?,?)", [(r[0], "s1", r[1]) for r in rows])
    conn.executemany("INSERT INTO source_records VALUES(?,?,?,?,?,?)",
        [(r[2], r[0], r[3], r[3], r[4], '{"artist":"Ryan Pancoast"}') for r in rows])
    return conn


def _mock_ocr(monkeypatch, full, header, footer):
    monkeypatch.setattr(ocr, "_ocr_image", lambda *args, **kwargs: {
        "text_by_region": {"full": full, "header": header, "footer": footer},
        "confidence_by_region": {"full": 90}, "language_models": ["eng", "fra"]})


def test_proposes_only_single_composite_match(tmp_path, monkeypatch):
    conn = _connection([("p1", "42", "card1", "Ancestral Recall", "en")])
    _mock_ocr(monkeypatch, "Ancestral Recall", "Ancestral Recall ABC", "42/300 ABC")
    result = ocr.propose_ocr(tmp_path / "card.png", conn, {})
    assert result["status"] == "unique"
    assert result["target"] == {"provider_card_id": "card1", "printing_id": "p1",
        "set_code": "ABC", "collector_number": "42", "face_index": 0, "language": "en",
        "title": "Ancestral Recall"}
    assert result["evidence"]["identity_proven"] is True


def test_keeps_duplicate_printings_ambiguous(tmp_path, monkeypatch):
    conn = _connection([("p1", "42", "card1", "Ancestral Recall", "en"),
                        ("p2", "42", "card2", "Ancestral Recall", "en")])
    _mock_ocr(monkeypatch, "Ancestral Recall", "Ancestral Recall ABC", "42/300 ABC")
    result = ocr.propose_ocr(tmp_path / "card.png", conn, {})
    assert result["status"] == "ambiguous"
    assert result["target"] is None


def test_folder_alias_cannot_replace_title_or_number(tmp_path, monkeypatch):
    conn = _connection([("p1", "42", "card1", "Ancestral Recall", "en")])
    _mock_ocr(monkeypatch, "Unrecognized", "Unrecognized", "99/300")
    result = ocr.propose_ocr(tmp_path / "card.png", conn,
        {"folder": {"status": "confirmed_exact_candidate", "code": "ABC"}})
    assert result["status"] == "none"
    assert result["target"] is None


def test_confirmed_folder_can_use_title_and_artist_without_number(tmp_path, monkeypatch):
    conn = _connection([("p1", "42", "card1", "Ancestral Recall", "en")])
    _mock_ocr(monkeypatch, "Ancestral Recall Ryan Pancoast", "Ancestral Recall", "Ryan Pancoast")
    result = ocr.propose_ocr(tmp_path / "folder" / "card.png", conn,
        {"folder": {"status": "confirmed_exact_candidate", "code": "ABC"}})
    assert result["status"] == "unique"
    assert result["target"]["printing_id"] == "p1"
    assert result["evidence"]["identity_method"] == "confirmed_folder_title_artist"


def test_ocr_language_hint_prefers_matching_localization(tmp_path, monkeypatch):
    conn = _connection([("p1", "42", "card-fr", "Ancestral Recall", "fr"),
                        ("p1", "42", "card-en", "Ancestral Recall", "en"),
                        ("p1", "42", "card-de", "Ancestral Recall", "de")])
    _mock_ocr(monkeypatch, "Ancestral Recall ABC", "Ancestral Recall ABC", "42/300 ABC")
    result = ocr.propose_ocr(tmp_path / "scan_fr.png", conn, {})
    assert result["status"] == "unique"
    assert result["target"]["language"] == "fr"
    assert result["evidence"]["language_proven"] is False


def test_precise_title_crop_proves_only_one_language(tmp_path, monkeypatch):
    conn = _connection([("p1", "42", "card-fr", "Rappel ancestral", "fr"),
                        ("p1", "42", "card-en", "Ancestral Recall", "en")])
    monkeypatch.setattr(ocr, "_ocr_image", lambda *args, **kwargs: {
        "text_by_region": {"full": "Rappel ancestral", "header": "Rappel ancestral",
                           "title": "Rappel ancestral", "footer": "42/300 ABC"},
        "confidence_by_region": {}, "language_models": ["eng", "fra"]})
    result = ocr.propose_ocr(tmp_path / "scan.png", conn, {})
    assert result["status"] == "unique"
    assert result["target"]["language"] == "fr"
    assert result["evidence"]["language_proven"] is True
    assert result["evidence"]["title_crop_score"] == 1.0


def test_token_abbreviation_and_zero_padded_number_resolve_exact_set(tmp_path, monkeypatch):
    conn = _connection([("p1", "25", "card1", "Insect", "en")])
    conn.execute("UPDATE editions SET code='tmh3'")
    _mock_ocr(monkeypatch, "Insect T 0025 MH3 EN", "Insect", "T 0025 MH3 EN")
    result = ocr.propose_ocr(tmp_path / "Horizons du Modern 3 (2024)" / "en_0078090090.png",
        conn, {})
    assert result["status"] == "unique"
    assert result["target"]["set_code"] == "tmh3"
    assert result["target"]["collector_number"] == "25"
    assert result["evidence"]["identity_method"] == "ocr_token_set_prefix_number_title"


def test_printed_neo_code_overrides_a_conflicting_folder_alias(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE editions(set_id TEXT, code TEXT, name TEXT);
        CREATE TABLE printings(printing_id TEXT, set_id TEXT, collector_number TEXT);
        CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT,
            name TEXT, printed_name TEXT, lang TEXT, raw_json TEXT);
    """)
    conn.executemany("INSERT INTO editions VALUES(?,?,?)", [
        ("old", "ktk", "Khans of Tarkir"), ("new", "neo", "Neon Dynasty")])
    conn.executemany("INSERT INTO printings VALUES(?,?,?)", [
        ("p-old", "old", "226"), ("p-neo", "new", "226")])
    conn.executemany("INSERT INTO source_records VALUES(?,?,?,?,?,?)", [
        ("c-old", "p-old", "Kaito Shizuki", "Kaito Shizuki", "fr", '{"artist":"Yongjae Choi"}'),
        ("c-neo", "p-neo", "Kaito Shizuki", "Kaito Shizuki", "fr", '{"artist":"Yongjae Choi"}')])
    _mock_ocr(monkeypatch, "Kaito Shizuki 226/302 NEO FR", "Kaito Shizuki", "226/302 NEO FR")
    path = tmp_path / "Kamigawa" / "fr_0eJ6yTeVtP.png"
    result = ocr.propose_ocr(path, conn,
        {"Kamigawa": {"status": "confirmed_exact_candidate", "code": "ktk"}})
    assert result["status"] == "unique"
    assert result["target"]["printing_id"] == "p-neo"
    assert result["target"]["set_code"] == "neo"


def test_verso_scan_proves_the_matching_face(tmp_path, monkeypatch):
    conn = _connection([("p1", "12", "card1", "Lune écarlate // Écho nocturne", "fr")])
    conn.execute("""UPDATE source_records SET raw_json=? WHERE provider_card_id='card1'""",
        (json.dumps({"artist": "Ryan Pancoast", "card_faces": [
            {"name": "Lune écarlate"}, {"name": "Écho nocturne"}]}),))
    monkeypatch.setattr(ocr, "_ocr_image", lambda *args, **kwargs: {
        "text_by_region": {"full": "Écho nocturne ABC 12", "header": "Écho nocturne",
                           "title": "Écho nocturne", "footer": "ABC · 12"},
        "confidence_by_region": {}, "language_models": ["eng", "fra"]})
    result = ocr.propose_ocr(tmp_path / "verso.png", conn, {})
    assert result["status"] == "unique"
    assert result["target"]["face_index"] == 1
    assert result["target"]["title"] == "Écho nocturne"
    assert result["evidence"]["language_proven"] is True


def test_recto_scan_still_proves_the_front_face(tmp_path, monkeypatch):
    conn = _connection([("p1", "12", "card1", "Lune écarlate // Écho nocturne", "fr")])
    conn.execute("""UPDATE source_records SET raw_json=? WHERE provider_card_id='card1'""",
        (json.dumps({"artist": "Ryan Pancoast", "card_faces": [
            {"name": "Lune écarlate"}, {"name": "Écho nocturne"}]}),))
    monkeypatch.setattr(ocr, "_ocr_image", lambda *args, **kwargs: {
        "text_by_region": {"full": "Lune écarlate ABC 12", "header": "Lune écarlate",
                           "title": "Lune écarlate", "footer": "ABC · 12"},
        "confidence_by_region": {}, "language_models": ["eng", "fra"]})
    result = ocr.propose_ocr(tmp_path / "recto.png", conn, {})
    assert result["status"] == "unique"
    assert result["target"]["face_index"] == 0
    assert result["target"]["title"] == "Lune écarlate"


def test_suggestion_rank_is_not_identity_proof():
    catalogue = [{"printing_id": "p1", "code": "ABC", "edition": "Alpha Beta",
        "collector_number": "42", "name": "Ancestral Recall", "printed_name": "Ancestral Recall", "lang": "en"}]
    candidates, evidence = ocr._candidate_rows({"text_by_region": {
        "full": "Ancestral Recall", "header": "Ancestral Recall", "footer": "42"}}, catalogue)
    assert candidates
    assert evidence["identity_proven"] is False


def test_report_is_atomic_and_resumes_unchanged_files(tmp_path, monkeypatch):
    library = tmp_path / "Cartes Magic"
    unknown = library / "Inconnu"
    unknown.mkdir(parents=True)
    image_path = unknown / "card.png"
    from PIL import Image
    Image.new("RGB", (20, 30), "white").save(image_path)
    staging = tmp_path / "catalogue"
    (staging / "versions" / "v1").mkdir(parents=True)
    database = staging / "versions" / "v1" / "catalogue.sqlite"
    sqlite3.connect(database).close()
    (staging / "versions" / "v1" / "editions").mkdir()
    from bibliotheque_mtg.v2.publish import atomic_publish_pointer
    atomic_publish_pointer(staging, "v1", "versions/v1/catalogue.sqlite", "versions/v1/editions")
    tesseract = tmp_path / "tesseract.exe"
    tesseract.write_text("stub", encoding="utf-8")
    calls = []
    monkeypatch.setattr(ocr, "_ocr_image", lambda *args, **kwargs: calls.append(1) or {
        "text_by_region": {"full": "", "header": "", "footer": "", "title": ""},
        "confidence_by_region": {}, "language_models": ["eng", "fra"], "text": ""})
    report = tmp_path / "partial.json"
    first = ocr.generate_candidates(library, staging, report, tesseract=tesseract,
        tessdata=tmp_path, limit=1)
    second = ocr.generate_candidates(library, staging, report, tesseract=tesseract,
        tessdata=tmp_path, limit=1)
    assert first["processed_count"] == second["processed_count"] == 1
    assert len(calls) == 1
    assert report.is_file()
    assert not report.with_name(report.name + ".part").exists()
