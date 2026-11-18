from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
from pathlib import Path

from .sources import (SourceError, SourceFormatError, SourceRateLimited,
                      rebuild_from_archives, run_sources, validate_catalogue)
from .reconciliation import (InventoryError, inventory_library, reconcile_all_existing,
                             reconcile_sample)
from .pipeline import (PipelineStorageError, build_publication, create_report,
                       update_dry_run)
from .publication import FileLock, InvalidPublication, LockedPublication, PublicationError
from .maintenance import MaintenanceError
from .paths import catalogue_root
from .collection import collect_pilot
from .batch_collection import BatchPlanError, collect_batch_plan, create_batch_plan


def _emit_json(value):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    encoding = getattr(sys.stdout, "encoding", None)
    if encoding:
        try:
            text.encode(encoding)
        except (LookupError, UnicodeEncodeError):
            text = json.dumps(value, ensure_ascii=True, indent=2)
    sys.stdout.write(text + "\n")


def _control_break_as_keyboard_interrupt(_signal_number, _frame):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _control_break_as_keyboard_interrupt)
    parser = argparse.ArgumentParser(prog="bibliotheque_mtg")
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    source = commands.add_parser("sources")
    source.add_argument("--refresh", action="store_true")
    source.add_argument("--offline", action="store_true")
    source.add_argument("--metadata-only", action="store_true")
    validate = commands.add_parser("validate")
    validate.add_argument("--scope", choices=("catalogue", "pilot", "existing", "edition", "library", "last-run"), required=True)
    validate.add_argument("--check-images", action="store_true")
    validate.add_argument("--strict-completeness", action="store_true")
    audit = commands.add_parser("audit")
    audit.add_argument("--input", required=True)
    audit.add_argument("--report", required=True)
    reconcile = commands.add_parser("reconcile")
    reconcile_target = reconcile.add_mutually_exclusive_group(required=True)
    reconcile_target.add_argument("--sample")
    reconcile_target.add_argument("--all-existing", action="store_true")
    reconcile.add_argument("--report")
    reconcile.add_argument("--dry-run", action="store_true")
    publish = commands.add_parser("publish")
    publish.add_argument("--scope", choices=("pilot", "existing", "edition", "library"), required=True)
    publish.add_argument("--set-id", action="append", default=[])
    publish.add_argument("--strict-completeness", action="store_true")
    deploy = commands.add_parser("deploy")
    deploy.add_argument("--scope", choices=("pilot", "existing", "edition", "library"), required=True)
    deploy.add_argument("--generation-id")
    report = commands.add_parser("report")
    report.add_argument("--scope", choices=("pilot", "existing", "edition", "library", "last-run"), default="library")
    report.add_argument("--format", choices=("json", "md"), default="json")
    report.add_argument("--output")
    commands.add_parser("init", help="construire le premier catalogue de métadonnées")
    activate = commands.add_parser("activate", help="activer une version restaurée, serveur arrêté")
    activate.add_argument("--version", required=True)
    update = commands.add_parser("update")
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--non-interactive", action="store_true")
    update.add_argument("--strict-completeness", action="store_true")
    update.add_argument("--upgrade-fr", action="store_true")
    update.add_argument("--upgrade-quality", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--destination-root", required=True)
    collect = commands.add_parser("collect")
    collect_target = collect.add_mutually_exclusive_group(required=True)
    collect_target.add_argument("--selection")
    collect_target.add_argument("--pending-existing", action="store_true")
    collect_target.add_argument("--missing-sets", action="store_true")
    collect.add_argument("--plan")
    collect.add_argument("--set-id", action="append", default=[])
    collect.add_argument("--max-sets", type=int, default=1)
    collect.add_argument("--max-faces", type=int)
    collect.add_argument("--max-image-downloads", type=int)
    collect.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        config_path = Path(args.config)
        if args.command == "sources":
            if not args.metadata_only:
                raise SourceFormatError("Step 3 requires --metadata-only")
            if args.refresh and args.offline:
                raise SourceFormatError("Choose either --refresh or --offline")
            if args.offline or not args.refresh:
                result = rebuild_from_archives(config_path)
            else:
                result = run_sources(config_path, refresh=True, metadata_only=True)
        elif args.command == "validate":
            ops_root = config_path.resolve().parent
            if args.scope == "catalogue":
                result = validate_catalogue(ops_root)
            else:
                settings_path = config_path.resolve()
                from .pipeline import load_valid_config
                settings, ops_root = load_valid_config(settings_path)
                scope = "library" if args.scope == "last-run" else args.scope
                publisher = _publisher_for_scope(settings, ops_root, scope)
                result = publisher.validate_all(strict_completeness=args.strict_completeness,
                                                check_images=args.check_images)
                if result["status"] == "incomplete":
                    _emit_json(result)
                    return 3
        elif args.command == "publish":
            publisher, catalogue, documents, artifacts, schemas = build_publication(
                config_path, args.scope, args.set_id)
            generation_id = catalogue["generation_id"]
            result = publisher.publish(documents, artifacts, schemas, scope=args.scope,
                                      generation_id=generation_id)
            result["editions_indexed"] = catalogue["completeness"]["editions_indexed"]
            result["faces_expected"] = catalogue["completeness"]["faces_expected"]
            result["faces_with_valid_images"] = catalogue["completeness"]["faces_with_valid_images"]
            result["technical_valid"] = True
            result["collection_complete"] = (catalogue["completeness"]["metadata_state"] == "complete"
                                              and catalogue["completeness"]["image_state"] == "complete")
            if args.strict_completeness and not result["collection_complete"]:
                result["status"] = "incomplete"
                _emit_json(result)
                return 3
        elif args.command == "deploy":
            from .deployment import deploy_generation
            from .pipeline import load_valid_config
            settings, ops_root = load_valid_config(config_path)
            publisher = _publisher_for_scope(settings, ops_root, args.scope)
            result = deploy_generation(publisher, settings["paths"]["library_root"],
                                       ops_root, generation_id=args.generation_id)
        elif args.command == "report":
            scope = "library" if args.scope == "last-run" else args.scope
            result, report_path = create_report(config_path, scope, args.output, args.format == "md")
            result["report_path"] = str(report_path)
        elif args.command == "init":
            from .maintenance import initialize_v2

            milestones = {}
            def report(stage, fraction, detail):
                percent = int(fraction * 100)
                if stage not in milestones or percent == 100 or percent >= milestones[stage] + 5:
                    milestones[stage] = percent
                    print(f"{stage} {percent:3d} % — {detail}", file=sys.stderr, flush=True)

            result = initialize_v2(config_path, progress=report)
        elif args.command == "activate":
            from .version_history import activate_version
            with FileLock(config_path.resolve().parent / "locks" / "catalogue-init.lock"):
                result = activate_version(catalogue_root(config_path.resolve().parent), args.version)
        elif args.command == "update":
            if args.upgrade_fr or args.upgrade_quality:
                raise SourceError("Les remplacements d’images restent des actions explicites depuis la fiche d’une carte")
            from .maintenance import refresh_v2
            result = refresh_v2(config_path, dry_run=args.dry_run)
            if result.get("status") in {"incomplete", "error"}:
                _emit_json(result); return 3
        elif args.command == "backup":
            from .maintenance import backup_current
            result = backup_current(catalogue_root(config_path.resolve().parent), Path(args.destination))
        elif args.command == "restore":
            from .maintenance import restore_backup
            result = restore_backup(Path(args.backup), Path(args.destination_root))
        elif args.command == "collect":
            if args.selection:
                result = collect_pilot(config_path, args.selection,
                                       max_faces=20 if args.max_faces is None else args.max_faces,
                                       max_image_downloads=120 if args.max_image_downloads is None else args.max_image_downloads,
                                       dry_run=args.dry_run)
            else:
                mode = "pending_existing" if args.pending_existing else "missing_sets"
                if args.plan:
                    result = collect_batch_plan(config_path, args.plan, dry_run=args.dry_run)
                else:
                    plan_result = create_batch_plan(
                        config_path, mode,
                        max_faces=100 if args.max_faces is None else args.max_faces,
                        max_image_downloads=600 if args.max_image_downloads is None else args.max_image_downloads,
                        max_sets=args.max_sets, set_ids=args.set_id)
                    if plan_result.get("status") == "no_work":
                        result = plan_result
                    else:
                        result = collect_batch_plan(config_path, plan_result["plan_path"], dry_run=args.dry_run)
                        result["created_plan"] = plan_result
            _emit_json(result)
            if result.get("status") in {"incomplete", "interrupted", "partial"}:
                return 3
            return 0
        elif args.command == "audit":
            ops_root = config_path.resolve().parent
            result = inventory_library(Path(args.input), ops_root / "catalogue.sqlite",
                                       ops_root / "sources" / "wizards" / "gallery_catalogue.json",
                                       Path(args.report))
        elif args.command == "reconcile":
            ops_root = config_path.resolve().parent
            settings = json.loads(config_path.read_text(encoding="utf-8-sig"))
            if args.all_existing:
                library_root = Path(settings["paths"]["library_root"])
                inventory_path = ops_root / "reports" / "inventory-current.json"
                inventory_library(library_root, ops_root / "catalogue.sqlite",
                                  ops_root / "sources" / "wizards" / "gallery_catalogue.json",
                                  inventory_path)
                default_name = "reconciliation-existing-dry-run.json" if args.dry_run else "reconciliation-existing.json"
                report_path = Path(args.report) if args.report else ops_root / "reports" / default_name
                result = reconcile_all_existing(library_root,
                                                ops_root / "catalogue.sqlite",
                                                ops_root / "sources" / "wizards" / "gallery_catalogue.json",
                                                ops_root / "reports" / "edition_aliases.json",
                                                inventory_path, ops_root / "overrides.json",
                                                report_path, dry_run=args.dry_run)
            else:
                if not args.dry_run:
                    raise InventoryError("Sample reconciliation is read-only; pass --dry-run")
                report_path = Path(args.report) if args.report else ops_root / "reports" / "reconciliation.json"
                result = reconcile_sample(Path(settings["paths"]["library_root"]),
                                          ops_root / "catalogue.sqlite",
                                          ops_root / "sources" / "wizards" / "gallery_catalogue.json",
                                          ops_root / "reports" / "edition_aliases.json",
                                          Path(args.sample), ops_root / "overrides.json",
                                          report_path, dry_run=True)
        else:
            raise SourceFormatError(f"Unsupported command: {args.command}")
        _emit_json(result)
        return 0
    except SourceRateLimited as exc:
        print(f"RETRY_DEFERRED retry_after={exc.retry_after}: {exc}", file=sys.stderr)
        return 3
    except LockedPublication as exc:
        print(f"PUBLICATION_LOCKED: {exc}", file=sys.stderr)
        return 5
    except InvalidPublication as exc:
        print(f"INVALID_PUBLICATION: {exc}", file=sys.stderr)
        return 2
    except PipelineStorageError as exc:
        print(f"STORAGE_OR_CATALOGUE_ERROR: {exc}", file=sys.stderr)
        return 4
    except MaintenanceError as exc:
        print(f"MAINTENANCE_ERROR: {exc}", file=sys.stderr)
        return 4
    except PublicationError as exc:
        print(f"PUBLICATION_ERROR: {exc}", file=sys.stderr)
        return 4
    except (SourceFormatError, InventoryError) as exc:
        print(f"INVALID_DATA: {exc}", file=sys.stderr)
        return 2
    except BatchPlanError as exc:
        print(f"INVALID_BATCH_PLAN: {exc}", file=sys.stderr)
        return 2
    except SourceError as exc:
        print(f"SOURCE_OR_STORAGE_ERROR: {exc}", file=sys.stderr)
        return 4
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"STORAGE_OR_DATABASE_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4


def _publisher_for_scope(config: dict, ops_root: Path, scope: str):
    from .publication import Publisher

    if scope == "pilot":
        name = "pilot"
    elif scope in {"existing", "edition"}:
        name = scope
    else:
        name = "library"
    return Publisher(Path(config["paths"]["staging_root"]) / name,
                     ops_root / "schemas", config["paths"]["library_root"],
                     config["publication"]["keep_valid_generations"])


if __name__ == "__main__":
    raise SystemExit(main())
