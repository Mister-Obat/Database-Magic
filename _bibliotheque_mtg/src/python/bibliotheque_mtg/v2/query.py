from __future__ import annotations
import sqlite3
import os
import hashlib
import tempfile
import threading
import unicodedata
from collections import OrderedDict
from contextlib import closing, contextmanager
from pathlib import Path

CONTRACT_VERSION = "2.0.0"
MAX_PAGE_SIZE = 100
_IMAGE_MTIME_CACHE = OrderedDict()
_IMAGE_MTIME_LOCK = threading.Lock()
_IMAGE_MTIME_CACHE_SIZE = 50000
_SEARCH_CANDIDATE_CACHE = OrderedDict()
_SEARCH_CANDIDATE_LOCK = threading.Lock()
_SEARCH_CANDIDATE_CACHE_SIZE = 2
_SEARCH_CANDIDATE_MAX_ROWS = 150000
_FRENCH_TOKEN_ALIASES = {
    "indice": "clue", "indices": "clue",
    "tresor": "treasure", "tresors": "treasure",
    "nourriture": "food", "nourritures": "food",
    "sang": "blood", "ferraille": "scrap",
}
_TYPE_ALIAS_CACHE = OrderedDict()
_TYPE_ALIAS_LOCK = threading.Lock()
_EXTRA_SEARCH_LOCK = threading.Lock()
_EXTRA_SEARCH_KEY = None
_EXTRA_SEARCH_DB = None


def _readable_record(row):
    record = dict(row)
    if record.get('lang') == 'ph':
        record['printed_name'] = None
        if 'printed_type_line' in record:
            record['printed_type_line'] = None
        if 'printed_text' in record:
            record['printed_text'] = None
    return record


def _extra_matches(db, match, name_match):
    """Index alternate names without modifying the published catalogue."""
    if Path(db).name == "proxies.sqlite":
        with closing(sqlite3.connect(":memory:")) as index:
            _build_extra_index(index, db)
            return tuple([row[0] for row in index.execute(
                "SELECT provider_card_id FROM extra WHERE extra MATCH ?", (pattern,))] for pattern in (match, name_match))
    global _EXTRA_SEARCH_KEY, _EXTRA_SEARCH_DB
    # Published snapshot paths are unique per version; image-slot writes do not
    # change source text and must not rebuild this index.
    key = str(Path(db).resolve())
    with _EXTRA_SEARCH_LOCK:
        if key != _EXTRA_SEARCH_KEY:
            library = next((parent for parent in Path(db).resolve().parents
                            if parent.name == "_bibliotheque_mtg" and (parent / "state").is_dir()), None)
            index_path = (library / "state" / ("search-names-v1-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20] + ".sqlite")) if library else None
            exists = bool(index_path and index_path.is_file())
            if exists:
                index = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
            else:
                index = sqlite3.connect(":memory:", check_same_thread=False)
            try:
                if not exists:
                    _build_extra_index(index, db)
                    if index_path:
                        fd, temp_path = tempfile.mkstemp(prefix="search-names-", suffix=".tmp", dir=index_path.parent)
                        os.close(fd)
                        try:
                            disk = sqlite3.connect(temp_path)
                            try:
                                index.backup(disk)
                            finally:
                                disk.close()
                            try:
                                os.replace(temp_path, index_path)
                            except PermissionError:
                                # Another process may already have published and opened it.
                                if not index_path.is_file():
                                    raise
                        finally:
                            Path(temp_path).unlink(missing_ok=True)
            except Exception:
                index.close()
                raise
            if _EXTRA_SEARCH_DB is not None:
                _EXTRA_SEARCH_DB.close()
            _EXTRA_SEARCH_DB, _EXTRA_SEARCH_KEY = index, key
        ids = [row[0] for row in _EXTRA_SEARCH_DB.execute("SELECT provider_card_id FROM extra WHERE extra MATCH ?", (match,))]
        name_ids = [row[0] for row in _EXTRA_SEARCH_DB.execute("SELECT provider_card_id FROM extra WHERE extra MATCH ?", (name_match,))]
        return ids, name_ids


def _build_extra_index(index, db):
    index.execute("CREATE VIRTUAL TABLE extra USING fts5(provider_card_id UNINDEXED, set_code, lang, name, printed_name, type_line, printed_type_line, oracle_text, printed_text, tokenize='unicode61 remove_diacritics 2')")
    with _connection(db) as source:
        rows = source.execute("""SELECT sr.provider_card_id, e.code, sr.lang, '',
            COALESCE(json_extract(sr.raw_json,'$.flavor_name'),'') || ' ' ||
            COALESCE(json_extract(sr.raw_json,'$.card_faces[0].flavor_name'),'') || ' ' ||
            COALESCE(json_extract(sr.raw_json,'$.card_faces[1].flavor_name'),''),
            sr.type_line, '', '', ''
            FROM source_records sr JOIN printings p USING(printing_id) JOIN editions e USING(set_id)
            WHERE json_valid(sr.raw_json) AND (json_type(sr.raw_json,'$.flavor_name') IS NOT NULL
              OR json_type(sr.raw_json,'$.card_faces[0].flavor_name') IS NOT NULL
              OR json_type(sr.raw_json,'$.card_faces[1].flavor_name') IS NOT NULL)""")
        index.executemany("INSERT INTO extra VALUES(?,?,?,?,?,?,?,?,?)", rows)
    index.commit()


def _fold(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))


def _french_type_aliases(db, term):
    path = Path(db).resolve()
    stat = path.stat()
    normalized = _fold(term)
    key = (str(path), stat.st_mtime_ns, stat.st_size, normalized)
    with _TYPE_ALIAS_LOCK:
        cached = _TYPE_ALIAS_CACHE.get(key)
        if cached is not None:
            _TYPE_ALIAS_CACHE.move_to_end(key)
            return cached
    aliases = set()
    with _connection(path) as conn:
        rows = conn.execute("""SELECT DISTINCT en.type_line, fr.printed_type_line
            FROM source_search JOIN source_records fr
              ON fr.provider_card_id=source_search.provider_card_id
            JOIN source_records en
              ON en.printing_id=fr.printing_id AND en.lang='en'
            WHERE source_search MATCH ? AND fr.lang='fr'
              AND fr.printed_type_line IS NOT NULL""",
              (f'printed_type_line : "{normalized.replace(chr(34), chr(34) * 2)}"*',))
        for english, french in rows:
            if not english or not french or '—' not in english or '—' not in french:
                continue
            en_parts = english.split('—', 1)[1].strip().split()
            fr_parts = french.split('—', 1)[1].strip().split(' et ')
            if len(en_parts) != len(fr_parts):
                continue
            for en, fr in zip(en_parts, fr_parts):
                fr = _fold(fr.strip())
                if fr == normalized and fr != _fold(en) and all(part.isalpha() for part in fr.split('-')) and en.isalpha():
                    aliases.add(en.casefold())
    result = tuple(sorted(aliases)) if len(aliases) <= 2 else ()
    with _TYPE_ALIAS_LOCK:
        _TYPE_ALIAS_CACHE[key] = result
        while len(_TYPE_ALIAS_CACHE) > 256:
            _TYPE_ALIAS_CACHE.popitem(last=False)
    return result


def connect(db):
    conn=sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro",uri=True)
    conn.row_factory=sqlite3.Row
    return conn


@contextmanager
def _connection(db):
    conn=connect(db)
    try:
        yield conn
    finally:
        conn.close()


def _page(limit, offset, *, max_size=MAX_PAGE_SIZE):
    limit=max(1,min(max_size,int(limit)))
    offset=max(0,int(offset))
    return limit,offset


def list_editions(db, *, limit=50, offset=0):
    limit,offset=_page(limit,offset)
    with _connection(db) as c:
        total=c.execute("SELECT COUNT(*) FROM editions WHERE eligibility='eligible'").fetchone()[0]
        rows=c.execute("SELECT set_id AS id,code,name,set_type,released_at,digital,source_card_count,eligibility FROM editions WHERE eligibility='eligible' ORDER BY released_at DESC,code LIMIT ? OFFSET ?",(limit,offset)).fetchall()
    return {"contract_version":CONTRACT_VERSION,"total":total,"limit":limit,"offset":offset,"items":[dict(r) for r in rows]}


def _match_term(term, db=None):
    words=[w for w in str(term).split() if w]
    if not words: return None
    # Keep FTS operators outside the quoted user text. Prefixes accept partial
    # card names and abilities without interpreting user input as FTS syntax.
    parts = []
    for word in words:
        original = '"' + word.replace('"', '""') + '"*'
        normalized = _fold(word)
        alternatives = set(_french_type_aliases(db, word)) if db else set()
        if normalized in _FRENCH_TOKEN_ALIASES:
            alternatives.add(_FRENCH_TOKEN_ALIASES[normalized])
        aliases = [f'"{alias}"*' for alias in sorted(alternatives)]
        parts.append(f'({original} OR {" OR ".join(aliases)})' if aliases else original)
    return " AND ".join(parts)


def _set_codes(value):
    codes = [code.strip().casefold() for code in str(value or "").replace("|", ",").split(",") if code.strip()]
    if len(codes) > 2000 or any(not code.isalnum() for code in codes):
        raise ValueError("Codes d'édition invalides")
    return list(dict.fromkeys(codes))


def _search_cache_key(db, values):
    path = Path(db).resolve()
    fingerprints = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            stat = candidate.stat()
            fingerprints.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            fingerprints.append((None, None))
    return (str(path), tuple(fingerprints), *values)


def search_cards(db, term="", *, set_code=None, language=None, rarity=None, color=None, color_mode="contains", mana_value=None, type_text=None, mana_min=None, mana_max=None, power_min=None, power_max=None, toughness_min=None, toughness_max=None, card_type=None, image_filter="all", result_sort="date", oracle_id=None, printing_id=None, unique=False, limit=50, offset=0):
    limit,offset=_page(limit,offset,max_size=300)
    color_mode = str(color_mode).casefold()
    if color_mode not in {"contains", "exact"}:
        raise ValueError("Mode couleur attendu : contains ou exact")
    sort_fields = {"mana": "sr.mana_value", "power": "json_extract(sr.raw_json,'$.power')", "toughness": "json_extract(sr.raw_json,'$.toughness')"}
    if image_filter not in {"all", "with", "without"}:
        raise ValueError("Filtre d'image invalide")
    if result_sort not in {"date", "date_asc", "added_asc", "added_desc"} | {f"{field}_{direction}" for field in sort_fields for direction in ("asc", "desc")}:
        raise ValueError("Classement invalide")
    cache_key = _search_cache_key(db, (str(term), str(set_code or ""), str(language or ""),
        str(rarity or ""), str(color or ""), color_mode, str(mana_value), str(type_text or ""),
        str(mana_min), str(mana_max), str(power_min), str(power_max), str(toughness_min),
        str(toughness_max), str(card_type or ""), image_filter, str(oracle_id or ""), str(printing_id or "")))
    match=None if oracle_id or printing_id else _match_term(term, db)
    filters=[]; args=[]
    if oracle_id:
        filters.append("p.oracle_id=?"); args.append(str(oracle_id))
    elif printing_id:
        filters.append("p.printing_id=?"); args.append(str(printing_id))
    codes = _set_codes(None if set_code == "-" else set_code)
    if set_code == "-": filters.append("0")
    if codes:
        filters.append("e.code IN (" + ",".join("?" for _ in codes) + ")")
        args.extend(codes)
    display_language = str(language).casefold() if language else None
    if rarity == "-": filters.append("0")
    elif rarity:
        rarities = [value.casefold() for value in str(rarity).split("|")]
        if any(value not in {"common", "uncommon", "rare", "special", "mythic"} for value in rarities):
            raise ValueError("Rareté invalide")
        filters.append("sr.rarity IN (" + ",".join("?" for _ in rarities) + ")")
        args.extend(rarities)
    if color:
        selected = set(str(color).upper().replace(",", "").replace(" ", ""))
        if not selected <= set("WUBRGC"):
            raise ValueError("Couleurs attendues : W, U, B, R, G ou C")
        canonical = "".join(symbol for symbol in "WUBRG" if symbol in selected)
        color_filters = []
        if color_mode == "exact" and canonical:
            color_filters.append("length(sr.colors_key)=?")
            args.append(len(canonical))
            color_filters.append("sr.colors_key NOT GLOB '*[^WUBRG]*'")
            for symbol in canonical:
                color_filters.append("instr(sr.colors_key,?)>0")
                args.append(symbol)
        else:
            for symbol in canonical:
                color_filters.append("instr(sr.colors_key,?)>0")
                args.append(symbol)
        if "C" in selected and canonical:
            filters.append("(sr.colors_key='' OR (" + " AND ".join(color_filters) + "))")
        elif "C" in selected:
            filters.append("sr.colors_key=''")
        else:
            filters.extend(color_filters)
    if mana_value is not None: filters.append("sr.mana_value=?"); args.append(float(mana_value))
    if type_text:
        type_pattern='%'+str(type_text).replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        filters.append("(sr.type_line LIKE ? ESCAPE '\\' OR sr.printed_type_line LIKE ? ESCAPE '\\')")
        args.extend([type_pattern,type_pattern])
    if card_type == "-": filters.append("0")
    elif card_type:
        types = set(str(card_type).split("|"))
        allowed_types = {"creature": "Creature", "land": "Land", "instant": "Instant", "sorcery": "Sorcery", "artifact": "Artifact", "enchantment": "Enchantment", "planeswalker": "Planeswalker", "battle": "Battle", "plane": "Plane", "phenomenon": "Phenomenon", "scheme": "Scheme", "conspiracy": "Conspiracy", "vanguard": "Vanguard", "dungeon": "Dungeon", "kindred": "Kindred"}
        if any(value not in allowed_types and value != "token" for value in types):
            raise ValueError("Type de carte invalide")
        predicates = {value: f"sr.type_line LIKE '%{name}%'" for value, name in allowed_types.items()}
        predicates["kindred"] = "(sr.type_line LIKE '%Kindred%' OR sr.type_line LIKE '%Tribal%')"
        predicates["token"] = "(e.set_type='token' OR p.layout IN ('token','double_faced_token'))"
        filters.append("(" + " OR ".join(predicates[value] for value in types) + ")")
        if types != {"token"}:
            filters.extend("NOT " + predicates[value] for value in predicates if value not in types)
    for field, lower, upper in (("sr.mana_value", mana_min, mana_max),
                                ("json_extract(sr.raw_json,'$.power')", power_min, power_max),
                                ("json_extract(sr.raw_json,'$.toughness')", toughness_min, toughness_max)):
        if lower is None and upper is None:
            continue
        if field != "sr.mana_value":
            filters.append(f"{field} IS NOT NULL AND {field} <> '' AND {field} NOT GLOB '*[^0-9.-]*'")
        if lower is not None:
            filters.append(f"CAST({field} AS REAL)>=?")
            args.append(float(lower))
        if upper is not None:
            filters.append(f"CAST({field} AS REAL)<=?")
            args.append(float(upper))
        if lower is not None and upper is not None and float(lower) > float(upper):
            raise ValueError("La borne minimale dépasse la borne maximale")
    where=["e.eligibility='eligible'"]+filters
    if image_filter != "all":
        ready = "EXISTS (SELECT 1 FROM image_slots ims WHERE ims.printing_id=p.printing_id AND ims.status='ready')"
        image_clause = ready if image_filter == "with" else "NOT " + ready
    else:
        image_clause = "1=1"
    if match is not None and not codes:
        with _connection(db) as c:
            edition_codes = [row[0] for row in c.execute(
                "SELECT code FROM editions WHERE eligibility='eligible' AND (code=? OR lower(name)=?)",
                (str(term).strip().casefold(), str(term).strip().casefold()))]
    else:
        edition_codes = []
    name_match = None
    if match is not None and not edition_codes:
        artist_match = match
        if codes:
            scoped = " OR ".join('set_code : "' + code + '"' for code in codes)
            match = "(" + scoped + ") AND " + match
        name_match = f"(name : ({match}) OR printed_name : ({match}))"
        from_sql=("source_search CROSS JOIN source_records sr ON sr.provider_card_id=source_search.provider_card_id "
                  "JOIN printings p USING(printing_id) JOIN editions e USING(set_id)")
        where.append("source_search MATCH ?")
        args.append(match)
    else:
        from_sql=("editions e CROSS JOIN printings p ON p.set_id=e.set_id "
                  "CROSS JOIN source_records sr ON sr.printing_id=p.printing_id")
        if edition_codes:
            where.append("e.code IN (" + ",".join("?" for _ in edition_codes) + ")")
            args.extend(edition_codes)
    clause=' AND '.join(where)
    name_expansion = bool(name_match and not edition_codes)
    base_clause = ' AND '.join(["e.eligibility='eligible'"] + filters)
    if match is not None and not edition_codes:
        matches_sql = f"SELECT DISTINCT p.printing_id FROM {from_sql} WHERE {clause}"
        matches_args = list(args)
        matches_sql += f" UNION SELECT DISTINCT p.printing_id FROM source_records sr JOIN printings p USING(printing_id) JOIN editions e USING(set_id) JOIN search_extra x ON x.provider_card_id=sr.provider_card_id WHERE {base_clause}"
        matches_args.extend(args[:-1])
        with _connection(db) as artist_connection:
            has_artist_search = artist_connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artist_search'").fetchone()
        if has_artist_search:
            matches_sql += f" UNION SELECT DISTINCT p.printing_id FROM artist_search JOIN source_records sr ON sr.provider_card_id=artist_search.provider_card_id JOIN printings p USING(printing_id) JOIN editions e USING(set_id) WHERE {base_clause} AND artist_search MATCH ?"
            matches_args.extend(args[:-1])
            matches_args.append(artist_match)
        if name_expansion:
            expanded_from = "editions e CROSS JOIN printings p ON p.set_id=e.set_id CROSS JOIN source_records sr ON sr.printing_id=p.printing_id"
            matches_sql += f" UNION SELECT DISTINCT p.printing_id FROM {expanded_from} WHERE {base_clause} AND p.oracle_id IN (SELECT oracle_id FROM search_oracles)"
            matches_args.extend(args[:-1])
        matches_cte = f"matches AS ({matches_sql})"
    else:
        matches_cte = f"matches AS (SELECT DISTINCT p.printing_id FROM {from_sql} WHERE {clause})"
        matches_args = list(args)
    language_clause = "AND sr.lang=?" if display_language else ""
    language_args = [display_language] if display_language else []
    ranked = f"""WITH {matches_cte}, ranked_printings AS (
        SELECT p.printing_id,
               ROW_NUMBER() OVER (PARTITION BY p.printing_id
                 ORDER BY CASE sr.lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 WHEN 'ja' THEN 2 ELSE 3 END,
                          CAST(p.collector_number AS INTEGER),p.collector_number,p.printing_id,sr.provider_card_id) AS language_rank
        FROM matches m JOIN printings p ON p.printing_id=m.printing_id
        JOIN source_records sr ON sr.printing_id=p.printing_id
        WHERE {language_clause[4:] if language_clause else '1=1'} AND {image_clause}
    ), chosen AS (SELECT printing_id FROM ranked_printings WHERE language_rank=1)"""
    ranked_args = matches_args + language_args
    with _connection(db) as c:
        has_dates = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='printing_added'").fetchone()
        # Reuse the expensive FTS/filter/grouping candidate set when users change
        # only sort direction or page. The active database fingerprint invalidates
        # this small bounded cache when a version or image slot changes.
        with _SEARCH_CANDIDATE_LOCK:
            cached_ids = _SEARCH_CANDIDATE_CACHE.get(cache_key)
            if cached_ids is not None:
                _SEARCH_CANDIDATE_CACHE.move_to_end(cache_key)
        if cached_ids is None:
            if name_expansion:
                extra_ids, extra_name_ids = _extra_matches(db, match, name_match)
                c.execute("CREATE TEMP TABLE search_extra (provider_card_id TEXT PRIMARY KEY) WITHOUT ROWID")
                c.executemany("INSERT OR IGNORE INTO search_extra VALUES(?)", ((value,) for value in extra_ids))
                c.execute("CREATE TEMP TABLE search_extra_names (provider_card_id TEXT PRIMARY KEY) WITHOUT ROWID")
                c.executemany("INSERT OR IGNORE INTO search_extra_names VALUES(?)", ((value,) for value in extra_name_ids))
                c.execute("CREATE TEMP TABLE search_oracles (oracle_id TEXT PRIMARY KEY) WITHOUT ROWID")
                oracle_ids = c.execute("""SELECT DISTINCT p.oracle_id
                    FROM source_search
                    JOIN source_records sr ON sr.provider_card_id=source_search.provider_card_id
                    JOIN printings p ON p.printing_id=sr.printing_id
                    WHERE source_search MATCH ? AND p.oracle_id IS NOT NULL""", (name_match,)).fetchall()
                c.executemany("INSERT INTO search_oracles VALUES(?)", ((row[0],) for row in oracle_ids))
                c.execute("INSERT OR IGNORE INTO search_oracles SELECT p.oracle_id FROM search_extra_names x JOIN source_records sr USING(provider_card_id) JOIN printings p USING(printing_id) WHERE p.oracle_id IS NOT NULL")
            c.execute("CREATE TEMP TABLE chosen_search AS " + ranked + " SELECT printing_id FROM chosen", ranked_args)
            count = c.execute("SELECT COUNT(*) FROM chosen_search").fetchone()[0]
            if count <= _SEARCH_CANDIDATE_MAX_ROWS:
                cached_ids = tuple(row[0] for row in c.execute("SELECT printing_id FROM chosen_search"))
                with _SEARCH_CANDIDATE_LOCK:
                    _SEARCH_CANDIDATE_CACHE[cache_key] = cached_ids
                    _SEARCH_CANDIDATE_CACHE.move_to_end(cache_key)
                    while len(_SEARCH_CANDIDATE_CACHE) > _SEARCH_CANDIDATE_CACHE_SIZE:
                        _SEARCH_CANDIDATE_CACHE.popitem(last=False)
        else:
            c.execute("CREATE TEMP TABLE chosen_search (printing_id TEXT PRIMARY KEY) WITHOUT ROWID")
            c.executemany("INSERT INTO chosen_search VALUES(?)", ((printing_id,) for printing_id in cached_ids))
        if str(unique).casefold() in {"1", "true", "yes"}:
            c.execute("""CREATE TEMP TABLE unique_search AS
                SELECT printing_id FROM (
                  SELECT p.printing_id, ROW_NUMBER() OVER (
                    PARTITION BY COALESCE((SELECT lower(trim(sr.name)) FROM source_records sr
                      WHERE sr.printing_id=p.printing_id
                      ORDER BY CASE sr.lang WHEN 'en' THEN 0 WHEN 'fr' THEN 1 ELSE 2 END,
                               sr.provider_card_id LIMIT 1), p.printing_id)
                    ORDER BY e.released_at DESC,e.code DESC,
                             CAST(p.collector_number AS INTEGER) DESC,p.collector_number DESC,
                             p.printing_id DESC) AS rank
                  FROM chosen_search JOIN printings p USING(printing_id)
                  JOIN editions e USING(set_id)) WHERE rank=1""")
            c.execute("DELETE FROM chosen_search WHERE printing_id NOT IN (SELECT printing_id FROM unique_search)")
            c.execute("DROP TABLE unique_search")
        count=c.execute("SELECT COUNT(*) FROM chosen_search").fetchone()[0]
        order = "e.released_at DESC,e.code,CAST(p.collector_number AS INTEGER),p.collector_number,p.printing_id"
        if result_sort == "date_asc":
            order = "e.released_at ASC,e.code,CAST(p.collector_number AS INTEGER),p.collector_number,p.printing_id"
        elif result_sort in {"added_asc", "added_desc"}:
            direction = "ASC" if result_sort == "added_asc" else "DESC"
            if image_filter == "with":
                asset_root = Path(db).resolve().parent
                def image_mtime(relative):
                    path = (asset_root / relative).resolve()
                    if not path.is_relative_to(asset_root):
                        return None
                    cache_key = str(path)
                    with _IMAGE_MTIME_LOCK:
                        cached = _IMAGE_MTIME_CACHE.get(cache_key)
                        if cached is not None:
                            _IMAGE_MTIME_CACHE.move_to_end(cache_key)
                            return cached
                    try:
                        value = os.stat(path).st_mtime
                    except OSError:
                        return None
                    with _IMAGE_MTIME_LOCK:
                        _IMAGE_MTIME_CACHE[cache_key] = value
                        _IMAGE_MTIME_CACHE.move_to_end(cache_key)
                        while len(_IMAGE_MTIME_CACHE) > _IMAGE_MTIME_CACHE_SIZE:
                            _IMAGE_MTIME_CACHE.popitem(last=False)
                    return value
                c.create_function("image_mtime", 1, image_mtime)
                date_value = "(SELECT MAX(image_mtime(a.relative_path)) FROM image_slots ims JOIN image_assets a USING(asset_id) WHERE ims.printing_id=p.printing_id AND ims.status='ready')"
            elif has_dates:
                date_value = "(SELECT added_at FROM printing_added WHERE printing_id=p.printing_id)"
            else:
                date_value = "NULL"
            # Historical cards can share one migration timestamp; reverse their
            # stable tie-breakers too so ascending and descending remain distinct.
            order = (f"({date_value}) IS NULL,({date_value}) {direction},"
                     f"e.released_at {direction},e.code {direction},"
                     f"CAST(p.collector_number AS INTEGER) {direction},"
                     f"p.collector_number {direction},p.printing_id {direction}")
        elif result_sort != "date":
            field_name, direction = result_sort.rsplit("_", 1)
            field = sort_fields[field_name]
            value = f"(SELECT {field} FROM source_records sr WHERE sr.printing_id=p.printing_id ORDER BY CASE sr.lang WHEN 'fr' THEN 0 ELSE 1 END LIMIT 1)"
            numeric = f"CASE WHEN {value} IS NULL OR {value} GLOB '*[^0-9.-]*' THEN NULL ELSE CAST({value} AS REAL) END"
            order = f"({numeric}) IS NULL,({numeric}) {direction.upper()}," + order
        page=""" SELECT chosen_search.printing_id FROM chosen_search
            JOIN printings p USING(printing_id) JOIN editions e USING(set_id)
            ORDER BY """ + order + " LIMIT ? OFFSET ?"
        printing_ids=[row[0] for row in c.execute(page,[limit,offset])]
        rows=[]
        if printing_ids:
            values = ",".join("(?,?)" for _ in printing_ids)
            page_args = [value for ordinal, printing_id in enumerate(printing_ids) for value in (printing_id, ordinal)]
            row_query = f"""WITH page(printing_id,ordinal) AS (VALUES {values}), ranked AS (
                SELECT page.ordinal,sr.provider_card_id,p.printing_id,e.code AS set_code,e.name AS edition,
                    sr.lang,sr.name,sr.printed_name,sr.type_line,sr.printed_type_line,
                    sr.mana_cost,sr.mana_value,sr.rarity,p.collector_number,p.layout,
                    p.identity_status,sr.oracle_text,sr.printed_text,
                    ROW_NUMBER() OVER (PARTITION BY p.printing_id ORDER BY
                      CASE sr.lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 WHEN 'ja' THEN 2 ELSE 3 END,
                      sr.provider_card_id) AS language_rank
                FROM page JOIN printings p USING(printing_id) JOIN editions e USING(set_id)
                JOIN source_records sr USING(printing_id) WHERE (? IS NULL OR sr.lang=?)
            ) SELECT provider_card_id,printing_id,set_code,edition,lang,name,printed_name,type_line,
                printed_type_line,mana_cost,mana_value,rarity,collector_number,layout,identity_status,
                oracle_text,printed_text FROM ranked WHERE language_rank=1 ORDER BY ordinal"""
            rows = c.execute(row_query, page_args + [display_language, display_language]).fetchall()
    return {"contract_version":CONTRACT_VERSION,"total":count,"limit":limit,"offset":offset,"items":[_readable_record(r) for r in rows]}


def card_detail(db, provider_card_id):
    with _connection(db) as c:
        row=c.execute("SELECT sr.*,p.set_id,p.oracle_id,p.collector_number,p.layout,p.identity_status,p.variant_attributes_json,e.code AS set_code,e.name AS edition FROM source_records sr JOIN printings p USING(printing_id) JOIN editions e USING(set_id) WHERE sr.provider_card_id=?",(str(provider_card_id),)).fetchone()
        if row is None: return {"contract_version":CONTRACT_VERSION,"found":False,"card":None}
        result=_readable_record(row)
        translations=[_readable_record(r) for r in c.execute("SELECT lang,name,printed_name,type_line,printed_type_line,oracle_text,printed_text,raw_json FROM source_records WHERE printing_id=? ORDER BY lang",(row['printing_id'],))]
        image_slots=[dict(r) for r in c.execute("SELECT face_index,preferred_language,selected_language,status,asset_id FROM image_slots WHERE printing_id=? ORDER BY face_index",(row['printing_id'],))]
    localized=next((x for x in translations if x['lang']=='fr'),None)
    fallback=next((x for x in translations if x['lang']=='en'),None)
    if fallback is None:
        fallback=next((x for x in translations if x['lang']=='ph'),None)
    result['translations']=translations
    result['display_text']={"language":"fr" if localized and localized['printed_text'] else ("en" if fallback and fallback['oracle_text'] else None),"value":(localized or fallback or {}).get('printed_text') if localized and localized['printed_text'] else (fallback or {}).get('oracle_text') if fallback else None,"fallback_used":not bool(localized and localized['printed_text'])}
    result['image_slots']=image_slots
    return {"contract_version":CONTRACT_VERSION,"found":True,"card":result}


def edition_cards(db, set_code, *, limit=50, offset=0, proxy_groups=False):
    limit, offset = _page(limit, offset, max_size=300)
    codes = _set_codes(set_code)
    if not codes:
        return {"contract_version": CONTRACT_VERSION, "total": 0, "limit": limit, "offset": offset, "items": []}
    placeholders = ",".join("?" for _ in codes)
    ranked = """WITH ranked AS (
        SELECT sr.provider_card_id,p.printing_id,e.code AS set_code,e.name AS edition,
               sr.lang,sr.name,sr.printed_name,sr.type_line,sr.printed_type_line,
               sr.mana_cost,sr.mana_value,sr.rarity,p.collector_number,p.layout,
               p.identity_status,sr.oracle_text,sr.printed_text,p.variant_attributes_json,
               ROW_NUMBER() OVER (
                   PARTITION BY p.printing_id
                   ORDER BY CASE sr.lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 WHEN 'ja' THEN 2 WHEN 'ph' THEN 3 ELSE 4 END,
                            CAST(p.collector_number AS INTEGER),p.collector_number,
                            p.printing_id,sr.provider_card_id
               ) AS preferred_rank
        FROM editions e
        CROSS JOIN printings p ON p.set_id=e.set_id
        CROSS JOIN source_records sr ON sr.printing_id=p.printing_id
        WHERE e.eligibility='eligible' AND e.code IN (""" + placeholders + """)
    )"""
    with _connection(db) as c:
        total = c.execute(ranked + " SELECT COUNT(*) FROM ranked WHERE preferred_rank=1", codes).fetchone()[0]
        association = ",json_extract(variant_attributes_json,'$.proxy_source_id') AS proxy_source_id" if proxy_groups else ""
        order = "proxy_source_id IS NOT NULL," if proxy_groups else ""
        rows = c.execute(
            ranked + " SELECT provider_card_id,printing_id,set_code,edition,lang,name,printed_name,"
            "type_line,printed_type_line,mana_cost,mana_value,rarity,collector_number,layout,"
            "identity_status,oracle_text,printed_text" + association + " FROM ranked WHERE preferred_rank=1 "
            "ORDER BY " + order + "CAST(collector_number AS INTEGER),collector_number,printing_id LIMIT ? OFFSET ?",
            codes + [limit, offset],
        ).fetchall()
    return {"contract_version": CONTRACT_VERSION, "total": total, "limit": limit,
            "offset": offset, "items": [_readable_record(row) for row in rows]}
