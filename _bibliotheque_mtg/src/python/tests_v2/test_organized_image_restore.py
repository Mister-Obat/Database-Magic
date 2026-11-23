import json
import shutil
import sqlite3

from bibliotheque_mtg import local_app
from bibliotheque_mtg.image_download import download_printing
from bibliotheque_mtg.organized_images import restore_organized_images

from .test_download_v2 import image_server, make_catalogue


def test_startup_indexes_copied_images_for_exact_edition_printing_and_face(tmp_path, monkeypatch, image_server):
    original, _, _, _ = make_catalogue(tmp_path / "original", image_server, face_count=2)
    download_printing("provider-fr", catalogue_root=original, ledger_path=original / "ledger.json",
                      test_hosts={"127.0.0.1"}, face_indexes=[0])
    target, version, db, manifest = make_catalogue(tmp_path / "recipient", image_server, face_count=2)
    shutil.copytree(original / "Images", target / "Images")
    backup = next((target / "Images").glob("*/*.webp"))
    # Matching should survive corrected edition and French display names.
    renamed_folder = backup.parent.with_name("TST - Ancien nom")
    backup.parent.rename(renamed_folder)
    backup = renamed_folder / backup.name.replace("Carte test", "Ancien titre")
    next(renamed_folder.glob("*.webp")).rename(backup)
    original_bytes = backup.read_bytes()
    (backup.parent / backup.name.replace("face 1", "face 2")).write_bytes(b"not a WebP")
    wrong_edition = target / "Images" / "XYZ - Another edition"
    wrong_edition.mkdir()
    (wrong_edition / backup.name.replace("face 1", "face 2")).write_bytes(original_bytes)

    monkeypatch.setattr(local_app, "CATALOGUE_ROOT", target)
    server = local_app.open_existing_or_start(port=0, open_browser=False)
    server.image_restore_thread.join(timeout=10)
    server.server_close()
    assert not server.image_restore_thread.is_alive()

    with sqlite3.connect(db) as conn:
        rows = conn.execute("""SELECT s.face_index,s.status,s.selected_language,a.relative_path,s.provenance_json
                               FROM image_slots s LEFT JOIN image_assets a USING(asset_id)
                               ORDER BY s.face_index""").fetchall()
    assert [(face, status) for face, status, *_ in rows] == [(0, "ready"), (1, "missing")]
    _, _, language, relative, provenance = rows[0]
    assert language == "und"
    assert json.loads(provenance)["provider"] == "organized_backup"
    assert (version / relative).read_bytes() == backup.read_bytes() == original_bytes
    assert json.loads(manifest.read_bytes())["slots"][0]["path"] == relative
    internal = version / relative
    internal.unlink()
    restore_organized_images(target)
    assert internal.read_bytes() == original_bytes
    assert list(renamed_folder.glob("*face 1*")) == [backup]
