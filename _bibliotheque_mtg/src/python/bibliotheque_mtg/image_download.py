from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlsplit, urljoin
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError
from bs4 import BeautifulSoup

from .v2.publish import resolve_current
from .v2.query import connect


_LOCK = threading.Lock()
_WAL_INIT_LOCK = threading.Lock()
_WAL_READY: set[Path] = set()
_SCRYFALL_API_LOCK = threading.Lock()
_SCRYFALL_API_LAST_REQUEST = 0.0
_SCRYFALL_API_MIN_INTERVAL = 0.6
_BATCH_IMAGE_RATE_LOCK = threading.Lock()
_BATCH_IMAGE_NEXT_REQUEST = 0.0
_BATCH_IMAGE_BLOCKED_UNTIL = 0.0
_BATCH_IMAGE_MIN_INTERVAL = 0.25
_MAX_BYTES = 20 * 1024 * 1024
_MAX_API_BYTES = 2 * 1024 * 1024
_MAX_GETS = 3
_MIN_SOURCE_SIDE = 401  # historical downloader required both sides > 400 px
_OUTPUT_SIZE = (531, 738)
_MANUAL_IMAGE_SOURCES_PATH = Path(__file__).with_name("image_sources_manual.json")
_WIZARDS_INDEX_PATH = Path(__file__).resolve().parents[3] / "sources" / "wizards" / "gallery_catalogue.json"
_SOURCE_CATALOGUE_PATH = Path(__file__).resolve().parents[3] / "catalogue.sqlite"
_SIZES = {"png": 0, "large": 1, "jpeg": 2, "normal": 3, "art_crop": 4, "crop": 5, "small": 6, "thumb": 7, "display": 8, "grid": 9}
_GALLERY_CACHE: dict[str, tuple[float, dict[str, list[str]]]] = {}
_PRODUCT_GALLERY_CACHE: dict[str, tuple[float, str, list[str]]] = {}
_CONTENTFUL_CLIENT_CACHE: tuple[float, str, str] | None = None
_CONTENTFUL_ENTRY_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_CONTENTFUL_DIRECT_CACHE: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}


def _gallery_key(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value.casefold())
    return "-".join("".join(c if c.isalnum() else " " for c in plain if not unicodedata.combining(c)).split())


def _reviewed_wizards_reference(provider_id: str, printing: dict) -> dict | None:
    """Resolve a reviewed Wizards image against the exact local provider identity."""
    if not _SOURCE_CATALOGUE_PATH.is_file():
        return None
    try:
        with sqlite3.connect(_SOURCE_CATALOGUE_PATH.resolve().as_uri() + "?mode=ro",
                             uri=True, timeout=5) as conn:
            row = conn.execute(
                "SELECT oracle_id,set_code,collector_number,fields_json,source_url,source_image_url "
                "FROM official_french_text "
                "WHERE provider_card_id=? AND language='fr'", (provider_id,)).fetchone()
    except sqlite3.Error:
        return None
    if (not row or row[0] != printing["oracle_id"]
            or str(row[1]).casefold() != str(printing["code"]).casefold()
            or str(row[2]) != str(printing["collector_number"])):
        return None
    try:
        name = json.loads(row[3]).get("printed_name")
    except (TypeError, json.JSONDecodeError):
        return None
    image_url = row[5]
    parsed = urlsplit(image_url) if isinstance(image_url, str) else None
    if (not isinstance(name, str) or not name or not parsed or parsed.scheme != "https"
            or parsed.hostname != "media.wizards.com"):
        return None
    return {"name": name, "url": image_url, "language": "fr", "source": "wizards_official_reviewed",
            "allow_host": "media.wizards.com", "evidence": row[4]}


def _gallery_metadata(session, url: str, *, headers: dict, params: dict | None = None,
                      max_bytes: int = 1024 * 1024) -> bytes | None:
    """Read bounded public page/API metadata without following redirects."""
    try:
        response = session.get(url, headers=headers, params=params, timeout=(8, 15),
                               allow_redirects=False, stream=True)
        try:
            if response.status_code != 200:
                return None
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > max_bytes:
                    return None
            return bytes(body)
        finally:
            response.close()
    except requests.RequestException:
        return None


def _contentful_client(session, scripts: list[str]) -> tuple[str, str] | None:
    global _CONTENTFUL_CLIENT_CACHE
    cached = _CONTENTFUL_CLIENT_CACHE
    if cached and time.monotonic() - cached[0] < 3600:
        return cached[1], cached[2]
    for script in reversed(scripts[-4:]):
        parsed = urlsplit(script)
        if parsed.scheme != "https" or parsed.hostname != "magic.wizards.com" or not parsed.path.startswith("/_nuxt/"):
            continue
        body = _gallery_metadata(session, script, headers={"Accept": "application/javascript"})
        if not body:
            continue
        match = re.search(rb'space:"([A-Za-z0-9]+)",host:"cdn\.contentful\.com",accessToken:"([A-Za-z0-9_-]+)"', body)
        if match:
            space, token = match.group(1).decode("ascii"), match.group(2).decode("ascii")
            _CONTENTFUL_CLIENT_CACHE = (time.monotonic(), space, token)
            return space, token
    return None


def _contentful_entry(session, space: str, token: str, entry_id: str) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9]{1,64}", entry_id):
        return None
    key = (space, entry_id)
    cached = _CONTENTFUL_ENTRY_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 3600:
        return cached[1]
    url = f"https://cdn.contentful.com/spaces/{space}/environments/master/entries/{entry_id}"
    body = _gallery_metadata(session, url,
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                             params={"locale": "fr"})
    if not body:
        return None
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if len(_CONTENTFUL_ENTRY_CACHE) >= 1024:
        _CONTENTFUL_ENTRY_CACHE.pop(next(iter(_CONTENTFUL_ENTRY_CACHE)))
    _CONTENTFUL_ENTRY_CACHE[key] = (time.monotonic(), value)
    return value


def _product_gallery_images(page_url: str, english_name: str, french_name: str,
                            collector_number: str, session) -> list[dict]:
    page = _PRODUCT_GALLERY_CACHE.get(page_url)
    if not page or time.monotonic() - page[0] >= 3600 or not english_name or not french_name:
        return []
    client = _contentful_client(session, page[2])
    if not client:
        return []
    space, token = client
    gallery = _contentful_entry(session, space, token, page[1])
    if not gallery or gallery.get("sys", {}).get("contentType", {}).get("sys", {}).get("id") != "cardImageGallery":
        return []
    card_list = gallery.get("fields", {}).get("cardList", {})
    body = card_list.get("body", "") if isinstance(card_list, dict) else ""
    if not isinstance(body, str):
        return []
    expected_name = _gallery_key(english_name)
    entry_ids = []
    for line in body.splitlines():
        match = re.fullmatch(r"\s*(?:\d+\s+)?(.+?)\s+\[([A-Za-z0-9]{1,64})\]\s*", line)
        if match and _gallery_key(match.group(1)) == expected_name:
            entry_ids.append(match.group(2))
    results = []
    for entry_id in dict.fromkeys(entry_ids[:6]):
        card = _contentful_entry(session, space, token, entry_id)
        if not card or card.get("sys", {}).get("contentType", {}).get("sys", {}).get("id") != "magicCard":
            continue
        fields = card.get("fields", {})
        if (not isinstance(fields, dict) or str(fields.get("collectorNumber") or "") != str(collector_number)
                or _gallery_key(str(fields.get("name") or "")) != _gallery_key(french_name)):
            continue
        image_url = fields.get("face")
        parsed = urlsplit(image_url) if isinstance(image_url, str) else None
        if not parsed or parsed.scheme != "https" or parsed.hostname != "media.wizards.com":
            continue
        results.append({"url": image_url, "language": "fr", "source": "wizards_product_gallery",
                        "allow_host": "media.wizards.com", "evidence": page_url})
    return results


def _contentful_direct_images(set_code: str, english_name: str, french_name: str,
                              collector_number: str, session) -> list[dict]:
    """Query Wizards' public card catalogue for one exact set/name/number."""
    identifier = _gallery_key(english_name)
    if not re.fullmatch(r"[a-z0-9-]{1,120}", identifier) or not set_code or not collector_number:
        return []
    key = (set_code.casefold(), identifier, str(collector_number))
    cached = _CONTENTFUL_DIRECT_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 3600:
        return cached[1]
    bootstrap = "https://magic.wizards.com/fr/products/modern-horizons-3/card-image-gallery"
    scripts = []
    used_cached_page = False
    if _CONTENTFUL_CLIENT_CACHE is None or time.monotonic() - _CONTENTFUL_CLIENT_CACHE[0] >= 3600:
        sample = _WIZARDS_INDEX_PATH.parent / "gallery" / "articles" / "modern_horizons_3.html"
        try:
            page_html = sample.read_bytes() if sample.is_file() and sample.stat().st_size <= 1024 * 1024 else None
        except OSError:
            page_html = None
        used_cached_page = page_html is not None
        if page_html is None:
            page_html = _gallery_metadata(session, bootstrap, headers={"Accept": "text/html"})
        if page_html:
            soup = BeautifulSoup(page_html, "html.parser")
            scripts = [urljoin(bootstrap, script.get("src", "")) for script in soup.select("script[src]")
                       if script.get("src", "").startswith("/_nuxt/")]
    client = _contentful_client(session, scripts)
    if not client and used_cached_page:
        live_page = _gallery_metadata(session, bootstrap, headers={"Accept": "text/html"})
        if live_page:
            soup = BeautifulSoup(live_page, "html.parser")
            scripts = [urljoin(bootstrap, script.get("src", "")) for script in soup.select("script[src]")
                       if script.get("src", "").startswith("/_nuxt/")]
            client = _contentful_client(session, scripts)
    if not client:
        return []
    space, token = client
    url = f"https://cdn.contentful.com/spaces/{space}/environments/master/entries"
    params = {"content_type": "magicCard",
              "fields.foundInSet.sys.contentType.sys.id": "productSet",
              "fields.foundInSet.fields.abbreviation": set_code.casefold(),
              "fields.identifier": identifier,
              "fields.collectorNumber": str(collector_number),
              "locale": "fr", "limit": 5, "include": 0}
    body = _gallery_metadata(session, url,
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                             params=params, max_bytes=256 * 1024)
    if not body:
        return []
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    results = []
    for card in payload.get("items", [])[:5] if isinstance(payload, dict) else []:
        if not isinstance(card, dict) or card.get("sys", {}).get("contentType", {}).get("sys", {}).get("id") != "magicCard":
            continue
        fields = card.get("fields", {})
        if (not isinstance(fields, dict) or str(fields.get("collectorNumber") or "") != str(collector_number)
                or _gallery_key(str(fields.get("identifier") or "")) != identifier):
            continue
        name = str(fields.get("name") or "")
        if not name or (french_name and _gallery_key(name) != _gallery_key(french_name)):
            continue
        image_url = fields.get("face")
        parsed = urlsplit(image_url) if isinstance(image_url, str) else None
        if (not parsed or parsed.scheme != "https" or parsed.hostname != "media.wizards.com"
                or not parsed.path.rsplit("/", 1)[-1].casefold().startswith("fr_")):
            continue
        entry_id = str(card.get("sys", {}).get("id") or "")
        results.append({"url": image_url, "language": "fr", "source": "wizards_contentful",
                        "allow_host": "media.wizards.com",
                        "evidence": f"{url}/{entry_id}" if re.fullmatch(r"[A-Za-z0-9]{1,64}", entry_id) else url})
    if len(_CONTENTFUL_DIRECT_CACHE) >= 1024:
        _CONTENTFUL_DIRECT_CACHE.pop(next(iter(_CONTENTFUL_DIRECT_CACHE)))
    _CONTENTFUL_DIRECT_CACHE[key] = (time.monotonic(), results)
    return results


def _official_gallery_images(set_name: str, french_name: str, session, *,
                             english_name: str = "", collector_number: str = "",
                             set_code: str = "", exact_only: bool = False) -> list[dict]:
    """Find a card by its exact French name in the indexed official set gallery."""
    if not french_name and not (set_code and english_name and collector_number):
        return []
    if not french_name:
        return _contentful_direct_images(set_code, english_name, "", collector_number, session)
    try:
        catalogue = json.loads(_WIZARDS_INDEX_PATH.read_text(encoding="utf-8"))
        articles = catalogue["articles"] + catalogue.get("sample_articles", [])
        product_pages = catalogue.get("product_pages", [])
    except (OSError, ValueError, KeyError):
        articles = []
        product_pages = []
    slug = _gallery_key(set_name).replace("-", "")
    urls = []
    for item in articles:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        candidate = item["url"]
        parsed = urlsplit(candidate)
        if parsed.scheme != "https" or parsed.hostname != "magic.wizards.com":
            continue
        path = parsed.path.rstrip("/")
        if path.startswith("/fr/news/card-image-gallery/"):
            if exact_only:
                continue
            edition_slug = path.rsplit("/", 1)[-1]
        elif path.startswith("/fr/products/") and path.endswith("/card-image-gallery"):
            edition_slug = path.split("/")[-2]
        else:
            continue
        if edition_slug.replace("-", "").startswith(slug):
            urls.append(candidate)
    for item in product_pages:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        parsed = urlsplit(item["url"])
        if (parsed.scheme == "https" and parsed.hostname == "magic.wizards.com"
                and parsed.path.startswith("/fr/products/")
                and parsed.path.rstrip("/").rsplit("/", 1)[-1].replace("-", "").startswith(slug)):
            candidate = item["url"].rstrip("/") + "/card-image-gallery"
            if candidate not in urls:
                urls.append(candidate)
    guessed_product = f"https://magic.wizards.com/fr/products/{_gallery_key(set_name)}/card-image-gallery"
    if not urls:
        urls.append(guessed_product)
    results = []
    seen = set()
    for url in urls[:3]:
        cached = _GALLERY_CACHE.get(url)
        if cached and time.monotonic() - cached[0] < 3600:
            images = cached[1]
        else:
            try:
                response = session.get(url, timeout=(8, 15), allow_redirects=False, stream=True,
                                       headers={"User-Agent": "BibliothequeMagicLocal/2.0", "Accept": "text/html"})
                try:
                    if response.status_code == 404:
                        _GALLERY_CACHE[url] = (time.monotonic(), {})
                        continue
                    if response.status_code != 200 or "text/html" not in response.headers.get("Content-Type", "").casefold():
                        continue
                    body = bytearray()
                    for chunk in response.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            break
                    if len(body) > 1024 * 1024:
                        continue
                finally:
                    response.close()
            except requests.RequestException:
                continue
            soup = BeautifulSoup(bytes(body), "html.parser")
            if "/fr/products/" in url:
                gallery_node = soup.select_one("[data-cig-v][data-ctf-id]")
                entry_id = gallery_node.get("data-ctf-id", "") if gallery_node else ""
                scripts = [urljoin(url, script.get("src", "")) for script in soup.select("script[src]")
                           if script.get("src", "").startswith("/_nuxt/")]
                if re.fullmatch(r"[A-Za-z0-9]{1,64}", entry_id):
                    _PRODUCT_GALLERY_CACHE[url] = (time.monotonic(), entry_id, scripts)
            images = {}
            for image in soup.select("img[alt][src], img[alt][data-src]"):
                source = urljoin(url, image.get("data-src") or image.get("src") or "")
                source = source.replace("http://media.wizards.com/", "https://media.wizards.com/", 1)
                if urlsplit(source).hostname == "media.wizards.com" and urlsplit(source).scheme == "https":
                    images.setdefault(_gallery_key(image["alt"]), []).append(source)
            _GALLERY_CACHE[url] = (time.monotonic(), images)
        for source in ([] if exact_only else images.get(_gallery_key(french_name), [])):
            if source in seen:
                continue
            seen.add(source)
            results.append({"url": source, "language": "fr", "source": "wizards_gallery",
                            "allow_host": "media.wizards.com", "evidence": url})
        if "/fr/products/" in url:
            for candidate in _product_gallery_images(url, english_name, french_name,
                                                     collector_number, session):
                if candidate["url"] not in seen:
                    seen.add(candidate["url"])
                    results.append(candidate)
    if not results and set_code and english_name and collector_number:
        results.extend(_contentful_direct_images(set_code, english_name, french_name,
                                                 collector_number, session))
    return results


def ensure_catalogue_wal(db: Path) -> None:
    """Allow the local UI to read while image transactions are committed."""
    db = Path(db).resolve()
    with _WAL_INIT_LOCK:
        if db in _WAL_READY:
            return
        conn = sqlite3.connect(db, timeout=10)
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.casefold() != "wal":
                raise sqlite3.OperationalError(f"Mode SQLite inattendu : {mode}")
            _WAL_READY.add(db)
        finally:
            conn.close()


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 60.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_time = parsedate_to_datetime(value)
            if retry_time.tzinfo is None:
                retry_time = retry_time.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_time - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 60.0


def _fetch_live_fr(set_code: str, collector_number: str, session, cancel_event=None):
    """Fetch one exact Scryfall set/number/French record; never follow redirects."""
    global _SCRYFALL_API_LAST_REQUEST
    url = ("https://api.scryfall.com/cards/" + quote(str(set_code), safe="") + "/"
           + quote(str(collector_number), safe="") + "/fr")
    if cancel_event is not None and cancel_event.is_set():
        return None, "cancelled", None
    with _SCRYFALL_API_LOCK:
        delay = _SCRYFALL_API_MIN_INTERVAL - (time.monotonic() - _SCRYFALL_API_LAST_REQUEST)
        if delay > 0:
            time.sleep(delay)
        if cancel_event is not None and cancel_event.is_set():
            return None, "cancelled", None
        _SCRYFALL_API_LAST_REQUEST = time.monotonic()
        try:
            response = session.get(url, timeout=(8, 12), allow_redirects=False, stream=True,
                                   headers={"User-Agent": "BibliothequeMagicLocal/2.0",
                                            "Accept": "application/json"})
        except requests.RequestException as exc:
            return None, f"network_error:{type(exc).__name__}", None
    try:
        if response.status_code == 404:
            return None, "not_found", None
        if response.status_code == 429:
            return None, "rate_limited", _retry_after_seconds(response.headers.get("Retry-After"))
        if response.status_code == 403:
            return None, "blocked", None
        if 300 <= response.status_code < 400:
            return None, "redirect_rejected", None
        if response.status_code != 200:
            return None, f"http_{response.status_code}", None
        if "json" not in response.headers.get("Content-Type", "").casefold():
            return None, "invalid_content_type", None
        body = bytearray()
        try:
            for chunk in response.iter_content(65536):
                if cancel_event is not None and cancel_event.is_set():
                    return None, "cancelled", None
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > _MAX_API_BYTES:
                    return None, "response_too_large", None
        except requests.RequestException as exc:
            return None, f"network_error:{type(exc).__name__}", None
        try:
            card = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "invalid_json", None
        return (card, "candidate_found", None) if isinstance(card, dict) else (None, "invalid_json", None)
    finally:
        response.close()


def _same_printing_identity(live: dict, printing: dict, expected: dict) -> bool:
    if (str(live.get("set") or "").casefold() != str(printing["code"]).casefold()
            or str(live.get("set_id") or "") != str(printing["set_id"])
            or str(live.get("collector_number") or "") != str(printing["collector_number"])
            or str(live.get("lang") or "").casefold() != "fr"
            or str(live.get("layout") or "").casefold() != str(printing["layout"] or "").casefold()):
        return False
    expected_faces = expected.get("card_faces") or []
    live_faces = live.get("card_faces") or []
    if len(expected_faces) != len(live_faces):
        return False
    identity_evidence = False
    for field in ("oracle_id", "illustration_id"):
        expected_value, live_value = expected.get(field), live.get(field)
        if expected_value and live_value:
            if expected_value != live_value:
                return False
            identity_evidence = True
    expected_face_ids = [face.get("oracle_id") for face in expected_faces]
    live_face_ids = [face.get("oracle_id") for face in live_faces]
    if any(expected_face_ids) and any(live_face_ids):
        if expected_face_ids != live_face_ids:
            return False
        identity_evidence = True
    return identity_evidence


def _allowed_url(url: str, test_hosts: set[str] | None = None,
                 allowed_hosts: set[str] | None = None) -> bool:
    p = urlsplit(url)
    allowed = {"cards.scryfall.io"} | (test_hosts or set()) | (allowed_hosts or set())
    host = (p.hostname or "").casefold()
    scheme_ok = p.scheme == "https" and p.port in (None, 443)
    if test_hosts and host in test_hosts and host in {"127.0.0.1", "localhost", "::1"}:
        scheme_ok = p.scheme == "http" and p.port is not None
    return scheme_ok and host in allowed and not p.username and not p.password


def _manual_candidates(set_code: str, collector_number: str, face_index: int) -> list[dict]:
    try:
        registry = json.loads(_MANUAL_IMAGE_SOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("registre de sources image manuelles illisible") from exc
    if (not isinstance(registry, dict) or registry.get("schema_version") != 1
            or not isinstance(registry.get("candidates"), list)):
        raise ValueError("contrat du registre de sources image manuelles invalide")
    matches = []
    for item in registry["candidates"]:
        if not isinstance(item, dict):
            raise ValueError("entrée invalide dans le registre de sources image manuelles")
        if (str(item.get("set_code", "")).casefold() != str(set_code).casefold()
                or str(item.get("collector_number", "")) != str(collector_number)
                or item.get("face_index") != face_index):
            continue
        parsed = urlsplit(str(item.get("url") or ""))
        host = (parsed.hostname or "").casefold()
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username or parsed.password or not host
                or host != str(item.get("allow_host") or "").casefold()
                or item.get("language") != "fr" or not item.get("source")
                or not item.get("evidence")):
            raise ValueError("candidat manuel sans hôte, langue ou preuve valide")
        matches.append(item)
    if len(matches) > 1:
        raise ValueError("plusieurs candidats manuels pour la même impression et face")
    return matches


def _ordered_uris(raw: dict, face_index: int, *, full_card_only: bool = False) -> list[str]:
    faces = raw.get("card_faces") or []
    face_uris = ((faces[face_index].get("image_uris") or {})
                 if face_index < len(faces) and isinstance(faces[face_index], dict) else {})
    # Split, flip and adventure layouts may expose one whole-card image at root.
    uris = face_uris or raw.get("image_uris") or {}
    if not isinstance(uris, dict):
        return []
    return [value for key, value in sorted(uris.items(), key=lambda item: (_SIZES.get(item[0], 99), item[0]))
            if isinstance(value, str) and value and
            (not full_card_only or key in {"png", "large", "jpeg", "normal", "small"})]


def _load_ledger(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": 1, "faces": {}}


def _save_ledger(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _read_webp(data: bytes):
    if not data or len(data) > _MAX_BYTES:
        raise ValueError("taille invalide")
    try:
        with Image.open(__import__("io").BytesIO(data)) as image:
            image.verify()
        with Image.open(__import__("io").BytesIO(data)) as image:
            image.load()
            if image.width * image.height > 40_000_000 or image.width < 1 or image.height < 1:
                raise ValueError("dimensions invalides")
            return image.copy()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("contenu image invalide") from exc


def _encode_webp(data: bytes, *, method: int = 6) -> tuple[bytes, int, int]:
    with _read_webp(data) as source:
        oriented = ImageOps.exif_transpose(source)
        image = oriented.convert("RGBA")
        if oriented is not source:
            oriented.close()
        contained = ImageOps.contain(image, _OUTPUT_SIZE, method=Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", _OUTPUT_SIZE, (0, 0, 0, 0))
        canvas.alpha_composite(contained, ((_OUTPUT_SIZE[0] - contained.width) // 2,
                                          (_OUTPUT_SIZE[1] - contained.height) // 2))
        out = __import__("io").BytesIO()
        canvas.save(out, format="WEBP", quality=95, method=method)
        image.close()
        if contained is not image:
            contained.close()
        canvas.close()
        return out.getvalue(), _OUTPUT_SIZE[0], _OUTPUT_SIZE[1]


def _edition_images_manifest(version: Path, set_id: str) -> Path:
    root = json.loads((version / "editions" / "index.json").read_text(encoding="utf-8"))
    entry = next((e for e in root.get("editions", []) if e.get("id") == set_id), None)
    if not entry:
        raise ValueError("manifeste d’édition absent")
    rel = entry.get("images_json") or (str(Path(entry["folder"]) / "images.json") if entry.get("folder") else None)
    if not rel:
        raise ValueError("chemin de manifeste absent")
    path = (version / "editions" / Path(rel)).resolve()
    if not path.is_relative_to((version / "editions").resolve()) or not path.is_file():
        raise ValueError("chemin de manifeste invalide")
    return path


def _set_manifest_slot(version: Path, set_id: str, printing_id: str, face_index: int,
                       *, status: str, language: str | None, relative_path: str | None):
    path = _edition_images_manifest(version, set_id)
    doc = json.loads(path.read_text(encoding="utf-8"))
    slot = next((x for x in doc.get("slots", []) if x.get("printing_id") == printing_id and x.get("face_index") == face_index), None)
    if slot is None: raise ValueError("slot absent du manifeste")
    slot.update({"status": status, "selected_language": language, "path": relative_path})
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _fetch(url: str, session, test_hosts: set[str] | None = None,
           allowed_hosts: set[str] | None = None,
           timeout: tuple[float, float] = (8, 20), cancel_event=None,
           batch_rate_limit: bool = False) -> tuple[bytes | None, str | None, float | None]:
    global _BATCH_IMAGE_NEXT_REQUEST, _BATCH_IMAGE_BLOCKED_UNTIL
    if cancel_event is not None and cancel_event.is_set():
        return None, "annule", None
    if not _allowed_url(url, test_hosts, allowed_hosts):
        return None, "url_non_autorisee", None
    if batch_rate_limit and urlsplit(url).hostname == "cards.scryfall.io":
        with _BATCH_IMAGE_RATE_LOCK:
            scheduled = max(time.monotonic(), _BATCH_IMAGE_NEXT_REQUEST, _BATCH_IMAGE_BLOCKED_UNTIL)
            _BATCH_IMAGE_NEXT_REQUEST = scheduled + _BATCH_IMAGE_MIN_INTERVAL
        if cancel_event is not None:
            if cancel_event.wait(max(0.0, scheduled - time.monotonic())):
                return None, "annule", None
        else:
            time.sleep(max(0.0, scheduled - time.monotonic()))
    try:
        response = session.get(url, timeout=timeout, allow_redirects=False, stream=True,
                               headers={"User-Agent": "BibliothequeMagicLocal/2.0", "Accept": "image/*"})
    except requests.RequestException as exc:
        return None, f"erreur_reseau:{type(exc).__name__}", None
    try:
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "60")
            try:
                retry_value = max(0.0, float(retry))
            except ValueError:
                try:
                    retry_time = parsedate_to_datetime(retry)
                    if retry_time.tzinfo is None: retry_time = retry_time.replace(tzinfo=timezone.utc)
                    retry_value = max(0.0, (retry_time - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError): retry_value = 60.0
            if batch_rate_limit and urlsplit(url).hostname == "cards.scryfall.io":
                with _BATCH_IMAGE_RATE_LOCK:
                    _BATCH_IMAGE_BLOCKED_UNTIL = max(_BATCH_IMAGE_BLOCKED_UNTIL,
                                                      time.monotonic() + retry_value)
            return None, "limite_debit_429", retry_value
        if response.status_code == 403:
            return None, "acces_refuse_403", None
        if response.status_code in (301, 302, 303, 307, 308):
            return None, "redirection_non_autorisee", None
        if response.status_code != 200:
            return None, f"http_{response.status_code}", None
        content_type = response.headers.get("Content-Type", "").casefold()
        if "image/" not in content_type:
            return None, "contenu_non_image", None
        size = 0
        chunks = []
        for chunk in response.iter_content(65536):
            if cancel_event is not None and cancel_event.is_set(): return None, "annule", None
            if not chunk: continue
            size += len(chunk)
            if size > _MAX_BYTES: return None, "taille_depassee", None
            chunks.append(chunk)
        return b"".join(chunks), None, None
    finally:
        response.close()


def _convertible(data: bytes, *, minimum_side: int = _MIN_SOURCE_SIDE):
    image = _read_webp(data)
    width, height = image.size
    if width < minimum_side or height < minimum_side:
        image.close()
        raise ValueError("resolution_insuffisante")
    rgb = image.convert("RGB")
    variance = max(ImageStat.Stat(rgb).stddev)
    rgb.close()
    if variance < 2:
        image.close()
        raise ValueError("image_vide")
    image.close()
    return width, height


def _publish_face(db: Path, version: Path, printing: dict, face_index: int, language: str,
                  source_url: str, source_data: bytes, source_width: int, source_height: int,
                  *, deep_refresh: bool = False, source_provider: str = "scryfall",
                  source_evidence: str | None = None, source_record_id: str | None = None) -> dict:
    webp, width, height = _encode_webp(source_data, method=4 if source_provider == "historical_import" else 6)
    digest = hashlib.sha256(webp).hexdigest()
    relative = f"assets/webp/{language}/{digest}.webp"
    output = (version / relative).resolve()
    if not output.is_relative_to(version.resolve()): raise ValueError("chemin d’image invalide")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = _edition_images_manifest(version, printing["set_id"])
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    slot = next((s for s in doc.get("slots", []) if s.get("printing_id") == printing["printing_id"] and s.get("face_index") == face_index), None)
    if slot is None: raise ValueError("slot d’image absent du manifeste")
    old_doc = json.loads(json.dumps(doc))
    provenance = json.dumps({"provider": source_provider, "source_url": source_url,
                             "source_sha256": hashlib.sha256(source_data).hexdigest(),
                             "source_dimensions": [source_width, source_height],
                             "output_dimensions": [width, height],
                             "selected_language": language, "quality": "normalized_531x738",
                             "acquisition_mode": "deep_refresh" if deep_refresh else "standard",
                             "source_evidence": source_evidence,
                             "source_record_id": source_record_id}, ensure_ascii=False, sort_keys=True)
    temp = output.with_suffix(".webp.part")
    try:
        temp.write_bytes(webp)
        with Image.open(temp) as check:
            check.verify()
        # The content-addressed target can already exist but be corrupted on
        # disk. Replacing it atomically also makes an explicit repair effective.
        os.replace(temp, output)
        slot.update({"path": relative, "selected_language": language, "status": "ready"})
        mtemp = manifest.with_suffix(".json.tmp")
        mtemp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(mtemp, manifest)
        conn = sqlite3.connect(db, timeout=10)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO image_assets VALUES(?,?,?,?,?,?,?,?)",
                         (digest, relative, "image/webp", len(webp), width, height, language, provenance))
            update = conn.execute("UPDATE image_slots SET selected_language=?,status='ready',asset_id=?,provenance_json=? WHERE printing_id=? AND face_index=?",
                                  (language, digest, provenance, printing["printing_id"], face_index))
            if update.rowcount != 1: raise ValueError("slot non mis à jour")
            conn.commit()
        except Exception:
            conn.rollback()
            manifest.write_text(json.dumps(old_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if not conn.execute("SELECT 1 FROM image_assets WHERE asset_id=?", (digest,)).fetchone(): output.unlink(missing_ok=True)
            raise
        finally:
            conn.close()
    finally:
        temp.unlink(missing_ok=True)
    from .organized_images import mirror_printing_face
    mirror_printing_face(db, version, printing["printing_id"], face_index)
    return {"face_index": face_index, "status": "ready", "language": language, "relative_path": relative,
            "width": width, "height": height, "source_width": source_width, "source_height": source_height}


def download_printing(provider_id: str, *, catalogue_root: Path, ledger_path: Path, session=None,
                      test_hosts: set[str] | None = None, face_indexes: list[int] | None = None,
                      cancel_event=None, repair: bool = False, _deep_refresh: bool = False,
                      _preview_only: bool = False, parallel_fetch: bool = False) -> dict:
    current = resolve_current(catalogue_root)
    db, version = current["database"], current["database"].parent
    ensure_catalogue_wal(db)
    with connect(db) as conn:
        row = conn.execute("SELECT p.printing_id,p.set_id,e.code,e.name,e.eligibility,p.collector_number,p.layout,p.oracle_id "
                           "FROM source_records sr JOIN printings p USING(printing_id) JOIN editions e USING(set_id) WHERE sr.provider_card_id=?",
                           (provider_id,)).fetchone()
        if row is None: raise ValueError("impression inconnue")
        printing = dict(row)
        if printing["eligibility"] != "eligible": raise ValueError("édition différée : téléchargement désactivé")
        slots = [dict(r) for r in conn.execute("SELECT s.face_index,s.status,s.selected_language,s.asset_id,a.relative_path "
                                               "FROM image_slots s LEFT JOIN image_assets a USING(asset_id) "
                                               "WHERE s.printing_id=? ORDER BY s.face_index", (printing["printing_id"],))]
        sources = [dict(r) for r in conn.execute("SELECT lang,raw_json FROM source_records WHERE printing_id=? AND lang IN ('fr','en','ja','ph') ORDER BY lang", (printing["printing_id"],))]
    raws = {s["lang"]: json.loads(s["raw_json"]) for s in sources}
    french_name = raws.get("fr", {}).get("printed_name") or ""
    if not french_name and printing["oracle_id"]:
        with connect(db) as conn:
            translation = conn.execute(
                "SELECT sr.printed_name FROM source_records sr JOIN printings p USING(printing_id) "
                "WHERE p.oracle_id=? AND sr.lang='fr' AND sr.printed_name IS NOT NULL LIMIT 1",
                (printing["oracle_id"],)).fetchone()
        french_name = translation[0] if translation else ""
    reviewed_wizards = _reviewed_wizards_reference(provider_id, printing)
    if not french_name and reviewed_wizards:
        french_name = reviewed_wizards["name"]
    if not slots: raise ValueError("impression sans face physique")
    if face_indexes is not None:
        requested = {int(i) for i in face_indexes}
        if not requested or not requested.issubset({int(slot["face_index"]) for slot in slots}):
            raise ValueError("face demandée invalide")
        slots = [slot for slot in slots if int(slot["face_index"]) in requested]
    s = session or requests.Session()
    with _LOCK:
        ledger = _load_ledger(ledger_path)
        results, attempts = [], []
        live_fr_raw = None
        if _deep_refresh:
            api_key = f"scryfall-api:{printing['printing_id']}:fr"
            api_entry = ledger["faces"].setdefault(api_key, {"gets": 0, "errors": []})
            now = time.time()
            if cancel_event is not None and cancel_event.is_set():
                lookup_status = "cancelled"
            elif float(api_entry.get("retry_after_epoch", 0)) > now:
                lookup_status = "rate_limited_cached"
            elif float(api_entry.get("not_found_until_epoch", 0)) > now:
                lookup_status = "not_found_cached"
            elif api_entry.get("blocked"):
                lookup_status = "blocked_cached"
            else:
                expected_raw = raws.get("en") or raws.get("fr") or raws.get("ph")
                if expected_raw is None:
                    lookup_status = "identity_source_unavailable"
                else:
                    live_fr_raw, lookup_status, retry = _fetch_live_fr(
                        printing["code"], printing["collector_number"], s, cancel_event=cancel_event)
                    api_entry["gets"] = int(api_entry.get("gets", 0)) + 1
                    api_entry["last_checked_epoch"] = time.time()
                    if lookup_status == "rate_limited":
                        api_entry["retry_after_epoch"] = time.time() + float(retry or 60)
                    elif lookup_status == "blocked":
                        api_entry["blocked"] = True
                    elif lookup_status == "not_found":
                        api_entry["not_found_until_epoch"] = time.time() + 3600
                    if lookup_status not in {"candidate_found", "not_found", "cancelled"}:
                        api_entry.setdefault("errors", []).append(lookup_status)
                    if live_fr_raw is not None:
                        if _same_printing_identity(live_fr_raw, printing, expected_raw):
                            lookup_status = "candidate_identity_verified"
                        else:
                            live_fr_raw = None
                            lookup_status = "candidate_identity_rejected"
                            api_entry.setdefault("errors", []).append(lookup_status)
                    _save_ledger(ledger_path, ledger)
            attempts.append({"kind": "metadata", "source": "scryfall_api", "language": "fr",
                             "status": lookup_status})
        for slot in slots:
            if cancel_event is not None and cancel_event.is_set():
                results.append({"face_index": slot["face_index"], "status": "cancelled"}); continue
            with connect(db) as conn:
                live_status = conn.execute("SELECT status FROM image_slots WHERE printing_id=? AND face_index=?",
                                           (printing["printing_id"], slot["face_index"])).fetchone()
            if live_status is None:
                raise ValueError("slot d’image disparu")
            current_quality = 0
            if _deep_refresh and live_status["status"] == "ready":
                with connect(db) as conn:
                    current_asset = conn.execute("SELECT a.asset_id,a.relative_path,a.width,a.height,a.provenance_json "
                                                  "FROM image_slots s LEFT JOIN image_assets a USING(asset_id) "
                                                  "WHERE s.printing_id=? AND s.face_index=?",
                                                  (printing["printing_id"], slot["face_index"])).fetchone()
                if current_asset and current_asset["relative_path"]:
                    current_file = (version / current_asset["relative_path"]).resolve()
                    try:
                        if (current_file.is_relative_to(version.resolve()) and current_file.is_file()
                                and hashlib.sha256(current_file.read_bytes()).hexdigest() == current_asset["asset_id"]):
                            previous = json.loads(current_asset["provenance_json"] or "{}")
                            previous_dimensions = previous.get("source_dimensions") or [current_asset["width"], current_asset["height"]]
                            current_quality = int(previous_dimensions[0]) * int(previous_dimensions[1])
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        current_quality = 0
            if live_status["status"] == "ready" and not _deep_refresh:
                with connect(db) as conn:
                    asset = conn.execute("SELECT s.asset_id,s.selected_language,a.relative_path,a.width,a.height,a.provenance_json FROM image_slots s LEFT JOIN image_assets a USING(asset_id) WHERE s.printing_id=? AND s.face_index=?",
                                         (printing["printing_id"], slot["face_index"])).fetchone()
                file = (version / asset["relative_path"]).resolve() if asset and asset["relative_path"] else None
                valid = bool(file and file.is_relative_to(version.resolve()) and file.is_file())
                needs_normalization = False
                if valid:
                    try:
                        if hashlib.sha256(file.read_bytes()).hexdigest() != asset["asset_id"]: valid = False
                        else:
                            with Image.open(file) as image:
                                if image.format != "WEBP": valid = False
                                else:
                                    image.verify()
                                    needs_normalization = image.size != _OUTPUT_SIZE
                    except (UnidentifiedImageError, OSError, ValueError): valid = False
                if valid:
                    if needs_normalization:
                        try:
                            old_provenance = json.loads(asset["provenance_json"] or "{}")
                        except (TypeError, json.JSONDecodeError):
                            old_provenance = {}
                        source_dimensions = old_provenance.get("source_dimensions") or [asset["width"], asset["height"]]
                        normalized = _publish_face(
                            db, version, printing, slot["face_index"],
                            asset["selected_language"] or old_provenance.get("selected_language") or "en",
                            old_provenance.get("source_url") or "local-existing-webp",
                            file.read_bytes(), int(source_dimensions[0]), int(source_dimensions[1]))
                        normalized["status"] = "normalized_existing"
                        results.append(normalized)
                        continue
                    results.append({"face_index": slot["face_index"], "status": "already_ready"}); continue
                if not repair:
                    results.append({"face_index": slot["face_index"], "status": "corrupt_requires_repair"}); continue
                with sqlite3.connect(db, timeout=10) as conn:
                    conn.execute("UPDATE image_slots SET status='missing',selected_language=NULL,asset_id=NULL,provenance_json=? WHERE printing_id=? AND face_index=?",
                                 (json.dumps({"reason":"explicit_repair_requested"}), printing["printing_id"], slot["face_index"]))
                _set_manifest_slot(version, printing["set_id"], printing["printing_id"], slot["face_index"],
                                   status="missing", language=None, relative_path=None)
            face_index = slot["face_index"]
            pools = {lang: _ordered_uris(raw, face_index, full_card_only=_preview_only) for lang, raw in raws.items()}
            live_fr_urls = _ordered_uris(live_fr_raw, face_index, full_card_only=_preview_only) if live_fr_raw is not None else []
            if live_fr_raw is not None:
                pools["fr"] = live_fr_urls + pools.get("fr", [])
            manual_by_url = {}
            if _deep_refresh:
                for candidate in _manual_candidates(printing["code"], printing["collector_number"], face_index):
                    manual_by_url[candidate["url"]] = candidate
                    pools.setdefault(candidate["language"], []).append(candidate["url"])
            if test_hosts is None and face_index == 0 and (_preview_only or not pools.get("fr")):
                english_name = (raws.get("en") or raws.get("fr") or {}).get("name") or ""
                gallery_candidates = (_official_gallery_images(
                    printing["name"], french_name, s, english_name=english_name,
                    collector_number=printing["collector_number"], set_code=printing["code"],
                    exact_only=not _preview_only)
                    if (french_name or english_name) and (_preview_only or not reviewed_wizards) else [])
                if reviewed_wizards:
                    gallery_candidates.insert(0, reviewed_wizards)
                for candidate in gallery_candidates:
                    manual_by_url[candidate["url"]] = candidate
                pools["fr"] = list(dict.fromkeys(
                    [candidate["url"] for candidate in gallery_candidates] + pools.get("fr", [])))
                base_number = str(printing["collector_number"]).removesuffix("★")
                if _preview_only and base_number.isdigit() and french_name:
                    lotus_url = (f"https://www.lotusnoir.info/cartes/magic_hq/"
                                 f"{printing['code'].upper()}/{printing['code'].upper()}{base_number}.jpg")
                    manual_by_url[lotus_url] = {"url": lotus_url, "language": "fr", "source": "lotusnoir",
                                                "allow_host": "www.lotusnoir.info",
                                                "evidence": f"{printing['code']}:{printing['collector_number']}:{french_name}"}
                    pools.setdefault("fr", []).append(lotus_url)
            got: dict[str, list[tuple[bytes,int,int,str]]] = {"fr": [], "en": [], "ja": [], "ph": []}
            chosen = None; deferred = None
            deep_run_id = uuid.uuid4().hex if _deep_refresh else None
            for language in ("fr", "en", "ja", "ph"):
                key = (f"deep:{deep_run_id}:{printing['printing_id']}:{face_index}:{language}"
                       if _deep_refresh else f"{printing['printing_id']}:{face_index}:{language}")
                ledger_entry = ledger["faces"].get(key, {})
                if float(ledger_entry.get("retry_after_epoch", 0)) > time.time():
                    deferred = "limite_debit_429"
                    break
                used = int(ledger_entry.get("gets", 0))
                urls = pools.get(language, [])
                if _deep_refresh:
                    urls = list(dict.fromkeys(urls))
                for url in urls:
                    if cancel_event is not None and cancel_event.is_set():
                        deferred = "annule"; break
                    if used >= (6 if _preview_only else _MAX_GETS): break
                    manual_source = manual_by_url.get(url)
                    allowed_hosts = {str(manual_source["allow_host"])} if manual_source else None
                    if not _allowed_url(url, test_hosts, allowed_hosts):
                        attempts.append({"face": face_index, "language": language, "url_rejected": True}); continue
                    if parallel_fetch:
                        # Reserve this attempt before releasing the ledger lock.
                        entry = ledger["faces"].setdefault(key, {"gets": 0, "errors": []})
                        entry["gets"] = used + 1
                        _save_ledger(ledger_path, ledger)
                        _LOCK.release()
                        try:
                            data, error, retry = _fetch(url, s, test_hosts, allowed_hosts=allowed_hosts,
                                                        cancel_event=cancel_event, batch_rate_limit=True)
                        finally:
                            _LOCK.acquire()
                        ledger = _load_ledger(ledger_path)
                        used = int(ledger["faces"].get(key, {}).get("gets", used + 1))
                    else:
                        data, error, retry = _fetch(url, s, test_hosts, allowed_hosts=allowed_hosts,
                                                    cancel_event=cancel_event)
                        used += 1
                    entry = ledger["faces"].setdefault(key, {"gets": 0, "errors": []}); entry["gets"] = used
                    _save_ledger(ledger_path, ledger)
                    if error:
                        entry.setdefault("errors", []).append(error)
                        attempts.append({"face": face_index, "language": language, "error": error, "retry_after": retry})
                        if error == "limite_debit_429":
                            entry["retry_after_epoch"] = time.time() + float(retry or 60); deferred = error; break
                        if error == "annule": deferred = error; break
                        if error == "acces_refuse_403": deferred = error; break
                        continue
                    try:
                        minimum = 1 if _preview_only or manual_source else _MIN_SOURCE_SIDE
                        width, height = _convertible(data, minimum_side=minimum)
                    except ValueError as exc:
                        entry.setdefault("errors", []).append("image_invalide")
                        attempts.append({"face": face_index, "language": language, "error": "image_invalide"}); continue
                    got[language].append((data, width, height, url))
                    exact_official = bool(manual_source and manual_source.get("source") in
                                          {"wizards_official_reviewed", "wizards_product_gallery",
                                           "wizards_contentful"})
                    if not _deep_refresh and ((width >= _MIN_SOURCE_SIDE and height >= _MIN_SOURCE_SIDE)
                                              or exact_official):
                        chosen = (language, data, width, height, url); break
                _save_ledger(ledger_path, ledger)
                if _deep_refresh and got[language]:
                    data, width, height, url = max(got[language], key=lambda x: x[1] * x[2])
                    chosen = (language, data, width, height, url)
                if (chosen and not _preview_only) or deferred: break
            if _preview_only:
                candidates = []
                seen = set()
                with connect(db) as conn:
                    previous = conn.execute(
                        "SELECT provenance_json FROM image_slots WHERE printing_id=? AND face_index=?",
                        (printing["printing_id"], face_index)).fetchone()
                try:
                    old_source = json.loads(previous[0] or "{}") if previous else {}
                except (TypeError, json.JSONDecodeError):
                    old_source = {}
                for lang in ("fr", "en", "ja", "ph"):
                    for data, width, height, url in got[lang]:
                        digest = hashlib.sha256(data).hexdigest()
                        if digest in seen or digest == old_source.get("source_sha256") or url == old_source.get("source_url"):
                            continue
                        seen.add(digest)
                        manual_source = manual_by_url.get(url)
                        is_live_fr = lang == "fr" and url in live_fr_urls
                        candidates.append({
                            "provider_card_id": provider_id, "printing_id": printing["printing_id"],
                            "face_index": int(face_index), "version_id": current["version_id"],
                            "old_asset_id": slot["asset_id"], "old_status": slot["status"],
                            "language": lang, "source_url": url, "source_data": data,
                            "mime_type": _image_mime_type(data), "source_width": width,
                            "source_height": height,
                            "source_provider": (manual_source["source"] if manual_source else
                                                "scryfall_api" if is_live_fr else "scryfall"),
                            "source_evidence": manual_source["evidence"] if manual_source else None,
                            "source_record_id": (live_fr_raw.get("id") if is_live_fr else
                                                 raws.get(lang, {}).get("id")),
                        })
                results.append({"face_index": face_index,
                                "status": "candidate" if candidates else "missing",
                                "candidates": candidates})
                continue
            if chosen is None:
                fallback_lang = next((lang for lang in ("fr", "en", "ja", "ph") if got[lang]), None)
                if fallback_lang:
                    data, width, height, url = max(got[fallback_lang], key=lambda x: x[1]*x[2])
                    chosen = (fallback_lang, data, width, height, url)
            if chosen and parallel_fetch and not _deep_refresh:
                # An explicit import can publish while this batch is fetching.
                # Keep the image selected by the user if it won that race.
                with connect(db) as conn:
                    latest_status = conn.execute(
                        "SELECT status FROM image_slots WHERE printing_id=? AND face_index=?",
                        (printing["printing_id"], face_index)).fetchone()
                if latest_status and latest_status["status"] == "ready":
                    results.append({"face_index": face_index, "status": "already_ready"})
                    continue
            if chosen:
                lang, data, width, height, url = chosen
                manual_source = manual_by_url.get(url)
                candidate_quality = width * height
                if _deep_refresh and not _preview_only and current_quality and candidate_quality < current_quality:
                    results.append({"face_index": face_index, "status": "lower_quality_kept_existing",
                                    "candidate_language": lang, "candidate_source_dimensions": [width, height]})
                    continue
                is_live_fr = bool(lang == "fr" and url in live_fr_urls)
                source_provider = (manual_source["source"] if manual_source else
                                   "scryfall_api" if is_live_fr else "scryfall")
                source_evidence = manual_source["evidence"] if manual_source else None
                source_record_id = (live_fr_raw.get("id") if is_live_fr else
                                    raws.get(lang, {}).get("id"))
                if _preview_only:
                    results.append({"face_index": face_index, "status": "candidate", "candidate": {
                        "provider_card_id": provider_id,
                        "printing_id": printing["printing_id"],
                        "face_index": int(face_index),
                        "version_id": current["version_id"],
                        "old_asset_id": slot["asset_id"],
                        "old_status": slot["status"],
                        "language": lang,
                        "source_url": url,
                        "source_data": data,
                        "mime_type": _image_mime_type(data),
                        "source_width": width,
                        "source_height": height,
                        "source_provider": source_provider,
                        "source_evidence": source_evidence,
                        "source_record_id": source_record_id,
                    }})
                    continue
                try:
                    published = _publish_face(db, version, printing, face_index, lang, url, data, width, height,
                                              deep_refresh=_deep_refresh,
                                              source_provider=source_provider,
                                              source_evidence=source_evidence,
                                              source_record_id=source_record_id)
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).casefold() and not _deep_refresh:
                        # The GET returned a valid image. A local commit failure
                        # must not consume the final network attempt forever.
                        retry_key = f"{printing['printing_id']}:{face_index}:{lang}"
                        entry = ledger["faces"].get(retry_key)
                        if entry:
                            entry["gets"] = max(0, int(entry.get("gets", 0)) - 1)
                            _save_ledger(ledger_path, ledger)
                    raise
                results.append(published)
            else:
                failed_status = ("cancelled" if deferred == "annule" else
                                 "retry_later" if deferred == "limite_debit_429" else
                                 "blocked" if deferred == "acces_refuse_403" else
                                 "refresh_failed_kept_existing" if _deep_refresh and live_status["status"] == "ready" else
                                 "missing")
                results.append({"face_index": face_index, "status": failed_status,
                                "reason": deferred or "aucune_image_valide"})
        return {"printing_id": printing["printing_id"], "edition": printing["code"], "collector_number": printing["collector_number"],
                "results": results, "attempts": attempts, "max_gets_per_language_face": _MAX_GETS}


def refresh_printing_deep(provider_id: str, *, catalogue_root: Path, ledger_path: Path, session=None,
                          test_hosts: set[str] | None = None, face_indexes: list[int] | None = None,
                          cancel_event=None) -> dict:
    """Force a bounded, FR-first quality pass for one exact provider printing ID."""
    report = download_printing(provider_id, catalogue_root=catalogue_root, ledger_path=ledger_path,
                               session=session, test_hosts=test_hosts, face_indexes=face_indexes,
                               cancel_event=cancel_event, _deep_refresh=True)
    report["mode"] = "deep_refresh"
    return report


def _image_mime_type(data: bytes) -> str:
    """Return the MIME type Pillow decoded from the validated image bytes."""
    with Image.open(__import__("io").BytesIO(data)) as image:
        image.verify()
        image_format = (image.format or "").casefold()
    mime_types = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
                  "gif": "image/gif", "bmp": "image/bmp", "tiff": "image/tiff"}
    return mime_types.get(image_format, "application/octet-stream")


def preview_printing_candidate(provider_id: str, *, catalogue_root: Path, ledger_path: Path,
                               face_index: int, session=None, test_hosts: set[str] | None = None,
                               cancel_event=None) -> dict:
    """Find a validated deep-refresh image candidate without publishing it."""
    report = download_printing(provider_id, catalogue_root=catalogue_root, ledger_path=ledger_path,
                               session=session, test_hosts=test_hosts, face_indexes=[face_index],
                               cancel_event=cancel_event, _deep_refresh=True, _preview_only=True)
    result = report["results"][0]
    response = {"status": result["status"]}
    if result["status"] == "candidate":
        response["candidates"] = result["candidates"]
    else:
        response["reason"] = result.get("reason")
    return response


def publish_preview_candidate(candidate: dict, *, catalogue_root: Path,
                              provider_card_id: str, face_index: int) -> dict:
    """Publish a preview only if the active identity and previous slot still match."""
    with _LOCK:
        current = resolve_current(catalogue_root)
        if candidate.get("version_id") != current["version_id"]:
            raise ValueError("candidate périmée : la version active a changé")
        if candidate.get("provider_card_id") != provider_card_id:
            raise ValueError("candidate périmée : identifiant fournisseur différent")
        if candidate.get("face_index") != face_index:
            raise ValueError("candidate périmée : index de face différent")
        db, version = current["database"], current["database"].parent
        with connect(db) as conn:
            row = conn.execute("SELECT p.printing_id,p.set_id,e.code,e.name,e.eligibility,p.collector_number,p.layout "
                               "FROM source_records sr JOIN printings p USING(printing_id) JOIN editions e USING(set_id) "
                               "WHERE sr.provider_card_id=?", (provider_card_id,)).fetchone()
            if row is None or row["printing_id"] != candidate.get("printing_id"):
                raise ValueError("candidate périmée : identité d’impression différente")
            if row["eligibility"] != "eligible":
                raise ValueError("édition différée : publication désactivée")
            slot = conn.execute("SELECT status,asset_id FROM image_slots WHERE printing_id=? AND face_index=?",
                                (row["printing_id"], face_index)).fetchone()
            if slot is None:
                raise ValueError("candidate périmée : slot d’image absent")
            if (slot["asset_id"] != candidate.get("old_asset_id")
                    or slot["status"] != candidate.get("old_status")):
                raise ValueError("candidate périmée : le slot d’image a changé")
        source_data = candidate.get("source_data")
        if not isinstance(source_data, bytes):
            raise ValueError("candidate invalide : source_data doit contenir des octets")
        width, height = _convertible(source_data, minimum_side=1)
        if (width != candidate.get("source_width") or height != candidate.get("source_height")
                or _image_mime_type(source_data) != candidate.get("mime_type")):
            raise ValueError("candidate invalide : métadonnées de l’image incohérentes")
        language = candidate.get("language")
        if language not in {"fr", "en", "ja", "ph"}:
            raise ValueError("candidate invalide : langue de source inconnue")
        latest = resolve_current(catalogue_root)
        if latest["version_id"] != current["version_id"]:
            raise ValueError("candidate périmée : la version active a changé")
        printing = dict(row)
        return _publish_face(db, version, printing, face_index, language,
                             candidate["source_url"], source_data, width, height,
                             deep_refresh=True, source_provider=candidate.get("source_provider") or "scryfall",
                             source_evidence=candidate.get("source_evidence"),
                             source_record_id=candidate.get("source_record_id"))


def import_local_image(provider_card_id: str, face_index: int, source_data: bytes,
                       *, catalogue_root: Path, language: str = "en",
                       source_provider: str = "manual_import",
                       source_evidence: str | None = None,
                       only_missing: bool = False) -> dict:
    """Publish a user-selected image to the exact shared printing/face slot."""
    if language not in {"fr", "en"} or source_provider not in {"manual_import", "historical_import"}:
        raise ValueError("langue ou origine d'image invalide")
    width, height = _convertible(source_data, minimum_side=1)
    with _LOCK:
        current = resolve_current(catalogue_root)
        db = current["database"]
        ensure_catalogue_wal(db)
        with connect(db) as conn:
            row = conn.execute(
                "SELECT p.printing_id,p.set_id,e.eligibility FROM source_records sr "
                "JOIN printings p USING(printing_id) JOIN editions e USING(set_id) "
                "WHERE sr.provider_card_id=?", (provider_card_id,)).fetchone()
            if row is None or row["eligibility"] != "eligible":
                raise ValueError("impression inconnue ou édition différée")
            slot = conn.execute("SELECT status FROM image_slots WHERE printing_id=? AND face_index=?",
                                (row["printing_id"], face_index)).fetchone()
            if slot is None:
                raise ValueError("face d’image inconnue")
            if only_missing and slot["status"] == "ready":
                raise ValueError("une image est déjà présente pour cette face")
        return _publish_face(db, db.parent, dict(row), face_index, language, source_provider,
                             source_data, width, height, source_provider=source_provider,
                             source_evidence=source_evidence)
