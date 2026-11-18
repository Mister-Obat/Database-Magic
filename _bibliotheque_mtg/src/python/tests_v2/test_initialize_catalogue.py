import hashlib
import json

import pytest

from bibliotheque_mtg import maintenance, sources, version_history
from bibliotheque_mtg.publication import FileLock, LockedPublication


def setup_build(monkeypatch, tmp_path):
    calls = []
    def run(config, **kwargs):
        calls.append(kwargs)
        return {"sources": {"scryfall_all_cards": {"archive_path": str(tmp_path / "archive"),
                "archive_sha256": "abc", "rows": 1}}, "network_calls": {}}
    def build(source, archive, db, **kwargs):
        db.write_bytes(b"database")
        editions = kwargs["edition_root"]
        editions.mkdir()
        (editions / "index.json").write_text('{}')
    monkeypatch.setattr(sources, "run_sources", run)
    monkeypatch.setattr(maintenance, "build_catalogue", build)
    monkeypatch.setattr(version_history, "_quick_check", lambda db: None)
    monkeypatch.setattr(version_history, "_edition_index", lambda root: None)
    return calls


def test_initialization_and_second_run(monkeypatch, tmp_path):
    calls = setup_build(monkeypatch, tmp_path)
    config = tmp_path / "config.json"
    first = maintenance.initialize_v2(config)
    second = maintenance.initialize_v2(config)
    assert first["status"] == "initialized"
    assert second["status"] == "already_initialized"
    assert first["version_id"] == second["version_id"]
    assert calls == [{"refresh": True, "metadata_only": True, "progress": None}]
    assert first["image_downloads"] == 0


def test_invalid_pointer_is_not_replaced(monkeypatch, tmp_path):
    calls = setup_build(monkeypatch, tmp_path)
    root = maintenance.get_catalogue_root(tmp_path)
    root.mkdir(parents=True)
    pointer = root / "current.json"
    pointer.write_text("invalid")
    with pytest.raises(ValueError):
        maintenance.initialize_v2(tmp_path / "config.json")
    assert pointer.read_text() == "invalid"
    assert not calls


def test_failure_and_collision_do_not_remove_existing_versions(monkeypatch, tmp_path):
    setup_build(monkeypatch, tmp_path)
    root = maintenance.get_catalogue_root(tmp_path)
    version = root / "versions" / ("v2-" + hashlib.sha256(b"database").hexdigest()[:12])
    version.mkdir(parents=True)
    (version / "sentinel").write_text("keep")
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.initialize_v2(tmp_path / "config.json")
    assert (version / "sentinel").read_text() == "keep"
    assert not (root / "current.json").exists()
    assert not list((root / "versions").glob(".init-*"))


def test_failed_pointer_publish_cleans_new_version(monkeypatch, tmp_path):
    setup_build(monkeypatch, tmp_path)
    def fail(*args, **kwargs):
        raise OSError("publish failed")
    monkeypatch.setattr(maintenance, "atomic_publish_pointer", fail)
    with pytest.raises(OSError):
        maintenance.initialize_v2(tmp_path / "config.json")
    root = maintenance.get_catalogue_root(tmp_path)
    assert not (root / "current.json").exists()
    assert not list((root / "versions").iterdir())


def test_concurrent_initialization_refused(monkeypatch, tmp_path):
    calls = setup_build(monkeypatch, tmp_path)
    with FileLock(tmp_path / "locks" / "catalogue-init.lock"):
        with pytest.raises(LockedPublication):
            maintenance.initialize_v2(tmp_path / "config.json")
    assert not calls
