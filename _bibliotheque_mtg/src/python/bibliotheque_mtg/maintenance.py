from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .paths import catalogue_root as get_catalogue_root
from .publication import FileLock
from .v2.build_catalogue import build_catalogue
from .v2.publish import atomic_publish_pointer, resolve_current


class MaintenanceError(RuntimeError):
    pass


class MaintenanceCancelled(MaintenanceError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def edition_delta(previous: dict, current: dict) -> dict:
    """Compare normalized fixture snapshots using exact stable IDs only."""
    old_editions = {x["code"]: x for x in previous.get("editions", [])}
    new_editions = {x["code"]: x for x in current.get("editions", [])}
    changed, added, removed = [], sorted(new_editions.keys() - old_editions.keys()), sorted(old_editions.keys() - new_editions.keys())
    for code in sorted(old_editions.keys() & new_editions.keys()):
        old_cards = {c["printing_id"]: c for c in old_editions[code].get("cards", [])}
        new_cards = {c["printing_id"]: c for c in new_editions[code].get("cards", [])}
        if old_cards != new_cards:
            changed.append({"code": code, "added": sorted(new_cards.keys() - old_cards.keys()),
                            "removed": sorted(old_cards.keys() - new_cards.keys()),
                            "updated": sorted(k for k in old_cards.keys() & new_cards.keys() if old_cards[k] != new_cards[k])})
    return {"added_editions": added, "removed_editions": removed, "changed_editions": changed,
            "no_change": not (added or removed or changed)}


def _copy_forward_assets(old_db: Path, new_db: Path, old_version: Path, new_version: Path, *, progress=None) -> dict:
    old = sqlite3.connect(f"file:{old_db.as_posix()}?mode=ro", uri=True); old.row_factory = sqlite3.Row
    new = sqlite3.connect(new_db); new.execute("PRAGMA foreign_keys=ON")
    copied = linked = skipped = 0
    if progress:
        progress(0.0, "Conservation des images…")
    try:
        has_dates = old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='printing_added'").fetchone()
        if has_dates:
            added = old.execute("SELECT printing_id,added_at FROM printing_added")
        else:
            # All cards in the pre-history catalogue share the migration day's baseline.
            baseline = time.strftime('%Y-%m-%dT00:00:00.000Z', time.gmtime())
            added = ((row[0], baseline) for row in old.execute('SELECT printing_id FROM printings'))
        new.executemany("UPDATE printing_added SET added_at=? WHERE printing_id=?",
                        ((date, printing_id) for printing_id, date in added))
        aliases = old.execute("SELECT legacy_id,v2_printing_id,evidence,status FROM legacy_aliases").fetchall()
        valid_printings = {r[0] for r in new.execute("SELECT printing_id FROM printings")}
        for alias in aliases:
            if alias[1] in valid_printings:
                new.execute("INSERT OR IGNORE INTO legacy_aliases VALUES(?,?,?,?)", tuple(alias))
        rows = old.execute("SELECT s.printing_id,s.face_index,s.selected_language,s.asset_id,s.provenance_json,"
                           "a.relative_path,a.mime_type,a.size_bytes,a.width,a.height,a.language,a.provenance_json "
                           "FROM image_slots s JOIN image_assets a USING(asset_id) WHERE s.status='ready'").fetchall()
        new_slots = {(r[0], r[1]) for r in new.execute("SELECT printing_id,face_index FROM image_slots")}
        total = len(rows)
        for index, row in enumerate(rows, 1):
            key = (row["printing_id"], row["face_index"])
            source = (old_version / row["relative_path"]).resolve()
            if key not in new_slots or not source.is_file() or not source.is_relative_to(old_version.resolve()):
                skipped += 1; continue
            expected_digest = row["asset_id"]
            legacy_prefix = f"asset_{row['language']}_"
            # Earlier V2 image assets used `asset_<lang>_<sha256>` identifiers;
            # newly downloaded assets use the bare content hash. Accept both,
            # while still checking the actual file bytes against that hash.
            if expected_digest.startswith(legacy_prefix):
                expected_digest = expected_digest[len(legacy_prefix):]
            if sha256_file(source) != expected_digest:
                skipped += 1; continue
            try:
                with Image.open(source) as image:
                    if image.format != "WEBP": skipped += 1; continue
                    image.verify()
            except (UnidentifiedImageError, OSError, ValueError):
                skipped += 1; continue
            target = (new_version / row["relative_path"]).resolve()
            if not target.is_relative_to(new_version.resolve()):
                skipped += 1; continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                try:
                    os.link(source, target); linked += 1
                except OSError:
                    shutil.copy2(source, target); copied += 1
            new.execute("INSERT OR IGNORE INTO image_assets VALUES(?,?,?,?,?,?,?,?)",
                        (row["asset_id"], row["relative_path"], row["mime_type"], row["size_bytes"],
                         row["width"], row["height"], row["language"], row["provenance_json"]))
            new.execute("UPDATE image_slots SET selected_language=?,status='ready',asset_id=?,provenance_json=? WHERE printing_id=? AND face_index=?",
                        (row["selected_language"], row["asset_id"], row["provenance_json"], *key))
            if progress and (index % 50 == 0 or index == total):
                progress(index / total, f"{index} / {total} images examinées")
        new.commit()
    except Exception:
        new.rollback(); raise
    finally:
        old.close(); new.close()
    if progress:
        progress(1.0, "Images conservées")
    return {"hardlinked_assets": linked, "copied_assets": copied, "unmatched_or_invalid_assets": skipped}


def _refresh_manifests(db: Path, version: Path, *, progress=None, edition_ids: set[str] | None = None) -> int:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True); conn.row_factory = sqlite3.Row
    root = version / "editions"; index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    editions = [item for item in index.get("editions", [])
                if edition_ids is None or item["id"] in edition_ids]
    total = len(editions)
    touched = 0
    try:
        for position, edition in enumerate(editions, 1):
            folder = edition["folder"]
            path = (root / folder / "images.json").resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file(): raise MaintenanceError("édition sans images.json")
            doc = json.loads(path.read_text(encoding="utf-8"))
            rows = conn.execute("SELECT s.printing_id,s.face_index,s.preferred_language,s.selected_language,s.status,a.relative_path "
                                "FROM image_slots s JOIN printings p USING(printing_id) LEFT JOIN image_assets a USING(asset_id) "
                                "WHERE p.set_id=? ORDER BY s.printing_id,s.face_index", (edition["id"],)).fetchall()
            by_key = {(r["printing_id"], r["face_index"]): r for r in rows}
            slots = []
            for item in doc.get("slots", []):
                row = by_key.get((item["printing_id"], item["face_index"]))
                if row:
                    item.update({"preferred_language": row["preferred_language"], "selected_language": row["selected_language"],
                                 "status": row["status"], "path": row["relative_path"]})
                slots.append(item)
            doc["slots"] = slots
            payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            if path.read_text(encoding="utf-8") != payload:
                temp = path.with_suffix(".json.tmp"); temp.write_text(payload, encoding="utf-8"); os.replace(temp, path); touched += 1
            if progress and (position % 25 == 0 or position == total):
                progress(position / total, f"{position} / {total} manifestes")
    finally:
        conn.close()
    if progress:
        progress(1.0, "Manifestes à jour")
    return touched


def _link_unchanged_exports(old_root: Path, new_root: Path, *, progress=None) -> dict:
    shared = changed = 0
    files = [new_file for new_file in new_root.rglob("*") if new_file.is_file()]
    total = len(files)
    for index, new_file in enumerate(files, 1):
        relative = new_file.relative_to(new_root); old_file = old_root / relative
        if old_file.is_file() and sha256_file(old_file) == sha256_file(new_file):
            new_file.unlink()
            try: os.link(old_file, new_file)
            except OSError: shutil.copy2(old_file, new_file)
            shared += 1
        else:
            changed += 1
        if progress and (index % 100 == 0 or index == total):
            progress(index / total, f"{index} / {total} fichiers examinés")
    if progress:
        progress(1.0, "Exports réutilisés")
    return {"unchanged_files_reused": shared, "changed_files": changed}


def initialize_v2(config_path: Path, *, progress=None) -> dict:
    """Build and publish the first metadata-only catalogue, without replacing one."""
    from .sources import run_sources
    from .version_history import _edition_index, _quick_check

    config_path = Path(config_path).resolve()
    ops = config_path.parent
    with FileLock(ops / "locks" / "catalogue-init.lock"):
        root = get_catalogue_root(ops)
        pointer = root / "current.json"
        if pointer.exists() or pointer.is_symlink():
            current = resolve_current(root)
            if current["editions"] != current["database"].parent / "editions":
                raise MaintenanceError("catalogue pointer targets inconsistent version paths")
            _quick_check(current["database"])
            _edition_index(current["database"].parent)
            return {"status": "already_initialized", "version_id": current["version_id"],
                    "image_downloads": 0, "network_calls": 0}

        source_progress = (lambda fraction, detail: progress("Sources", fraction, detail)) if progress else None
        source_report = run_sources(config_path, refresh=True, metadata_only=True, progress=source_progress)
        source = source_report["sources"]["scryfall_all_cards"]
        versions = root / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        temp_version = versions / f".init-{uuid.uuid4().hex}"
        temp_version.mkdir()
        final = None
        published = False
        installed = False
        try:
            build_progress = (lambda fraction, detail: progress("Catalogue", fraction, detail)) if progress else None
            build_catalogue(ops / "catalogue.sqlite", Path(source["archive_path"]),
                            temp_version / "catalogue.sqlite",
                            archive_sha256=source["archive_sha256"],
                            archive_as_of=str(source.get("updated_at") or source_report.get("generated_at_utc")),
                            edition_root=temp_version / "editions", progress=build_progress,
                            total_records=int(source.get("rows") or 0))
            _quick_check(temp_version / "catalogue.sqlite")
            _edition_index(temp_version)
            version_id = f"v2-{sha256_file(temp_version / 'catalogue.sqlite')[:12]}"
            final = versions / version_id
            if final.exists() or final.is_symlink():
                raise MaintenanceError(f"snapshot version already exists: {version_id}")
            os.rename(temp_version, final)
            installed = True
            def ensure_no_pointer():
                if pointer.exists() or pointer.is_symlink():
                    raise MaintenanceError("catalogue pointer appeared during initialization")
            atomic_publish_pointer(root, version_id, f"versions/{version_id}/catalogue.sqlite",
                                   f"versions/{version_id}/editions", pre_replace_hook=ensure_no_pointer)
            published = True
            return {"status": "initialized", "version_id": version_id,
                    "source_archive_sha256": source["archive_sha256"],
                    "network_calls": source_report.get("network_calls", {}), "image_downloads": 0}
        finally:
            shutil.rmtree(temp_version, ignore_errors=True)
            if installed and not published:
                shutil.rmtree(final, ignore_errors=True)


def refresh_v2(config_path: Path, *, dry_run: bool = False, progress=None, additions_only: bool = False) -> dict:
    config_path = Path(config_path).resolve(); ops = config_path.parent
    catalogue_root = get_catalogue_root(ops)
    current = resolve_current(catalogue_root); old_version = current["database"].parent
    old = sqlite3.connect(f"file:{current['database'].as_posix()}?mode=ro", uri=True)
    try: old_meta = dict(old.execute("SELECT key,value FROM catalogue_meta"))
    finally: old.close()
    old_sources_path = ops / "reports" / "sources.json"
    try: old_sources = json.loads(old_sources_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError): old_sources = {}
    if dry_run:
        scope_path = ops / "reports" / "catalogue_scope.json"
        scope = json.loads(scope_path.read_text(encoding="utf-8")) if scope_path.is_file() else {}
        return {"status": "preview", "network_calls": 0, "current_version": current["version_id"],
                "current_archive_sha256": old_meta.get("source_archive_sha256"),
                "cached_source_as_of": scope.get("archive_as_of"), "image_downloads": 0,
                "changes_published": 0, "next_action": "lancer le .bat sans --dry-run pour actualiser uniquement les métadonnées"}

    from .sources import run_sources
    from .v2.publish import atomic_publish_pointer
    from .v2.build_catalogue import _sha256

    def report(step: str, fraction: float, detail: str = "", *, cancellable: bool = True) -> None:
        if progress is not None:
            if cancellable:
                progress(step, fraction, detail)
            else:
                try:
                    progress(step, fraction, detail)
                except MaintenanceCancelled:
                    pass

    if additions_only:
        from .sources import run_additions_only
        from .v2.restrictions import refresh_restrictions
        source_report = run_additions_only(
            config_path, current["database"],
            progress=(lambda fraction, detail="": report("sources", fraction * 0.8, detail)) if progress else None)
        restrictions = refresh_restrictions(
            config_path, catalogue_root, old_meta.get("source_archive_sha256", ""),
            progress=(lambda fraction, detail="": report("sources", 0.8 + fraction * 0.2, detail)) if progress else None)
        network_calls = dict(source_report.get("network_calls", {}))
        for host, calls in restrictions["network_calls"].items():
            network_calls[host] = network_calls.get(host, 0) + calls
        if source_report["status"] == "no_change":
            return {"status": "no_change", "network_calls": network_calls, "card_restrictions": restrictions,
                    "image_downloads": 0, "changes_published": 0,
                    "current_version": current["version_id"],
                    "official_french_text_coverage": source_report.get("official_french_text_coverage")}
        from .v2.incremental import build_incremental_version
        versions = catalogue_root / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        temp_version = versions / f".update-{uuid.uuid4().hex}"
        started = time.perf_counter()
        try:
            built = build_incremental_version(
                config_path, current["database"], old_version, temp_version, source_report,
                old_meta.get("source_archive_sha256") or "",
                progress=(lambda step, fraction, detail="": report(step, fraction, detail)) if progress else None)
            report("publish", 0.1, "Publication de la version ciblée…")
            digest = _sha256(temp_version / "catalogue.sqlite")
            version_id = f"v2-{digest[:12]}"
            final = versions / version_id
            if final.exists():
                raise MaintenanceError(f"snapshot version already exists: {version_id}")
            os.replace(temp_version, final)
            report("publish", 0.5, "Bascule du pointeur courant…", cancellable=False)
            atomic_publish_pointer(catalogue_root, version_id,
                                   f"versions/{version_id}/catalogue.sqlite",
                                   f"versions/{version_id}/editions")
            report("publish", 1.0, f"Version {version_id} publiée", cancellable=False)
            from .version_history import prune_old_versions
            try:
                pruned_versions = prune_old_versions(catalogue_root)
                prune_error = None
            except OSError as exc:
                pruned_versions, prune_error = [], str(exc)
            from .sources import atomic_json
            public_report = {key: value for key, value in source_report.items() if key != "set_items"}
            report_error = None
            try:
                atomic_json(ops / "reports" / "additions-update.json", public_report)
            except OSError as exc:
                report_error = str(exc)
            return {"status": "updated", "version_id": version_id,
                    "network_calls": network_calls, "card_restrictions": restrictions, "image_downloads": 0,
                    "official_french_text_coverage": source_report.get("official_french_text_coverage"),
                    "build_seconds": round(time.perf_counter() - started, 2),
                    "report_error": report_error, "pruned_versions": pruned_versions,
                    "prune_error": prune_error, **built}
        except Exception:
            shutil.rmtree(temp_version, ignore_errors=True)
            raise

    source_report = run_sources(
        config_path, refresh=True, metadata_only=True,
        progress=(lambda fraction, detail="": report("sources", fraction, detail)) if progress else None)
    source = source_report["sources"]["scryfall_all_cards"]
    sets_sha = source_report["sources"]["scryfall_sets"].get("sha256")
    old_sets_sha = (old_sources.get("sources", {}).get("scryfall_sets", {}) or {}).get("sha256")
    sets_source = ops / "catalogue.sqlite"
    archive = Path(source["archive_path"])
    new_archive_sha = source["archive_sha256"]
    if new_archive_sha == old_meta.get("source_archive_sha256") and sets_sha and sets_sha == old_sets_sha:
        return {"status": "no_change", "network_calls": source_report.get("network_calls", {}),
                "image_downloads": 0, "changes_published": 0, "current_version": current["version_id"],
                "source_archive_sha256": new_archive_sha, "source_as_of": source.get("updated_at"),
                "official_french_text_coverage": source_report.get("import", {}).get(
                    "official_french_text_coverage")}

    versions = catalogue_root / "versions"; versions.mkdir(parents=True, exist_ok=True)
    temp_version = versions / f".update-{uuid.uuid4().hex}"
    temp_version.mkdir()
    started = time.perf_counter()
    try:
        build = build_catalogue(sets_source, archive, temp_version / "catalogue.sqlite",
                                archive_sha256=new_archive_sha,
                                archive_as_of=str(source.get("updated_at") or source_report.get("generated_at_utc")),
                                edition_root=temp_version / "editions",
                                progress=(lambda fraction, detail="": report("catalogue", fraction, detail)) if progress else None,
                                total_records=int(source.get("rows") or 0))
        carried = _copy_forward_assets(
            current["database"], temp_version / "catalogue.sqlite", old_version, temp_version,
            progress=(lambda fraction, detail="": report("images", fraction, detail)) if progress else None)
        manifests = _refresh_manifests(
            temp_version / "catalogue.sqlite", temp_version,
            progress=(lambda fraction, detail="": report("manifests", fraction, detail)) if progress else None)
        file_delta = _link_unchanged_exports(
            old_version / "editions", temp_version / "editions",
            progress=(lambda fraction, detail="": report("exports", fraction, detail)) if progress else None)
        report("publish", 0.1, "Publication de la nouvelle version…")
        digest = _sha256(temp_version / "catalogue.sqlite")
        version_id = f"v2-{digest[:12]}"
        final = versions / version_id
        if final.exists(): raise MaintenanceError(f"snapshot version already exists: {version_id}")
        os.replace(temp_version, final)
        report("publish", 0.5, "Bascule du pointeur courant…", cancellable=False)
        atomic_publish_pointer(catalogue_root, version_id,
                               f"versions/{version_id}/catalogue.sqlite", f"versions/{version_id}/editions")
        report("publish", 1.0, f"Version {version_id} publiée", cancellable=False)
        from .version_history import prune_old_versions
        try:
            pruned_versions = prune_old_versions(catalogue_root)
            prune_error = None
        except OSError as exc:
            pruned_versions, prune_error = [], str(exc)
        return {"status": "updated", "version_id": version_id, "source_archive_sha256": new_archive_sha,
                "network_calls": source_report.get("network_calls", {}), "image_downloads": 0,
                "official_french_text_coverage": source_report.get("import", {}).get(
                    "official_french_text_coverage"),
                "carried_images": carried, "image_manifests_changed": manifests, **file_delta,
                "pruned_versions": pruned_versions, "prune_error": prune_error,
                "build_sha256_before_image_carry": build.get("target_sha256"),
                "build_seconds": round(time.perf_counter()-started, 2)}
    except Exception:
        shutil.rmtree(temp_version, ignore_errors=True)
        raise


def backup_current(catalogue_root: Path, destination: Path) -> dict:
    current = resolve_current(catalogue_root); source = current["database"].parent
    destination = Path(destination).resolve()
    if destination.exists(): raise MaintenanceError("backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    def omit_images_and_database(folder: str, names: list[str]) -> set[str]:
        return {name for name in names if Path(folder) == source and name in
                {"assets", "catalogue.sqlite", "catalogue.sqlite-wal", "catalogue.sqlite-shm"}}

    try:
        shutil.copytree(source, destination, ignore=omit_images_and_database)
        # SQLite's backup API includes committed WAL pages in a consistent copy.
        with sqlite3.connect(f"file:{(source / 'catalogue.sqlite').as_posix()}?mode=ro", uri=True) as active:
            with sqlite3.connect(destination / "catalogue.sqlite") as snapshot:
                active.backup(snapshot)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    files = {}
    for file in destination.rglob("*"):
        if file.is_file(): files[file.relative_to(destination).as_posix()] = {"size":file.stat().st_size,"sha256":sha256_file(file)}
    manifest = {"schema_version":2,"version_id":current["version_id"],"files":files}
    (destination / "backup-manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return {"status":"valid","version_id":current["version_id"],"file_count":len(files),
            "bytes":sum(value["size"] for value in files.values()),"backup":str(destination)}


def restore_backup(backup: Path, destination_root: Path) -> dict:
    backup, destination_root = Path(backup).resolve(), Path(destination_root).resolve()
    manifest_path = backup / "backup-manifest.json"
    if not manifest_path.is_file(): raise MaintenanceError("backup manifest missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version_id = manifest.get("version_id")
    if (not isinstance(version_id, str) or version_id in {"", ".", ".."}
            or Path(version_id).name != version_id or ":" in version_id or "\\" in version_id):
        raise MaintenanceError("invalid backup version identifier")
    files = manifest["files"]
    for relative, expected in files.items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or ":" in relative or "\\" in relative:
            raise MaintenanceError(f"unsafe backup path: {relative}")
        # Legacy v1 backups included images; they may have been removed to save space.
        if path.parts[0] == "assets":
            continue
        source = (backup / relative).resolve()
        if not source.is_relative_to(backup) or not source.is_file() or source.stat().st_size != expected["size"] or sha256_file(source) != expected["sha256"]:
            raise MaintenanceError(f"backup verification failed: {relative}")
    target = destination_root / "versions" / version_id
    if target.exists(): raise MaintenanceError("restore target already exists")
    target.mkdir(parents=True)
    try:
        for relative in files:
            if Path(relative).parts[0] == "assets":
                continue
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / relative, output)
    except Exception:
        shutil.rmtree(target)
        raise
    return {"status":"restored","version_id":manifest["version_id"],"target":str(target)}
