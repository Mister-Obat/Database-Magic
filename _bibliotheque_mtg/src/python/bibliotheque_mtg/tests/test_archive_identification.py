import sqlite3
import json

import pytest

from bibliotheque_mtg import archive_identification as archive


def _database(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE editions(set_id TEXT, code TEXT, name TEXT, eligibility TEXT);
            CREATE TABLE printings(printing_id TEXT, set_id TEXT, collector_number TEXT);
            CREATE TABLE source_records(provider_card_id TEXT, printing_id TEXT,
                lang TEXT, name TEXT, printed_name TEXT, raw_json TEXT);
            CREATE TABLE image_slots(printing_id TEXT, face_index INTEGER, status TEXT);
            INSERT INTO editions VALUES('set-1','ABC','Alpha Beta','eligible');
            INSERT INTO printings VALUES('print-1','set-1','12');
            INSERT INTO source_records VALUES('card-fr','print-1','fr','Front','Avant',
                '{"card_faces":[{"name":"Front","printed_name":"Avant"},{"name":"Back","printed_name":"Arrière"}]}');
            INSERT INTO source_records VALUES('card-en','print-1','en','Front',NULL,
                '{"card_faces":[{"name":"Front"},{"name":"Back"}]}');
            INSERT INTO image_slots VALUES('print-1',0,'missing');
            INSERT INTO image_slots VALUES('print-1',1,'missing');
        """)


def test_manual_search_is_explicitly_suggestion_only(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(archive.api, "dispatch", lambda db, route, params: seen.update(
        database=db, route=route, params=params) or {"items": []})
    result = archive.search_archive_printings(tmp_path / "catalogue.sqlite", "black lotus",
                                              set_code="LEA", language="en", limit=500)
    assert result["identity_proven"] is False
    assert result["warning"]
    assert seen["params"] == {"q": "black lotus", "set": "LEA", "lang": "en", "limit": 100, "offset": 0}


def test_archive_search_uses_local_set_icon_and_rejects_other_hosts(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sets(code TEXT, raw_json TEXT)")
        connection.executemany("INSERT INTO sets VALUES(?,?)", [
            ("lea", json.dumps({"icon_svg_uri": "https://svgs.scryfall.io/sets/lea.svg?1"})),
            ("bad", json.dumps({"icon_svg_uri": "https://example.com/bad.svg"}))])
    monkeypatch.setattr(archive.api, "dispatch", lambda *args: {"items": [
        {"set_code": "lea"}, {"set_code": "bad"}]})
    result = archive.search_archive_printings(tmp_path / "active.sqlite", "Bayou",
                                              source_database=source)
    assert result["items"][0]["edition_icon_url"] == "https://svgs.scryfall.io/sets/lea.svg?1"
    assert result["items"][1]["edition_icon_url"] is None


@pytest.mark.parametrize("query", ["", "x", "a" * 201, None])
def test_manual_search_rejects_bad_query(tmp_path, query):
    with pytest.raises(ValueError):
        archive.search_archive_printings(tmp_path / "catalogue.sqlite", query)


def test_manual_target_returns_face_specific_identity(tmp_path):
    db = tmp_path / "catalogue.sqlite"
    _database(db)
    result = archive.describe_archive_target(db, provider_card_id="card-fr",
        printing_id="print-1", face_index=1, language="fr")
    assert result["title"] == "Arrière"
    assert result["set_code"] == "ABC"
    assert result["collector_number"] == "12"
    assert result["manual_review_required"] is True


@pytest.mark.parametrize("provider,printing,face,language", [
    ("card-fr", "wrong", 0, "fr"), ("card-fr", "print-1", 3, "fr"),
    ("card-fr", "print-1", 0, "de"), ("card-en", "print-1", 0, "fr"),
])
def test_manual_target_rejects_mismatch(tmp_path, provider, printing, face, language):
    db = tmp_path / "catalogue.sqlite"
    _database(db)
    with pytest.raises(ValueError):
        archive.describe_archive_target(db, provider_card_id=provider,
            printing_id=printing, face_index=face, language=language)


def test_archive_ocr_is_read_only_and_returns_source_hash(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    root.mkdir()
    image = root / "folder" / "card.png"
    image.parent.mkdir()
    image.write_bytes(b"source image bytes")
    database = tmp_path / "catalogue.sqlite"
    sqlite3.connect(database).close()
    observed = {}
    monkeypatch.setattr(archive.ocr_candidates, "propose_ocr", lambda path, conn, alias:
                        observed.update(path=path, alias=alias) or {"status": "none", "target": None})
    result = archive.propose_archive_ocr(root, "folder/card.png", database)
    assert result["sha256"] == "e12a7dec4b2ed7f9c36fdf86921a2e97fe9010e0349c97f19be0af261cf95ba6"
    assert result["identity_proposal"]["status"] == "none"
    assert result["read_only"] is True
    assert result["manual_review_required"] is True
    assert result["cached"] is False


def test_archive_ocr_returns_cached_proposal_for_unchanged_image(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    root.mkdir()
    (root / "scan.png").write_bytes(b"same source")
    database = tmp_path / "catalogue.sqlite"
    sqlite3.connect(database).close()
    calls = []
    monkeypatch.setattr(archive.ocr_candidates, "propose_ocr", lambda *args, **kwargs:
                        calls.append(1) or {"status": "none", "target": None})
    first = archive.propose_archive_ocr(root, "scan.png", database)
    second = archive.propose_archive_ocr(root, "scan.png", database)
    assert first["cached"] is False
    assert second["cached"] is True
    assert len(calls) == 1


def test_archive_ocr_survives_memory_cache_and_invalidates_changed_source(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    root.mkdir()
    image = root / "scan.png"
    image.write_bytes(b"first source")
    database = tmp_path / "catalogue.sqlite"
    sqlite3.connect(database).close()
    calls = []
    monkeypatch.setattr(archive.ocr_candidates, "propose_ocr", lambda *args:
                        calls.append(1) or {"status": "none", "target": None})
    state = tmp_path / "ocr-state"
    first = archive.propose_archive_ocr(root, "scan.png", database, cache_root=state)
    archive._OCR_CACHE.clear()
    saved = archive.cached_archive_ocr(root, "scan.png", database, cache_root=state)
    second = archive.propose_archive_ocr(root, "scan.png", database, cache_root=state)
    assert saved == second
    assert second["cached"] is True
    assert len(list(state.glob("*.json"))) == 1
    image.write_bytes(b"changed source")
    assert archive.cached_archive_ocr(root, "scan.png", database, cache_root=state) is None
    third = archive.propose_archive_ocr(root, "scan.png", database, cache_root=state)
    assert third["cached"] is False
    assert third["sha256"] != first["sha256"]
    assert len(calls) == 2


def test_auto_import_requires_precise_ocr_and_empty_slot(tmp_path, monkeypatch):
    database = tmp_path / "catalogue.sqlite"
    database.touch()
    monkeypatch.setattr(archive, "resolve_current", lambda root: {"database": database})
    slot = {"set_code": "ABC", "collector_number": "12", "image_status": "missing",
            "title": "Rappel ancestral"}
    monkeypatch.setattr(archive, "describe_archive_target", lambda *args, **kwargs: slot)
    imported = []
    monkeypatch.setattr(archive, "import_manual_archive_choice", lambda *args, **kwargs:
                        imported.append(kwargs) or {"status": "imported"})
    result = {"sha256": "digest", "database": str(database), "identity_proposal": {
        "status": "unique", "target": {"provider_card_id": "card-fr", "printing_id": "print-1",
            "face_index": 0, "language": "fr", "set_code": "ABC", "collector_number": "12"},
        "evidence": {"identity_method": "printed_code_number_title", "title_crop_score": .96,
            "ocr_title": "Rappel ancestral",
            "language_proven": True, "set_codes": ["ABC"], "collector_numbers": ["12"]}}}
    kwargs = {"resolved_state_path": tmp_path / "resolved.json"}
    assert archive.auto_import_archive_ocr(tmp_path, "scan.png", tmp_path, result, **kwargs)["status"] == "imported"
    assert imported[0]["verification_method"] == "ocr_printed_code_number_title_language"
    slot["image_status"] = "ready"
    assert archive.auto_import_archive_ocr(tmp_path, "scan.png", tmp_path, result, **kwargs)["status"] == "review_required"
    slot["image_status"] = "missing"
    result["identity_proposal"]["evidence"]["language_proven"] = False
    assert archive.auto_import_archive_ocr(tmp_path, "scan.png", tmp_path, result, **kwargs)["status"] == "review_required"
    assert len(imported) == 1


@pytest.mark.parametrize("relative", ["../outside.png", "Impossible/card.png", "card.txt"])
def test_archive_ocr_rejects_unsafe_or_unsupported_path(tmp_path, relative):
    root = tmp_path / "archive"
    root.mkdir()
    with pytest.raises(ValueError):
        archive.propose_archive_ocr(root, relative, tmp_path / "catalogue.sqlite")


def _manual_import_setup(tmp_path, monkeypatch, *, slot_status="missing", duplicate=False):
    root = tmp_path / "archive"
    source = root / "folder" / "card.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"validated original")
    import hashlib
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    inventory = tmp_path / "missing-inventory.json"
    if duplicate:
        duplicate_path = root / "other" / "card.png"
        duplicate_path.parent.mkdir()
        duplicate_path.write_bytes(source.read_bytes())
    database = tmp_path / "active.sqlite"
    database.touch()
    state = tmp_path / "resolved.json"
    calls = []
    monkeypatch.setattr(archive, "resolve_current", lambda _root: {
        "version_id": "active-v1", "database": database})
    monkeypatch.setattr(archive, "describe_archive_target", lambda *args, **kwargs: {
        "image_status": slot_status, "printing_id": "print-1"})
    monkeypatch.setattr(archive, "import_local_image", lambda *args, **kwargs:
                        calls.append("import") or {"status": "ready"})
    def verify(*args):
        assert source.is_file(), "source must remain until organized image verification"
        calls.append("verify")
        return {"status": "ready", "asset_id": "asset"}
    monkeypatch.setattr(archive, "_verify_organized_image", verify)
    return root, source, digest, inventory, database, state, calls


def test_manual_import_deletes_only_after_verified_ready_image(tmp_path, monkeypatch):
    root, source, digest, inventory, database, state, calls = _manual_import_setup(tmp_path, monkeypatch)
    result = archive.import_manual_archive_choice(root, "folder/card.png", tmp_path / "catalogue",
        sha256=digest, provider_card_id="card-fr", printing_id="print-1", face_index=0,
        language="fr", inventory_path=inventory, resolved_state_path=state, action="archive")
    assert calls == ["import", "verify"]
    assert result["status"] == "imported"
    assert not source.exists()
    assert json.loads(state.read_text(encoding="utf-8"))["entries"]["folder/card.png"]["status"] == "verified_removed"


def test_duplicate_archive_hash_is_preserved_without_import(tmp_path, monkeypatch):
    root, source, digest, inventory, database, state, calls = _manual_import_setup(
        tmp_path, monkeypatch, duplicate=True)
    with pytest.raises(archive.ArchiveConflictError):
        archive.import_manual_archive_choice(root, "folder/card.png", tmp_path / "catalogue",
            sha256=digest, provider_card_id="card-fr", printing_id="print-1", face_index=0,
            language="fr", inventory_path=inventory, resolved_state_path=state, action="archive")
    assert source.is_file()
    assert calls == []


def test_already_indexed_choice_verifies_and_removes_source(tmp_path, monkeypatch):
    root, source, digest, inventory, database, state, calls = _manual_import_setup(
        tmp_path, monkeypatch, slot_status="ready")
    result = archive.import_manual_archive_choice(root, "folder/card.png", tmp_path / "catalogue",
        sha256=digest, provider_card_id="card-fr", printing_id="print-1", face_index=0,
        language="fr", inventory_path=inventory, resolved_state_path=state, action="already_indexed")
    assert calls == ["verify"]
    assert result["status"] == "already_indexed"
    assert not source.exists()


def test_import_preserves_other_archive_names(tmp_path, monkeypatch):
    root, source, digest, inventory, database, state, calls = _manual_import_setup(tmp_path, monkeypatch)
    state.write_text(json.dumps({"version_id": "older", "entries": {},
        "display_names": {"folder/card.png": "Imported", "folder/next.png": "Next card"}}),
        encoding="utf-8")
    archive.import_manual_archive_choice(root, "folder/card.png", tmp_path / "catalogue",
        sha256=digest, provider_card_id="card-fr", printing_id="print-1", face_index=0,
        language="fr", inventory_path=inventory, resolved_state_path=state, action="archive")
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["display_names"] == {"folder/next.png": "Next card"}
    assert saved["entries"]["folder/card.png"]["status"] == "verified_removed"
    assert saved["previous_version_ids"] == ["older"]


def test_parallel_archive_names_are_not_lost(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    state = tmp_path / "resolved.json"
    with ThreadPoolExecutor(max_workers=10) as workers:
        list(workers.map(lambda i: archive._record_display_name(
            state, f"card-{i}.png", f"Card {i}"), range(30)))
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["display_names"] == {f"card-{i}.png": f"Card {i}" for i in range(30)}


def test_replace_indexed_choice_reimports_before_verifying_and_removing(tmp_path, monkeypatch):
    root, source, digest, inventory, database, state, calls = _manual_import_setup(
        tmp_path, monkeypatch, slot_status="ready")
    result = archive.import_manual_archive_choice(root, "folder/card.png", tmp_path / "catalogue",
        sha256=digest, provider_card_id="card-fr", printing_id="print-1", face_index=0,
        language="fr", inventory_path=inventory, resolved_state_path=state,
        action="replace_indexed")
    assert calls == ["import", "verify"]
    assert result["status"] == "replaced_indexed"
    assert not source.exists()


def _manual_import_setup(tmp_path, monkeypatch, *, slot_status="missing", duplicate=False):
    root = tmp_path / "archive"
    source = root / "folder" / "card.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"validated original")
    import hashlib
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    inventory = tmp_path / "missing-inventory.json"
    if duplicate:
        duplicate_path = root / "other" / "card.png"
        duplicate_path.parent.mkdir()
        duplicate_path.write_bytes(source.read_bytes())
    database = tmp_path / "active.sqlite"
    database.touch()
    state = tmp_path / "resolved.json"
    calls = []
    monkeypatch.setattr(archive, "resolve_current", lambda _root: {
        "version_id": "active-v1", "database": database})
    monkeypatch.setattr(archive, "describe_archive_target", lambda *args, **kwargs: {
        "image_status": slot_status, "printing_id": "print-1"})
    monkeypatch.setattr(archive, "import_local_image", lambda *args, **kwargs:
                        calls.append("import") or {"status": "ready"})
    def verify(*args):
        assert source.is_file(), "source must remain until organized image verification"
        calls.append("verify")
        return {"status": "ready", "asset_id": "asset"}
    monkeypatch.setattr(archive, "_verify_organized_image", verify)
    return root, source, digest, inventory, database, state, calls


def test_manual_import_deletes_only_after_verified_ready_image(tmp_path, monkeypatch):
    root, source, digest, inventory, database, state, calls = _manual_import_setup(tmp_path, monkeypatch)
    result = archive.import_manual_archive_choice(root, "folder/card.png", tmp_path / "catalogue",
        sha256=digest, provider_card_id="card-fr", printing_id="print-1", face_index=0,
        language="fr", inventory_path=inventory, resolved_state_path=state, action="archive")
    assert calls == ["import", "verify"]
    assert result["status"] == "imported"
    assert not source.exists()
    assert json.loads(state.read_text(encoding="utf-8"))["entries"]["folder/card.png"]["status"] == "verified_removed"


def test_duplicate_archive_hash_is_preserved_without_import(tmp_path, monkeypatch):
    root, source, digest, inventory, database, state, calls = _manual_import_setup(
        tmp_path, monkeypatch, duplicate=True)
    with pytest.raises(archive.ArchiveConflictError):
        archive.import_manual_archive_choice(root, "folder/card.png", tmp_path / "catalogue",
            sha256=digest, provider_card_id="card-fr", printing_id="print-1", face_index=0,
            language="fr", inventory_path=inventory, resolved_state_path=state, action="archive")
    assert source.is_file()
    assert calls == []


def test_already_indexed_choice_verifies_and_removes_source(tmp_path, monkeypatch):
    root, source, digest, inventory, database, state, calls = _manual_import_setup(
        tmp_path, monkeypatch, slot_status="ready")
    result = archive.import_manual_archive_choice(root, "folder/card.png", tmp_path / "catalogue",
        sha256=digest, provider_card_id="card-fr", printing_id="print-1", face_index=0,
        language="fr", inventory_path=inventory, resolved_state_path=state, action="already_indexed")
    assert calls == ["verify"]
    assert result["status"] == "already_indexed"
    assert not source.exists()
