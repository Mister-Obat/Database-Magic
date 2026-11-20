"""Durable personal compositions, independent of published card catalogues."""
from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .deck_media import enrich_deck_media, export_deck_to_desktop
from .v2.query import connect


SECTION_NAMES = ("Créatures", "Rituels", "Éphémères", "Artefacts", "Enchantements",
                 "Planeswalkers", "Terrains", "Jetons")
SECTION_LABELS = {"commander": "Commandant", "mainboard": "Deck principal",
                  "sideboard": "Réserve", "display_commander": "Commandant de présentation",
                  "planar_deck": "Deck planaire", "scheme_deck": "Machinations", "tokens": "Jetons"}
_BACKUP_LOCK = threading.Lock()


class RevisionConflict(ValueError):
    pass


def _id():
    return str(uuid.uuid4())


def _text(value, label, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} invalide.")
    return value.strip()


def _colors(value):
    if not isinstance(value, list) or any(not isinstance(c, str) or len(c) != 1 or c not in "WUBRG" for c in value):
        raise ValueError("Couleurs invalides : choisir W/U/B/R/G.")
    return [c for c in "WUBRG" if c in value]


@contextmanager
def _database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS personal_decks (
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS personal_deck_revisions (
                deck_id TEXT NOT NULL REFERENCES personal_decks(id), revision INTEGER NOT NULL,
                document TEXT NOT NULL, PRIMARY KEY(deck_id,revision));
            CREATE TABLE IF NOT EXISTS personal_deck_groups (
                id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS personal_deck_group_assignments (
                deck_id TEXT PRIMARY KEY REFERENCES personal_decks(id),
                group_id TEXT NOT NULL REFERENCES personal_deck_groups(id) ON DELETE CASCADE);
        """)
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
        if conn.total_changes:
            backups = path.parent / "personal-decks-backups"
            temporary = backups / f"{uuid.uuid4().hex}.sqlite.tmp"
            try:
                backups.mkdir(exist_ok=True)
                with _BACKUP_LOCK:
                    with closing(sqlite3.connect(temporary)) as backup:
                        conn.backup(backup)
                    os.replace(temporary, backups / "latest.sqlite")
            except (OSError, sqlite3.Error):
                # The primary transaction is already durable: acknowledge it to
                # avoid duplicate additions on a retry, and retain the last backup.
                logging.exception("Composition enregistrée ; sauvegarde secondaire indisponible")
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logging.exception("Nettoyage de sauvegarde temporaire impossible")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _read(conn, deck_id, *, include_deleted=False):
    if not isinstance(deck_id, str):
        raise ValueError("Identifiant de deck requis.")
    row = conn.execute("SELECT document FROM personal_decks WHERE id=?", (deck_id,)).fetchone()
    if row is None:
        raise KeyError("Deck personnel introuvable.")
    deck = json.loads(row[0])
    if deck.get("deleted_at") and not include_deleted:
        raise KeyError("Deck personnel introuvable.")
    return deck


def restore_backup(backup_path, destination):
    """Restore into a NEW file only; never replace a damaged original automatically."""
    destination = Path(destination)
    with closing(sqlite3.connect(f"file:{Path(backup_path).resolve().as_posix()}?mode=ro", uri=True)) as source:
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Sauvegarde SQLite endommagée.")
        source.execute("SELECT id,revision,document FROM personal_decks LIMIT 1")
        with destination.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(destination)) as restored:
                source.backup(restored)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return destination


def _write(conn, deck):
    deck["updated_at"] = datetime.now(timezone.utc).isoformat()
    document = json.dumps(deck, ensure_ascii=False)
    conn.execute("INSERT INTO personal_decks VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,document=excluded.document",
                 (deck["id"], deck["revision"], document))
    conn.execute("INSERT INTO personal_deck_revisions VALUES(?,?,?)", (deck["id"], deck["revision"], document))
    return deck


def _new(name, colors, sections=None, commander=False):
    names = (["Commandant"] if commander else []) + list(SECTION_NAMES)
    return {"id": _id(), "name": _text(name, "Nom"), "colors": _colors(colors), "revision": 1,
            "sections": sections if sections is not None else [
                {"id": _id(), "name": name, "cards": [],
                 **({"source_section": "tokens"} if name == "Jetons" else
                    {"source_section": "commander"} if name == "Commandant" else {})} for name in names]}


def create_deck(path, name, colors, commander=False):
    with _database(path) as conn:
        return _write(conn, _new(name, colors, commander=bool(commander)))


def create_deck_from_text(path, name, colors, sections):
    if not 1 <= len(sections) <= 100:
        raise ValueError("Conserver de 1 à 100 sections.")
    prepared = []
    for section in sections:
        cards = []
        for card in section["cards"]:
            count = card.get("count")
            if type(count) is not int or not 1 <= count <= 9999:
                raise ValueError("Quantité de carte invalide.")
            cards.append({**card, "id": _id()})
        prepared.append({**section, "id": _id(), "name": _text(section["name"], "Nom de section"),
                         "cards": cards})
    deck = _new(name, colors, sections=prepared)
    with _database(path) as conn:
        return _write(conn, deck)


def duplicate_deck(path, deck_id, name, revision):
    with _database(path) as conn:
        source = _read(conn, deck_id)
        if type(revision) is not int or revision != source["revision"]:
            raise RevisionConflict("Le deck a changé. Actualisez-le avant de le dupliquer.")
        for section in source["sections"]:
            section["id"] = _id()
            for card in section["cards"]:
                card["id"] = _id()
        return _write(conn, _new(name, source["colors"], sections=source["sections"]))


def _read_connection(path):
    return closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True))


def get_deck(path, deck_id):
    if not Path(path).is_file():
        raise KeyError("Deck personnel introuvable.")
    with _read_connection(path) as conn:
        return _read(conn, deck_id)


def list_decks(path, query="", colors=None, *, include_groups=False):
    if not Path(path).is_file():
        return []
    with _read_connection(path) as conn:
        if include_groups and _has_deck_groups(conn):
            rows = conn.execute("""
                SELECT decks.document, assignments.group_id FROM personal_decks AS decks
                LEFT JOIN personal_deck_group_assignments AS assignments ON assignments.deck_id=decks.id
                ORDER BY decks.rowid DESC
            """)
        else:
            rows = conn.execute("SELECT document, NULL FROM personal_decks ORDER BY rowid DESC")
        decks = []
        for document, group_id in rows:
            deck = json.loads(document)
            if include_groups:
                deck["group_id"] = group_id
            decks.append(deck)
    needle = str(query).casefold().strip()
    selected = _colors(colors) if colors else []
    return [deck for deck in decks if not deck.get("deleted_at") and needle in deck["name"].casefold()
            and all(c in deck["colors"] for c in selected)]


def _has_deck_groups(conn):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='personal_deck_group_assignments'"
    ).fetchone()
    return row is not None


def list_deck_groups(path):
    if not Path(path).is_file():
        return []
    with _read_connection(path) as conn:
        if not _has_deck_groups(conn):
            return []
        return [{"id": group_id, "name": name} for group_id, name in conn.execute(
            "SELECT id, name FROM personal_deck_groups ORDER BY rowid")]


def _read_deck_group(conn, group_id):
    if not isinstance(group_id, str):
        raise ValueError("Identifiant de section requis.")
    row = conn.execute("SELECT name FROM personal_deck_groups WHERE id=?", (group_id,)).fetchone()
    if row is None:
        raise KeyError("Section de decks introuvable.")
    return {"id": group_id, "name": row[0]}


def create_deck_group(path, name):
    group = {"id": _id(), "name": _text(name, "Nom de section")}
    with _database(path) as conn:
        conn.execute("INSERT INTO personal_deck_groups (id, name) VALUES (?, ?)",
                     (group["id"], group["name"]))
    return group


def rename_deck_group(path, group_id, name):
    name = _text(name, "Nom de section")
    with _database(path) as conn:
        group = _read_deck_group(conn, group_id)
        conn.execute("UPDATE personal_deck_groups SET name=? WHERE id=?", (name, group_id))
        group["name"] = name
    return group


def delete_deck_group(path, group_id):
    with _database(path) as conn:
        group = _read_deck_group(conn, group_id)
        conn.execute("DELETE FROM personal_deck_groups WHERE id=?", (group_id,))
    return group


def move_deck_to_group(path, deck_id, group_id):
    with _database(path) as conn:
        _read(conn, deck_id)
        if group_id is None:
            conn.execute("DELETE FROM personal_deck_group_assignments WHERE deck_id=?", (deck_id,))
        else:
            _read_deck_group(conn, group_id)
            conn.execute("""
                INSERT INTO personal_deck_group_assignments (deck_id, group_id) VALUES (?, ?)
                ON CONFLICT(deck_id) DO UPDATE SET group_id=excluded.group_id
                WHERE group_id<>excluded.group_id
            """, (deck_id, group_id))
    return {"id": deck_id, "group_id": group_id}


def recent_decks(path, limit):
    return sorted(list_decks(path), key=lambda deck: deck.get("updated_at") or "", reverse=True)[:limit]


def delete_deck(path, deck_id, revision):
    if type(revision) is not int or revision < 1:
        raise ValueError("Révision du deck requise.")
    with _database(path) as conn:
        deck = _read(conn, deck_id)
        if revision != deck["revision"]:
            raise RevisionConflict("Ce deck a changé. Rechargez-le avant de réessayer.")
        deck["deleted_at"] = datetime.now(timezone.utc).isoformat()
        deck["revision"] += 1
        return _write(conn, deck)


def restore_deleted_deck(path, deck_id):
    """Recover one deleted composition and retain its deletion in the revision history."""
    with _database(path) as conn:
        deck = _read(conn, deck_id, include_deleted=True)
        if not deck.get("deleted_at"):
            raise ValueError("Ce deck est déjà visible.")
        del deck["deleted_at"]
        deck["revision"] += 1
        return _write(conn, deck)


def save_deck(path, draft):
    if not isinstance(draft, dict):
        raise ValueError("Deck requis.")
    with _database(path) as conn:
        current = _read(conn, draft.get("id"))
        if type(draft.get("revision")) is not int or draft["revision"] != current["revision"]:
            raise RevisionConflict("Ce deck a changé. Rechargez-le avant de réessayer.")
        sections = draft.get("sections")
        if not isinstance(sections, list) or not 1 <= len(sections) <= 100:
            raise ValueError("Conserver de 1 à 100 sections.")
        existing = {c["id"]: c for s in current["sections"] for c in s["cards"]}
        old_sections = {s["id"]: s for s in current["sections"]}
        seen_sections, seen_cards, cleaned = set(), set(), []
        for section in sections:
            if not isinstance(section, dict):
                raise ValueError("Section invalide.")
            if not isinstance(section.get("id"), str):
                raise ValueError("Identifiant de section invalide.")
            sid = str(uuid.UUID(section.get("id", "")))
            if sid in seen_sections or not isinstance(section.get("cards"), list):
                raise ValueError("Section invalide ou dupliquée.")
            old_section = old_sections.get(sid, {})
            color = section.get("color", old_section.get("color"))
            if color is not None and color not in ("W", "U", "B", "R", "G", "C"):
                raise ValueError("Couleur de section invalide.")
            seen_sections.add(sid)
            cards = []
            for card in section["cards"]:
                if not isinstance(card, dict):
                    raise ValueError("Carte invalide.")
                cid = card.get("id")
                quantity = card.get("count")
                if not isinstance(cid, str) or cid not in existing or cid in seen_cards or type(quantity) is not int or not 1 <= quantity <= 9999:
                    raise ValueError("Carte ou quantité invalide ; ajouter une impression depuis sa fiche.")
                seen_cards.add(cid)
                cards.append({**existing[cid], "count": quantity})
            updated = {"id": sid, "name": _text(section.get("name"), "Nom de section"), "cards": cards}
            if "source_section" in old_section:
                updated["source_section"] = old_section["source_section"]
            if color is not None:
                updated["color"] = color
            cleaned.append(updated)
        # Removing a section never implicitly discards its remaining cards.
        for old in current["sections"]:
            if old["id"] not in seen_sections:
                cleaned[0]["cards"].extend(c for c in old["cards"] if c["id"] not in seen_cards)
        current.update(name=_text(draft.get("name"), "Nom"), colors=_colors(draft.get("colors")),
                       sections=cleaned, revision=current["revision"] + 1)
        return _write(conn, current)


def _snapshot(catalog_db, provider_id, printing_id=None):
    if not isinstance(provider_id, str) or not provider_id:
        raise ValueError("Identifiant exact de carte requis.")
    with closing(connect(catalog_db)) as conn:
        row = conn.execute("""SELECT sr.provider_card_id,sr.printing_id,sr.name,
            COALESCE((SELECT COALESCE(fr.printed_name,fr.name) FROM source_records fr
                      WHERE fr.printing_id=sr.printing_id AND fr.lang='fr'
                      ORDER BY fr.provider_card_id LIMIT 1),sr.printed_name) AS printed_name,
            sr.type_line,sr.mana_value,p.collector_number,e.code AS set_code
            FROM source_records sr JOIN printings p USING(printing_id)
            JOIN editions e USING(set_id) WHERE sr.provider_card_id=? AND e.eligibility='eligible'""",
                           (provider_id,)).fetchone()
    if not row or printing_id is not None and row["printing_id"] != printing_id:
        raise ValueError("Impression exacte introuvable dans le catalogue actif.")
    return {**dict(row), "id": _id(), "count": 1}


def _category(card):
    line = card.get("type_line") or ""
    for word, name in (("Token", "Jetons"), ("Creature", "Créatures"), ("Sorcery", "Rituels"),
                       ("Instant", "Éphémères"), ("Artifact", "Artefacts"), ("Enchantment", "Enchantements"),
                       ("Planeswalker", "Planeswalkers"), ("Land", "Terrains")):
        if word in line:
            return name
    return "Deck principal"


def add_card(path, deck_id, catalog_db, provider_id, printing_id=None, section_id=None):
    with _database(path) as conn:
        deck = _read(conn, deck_id)
        card = _snapshot(catalog_db, provider_id, printing_id)
        section = next((s for s in deck["sections"] if s["id"] == section_id), None) if section_id else next(
            (s for s in deck["sections"] if s["name"] == _category(card)), deck["sections"][0])
        if section is None:
            raise ValueError("Section introuvable.")
        same = next((c for c in section["cards"] if c.get("provider_card_id") == provider_id
                     and c.get("printing_id") == card["printing_id"]), None)
        if same:
            if same["count"] >= 9999:
                raise ValueError("Quantité maximale atteinte.")
            same["count"] += 1
        else:
            section["cards"].append(card)
        deck["revision"] += 1
        return _write(conn, deck)


def import_preconstructed(path, source, catalog_db):
    commander = bool((source.get("sections") or {}).get("commander"))
    sections = _new("Import", [], commander=commander)["sections"]
    if commander:
        sections[0]["source_section"] = "commander"
    by_name = {s["name"]: s for s in sections}
    for key, entries in (source.get("sections") or {}).items():
        cards = []
        for entry in entries:
            card = dict(entry)
            if entry.get("provider_card_id"):
                try:
                    card.update(_snapshot(catalog_db, entry["provider_card_id"], entry.get("printing_id")))
                except ValueError:
                    pass
            card.update(id=_id(), count=int(entry.get("count", entry.get("quantity", 1))))
            if card["count"] < 1:
                raise ValueError("Quantité invalide dans le préconstruit.")
            cards.append(card)
        if key == "mainboard":
            for card in cards:
                name = _category(card)
                if name not in by_name:
                    by_name[name] = {"id": _id(), "name": name, "cards": []}
                    sections.append(by_name[name])
                by_name[name]["cards"].append(card)
        elif cards:
            name = SECTION_LABELS.get(key, key)
            if name in by_name:
                by_name[name]["cards"].extend(cards)
                by_name[name]["source_section"] = key
            else:
                section = {"id": _id(), "name": name, "source_section": key, "cards": cards}
                sections.append(section)
                by_name[name] = section
    # Imported decks start with the command zone when one is present.
    sections.sort(key=lambda s: 0 if s.get("source_section") == "commander" else 1)
    colors = source.get("colors") or []
    if isinstance(colors, str):
        colors = list(colors)
    deck = _new(source.get("name_fr") or source.get("name"), colors, sections or None)
    deck["source_deck_id"] = source.get("id")
    with _database(path) as conn:
        return _write(conn, deck)


def _cover_section(deck):
    return next((section for section in deck["sections"] if section["cards"]), None)


def summary_with_media(deck, catalog_db, proxy_lookup=None):
    summary = {key: deck.get(key) for key in ("id", "name", "colors", "revision", "updated_at")}
    if "group_id" in deck:
        summary["group_id"] = deck["group_id"]
    section = _cover_section(deck)
    cover_cards = section["cards"][:1] if section else []
    cover = None
    if cover_cards:
        cover = with_media({"sections": [{**section, "cards": cover_cards}]},
                           catalog_db, proxy_lookup)["cover_card"]
    return {**summary, "cover_card": cover,
            "cover_image_path": cover.get("image_path") if cover else None,
            "card_count": sum(card["count"] for section in deck["sections"] for card in section["cards"])}


def with_media(deck, catalog_db, proxy_lookup=None):
    result = copy.deepcopy(deck)
    # Keep snapshot text if the exact card disappears from a future catalogue.
    enriched = enrich_deck_media({"sections": {s["id"]: s["cards"] for s in result["sections"]}}, catalog_db)
    for section in result["sections"]:
        for card, media in zip(section["cards"], enriched["sections"][section["id"]]):
            card.update({key: value for key, value in media.items() if key.startswith("image_")})
            if proxy_lookup and card.get("set_code") == "proxy":
                linked = proxy_lookup(card.get("provider_card_id"))
                if linked:
                    current, _ = linked
                    for key in ("name", "printed_name", "type_line", "mana_value", "collector_number", "image_url"):
                        card[key] = current.get(key)
                    card.update(image_ready=True, image_status="ready")
    cover_section = _cover_section(result)
    first = cover_section["cards"] if cover_section else []
    result["cover_card"] = copy.deepcopy(first[0]) if first else None
    result["cover_image_path"] = first[0].get("image_path") if first else None
    result["card_count"] = sum(c["count"] for s in result["sections"] for c in s["cards"])
    return result


def export_deck(path, deck_id, catalog_db, desktop=None, proxy_lookup=None):
    deck = get_deck(path, deck_id)
    sections = {}
    for section in deck["sections"]:
        key = "tokens" if section["name"] == "Jetons" or section.get("source_section") == "tokens" else section["id"]
        sections.setdefault(key, []).extend(section["cards"])
    return export_deck_to_desktop({**deck, "sections": sections}, catalog_db, desktop, proxy_lookup=proxy_lookup)
