import json
import sqlite3
from contextlib import closing

import pytest

from bibliotheque_mtg import version_history
from bibliotheque_mtg.v2.publish import atomic_publish_pointer, resolve_current


VERSION_ID = "v2-0123456789ab"


def _version(root, version_id=VERSION_ID):
    version = root / "versions" / version_id
    editions = version / "editions"
    editions.mkdir(parents=True)
    with closing(sqlite3.connect(version / "catalogue.sqlite")) as connection:
        connection.executescript("""
            CREATE TABLE catalogue_meta(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO catalogue_meta VALUES('schema_version','2.0.0');
            CREATE TABLE editions(set_id TEXT);
            CREATE TABLE printings(printing_id TEXT);
            CREATE TABLE source_records(provider_card_id TEXT);
            CREATE TABLE image_slots(printing_id TEXT);
            CREATE VIRTUAL TABLE source_search USING fts5(name);
        """)
    (editions / "index.json").write_text(json.dumps({
        "schema_version": "2.0.0",
        "editions": [{"index_json": "abc/index.json"}],
    }), encoding="utf-8")
    edition = editions / "abc"
    edition.mkdir()
    (edition / "index.json").write_text("{}\n", encoding="utf-8")
    return version


def test_list_candidates_reports_sizes_and_active_version(tmp_path):
    _version(tmp_path)
    atomic_publish_pointer(tmp_path, VERSION_ID,
                           f"versions/{VERSION_ID}/catalogue.sqlite",
                           f"versions/{VERSION_ID}/editions")

    candidates = version_history.list_version_candidates(tmp_path)

    assert len(candidates) == 1
    assert candidates[0]["version_id"] == VERSION_ID
    assert candidates[0]["active"] is True
    assert candidates[0]["eligible"] is True
    assert candidates[0]["size_bytes"] > 0
    assert candidates[0]["error"] is None


def test_activate_valid_version_switches_pointer_without_copy(tmp_path):
    _version(tmp_path)
    original_path = tmp_path / "versions" / VERSION_ID / "catalogue.sqlite"

    result = version_history.activate_version(tmp_path, VERSION_ID)

    assert result["status"] == "activated"
    assert resolve_current(tmp_path)["version_id"] == VERSION_ID
    assert original_path.is_file()


def test_activate_rejects_invalid_identifier_and_missing_target(tmp_path):
    with pytest.raises(version_history.VersionHistoryError, match="identifier"):
        version_history.activate_version(tmp_path, "../v2-0123456789ab")
    with pytest.raises(version_history.VersionHistoryError, match="not found"):
        version_history.activate_version(tmp_path, VERSION_ID)


def test_activate_rejects_bad_quick_check_without_changing_pointer(tmp_path):
    _version(tmp_path)
    (tmp_path / "current.json").write_text('{"unchanged":true}\n', encoding="utf-8")
    database = tmp_path / "versions" / VERSION_ID / "catalogue.sqlite"
    database.write_bytes(b"not a sqlite database")

    with pytest.raises(version_history.VersionHistoryError, match="quick_check"):
        version_history.activate_version(tmp_path, VERSION_ID)

    assert (tmp_path / "current.json").read_text(encoding="utf-8") == '{"unchanged":true}\n'


def test_activate_rejects_unsafe_or_missing_edition_index(tmp_path):
    version = _version(tmp_path)
    index = version / "editions" / "index.json"
    index.write_text(json.dumps({"schema_version": "2.0.0", "editions": [
        {"index_json": "../outside.json"}]}), encoding="utf-8")

    with pytest.raises(version_history.VersionHistoryError, match="unsafe"):
        version_history.activate_version(tmp_path, VERSION_ID)


def test_activation_failure_before_replace_keeps_current_pointer(tmp_path, monkeypatch):
    old_id = "v2-aaaaaaaaaaaa"
    _version(tmp_path, old_id)
    _version(tmp_path, VERSION_ID)
    atomic_publish_pointer(tmp_path, old_id,
                           f"versions/{old_id}/catalogue.sqlite",
                           f"versions/{old_id}/editions")
    before = (tmp_path / "current.json").read_bytes()

    def fail_publish(*args, **kwargs):
        raise OSError("injected before pointer replacement")

    monkeypatch.setattr(version_history, "atomic_publish_pointer", fail_publish)
    with pytest.raises(OSError, match="injected"):
        version_history.activate_version(tmp_path, VERSION_ID)

    assert (tmp_path / "current.json").read_bytes() == before
    assert resolve_current(tmp_path)["version_id"] == old_id


def test_prune_keeps_active_and_two_newest_versions(tmp_path):
    ids = [f"v2-{value:012x}" for value in range(4)]
    for version_id in ids:
        _version(tmp_path, version_id)
    legacy = tmp_path / "versions" / "v2.4-legacy"
    legacy.mkdir()
    images = tmp_path / "Images"
    images.mkdir()
    (images / "keep.webp").write_bytes(b"image")
    atomic_publish_pointer(tmp_path, ids[0],
                           f"versions/{ids[0]}/catalogue.sqlite",
                           f"versions/{ids[0]}/editions")

    removed = version_history.prune_old_versions(tmp_path)

    assert removed == [ids[1]]
    assert all((tmp_path / "versions" / version_id).is_dir() for version_id in (ids[0], ids[2], ids[3]))
    assert legacy.is_dir() and (images / "keep.webp").is_file()
