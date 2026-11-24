"""Small Scryfall ban and restriction snapshot shared by catalogue versions."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

RESTRICTION_FORMATS = (
    "alchemy", "brawl", "commander", "competitivebrawl", "duel", "future",
    "gladiator", "historic", "legacy", "modern", "oathbreaker", "oldschool",
    "pauper", "paupercommander", "penny", "pioneer", "predh", "premodern",
    "standard", "standardbrawl", "timeless", "tlr", "vintage",
)
RESTRICTED_STATUSES = ("banned", "restricted")
SNAPSHOT_NAME = "card-restrictions.json"


def refresh_restrictions(config_path: Path, catalogue_root: Path, archive_sha: str, *, progress=None):
    from ..sources import HttpClient, SourceFormatError, _load_config, atomic_json, iter_api_pages, utc_now

    network = _load_config(config_path, config_path.parent)["network"]
    client = HttpClient(network["user_agent"], min_intervals={
        "api.scryfall.com": network["minimum_scryfall_interval_ms"] / 1000,
    })
    query = " OR ".join(f"{status}:{key}" for status in RESTRICTED_STATUSES
                        for key in RESTRICTION_FORMATS)
    params = urllib.parse.urlencode({"q": query, "unique": "cards", "include_extras": "true"})
    url = f"https://api.scryfall.com/cards/search?{params}"
    by_oracle = {}
    total = None
    count = 0
    if progress:
        progress(0.0, "Vérification Scryfall des interdictions et restrictions…")
    try:
        for _, page in iter_api_pages(client, url, max_pages=12):
            if total is None:
                total = page.get("total_cards")
            if (type(total) is not int or total <= 0 or page.get("total_cards", total) != total
                    or page.get("warnings")):
                raise SourceFormatError("Nombre de cartes incohérent ou recherche partielle")
            for card in page["data"]:
                if not isinstance(card, dict):
                    raise SourceFormatError("Données de restriction invalides")
                oracle_id, legalities = card.get("oracle_id"), card.get("legalities")
                if not isinstance(oracle_id, str) or not oracle_id or not isinstance(legalities, dict):
                    raise SourceFormatError("Carte sans identité Oracle ou statuts de format")
                statuses = {key: legalities[key] for key in RESTRICTION_FORMATS
                            if legalities.get(key) in RESTRICTED_STATUSES}
                if not statuses or (oracle_id in by_oracle and by_oracle[oracle_id] != statuses):
                    raise SourceFormatError("Statuts de restriction inattendus ou contradictoires")
                by_oracle[oracle_id] = statuses
                count += 1
            if progress:
                progress(min(count / total, 0.95), f"{count} / {total} cartes vérifiées")
        if count != total or not by_oracle:
            raise SourceFormatError("Résultats de restrictions incomplets")
    finally:
        client.session.close()

    path = catalogue_root / SNAPSHOT_NAME
    snapshot = {"archive_sha256": archive_sha, "formats": list(RESTRICTION_FORMATS), "cards": by_oracle}
    previous = _read_snapshot(path, archive_sha)
    changed = previous is None or any(previous.get(key) != value for key, value in snapshot.items())
    if progress:
        progress(1.0, "Interdictions et restrictions vérifiées")
    if changed:
        atomic_json(path, {**snapshot, "as_of": utc_now()})
    return {"changed": changed, "cards": len(by_oracle), "network_calls": dict(client.calls)}


def _read_snapshot(path, archive_sha):
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (isinstance(snapshot, dict) and snapshot.get("archive_sha256") == archive_sha
            and isinstance(snapshot.get("cards"), dict) and isinstance(snapshot.get("formats"), list)):
        return snapshot
    return None


def active_restrictions(db: Path, connection):
    path = Path(db).resolve()
    if path.parent.parent.name != "versions":
        return None
    snapshot_path = path.parent.parent.parent / SNAPSHOT_NAME
    if not snapshot_path.is_file():
        return None
    archive = connection.execute("SELECT value FROM catalogue_meta WHERE key='source_archive_sha256'").fetchone()
    return _read_snapshot(snapshot_path, archive[0]) if archive else None
