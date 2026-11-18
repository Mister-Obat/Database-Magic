from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image, ImageOps, UnidentifiedImageError


OUTPUT_SIZE = (531, 738)
MAX_DECODED_PIXELS = 40_000_000
SOURCE_PRIORITY = {
    "local_exact": 0,
    "scryfall": 1,
    "wizards_gallery": 2,
    "gatherer": 3,
    "smfcorp": 4,
    "magiccorporation": 5,
}
IMAGE_URI_QUALITY = {"png": "high", "large": "high", "normal": "medium", "small": "low"}
QUALITY_ORDER = {"high": 0, "medium": 1, "low": 2, None: 3}
PLACEHOLDER_STATUSES = {"missing", "placeholder", "not_available", "back"}


class ImageCollectionError(Exception):
    pass


class ImageDeferred(ImageCollectionError):
    def __init__(self, retry_after: float, message: str = "Provider rate limited"):
        self.retry_after = max(0.0, float(retry_after))
        super().__init__(message)


@dataclass(frozen=True)
class ImageCandidate:
    printing_id: str
    face_index: int
    language: str | None
    provider: str
    url: str | None = None
    local_path: str | None = None
    declared_quality: str | None = None
    declared_status: str | None = None
    exact_identity: bool = False
    expected_set_id: str | None = None
    source_set_id: str | None = None
    expected_collector_number: str | None = None
    source_collector_number: str | None = None
    source_priority: int | None = None
    identity_evidence: str | None = None

    def __post_init__(self):
        if not self.printing_id or self.face_index < 0:
            raise ValueError("Candidate must identify a printing and a non-negative face")
        if bool(self.url) == bool(self.local_path):
            raise ValueError("Candidate must have exactly one of url or local_path")
        if self.source_priority is None:
            object.__setattr__(self, "source_priority", SOURCE_PRIORITY.get(self.provider, 99))

    @property
    def candidate_key(self) -> str:
        if self.url:
            return "url:" + hashlib.sha256(self.url.encode("utf-8")).hexdigest()
        path = str(Path(self.local_path or "").resolve())
        return "local:" + hashlib.sha256(path.encode("utf-8")).hexdigest()


@dataclass
class CandidateOutcome:
    candidate: ImageCandidate
    status: str
    reason: str | None = None
    get_count: int = 0
    source_width: int | None = None
    source_height: int | None = None
    source_format: str | None = None
    source_bytes: int | None = None
    source_sha256: str | None = None
    image_bytes: bytes | None = field(default=None, repr=False)

    @property
    def area(self) -> int:
        return int(self.source_width or 0) * int(self.source_height or 0)

    @property
    def short_side(self) -> int:
        return min(int(self.source_width or 0), int(self.source_height or 0))

    @property
    def target_quality(self) -> bool:
        return (self.status == "valid" and self.short_side >= 531
                and max(int(self.source_width or 0), int(self.source_height or 0)) >= 738
                and (self.candidate.declared_status or "").casefold() not in {"lowres", "low_resolution"})


@dataclass
class ImageResult:
    status: str
    path: str | None
    selected_language: str | None
    provider: str | None
    source_url: str | None
    source_width: int | None
    source_height: int | None
    source_format: str | None
    source_bytes: int | None
    source_sha256: str | None
    output_sha256: str | None
    final_width: int | None
    final_height: int | None
    quality: str
    choice_reason: str
    retryable: bool
    attempts: dict
    candidates: list[dict]
    errors: list[dict]

    def to_dict(self) -> dict:
        return asdict(self)


def candidates_for_face(face, verified_candidates: Iterable[ImageCandidate] = ()) -> list[ImageCandidate]:
    """Build language-proven candidates from a PhysicalFace and exact matches.

    The caller may supply local or official-gallery candidates only after the
    reconciliation layer has verified the same printing and face.
    """
    candidates = []
    for candidate in verified_candidates:
        if (candidate.printing_id == face.printing_id and candidate.face_index == face.face_index
                and candidate.exact_identity):
            candidates.append(candidate)
    # Each Scryfall URI belongs to this exact provider record. Cross-language
    # identity may remain unresolved while the record's own image stays usable.
    exact = bool(face.set_id and face.collector_number and face.provider_ids)
    for language in ("fr", "en"):
        uris = face.image_uris.get(language, {}) if isinstance(face.image_uris, dict) else {}
        if not isinstance(uris, dict):
            continue
        for field_name in ("png", "large", "normal", "small"):
            url = uris.get(field_name)
            if not isinstance(url, str) or not url.strip():
                continue
            candidates.append(ImageCandidate(
                printing_id=face.printing_id, face_index=face.face_index,
                language=language, provider="scryfall", url=url,
                declared_quality=IMAGE_URI_QUALITY[field_name], exact_identity=exact,
                expected_set_id=face.set_id, source_set_id=face.set_id,
                expected_collector_number=face.collector_number,
                source_collector_number=face.collector_number,
                identity_evidence="Scryfall image URI on the exact set/collector printing record"))
    return order_candidates(candidates)


def _read_document(document):
    if isinstance(document, dict):
        return document
    if document is None:
        return {}
    return json.loads(Path(document).read_text(encoding="utf-8-sig"))


def verified_local_candidates(face, library_root: str | Path, reconciliation_report) -> list[ImageCandidate]:
    """Return only unchanged local files linked to this exact provider printing/face."""
    report = _read_document(reconciliation_report)
    root = Path(library_root).resolve()
    result = []
    for row in report.get("results", []):
        if (row.get("status") != "matched" or row.get("match_method") not in {
                "exact_gallery_filename_name_language_set", "manual_override"}):
            continue
        language = (row.get("evidence") or {}).get("language")
        if language not in {"fr", "en"} or row.get("target_id") != face.provider_ids.get(language):
            continue
        if str((row.get("evidence") or {}).get("set_code", "")).casefold() != str(face.set_code or "").casefold():
            continue
        row_face = row.get("face_index")
        if (face.face_index == 0 and row_face not in (None, 0)) or (face.face_index != 0 and row_face != face.face_index):
            continue
        relative = Path(str(row.get("path", "")))
        try:
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                continue
            data = path.read_bytes()
        except OSError:
            continue
        expected_sha = row.get("sha256")
        if not expected_sha or hashlib.sha256(data).hexdigest() != expected_sha:
            continue
        result.append(ImageCandidate(
            printing_id=face.printing_id, face_index=face.face_index,
            language=language, provider="local_exact", local_path=str(path),
            declared_quality=None, exact_identity=True,
            expected_set_id=face.set_id, source_set_id=face.set_id,
            expected_collector_number=face.collector_number,
            source_collector_number=face.collector_number,
            identity_evidence=f"unchanged local SHA-256 matched exact gallery reconciliation {expected_sha}"))
    return result


def verified_gallery_candidates(face, gallery_catalogue, reconciliation_report) -> list[ImageCandidate]:
    """Join remote Wizards image URLs only through exact, reviewed reconciliation rows."""
    gallery = _read_document(gallery_catalogue)
    report = _read_document(reconciliation_report)
    assets_by_filename = {}
    for article in gallery.get("sample_articles", gallery.get("articles", [])):
        for asset in article.get("assets", []):
            filename = str(asset.get("filename") or "").casefold()
            if filename:
                assets_by_filename.setdefault(filename, []).append({**asset, "article_url": article.get("url")})
    result = []
    for row in report.get("results", []):
        if row.get("status") != "matched" or row.get("match_method") != "exact_gallery_filename_name_language_set":
            continue
        evidence = row.get("evidence") or {}
        language = evidence.get("language")
        filename = evidence.get("gallery_filename_exact")
        if language not in {"fr", "en"} or row.get("target_id") != face.provider_ids.get(language):
            continue
        if str(evidence.get("set_code", "")).casefold() != str(face.set_code or "").casefold():
            continue
        row_face = row.get("face_index")
        if (face.face_index == 0 and row_face not in (None, 0)) or (face.face_index != 0 and row_face != face.face_index):
            continue
        assets = assets_by_filename.get(str(filename or "").casefold(), [])
        if len(assets) != 1:
            continue
        asset = assets[0]
        if ((asset.get("language") or "").casefold() != language
                or asset.get("article_url") != evidence.get("article_url")
                or str(asset.get("url") or "").casefold().startswith(("http://", "https://")) is False):
            continue
        if str(asset.get("printed_name") or "").strip().casefold() != str(evidence.get("printed_name_exact") or "").strip().casefold():
            continue
        result.append(ImageCandidate(
            printing_id=face.printing_id, face_index=face.face_index,
            language=language, provider="wizards_gallery", url=asset["url"],
            exact_identity=True, expected_set_id=face.set_id, source_set_id=face.set_id,
            expected_collector_number=face.collector_number,
            source_collector_number=face.collector_number,
            identity_evidence=(f"exact reconciliation of filename, printed name, language, edition and provider id; "
                               f"article={asset.get('article_url')}")))
    return result


def order_candidates(candidates: Iterable[ImageCandidate]) -> list[ImageCandidate]:
    seen = set()
    unique = []
    for candidate in candidates:
        if candidate.candidate_key in seen:
            continue
        seen.add(candidate.candidate_key)
        unique.append(candidate)
    return sorted(unique, key=lambda c: ({"fr": 0, "en": 1}.get(c.language, 2),
                                         0 if c.exact_identity else 1,
                                         int(c.source_priority if c.source_priority is not None else 99), c.provider,
                                         QUALITY_ORDER.get(c.declared_quality, 3),
                                         c.url or c.local_path or ""))


def safe_component(value: str, limit: int = 24) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = value.strip(" .")[:limit].rstrip(" .") or "sans-numero"
    stem = value.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"COM[1-9]|LPT[1-9]", stem):
        value = "_" + value
    return value


def image_relative_path(candidate: ImageCandidate, content_sha256: str) -> str:
    number = safe_component(candidate.expected_collector_number or "sans-numero")
    id_part = re.sub(r"[^A-Za-z0-9]", "", candidate.printing_id)[:12] or "unknown"
    language_folder = "images_fr" if candidate.language == "fr" else "cartes anglaises"
    return (f"{language_folder}/{number}__{id_part}__f{candidate.face_index}__"
            f"{candidate.language}__{content_sha256[:8]}.png")


def _validate_identity(candidate: ImageCandidate) -> str | None:
    if candidate.language not in {"fr", "en"}:
        return "unknown_language"
    if not candidate.exact_identity:
        return "identity_unverified"
    if candidate.expected_set_id and candidate.source_set_id != candidate.expected_set_id:
        return "wrong_edition"
    if (candidate.expected_collector_number is not None
            and str(candidate.source_collector_number or "") != str(candidate.expected_collector_number)):
        return "wrong_collector_number"
    status = (candidate.declared_status or "").casefold()
    if status in PLACEHOLDER_STATUSES:
        return "declared_placeholder"
    return None


def validate_image_bytes(data: bytes, candidate: ImageCandidate, max_bytes: int = 20 * 1024 * 1024,
                         max_pixels: int = MAX_DECODED_PIXELS) -> CandidateOutcome:
    identity_problem = _validate_identity(candidate)
    if identity_problem:
        return CandidateOutcome(candidate, "rejected", identity_problem)
    if not data:
        return CandidateOutcome(candidate, "rejected", "empty_content")
    if len(data) > max_bytes:
        return CandidateOutcome(candidate, "rejected", "size_limit_exceeded", source_bytes=len(data))
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if getattr(probe, "is_animated", False) or int(getattr(probe, "n_frames", 1)) > 1:
                return CandidateOutcome(candidate, "rejected", "animated_image")
            width, height = probe.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                return CandidateOutcome(candidate, "rejected", "decoded_pixel_limit", source_width=width,
                                        source_height=height)
            source_format = probe.format
            probe.verify()
        with Image.open(io.BytesIO(data)) as decoded:
            decoded.load()
            if decoded.size != (width, height):
                return CandidateOutcome(candidate, "rejected", "inconsistent_dimensions")
            if "A" in decoded.getbands() and decoded.getchannel("A").getextrema()[1] == 0:
                return CandidateOutcome(candidate, "rejected", "fully_transparent_placeholder",
                                        source_width=width, source_height=height, source_format=source_format)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        return CandidateOutcome(candidate, "rejected", f"decode_error:{type(exc).__name__}")
    except Exception as exc:
        return CandidateOutcome(candidate, "rejected", f"decode_error:{type(exc).__name__}")
    digest = hashlib.sha256(data).hexdigest()
    return CandidateOutcome(candidate, "valid", source_width=width, source_height=height,
                            source_format=source_format, source_bytes=len(data), source_sha256=digest,
                            image_bytes=data)


class ImageStateStore:
    """Persistent per-face/per-language candidate and GET ledger."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS image_attempts (
                task_key TEXT NOT NULL, cycle TEXT NOT NULL, candidate_key TEXT NOT NULL,
                language TEXT NOT NULL, provider TEXT NOT NULL, url TEXT, status TEXT NOT NULL,
                reason TEXT, get_count INTEGER NOT NULL DEFAULT 0, created_utc TEXT NOT NULL,
                updated_utc TEXT NOT NULL,
                PRIMARY KEY(task_key, cycle, candidate_key)
            );
            CREATE TABLE IF NOT EXISTS image_get_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT NOT NULL, cycle TEXT NOT NULL,
                language TEXT NOT NULL, candidate_key TEXT NOT NULL, url TEXT NOT NULL,
                status TEXT NOT NULL, created_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS image_get_count_idx
                ON image_get_events(task_key, cycle, language);
            CREATE TABLE IF NOT EXISTS image_provider_blocks (
                provider TEXT PRIMARY KEY, until_epoch REAL NOT NULL, reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS image_results (
                task_key TEXT NOT NULL, cycle TEXT NOT NULL, result_json TEXT NOT NULL,
                updated_utc TEXT NOT NULL, PRIMARY KEY(task_key, cycle)
            );
        """)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def candidate_count(self, task_key: str, cycle: str, language: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) n FROM image_attempts WHERE task_key=? AND cycle=? AND language=?",
                                (task_key, cycle, language)).fetchone()
        return int(row["n"])

    def get_count(self, task_key: str, cycle: str, language: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) n FROM image_get_events WHERE task_key=? AND cycle=? AND language=?",
                                (task_key, cycle, language)).fetchone()
        return int(row["n"])

    def total_get_count(self, cycle: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) n FROM image_get_events WHERE cycle=?", (cycle,)).fetchone()
        return int(row["n"])

    def existing_attempt(self, task_key: str, cycle: str, candidate_key: str):
        return self.conn.execute("SELECT * FROM image_attempts WHERE task_key=? AND cycle=? AND candidate_key=?",
                                 (task_key, cycle, candidate_key)).fetchone()

    def begin_candidate(self, task_key: str, cycle: str, candidate: ImageCandidate) -> bool:
        now = self._now()
        try:
            self.conn.execute("""INSERT INTO image_attempts
                (task_key,cycle,candidate_key,language,provider,url,status,created_utc,updated_utc)
                VALUES (?,?,?,?,?,?, 'in_progress',?,?)""",
                (task_key, cycle, candidate.candidate_key, candidate.language or "unknown", candidate.provider,
                 candidate.url, now, now))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def set_attempt(self, task_key: str, cycle: str, candidate: ImageCandidate,
                    status: str, reason: str | None = None):
        self.conn.execute("UPDATE image_attempts SET status=?,reason=?,updated_utc=? "
                          "WHERE task_key=? AND cycle=? AND candidate_key=?",
                          (status, reason, self._now(), task_key, cycle, candidate.candidate_key))
        self.conn.commit()

    def resume_budget_deferred(self, task_key: str, cycle: str, candidate: ImageCandidate):
        self.conn.execute("UPDATE image_attempts SET status='in_progress',reason=NULL,updated_utc=? "
                          "WHERE task_key=? AND cycle=? AND candidate_key=? AND status='budget_deferred'",
                          (self._now(), task_key, cycle, candidate.candidate_key))
        self.conn.commit()

    def mark_interrupted_gets(self, task_key: str, cycle: str, candidate: ImageCandidate):
        self.conn.execute("UPDATE image_get_events SET status='interrupted_consumed' "
                          "WHERE task_key=? AND cycle=? AND candidate_key=? AND status='in_progress'",
                          (task_key, cycle, candidate.candidate_key))
        self.conn.commit()

    def reserve_get(self, task_key: str, cycle: str, candidate: ImageCandidate,
                    url: str, max_gets: int) -> bool:
        if self.get_count(task_key, cycle, candidate.language or "") >= max_gets:
            return False
        self.conn.execute("""INSERT INTO image_get_events
            (task_key,cycle,language,candidate_key,url,status,created_utc)
            VALUES (?,?,?,?,?,'in_progress',?)""",
            (task_key, cycle, candidate.language, candidate.candidate_key, url, self._now()))
        self.conn.execute("UPDATE image_attempts SET get_count=get_count+1,updated_utc=? "
                          "WHERE task_key=? AND cycle=? AND candidate_key=?",
                          (self._now(), task_key, cycle, candidate.candidate_key))
        self.conn.commit()
        return True

    def complete_get(self, task_key: str, cycle: str, candidate: ImageCandidate,
                     url: str, status: str):
        row = self.conn.execute("SELECT id FROM image_get_events WHERE task_key=? AND cycle=? "
                                "AND candidate_key=? AND url=? AND status='in_progress' ORDER BY id DESC LIMIT 1",
                                (task_key, cycle, candidate.candidate_key, url)).fetchone()
        if row:
            self.conn.execute("UPDATE image_get_events SET status=? WHERE id=?", (status, row["id"]))
            self.conn.commit()

    def provider_blocked(self, provider: str, now: float | None = None) -> bool:
        row = self.conn.execute("SELECT until_epoch FROM image_provider_blocks WHERE provider=?", (provider,)).fetchone()
        return bool(row and float(row["until_epoch"]) > (time.time() if now is None else now))

    def block_provider(self, provider: str, seconds: float, reason: str):
        until = time.time() + max(0.0, seconds)
        self.conn.execute("INSERT INTO image_provider_blocks(provider,until_epoch,reason) VALUES(?,?,?) "
                          "ON CONFLICT(provider) DO UPDATE SET until_epoch=excluded.until_epoch,reason=excluded.reason",
                          (provider, until, reason))
        self.conn.commit()

    def save_result(self, task_key: str, cycle: str, value: dict):
        self.conn.execute("INSERT INTO image_results(task_key,cycle,result_json,updated_utc) VALUES(?,?,?,?) "
                          "ON CONFLICT(task_key,cycle) DO UPDATE SET result_json=excluded.result_json,updated_utc=excluded.updated_utc",
                          (task_key, cycle, json.dumps(value, ensure_ascii=False, sort_keys=True), self._now()))
        self.conn.commit()


class ImageTransport:
    def __init__(self, state: ImageStateStore, cache_root: str | Path, settings: dict | None = None,
                 session=None, sleep=time.sleep, clock=time.monotonic):
        self.state = state
        self.cache_root = Path(cache_root)
        self.raw_cache = self.cache_root / "raw"
        self.temp_root = self.cache_root / "tmp"
        self.raw_cache.mkdir(parents=True, exist_ok=True)
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.settings = settings or {}
        self.network = self.settings.get("network", self.settings)
        self.image_policy = self.settings.get("image_policy", {})
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self.max_bytes = int(self.network.get("max_image_bytes", 20 * 1024 * 1024))
        self.connect_timeout = int(self.network.get("connect_timeout_seconds", 15))
        self.read_timeout = int(self.network.get("read_timeout_seconds", 45))
        self.max_redirects = int(self.network.get("max_redirects", 2))
        self.user_agent = str(self.network.get("user_agent", "BibliothequeMagicLocal/1.0"))
        self.max_gets = int(self.image_policy.get("max_image_gets_per_language", 3))
        self.max_stream_seconds = int(self.network.get("max_image_download_seconds", 120))
        self.last_request_by_host: dict[str, float] = {}
        self.max_total_gets: int | None = None

    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.raw_cache / f"{key}.bin"

    def _delay(self, provider: str, url: str):
        host = (urlparse(url).hostname or "").casefold()
        if "scryfall" in host:
            interval = int(self.network.get("minimum_scryfall_interval_ms", 600)) / 1000
        elif provider in {"wizards_gallery", "gatherer", "smfcorp", "magiccorporation"}:
            interval = int(self.network.get("minimum_gallery_interval_ms", 1000)) / 1000
        else:
            interval = 0
        last = self.last_request_by_host.get(host)
        if last is not None:
            remaining = interval - (self.clock() - last)
            if remaining > 0:
                self.sleep(remaining)
        self.last_request_by_host[host] = self.clock()

    @staticmethod
    def _retry_after(headers) -> float:
        raw = headers.get("Retry-After") if headers else None
        if raw is None:
            return 60.0
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            try:
                target = parsedate_to_datetime(str(raw))
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return 60.0

    def fetch(self, candidate: ImageCandidate, task_key: str, cycle: str,
              max_gets: int | None = None) -> tuple[bytes | None, int, str | None]:
        if not candidate.url:
            raise ValueError("fetch requires a URL candidate")
        cache_path = self._cache_path(candidate.url)
        if cache_path.is_file():
            return cache_path.read_bytes(), 0, "cache_hit"
        if self.state.provider_blocked(candidate.provider):
            return None, 0, "provider_deferred"
        current_url = candidate.url
        redirects = 0
        request_count = 0
        started = self.clock()
        get_budget = self.max_gets if max_gets is None else min(self.max_gets, max_gets)
        while True:
            if (self.max_total_gets is not None
                    and self.state.total_get_count(cycle) >= self.max_total_gets):
                return None, request_count, "global_get_budget_exhausted"
            self._delay(candidate.provider, current_url)
            if (self.max_total_gets is not None
                    and self.state.total_get_count(cycle) >= self.max_total_gets):
                return None, request_count, "global_get_budget_exhausted"
            if not self.state.reserve_get(task_key, cycle, candidate, current_url, get_budget):
                return None, request_count, "get_budget_exhausted"
            request_count += 1
            try:
                response = self.session.get(current_url, stream=True, allow_redirects=False,
                                            timeout=(self.connect_timeout, self.read_timeout),
                                            headers={"User-Agent": self.user_agent})
            except requests.RequestException as exc:
                self.state.complete_get(task_key, cycle, candidate, current_url,
                                        f"network_error:{type(exc).__name__}")
                return None, request_count, f"network_error:{type(exc).__name__}"
            try:
                status = int(response.status_code)
                if status in {301, 302, 303, 307, 308}:
                    self.state.complete_get(task_key, cycle, candidate, current_url, "redirect")
                    location = response.headers.get("Location")
                    response.close()
                    if not location or redirects >= self.max_redirects:
                        return None, request_count, "redirect_limit_or_missing_location"
                    next_url = urljoin(current_url, location)
                    if urlparse(current_url).scheme == "https" and urlparse(next_url).scheme != "https":
                        return None, request_count, "https_downgrade_rejected"
                    redirects += 1
                    current_url = next_url
                    continue
                self.state.complete_get(task_key, cycle, candidate, current_url, f"http_{status}")
                if status == 403:
                    self.state.block_provider(candidate.provider, 10**12, "HTTP 403 provider disabled")
                    return None, request_count, "provider_disabled_403"
                if status == 429:
                    delay = self._retry_after(response.headers)
                    self.state.block_provider(candidate.provider, delay, "HTTP 429 Retry-After")
                    return None, request_count, "rate_limited"
                if 500 <= status <= 599:
                    return None, request_count, f"http_{status}"
                if status != 200:
                    return None, request_count, f"http_{status}"
                content_type = str(response.headers.get("Content-Type", "")).casefold()
                if "text/html" in content_type or "application/json" in content_type:
                    return None, request_count, "non_image_content_type"
                length = response.headers.get("Content-Length")
                if length:
                    try:
                        if int(length) > self.max_bytes:
                            return None, request_count, "size_limit_exceeded"
                    except ValueError:
                        pass
                temp_path = None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.temp_root, suffix=".part", delete=False) as tmp:
                        temp_path = Path(tmp.name)
                        total = 0
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            if self.clock() - started > self.max_stream_seconds:
                                return None, request_count, "download_time_limit_exceeded"
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > self.max_bytes:
                                return None, request_count, "size_limit_exceeded"
                            tmp.write(chunk)
                    data = temp_path.read_bytes()
                    return data, request_count, None
                finally:
                    response.close()
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)
            except requests.RequestException as exc:
                self.state.complete_get(task_key, cycle, candidate, current_url,
                                        f"network_error:{type(exc).__name__}")
                return None, request_count, f"network_error:{type(exc).__name__}"
            finally:
                try:
                    response.close()
                except Exception:
                    pass

    def cache_validated(self, candidate: ImageCandidate, data: bytes):
        if candidate.url:
            path = self._cache_path(candidate.url)
            if not path.exists():
                temp = path.with_suffix(".tmp")
                temp.write_bytes(data)
                os.replace(temp, path)


def _task_key(printing_id: str, face_index: int) -> str:
    return f"{printing_id}:face:{face_index}"


def _attempt_summary(state: ImageStateStore, task_key: str, cycle: str) -> dict:
    return {language: {
        "candidates": state.candidate_count(task_key, cycle, language),
        "gets": state.get_count(task_key, cycle, language),
    } for language in ("fr", "en")}


def _process_candidate(candidate: ImageCandidate, task_key: str, cycle: str,
                       store: ImageStateStore, transport: ImageTransport,
                       max_candidates: int, max_gets: int) -> CandidateOutcome:
    existing = store.existing_attempt(task_key, cycle, candidate.candidate_key)
    replay_valid = False
    resume_deferred = False
    if existing:
        if existing and existing["status"] == "in_progress":
            store.set_attempt(task_key, cycle, candidate, "interrupted_consumed", "resume_skips_in_progress_attempt")
            store.mark_interrupted_gets(task_key, cycle, candidate)
            return CandidateOutcome(candidate, "skipped", "interrupted_consumed")
        if existing and existing["status"] == "budget_deferred":
            if (transport.max_total_gets is not None
                    and store.total_get_count(cycle) >= transport.max_total_gets):
                return CandidateOutcome(candidate, "skipped", "global_get_budget_exhausted")
            store.resume_budget_deferred(task_key, cycle, candidate)
            resume_deferred = True
        elif existing and existing["status"] == "valid":
            replay_valid = bool(candidate.local_path or
                                (candidate.url and transport._cache_path(candidate.url).is_file()))
            if not replay_valid:
                return CandidateOutcome(candidate, "skipped", "already_examined")
        else:
            return CandidateOutcome(candidate, "skipped", "already_examined")
    language_key = candidate.language or "unknown"
    if not (replay_valid or resume_deferred) and store.candidate_count(task_key, cycle, language_key) >= max_candidates:
        return CandidateOutcome(candidate, "skipped", "candidate_budget_exhausted")
    cache_available = bool(candidate.url and transport._cache_path(candidate.url).is_file())
    if (not (replay_valid or resume_deferred) and candidate.language in {"fr", "en"} and candidate.url
            and not cache_available and store.get_count(task_key, cycle, candidate.language) >= max_gets):
        return CandidateOutcome(candidate, "skipped", "get_budget_exhausted")
    if not (replay_valid or resume_deferred) and not store.begin_candidate(task_key, cycle, candidate):
        return CandidateOutcome(candidate, "skipped", "already_examined")
    reason = _validate_identity(candidate)
    if reason:
        store.set_attempt(task_key, cycle, candidate, "rejected", reason)
        return CandidateOutcome(candidate, "rejected", reason)
    if candidate.local_path:
        try:
            data = Path(candidate.local_path).read_bytes()
        except OSError as exc:
            store.set_attempt(task_key, cycle, candidate, "rejected", f"local_read_error:{type(exc).__name__}")
            return CandidateOutcome(candidate, "rejected", f"local_read_error:{type(exc).__name__}")
        outcome = validate_image_bytes(data, candidate)
        outcome.get_count = 0
        store.set_attempt(task_key, cycle, candidate, outcome.status, outcome.reason)
        return outcome
    data, get_count, fetch_error = transport.fetch(candidate, task_key, cycle, max_gets=max_gets)
    if data is None:
        if fetch_error == "global_get_budget_exhausted":
            store.set_attempt(task_key, cycle, candidate, "budget_deferred", fetch_error)
            return CandidateOutcome(candidate, "skipped", fetch_error, get_count=get_count)
        store.set_attempt(task_key, cycle, candidate, "deferred" if fetch_error == "rate_limited" else "rejected",
                          fetch_error)
        return CandidateOutcome(candidate, "rejected", fetch_error, get_count=get_count)
    outcome = validate_image_bytes(data, candidate, max_bytes=transport.max_bytes)
    outcome.get_count = get_count
    if outcome.status == "valid":
        transport.cache_validated(candidate, data)
    store.set_attempt(task_key, cycle, candidate, outcome.status, outcome.reason)
    return outcome


def _selection_key(outcome: CandidateOutcome):
    return (outcome.area, outcome.short_side,
            1 if outcome.candidate.language == "fr" else 0,
            -int(outcome.candidate.source_priority if outcome.candidate.source_priority is not None else 99),
            outcome.candidate.provider, outcome.candidate.url or outcome.candidate.local_path or "")


def normalize_png(data: bytes, output_path: str | Path,
                  output_size: tuple[int, int] = OUTPUT_SIZE) -> tuple[int, int]:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        oriented = ImageOps.exif_transpose(source)
        rgba = oriented.convert("RGBA")
        rgba.thumbnail(output_size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", output_size, (0, 0, 0, 0))
        left = (output_size[0] - rgba.width) // 2
        top = (output_size[1] - rgba.height) // 2
        canvas.alpha_composite(rgba, (left, top))
        # A one- or two-pixel contain margin is extended from the adjacent card
        # edge so opaque card scans can use RGB. Larger margins and source alpha
        # remain transparent; no card pixels are cropped or stretched.
        padding = (left, top, output_size[0] - left - rgba.width,
                   output_size[1] - top - rgba.height)
        opaque_source = rgba.getchannel("A").getextrema() == (255, 255)
        if opaque_source and max(padding) <= 2:
            rgb_content = rgba.convert("RGB")
            rgb_canvas = Image.new("RGB", output_size)
            rgb_canvas.paste(rgb_content, (left, top))
            if left:
                strip = rgb_content.crop((0, 0, 1, rgba.height)).resize((left, rgba.height), Image.Resampling.NEAREST)
                rgb_canvas.paste(strip, (0, top))
            if padding[2]:
                strip = rgb_content.crop((rgba.width - 1, 0, rgba.width, rgba.height)).resize(
                    (padding[2], rgba.height), Image.Resampling.NEAREST)
                rgb_canvas.paste(strip, (left + rgba.width, top))
            if top:
                strip = rgb_content.crop((0, 0, rgba.width, 1)).resize((output_size[0], top), Image.Resampling.NEAREST)
                rgb_canvas.paste(strip, (0, 0))
            if padding[3]:
                strip = rgb_content.crop((0, rgba.height - 1, rgba.width, rgba.height)).resize(
                    (output_size[0], padding[3]), Image.Resampling.NEAREST)
                rgb_canvas.paste(strip, (0, top + rgba.height))
            rgb_canvas.save(output_path, format="PNG", optimize=True)
        else:
            canvas.save(output_path, format="PNG", optimize=True)
        return rgba.size


def collect_face(candidates: Iterable[ImageCandidate], staging_root: str | Path,
                 store: ImageStateStore, transport: ImageTransport,
                 cycle: str = "default", max_candidates: int = 3, max_gets: int = 3,
                 printing_id: str | None = None, face_index: int = 0,
                 published_image: str | Path | None = None) -> ImageResult:
    candidates = order_candidates(candidates)
    if not candidates:
        if not printing_id:
            raise ValueError("printing_id is required when a physical face has no candidates")
        task_key = _task_key(printing_id, face_index)
    else:
        candidate = candidates[0]
        task_key = _task_key(candidate.printing_id, candidate.face_index)
    prior = store.conn.execute("SELECT result_json FROM image_results WHERE task_key=? AND cycle=?",
                               (task_key, cycle)).fetchone()
    if prior:
        try:
            value = json.loads(prior["result_json"])
            if value.get("status") == "ready" and value.get("path"):
                existing_paths = [Path(staging_root) / value["path"]]
                if published_image is not None:
                    existing_paths.append(Path(published_image))
                for existing in existing_paths:
                    if existing.is_file() and hashlib.sha256(existing.read_bytes()).hexdigest() == value.get("output_sha256"):
                        try:
                            with Image.open(existing) as image:
                                image.load()
                                if image.format == "PNG" and image.size == OUTPUT_SIZE:
                                    return ImageResult(**{key: value[key] for key in ImageResult.__dataclass_fields__})
                        except (OSError, UnidentifiedImageError):
                            continue
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
    outcomes: list[CandidateOutcome] = []
    errors: list[dict] = []
    for source_candidate in candidates:
        if source_candidate.language in {"fr", "en"}:
            continue
        outcome = _process_candidate(source_candidate, task_key, cycle, store, transport,
                                     max_candidates, max_gets)
        if outcome.reason:
            errors.append({"provider": source_candidate.provider,
                           "language": source_candidate.language or "unknown",
                           "reason": outcome.reason, "url": source_candidate.url})
    for language in ("fr", "en"):
        language_candidates = [c for c in candidates if c.language == language]
        for source_candidate in language_candidates:
            outcome = _process_candidate(source_candidate, task_key, cycle, store, transport,
                                         max_candidates, max_gets)
            if outcome.status == "skipped" and outcome.reason == "candidate_budget_exhausted":
                break
            if outcome.status == "skipped" and outcome.reason == "get_budget_exhausted":
                break
            if outcome.status == "skipped" and outcome.reason == "global_get_budget_exhausted":
                break
            if outcome.status == "valid":
                outcomes.append(outcome)
                if outcome.target_quality and language == "fr":
                    selected = outcome
                    reason = "first_target_quality_fr"
                    return _publish(selected, reason, outcomes, candidates, staging_root,
                                    store, task_key, cycle, errors)
                if outcome.target_quality and language == "en":
                    selected = outcome
                    reason = "target_quality_en_after_fr_exhausted"
                    return _publish(selected, reason, outcomes, candidates, staging_root,
                                    store, task_key, cycle, errors)
            elif outcome.reason:
                errors.append({"provider": source_candidate.provider, "language": language,
                               "reason": outcome.reason, "url": source_candidate.url})
    if outcomes:
        selected = max(outcomes, key=_selection_key)
        return _publish(selected, "largest_valid_examined_low_resolution_fallback", outcomes,
                        candidates, staging_root, store, task_key, cycle, errors)
    attempts = _attempt_summary(store, task_key, cycle)
    deferred = any(error["reason"] in {"rate_limited", "provider_deferred"} for error in errors)
    result = ImageResult(status="missing", path=None, selected_language=None, provider=None,
                         source_url=None, source_width=None, source_height=None, source_format=None,
                         source_bytes=None, source_sha256=None, output_sha256=None,
                         final_width=None, final_height=None, quality="missing",
                         choice_reason="no_valid_candidate", retryable=True, attempts=attempts,
                         candidates=[_candidate_summary(c) for c in candidates], errors=errors)
    store.save_result(task_key, cycle, {**result.to_dict(), "deferred": deferred})
    return result


def _candidate_summary(candidate: ImageCandidate) -> dict:
    return {"language": candidate.language, "provider": candidate.provider,
            "url": candidate.url, "local_path": candidate.local_path,
            "declared_quality": candidate.declared_quality,
            "exact_identity": candidate.exact_identity,
            "identity_evidence": candidate.identity_evidence}


def _publish(selected: CandidateOutcome, reason: str, outcomes: list[CandidateOutcome],
             candidates: list[ImageCandidate], staging_root: str | Path,
             store: ImageStateStore, task_key: str, cycle: str, errors: list[dict]) -> ImageResult:
    assert selected.image_bytes is not None and selected.source_sha256
    relative = image_relative_path(selected.candidate, selected.source_sha256)
    staging_root = Path(staging_root)
    output_path = staging_root / relative
    if len(str(output_path.resolve())) > 240:
        raise ImageCollectionError("Normalized image path would exceed the 240-character Windows limit")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output_path.parent, suffix=".png.part", delete=False) as tmp:
        normalized_temp = Path(tmp.name)
    try:
        normalize_png(selected.image_bytes, normalized_temp)
        normalized_bytes = normalized_temp.read_bytes()
        if output_path.exists():
            if output_path.read_bytes() != normalized_bytes:
                png_sha = hashlib.sha256(normalized_bytes).hexdigest()
                relative = relative[:-4] + "__" + png_sha[:8] + ".png"
                output_path = staging_root / relative
                if len(str(output_path.resolve())) > 240:
                    raise ImageCollectionError("Unique normalized image path exceeds the 240-character limit")
                if output_path.exists() and output_path.read_bytes() != normalized_bytes:
                    raise ImageCollectionError("Content-addressed image path collision")
            if not output_path.exists():
                os.replace(normalized_temp, output_path)
        else:
            os.replace(normalized_temp, output_path)
    finally:
        normalized_temp.unlink(missing_ok=True)
    with Image.open(output_path) as final:
        final.load()
        final_width, final_height = final.size
    quality = "target" if selected.target_quality else "low_resolution_fallback"
    attempts = _attempt_summary(store, task_key, cycle)
    output_sha = hashlib.sha256(output_path.read_bytes()).hexdigest()
    result = ImageResult(status="ready", path=relative.replace("\\", "/"),
                         selected_language=selected.candidate.language, provider=selected.candidate.provider,
                         source_url=selected.candidate.url, source_width=selected.source_width,
                         source_height=selected.source_height, source_format=selected.source_format,
                         source_bytes=selected.source_bytes, source_sha256=selected.source_sha256,
                         output_sha256=output_sha, final_width=final_width, final_height=final_height,
                         quality=quality, choice_reason=reason, retryable=False, attempts=attempts,
                         candidates=[_candidate_summary(c) for c in candidates], errors=errors)
    value = {**result.to_dict(), "selected_at_utc": datetime.now(timezone.utc).isoformat(),
             "valid_candidates": [{"language": item.candidate.language, "provider": item.candidate.provider,
                                   "width": item.source_width, "height": item.source_height,
                                   "source_sha256": item.source_sha256} for item in outcomes]}
    store.save_result(task_key, cycle, value)
    return result
