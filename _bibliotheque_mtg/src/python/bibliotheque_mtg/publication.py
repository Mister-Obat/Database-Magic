from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from PIL import Image, UnidentifiedImageError

from .faces import PHYSICAL_MULTIFACE_LAYOUTS
from .indexing import check_recomputed_counts, markdown_link_targets, sha256_bytes, validate_json_document


class PublicationError(RuntimeError):
    pass


class InvalidPublication(PublicationError):
    pass


class StorageFailure(PublicationError):
    pass


class LockedPublication(PublicationError):
    pass


class InjectedFailure(PublicationError):
    """Test-only deterministic crash point."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InvalidPublication("Publication path must be a non-empty relative string")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise InvalidPublication(f"Absolute path rejected: {value!r}")
    components = normalized.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in components):
        raise InvalidPublication(f"Path traversal or Windows stream rejected: {value!r}")
    path = PurePosixPath(normalized)
    for part in path.parts:
        if part.endswith((".", " ")):
            raise InvalidPublication(f"Windows trailing dot/space rejected: {value!r}")
        stem = part.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"COM[1-9]|LPT[1-9]", stem):
            raise InvalidPublication(f"Reserved Windows path component rejected: {part!r}")
    return path.as_posix()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + f".{uuid.uuid4().hex}.part")
    try:
        with temp_path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, AttributeError):
            pass
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise StorageFailure(f"Atomic write failed for {path}: {exc}") from exc


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a safe liveness probe on Windows: its signal
        # emulation can affect the target process. Query a synchronization
        # handle instead, and treat uncertain access as alive (never reap).
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        synchronize = 0x00100000
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: conservatively alive
        try:
            wait_result = kernel32.WaitForSingleObject(handle, 0)
            return wait_result != 0  # WAIT_OBJECT_0 means the process has exited.
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False


class FileLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        owner = {"pid": os.getpid(), "host": socket.gethostname(), "token": self.token,
                 "started_at_utc": utc_now()}
        content = (json.dumps(owner, sort_keys=True) + "\n").encode("utf-8")
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content); stream.flush(); os.fsync(stream.fileno())
                self.acquired = True
                return
            except FileExistsError:
                try:
                    observed_bytes = self.path.read_bytes()
                    observed = json.loads(observed_bytes.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    raise LockedPublication(f"Unverifiable lock file; refusing to remove it: {self.path}")
                same_host = observed.get("host") == socket.gethostname()
                try:
                    pid = int(observed.get("pid", 0))
                except (TypeError, ValueError):
                    pid = 0
                if not same_host or _process_alive(pid):
                    raise LockedPublication(f"Publication lock is held by pid {pid} on {observed.get('host')}")
                try:
                    if self.path.read_bytes() != observed_bytes:
                        raise LockedPublication("Lock owner changed while checking a stale lock")
                    self.path.unlink()
                except FileNotFoundError:
                    continue
        raise LockedPublication(f"Could not acquire publication lock: {self.path}")

    def release(self):
        if not self.acquired:
            return
        try:
            owner = json.loads(self.path.read_text(encoding="utf-8"))
            if owner.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class StateStore:
    RUN_TRANSITIONS = {
        "running": {"validated", "committed", "failed", "interrupted"},
        "validated": {"committed", "failed", "interrupted"},
        "interrupted": {"running", "failed"},
        "failed": {"running"},
        "committed": set(),
    }
    TASK_TRANSITIONS = {
        "pending": {"in_progress", "failed"},
        "in_progress": {"staged", "validated", "failed", "interrupted"},
        "staged": {"validated", "failed", "interrupted"},
        "validated": {"committed", "failed"},
        "interrupted": {"in_progress", "failed"},
        "failed": {"in_progress"},
        "committed": set(),
    }

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript("""
          CREATE TABLE IF NOT EXISTS publication_runs(
            run_id TEXT PRIMARY KEY, scope TEXT NOT NULL, status TEXT NOT NULL,
            started_at_utc TEXT NOT NULL, finished_at_utc TEXT, error TEXT);
          CREATE TABLE IF NOT EXISTS publication_tasks(
            task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
            object_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            checkpoint_json TEXT NOT NULL DEFAULT '{}', error TEXT, updated_at_utc TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES publication_runs(run_id));
          CREATE INDEX IF NOT EXISTS publication_task_run_idx ON publication_tasks(run_id,status);
          CREATE TABLE IF NOT EXISTS publication_files(
            run_id TEXT NOT NULL, relative_path TEXT NOT NULL, sha256 TEXT NOT NULL,
            bytes INTEGER NOT NULL, status TEXT NOT NULL, verified_at_utc TEXT NOT NULL,
            PRIMARY KEY(run_id,relative_path));
          CREATE TABLE IF NOT EXISTS publication_generations(
            generation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, scope TEXT NOT NULL,
            status TEXT NOT NULL, relative_path TEXT NOT NULL, manifest_sha256 TEXT,
            created_at_utc TEXT NOT NULL, committed_at_utc TEXT,
            FOREIGN KEY(run_id) REFERENCES publication_runs(run_id));
          CREATE TABLE IF NOT EXISTS repair_tasks(
            task_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL, relative_path TEXT NOT NULL,
            reason TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL);
        """)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _now():
        return utc_now()

    def create_run(self, run_id: str, scope: str):
        self.conn.execute("INSERT INTO publication_runs(run_id,scope,status,started_at_utc) VALUES(?,?,?,?)",
                          (run_id, scope, "running", self._now()))
        self.conn.commit()

    def add_task(self, task_id: str, run_id: str, kind: str, object_id: str):
        self.conn.execute("INSERT INTO publication_tasks(task_id,run_id,kind,object_id,status,updated_at_utc) "
                          "VALUES(?,?,?,?,?,?)", (task_id, run_id, kind, object_id, "pending", self._now()))
        self.conn.commit()

    def transition_task(self, task_id: str, status: str, error: str | None = None, checkpoint: dict | None = None):
        row = self.conn.execute("SELECT status,attempts,checkpoint_json FROM publication_tasks WHERE task_id=?",
                                (task_id,)).fetchone()
        if not row:
            raise PublicationError(f"Unknown publication task: {task_id}")
        old = row["status"]
        if status != old and status not in self.TASK_TRANSITIONS.get(old, set()):
            raise PublicationError(f"Illegal task transition {old} -> {status} for {task_id}")
        payload = json.dumps(checkpoint if checkpoint is not None else json.loads(row["checkpoint_json"]),
                             ensure_ascii=False, sort_keys=True)
        attempts = row["attempts"] + (1 if status == "in_progress" and old != "in_progress" else 0)
        self.conn.execute("UPDATE publication_tasks SET status=?,attempts=?,checkpoint_json=?,error=?,updated_at_utc=? "
                          "WHERE task_id=?", (status, attempts, payload, error, self._now(), task_id))
        self.conn.commit()

    def transition_run(self, run_id: str, status: str, error: str | None = None):
        row = self.conn.execute("SELECT status FROM publication_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise PublicationError(f"Unknown publication run: {run_id}")
        old = row["status"]
        if status != old and status not in self.RUN_TRANSITIONS.get(old, set()):
            raise PublicationError(f"Illegal run transition {old} -> {status} for {run_id}")
        finished = self._now() if status in {"committed", "failed", "interrupted"} else None
        self.conn.execute("UPDATE publication_runs SET status=?,finished_at_utc=?,error=? WHERE run_id=?",
                          (status, finished, error, run_id))
        self.conn.commit()

    def add_generation(self, generation_id: str, run_id: str, scope: str, relative_path: str):
        self.conn.execute("INSERT INTO publication_generations(generation_id,run_id,scope,status,relative_path,created_at_utc) "
                          "VALUES(?,?,?,?,?,?)", (generation_id, run_id, scope, "staging", relative_path, self._now()))
        self.conn.commit()

    def transition_generation(self, generation_id: str, status: str,
                              manifest_sha256: str | None = None):
        allowed = {"staging": {"validated", "interrupted", "failed"},
                   "validated": {"committed", "interrupted", "failed"},
                   "interrupted": {"staging", "failed"}, "failed": {"staging"}, "committed": set()}
        row = self.conn.execute("SELECT status FROM publication_generations WHERE generation_id=?",
                                (generation_id,)).fetchone()
        if not row:
            raise PublicationError(f"Unknown publication generation: {generation_id}")
        if status != row["status"] and status not in allowed.get(row["status"], set()):
            raise PublicationError(f"Illegal generation transition {row['status']} -> {status}")
        committed = self._now() if status == "committed" else None
        self.conn.execute("UPDATE publication_generations SET status=?,manifest_sha256=COALESCE(?,manifest_sha256),"
                          "committed_at_utc=COALESCE(?,committed_at_utc) WHERE generation_id=?",
                          (status, manifest_sha256, committed, generation_id))
        self.conn.commit()

    def record_verified_file(self, run_id: str, relative_path: str, data: bytes):
        self.conn.execute("INSERT INTO publication_files(run_id,relative_path,sha256,bytes,status,verified_at_utc) "
                          "VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,relative_path) DO UPDATE SET "
                          "sha256=excluded.sha256,bytes=excluded.bytes,status=excluded.status,verified_at_utc=excluded.verified_at_utc",
                          (run_id, relative_path, sha256_bytes(data), len(data), "verified", self._now()))
        self.conn.commit()

    def recover_from_marker(self, generation_id: str, manifest_sha: str):
        row = self.conn.execute("SELECT status FROM publication_generations WHERE generation_id=?",
                                (generation_id,)).fetchone()
        if row:
            if row["status"] != "committed":
                self.transition_generation(generation_id, "committed", manifest_sha)
            run_id = self.conn.execute("SELECT run_id FROM publication_generations WHERE generation_id=?",
                                       (generation_id,)).fetchone()[0]
            run = self.conn.execute("SELECT status FROM publication_runs WHERE run_id=?", (run_id,)).fetchone()
            if run and run[0] != "committed":
                self.transition_run(run_id, "committed")
            tasks = self.conn.execute("SELECT task_id,status FROM publication_tasks WHERE run_id=?", (run_id,)).fetchall()
            for task in tasks:
                if task["status"] == "validated":
                    self.transition_task(task["task_id"], "committed")

    def enqueue_repair(self, generation_id: str, relative_path: str, reason: str):
        path = safe_relative_path(relative_path)
        task_id = f"repair:{generation_id}:{path}"
        now = self._now()
        self.conn.execute("INSERT INTO repair_tasks(task_id,generation_id,relative_path,reason,status,created_at_utc,updated_at_utc) "
                          "VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                          "reason=excluded.reason,status=CASE WHEN repair_tasks.status='completed' THEN 'pending' "
                          "ELSE repair_tasks.status END,updated_at_utc=excluded.updated_at_utc",
                          (task_id, generation_id, path, reason, "pending", now, now))
        self.conn.commit()


def _schema_validate_documents(documents: dict[str, dict], schema_map: dict[str, str], schemas_dir: Path):
    for relative, value in documents.items():
        schema_name = schema_map.get(relative)
        if not schema_name:
            continue
        errors = validate_json_document(value, schemas_dir / schema_name)
        if errors:
            raise InvalidPublication(f"Schema errors in {relative}: {'; '.join(errors[:5])}")


def _resolve_generation_reference(generation_root: Path, base: PurePosixPath, target: str) -> Path:
    """Resolve an index link within a generation, allowing safe parent links."""
    raw = str(target)
    parts = PurePosixPath(raw).parts
    if (not raw or PurePosixPath(raw).is_absolute() or raw.startswith("//")
            or "\\" in raw or "\x00" in raw or (parts and ":" in parts[0])):
        raise InvalidPublication(f"Invalid relative index reference: {target}")
    root = generation_root.resolve()
    resolved = (root / Path(*base.parts) / Path(*parts)).resolve()
    if not resolved.is_relative_to(root):
        raise InvalidPublication(f"Index reference escapes generation: {target}")
    return resolved


def _check_generation_refs(generation_root: Path, docs: dict[str, dict], markdowns: dict[str, str],
                           external_root: Path | None = None, check_images: bool = True):
    generation_root = generation_root.resolve()
    for relative, document in docs.items():
        rel = safe_relative_path(relative)
        index_dir = PurePosixPath(rel).parent
        if "cards" in document and "edition" in document:
            errors = check_recomputed_counts(document)
            if errors:
                raise InvalidPublication(f"Count mismatch in {relative}: {'; '.join(errors)}")
            seen_printings = set()
            seen_provider_ids = set()
            for card in document.get("cards", []):
                if card["printing_id"] in seen_printings:
                    raise InvalidPublication(f"Duplicate printing_id in {relative}: {card['printing_id']}")
                seen_printings.add(card["printing_id"])
                languages = card.get("language_codes", [])
                if not languages or any(language not in {"fr", "en"} for language in languages):
                    raise InvalidPublication(f"Unsupported or empty language set in {relative}: {card['printing_id']}")
                for provider_id in card.get("provider_ids", {}).values():
                    if provider_id in seen_provider_ids:
                        raise InvalidPublication(f"Duplicate provider card id in {relative}: {provider_id}")
                    seen_provider_ids.add(provider_id)
                faces = card.get("faces", [])
                expected_face_count = 2 if card.get("layout") in PHYSICAL_MULTIFACE_LAYOUTS else 1
                if len(faces) != expected_face_count:
                    raise InvalidPublication(f"Unexpected physical face count in {relative}: {card['printing_id']}")
                face_indices = [face.get("face_index") for face in faces]
                if face_indices != list(range(expected_face_count)):
                    raise InvalidPublication(f"Missing, duplicate, or unordered face indexes in {relative}: {card['printing_id']}")
                for face in card.get("faces", []):
                    image = face.get("image", {})
                    image_path = image.get("path")
                    if image.get("status") == "ready":
                        if not image_path:
                            raise InvalidPublication(f"Ready image has no path in {relative}")
                        selected_language = image.get("selected_language")
                        if selected_language not in languages:
                            raise InvalidPublication(f"Image language is absent from card identity: {card['printing_id']}")
                        expected_folder = "images_fr" if selected_language == "fr" else "cartes anglaises"
                        if PurePosixPath(image_path).parts[0] != expected_folder:
                            raise InvalidPublication(f"Image is stored under the wrong language folder: {image_path}")
                        target = safe_relative_path((index_dir / image_path).as_posix())
                        candidate_path = (generation_root / target).resolve()
                        if not candidate_path.is_relative_to(generation_root) or not candidate_path.is_file():
                            raise InvalidPublication(f"Image reference does not resolve inside generation: {target}")
                        data = candidate_path.read_bytes()
                        if sha256_bytes(data) != image.get("sha256"):
                            raise InvalidPublication(f"Image SHA-256 mismatch: {target}")
                        if check_images:
                            try:
                                with Image.open(candidate_path) as image_file:
                                    image_file.load()
                                    if image_file.format != "PNG" or image_file.size != (531, 738):
                                        raise InvalidPublication(f"Image is not a 531x738 PNG: {target}")
                            except (OSError, UnidentifiedImageError) as exc:
                                raise InvalidPublication(f"Image cannot be decoded: {target}: {exc}") from exc
            if external_root:
                ext_root = external_root.resolve()
                for asset in document.get("local_assets", []):
                    target_rel = safe_relative_path(asset["path"])
                    asset_path = (ext_root / target_rel).resolve()
                    if not asset_path.is_relative_to(ext_root):
                        raise InvalidPublication(f"Local asset escapes library root: {asset['path']}")
                    if not asset_path.is_file() or sha256_bytes(asset_path.read_bytes()) != asset["sha256"]:
                        raise InvalidPublication(f"Local asset is missing or stale: {asset['path']}")
        elif "editions" in document and "completeness" in document:
            for edition in document.get("editions", []):
                for field in ("index_json", "index_md"):
                    target = safe_relative_path((PurePosixPath(index_dir) / edition[field]).as_posix())
                    path = (generation_root / target).resolve()
                    if not path.is_relative_to(generation_root) or not path.is_file():
                        raise InvalidPublication(f"Catalogue link missing or escapes generation: {edition[field]}")
        elif "folder" in document:
            folder_kind = document["folder"].get("kind")
            folder_language = document["folder"].get("language")
            for asset in document.get("assets", []):
                asset_rel = safe_relative_path(asset["path"])
                if folder_kind == "historical_group":
                    if external_root is None:
                        raise InvalidPublication("Historical group indexes require an external library root")
                    folder_rel = safe_relative_path(document["folder"]["path"])
                    asset_path = (external_root / folder_rel / asset_rel).resolve()
                    if not asset_path.is_relative_to(external_root.resolve()):
                        raise InvalidPublication(f"Historical asset escapes library root: {asset['path']}")
                else:
                    asset_path = (generation_root / PurePosixPath(rel).parent / asset_rel).resolve()
                    if not asset_path.is_relative_to(generation_root):
                        raise InvalidPublication(f"Folder asset escapes generation: {asset['path']}")
                if not asset_path.is_file():
                    raise InvalidPublication(f"Folder asset is missing: {asset['path']}")
                payload = asset_path.read_bytes()
                if len(payload) != asset["bytes"] or sha256_bytes(payload) != asset["sha256"]:
                    raise InvalidPublication(f"Folder asset hash mismatch: {asset['path']}")
                if asset["kind"] == "card_image" and folder_kind == "language":
                    expected_folder = "images_fr" if asset["language"] == "fr" else "cartes anglaises"
                    if folder_language != asset["language"] or PurePosixPath(rel).parent.name != expected_folder:
                        raise InvalidPublication(f"Folder asset language mismatch: {asset['path']}")
                    if asset["identity_status"] == "matched" and (not asset["printing_id"] or asset["face_index"] is None):
                        raise InvalidPublication(f"Matched asset has no printing/face identity: {asset['path']}")
                    if check_images:
                        try:
                            with Image.open(asset_path) as image_file:
                                image_file.load()
                                if image_file.format != "PNG" or image_file.size != (531, 738):
                                    raise InvalidPublication(f"Managed card image is not 531x738 PNG: {asset['path']}")
                        except (OSError, UnidentifiedImageError) as exc:
                            raise InvalidPublication(f"Folder image cannot be decoded: {asset['path']}: {exc}") from exc
                elif asset["kind"] == "card_image" and folder_kind == "historical_group":
                    if asset["identity_status"] == "matched" and (not asset["printing_id"] or asset["face_index"] is None):
                        raise InvalidPublication(f"Matched historical asset has no printing/face identity: {asset['path']}")
            for link in document.get("edition_links", []):
                for field in ("index_json", "index_md"):
                    path = _resolve_generation_reference(generation_root, index_dir, link[field])
                    if not path.is_file():
                        raise InvalidPublication(f"Folder index link missing: {link[field]}")
    for md_path, markdown in markdowns.items():
        rel = safe_relative_path(md_path)
        parent = PurePosixPath(rel).parent
        paired_json = str(PurePosixPath(rel).with_suffix(".json"))
        paired_doc = docs.get(paired_json, {})
        historical_group = (paired_doc.get("folder", {}).get("kind") == "historical_group")
        for target_ref in markdown_link_targets(markdown):
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target_ref) or target_ref.startswith("#"):
                continue
            # Markdown renderers percent-encode non-ASCII and spaces in links;
            # resolve the decoded path against the actual filesystem name.
            resolved = _resolve_generation_reference(generation_root, parent, unquote(target_ref))
            if resolved.is_file():
                continue
            if historical_group and external_root is not None:
                folder_rel = safe_relative_path(paired_doc["folder"]["path"])
                external = (external_root / folder_rel / Path(unquote(target_ref))).resolve()
                if external.is_relative_to(external_root.resolve()) and external.is_file():
                    continue
            raise InvalidPublication(f"Markdown relative link missing or escapes generation: {md_path} -> {target_ref}")


class Publisher:
    def __init__(self, root: str | Path, schemas_dir: str | Path,
                 external_library_root: str | Path | None = None,
                 keep_valid_generations: int = 2):
        self.root = Path(root).resolve()
        self.generations = self.root / "generations"
        self.schemas_dir = Path(schemas_dir).resolve()
        self.external_library_root = Path(external_library_root).resolve() if external_library_root else None
        self.keep_valid_generations = max(2, int(keep_valid_generations))
        self.state_path = self.root / "state.sqlite"
        self.marker_path = self.root / "publication.json"
        self.previous_marker_path = self.root / "publication.previous.json"

    def _parse_documents(self, document_payloads: dict[str, dict | str]):
        documents, markdowns, serialized = {}, {}, {}
        for name, value in document_payloads.items():
            relative = safe_relative_path(name)
            if relative.casefold().endswith(".json"):
                if not isinstance(value, dict):
                    raise InvalidPublication(f"JSON document must be an object: {relative}")
                documents[relative] = value
                serialized[relative] = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            elif relative.casefold().endswith(".md"):
                if not isinstance(value, str):
                    raise InvalidPublication(f"Markdown document must be text: {relative}")
                markdowns[relative] = value
                serialized[relative] = value.encode("utf-8")
            else:
                raise InvalidPublication(f"Unsupported publication document extension: {relative}")
        return documents, markdowns, serialized

    def validate_generation(self, generation_id: str, check_images: bool = True) -> dict:
        gen_dir = (self.generations / safe_relative_path(generation_id)).resolve()
        if not gen_dir.is_relative_to(self.generations.resolve()) or not gen_dir.is_dir():
            raise InvalidPublication(f"Generation directory is missing or unsafe: {generation_id}")
        manifest_path = gen_dir / "generation.json"
        if not manifest_path.is_file():
            raise InvalidPublication("Generation manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidPublication(f"Generation manifest cannot be read: {exc}") from exc
        if manifest.get("generation_id") != generation_id:
            raise InvalidPublication("Generation id mismatch")
        files = manifest.get("files")
        if not isinstance(files, list):
            raise InvalidPublication("Generation manifest files must be a list")
        docs, markdowns = {}, {}
        schemas = {}
        for row in files:
            relative = safe_relative_path(row.get("path", ""))
            path = (gen_dir / relative).resolve()
            if not path.is_relative_to(gen_dir) or not path.is_file():
                raise InvalidPublication(f"Generation file missing or unsafe: {relative}")
            data = path.read_bytes()
            if len(data) != row.get("bytes") or sha256_bytes(data) != row.get("sha256"):
                raise InvalidPublication(f"Generation file hash mismatch: {relative}")
            if row.get("kind") == "json":
                try:
                    docs[relative] = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise InvalidPublication(f"Invalid JSON {relative}: {exc}") from exc
                schema_name = row.get("schema")
                if schema_name:
                    schemas[relative] = schema_name
            elif row.get("kind") == "markdown":
                markdowns[relative] = data.decode("utf-8")
        _schema_validate_documents(docs, schemas, self.schemas_dir)
        for relative, document in docs.items():
            paired = str(PurePosixPath(relative).with_suffix(".md"))
            if paired in markdowns and document.get("generation_id") not in markdowns[paired]:
                raise InvalidPublication(f"Markdown generation id differs from JSON: {relative}")
        _check_generation_refs(gen_dir, docs, markdowns, self.external_library_root, check_images)
        return manifest

    def validate_current(self, check_images: bool = True) -> dict:
        try:
            marker_bytes = self.marker_path.read_bytes()
            marker = json.loads(marker_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidPublication(f"Publication marker cannot be read: {exc}") from exc
        generation_id = marker.get("generation_id")
        if not isinstance(generation_id, str) or not generation_id:
            raise InvalidPublication("Publication marker has no generation id")
        manifest_path = self.generations / generation_id / "generation.json"
        manifest_bytes = manifest_path.read_bytes()
        if sha256_bytes(manifest_bytes) != marker.get("manifest_sha256"):
            raise InvalidPublication("Publication marker manifest hash mismatch")
        try:
            manifest = self.validate_generation(generation_id, check_images)
        except InvalidPublication as exc:
            self._queue_repairs_for_generation(generation_id, str(exc))
            raise
        gen_root = (self.generations / generation_id).resolve()
        for relative, record in marker.get("indexes", {}).items():
            index_rel = safe_relative_path(relative)
            index_path = (gen_root / index_rel).resolve()
            if (not index_path.is_relative_to(gen_root) or not index_path.is_file()
                    or sha256_bytes(index_path.read_bytes()) != record.get("sha256")):
                raise InvalidPublication(f"Publication index reference/hash mismatch: {relative}")
        return {"status": "valid", "generation_id": generation_id,
                "manifest_sha256": marker["manifest_sha256"], "marker": marker, "manifest": manifest}

    def _queue_repairs_for_generation(self, generation_id: str, reason: str):
        manifest_path = self.generations / generation_id / "generation.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        with StateStore(self.state_path) as state:
            for row in manifest.get("files", []):
                if row.get("kind") not in {"artifact", "json", "markdown"}:
                    continue
                try:
                    relative = safe_relative_path(row.get("path", ""))
                    path = self.generations / generation_id / relative
                    payload = path.read_bytes() if path.is_file() else None
                    is_bad = payload is None or len(payload) != row.get("bytes") or sha256_bytes(payload) != row.get("sha256")
                except (InvalidPublication, OSError, TypeError):
                    continue
                if is_bad:
                    state.enqueue_repair(generation_id, relative, reason)

    def _artifact_reuse_source(self, relative: str, data: bytes, current: dict | None) -> Path | None:
        """Return a verified staged image to hard-link into the generation."""
        digest = sha256_bytes(data)
        sources = []
        parts = PurePosixPath(relative).parts
        if len(parts) >= 3:
            sources.append(self.root / "image_staging" / Path(*parts[1:]))
        for source in sources:
            try:
                resolved = source.resolve(strict=True)
                if not resolved.is_relative_to(self.root) or not resolved.is_file():
                    continue
                if resolved.stat().st_size == len(data) and sha256_bytes(resolved.read_bytes()) == digest:
                    return resolved
            except OSError:
                continue
        return None

    def _prune_committed_image_inputs(self, artifacts: dict[str, bytes], generation_id: str):
        """Remove only verified, now-published image staging/cache files."""
        if not artifacts:
            return
        generation_root = (self.generations / generation_id).resolve()
        staging_root = (self.root / "image_staging").resolve()
        for relative, data in artifacts.items():
            parts = PurePosixPath(relative).parts
            if len(parts) < 3:
                continue
            staged = self.root / "image_staging" / Path(*parts[1:])
            target = generation_root / relative
            try:
                staged_real = staged.resolve(strict=True)
                target_real = target.resolve(strict=True)
                if (staged_real.is_relative_to(staging_root) and target_real.is_relative_to(generation_root)
                        and staged_real.is_file() and target_real.is_file()
                        and staged_real.stat().st_size == len(data)
                        and sha256_bytes(staged_real.read_bytes()) == sha256_bytes(data)
                        and sha256_bytes(target_real.read_bytes()) == sha256_bytes(data)):
                    staged_real.unlink()
            except OSError:
                continue

        staged_hashes = {}
        if staging_root.is_dir():
            for path in staging_root.rglob("*"):
                try:
                    resolved = path.resolve(strict=True)
                    if (path.is_symlink() or not resolved.is_relative_to(staging_root)
                            or not resolved.is_file()):
                        continue
                    staged_hashes[resolved] = sha256_bytes(resolved.read_bytes())
                except OSError:
                    continue
        wanted_hashes = set(staged_hashes.values())
        committed_hashes = set()
        try:
            with closing(sqlite3.connect(self.state_path)) as conn:
                committed_ids = {row[0] for row in conn.execute(
                    "SELECT generation_id FROM publication_generations WHERE status='committed'")}
        except sqlite3.Error:
            committed_ids = {generation_id}
        for old_generation_id in committed_ids:
            old_root = (self.generations / old_generation_id).resolve()
            if not old_root.is_relative_to(self.generations.resolve()) or not old_root.is_dir():
                continue
            try:
                old_manifest = json.loads((old_root / "generation.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if old_manifest.get("generation_id") != old_generation_id:
                continue
            for row in old_manifest.get("files", []):
                if row.get("kind") != "artifact" or row.get("sha256") not in wanted_hashes:
                    continue
                try:
                    relative = safe_relative_path(row.get("path", ""))
                    stored = (old_root / relative).resolve(strict=True)
                    if (stored.is_relative_to(old_root) and stored.is_file()
                            and stored.stat().st_size == row.get("bytes")
                            and sha256_bytes(stored.read_bytes()) == row["sha256"]):
                        committed_hashes.add(row["sha256"])
                except (InvalidPublication, OSError, KeyError, TypeError):
                    continue
        for path, digest in staged_hashes.items():
            if digest in committed_hashes:
                try:
                    path.unlink()
                except OSError:
                    continue

        cache_root = (self.root / "cache").resolve()
        if not cache_root.is_relative_to(self.root) or cache_root.name != "cache" or not cache_root.is_dir():
            return
        for path in cache_root.rglob("*"):
            try:
                resolved = path.resolve(strict=True)
                if path.is_symlink() or not resolved.is_relative_to(cache_root):
                    continue
                if resolved.is_file():
                    resolved.unlink()
            except OSError:
                continue
        for path in sorted(cache_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                if not path.is_symlink() and path.resolve().is_relative_to(cache_root) and path.is_dir():
                    path.rmdir()
            except OSError:
                continue

    def _prune_old_generations(self) -> list[str]:
        """Keep the configured newest committed generations and preserve crash diagnostics."""
        try:
            current_marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
            current_id = current_marker.get("generation_id")
        except (OSError, json.JSONDecodeError):
            return []
        protected = {current_id}
        try:
            previous_marker = json.loads(self.previous_marker_path.read_text(encoding="utf-8"))
            if previous_marker.get("generation_id"):
                protected.add(previous_marker["generation_id"])
        except (OSError, json.JSONDecodeError):
            pass
        with closing(sqlite3.connect(self.state_path)) as conn:
            rows = conn.execute(
                "SELECT generation_id FROM publication_generations WHERE status='committed' "
                "ORDER BY committed_at_utc DESC, created_at_utc DESC, rowid DESC").fetchall()
        keep_ids = protected | {row[0] for row in rows[:self.keep_valid_generations]}
        pruned = []
        generations_root = self.generations.resolve()
        for (generation_id,) in rows:
            if generation_id in keep_ids:
                continue
            try:
                relative = safe_relative_path(generation_id)
                target = self.generations / relative
                resolved = target.resolve(strict=True)
                if (target.is_symlink() or resolved.parent != generations_root
                        or not resolved.is_dir()):
                    continue
                # Remove entries bottom-up without following links.
                for entry in sorted(resolved.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                    if entry.is_symlink() or entry.is_file():
                        entry.unlink(missing_ok=True)
                    elif entry.is_dir():
                        entry.rmdir()
                resolved.rmdir()
                pruned.append(generation_id)
            except OSError:
                continue
        return pruned

    def reconcile(self, state: StateStore | None = None) -> dict | None:
        try:
            current = self.validate_current()
            if state:
                state.recover_from_marker(current["generation_id"], current["manifest_sha256"])
            return current
        except (InvalidPublication, OSError, TypeError, KeyError):
            pass
        try:
            previous_bytes = self.previous_marker_path.read_bytes()
            previous = json.loads(previous_bytes.decode("utf-8"))
            generation_id = previous.get("generation_id")
            manifest_path = self.generations / generation_id / "generation.json"
            manifest_bytes = manifest_path.read_bytes()
            if sha256_bytes(manifest_bytes) != previous.get("manifest_sha256"):
                return None
            self.validate_generation(generation_id)
            atomic_write(self.marker_path, previous_bytes)
            if state:
                state.recover_from_marker(generation_id, previous["manifest_sha256"])
            return {"status": "recovered_previous", "generation_id": generation_id,
                    "manifest_sha256": previous["manifest_sha256"], "marker": previous}
        except (InvalidPublication, OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            return None

    def publish(self, document_payloads: dict[str, dict | str], artifacts: dict[str, bytes],
                schema_map: dict[str, str], scope: str = "pilot", fail_after: str | None = None,
                generation_id: str | None = None) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        self.generations.mkdir(parents=True, exist_ok=True)
        documents, markdowns, serialized = self._parse_documents(document_payloads)
        _schema_validate_documents(documents, schema_map, self.schemas_dir)
        document_generations = {value.get("generation_id") for value in documents.values()
                                if isinstance(value, dict) and value.get("generation_id")}
        if generation_id is None:
            if len(document_generations) > 1:
                raise InvalidPublication("All indexes in a publication must share one generation_id")
            generation_id = next(iter(document_generations),
                                 f"g-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}")
        if document_generations and document_generations != {generation_id}:
            raise InvalidPublication("Publication generation_id differs from index generation_id")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", generation_id) or generation_id in {".", ".."}:
            raise InvalidPublication("Unsafe generation_id")
        normalized_artifacts = {}
        for path, payload in artifacts.items():
            relative = safe_relative_path(path)
            if relative in serialized or relative == "generation.json":
                raise InvalidPublication(f"Duplicate publication path: {relative}")
            if not isinstance(payload, (bytes, bytearray)):
                raise InvalidPublication(f"Artifact content must be bytes: {relative}")
            normalized_artifacts[relative] = bytes(payload)
        current_marker_bytes = None
        with FileLock(self.root / ".publish.lock"):
            with StateStore(self.state_path) as state:
                current = self.reconcile(state)
                if self.marker_path.is_file():
                    current_marker_bytes = self.marker_path.read_bytes()
                run_id = uuid.uuid4().hex
                gen_relative = f"generations/{generation_id}"
                gen_dir = self.generations / generation_id
                state.create_run(run_id, scope)
                state.add_generation(generation_id, run_id, scope, gen_relative)
                file_paths = list(normalized_artifacts) + list(serialized)
                for rel in file_paths:
                    task_id = f"{run_id}:{rel}"
                    state.add_task(task_id, run_id, "artifact" if rel in normalized_artifacts else "index", rel)
                try:
                    marker_committed = False
                    for relative, data in normalized_artifacts.items():
                        task_id = f"{run_id}:{relative}"
                        state.transition_task(task_id, "in_progress")
                        target = (gen_dir / relative).resolve()
                        if not target.is_relative_to(gen_dir.resolve()) or len(str(target)) > 240:
                            raise InvalidPublication(f"Artifact path escapes generation: {relative}")
                        reuse_source = self._artifact_reuse_source(relative, data, current)
                        if reuse_source is not None:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            try:
                                os.link(reuse_source, target)
                            except OSError:
                                atomic_write(target, data)
                        else:
                            atomic_write(target, data)
                        if fail_after == "after_artifact_before_state":
                            raise InjectedFailure("injected after artifact promotion before state checkpoint")
                        if target.read_bytes() != data:
                            raise StorageFailure(f"Artifact verification differs: {relative}")
                        state.record_verified_file(run_id, relative, data)
                        state.transition_task(task_id, "staged", checkpoint={"sha256": sha256_bytes(data)})

                    for relative, data in serialized.items():
                        task_id = f"{run_id}:{relative}"
                        state.transition_task(task_id, "in_progress")
                        target = (gen_dir / relative).resolve()
                        if not target.is_relative_to(gen_dir.resolve()) or len(str(target)) > 240:
                            raise InvalidPublication(f"Document path escapes generation: {relative}")
                        atomic_write(target, data)
                        if target.read_bytes() != data:
                            raise StorageFailure(f"Document verification differs: {relative}")
                        state.record_verified_file(run_id, relative, data)
                        state.transition_task(task_id, "staged", checkpoint={"sha256": sha256_bytes(data)})
                        if fail_after == "after_json_before_markdown" and relative.casefold().endswith(".json"):
                            paired = str(PurePosixPath(relative).with_suffix(".md"))
                            if paired in serialized:
                                raise InjectedFailure("injected after JSON before paired Markdown")

                    _check_generation_refs(gen_dir, documents, markdowns, self.external_library_root)
                    for relative in file_paths:
                        state.transition_task(f"{run_id}:{relative}", "validated")
                    state.transition_run(run_id, "validated")
                    file_manifest = []
                    # Content manifest hashes only the committed data files, never itself.
                    for relative in file_paths:
                        path = gen_dir / relative
                        data = path.read_bytes()
                        kind = "artifact" if relative in normalized_artifacts else (
                            "json" if relative.casefold().endswith(".json") else "markdown")
                        schema_name = schema_map.get(relative)
                        file_manifest.append({"path": relative, "sha256": sha256_bytes(data),
                                              "bytes": len(data), "kind": kind,
                                              **({"schema": schema_name} if schema_name else {})})
                    manifest = {"schema_version": "1.0.0", "generation_id": generation_id,
                                "scope": scope, "created_at_utc": utc_now(), "files": file_manifest}
                    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2,
                                                 sort_keys=True) + "\n").encode("utf-8")
                    atomic_write(gen_dir / "generation.json", manifest_bytes)
                    manifest_sha = sha256_bytes(manifest_bytes)
                    state.transition_generation(generation_id, "validated", manifest_sha)
                    self.validate_generation(generation_id)
                    if fail_after == "before_marker_commit":
                        raise InjectedFailure("injected before publication marker commit")
                    if current_marker_bytes is not None:
                        atomic_write(self.previous_marker_path, current_marker_bytes)
                    index_paths = {}
                    for relative in file_paths:
                        if relative.casefold().endswith(("index.json", "index.md")):
                            data = (gen_dir / relative).read_bytes()
                            index_paths[relative] = {"sha256": sha256_bytes(data), "bytes": len(data)}
                    marker = {"schema_version": "1.0.0", "generation_id": generation_id,
                              "generation_path": gen_relative, "manifest_sha256": manifest_sha,
                              "committed_at_utc": utc_now(), "previous_generation_id":
                              current["generation_id"] if current else None,
                              "indexes": index_paths}
                    marker_bytes = (json.dumps(marker, ensure_ascii=False, indent=2,
                                               sort_keys=True) + "\n").encode("utf-8")
                    atomic_write(self.marker_path, marker_bytes)
                    marker_committed = True
                    if fail_after == "after_marker_before_state":
                        raise InjectedFailure("injected after commit marker before SQLite checkpoint")
                    state.transition_generation(generation_id, "committed", manifest_sha)
                    if fail_after == "after_generation_state":
                        raise InjectedFailure("injected after generation SQLite checkpoint")
                    state.transition_run(run_id, "committed")
                    if fail_after == "after_run_state":
                        raise InjectedFailure("injected after run SQLite checkpoint")
                    for relative in file_paths:
                        state.transition_task(f"{run_id}:{relative}", "committed")
                    self.validate_current()
                    self._prune_committed_image_inputs(normalized_artifacts, generation_id)
                    pruned_generations = self._prune_old_generations()
                    return {"status": "committed", "scope": scope, "generation_id": generation_id,
                            "generation_path": gen_relative, "marker_path": str(self.marker_path),
                            "files": len(file_paths), "manifest_sha256": manifest_sha,
                            "previous_generation_id": marker["previous_generation_id"],
                            "pruned_generations": pruned_generations}
                except BaseException as exc:
                    if not marker_committed:
                        try:
                            state.transition_generation(generation_id, "interrupted" if isinstance(exc, (InjectedFailure, KeyboardInterrupt)) else "failed")
                        except PublicationError:
                            pass
                        try:
                            state.transition_run(run_id, "interrupted" if isinstance(exc, (InjectedFailure, KeyboardInterrupt)) else "failed",
                                                 error=f"{type(exc).__name__}: {exc}")
                        except PublicationError:
                            pass
                    raise

    def validate_all(self, strict_completeness: bool = False, check_images: bool = True) -> dict:
        with StateStore(self.state_path) as state:
            recovered = self.reconcile(state)
        if not recovered:
            raise InvalidPublication("No valid committed publication is available")
        current = self.validate_current(check_images=check_images)
        gen_dir = self.generations / current["generation_id"]
        manifest = current["manifest"]
        with closing(sqlite3.connect(self.state_path)) as conn:
            generation = conn.execute("SELECT run_id,status,manifest_sha256 FROM publication_generations WHERE generation_id=?",
                                      (current["generation_id"],)).fetchone()
            if not generation or generation[1] != "committed" or generation[2] != current["manifest_sha256"]:
                raise InvalidPublication("Committed generation and SQLite state do not reconcile")
            run_status = conn.execute("SELECT status FROM publication_runs WHERE run_id=?",
                                      (generation[0],)).fetchone()
            if not run_status or run_status[0] != "committed":
                raise InvalidPublication("Publication run is not committed in SQLite state")
            task_rows = conn.execute("SELECT status FROM publication_tasks WHERE run_id=?", (generation[0],)).fetchall()
            if len(task_rows) != len(manifest["files"]) or any(row[0] != "committed" for row in task_rows):
                raise InvalidPublication("Publication task states do not match the committed manifest")
            for row in manifest["files"]:
                verified = conn.execute("SELECT sha256,bytes,status FROM publication_files WHERE run_id=? AND relative_path=?",
                                        (generation[0], row["path"])).fetchone()
                if (not verified or verified[2] != "verified" or verified[0] != row["sha256"]
                        or verified[1] != row["bytes"]):
                    raise InvalidPublication(f"SQLite verified-file record differs from manifest: {row['path']}")
        editions = []
        gaps = []
        for row in manifest["files"]:
            if row["kind"] != "json" or not row.get("schema"):
                continue
            value = json.loads((gen_dir / row["path"]).read_text(encoding="utf-8"))
            if "edition" in value:
                editions.append(value)
                gaps.extend(value.get("gaps", []))
        incomplete = [index["edition"]["code"] for index in editions
                      if index["completeness"]["metadata_state"] != "complete"
                      or index["completeness"]["image_state"] != "complete"]
        result = {"status": "valid", "generation_id": current["generation_id"],
                  "technical_valid": True, "editions": len(editions),
                  "incomplete_editions": incomplete, "gaps": len(gaps),
                  "strict_complete": not incomplete and not gaps, "state_reconciled": True}
        if strict_completeness and not result["strict_complete"]:
            result["status"] = "incomplete"
        return result

    def report(self) -> dict:
        current = self.validate_current(check_images=False)
        manifest = current["manifest"]
        gen_dir = self.generations / current["generation_id"]
        edition_indexes = []
        cards = 0; faces = 0; valid_images = 0; french = english = lowres = corrupt = unresolved = 0
        for row in manifest["files"]:
            if row["kind"] != "json" or not row.get("schema"):
                continue
            value = json.loads((gen_dir / row["path"]).read_text(encoding="utf-8"))
            if "edition" not in value:
                continue
            edition_indexes.append(value)
            cards += len(value.get("cards", []))
            for card in value.get("cards", []):
                for face in card.get("faces", []):
                    faces += 1
                    image = face.get("image", {})
                    if image.get("status") == "ready":
                        valid_images += 1
                        french += image.get("selected_language") == "fr"
                        english += image.get("selected_language") == "en"
                        lowres += image.get("quality") == "low_resolution_fallback"
            unresolved += value.get("completeness", {}).get("unresolved_identities", 0)
        state_counts = {"fr_gets": 0, "en_gets": 0, "blocked_sources": 0, "failed_images": 0}
        corruption_count = 0
        with closing(sqlite3.connect(self.state_path)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "image_get_events" in tables:
                for lang, count in conn.execute("SELECT language,COUNT(*) FROM image_get_events GROUP BY language"):
                    if lang == "fr": state_counts["fr_gets"] = count
                    if lang == "en": state_counts["en_gets"] = count
            if "image_provider_blocks" in tables:
                state_counts["blocked_sources"] = conn.execute("SELECT COUNT(*) FROM image_provider_blocks").fetchone()[0]
            if "image_attempts" in tables:
                state_counts["failed_images"] = conn.execute("SELECT COUNT(*) FROM image_attempts WHERE status='rejected'").fetchone()[0]
                corruption_count = conn.execute("SELECT COUNT(*) FROM image_attempts WHERE reason LIKE 'decode_error:%' "
                                                "OR reason='fully_transparent_placeholder'").fetchone()[0]
        published_bytes = sum(row["bytes"] for row in manifest["files"])
        try:
            free_bytes = shutil.disk_usage(self.root).free
        except OSError:
            free_bytes = None
        return {"generation_id": current["generation_id"],
                "editions_planned": len(edition_indexes), "editions_processed": len(edition_indexes),
                "editions_incomplete": sum(index["completeness"]["image_state"] != "complete"
                                            or index["completeness"]["metadata_state"] != "complete"
                                            for index in edition_indexes),
                "cards_indexed": cards, "faces_expected": faces, "faces_with_valid_image": valid_images,
                "images_fr": french, "images_en": english, "low_resolution_fallbacks": lowres,
                "corruptions": corruption_count, "unresolved_identities": unresolved,
                **state_counts, "published_bytes": published_bytes, "free_bytes": free_bytes}


def render_report_markdown(report: dict) -> str:
    lines = ["# Rapport de la bibliothèque Magic", "", f"Génération : `{report['generation_id']}`", "",
             f"- Éditions prévues / traitées / incomplètes : {report['editions_planned']} / {report['editions_processed']} / {report['editions_incomplete']}",
             f"- Faces valides / attendues : {report['faces_with_valid_image']} / {report['faces_expected']}",
             f"- Images FR / EN : {report['images_fr']} / {report['images_en']}",
             f"- Replis basse résolution : {report['low_resolution_fallbacks']}",
             f"- Corruptions / identités non résolues : {report['corruptions']} / {report['unresolved_identities']}",
             f"- GET FR / EN : {report['fr_gets']} / {report['en_gets']}",
             f"- Sources bloquées : {report['blocked_sources']}",
             f"- Espace publié : {report['published_bytes']} octets ; libre : {report['free_bytes']}", ""]
    return "\n".join(lines)
