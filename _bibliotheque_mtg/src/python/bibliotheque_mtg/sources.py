from __future__ import annotations

import email.utils
import gzip
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
import uuid
import zlib
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator, FormatChecker

SCRYFALL_BULK = "https://api.scryfall.com/bulk-data"
SCRYFALL_SETS = "https://api.scryfall.com/sets"
WIZARDS_ARCHIVE = "https://magic.wizards.com/fr/news/card-image-gallery?page={page}"
WIZARDS_PRODUCT_ARCHIVE = "https://magic.wizards.com/fr/products/card-set-archive"
WIZARDS_ORIGINS = "https://magic.wizards.com/fr/news/card-image-gallery/magicorigins"
WIZARDS_MH3 = "https://magic.wizards.com/fr/products/modern-horizons-3/card-image-gallery"
MAX_PAGINATION_PAGES = 1000
MAX_GALLERY_PAGES = 500
MAX_GALLERY_INDEX_PAGES = 2
MAX_HTTP_ATTEMPTS = 3
OFFICIAL_FRENCH_OVERRIDES = Path(__file__).with_name("data") / "official_french_text_overrides.json"


class SourceError(RuntimeError):
    """Une source a échoué ; l'échec doit rester visible dans les rapports."""


class SourceBlocked(SourceError):
    pass


class SourceRateLimited(SourceError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SourceHTTPError(SourceError):
    pass


class SourceTransportError(SourceError):
    pass


class SourceFormatError(SourceError):
    pass


class PaginationError(SourceError):
    pass


class ArchiveIntegrityError(SourceError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".part")
    with temp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def atomic_json(path: Path, value: object) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class HttpClient:
    """Client HTTP borné pour les métadonnées. Les images auront leur propre budget."""

    def __init__(self, user_agent: str, *, session=None, sleep=time.sleep, min_intervals=None,
                 timeout=(15, 45), max_attempts=MAX_HTTP_ATTEMPTS, max_inline_retry_after=30):
        self.session = session or requests.Session()
        self.session.max_redirects = 3
        self.sleep = sleep
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.max_inline_retry_after = max_inline_retry_after
        self.min_intervals = min_intervals or {
            "api.scryfall.com": 0.6, "data.scryfall.io": 0.1, "magic.wizards.com": 1.0
        }
        self.user_agent = user_agent
        self.last_request = {}
        self.calls = Counter()

    def get(self, url: str, *, stream=False, extra_headers=None):
        host = urllib.parse.urlparse(url).hostname or ""
        headers = {"User-Agent": self.user_agent,
                   "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
        if extra_headers:
            headers.update(extra_headers)
        for attempt in range(self.max_attempts):
            wait = self.min_intervals.get(host, 0.0) - (time.monotonic() - self.last_request.get(host, 0.0))
            if wait > 0:
                self.sleep(wait)
            self.last_request[host] = time.monotonic()
            self.calls[host] += 1
            try:
                response = self.session.get(url, headers=headers, timeout=self.timeout,
                                            stream=stream, allow_redirects=True)
            except requests.RequestException as exc:
                if attempt + 1 == self.max_attempts:
                    raise SourceTransportError(f"Transport error for {url}: {exc}") from exc
                self.sleep(min(2 ** attempt, 5))
                continue
            status = response.status_code
            if status == 304:
                return response
            if status == 403:
                raise SourceBlocked(f"HTTP 403 for {url}; source disabled without bypass")
            if status == 429:
                delay = retry_after_seconds(response.headers.get("Retry-After"))
                if delay is None:
                    delay = min(2 ** attempt, 5)
                if attempt + 1 == self.max_attempts or delay > self.max_inline_retry_after:
                    raise SourceRateLimited(f"HTTP 429 for {url}; retry deferred", delay)
                self.sleep(delay)
                continue
            if 500 <= status < 600:
                if attempt + 1 == self.max_attempts:
                    raise SourceHTTPError(f"HTTP {status} for {url}")
                self.sleep(min(2 ** attempt, 5))
                continue
            if status >= 400:
                raise SourceHTTPError(f"HTTP {status} for {url}")
            return response
        raise SourceTransportError(f"No response for {url}")


def fetch_cached(client: HttpClient, url: str, path: Path, *, expected: str):
    """Fetch JSON/HTML with conditional requests and retain the exact public response."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta_path = path.with_name(path.name + ".meta.json")
    old_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    extra = {}
    if path.exists() and old_meta.get("etag"):
        extra["If-None-Match"] = old_meta["etag"]
    if path.exists() and old_meta.get("last_modified"):
        extra["If-Modified-Since"] = old_meta["last_modified"]
    response = client.get(url, extra_headers=extra)
    if response.status_code == 304:
        if not path.exists():
            raise SourceFormatError(f"HTTP 304 without cached body: {url}")
        return path.read_bytes(), old_meta, False
    body = response.content
    if not body:
        raise SourceFormatError(f"Empty HTTP 200 response: {url}")
    content_type = response.headers.get("Content-Type", "").lower()
    if expected == "json":
        if "html" in content_type or body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise SourceFormatError(f"HTTP 200 returned HTML where JSON was expected: {url}")
        try:
            json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceFormatError(f"Invalid JSON response from {url}: {exc}") from exc
    elif expected == "html":
        if "json" in content_type and not body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise SourceFormatError(f"HTTP 200 returned JSON where HTML was expected: {url}")
        if b"<html" not in body[:4096].lower() and b"<!doctype html" not in body[:4096].lower():
            raise SourceFormatError(f"HTTP 200 is not an HTML document: {url}")
    digest = hashlib.sha256(body).hexdigest()
    previous = old_meta.get("sha256")
    if not path.exists() or previous != digest:
        atomic_bytes(path, body)
    meta = {"requested_url": url, "final_url": getattr(response, "url", None) or url,
            "retrieved_at_utc": utc_now(), "http_status": response.status_code,
            "content_type": content_type, "bytes": len(body), "sha256": digest,
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "changed": previous != digest}
    atomic_json(meta_path, meta)
    return body, meta, True


def iter_api_pages(client: HttpClient, first_url: str, *, max_pages=MAX_PAGINATION_PAGES):
    seen_urls, seen_fingerprints = set(), set()
    url = first_url
    for _ in range(max_pages):
        if url in seen_urls:
            raise PaginationError(f"Repeated pagination URL: {url}")
        seen_urls.add(url)
        response = client.get(url)
        try:
            payload = response.json()
        except (ValueError, AttributeError) as exc:
            raise SourceFormatError(f"Invalid paginated JSON from {url}: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise SourceFormatError(f"Unexpected page structure from {url}")
        fingerprint = hashlib.sha256(json.dumps(payload["data"], sort_keys=True).encode()).hexdigest()
        if fingerprint in seen_fingerprints:
            raise PaginationError(f"Repeated page content at {url}; refusing incomplete catalogue")
        seen_fingerprints.add(fingerprint)
        yield url, payload
        if payload.get("has_more") is False:
            return
        if payload.get("has_more") is not True:
            raise SourceFormatError(f"Missing boolean has_more on {url}")
        next_page = payload.get("next_page")
        if not isinstance(next_page, str) or not next_page:
            raise SourceFormatError(f"has_more=true without next_page on {url}")
        url = urllib.parse.urljoin(url, next_page)
    raise PaginationError(f"Pagination safety cap reached ({max_pages} pages)")


def all_cards_descriptor(payload: object) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise SourceFormatError("Scryfall /bulk-data response has no data array")
    matches = [row for row in payload["data"] if isinstance(row, dict) and row.get("type") == "all_cards"]
    if len(matches) != 1:
        raise SourceFormatError(f"Expected exactly one all_cards descriptor, got {len(matches)}")
    item = matches[0]
    parsed = urllib.parse.urlparse(item.get("jsonl_download_uri") or "")
    if parsed.scheme != "https" or parsed.hostname != "data.scryfall.io" or not parsed.path.endswith(".jsonl.gz"):
        raise SourceFormatError("all_cards lacks trusted HTTPS jsonl_download_uri")
    if not item.get("updated_at") or not isinstance(item.get("compressed_size"), int):
        raise SourceFormatError("all_cards lacks updated_at/compressed_size")
    return item


def validate_gzip_jsonl(path: Path) -> dict:
    languages = Counter()
    rows = 0
    try:
        with gzip.open(path, "rb") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    raise ArchiveIntegrityError(f"Blank JSONL line {number}")
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ArchiveIntegrityError(f"Invalid JSONL at line {number}: {exc}") from exc
                if not isinstance(record, dict) or record.get("object") != "card" or not isinstance(record.get("id"), str):
                    raise ArchiveIntegrityError(f"Unexpected card object at JSONL line {number}")
                lang = record.get("lang")
                if not isinstance(lang, str) or not lang:
                    raise ArchiveIntegrityError(f"Card without language at JSONL line {number}")
                languages[lang] += 1
                rows += 1
    except (OSError, EOFError, zlib.error) as exc:
        raise ArchiveIntegrityError(f"Invalid or truncated gzip JSONL: {exc}") from exc
    if rows == 0:
        raise ArchiveIntegrityError("All Cards archive contains no rows")
    return {"rows": rows, "languages": dict(sorted(languages.items()))}


def download_bulk(client: HttpClient, descriptor: dict, archive_dir: Path, *, progress=None):
    archive_dir.mkdir(parents=True, exist_ok=True)
    name = Path(urllib.parse.urlparse(descriptor["jsonl_download_uri"]).path).name
    archive, meta_path = archive_dir / name, archive_dir / (name + ".meta.json")
    previous = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if archive.exists() and previous.get("updated_at") == descriptor["updated_at"]:
        size, digest = archive.stat().st_size, sha256_file(archive)
        if size == descriptor["compressed_size"] and digest == previous.get("sha256"):
            validation = previous.get("validation") or validate_gzip_jsonl(archive)
            if progress:
                progress(1.0, "Archive Scryfall déjà à jour")
            return archive, {**previous, "validation": validation, "reused": True}, False
    response = client.get(descriptor["jsonl_download_uri"], stream=True)
    temp, digest, size = archive.with_name(archive.name + ".part"), hashlib.sha256(), 0
    expected = max(1, int(descriptor["compressed_size"]))
    if progress:
        progress(0.0, "Téléchargement des cartes Scryfall…")
    try:
        with temp.open("wb") as output:
            next_progress = 50 * 1024 * 1024
            for block in response.iter_content(chunk_size=1024 * 1024):
                if not block:
                    continue
                output.write(block); digest.update(block); size += len(block)
                if progress:
                    progress(min(size / expected, 0.95),
                             f"{size / (1024 * 1024):.1f} / {expected / (1024 * 1024):.1f} Mo")
                if size >= next_progress:
                    print(f"Scryfall All Cards: {size / (1024 * 1024):.0f} MiB reçus", flush=True)
                    next_progress += 50 * 1024 * 1024
            output.flush(); os.fsync(output.fileno())
        if size != descriptor["compressed_size"]:
            raise ArchiveIntegrityError(f"Bulk size mismatch: expected {descriptor['compressed_size']}, got {size}")
        if progress:
            progress(0.96, "Vérification de l'archive Scryfall…")
        validation = validate_gzip_jsonl(temp)
        os.replace(temp, archive)
        if progress:
            progress(1.0, "Archive Scryfall prête")
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    meta = {"url": descriptor["jsonl_download_uri"], "updated_at": descriptor["updated_at"],
            "compressed_size": size, "sha256": digest.hexdigest(), "validation": validation,
            "downloaded_at_utc": utc_now(), "reused": False}
    atomic_json(meta_path, meta)
    return archive, meta, True


def _fetch_set_pages(client: HttpClient, archive_dir: Path):
    items, pages, seen_urls, seen_hashes = [], [], set(), set()
    url = SCRYFALL_SETS
    for page_number in range(1, MAX_PAGINATION_PAGES + 1):
        if url in seen_urls:
            raise PaginationError(f"Repeated /sets URL: {url}")
        seen_urls.add(url)
        path = archive_dir / f"sets-page-{page_number:04d}.json"
        body, meta, fetched = fetch_cached(client, url, path, expected="json")
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or not isinstance(payload.get("has_more"), bool):
            raise SourceFormatError(f"Unexpected /sets structure at {url}")
        fingerprint = hashlib.sha256(json.dumps(payload["data"], sort_keys=True).encode()).hexdigest()
        if fingerprint in seen_hashes:
            raise PaginationError(f"Repeated /sets page content at {url}")
        seen_hashes.add(fingerprint)
        if any(not isinstance(item, dict) for item in payload["data"]):
            raise SourceFormatError(f"Non-object set item at {url}")
        items.extend(payload["data"])
        pages.append({"url": url, "path": str(path), "sha256": meta["sha256"],
                      "items": len(payload["data"]), "http_cached": not fetched})
        if payload["has_more"] is False:
            return items, pages
        next_page = payload.get("next_page")
        if not isinstance(next_page, str) or not next_page:
            raise SourceFormatError(f"/sets has_more without next_page at {url}")
        url = urllib.parse.urljoin(url, next_page)
    raise PaginationError(f"/sets safety cap reached ({MAX_PAGINATION_PAGES})")


def _set_reason(item: dict, config: dict, as_of: date):
    exclusions = {row["provider_set_id"]: row["reason"]
                  for row in config["scope"].get("explicit_exclusions", [])}
    if item.get("id") in exclusions:
        return "excluded", exclusions[item["id"]]
    released = item.get("released_at")
    if not released:
        return "deferred", "date de publication inconnue"
    try:
        release_date = date.fromisoformat(released[:10])
    except (ValueError, TypeError):
        return "deferred", "date de publication invalide"
    if release_date > as_of and not config["scope"]["include_unreleased"]:
        return "deferred", "édition non publiée à la date de référence"
    if item.get("digital", False) and not config["scope"]["include_digital"]:
        return "excluded", "édition numérique exclue par configuration"
    if not item.get("digital", False) and not config["scope"]["include_paper"]:
        return "excluded", "édition papier exclue par configuration"
    kind = item.get("set_type")
    if kind == "promo" and not config["scope"]["include_promos"]:
        return "excluded", "promotions exclues par configuration"
    if kind == "token" and not config["scope"]["include_tokens"]:
        return "excluded", "jetons exclus par configuration"
    return "eligible", None


def build_catalogue_db(archive: Path, set_items: list[dict], output: Path, config: dict,
                       bulk_meta: dict, sets_sha: str, as_of: date, *, progress=None):
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".part")
    temp.unlink(missing_ok=True)
    conn = sqlite3.connect(temp)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript("""
      CREATE TABLE sets(id TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL, set_type TEXT NOT NULL,
        released_at TEXT, digital INTEGER NOT NULL, card_count INTEGER, eligibility TEXT NOT NULL,
        exclusion_reason TEXT, raw_json TEXT NOT NULL);
      CREATE INDEX sets_code_idx ON sets(code);
      CREATE TABLE cards(id TEXT PRIMARY KEY, oracle_id TEXT, lang TEXT NOT NULL, set_id TEXT, set_code TEXT NOT NULL,
        collector_number TEXT NOT NULL, layout TEXT, name TEXT NOT NULL, type_line TEXT, mana_cost TEXT,
        mana_value REAL, colors_json TEXT, color_identity_json TEXT, rarity TEXT, artist_json TEXT,
        printed_text TEXT, oracle_text TEXT, faces_json TEXT, image_uris_json TEXT, raw_json TEXT NOT NULL);
      CREATE INDEX cards_set_num_lang_idx ON cards(set_id, collector_number, lang);
      CREATE INDEX cards_oracle_idx ON cards(oracle_id);
      CREATE TABLE language_counts(lang TEXT PRIMARY KEY, record_count INTEGER NOT NULL);
      CREATE TABLE source_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    categories, statuses, codes = Counter(), Counter(), Counter()
    excluded, set_ids = [], set()
    for item in set_items:
        if any(not isinstance(item.get(k), str) or not item.get(k) for k in ("id", "code", "name", "set_type")):
            conn.close(); temp.unlink(missing_ok=True)
            raise SourceFormatError(f"Set record lacks stable identity fields: {item!r}")
        if item["id"] in set_ids:
            conn.close(); temp.unlink(missing_ok=True)
            raise SourceFormatError(f"Duplicate Scryfall set id: {item['id']}")
        set_ids.add(item["id"]); codes[item["code"]] += 1
        status, reason = _set_reason(item, config, as_of)
        statuses[status] += 1; categories[item["set_type"]] += 1
        if status != "eligible":
            excluded.append({"provider_set_id": item["id"], "code": item["code"], "name": item["name"],
                             "status": status, "reason": reason})
        conn.execute("INSERT INTO sets VALUES (?,?,?,?,?,?,?,?,?,?)", (
            item["id"], item["code"], item["name"], item["set_type"], item.get("released_at"),
            int(bool(item.get("digital", False))), item.get("card_count"), status, reason,
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))))
    total_rows = int((bulk_meta.get("validation") or {}).get("rows") or 0)
    if progress:
        progress(0.0, "Construction de la base source…")
    languages, rows, retained, missing_set_id = Counter(), 0, 0, 0
    try:
        with gzip.open(archive, "rb") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    card = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ArchiveIntegrityError(f"Invalid JSONL during import at line {number}: {exc}") from exc
                rows += 1
                if progress and total_rows and number % 5000 == 0:
                    progress(min(number / total_rows, 1.0), f"{number} / {total_rows} lignes")
                lang = card["lang"]; languages[lang] += 1
                if lang not in ("fr", "en"):
                    continue
                if not card.get("set_id"):
                    missing_set_id += 1
                faces = card.get("card_faces") or []
                values = (card["id"], card.get("oracle_id"), lang, card.get("set_id"), card.get("set", ""),
                          str(card.get("collector_number", "")), card.get("layout"), card.get("name", ""),
                          card.get("type_line"), card.get("mana_cost"), card.get("cmc"),
                          json.dumps(card.get("colors", []), ensure_ascii=False),
                          json.dumps(card.get("color_identity", []), ensure_ascii=False), card.get("rarity"),
                          json.dumps(card.get("artist", []), ensure_ascii=False), card.get("printed_text"),
                          card.get("oracle_text"), json.dumps(faces, ensure_ascii=False),
                          json.dumps(card.get("image_uris"), ensure_ascii=False),
                          json.dumps(card, ensure_ascii=False, separators=(",", ":")))
                try:
                    conn.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
                except sqlite3.IntegrityError as exc:
                    raise SourceFormatError(f"Duplicate provider card id {card['id']} at JSONL line {number}") from exc
                retained += 1
                if rows % 5000 == 0:
                    conn.commit()
        for lang, count in languages.items():
            conn.execute("INSERT INTO language_counts VALUES (?,?)", (lang, count))
        if rows != sum(languages.values()):
            raise SourceFormatError("Raw records do not reconcile with language counts")
        unmatched = conn.execute("SELECT COUNT(*) FROM cards c LEFT JOIN sets s ON c.set_id=s.id WHERE c.set_id IS NOT NULL AND s.id IS NULL").fetchone()[0]
        scope = {"as_of_date": as_of.isoformat(), "provider": "scryfall", "bulk_updated_at": bulk_meta["updated_at"],
                 "sets_total": len(set_items), "sets_eligible": statuses["eligible"],
                 "sets_deferred": statuses["deferred"], "sets_explicitly_excluded": statuses["excluded"],
                 "set_categories": dict(sorted(categories.items())),
                 "set_code_collisions": {k: v for k, v in sorted(codes.items()) if v > 1},
                 "card_lines_total": rows, "normalized_fr_en_records": retained,
                 "other_language_records_archived_only": rows-retained,
                 "language_counts": dict(sorted(languages.items())), "invalid_records": 0,
                 "cards_without_set_id": missing_set_id, "unmatched_set_id_references": unmatched,
                 "excluded_or_deferred_sets": excluded, "sets_sha256": sets_sha,
                 "bulk_sha256": bulk_meta["sha256"]}
        for key, value in (("bulk_updated_at", bulk_meta["updated_at"]),
                           ("bulk_sha256", bulk_meta["sha256"]), ("sets_sha256", sets_sha),
                           ("as_of_date", as_of.isoformat())):
            conn.execute("INSERT INTO source_meta VALUES (?,?)", (key, value))
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SourceFormatError(f"SQLite integrity check failed: {integrity}")
        conn.close(); os.replace(temp, output)
        if progress:
            progress(1.0, "Base source construite")
        return scope
    except Exception:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        temp.unlink(missing_ok=True)
        raise


def gallery_article_links(html: bytes, base_url: str) -> list[dict]:
    soup, rows = BeautifulSoup(html, "html.parser"), {}
    for anchor in soup.select("a[href]"):
        absolute = urllib.parse.urljoin(base_url, anchor.get("href", ""))
        parsed = urllib.parse.urlparse(absolute); path = parsed.path.rstrip("/")
        if parsed.hostname != "magic.wizards.com":
            continue
        news = path.startswith("/fr/news/card-image-gallery/") and path != "/fr/news/card-image-gallery"
        product = path.startswith("/fr/products/") and path.endswith("/card-image-gallery")
        if news or product:
            canonical = urllib.parse.urlunparse(("https", parsed.netloc, path, "", "", ""))
            rows.setdefault(canonical, {"url": canonical, "title": anchor.get_text(" ", strip=True) or None})
    return sorted(rows.values(), key=lambda row: row["url"])


def gallery_product_archive_entries(html: bytes, base_url: str = WIZARDS_PRODUCT_ARCHIVE) -> dict:
    """Extract gallery URLs and titled product-page links from the French product archive."""
    soup = BeautifulSoup(html, "html.parser")
    text = str(soup)
    # The product archive currently serializes gallery URLs in JSON with escaped slashes.
    text = text.replace(r"\u002F", "/").replace(r"\/", "/").replace(r"\u0026", "&")
    soup, rows, products = BeautifulSoup(text, "html.parser"), {}, {}
    candidates = [anchor.get("href", "") for anchor in soup.select("a[href]")]
    candidates.extend(re.findall(r"(?<![\w.-])(?:https?://magic\.wizards\.com)?/fr/products/[^\s\"'<>\\]+/card-image-gallery(?:\?[^\s\"'<>\\]*)?", text))
    for anchor in soup.select("a[href]"):
        if anchor.find_parent(("header", "nav", "footer")):
            continue
        absolute = urllib.parse.urljoin(base_url, anchor.get("href", ""))
        parsed = urllib.parse.urlparse(absolute)
        path = parsed.path.rstrip("/")
        if parsed.hostname != "magic.wizards.com" or not re.fullmatch(r"/fr/products/(?:[a-z0-9-]+/)*[a-z0-9-]+", path):
            continue
        if path.endswith("/card-image-gallery"):
            continue
        canonical = urllib.parse.urlunparse(("https", parsed.netloc.lower(), path, "", "", ""))
        if canonical == WIZARDS_PRODUCT_ARCHIVE:
            continue
        title = anchor.get("title") or anchor.get_text(" ", strip=True) or None
        row = products.setdefault(canonical, {"url": canonical, "title": title})
        if not row["title"] and title:
            row["title"] = title
    for candidate in candidates:
        absolute = urllib.parse.urljoin(base_url, candidate)
        parsed = urllib.parse.urlparse(absolute)
        path = parsed.path.rstrip("/")
        if parsed.hostname != "magic.wizards.com" or not re.fullmatch(
                r"/fr/products/(?:[a-z0-9-]+/)*[a-z0-9-]+/card-image-gallery", path):
            continue
        canonical = urllib.parse.urlunparse(("https", parsed.netloc.lower(), path, "", "", ""))
        rows.setdefault(canonical, {"url": canonical, "title": None, "discovered_from": "product_archive"})
    return {"gallery_links": sorted(rows.values(), key=lambda row: row["url"]),
            "product_pages": sorted(products.values(), key=lambda row: row["url"])}


def gallery_product_archive_links(html: bytes, base_url: str = WIZARDS_PRODUCT_ARCHIVE) -> list[dict]:
    """Extract French gallery URLs published in Wizards' product archive HTML/embedded JSON."""
    return gallery_product_archive_entries(html, base_url)["gallery_links"]


def extract_official_card_assets(html: bytes, page_url: str) -> list[dict]:
    soup, assets = BeautifulSoup(html, "html.parser"), {}
    for image in soup.select("img[src], img[data-src]"):
        src = image.get("src") or image.get("data-src")
        if not src:
            continue
        absolute = urllib.parse.urljoin(page_url, src); parsed = urllib.parse.urlparse(absolute)
        if parsed.hostname != "media.wizards.com":
            continue
        filename = Path(parsed.path).name
        match = re.match(r"^(fr|en)_", filename, re.IGNORECASE)
        assets[absolute] = {"url": absolute, "filename": filename,
                            "printed_name": image.get("alt", "").strip() or None,
                            "language": match.group(1).lower() if match else "unknown"}
    return sorted(assets.values(), key=lambda row: row["filename"])


def _load_config(path: Path, ops_root: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        schema = json.loads((ops_root / "schemas" / "config.schema.json").read_text(encoding="utf-8"))
        errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(config))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceFormatError(f"Invalid config or schema: {exc}") from exc
    if errors:
        raise SourceFormatError("Configuration invalid: " + "; ".join(error.message for error in errors[:10]))
    for key, value in config["paths"].items():
        configured = Path(value)
        if not configured.is_absolute():
            config["paths"][key] = str((path.parent / configured).resolve())
    return config


def rebuild_from_archives(config_path: Path, *, as_of: date | None = None) -> dict:
    """Rebuild the local metadata DB from verified archives without creating an HTTP client."""
    config_path = config_path.resolve()
    ops_root = config_path.parent
    config = _load_config(config_path, ops_root)
    bulk_root = ops_root / "sources" / "scryfall" / "bulk"
    sets_root = ops_root / "sources" / "scryfall" / "sets"
    descriptor_path = bulk_root / "bulk-data.json"
    descriptor_body = descriptor_path.read_bytes()
    descriptor = all_cards_descriptor(json.loads(descriptor_body))
    bulk_archive = bulk_root / Path(urllib.parse.urlparse(descriptor["jsonl_download_uri"]).path).name
    bulk_meta_path = bulk_archive.with_name(bulk_archive.name + ".meta.json")
    if not bulk_archive.exists() or not bulk_meta_path.exists():
        raise SourceFormatError("Verified All Cards archive is absent from the local source cache")
    bulk_meta = json.loads(bulk_meta_path.read_text(encoding="utf-8"))
    if sha256_file(bulk_archive) != bulk_meta.get("sha256"):
        raise ArchiveIntegrityError("Cached All Cards archive failed its SHA-256 check")
    validation = validate_gzip_jsonl(bulk_archive)
    if validation["rows"] != bulk_meta.get("validation", {}).get("rows"):
        raise ArchiveIntegrityError("Cached All Cards archive row count differs from its manifest")
    items, page_hashes = [], []
    page_number = 1
    while True:
        path = sets_root / f"sets-page-{page_number:04d}.json"
        meta_path = path.with_name(path.name + ".meta.json")
        if not path.exists() or not meta_path.exists():
            raise SourceFormatError(f"Verified /sets page {page_number} is missing")
        body = path.read_bytes()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(body).hexdigest()
        if digest != meta.get("sha256"):
            raise ArchiveIntegrityError(f"Cached /sets page {page_number} SHA-256 mismatch")
        payload = json.loads(body)
        if not isinstance(payload.get("data"), list) or not isinstance(payload.get("has_more"), bool):
            raise SourceFormatError(f"Cached /sets page {page_number} has an unexpected structure")
        items.extend(payload["data"]); page_hashes.append(digest)
        if not payload["has_more"]:
            break
        page_number += 1
        if page_number > MAX_PAGINATION_PAGES:
            raise PaginationError("Offline /sets archive exceeds the documented page limit")
    sets_sha = hashlib.sha256("".join(page_hashes).encode()).hexdigest()
    scope = build_catalogue_db(bulk_archive, items, ops_root / "catalogue.sqlite", config,
                               bulk_meta, sets_sha, as_of or date.today())
    atomic_json(ops_root / "reports" / "catalogue_scope.json", scope)
    return scope


def _fetch_gallery(client: HttpClient, archive_root: Path) -> dict:
    listing_dir, article_dir = archive_root / "listing", archive_root / "articles"
    listing_dir.mkdir(parents=True, exist_ok=True); article_dir.mkdir(parents=True, exist_ok=True)
    listed, pages, seen_hashes, seen_urls = [], [], set(), set()
    termination = "safety_cap_500"
    page_num, url = 1, WIZARDS_ARCHIVE.format(page=1)
    for _ in range(MAX_GALLERY_PAGES):
        if url in seen_urls:
            termination = "repeated_page_url"; break
        seen_urls.add(url)
        body, meta, fetched = fetch_cached(client, url, listing_dir / f"page-{page_num:04d}.html", expected="html")
        digest = meta["sha256"]
        if digest in seen_hashes:
            termination = "repeated_page_content"; break
        seen_hashes.add(digest)
        links = gallery_article_links(body, url)
        if not links:
            termination = "no_gallery_links"; break
        listed.extend(links)
        soup = BeautifulSoup(body, "html.parser")
        next_link = soup.select_one('a[rel="next"], link[rel="next"]')
        explicit_next = urllib.parse.urljoin(url, next_link["href"]) if next_link and next_link.get("href") else None
        pages.append({"page": page_num, "url": url, "sha256": digest, "bytes": len(body),
                      "links": len(links), "http_cached": not fetched,
                      "explicit_next": explicit_next})
        if page_num % 25 == 0:
            print(f"Galeries Wizards : page {page_num}, {len(listed)} liens relevés", flush=True)
        if explicit_next:
            next_url = explicit_next
            query = urllib.parse.parse_qs(urllib.parse.urlparse(next_url).query)
            try:
                page_num = int(query.get("page", [str(page_num + 1)])[0])
            except ValueError:
                page_num += 1
            url = next_url
        else:
            # The public archive exposes a stable ?page=N route but omits rel=next.
            page_num += 1
            url = WIZARDS_ARCHIVE.format(page=page_num)
    if len(pages) == MAX_GALLERY_PAGES and termination == "safety_cap_500":
        termination = "safety_cap_500"
    unique = {row["url"]: row for row in listed}
    index_pages = []
    product_pages = []
    index_url = WIZARDS_PRODUCT_ARCHIVE
    for index_num in range(1, MAX_GALLERY_INDEX_PAGES + 1):
        body, meta, fetched = fetch_cached(
            client, index_url, listing_dir / f"product-archive-{index_num:02d}.html", expected="html")
        index_data = gallery_product_archive_entries(body, index_url)
        links = index_data["gallery_links"]
        index_pages.append({"url": index_url, "sha256": meta["sha256"], "bytes": len(body),
                            "gallery_links": len(links), "product_pages": len(index_data["product_pages"]),
                            "http_cached": not fetched})
        for row in links:
            unique.setdefault(row["url"], row)
        if index_num == 1:
            product_pages = {row["url"]: row for row in index_data["product_pages"]}
        else:
            product_pages.update({row["url"]: row for row in index_data["product_pages"]})
        # Wizards currently serves the archive as one document; the hard limit leaves
        # room for a future explicit pagination link without unbounded discovery.
        soup = BeautifulSoup(body, "html.parser")
        next_link = soup.select_one('a[rel="next"], link[rel="next"]')
        if not next_link or not next_link.get("href"):
            break
        next_url = urllib.parse.urljoin(index_url, next_link["href"])
        if next_url in {row["url"] for row in index_pages}:
            break
        index_url = next_url
    article_results = []
    for name, url in (("magic_origins", WIZARDS_ORIGINS), ("modern_horizons_3", WIZARDS_MH3)):
        body, meta, fetched = fetch_cached(client, url, article_dir / f"{name}.html", expected="html")
        assets = extract_official_card_assets(body, url)
        article_results.append({"name": name, "url": url, "path": str(article_dir / f"{name}.html"),
                                "http_status": meta["http_status"], "sha256": meta["sha256"],
                                "bytes": len(body), "official_card_assets": len(assets), "assets": assets,
                                "http_cached": not fetched})
    return {"archive_base": WIZARDS_ARCHIVE.format(page=1), "requested_initial_pages": list(range(1, 27)),
            "product_archive": WIZARDS_PRODUCT_ARCHIVE, "product_archive_pages": index_pages,
            "product_pages": sorted(product_pages.values(), key=lambda row: row["url"]),
            "pages": pages, "page_count": len(pages), "termination": termination,
            "unique_gallery_articles": len(unique), "articles": sorted(unique.values(), key=lambda row: row["url"]),
            "page_23": next((row for row in pages if row["page"] == 23), None),
            "sample_articles": article_results, "safety_cap": MAX_GALLERY_PAGES}


def _refresh_product_gallery_index(client: HttpClient, ops_root: Path) -> dict:
    """Refresh just the cached product archive and merge its URLs into the gallery index."""
    wizard_root = ops_root / "sources" / "wizards"
    listing_dir = wizard_root / "gallery" / "listing"
    catalogue_path = wizard_root / "gallery_catalogue.json"
    try:
        body, meta, fetched = fetch_cached(
            client, WIZARDS_PRODUCT_ARCHIVE, listing_dir / "product-archive-01.html", expected="html")
        index_data = gallery_product_archive_entries(body)
        links = index_data["gallery_links"]
        product_pages = index_data["product_pages"]
        previous = json.loads(catalogue_path.read_text(encoding="utf-8")) if catalogue_path.exists() else {}
        if not isinstance(previous, dict):
            raise SourceFormatError("Cached Wizards gallery catalogue is not an object")
        articles = previous.get("articles", [])
        if not isinstance(articles, list):
            raise SourceFormatError("Cached Wizards gallery articles are not a list")
        merged = {row["url"]: row for row in articles if isinstance(row, dict) and isinstance(row.get("url"), str)}
        before = set(merged)
        for row in links:
            merged.setdefault(row["url"], row)
        pages = previous.get("product_archive_pages", [])
        if not isinstance(pages, list):
            pages = []
        index_page = {"url": WIZARDS_PRODUCT_ARCHIVE, "sha256": meta["sha256"], "bytes": len(body),
                      "gallery_links": len(links), "product_pages": len(product_pages),
                      "http_cached": not fetched}
        pages = [row for row in pages if not isinstance(row, dict) or row.get("url") != WIZARDS_PRODUCT_ARCHIVE]
        pages.append(index_page)
        previous.update({"product_archive": WIZARDS_PRODUCT_ARCHIVE, "product_archive_pages": pages,
                         "product_pages": product_pages,
                         "articles": sorted(merged.values(), key=lambda row: row["url"]),
                         "unique_gallery_articles": len(merged)})
        atomic_json(catalogue_path, previous)
        return {"status": "available", "pages_fetched": 1, "http_cached": not fetched,
                "gallery_links": len(links), "new_gallery_links": len(set(merged) - before),
                "unique_gallery_articles": len(merged), "sha256": meta["sha256"]}
    except (SourceError, OSError, json.JSONDecodeError) as exc:
        return {"status": "unavailable", "pages_fetched": 0, "error": str(exc)}


def run_sources(config_path: Path, *, refresh=True, metadata_only=True, progress=None) -> dict:
    if not metadata_only:
        raise SourceFormatError("Step 3 requires --metadata-only; image collection belongs to a later step")
    config_path = config_path.resolve()

    def report(fraction: float, detail: str = "") -> None:
        if progress is not None:
            progress(fraction, detail)

    if not refresh:
        rebuild_from_archives(config_path)
        report_path = config_path.parent / "reports" / "sources.json"
        if report_path.exists():
            return json.loads(report_path.read_text(encoding="utf-8"))
        raise SourceFormatError("Offline metadata replay needs a prior sources report")
    ops_root = config_path.parent
    config = _load_config(config_path, ops_root)
    source_root, reports = ops_root / "sources", ops_root / "reports"
    bulk_root, sets_root = source_root / "scryfall" / "bulk", source_root / "scryfall" / "sets"
    wizard_root = source_root / "wizards"
    for folder in (bulk_root, sets_root, wizard_root):
        folder.mkdir(parents=True, exist_ok=True)
    client = HttpClient(config["network"]["user_agent"], min_intervals={
        "api.scryfall.com": config["network"]["minimum_scryfall_interval_ms"] / 1000,
        "data.scryfall.io": 0.1,
        "magic.wizards.com": config["network"]["minimum_gallery_interval_ms"] / 1000})
    report(0.0, "Préparation des sources…")
    descriptor_body, descriptor_meta, descriptor_fetched = fetch_cached(
        client, SCRYFALL_BULK, bulk_root / "bulk-data.json", expected="json")
    descriptor = all_cards_descriptor(json.loads(descriptor_body))
    report(0.05, "Descripteur Scryfall vérifié")
    bulk_archive, bulk_meta, bulk_downloaded = download_bulk(
        client, descriptor, bulk_root,
        progress=(lambda fraction, detail="": report(0.05 + 0.65 * fraction, detail)) if progress else None)
    report(0.70, "Archive All Cards prête")
    report(0.72, "Vérification des éditions…")
    sets, set_pages = _fetch_set_pages(client, sets_root)
    sets_sha = hashlib.sha256("".join(page["sha256"] for page in set_pages).encode()).hexdigest()
    report(0.76, f"{len(sets)} éditions vérifiées")
    db_path = ops_root / "catalogue.sqlite"
    reports.mkdir(parents=True, exist_ok=True)
    old_meta = {}
    if db_path.exists():
        try:
            connection = sqlite3.connect(db_path)
            old_meta = dict(connection.execute("SELECT key,value FROM source_meta")); connection.close()
        except sqlite3.Error:
            old_meta = {}
    as_of = date.today()
    reused = (old_meta.get("bulk_sha256") == bulk_meta["sha256"] and
              old_meta.get("sets_sha256") == sets_sha and
              old_meta.get("as_of_date") == as_of.isoformat() and db_path.exists() and
              (reports / "catalogue_scope.json").exists())
    if reused:
        scope = json.loads((reports / "catalogue_scope.json").read_text(encoding="utf-8"))
        report(0.96, "Base source déjà à jour")
    else:
        scope = build_catalogue_db(
            bulk_archive, sets, db_path, config, bulk_meta, sets_sha, as_of,
            progress=(lambda fraction, detail="": report(0.76 + 0.20 * fraction, detail)) if progress else None)
        atomic_json(reports / "catalogue_scope.json", scope)
    override_report = apply_official_french_text_overrides(db_path)
    french_text_coverage = official_french_text_coverage(db_path)
    french_text_coverage["curated_official_translations"] = override_report
    scope["official_french_text_coverage"] = french_text_coverage
    atomic_json(reports / "catalogue_scope.json", scope)
    atomic_json(reports / "french-text-coverage.json", french_text_coverage)
    report(0.97, "Vérification de la galerie officielle…")
    gallery = _fetch_gallery(client, wizard_root / "gallery")
    atomic_json(wizard_root / "gallery_catalogue.json", gallery)
    report(1.0, "Sources vérifiées")
    sources = {
        "generated_at_utc": utc_now(), "metadata_only": True,
        "sources": {
            "scryfall_all_cards": {"status": "available", "url": SCRYFALL_BULK,
                "updated_at": descriptor["updated_at"], "descriptor_sha256": descriptor_meta["sha256"],
                "archive_path": str(bulk_archive), "archive_sha256": bulk_meta["sha256"],
                "compressed_bytes": bulk_meta["compressed_size"], "rows": bulk_meta["validation"]["rows"],
                "languages": bulk_meta["validation"]["languages"], "downloaded_this_run": bulk_downloaded,
                "descriptor_changed": descriptor_fetched and descriptor_meta.get("changed", True)},
            "scryfall_sets": {"status": "available", "pages": set_pages, "count": len(sets), "sha256": sets_sha},
            "wizards_gallery": {"status": "available" if gallery["pages"] else "unavailable", **gallery},
            "mtgjson": {"status": "evaluated_for_known_gap",
                "foreign_data_model": "https://mtgjson.com/data-models/foreign-data/",
                "known_gap_probe": {"set_code": "40K", "collector_number": "176",
                    "status": "no_french_foreign_data", "source_file": "40K.json.bz2",
                    "compressed_bytes": 419247},
                "reason": "MTGJSON ForeignData has no French text for the confirmed 40K/176 gap; no bulk dataset was downloaded or treated as complete."},
            "official_french_text_overrides": {"status": "curated_local_sidecar",
                "path": str(OFFICIAL_FRENCH_OVERRIDES), "count": override_report["count"]},
            "gatherer": {"status": "disabled_pending_image_adapter_validation"},
            "smfcorp": {"status": "disabled_pending_image_adapter_validation"},
            "magiccorporation": {"status": "disabled_pending_image_adapter_validation"}},
        "import": {"database_path": str(db_path), "database_reused": reused,
            "sets": scope["sets_total"], "eligible_sets": scope["sets_eligible"],
            "deferred_sets": scope["sets_deferred"], "card_lines": scope["card_lines_total"],
            "normalized_fr_en_records": scope["normalized_fr_en_records"],
            "other_language_records_archived_only": scope["other_language_records_archived_only"],
            "official_french_text_coverage": french_text_coverage},
        "network_calls": dict(client.calls), "image_downloads": 0, "gallery_safety_cap": MAX_GALLERY_PAGES}
    atomic_json(reports / "sources.json", sources); atomic_json(reports / "catalogue_scope.json", scope)
    return sources


def run_additions_only(config_path: Path, current_db: Path, *, progress=None) -> dict:
    """Fetch only cards in new or growing sets for a small local catalogue build."""
    config_path = Path(config_path).resolve()
    ops_root = config_path.parent
    config = _load_config(config_path, ops_root)
    reports_root = ops_root / "reports"
    reports_root.mkdir(parents=True, exist_ok=True)
    source_db = ops_root / "catalogue.sqlite"
    french_text_coverage = (official_french_text_coverage(source_db) if source_db.is_file()
                            else {"status": "unavailable", "reason": "source_catalogue_missing"})
    override_report = (apply_official_french_text_overrides(source_db) if source_db.is_file()
                       else {"status": "unavailable", "count": 0})
    french_text_coverage["curated_official_translations"] = override_report
    if source_db.is_file():
        french_text_coverage = official_french_text_coverage(source_db)
        french_text_coverage["curated_official_translations"] = override_report
    atomic_json(reports_root / "french-text-coverage.json", french_text_coverage)
    bulk_root = ops_root / "sources" / "scryfall" / "bulk"
    sets_root = ops_root / "sources" / "scryfall" / "sets"
    client = HttpClient(config["network"]["user_agent"], min_intervals={
        "api.scryfall.com": config["network"]["minimum_scryfall_interval_ms"] / 1000})
    gallery_index = _refresh_product_gallery_index(client, ops_root)

    def report(fraction, detail):
        if progress:
            progress(fraction, detail)

    report(0.0, "Vérification des nombres de cartes par édition…")
    sets, pages = _fetch_set_pages(client, sets_root)
    with sqlite3.connect(f"file:{Path(current_db).as_posix()}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT set_id,source_card_count,eligibility,released_at FROM editions").fetchall()
        previous = {row[0]: (row[1] or 0, row[2]) for row in rows}
        latest_release = max((row[3] for row in rows if row[2] == "eligible" and row[3]), default="")
    as_of = date.today()
    growing = []
    newly_eligible = []
    for item in sets:
        status, _ = _set_reason(item, config, as_of)
        old_count, old_status = previous.get(item.get("id"), (0, None))
        if status == "eligible" and (old_status != "eligible" or (item.get("card_count") or 0) > old_count):
            growing.append(item)
            if old_status != "eligible":
                newly_eligible.append(item)
    growing.sort(key=lambda item: item.get("released_at") or "", reverse=True)
    report(0.12, f"Dernière édition locale : {latest_release or 'inconnue'} ; {len(growing)} éditions à vérifier")
    if not growing:
        detail = "Aucune nouvelle édition ni carte signalée"
        if french_text_coverage.get("status") != "complete":
            detail += "; la couverture française reste incomplète"
        report(1.0, detail)
        return {"status": "no_change", "network_calls": dict(client.calls),
                "wizards_gallery_index": gallery_index,
                "official_french_text_coverage": french_text_coverage}

    wanted = {item["id"] for item in growing}
    placeholders = ",".join("?" for _ in wanted)
    with sqlite3.connect(f"file:{Path(current_db).as_posix()}?mode=ro", uri=True) as conn:
        existing = {row[0] for row in conn.execute(
            f"SELECT sr.provider_card_id FROM source_records sr JOIN printings p USING(printing_id) WHERE p.set_id IN ({placeholders})",
            sorted(wanted))}

    additions = {}
    fetched = {}
    for index, item in enumerate(growing, 1):
        if not item.get("card_count"):
            continue
        fetched_for_set = set()
        query = urllib.parse.urlencode({"q": f"e:{item['code']}", "unique": "prints",
                                        "include_multilingual": "true", "include_extras": "true"})
        for _, payload in iter_api_pages(client, f"https://api.scryfall.com/cards/search?{query}"):
            for card in payload["data"]:
                if card.get("set_id") != item["id"] or not card.get("id"):
                    raise SourceFormatError(f"Carte inattendue dans l'édition {item['code']}")
                fetched[card["id"]] = card
                fetched_for_set.add(card["id"])
                if card["id"] not in existing:
                    additions[card["id"]] = card
            report(0.30 + 0.25 * index / len(growing),
                   f"{index} / {len(growing)} éditions ; {len(additions)} cartes ajoutées")
        if len(fetched_for_set) < item["card_count"]:
            raise SourceFormatError(f"Recherche incomplète pour l'édition {item['code']}")
    if not additions and not newly_eligible:
        report(1.0, "Aucun nouvel identifiant de carte")
        return {"status": "no_change", "network_calls": dict(client.calls),
                "wizards_gallery_index": gallery_index,
                "official_french_text_coverage": french_text_coverage}

    bulk_root.mkdir(parents=True, exist_ok=True)
    archive = bulk_root / f"delta-{uuid.uuid4().hex}.jsonl.gz"
    temp = archive.with_name(archive.name + ".part")
    try:
        with gzip.open(temp, "wb") as target:
            for card_id in sorted(fetched):
                target.write(json.dumps(fetched[card_id], ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        os.replace(temp, archive)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    languages = Counter(card["lang"] for card in fetched.values())
    bulk_meta = {"updated_at": utc_now(), "sha256": sha256_file(archive),
                 "compressed_size": archive.stat().st_size,
                 "validation": {"rows": len(fetched),
                                "languages": dict(sorted(languages.items()))}}
    atomic_json(archive.with_name(archive.name + ".meta.json"), bulk_meta)
    sets_sha = hashlib.sha256("".join(page["sha256"] for page in pages).encode()).hexdigest()
    report(1.0, f"{len(additions)} nouvelles lignes de cartes")
    result = {"status": "additions", "generated_at_utc": utc_now(), "set_items": growing,
              "sources": {"scryfall_all_cards": {"archive_path": str(archive),
                  "archive_sha256": bulk_meta["sha256"], "updated_at": bulk_meta["updated_at"],
                  "rows": bulk_meta["validation"]["rows"], "downloaded_this_run": False},
                  "scryfall_sets": {"pages": pages, "count": len(sets), "sha256": sets_sha},
                  "wizards_gallery": {**gallery_index,
                      "status": "product_archive_index_refreshed" if gallery_index["status"] == "available" else "unavailable"}},
              "import": {"new_card_records": len(additions), "changed_sets": len(growing)},
              "network_calls": dict(client.calls), "image_downloads": 0,
              "official_french_text_coverage": french_text_coverage,
              "wizards_gallery_index": gallery_index}
    return result


def validate_catalogue(ops_root: Path) -> dict:
    reports = ops_root / "reports"
    scope = json.loads((reports / "catalogue_scope.json").read_text(encoding="utf-8"))
    sources = json.loads((reports / "sources.json").read_text(encoding="utf-8"))
    db = ops_root / "catalogue.sqlite"
    if not db.is_file() or not db.stat().st_size:
        raise SourceFormatError("Catalogue SQLite missing or empty")
    conn = sqlite3.connect(db)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    sets_count = conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    card_count = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    language_sum = conn.execute("SELECT COALESCE(SUM(record_count),0) FROM language_counts").fetchone()[0]
    duplicates = conn.execute("SELECT COUNT(*) FROM (SELECT id FROM cards GROUP BY id HAVING COUNT(*)>1)").fetchone()[0]
    conn.close()
    if integrity != "ok" or sets_count != scope["sets_total"] or card_count != scope["normalized_fr_en_records"]:
        raise SourceFormatError("Catalogue integrity or counters differ from report")
    if duplicates or language_sum != scope["card_lines_total"]:
        raise SourceFormatError("Duplicate IDs or language counts do not reconcile")
    if scope["card_lines_total"] != scope["normalized_fr_en_records"] + scope["other_language_records_archived_only"]:
        raise SourceFormatError("Raw card rows do not reconcile with FR/EN and other-language records")
    if scope["sets_total"] != scope["sets_eligible"] + scope["sets_deferred"] + scope["sets_explicitly_excluded"]:
        raise SourceFormatError("Set scope counters do not reconcile")
    bulk = sources["sources"]["scryfall_all_cards"]
    if sha256_file(Path(bulk["archive_path"])) != bulk["archive_sha256"]:
        raise SourceFormatError("All Cards archive SHA-256 mismatch")
    if sources.get("image_downloads") != 0:
        raise SourceFormatError("Step 3 must not download card images")
    french_text_coverage = official_french_text_coverage(db)
    atomic_json(reports / "french-text-coverage.json", french_text_coverage)
    return {"status": "valid", "sets": sets_count, "normalized_fr_en_records": card_count,
            "raw_card_lines": language_sum, "unique_provider_ids": duplicates == 0,
            "source_archive_sha256": bulk["archive_sha256"], "image_downloads": 0,
            "official_french_text_coverage": french_text_coverage}


def official_french_text_coverage(source_db: Path) -> dict:
    """Measure French source coverage without synthesizing or translating text."""
    source_db = Path(source_db)
    if not source_db.is_file():
        return {"status": "unavailable", "reason": "source_catalogue_missing"}
    conn = sqlite3.connect(source_db)
    conn.row_factory = sqlite3.Row
    try:
        english_rows = conn.execute(
            "SELECT oracle_id,oracle_text,faces_json FROM cards "
            "WHERE lang='en' AND oracle_id IS NOT NULL AND oracle_id<>''").fetchall()
        english_oracles = {str(row["oracle_id"]) for row in english_rows}
        english_text_oracles = set()
        for row in english_rows:
            has_text = bool(row["oracle_text"])
            if not has_text and row["faces_json"] and row["faces_json"] != "[]":
                try:
                    faces = json.loads(row["faces_json"])
                except (TypeError, json.JSONDecodeError):
                    faces = []
                has_text = any(face.get("oracle_text") for face in faces if isinstance(face, dict))
            if has_text:
                english_text_oracles.add(str(row["oracle_id"]))
        french_rows = conn.execute(
            "SELECT id,oracle_id,set_code,collector_number,raw_json,printed_text "
            "FROM cards WHERE lang='fr' ORDER BY set_code,collector_number,id").fetchall()
        french_oracles, name_oracles, type_oracles, text_oracles = set(), set(), set(), set()
        french_printings = {(str(row["set_id"] or ""), str(row["collector_number"] or ""))
                            for row in conn.execute(
                                "SELECT DISTINCT set_id,collector_number FROM cards WHERE lang='fr'")}
        exact_french_ids = {str(row["id"]) for row in conn.execute(
            "SELECT id,set_id,collector_number FROM cards WHERE lang='en'")
            if (str(row["set_id"] or ""), str(row["collector_number"] or "")) in french_printings}
        has_override_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='official_french_text'").fetchone()
        curated_rows = (conn.execute(
            "SELECT provider_card_id,oracle_id FROM official_french_text WHERE language='fr'").fetchall()
            if has_override_table else [])
        curated_oracles = {str(row["oracle_id"]) for row in curated_rows if row["oracle_id"]}
        curated_provider_ids = {str(row["provider_card_id"]) for row in curated_rows}
        exact_french_ids |= curated_provider_ids
        for row in french_rows:
            oracle_id = str(row["oracle_id"] or "")
            if oracle_id:
                french_oracles.add(oracle_id)
            try:
                raw = json.loads(row["raw_json"])
            except (TypeError, json.JSONDecodeError):
                raw = {}
            faces = raw.get("card_faces") if isinstance(raw.get("card_faces"), list) else []
            face_dicts = [face for face in faces if isinstance(face, dict)]
            if raw.get("printed_name") or any(face.get("printed_name") for face in face_dicts):
                if oracle_id:
                    name_oracles.add(oracle_id)
            if raw.get("printed_type_line") or any(face.get("printed_type_line") for face in face_dicts):
                if oracle_id:
                    type_oracles.add(oracle_id)
            if row["printed_text"] or any(face.get("printed_text") for face in face_dicts):
                if oracle_id:
                    text_oracles.add(oracle_id)
        covered = english_oracles & french_oracles
        rules_text_satisfied = text_oracles | (english_oracles - english_text_oracles)
        official_text_oracles = (name_oracles & type_oracles & rules_text_satisfied) | curated_oracles
        official_covered = english_oracles & official_text_oracles
        missing = sorted(english_oracles - french_oracles)
        missing_official = sorted(english_oracles - official_text_oracles)
        denominator = len(english_oracles)
        return {
            "status": "complete" if denominator and len(official_covered) == denominator else "incomplete",
            "basis": "distinct_english_oracle_ids_in_local_scryfall_source_db",
            "english_oracle_ids": denominator,
            "oracle_ids_with_french_records": len(covered),
            "oracle_coverage_percent": round(100 * len(covered) / denominator, 2) if denominator else 0.0,
            "oracle_ids_with_official_french_text": len(official_covered),
            "official_french_text_coverage_percent": (round(100 * len(official_covered) / denominator, 2)
                                                       if denominator else 0.0),
            "oracle_ids_with_printed_french_name": len(english_oracles & name_oracles),
            "oracle_ids_with_printed_french_type": len(english_oracles & type_oracles),
            "oracle_ids_with_printed_french_text": len(english_oracles & text_oracles),
            "english_printings_with_exact_set_number_french_record": len(exact_french_ids),
            "english_printings_with_curated_official_french_text": len(curated_provider_ids),
            "french_source_records": len(french_rows),
            "curated_official_french_records": len(curated_rows),
            "oracle_ids_missing_french_records": len(missing),
            "missing_oracle_ids_sample": missing[:50],
            "oracle_ids_missing_official_french_text": len(missing_official),
            "missing_official_french_oracle_ids_sample": missing_official[:50],
            "source": "Scryfall All Cards FR/EN archive only",
            "note": ("Coverage is measured, not inferred from English text. Missing official French fields "
                     "remain unresolved; no translations are generated by this audit."),
        }
    finally:
        conn.close()


def apply_official_french_text_overrides(source_db: Path) -> dict:
    """Idempotently import reviewed, exact-identity official FR text sidecar records."""
    source_db = Path(source_db)
    if not source_db.is_file():
        return {"status": "unavailable", "count": 0, "reason": "source_catalogue_missing"}
    if not OFFICIAL_FRENCH_OVERRIDES.is_file():
        return {"status": "unavailable", "count": 0, "reason": "override_file_missing"}
    try:
        document = json.loads(OFFICIAL_FRENCH_OVERRIDES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceFormatError("Official French text override file is unreadable") from exc
    entries = document.get("entries") if isinstance(document, dict) else None
    if not isinstance(document, dict) or document.get("schema_version") != 1 or not isinstance(entries, list):
        raise SourceFormatError("Official French text override schema is invalid")
    conn = sqlite3.connect(source_db)
    conn.row_factory = sqlite3.Row
    inserted = updated = 0
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS official_french_text (
            provider_card_id TEXT PRIMARY KEY,
            oracle_id TEXT NOT NULL,
            set_code TEXT NOT NULL,
            collector_number TEXT NOT NULL,
            language TEXT NOT NULL CHECK(language='fr'),
            fields_json TEXT NOT NULL,
            source TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_image_url TEXT NOT NULL,
            source_sha256 TEXT,
            reviewed_at TEXT NOT NULL
        )""")
        for entry in entries:
            required = {"provider_card_id", "oracle_id", "set_code", "collector_number",
                        "fields", "source", "source_url", "source_image_url", "reviewed_at"}
            if not isinstance(entry, dict) or set(entry) != required:
                raise SourceFormatError("Official French text override entry fields are invalid")
            fields = entry["fields"]
            if (not isinstance(fields, dict) or not all(isinstance(fields.get(key), str) and fields[key]
                    for key in ("printed_name", "printed_type_line", "printed_text"))):
                raise SourceFormatError("Official French override must contain printed name, type and text")
            matches = conn.execute(
                "SELECT id,oracle_id,set_code,collector_number,lang FROM cards WHERE id=?",
                (entry["provider_card_id"],)).fetchall()
            if (len(matches) != 1 or matches[0]["oracle_id"] != entry["oracle_id"]
                    or str(matches[0]["set_code"]).casefold() != str(entry["set_code"]).casefold()
                    or str(matches[0]["collector_number"]) != str(entry["collector_number"])
                    or matches[0]["lang"] != "en"):
                raise SourceFormatError("Official French override does not match one exact local source card")
            canonical_fields = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fingerprint = hashlib.sha256(canonical_fields.encode("utf-8")).hexdigest()
            before = conn.execute("SELECT fields_json,source_sha256 FROM official_french_text WHERE provider_card_id=?",
                                  (entry["provider_card_id"],)).fetchone()
            conn.execute("""INSERT INTO official_french_text VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider_card_id) DO UPDATE SET oracle_id=excluded.oracle_id,
                set_code=excluded.set_code,collector_number=excluded.collector_number,
                language=excluded.language,fields_json=excluded.fields_json,source=excluded.source,
                source_url=excluded.source_url,source_image_url=excluded.source_image_url,
                source_sha256=excluded.source_sha256,reviewed_at=excluded.reviewed_at""",
                (entry["provider_card_id"], entry["oracle_id"], entry["set_code"],
                 str(entry["collector_number"]), "fr", canonical_fields, entry["source"],
                 entry["source_url"], entry["source_image_url"], fingerprint, entry["reviewed_at"]))
            if before is None:
                inserted += 1
            elif before["fields_json"] != canonical_fields or before["source_sha256"] != fingerprint:
                updated += 1
        conn.commit()
        return {"status": "available", "count": len(entries), "inserted": inserted,
                "updated": updated, "provider": "wizards_official_manual_review"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
