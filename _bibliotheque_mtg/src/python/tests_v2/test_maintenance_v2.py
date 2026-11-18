import json
import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

from bibliotheque_mtg.maintenance import (MaintenanceError, _copy_forward_assets, backup_current, edition_delta,
                                          refresh_v2, restore_backup)
from .test_download_v2 import make_catalogue


def _nest_catalogue(ops):
    root=ops/"work"/"v3-staging"; root.mkdir(parents=True)
    shutil.move(str(ops/"versions"),str(root/"versions")); shutil.move(str(ops/"current.json"),str(root/"current.json"))
    return root


def test_delta_reports_only_exact_edition_and_printing_changes():
    old = {"editions":[{"code":"a","cards":[{"printing_id":"p1","text":"old"}]}]}
    new = {"editions":[{"code":"a","cards":[{"printing_id":"p1","text":"new"},{"printing_id":"p2"}]},
                         {"code":"b","cards":[]}]}
    delta = edition_delta(old, new)
    assert delta == {"added_editions":["b"],"removed_editions":[],"changed_editions":[{"code":"a","added":["p2"],"removed":[],"updated":["p1"]}],"no_change":False}
    assert edition_delta(new, json.loads(json.dumps(new)))["no_change"] is True


def _seed_source_metadata(ops, db, archive_sha="archive-old", sets_sha="sets-old"):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO catalogue_meta VALUES(?,?)", [("source_archive_sha256",archive_sha),("source_database_sha256","db-old")])
    conn.commit(); conn.close()
    (ops / "reports").mkdir(exist_ok=True)
    (ops / "reports" / "sources.json").write_text(json.dumps({"sources":{"scryfall_sets":{"sha256":sets_sha}}}),encoding="utf-8")


def test_update_preview_and_no_change_do_not_rebuild_or_download_images(tmp_path, monkeypatch):
    ops, _, db, _ = make_catalogue(tmp_path, "http://localhost")
    _seed_source_metadata(ops,db)
    catalogue_root=_nest_catalogue(ops); db=catalogue_root/"versions"/"demo"/"catalogue.sqlite"
    config=ops/"config.json"; config.write_text("{}",encoding="utf-8")
    pointer=(catalogue_root/"current.json").read_bytes()
    preview=refresh_v2(config,dry_run=True)
    assert preview["status"]=="preview" and preview["network_calls"]==0 and preview["image_downloads"]==0
    sources=types.SimpleNamespace(run_sources=lambda *a,**k:{"sources":{"scryfall_all_cards":{"archive_path":str(ops/"archive.gz"),"archive_sha256":"archive-old","updated_at":"2026-10-20"},"scryfall_sets":{"sha256":"sets-old"}},"network_calls":{"descriptor":1,"sets":1}})
    monkeypatch.setitem(sys.modules,"bibliotheque_mtg.sources",sources)
    import bibliotheque_mtg.maintenance as maintenance
    monkeypatch.setattr(maintenance,"build_catalogue",lambda *a,**k:pytest.fail("no-op update rebuilt the catalogue"))
    result=refresh_v2(config)
    assert result["status"]=="no_change" and result["image_downloads"]==0 and result["changes_published"]==0
    assert (catalogue_root/"current.json").read_bytes()==pointer


def test_failed_changed_snapshot_keeps_previous_pointer_and_version_readable(tmp_path, monkeypatch):
    ops,_,db,_=make_catalogue(tmp_path,"http://localhost")
    _seed_source_metadata(ops,db)
    catalogue_root=_nest_catalogue(ops); db=catalogue_root/"versions"/"demo"/"catalogue.sqlite"
    config=ops/"config.json"; config.write_text("{}",encoding="utf-8")
    pointer=(catalogue_root/"current.json").read_bytes()
    sources=types.SimpleNamespace(run_sources=lambda *a,**k:{"sources":{"scryfall_all_cards":{"archive_path":str(ops/"archive.gz"),"archive_sha256":"archive-new","updated_at":"2026-10-21"},"scryfall_sets":{"sha256":"sets-new"}},"network_calls":{}})
    monkeypatch.setitem(sys.modules,"bibliotheque_mtg.sources",sources)
    import bibliotheque_mtg.maintenance as maintenance
    def fail(*a,**k): raise RuntimeError("injected build failure")
    monkeypatch.setattr(maintenance,"build_catalogue",fail)
    with pytest.raises(RuntimeError): refresh_v2(config)
    assert (catalogue_root/"current.json").read_bytes()==pointer
    assert db.is_file() and sqlite3.connect(db).execute("SELECT COUNT(*) FROM printings").fetchone()[0]==1


def test_copy_forward_preserves_legacy_prefixed_webp_asset_ids(tmp_path):
    old_ops, old_version, old_db, old_manifest = make_catalogue(tmp_path / "old", "http://localhost")
    new_ops, new_version, new_db, _ = make_catalogue(tmp_path / "new", "http://localhost")
    relative = "assets/webp/fr/fixture.webp"
    asset = old_version / relative
    asset.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (600, 800), "#267").save(asset, format="WEBP")
    import hashlib
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    legacy_asset_id = f"asset_fr_{digest}"
    conn = sqlite3.connect(old_db)
    conn.execute("INSERT INTO image_assets VALUES(?,?,?,?,?,?,?,?)",
                 (legacy_asset_id, relative, "image/webp", asset.stat().st_size, 600, 800, "fr", "{}"))
    conn.execute("UPDATE image_slots SET selected_language='fr',status='ready',asset_id=?", (legacy_asset_id,))
    conn.commit(); conn.close()
    old_doc = json.loads(old_manifest.read_text(encoding="utf-8"))
    old_doc["slots"][0].update({"selected_language":"fr","status":"ready","path":relative})
    old_manifest.write_text(json.dumps(old_doc), encoding="utf-8")

    result = _copy_forward_assets(old_db, new_db, old_version, new_version)
    assert result == {"hardlinked_assets":1,"copied_assets":0,"unmatched_or_invalid_assets":0}
    conn = sqlite3.connect(new_db)
    assert conn.execute("SELECT status,selected_language,asset_id FROM image_slots").fetchone() == ("ready","fr",legacy_asset_id)
    conn.close()
    assert (new_version / relative).read_bytes() == asset.read_bytes()


def test_copy_forward_keeps_first_added_date(tmp_path):
    _, old_version, old_db, _ = make_catalogue(tmp_path / "old", "http://localhost")
    _, new_version, new_db, _ = make_catalogue(tmp_path / "new", "http://localhost")
    with sqlite3.connect(old_db) as conn:
        conn.execute("INSERT INTO printing_added VALUES('printing-demo','2026-10-20T12:00:00Z')")
    with sqlite3.connect(new_db) as conn:
        conn.execute("INSERT INTO printing_added VALUES('printing-demo','2026-11-17T12:00:00Z')")
    _copy_forward_assets(old_db, new_db, old_version, new_version)
    with sqlite3.connect(new_db) as conn:
        assert conn.execute("SELECT added_at FROM printing_added WHERE printing_id='printing-demo'").fetchone()[0] == '2026-10-20T12:00:00Z'


def test_incremental_merge_preserves_existing_added_date(tmp_path):
    from bibliotheque_mtg.v2.incremental import _merge_edition_rows
    _, _, old_db, _ = make_catalogue(tmp_path / "old", "http://localhost")
    _, _, delta_db, _ = make_catalogue(tmp_path / "delta", "http://localhost")
    with sqlite3.connect(old_db) as conn:
        conn.execute("INSERT INTO printing_added VALUES('printing-demo','2026-10-20T12:00:00Z')")
    with sqlite3.connect(delta_db) as conn:
        conn.execute("INSERT INTO printing_added VALUES('printing-demo','2026-11-17T12:00:00Z')")
    _merge_edition_rows(old_db, delta_db, ['set-id'], 'sha', '2026-11-17T12:00:00Z')
    with sqlite3.connect(old_db) as conn:
        assert conn.execute("SELECT added_at FROM printing_added WHERE printing_id='printing-demo'").fetchone()[0] == '2026-10-20T12:00:00Z'


def test_changed_update_publishes_new_edition_without_image_gets(tmp_path, monkeypatch):
    ops, old_version, old_db, _ = make_catalogue(tmp_path, "http://localhost")
    _seed_source_metadata(ops, old_db, archive_sha="archive-before", sets_sha="sets-before")
    catalogue_root = _nest_catalogue(ops)
    old_version = catalogue_root / "versions" / "demo"
    old_db = old_version / "catalogue.sqlite"
    config = ops / "config.json"
    config.write_text("{}", encoding="utf-8")
    previous_pointer = (catalogue_root / "current.json").read_bytes()
    sources = types.SimpleNamespace(run_sources=lambda *a, **k: {
        "sources": {
            "scryfall_all_cards": {"archive_path": str(ops / "archive-new.gz"), "archive_sha256": "archive-after", "updated_at": "2026-10-21"},
            "scryfall_sets": {"sha256": "sets-after"},
        },
        "network_calls": {"api.scryfall.com": 2},
    })
    monkeypatch.setitem(sys.modules, "bibliotheque_mtg.sources", sources)
    import bibliotheque_mtg.maintenance as maintenance

    def build_with_new_edition(_sets, _archive, target_db, *, archive_sha256, archive_as_of, edition_root,
                               progress=None, total_records=0):
        shutil.copy2(old_db, target_db)
        conn = sqlite3.connect(target_db)
        conn.execute("UPDATE catalogue_meta SET value=? WHERE key='source_archive_sha256'", (archive_sha256,))
        conn.execute("INSERT INTO editions VALUES(?,?,?,?,?,?,?,?,?)",
                     ("set-id-2", "new", "New Edition", "core", "2026-10-21", 0, 1, "eligible", None))
        conn.commit(); conn.close()
        shutil.copytree(old_version / "editions", edition_root)
        root = json.loads((edition_root / "index.json").read_text(encoding="utf-8"))
        root["editions"][0]["folder"] = "set-demo"
        root["editions"].append({"id":"set-id-2", "code":"new", "folder":"new-edition"})
        (edition_root / "index.json").write_text(json.dumps(root), encoding="utf-8")
        new_edition = edition_root / "new-edition"
        new_edition.mkdir()
        (new_edition / "index.json").write_text(json.dumps({"edition":{"id":"set-id-2"},"cards":[]}), encoding="utf-8")
        (new_edition / "index.md").write_text("# New Edition\n", encoding="utf-8")
        (new_edition / "images.json").write_text(json.dumps({"edition_id":"set-id-2","slots":[]}), encoding="utf-8")
        return {"target_sha256":"fixture-build"}

    monkeypatch.setattr(maintenance, "build_catalogue", build_with_new_edition)
    result = refresh_v2(config)
    pointer = json.loads((catalogue_root / "current.json").read_text(encoding="utf-8"))
    assert result["status"] == "updated" and result["image_downloads"] == 0
    assert pointer["version_id"] == result["version_id"] != "demo"
    assert (catalogue_root / "current.json").read_bytes() != previous_pointer
    assert (old_db).is_file() and (old_version / "editions" / "index.json").is_file()
    published_index = json.loads((catalogue_root / pointer["editions"] / "index.json").read_text(encoding="utf-8"))
    assert {edition["id"] for edition in published_index["editions"]} == {"set-id", "set-id-2"}
    assert (catalogue_root / pointer["editions"] / "new-edition" / "images.json").is_file()


def test_backup_restore_verifies_hashes_and_keeps_sources_separate(tmp_path):
    ops,version,db,manifest=make_catalogue(tmp_path/"origin","http://localhost"); catalogue_root=_nest_catalogue(ops)
    version=catalogue_root/"versions"/"demo"; db=version/"catalogue.sqlite"; manifest=version/"editions"/"set-demo"/"images.json"
    backup=tmp_path/"backups"/"demo"
    asset=version/"assets"/"webp"/"fr"/"sample.webp"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image fixture")
    result=backup_current(catalogue_root,backup)
    assert result["status"]=="valid" and result["file_count"]>=3
    assert not (backup/"assets").exists()
    assert "assets/webp/fr/sample.webp" not in json.loads((backup/"backup-manifest.json").read_text(encoding="utf-8"))["files"]
    destination=tmp_path/"restored"
    restored=restore_backup(backup,destination)
    target=Path(restored["target"])
    assert restored["status"]=="restored" and (target/"catalogue.sqlite").is_file()
    assert sqlite3.connect(target/"catalogue.sqlite").execute("SELECT COUNT(*) FROM printings").fetchone()[0]==1
    assert (target/"editions"/"set-demo"/"images.json").read_bytes()==manifest.read_bytes()
    assert not (target/"assets").exists()
    with pytest.raises(MaintenanceError): restore_backup(backup,destination)
    target_manifest=backup/"editions"/"set-demo"/"images.json"
    target_manifest.write_text("tampered",encoding="utf-8")
    with pytest.raises(MaintenanceError): restore_backup(backup,tmp_path/"bad-restore")
    assert version.is_dir() and db.is_file()


def test_restore_accepts_legacy_backup_after_assets_are_removed(tmp_path):
    ops, version, db, _ = make_catalogue(tmp_path/"origin", "http://localhost")
    catalogue_root = _nest_catalogue(ops)
    backup = tmp_path/"legacy"
    result = backup_current(catalogue_root, backup)
    manifest_path = backup/"backup-manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["schema_version"] = 1
    document["files"]["assets/webp/fr/removed.webp"] = {"size": 4, "sha256": "0" * 64}
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    restored = restore_backup(backup, tmp_path/"restored")
    assert (Path(restored["target"])/"catalogue.sqlite").is_file()
    assert not (Path(restored["target"])/"assets").exists()
