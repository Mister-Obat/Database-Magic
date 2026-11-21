"""Local proxy catalogue, separate from the published provider database."""

import hashlib
import io
import json
import mimetypes
import sqlite3
from collections import OrderedDict
from contextlib import closing
from datetime import datetime, timezone
from functools import cmp_to_key
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from urllib.parse import urlencode
from uuid import uuid4

from PIL import Image, ImageOps

from .v2 import api, query

SET = "proxy"
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp"}


def _merge_rank_page(proxy_ranks, official_ranks, directions, limit, offset):
    def compare(left, right):
        for a, b, direction in zip(left, right, directions):
            if a == b:
                continue
            comparison = -1 if a is None or (b is not None and a < b) else 1
            return -comparison if direction == "DESC" else comparison
        return 0

    return sorted(proxy_ranks + official_ranks, key=cmp_to_key(compare))[offset:offset + limit]


class Proxies:
    def __init__(self, root, state):
        self.root = Path(root)
        self.state = Path(state)
        self.database = self.state / "proxies.sqlite"
        self.lock = RLock()
        self.signature = None
        self.search_snapshots = OrderedDict()

    def files(self):
        if not self.root.is_dir():
            return {}
        paths = sorted(self.root.iterdir(), key=lambda path: (path.name.casefold(), path.name))
        return {
            "proxy-" + hashlib.sha256(path.name.encode("utf-8")).hexdigest(): path
            for path in paths
            if path.suffix.lower() in IMAGE_TYPES and not path.is_symlink() and path.is_file()
            and path.resolve().parent == self.root.resolve()
        }

    def image(self, identifier):
        path = self.files().get(identifier)
        if path is None:
            raise FileNotFoundError(identifier)
        return path.read_bytes(), mimetypes.guess_type(path)[0] or "application/octet-stream"

    def linked(self, database, identifier):
        """Return the current proxy and its local image for deck and image actions."""
        with self.lock:
            files = self.files()
            path = files.get(identifier)
            if path is None:
                return None
            self._rebuild(Path(database), files)
            card = self._card(identifier)["card"]
            return (card, path) if card["proxy_source_id"] else None

    def printing_ids(self, database, oracle_id):
        if not oracle_id:
            return set()
        with self.lock:
            files = self.files()
            if not files:
                return set()
            self._rebuild(Path(database), files)
            with closing(query.connect(self.database)) as conn:
                return {row[0] for row in conn.execute(
                    "SELECT printing_id FROM printings WHERE oracle_id=?", (oracle_id,))}

    def replace_image(self, database, identifier, data):
        with self.lock:
            linked = self.linked(database, identifier)
            if not linked:
                raise ValueError("Proxy associé introuvable.")
            _, path = linked
            image_format = Image.registered_extensions().get(path.suffix.lower())
            if not image_format:
                raise ValueError("Format du fichier proxy non pris en charge.")
            draft = None
            try:
                with Image.open(io.BytesIO(data)) as incoming:
                    if incoming.width * incoming.height > 30_000_000:
                        raise ValueError("Image de proxy trop grande.")
                    picture = ImageOps.exif_transpose(incoming).convert(
                        "RGB" if image_format in {"JPEG", "BMP"} else "RGBA")
                    with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as target:
                        draft = Path(target.name)
                        picture.save(target, format=image_format)
                draft.replace(path)
            except (OSError, KeyError, Image.DecompressionBombError) as exc:
                raise ValueError("Impossible de remplacer l’image du proxy.") from exc
            finally:
                if draft:
                    draft.unlink(missing_ok=True)
            return {"provider_card_id": identifier, "face_index": 0}

    def import_image(self, filename, data):
        name = Path(filename)
        if (not filename or len(filename) > 160 or name.name != filename
                or any(char in filename for char in '<>:"/\\|?*')
                or any(ord(char) < 32 for char in filename)
                or name.suffix.lower() not in IMAGE_TYPES):
            raise ValueError("Nom ou format de fichier proxy invalide.")
        try:
            with Image.open(io.BytesIO(data)) as image:
                if (image.width * image.height > 30_000_000
                        or Image.registered_extensions().get(name.suffix.lower()) != image.format):
                    raise ValueError("Dimensions ou format de l’image proxy invalide.")
                image.verify()
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError("Fichier image proxy invalide.") from exc

        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.root, suffix=".tmp", delete=False) as draft:
                temporary = Path(draft.name)
                try:
                    draft.write(data)
                    draft.close()
                    target = self.root / name
                    counter = 1
                    while True:
                        try:
                            target.hardlink_to(temporary)
                            break
                        except FileExistsError:
                            counter += 1
                            target = self.root / f"{name.stem} ({counter}){name.suffix}"
                finally:
                    draft.close()
                    temporary.unlink(missing_ok=True)
            self.signature = None
        return {"filename": target.name}

    def associations(self):
        return sqlite3.connect(self.state / "proxy-associations.sqlite")

    def delete(self, identifier):
        if not isinstance(identifier, str):
            raise ValueError("Identifiant de proxy invalide.")
        with self.lock:
            path = self.files().get(identifier)
            if path is None:
                raise FileNotFoundError("Proxy introuvable.")
            try:
                if (self.state / "proxy-associations.sqlite").is_file():
                    with closing(self.associations()) as conn, conn:
                        conn.execute("DELETE FROM associations WHERE id=?", (identifier,))
                        path.unlink()
                else:
                    path.unlink()
            finally:
                self.signature = None
            return {"deleted": True, "provider_card_id": identifier}

    def associate(self, database, identifier, source_id):
        with self.lock:
            if not isinstance(identifier, str) or identifier not in self.files():
                raise ValueError("Proxy introuvable.")
            if not isinstance(source_id, str):
                raise ValueError("Impression réelle introuvable.")
            with closing(query.connect(database)) as source:
                row = source.execute("SELECT printing_id FROM source_records WHERE provider_card_id=?",
                                     (source_id,)).fetchone()
            if not row:
                raise ValueError("Impression réelle introuvable.")
            self.state.mkdir(parents=True, exist_ok=True)
            with closing(self.associations()) as conn, conn:
                conn.execute("CREATE TABLE IF NOT EXISTS associations (id TEXT PRIMARY KEY, source_id TEXT NOT NULL)")
                conn.execute("INSERT OR REPLACE INTO associations VALUES (?,?)", (identifier, source_id))
            self.signature = None
            return {"status": "saved"}

    def _rebuild(self, database, files):
        def fingerprint(path):
            try:
                stat = path.stat()
            except FileNotFoundError:
                return None
            return stat.st_ino, stat.st_mtime_ns, stat.st_size

        links_path = self.state / "proxy-associations.sqlite"
        signature = (query.catalogue_text_key(database), tuple(files),
                     fingerprint(links_path), fingerprint(links_path.with_suffix(".sqlite-wal")))
        if (*signature, fingerprint(self.database)) == self.signature:
            return
        self.state.mkdir(parents=True, exist_ok=True)
        links = {}
        if links_path.exists():
            with closing(self.associations()) as conn:
                links = dict(conn.execute("SELECT id,source_id FROM associations"))
        with NamedTemporaryFile(dir=self.state, prefix="proxies-", suffix=".tmp", delete=False) as tmp:
            draft = Path(tmp.name)
        try:
            with closing(query.connect(database)) as source, closing(sqlite3.connect(draft)) as out, out:
                out.executescript((Path(__file__).parent / "v2" / "schema.sql").read_text(encoding="utf-8"))
                out.execute("INSERT INTO editions VALUES (?,?,?,?,?,?,?,?,?)",
                            (SET, SET, "Proxy", SET, None, 0, len(files), "eligible", None))
                for number, (identifier, path) in enumerate(files.items(), 1):
                    self._insert(source, out, identifier, number, path, links.get(identifier))
            self.signature = (*signature, fingerprint(draft))
            draft.replace(self.database)
        finally:
            draft.unlink(missing_ok=True)

    @staticmethod
    def _insert(source, out, identifier, number, path, source_id):
        printing = source.execute(
            "SELECT p.* FROM printings p JOIN source_records sr USING(printing_id) WHERE sr.provider_card_id=?",
            (source_id,)).fetchone()
        layout = printing["layout"] if printing else "normal"
        oracle_id = printing["oracle_id"] if printing else None
        if oracle_id:
            out.execute("INSERT OR IGNORE INTO oracle_cards VALUES (?,1)", (oracle_id,))
        out.execute("INSERT INTO printings VALUES (?,?,?,?,?,?,?,?,?)",
                    (identifier, SET, str(number), oracle_id, layout, None,
                     printing["identity_status"] if printing else "unresolved_identity",
                     "local_proxy", json.dumps({"proxy_source_id": source_id if printing else None})))
        records = source.execute(
            "SELECT * FROM source_records WHERE printing_id=? "
            "ORDER BY provider_card_id=? DESC, lang, provider_card_id",
            (printing["printing_id"], source_id),
        ).fetchall() if printing else []
        if not records:
            records = [{"provider_card_id": None, "lang": "und", "name": path.stem, "colors_json": "[]",
                        "color_identity_json": "[]", "colors_key": "", "color_identity_key": "",
                        "artist_json": "[]", "raw_json": "{}"}]
        seen = set()
        for row in records:
            record = dict(row)
            if record["lang"] in seen:
                continue
            seen.add(record["lang"])
            provider = identifier if record["provider_card_id"] == source_id or not printing else identifier + "-" + record["lang"]
            raw = json.loads(record["raw_json"])
            for key in ("image_uris", "scryfall_uri", "uri", "prints_search_uri"):
                raw.pop(key, None)
            for face in raw.get("card_faces") or []:
                if isinstance(face, dict):
                    face.pop("image_uris", None)
            raw.update(id=provider, set=SET, set_id=SET, set_name="Proxy", collector_number=str(number))
            record.update(provider_card_id=provider, printing_id=identifier, source_uri=None, raw_json=json.dumps(raw))
            columns = ",".join(record)
            placeholders = ",".join("?" for _ in record)
            out.execute(f"INSERT INTO source_records ({columns}) VALUES ({placeholders})", tuple(record.values()))
            out.execute("INSERT INTO printing_languages VALUES (?,?,?)", (identifier, record["lang"], provider))
            out.execute("INSERT INTO source_search VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (provider, SET, record["lang"], record["name"],
                         *(record.get(key) or "" for key in ("printed_name", "type_line", "printed_type_line",
                                                             "oracle_text", "printed_text")), record["artist_json"]))
        url = "/api/v2/proxies/image?" + urlencode({"id": identifier})
        out.execute("INSERT INTO card_faces VALUES (?,0,1)", (identifier,))
        out.execute("INSERT INTO image_assets VALUES (?,?,?,0,1,1,'und','{}')", (identifier, url, "image/local"))
        out.execute("INSERT INTO image_slots VALUES (?,0,'und','und','ready',?,'{}')", (identifier, identifier))

    @staticmethod
    def _decorate(card):
        identifier = card["printing_id"]
        url = "/api/v2/proxies/image?" + urlencode({"id": identifier})
        card.update(provider_card_id=identifier, proxy=True, image_url=url)
        return card

    def _card(self, identifier):
        result = api.dispatch(self.database, "/api/v2/card", {"id": identifier})
        card = self._decorate(result["card"])
        card["proxy_source_id"] = json.loads(card["variant_attributes_json"]).get("proxy_source_id")
        return result

    def _search_ranks(self, params, files):
        def proxy_date(identifier):
            try:
                stat = files[identifier].stat()
            except OSError:
                return None
            if params.get("image_filter") == "with":
                return stat.st_mtime
            created = getattr(stat, "st_birthtime", stat.st_ctime)
            return datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        ranks = api.dispatch(self.database, "/api/v2/search",
                             {**params, "limit": len(files), "offset": 0},
                             ranks_only=True, proxy_date=proxy_date)
        cards = query.read_search_rank_page(self.database, ranks["ranks"],
                                            display_language=ranks["display_language"])
        return ranks, {card["printing_id"]: self._decorate(card) for card in cards}

    @staticmethod
    def _merge_search(database, params, enrich, empty, local_ranks, local_cards):
        if not local_cards:
            return enrich(database, api.dispatch(database, "/api/v2/search", params))
        limit, offset = empty["limit"], empty["offset"]
        # Any official row before this offset is strictly before the requested
        # page, even if every matching proxy precedes it.
        official_offset = max(0, offset - local_ranks["total"])
        official = api.dispatch(database, "/api/v2/search",
                                {**params, "limit": offset - official_offset + limit,
                                 "offset": official_offset}, ranks_only=True)
        skipped = min(official_offset, official["total"])
        ranks = _merge_rank_page(local_ranks["ranks"], official["ranks"], official["directions"],
                                 limit, offset - skipped)
        rows = query.read_search_rank_page(database, [rank for rank in ranks if rank[-1] not in local_cards],
                                           display_language=official["display_language"])
        official_cards = {card["printing_id"]: card for card in enrich(database, {"items": rows})["items"]}
        return {**empty, "total": local_ranks["total"] + official["total"],
                "items": [local_cards[rank[-1]] if rank[-1] in local_cards else official_cards[rank[-1]]
                          for rank in ranks]}

    def start_search(self, database, params, enrich):
        with self.lock:
            files = self.files() if not params.get("official_only") else {}
            if files:
                self._rebuild(Path(database), files)
                local, _ = self._search_ranks(params, files)
            else:
                local = {"ranks": []}
        official = api.dispatch(database, "/api/v2/search", {**params, "limit": None}, ranks_only=True)
        if params.get("result_sort") in {None, "", "date", "date_asc"}:
            ranks = local["ranks"] + official["ranks"]
        else:
            ranks = _merge_rank_page(local["ranks"], official["ranks"], official["directions"],
                                     len(local["ranks"]) + len(official["ranks"]), 0)
        snapshot = (Path(database), tuple(rank[-1] for rank in ranks), official["display_language"])
        token = uuid4().hex
        with self.lock:
            self.search_snapshots[token] = snapshot
            if len(self.search_snapshots) > 12:
                self.search_snapshots.popitem(last=False)
        return self._snapshot_page(snapshot, token, params, enrich)

    def search_page(self, database, params, enrich):
        token = params.get("snapshot")
        with self.lock:
            snapshot = self.search_snapshots.get(token)
            if snapshot is None or snapshot[0] != Path(database):
                return None
            self.search_snapshots.move_to_end(token)
        return self._snapshot_page(snapshot, token, params, enrich)

    def _snapshot_page(self, snapshot, token, params, enrich):
        database, printing_ids, language = snapshot
        limit = min(300, max(1, int(params.get("limit", 50))))
        total = len(printing_ids)
        offset = min(max(0, int(params.get("offset", 0))), max(0, (total - 1) // limit) * limit)
        ids = printing_ids[offset:offset + limit]
        official = query.read_search_rank_page(database, [(key,) for key in ids if not key.startswith("proxy-")],
                                               display_language=language)
        by_id = {item["printing_id"]: item for item in enrich(database, {"items": official})["items"]}
        proxies = [(key,) for key in ids if key.startswith("proxy-")]
        if proxies:
            with self.lock:
                files = self.files()
                if files:
                    self._rebuild(database, files)
                    for item in query.read_search_rank_page(self.database, proxies, display_language=language):
                        by_id[item["printing_id"]] = self._decorate(item)
        return {"contract_version": query.CONTRACT_VERSION, "snapshot": token, "total": total,
                "offset": offset, "limit": limit, "items": [by_id[key] for key in ids if key in by_id]}

    def dispatch(self, database, route, params, enrich):
        database = Path(database)
        offset = max(0, int(params.get("offset", 0)))
        limit = min(query.MAX_PAGE_SIZE if route == "/api/v2/editions" else 300,
                    max(1, int(params.get("limit", 50))))
        if route == "/api/v2/editions":
            enrich = lambda _db, result: result
        if route == "/api/v2/edition" and params.get("focus") and params.get("set") != SET:
            return enrich(database, api.dispatch(database, route, params))
        merge_search = route == "/api/v2/search" and params.get("result_sort") not in {None, "", "date", "date_asc"}
        empty = {"contract_version": query.CONTRACT_VERSION, "items": [], "total": 0, "limit": limit, "offset": offset}
        local = None
        local_ranks = None
        with self.lock:
            files = self.files()
            if route == "/api/v2/card":
                identifier = params.get("id")
                if identifier not in files:
                    return {"contract_version": query.CONTRACT_VERSION, "found": False, "card": None}
                self._rebuild(database, files)
                return self._card(identifier)
            if files and not params.get("official_only"):
                self._rebuild(database, files)
                if merge_search:
                    local_ranks, local_cards = self._search_ranks(params, files)
                elif route == "/api/v2/proxies":
                    local = query.edition_cards(self.database, SET, limit=limit, offset=offset, proxy_groups=True)
                else:
                    local = api.dispatch(self.database, route, {**params, "limit": limit, "offset": offset})
                if local is not None and route != "/api/v2/editions":
                    for card in local["items"]:
                        self._decorate(card)
                if route == "/api/v2/proxies" or (route == "/api/v2/edition" and params.get("set") == SET):
                    return local
        if local_ranks is not None:
            return self._merge_search(database, params, enrich, empty, local_ranks, local_cards)
        if local is None:
            return empty if route == "/api/v2/proxies" else enrich(database, api.dispatch(database, route, params))
        remaining = limit - len(local["items"])
        official = api.dispatch(database, route, {**params, "limit": max(1, remaining),
                                                  "offset": max(0, offset - local["total"])})
        if remaining:
            local["items"].extend(enrich(database, official)["items"])
        local["total"] += official["total"]
        return local
