"""Local index and on-demand loading for MTGJSON preconstructed decks."""
from __future__ import annotations

import hashlib
import gzip
import json
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "1.0.0"
DECK_LIST_URL = "https://mtgjson.com/api/v5/DeckList.json"
DECK_FILE_BASE_URL = "https://mtgjson.com/api/v5/decks/"
PRECON_ROWS_URL = "https://datasets-server.huggingface.co/rows"
PRECON_DATASET = "kasper-cbx/mtg-search-index"
PRECON_CONFIG = "precons"
PRECON_SPLIT = "train"
DEFAULT_DB = Path(__file__).resolve().parents[3] / "state" / "preconstructed-decks.sqlite"
SECTIONS = ("mainboard", "sideboard", "commander", "display_commander", "planar_deck", "scheme_deck", "tokens")
MAX_PAGE_SIZE = 200
SUPPORTED_DECK_TYPES = (
    "Advanced Deck", "Archenemy Deck", "Arena Promotional Deck", "Arena Starter Deck", "Arena Starter Kit",
    "Brawl Deck", "Challenge Deck", "Challenger Deck", "Clash Pack", "Dandan Deck",
    "Commander Deck", "Demo Deck", "Duel Deck", "Duel Of The Planeswalkers Deck",
    "Enemy Deck", "Enhanced Deck", "Event Deck", "Game Night Deck", "Guild Kit",
    "Halfdeck", "Historic Brawl Precon Deck", "Intro Pack", "Jumpstart",
    "Modern Event Deck", "MTGO Commander Deck", "MTGO Duel Deck", "MTGO Theme Deck",
    "Pioneer Challenger Deck", "Planechase Deck", "Planeswalker Deck", "Premium Deck",
    "Pro Tour Deck", "Sample Deck", "Shandalar Enemy Deck", "Spellslinger Starter Kit",
    "Starter Deck", "Starter Kit", "Theme Deck", "Welcome Deck", "World Championship Deck",
)
_WHO_DECK_NAMES_FR = {
    "BlastFromThePast_WHO": "Voyage dans le passé",
    "TimeyWimey_WHO": "Méli-mélo spatio-temporel",
    "ParadoxPower_WHO": "Puissance paradoxale",
    "MastersOfEvil_WHO": "Maîtres du mal",
}
_SET_NAMES_FR = json.loads((Path(__file__).resolve().parent / "data" / "set_names_fr.json").read_text(encoding="utf-8"))["names"]


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT,
            set_code TEXT,
            set_name TEXT,
            release_date TEXT,
            colors TEXT,
            file_name TEXT UNIQUE,
            source TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            added_at TEXT NOT NULL,
            loaded INTEGER NOT NULL DEFAULT 0 CHECK (loaded IN (0,1)),
            tokens_loaded INTEGER NOT NULL DEFAULT 0 CHECK (tokens_loaded IN (0,1))
        );
        CREATE INDEX IF NOT EXISTS decks_name_idx ON decks(name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS decks_set_idx ON decks(set_code, release_date);
        CREATE TABLE IF NOT EXISTS deck_cards (
            deck_id TEXT NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
            section TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            name TEXT NOT NULL,
            count INTEGER NOT NULL CHECK(count > 0),
            set_code TEXT,
            collector_number TEXT,
            mtgjson_uuid TEXT,
            foil INTEGER NOT NULL DEFAULT 0 CHECK(foil IN (0,1)),
            raw_json TEXT NOT NULL,
            PRIMARY KEY(deck_id, section, ordinal)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS deck_cards_uuid_idx ON deck_cards(mtgjson_uuid);
        CREATE TABLE IF NOT EXISTS deck_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(decks)")}
    if "added_at" not in columns:
        conn.execute("ALTER TABLE decks ADD COLUMN added_at TEXT NOT NULL DEFAULT ''")
    added_date_migration = conn.execute("SELECT value FROM deck_meta WHERE key='added_at_release_date_baseline'").fetchone()
    if not added_date_migration:
        conn.execute("""UPDATE decks SET added_at=CASE
            WHEN release_date IS NOT NULL AND trim(release_date)!=''
              THEN substr(release_date,1,10)||'T00:00:00Z'
            ELSE strftime('%Y-%m-%dT%H:%M:%fZ','now') END""")
        conn.execute("INSERT OR REPLACE INTO deck_meta(key,value) VALUES('added_at_release_date_baseline','1')")
    conn.execute("CREATE INDEX IF NOT EXISTS decks_added_idx ON decks(added_at)")
    if "tokens_loaded" not in columns:
        conn.execute("ALTER TABLE decks ADD COLUMN tokens_loaded INTEGER NOT NULL DEFAULT 0 CHECK (tokens_loaded IN (0,1))")
    token_migration = conn.execute("SELECT value FROM deck_meta WHERE key='tokens_all_parts_migration'").fetchone()
    if not token_migration:
        # Older builds marked an empty MTGJSON `tokens` array as complete.
        # Reopen those cached compositions for an on-demand catalog derivation.
        conn.execute("""UPDATE decks SET tokens_loaded=0 WHERE tokens_loaded=1
            AND NOT EXISTS (SELECT 1 FROM deck_cards c WHERE c.deck_id=decks.id AND c.section='tokens')""")
        conn.execute("INSERT OR REPLACE INTO deck_meta(key,value) VALUES('tokens_all_parts_migration','1')")
    edition_migration = conn.execute("SELECT value FROM deck_meta WHERE key='tokens_companion_edition_migration'").fetchone()
    if not edition_migration:
        # Previously derived tokens could retain an all_parts printing from an
        # unrelated set. Remove only those rows and rebuild their deck locally.
        candidates = conn.execute("""SELECT c.deck_id,c.ordinal,c.set_code,c.raw_json,d.set_code AS deck_set
            FROM deck_cards c JOIN decks d ON d.id=c.deck_id WHERE c.section='tokens'""").fetchall()
        for card in candidates:
            raw = json.loads(card["raw_json"])
            expected = "T" + str(card["deck_set"] or "").upper()
            if raw.get("all_parts_source") and str(card["set_code"] or "").upper() != expected:
                conn.execute("DELETE FROM deck_cards WHERE deck_id=? AND section='tokens' AND ordinal=?",
                             (card["deck_id"], card["ordinal"]))
                conn.execute("UPDATE decks SET tokens_loaded=0 WHERE id=?", (card["deck_id"],))
        conn.execute("INSERT INTO deck_meta(key,value) VALUES('tokens_companion_edition_migration','1')")
    if "colors" not in columns:
        conn.execute("ALTER TABLE decks ADD COLUMN colors TEXT")
    if "name_fr" not in columns:
        conn.execute("ALTER TABLE decks ADD COLUMN name_fr TEXT")
    if "set_name_fr" not in columns:
        conn.execute("ALTER TABLE decks ADD COLUMN set_name_fr TEXT")
    if "name_fr_search" not in columns:
        conn.execute("ALTER TABLE decks ADD COLUMN name_fr_search TEXT")
    if "set_name_fr_search" not in columns:
        conn.execute("ALTER TABLE decks ADD COLUMN set_name_fr_search TEXT")
    for file_name, french_name in _WHO_DECK_NAMES_FR.items():
        conn.execute("""UPDATE decks SET name_fr=?,name_fr_search=?,set_name_fr='Doctor Who',
            set_name_fr_search='doctor who' WHERE file_name=? AND (name_fr IS NULL OR name_fr='')""",
            (french_name, _normalize_search(french_name), file_name))
    for code, french_name in _SET_NAMES_FR.items():
        conn.execute("""UPDATE decks SET set_name_fr=?,set_name_fr_search=?
            WHERE upper(set_code)=? AND (set_name_fr IS NULL OR set_name_fr='')""",
            (french_name, _normalize_search(french_name), code.upper()))
    for row in conn.execute("SELECT id,name_fr,set_name_fr FROM decks WHERE name_fr_search IS NULL OR set_name_fr_search IS NULL"):
        conn.execute("UPDATE decks SET name_fr_search=?,set_name_fr_search=? WHERE id=?",
                     (_normalize_search(row[1]), _normalize_search(row[2]), row[0]))


def _normalize_search(value: Any) -> str | None:
    if value is None:
        return None
    decomposed = unicodedata.normalize("NFKD", str(value).casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _stable_id(deck: dict[str, Any], source: str) -> str:
    file_name = deck.get("fileName") or deck.get("file_name")
    if file_name:
        return "mtgjson:" + str(file_name)
    set_code = str(deck.get("code") or deck.get("set_code") or "unknown").upper()
    name = str(deck.get("name") or "Deck")
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "deck"
    unique = f"{source}\0{set_code}\0{slug}"
    suffix = hashlib.sha256(unique.encode("utf-8")).hexdigest()[:10]
    return f"{source}:{set_code}:{slug}:{suffix}"


def _deck_meta(deck: dict[str, Any], source: str) -> dict[str, Any]:
    set_code = deck.get("code") or deck.get("set_code") or deck.get("setCode")
    set_name = deck.get("set_name") or deck.get("setName")
    translations = deck.get("translations") or {}
    set_translations = deck.get("setTranslations") or deck.get("set_translations") or {}
    name_fr = (deck.get("name_fr") or deck.get("nameFr") or
               translations.get("French") or translations.get("french") or translations.get("fr"))
    if not name_fr:
        name_fr = _WHO_DECK_NAMES_FR.get(str(deck.get("fileName") or deck.get("file_name") or ""))
    set_name_fr = (deck.get("set_name_fr") or deck.get("setNameFr") or
                   set_translations.get("French") or set_translations.get("french") or set_translations.get("fr"))
    if not set_name_fr:
        set_name_fr = _SET_NAMES_FR.get(str(set_code or "").upper())
    if not set_name_fr and str(set_code or "").upper() == "WHO":
        set_name_fr = "Doctor Who"
    return {
        "id": _stable_id(deck, source),
        "name": str(deck.get("name") or "Deck"),
        "name_fr": str(name_fr) if name_fr else None,
        "name_fr_search": _normalize_search(name_fr),
        "type": deck.get("type"),
        "set_code": str(set_code).upper() if set_code else None,
        "set_name": set_name,
        "set_name_fr": str(set_name_fr) if set_name_fr else None,
        "set_name_fr_search": _normalize_search(set_name_fr),
        "release_date": deck.get("releaseDate") or deck.get("release_date"),
        "file_name": deck.get("fileName") or deck.get("file_name"),
        "source": source,
        "raw_json": json.dumps(deck, ensure_ascii=False, separators=(",", ":")),
    }


def _section_values(deck: dict[str, Any]) -> dict[str, Any]:
    """Normalize MTGJSON Deck and community decks_v2 sections."""
    if "mainBoard" in deck or "sideBoard" in deck:
        return {
            "mainboard": deck.get("mainBoard") or [],
            "sideboard": deck.get("sideBoard") or [],
            "commander": deck.get("commander") or [],
            "display_commander": deck.get("displayCommander") or [],
            "planar_deck": deck.get("planarDeck") or [],
            "scheme_deck": deck.get("schemeDeck") or [],
            "tokens": deck.get("tokens") or [],
        }
    return {
        "mainboard": deck.get("cards") or deck.get("mainboard") or [],
        "sideboard": deck.get("sideboard") or [],
        "commander": deck.get("commander") or [],
        "display_commander": deck.get("displayCommander") or deck.get("display_commander") or [],
        "planar_deck": deck.get("planarDeck") or deck.get("planar_deck") or [],
        "scheme_deck": deck.get("schemeDeck") or deck.get("scheme_deck") or [],
        "tokens": deck.get("tokens") or [],
    }


def _card_row(deck_id: str, section: str, ordinal: int, card: Any) -> tuple[Any, ...]:
    if isinstance(card, str):
        card = {"name": card, "count": 1}
    if not isinstance(card, dict):
        raise ValueError(f"Carte invalide dans {section}, ligne {ordinal + 1}")
    name = card.get("name") or card.get("faceName")
    if not name:
        raise ValueError(f"Nom de carte absent dans {section}, ligne {ordinal + 1}")
    count = card.get("count", card.get("quantity", 1))
    try:
        count = int(count)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Quantité invalide pour {name}") from exc
    if count < 1:
        raise ValueError(f"Quantité non positive pour {name}")
    return (
        deck_id, section, ordinal, str(name), count,
        card.get("setCode") or card.get("set_code") or card.get("set"),
        str(card.get("number") or card.get("collector_number") or "") or None,
        card.get("uuid") or card.get("mtgjson_uuid") or card.get("mtgjsonUuid"),
        int(bool(card.get("foil", False))),
        json.dumps(card, ensure_ascii=False, separators=(",", ":")),
    )


def _insert_cards(conn: sqlite3.Connection, deck_id: str, deck: dict[str, Any], *, only_sections: set[str] | None = None) -> None:
    rows = []
    for section, cards in _section_values(deck).items():
        if only_sections is not None and section not in only_sections:
            continue
        if not isinstance(cards, list):
            raise ValueError(f"La section {section} doit être une liste")
        rows.extend(_card_row(deck_id, section, i, card) for i, card in enumerate(cards))
    if only_sections is None:
        conn.execute("DELETE FROM deck_cards WHERE deck_id=?", (deck_id,))
    else:
        for section in only_sections:
            conn.execute("DELETE FROM deck_cards WHERE deck_id=? AND section=?", (deck_id, section))
    conn.executemany("""INSERT INTO deck_cards
        (deck_id,section,ordinal,name,count,set_code,collector_number,mtgjson_uuid,foil,raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
    if only_sections is not None:
        if "tokens" in only_sections:
            conn.execute("UPDATE decks SET tokens_loaded=1 WHERE id=?", (deck_id,))
        return
    color_identity = set()
    for row in rows:
        if row[1] in {"tokens", "planar_deck", "scheme_deck"}:
            continue
        card = json.loads(row[-1])
        values = card.get("colorIdentity") or card.get("color_identity") or []
        color_identity.update(str(value).upper() for value in values if str(value).upper() in "WUBRG")
    color_key = "".join(color for color in "WUBRG" if color in color_identity)
    # Empty MTGJSON token arrays are common placeholders. Keep them eligible for
    # the on-demand all_parts derivation instead of treating them as complete.
    conn.execute("UPDATE decks SET loaded=1,colors=?,tokens_loaded=? WHERE id=?",
                 (color_key, int(bool(deck.get("tokens"))), deck_id))


def import_decks(source_json: str | Path | dict[str, Any] | list[dict[str, Any]], db_path: str | Path = DEFAULT_DB) -> dict[str, int]:
    """Atomically replace the metadata index with an MTGJSON or community JSON snapshot."""
    if isinstance(source_json, (str, Path)):
        doc = json.loads(Path(source_json).read_text(encoding="utf-8"))
    else:
        doc = source_json
    meta = doc.get("meta", {}) if isinstance(doc, dict) else {}
    data = doc.get("data", []) if isinstance(doc, dict) else doc
    if isinstance(data, dict):
        decks = list(data.values())
    elif isinstance(data, list):
        decks = data
    else:
        raise ValueError("Format JSON de decks non reconnu")
    source = "mtgjson" if isinstance(doc, dict) and "meta" in doc else "community"
    prepared = []
    for item in decks:
        if not isinstance(item, dict) or not item.get("name"):
            raise ValueError("Chaque deck doit être un objet avec un nom")
        prepared.append((_deck_meta(item, source), item))
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(target)
    try:
        _schema(conn)
        with conn:
            incoming_ids = [m["id"] for m, _ in prepared]
            # Refresh only the matching source namespace; preserve any independently
            # imported community lists in the same local database.
            if incoming_ids:
                placeholders = ",".join("?" for _ in incoming_ids)
                conn.execute(f"DELETE FROM decks WHERE source=? AND id NOT IN ({placeholders})", [source, *incoming_ids])
            else:
                conn.execute("DELETE FROM decks WHERE source=?", (source,))
            for m, deck in prepared:
                conn.execute("""INSERT INTO decks
                    (id,name,name_fr,name_fr_search,type,set_code,set_name,set_name_fr,set_name_fr_search,release_date,file_name,source,raw_json,added_at,loaded)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,name_fr=excluded.name_fr,
                      name_fr_search=excluded.name_fr_search,type=excluded.type,
                      set_code=excluded.set_code,set_name=excluded.set_name,
                      set_name_fr=excluded.set_name_fr,set_name_fr_search=excluded.set_name_fr_search,
                      release_date=excluded.release_date,file_name=excluded.file_name,
                      source=excluded.source,raw_json=excluded.raw_json""",
                    (m["id"],m["name"],m["name_fr"],m["name_fr_search"],m["type"],m["set_code"],m["set_name"],m["set_name_fr"],m["set_name_fr_search"],m["release_date"],m["file_name"],m["source"],m["raw_json"],time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
                # Community exports contain composition inline; MTGJSON DeckList is metadata-only.
                if any(key in deck for key in ("cards", "mainBoard", "mainboard", "tokens")):
                    _insert_cards(conn, m["id"], deck)
            conn.execute("INSERT OR REPLACE INTO deck_meta(key,value) VALUES('source_date',?)", (str(meta.get("date") or ""),))
            conn.execute("INSERT OR REPLACE INTO deck_meta(key,value) VALUES('source_version',?)", (str(meta.get("version") or ""),))
    finally:
        conn.close()
    return {"decks": len(prepared), "loaded": sum(1 for _, d in prepared if any(k in d for k in ("cards", "mainBoard", "mainboard", "tokens")))}


def ensure_deck_index(db_path: str | Path = DEFAULT_DB, *, refresh: bool = False, timeout: int = 30, progress=None) -> dict[str, int]:
    """Create the local index if absent; refresh=True explicitly downloads MTGJSON DeckList."""
    target = Path(db_path)
    if target.exists() and not refresh:
        with _connect(target) as conn:
            try:
                _schema(conn)
                count = conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0]
            except sqlite3.DatabaseError:
                count = 0
        if count:
            _apply_doctor_who_planechase(target)
            with _connect(target) as conn:
                migrated = conn.execute("SELECT 1 FROM deck_meta WHERE key='playable_colors_v1'").fetchone()
            if not migrated:
                _reindex_loaded_colors(target)
                with _connect(target) as conn:
                    conn.execute("INSERT OR REPLACE INTO deck_meta(key,value) VALUES('playable_colors_v1','1')")
                    conn.commit()
            if progress:
                progress(1.0, "Index des decks déjà disponible")
            return {"decks": count, "loaded": 0, "refreshed": 0}
    if progress:
        progress(0.0, "Téléchargement de l'index MTGJSON…")
    request = urllib.request.Request(DECK_LIST_URL, headers={"User-Agent": "LocalMagicLibrary/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"MTGJSON DeckList HTTP {response.status}")
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        chunks = []
        received = 0
        while True:
            block = response.read(256 * 1024)
            if not block:
                break
            chunks.append(block); received += len(block)
            if progress and total:
                progress(min(received / total, 0.85), f"{received / 1024:.0f} / {total / 1024:.0f} Ko")
        if progress and not total:
            progress(0.85, "Index téléchargé")
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    if progress:
        progress(0.90, "Import des decks…")
    result = import_decks(payload, target)
    if progress:
        progress(0.96, "Application des compléments…")
    _apply_doctor_who_planechase(target)
    _reindex_loaded_colors(target)
    with _connect(target) as conn:
        conn.execute("INSERT OR REPLACE INTO deck_meta(key,value) VALUES('playable_colors_v1','1')")
        conn.commit()
    result["refreshed"] = 1
    if progress:
        progress(1.0, f"{result['decks']} decks disponibles")
    return result


def refresh_deck_index(db_path: str | Path = DEFAULT_DB, *, timeout: int = 30, progress=None) -> dict[str, int]:
    return ensure_deck_index(db_path, refresh=True, timeout=timeout, progress=progress)


def _read_json_response(response: Any) -> tuple[Any, int]:
    payload = response.read()
    received = len(payload)
    if str(response.headers.get("Content-Encoding") or "").casefold() == "gzip":
        payload = gzip.decompress(payload)
    return json.loads(payload.decode("utf-8")), received


class _DownloadError(OSError):
    def __init__(self, message: str, bytes_received: int = 0):
        super().__init__(message)
        self.bytes_received = bytes_received


def _download_json(request: urllib.request.Request, timeout: int, label: str) -> tuple[Any, int]:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    raise _DownloadError(f"{label} HTTP {response.status}")
                return _read_json_response(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise _DownloadError(f"{label} HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 2:
                raise _DownloadError(f"{label}: {exc}") from exc
        time.sleep(0.4 * (attempt + 1))
    raise _DownloadError(f"{label}: téléchargement impossible")


def _catalog_color_map(catalog_db: str | Path, oracle_ids: set[str],
                       progress_callback: Any = None, *, bytes_received: int = 0,
                       unmatched_count: int = 0) -> dict[str, set[str]]:
    uri = Path(catalog_db).resolve().as_uri() + "?mode=ro"
    by_oracle: dict[str, set[str]] = {}
    ordered_ids = sorted(oracle_ids)
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            variable_limit = (conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
                              if hasattr(conn, "getlimit") else 999)
            chunk_size = max(1, min(5000, variable_limit - 16))
            chunks = [ordered_ids[i:i + chunk_size]
                      for i in range(0, len(ordered_ids), chunk_size)]
            for index, chunk in enumerate(chunks, start=1):
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(f"""SELECT p.oracle_id,sr.color_identity_json
                    FROM printings p JOIN source_records sr USING(printing_id)
                    WHERE p.oracle_id IN ({placeholders})""", chunk).fetchall()
                for oracle_id, encoded in rows:
                    try:
                        values = json.loads(encoded or "[]")
                    except (TypeError, json.JSONDecodeError):
                        values = []
                    colors = {str(value).upper() for value in values if str(value).upper() in "WUBRG"}
                    by_oracle.setdefault(str(oracle_id), set()).update(colors)
                if progress_callback:
                    progress_callback({"phase": "catalog", "completed": index,
                                       "total": len(chunks), "indexed": 0,
                                       "unmatched": unmatched_count, "failed": 0,
                                       "bytes_received": bytes_received})
    except sqlite3.DatabaseError as exc:
        raise ValueError("Le catalogue actif doit exposer source_records, printings et color_identity_json") from exc
    return by_oracle


def _color_key(values: set[str]) -> str:
    return "".join(color for color in "WUBRG" if color in values)


def _precon_snapshot(timeout: int, page_size: int, progress_callback: Any) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    bytes_received = 0
    total_rows: int | None = None
    offset = 0
    while total_rows is None or offset < total_rows:
        query = urllib.parse.urlencode({
            "dataset": PRECON_DATASET,
            "config": PRECON_CONFIG,
            "split": PRECON_SPLIT,
            "offset": offset,
            "length": page_size,
        })
        request = urllib.request.Request(
            PRECON_ROWS_URL + "?" + query,
            headers={"User-Agent": "LocalMagicLibrary/1.0", "Accept-Encoding": "gzip"},
        )
        try:
            doc, received = _download_json(request, timeout, "Index précon")
        except _DownloadError as exc:
            exc.bytes_received = bytes_received
            raise
        bytes_received += received
        if total_rows is None:
            total_rows = int(doc.get("num_rows_total") or 0)
        page = doc.get("rows")
        if not isinstance(page, list):
            raise ValueError("Format de page précon invalide")
        rows.extend(item.get("row", {}) for item in page if isinstance(item, dict))
        offset += len(page)
        if progress_callback:
            progress_callback({"phase": "fetch", "completed": min(offset, total_rows),
                               "total": total_rows, "indexed": 0, "unmatched": 0,
                               "failed": 0, "bytes_received": bytes_received})
        if not page and offset < total_rows:
            raise ValueError("L'index précon a retourné une page vide avant la fin")
    return rows, bytes_received


def _fetch_deck_color(file_name: str, oracle_colors: dict[str, set[str]], timeout: int) -> tuple[str | None, int]:
    request = urllib.request.Request(
        DECK_FILE_BASE_URL + urllib.parse.quote(file_name, safe="") + ".json",
        headers={"User-Agent": "LocalMagicLibrary/1.0", "Accept-Encoding": "gzip"},
    )
    doc, received = _download_json(request, timeout, f"MTGJSON deck {file_name}")
    deck = doc.get("data", doc)
    if not isinstance(deck, dict):
        raise ValueError(f"Composition MTGJSON invalide pour {file_name}")
    identities: set[str] = set()
    unresolved = False
    for section, cards in _section_values(deck).items():
        if section in {"tokens", "planar_deck", "scheme_deck"}:
            continue
        if not isinstance(cards, list):
            raise ValueError(f"Section MTGJSON invalide pour {file_name}: {section}")
        for card in cards:
            if not isinstance(card, dict):
                continue
            direct_present = "colorIdentity" in card or "color_identity" in card
            direct = card.get("colorIdentity") if "colorIdentity" in card else card.get("color_identity")
            if direct_present:
                direct = direct or []
                identities.update(str(value).upper() for value in direct if str(value).upper() in "WUBRG")
                continue
            identifiers = card.get("identifiers") or {}
            oracle_id = identifiers.get("scryfallOracleId") or identifiers.get("oracleId") or card.get("oracle_id")
            colors = oracle_colors.get(str(oracle_id)) if oracle_id else None
            if colors is None:
                unresolved = True
            else:
                identities.update(colors)
    return (None if unresolved else _color_key(identities)), received


def index_deck_colors(db_path: str | Path = DEFAULT_DB, catalog_db: str | Path | None = None,
                      *, progress_callback: Any = None, timeout: int = 30,
                      page_size: int = 100) -> dict[str, Any]:
    """Index precon color identities from a compact MTGJSON-derived snapshot.

    The remote snapshot only supplies file names and card oracle IDs. Color
    identities are read from the active local catalogue. Current MTGJSON deck
    files are fetched individually only when the snapshot is stale or a card is
    missing from that catalogue. No database update occurs until all requests
    succeed; unresolved card identities remain unindexed.
    """
    if not catalog_db:
        raise ValueError("catalog_db actif requis pour indexer les couleurs")
    if not Path(db_path).exists():
        raise FileNotFoundError(db_path)
    page_size = max(1, min(100, int(page_size)))
    with _connect(db_path) as conn:
        _schema(conn)
        eligible = "source='mtgjson' AND type IN (" + ",".join("?" for _ in SUPPORTED_DECK_TYPES) + ")"
        targets = conn.execute(f"SELECT id,file_name FROM decks WHERE {eligible} AND colors IS NULL", SUPPORTED_DECK_TYPES).fetchall()
    target_by_file = {str(row["file_name"]): row["id"] for row in targets if row["file_name"]}
    prepared: dict[str, str] = {}
    bytes_received = 0
    try:
        source_rows, bytes_received = _precon_snapshot(timeout, page_size, progress_callback)
        rows_by_file = {str(row.get("file")): row for row in source_rows if row.get("file")}
        target_rows = [rows_by_file[file_name] for file_name in target_by_file if file_name in rows_by_file]
        oracle_ids = {str(card["oracle_id"]) for row in target_rows
                      for card in (row.get("cards") or [])
                      if isinstance(card, dict) and card.get("oracle_id")}
        oracle_colors = _catalog_color_map(catalog_db, oracle_ids, progress_callback,
                                           bytes_received=bytes_received,
                                           unmatched_count=len(target_by_file))
        fallback: list[tuple[str, str]] = []
        unresolved_cards = 0
        for file_name, deck_id in target_by_file.items():
            source_row = rows_by_file.get(file_name)
            if source_row is None:
                fallback.append((file_name, deck_id))
                continue
            identities: set[str] = set()
            unresolved = False
            cards = source_row.get("cards") or []
            if not isinstance(cards, list):
                unresolved = True
            else:
                for card in cards:
                    if not isinstance(card, dict):
                        unresolved = True
                        continue
                    oracle_id = card.get("oracle_id")
                    colors = oracle_colors.get(str(oracle_id)) if oracle_id else None
                    if colors is None:
                        unresolved = True
                        unresolved_cards += 1
                    else:
                        identities.update(colors)
            if unresolved:
                fallback.append((file_name, deck_id))
            else:
                prepared[deck_id] = _color_key(identities)

        failed = 0
        for index, (file_name, deck_id) in enumerate(fallback, start=1):
            try:
                color_key, received = _fetch_deck_color(file_name, oracle_colors, timeout)
                bytes_received += received
                if color_key is None:
                    unresolved_cards += 1
                else:
                    prepared[deck_id] = color_key
            except Exception:
                failed += 1
                raise
            if progress_callback:
                progress_callback({"phase": "fallback", "completed": index,
                                   "total": len(fallback), "indexed": len(prepared),
                                   "unmatched": len(target_by_file) - len(prepared),
                                   "failed": failed, "bytes_received": bytes_received})
        with _connect(db_path) as conn:
            _schema(conn)
            with conn:
                conn.executemany("UPDATE decks SET colors=? WHERE id=? AND colors IS NULL",
                                 [(_color_key(set(value)), deck_id) for deck_id, value in prepared.items()])
    except Exception as exc:
        bytes_received = max(bytes_received, int(getattr(exc, "bytes_received", 0)))
        if progress_callback:
            progress_callback({"phase": "failed", "completed": 0, "total": len(target_by_file),
                               "indexed": 0, "unmatched": len(target_by_file),
                               "failed": 1, "bytes_received": bytes_received,
                               "error": str(exc)})
        raise
    unmatched = len(target_by_file) - len(prepared)
    result = {"decks": len(target_by_file), "indexed": len(prepared), "unmatched": unmatched,
              "unresolved_cards": unresolved_cards, "failed": 0,
              "bytes_received": bytes_received, "snapshot_rows": len(source_rows)}
    if progress_callback:
        progress_callback({"phase": "complete", "completed": len(target_by_file),
                           "total": len(target_by_file), "indexed": len(prepared),
                           "unmatched": unmatched, "failed": 0,
                           "bytes_received": bytes_received})
    return result


def _format_for_type(deck_type: str | None) -> str:
    normalized = str(deck_type or "").casefold()
    if "commander" in normalized:
        return "Commander"
    if "pioneer" in normalized:
        return "Pioneer"
    if "modern" in normalized:
        return "Modern"
    if "challenger deck" in normalized:
        return "Standard"
    return "Autre"


def _format_sql() -> str:
    return """CASE
        WHEN lower(type) LIKE '%commander%' THEN 'Commander'
        WHEN lower(type) LIKE '%pioneer%' THEN 'Pioneer'
        WHEN lower(type) LIKE '%modern%' THEN 'Modern'
        WHEN lower(type) LIKE '%challenger deck%' THEN 'Standard'
        ELSE 'Autre' END"""


def _color_filter(colors: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if colors is None:
        return []
    values = list(colors) if not isinstance(colors, str) else list(colors.upper())
    normalized = {str(value).strip().upper() for value in values if str(value).strip()}
    if any(len(value) != 1 or value not in "WUBRGC" for value in normalized):
        raise ValueError("Couleurs attendues : W, U, B, R, G ou C")
    return [color for color in "WUBRGC" if color in normalized]


def deck_facets(db_path: str | Path = DEFAULT_DB) -> dict[str, Any]:
    if not Path(db_path).exists():
        return {"types": [], "formats": [], "editions": [], "color_indexed": 0, "total": 0}
    with _connect(db_path) as conn:
        _schema(conn)
        eligible = "source='community' OR type IN (" + ",".join("?" for _ in SUPPORTED_DECK_TYPES) + ")"
        types = [row[0] for row in conn.execute(f"SELECT DISTINCT type FROM decks WHERE type IS NOT NULL AND ({eligible}) ORDER BY type COLLATE NOCASE", SUPPORTED_DECK_TYPES)]
        formats = [row[0] for row in conn.execute(f"SELECT DISTINCT {_format_sql()} AS deck_format FROM decks WHERE {eligible} ORDER BY deck_format", SUPPORTED_DECK_TYPES)]
        total, color_indexed = conn.execute(f"SELECT COUNT(*),COUNT(colors) FROM decks WHERE {eligible}", SUPPORTED_DECK_TYPES).fetchone()
        editions = [dict(row) for row in conn.execute(f"""SELECT set_code AS code,
            MAX(set_name_fr) AS name_fr,MAX(set_name) AS name_en
            FROM decks WHERE {eligible} AND set_code IS NOT NULL
            GROUP BY set_code ORDER BY set_code COLLATE NOCASE""", SUPPORTED_DECK_TYPES)]
    return {"types": types, "formats": formats, "editions": editions,
            "color_indexed": color_indexed, "total": total}


def list_decks(db_path: str | Path = DEFAULT_DB, *, query: str | None = None, name_only: bool = False, deck_type: str | None = None, deck_format: str | None = None, colors: str | list[str] | tuple[str, ...] | None = None, color_mode: str = "contains", set_code: str | None = None, sort: str = "date", order: str = "desc", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    color_mode = str(color_mode).casefold()
    if color_mode not in {"contains", "exact"}:
        raise ValueError("Mode couleur attendu : contains ou exact")
    selected_colors = _color_filter(colors)
    if not Path(db_path).exists():
        return {"contract_version": CONTRACT_VERSION, "total": 0, "limit": 0, "offset": 0, "items": []}
    limit = max(1, min(MAX_PAGE_SIZE, int(limit)))
    offset = max(0, int(offset))
    eligible = "(source='community' OR type IN (" + ",".join("?" for _ in SUPPORTED_DECK_TYPES) + "))"
    clauses, args = [eligible], list(SUPPORTED_DECK_TYPES)
    if query and str(query).strip():
        q = "%" + str(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        q_normalized = "%" + _normalize_search(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        if name_only:
            clauses.append("(name LIKE ? ESCAPE '\\' OR name_fr_search LIKE ? ESCAPE '\\')")
            args.extend([q, q_normalized])
        else:
            clauses.append("(name LIKE ? ESCAPE '\\' OR name_fr_search LIKE ? ESCAPE '\\' OR set_code LIKE ? ESCAPE '\\' OR set_name LIKE ? ESCAPE '\\' OR set_name_fr_search LIKE ? ESCAPE '\\' OR type LIKE ? ESCAPE '\\')")
            args.extend([q, q_normalized, q, q, q_normalized, q])
    if deck_type:
        clauses.append("type=?")
        args.append(str(deck_type))
    if set_code and str(set_code).strip():
        clauses.append("upper(set_code)=upper(?)")
        args.append(str(set_code).strip())
    if deck_format:
        if deck_format not in {"Commander", "Standard", "Pioneer", "Modern", "Autre"}:
            raise ValueError(f"Format invalide : {deck_format}")
        clauses.append(f"({_format_sql()})=?")
        args.append(deck_format)
    if selected_colors:
        clauses.append("colors IS NOT NULL")
        canonical = [color for color in selected_colors if color != "C"]
        color_clauses = []
        if color_mode == "exact" and canonical:
            color_clauses.append("length(colors)=?")
            args.append(len(canonical))
            color_clauses.append("colors NOT GLOB '*[^WUBRG]*'")
            for color in canonical:
                color_clauses.append("instr(colors,?)>0")
                args.append(color)
        else:
            for color in canonical:
                color_clauses.append("instr(colors,?)>0")
                args.append(color)
        if "C" in selected_colors and canonical:
            clauses.append("(colors='' OR (" + " AND ".join(color_clauses) + "))")
        elif "C" in selected_colors:
            clauses.append("colors=''")
        else:
            clauses.extend(color_clauses)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sort_column = {"name": "name COLLATE NOCASE", "date": "release_date", "added": "added_at"}.get(str(sort).casefold())
    if sort_column is None:
        raise ValueError("Tri attendu : name, date ou added")
    order_value = str(order).casefold()
    if order_value not in {"asc", "desc"}:
        raise ValueError("Ordre attendu : asc ou desc")
    nulls_last = "(release_date IS NULL)," if str(sort).casefold() == "date" else ""
    with _connect(db_path) as conn:
        _schema(conn)
        total = conn.execute(f"SELECT COUNT(*) FROM decks {where}", args).fetchone()[0]
        rows = conn.execute(f"SELECT id,name,name_fr,type,{_format_sql()} AS deck_format,set_code,set_name,set_name_fr,release_date,added_at,colors,file_name,loaded FROM decks {where} ORDER BY {nulls_last}{sort_column} {order_value.upper()},name COLLATE NOCASE,id LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["colors"] = list(item["colors"]) if item["colors"] is not None else None
        items.append(item)
    return {"contract_version": CONTRACT_VERSION, "total": total, "limit": limit, "offset": offset, "items": items}


def _load_missing(db_path: str | Path, deck_row: sqlite3.Row, timeout: int,
                  catalog_db: str | Path | None = None) -> None:
    file_name = deck_row["file_name"]
    if not file_name:
        return
    request = urllib.request.Request(DECK_FILE_BASE_URL + urllib.parse.quote(file_name, safe="") + ".json", headers={"User-Agent": "LocalMagicLibrary/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"MTGJSON deck HTTP {response.status}")
        doc = json.loads(response.read().decode("utf-8"))
    payload = doc.get("data", doc)
    if not isinstance(payload, dict):
        raise ValueError("Format de composition MTGJSON invalide")
    with _connect(db_path) as conn:
        _schema(conn)
        with conn:
            _insert_cards(conn, deck_row["id"], payload)
            _apply_doctor_who_planechase_conn(conn, deck_row["id"], deck_row["file_name"])
            if not (payload.get("tokens") or []) and catalog_db and Path(catalog_db).is_file():
                cards = conn.execute("""SELECT raw_json FROM deck_cards
                    WHERE deck_id=? AND section!='tokens' ORDER BY section,ordinal""", (deck_row["id"],)).fetchall()
                tokens = _derive_tokens(deck_row["id"], deck_row["set_code"], cards, catalog_db)
                if tokens:
                    _insert_cards(conn, deck_row["id"], {"tokens": tokens}, only_sections={"tokens"})


def _derive_tokens(deck_id: str, deck_set_code: str | None,
                   deck_cards: list[sqlite3.Row], catalog_db: str | Path) -> list[dict[str, Any]]:
    """Derive token cards from Scryfall all_parts and prefer the deck's token set."""
    if not deck_set_code or not Path(catalog_db).is_file():
        return []
    token_set = "t" + str(deck_set_code).casefold()
    uri = Path(catalog_db).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        token_links: dict[str, str] = {}
        for card_row in deck_cards:
            raw_card = json.loads(card_row["raw_json"])
            identifiers = raw_card.get("identifiers") or {}
            card_id = raw_card.get("scryfall_id") or identifiers.get("scryfallId")
            if not card_id:
                continue
            source = conn.execute("SELECT raw_json FROM source_records WHERE provider_card_id=?", (str(card_id),)).fetchone()
            if not source:
                continue
            for part in json.loads(source["raw_json"]).get("all_parts") or []:
                target_id = part.get("id")
                if part.get("component") == "token" and target_id:
                    token_links.setdefault(str(target_id), str(target_id))

        selected: dict[str, dict[str, Any]] = {}
        for linked_id in token_links:
            linked = conn.execute("""SELECT p.printing_id,p.oracle_id,e.code,e.eligibility
                FROM source_records sr JOIN printings p USING(printing_id)
                JOIN editions e USING(set_id) WHERE sr.provider_card_id=?""", (linked_id,)).fetchone()
            if not linked or linked["eligibility"] != "eligible":
                continue
            printing_id = linked["printing_id"]
            # The all_parts link proves the token identity, but its printing
            # may belong to an unrelated set. Require this deck's token set.
            if linked["oracle_id"] and str(linked["code"]).casefold() != token_set:
                preferred = conn.execute("""SELECT p.printing_id FROM printings p
                    JOIN editions e USING(set_id) WHERE p.oracle_id=? AND lower(e.code)=?
                      AND e.eligibility='eligible'
                    ORDER BY length(p.collector_number),p.collector_number,p.printing_id LIMIT 1""",
                    (linked["oracle_id"], token_set)).fetchone()
                if preferred:
                    printing_id = preferred["printing_id"]
                else:
                    continue
            elif str(linked["code"]).casefold() != token_set:
                continue
            token_rows = conn.execute("""SELECT sr.provider_card_id,sr.raw_json,sr.lang,sr.name
                FROM source_records sr JOIN printings p USING(printing_id)
                JOIN editions e ON e.set_id=p.set_id
                WHERE p.printing_id=? AND e.eligibility='eligible'
                ORDER BY CASE sr.lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 ELSE 2 END,
                         sr.provider_card_id""", (printing_id,)).fetchall()
            if not token_rows:
                continue
            raw = json.loads(token_rows[0]["raw_json"])
            identity = str(linked["oracle_id"] or printing_id)
            selected.setdefault(identity, {
                "name": token_rows[0]["name"] or raw.get("name") or raw.get("printed_name"),
                "count": 1,
                "setCode": str(raw.get("set") or "").upper() or None,
                "number": str(raw.get("collector_number") or "") or None,
                "identifiers": {"scryfallId": token_rows[0]["provider_card_id"]},
                "all_parts_source": linked_id,
            })
        return list(selected.values())


def _load_tokens(db_path: str | Path, deck_row: sqlite3.Row, timeout: int,
                 catalog_db: str | Path | None = None) -> None:
    """Load explicit tokens or derive them from catalog all_parts links."""
    file_name = deck_row["file_name"]
    if not file_name:
        return
    request = urllib.request.Request(DECK_FILE_BASE_URL + urllib.parse.quote(file_name, safe="") + ".json", headers={"User-Agent": "LocalMagicLibrary/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"MTGJSON deck HTTP {response.status}")
        doc = json.loads(response.read().decode("utf-8"))
    payload = doc.get("data", doc)
    if not isinstance(payload, dict):
        raise ValueError("Format de composition MTGJSON invalide")
    tokens = payload.get("tokens") or []
    if not tokens and catalog_db and Path(catalog_db).is_file():
        with _connect(db_path) as conn:
            _schema(conn)
            deck_cards = conn.execute("""SELECT raw_json FROM deck_cards
                WHERE deck_id=? AND section!='tokens' ORDER BY section,ordinal""", (deck_row["id"],)).fetchall()
        tokens = _derive_tokens(deck_row["id"], deck_row["set_code"], deck_cards, catalog_db)
    with _connect(db_path) as conn:
        _schema(conn)
        with conn:
            _insert_cards(conn, deck_row["id"], {"tokens": tokens}, only_sections={"tokens"})


def _apply_doctor_who_planechase_conn(conn: sqlite3.Connection, deck_id: str, file_name: str | None) -> None:
    data_file = Path(__file__).resolve().parent / "data" / "doctor_who_planechase.json"
    supplement = json.loads(data_file.read_text(encoding="utf-8"))
    cards = supplement.get("decks", {}).get(file_name, [])
    if not cards:
        return
    present = conn.execute("SELECT COUNT(*) FROM deck_cards WHERE deck_id=? AND section='planar_deck'", (deck_id,)).fetchone()[0]
    if present:
        return
    rows = [_card_row(deck_id, "planar_deck", i, {**card, "set_code": supplement.get("set_code", "WHO")}) for i, card in enumerate(cards)]
    conn.executemany("""INSERT INTO deck_cards
        (deck_id,section,ordinal,name,count,set_code,collector_number,mtgjson_uuid,foil,raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)


def _apply_doctor_who_planechase(db_path: str | Path) -> None:
    with _connect(db_path) as conn:
        _schema(conn)
        rows = conn.execute("SELECT id,file_name,loaded FROM decks WHERE set_code='WHO'").fetchall()
        if rows:
            with conn:
                for row in rows:
                    if row["loaded"]:
                        _apply_doctor_who_planechase_conn(conn, row["id"], row["file_name"])


def _reindex_loaded_colors(db_path: str | Path) -> None:
    with _connect(db_path) as conn:
        _schema(conn)
        with conn:
            for row in conn.execute("SELECT id FROM decks WHERE loaded=1"):
                identities = set()
                for card_row in conn.execute("SELECT raw_json FROM deck_cards WHERE deck_id=? AND section NOT IN ('tokens','planar_deck','scheme_deck')", (row["id"],)):
                    card = json.loads(card_row[0])
                    values = card.get("colorIdentity") or card.get("color_identity") or []
                    identities.update(str(value).upper() for value in values if str(value).upper() in "WUBRG")
                color_key = "".join(color for color in "WUBRG" if color in identities)
                conn.execute("UPDATE decks SET colors=? WHERE id=?", (color_key, row["id"]))


def _resolve_provider_ids(rows: list[dict[str, Any]], catalog_db: str | Path | None) -> None:
    for row in rows:
        row["provider_card_id"] = None
        row["printed_name"] = None
    if not catalog_db or not Path(catalog_db).exists() or not rows:
        return
    with sqlite3.connect(f"file:{Path(catalog_db).resolve().as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for row in rows:
            matches = []
            card_raw = row.get("raw") or {}
            scryfall_id = card_raw.get("scryfall_id") or card_raw.get("identifiers", {}).get("scryfallId")
            if scryfall_id:
                matches = conn.execute("""SELECT p.printing_id,sr.provider_card_id,sr.lang
                    FROM source_records sr JOIN printings p USING(printing_id)
                    JOIN editions e USING(set_id)
                    WHERE sr.provider_card_id=? AND e.eligibility='eligible'
                      AND (? IS NULL OR upper(e.code)=upper(?))
                    ORDER BY CASE sr.lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 ELSE 2 END,sr.provider_card_id""",
                    (str(scryfall_id), row.get("set_code"), row.get("set_code"))).fetchall()
            # A set and collector number uniquely identify the physical printing only when single.
            if not matches and row.get("set_code") and row.get("collector_number"):
                matches = conn.execute("""SELECT p.printing_id,sr.provider_card_id,sr.lang FROM source_records sr
                    JOIN printings p USING(printing_id) JOIN editions e USING(set_id)
                    WHERE upper(e.code)=upper(?) AND p.collector_number=?
                      AND e.eligibility='eligible'
                    ORDER BY CASE sr.lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 ELSE 2 END,sr.provider_card_id""",
                    (row["set_code"], row["collector_number"])).fetchall()
            printing_ids = {match[0] for match in matches}
            if len(printing_ids) == 1 and matches:
                row["provider_card_id"] = matches[0][1]
                french = conn.execute("""SELECT printed_name FROM source_records
                    WHERE printing_id=? AND lang='fr' AND printed_name IS NOT NULL
                    ORDER BY provider_card_id LIMIT 1""", (matches[0][0],)).fetchone()
                if french:
                    row["printed_name"] = french[0]


def get_deck(db_path: str | Path = DEFAULT_DB, deck_id: str = "", *, catalog_db: str | Path | None = None, fetch_missing: bool = True, include_tokens: bool = False, timeout: int = 30) -> dict[str, Any]:
    if not Path(db_path).exists():
        return {"contract_version": CONTRACT_VERSION, "found": False, "deck": None}
    with _connect(db_path) as conn:
        _schema(conn)
        deck = conn.execute("SELECT * FROM decks WHERE id=?", (str(deck_id),)).fetchone()
    if not deck:
        return {"contract_version": CONTRACT_VERSION, "found": False, "deck": None}
    just_loaded = False
    if not deck["loaded"] and fetch_missing and deck["file_name"]:
        _load_missing(db_path, deck, timeout, catalog_db)
        just_loaded = True
        with _connect(db_path) as conn:
            deck = conn.execute("SELECT * FROM decks WHERE id=?", (str(deck_id),)).fetchone()
    if include_tokens and fetch_missing and not deck["tokens_loaded"] and deck["file_name"]:
        # Existing cached compositions can be derived locally first. This is
        # especially important for pre-token-cache decks; do not refetch their
        # already cached source when all_parts resolves successfully.
        derived = []
        if not just_loaded and catalog_db and Path(catalog_db).is_file():
            with _connect(db_path) as conn:
                _schema(conn)
                cards = conn.execute("""SELECT raw_json FROM deck_cards
                    WHERE deck_id=? AND section!='tokens' ORDER BY section,ordinal""", (str(deck_id),)).fetchall()
            derived = _derive_tokens(str(deck_id), deck["set_code"], cards, catalog_db)
        if derived:
            with _connect(db_path) as conn:
                _schema(conn)
                with conn:
                    _insert_cards(conn, str(deck_id), {"tokens": derived}, only_sections={"tokens"})
        elif not just_loaded:
            _load_tokens(db_path, deck, timeout, catalog_db)
        with _connect(db_path) as conn:
            deck = conn.execute("SELECT * FROM decks WHERE id=?", (str(deck_id),)).fetchone()
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT section,ordinal,name,count,set_code,collector_number,mtgjson_uuid,foil,raw_json FROM deck_cards WHERE deck_id=? ORDER BY section,ordinal", (str(deck_id),)).fetchall()
    sections = {s: [] for s in SECTIONS}
    for r in rows:
        item = dict(r)
        item["raw"] = json.loads(item.pop("raw_json"))
        item["foil"] = bool(item["foil"])
        item.pop("ordinal", None)
        section = item.pop("section")
        sections[section].append(item)
    all_cards = [card for group in sections.values() for card in group]
    _resolve_provider_ids(all_cards, catalog_db)
    result = {k: deck[k] for k in ("id", "name", "name_fr", "type", "set_code", "set_name", "set_name_fr", "release_date", "loaded")}
    result["tokens_loaded"] = bool(deck["tokens_loaded"])
    result["deck_format"] = _format_for_type(result["type"])
    result["colors"] = list(deck["colors"]) if deck["colors"] is not None else None
    result["sections"] = sections
    return {"contract_version": CONTRACT_VERSION, "found": True, "deck": result}
